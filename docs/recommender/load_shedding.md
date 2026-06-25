# Design : Actions de Load Shedding (Effacement de consommation)

> **Version** : 0.2.0 (draft)
> **Auteur** : RTE / Expert Op4Grid Recommender
> **Date** : 2026-03-23

---

> **Voir aussi** : [`renewable_curtailment.md`](renewable_curtailment.md)
> (analogue **amont** — écrêtement de production renouvelable),
> [`superposition_module.md`](superposition_module.md) (estimation d'impact via le
> théorème de superposition généralisé). Index : [`../README.md`](../README.md).

## 1. Objectif

Introduire un nouveau type d'action corrective : le **load shedding** (effacement / délestage de consommation). L'idée est de réduire la consommation sur des noeuds bien choisis du réseau pour ramener le taux de charge (rho) de la ligne contrainte sous son seuil admissible.

---

## 2. Principe de sélection des candidats

### 2.1 Identification des noeuds cibles

Les candidats au load shedding sont les **noeuds en aval de la contrainte** sur le **chemin de contrainte** (edges bleus du graphe de surcharge) :

1. **Extraire le chemin de contrainte** via `g_distribution_graph.get_constrained_edges_nodes()` qui retourne `(lines_constrained_path, nodes_constrained_path, other_blue_edges, other_blue_nodes)`.

2. **Identifier les noeuds aval** via `g_distribution_graph.get_constrained_path().n_aval()`. Ce sont les noeuds situés en aval du goulot d'étranglement (la contrainte, edge noir/jaune).

3. **Filtrer les noeuds avec consommation** : parmi les noeuds aval, ne garder que ceux qui possèdent au moins une charge (load). On utilise `obs.get_obj_connect_to(substation_id=sub_id)` pour récupérer les `loads_id` de chaque poste. Un noeud avec des loads est visuellement représenté en bleu dans le graphe.

### 2.2 Priorisation par report négatif maximum

Parmi les noeuds aval candidats, on les priorise selon la **valeur absolue du report négatif maximum** sur les edges bleus qui y sont connectés :

- Pour chaque noeud candidat, on examine les edges bleus adjacents dans `g_overflow.g`.
- On récupère l'attribut `capacity` (delta de flux en MW) de chaque edge.
- L'edge avec la valeur de `capacity` la plus élevée (en valeur absolue) et un report négatif (flux sortant, signe négatif dans `delta_flows`) indique la direction où la réduction de charge sera la plus influente.
- **Score de priorisation** : on utilise cette valeur absolue du report négatif maximum comme score brut. Plus le report est important, plus le noeud est influent pour soulager la contrainte.

### 2.3 Visualisation dans le graphe de surcharge

```
   [Amont] ──edge bleu──▶ [CONTRAINTE edge noir/jaune] ──edge bleu──▶ [Aval + Load] ← candidat
                                                          ──edge bleu──▶ [Aval sans load] ← non candidat
```

Les noeuds aval avec consommation (bleus visuellement) sont les bons candidats.

---

## 3. Calcul du volume de load shedding (MW)

### 3.1 Volume minimum nécessaire

Pour chaque noeud candidat, on calcule le **volume minimum de réduction** de consommation nécessaire pour ramener la contrainte sous son seuil :

```
P_shedding_min = P_overload_excess * influence_factor * (1 + margin)
```

Avec :
- **`P_overload_excess`** : l'excès de puissance sur la contrainte en MW.
  ```
  P_overload_excess = (rho_max - 1.0) * thermal_limit_MW
  ```
  Où `rho_max` est le rho de la ligne la plus surchargée et `thermal_limit_MW` sa limite thermique en MW (issue de `obs.thermal_limit` ou du facteur monitored).

- **`influence_factor`** : le facteur d'influence du noeud sur la contrainte. C'est le ratio entre la capacité de l'edge bleu reliant le noeud à la contrainte et le flux total sur la contrainte. Plus l'edge est gros en report, plus le facteur est proche de 1.
  ```
  influence_factor = capacity_edge_bleu / max_overload_flow
  ```

- **`margin`** : marge de sécurité de **5%** minimum en plus.
  ```
  margin = 0.05
  ```

### 3.2 Volume effectif de shedding

Le volume effectif proposé est :

```
P_shedding = max(P_shedding_min, seuil_minimum_MW)
```

Avec `seuil_minimum_MW` un seuil configurable (par défaut 1 MW) pour éviter des actions de shedding insignifiantes.

Le volume est **plafonné** par la consommation totale disponible au noeud :

```
P_shedding = min(P_shedding, sum(load_p[loads_on_node]))
```

Si la consommation totale du noeud est insuffisante pour couvrir `P_shedding_min`, le noeud est marqué comme partiellement efficace (le score reflète cette limitation).

---

## 4. Scoring

Le score de chaque action de load shedding est calculé comme suit :

```
score = influence_factor * coverage_ratio
```

Avec :
- **`influence_factor`** : ratio du report négatif de l'edge bleu aval sur le flow max de la contrainte (cf. section 3.1).
- **`coverage_ratio`** : `min(1.0, available_load / P_shedding_min)` — le ratio de couverture de la charge disponible par rapport au besoin minimum. Vaut 1.0 si le noeud a suffisamment de charge.

Le score est dans l'intervalle [0, 1]. Un score de 1.0 signifie que le noeud est parfaitement positionné et dispose d'assez de consommation pour résoudre entièrement la contrainte.

---

## 5. Construction de l'action Grid2Op

L'action de load shedding est une action de type `set_bus` mettant les loads à `-1` (déconnexion) ou, si le backend le supporte, une réduction proportionnelle via `redispatch` ou `curtail`. Dans un premier temps, on utilise la **déconnexion de load** :

```python
action = env.action_space({
    "set_bus": {
        "loads_id": {load_id: -1}  # Déconnecter le load
    }
})
```

Pour un shedding partiel (ne déconnecter qu'une partie de la charge), on sélectionne les loads individuels au noeud, triés par puissance décroissante, jusqu'à atteindre le volume `P_shedding`.

---

## 6. Intégration dans le pipeline

### 6.1 Nouveau type d'action

Ajout de `"load_shedding"` dans le classifier (`classifier.py`) et les règles (`rules.py`).

### 6.2 Méthode de discovery

Nouvelle méthode `find_relevant_load_shedding()` dans `ActionDiscoverer` (`discovery.py`), appelée dans `discover_and_prioritize()` après les actions existantes.

### 6.3 Configuration

Ajout dans `config.py` :
```python
MIN_LOAD_SHEDDING = 0          # Nombre minimum d'actions de load shedding
LOAD_SHEDDING_MARGIN = 0.05    # Marge de sécurité (5%)
LOAD_SHEDDING_MIN_MW = 1.0     # Seuil minimum de shedding en MW
```

### 6.4 Structure du action_scores

```python
"load_shedding": {
    "scores": {action_id: float, ...},   # trié par score décroissant
    "params": {
        action_id: {
            "substation": str,           # nom du poste
            "node_type": "aval",         # toujours aval pour le load shedding
            "influence_factor": float,   # facteur d'influence du noeud
            "P_shedding_MW": float,      # volume de shedding proposé (MW)
            "P_overload_excess_MW": float, # excès de surcharge (MW)
            "available_load_MW": float,  # consommation disponible au noeud (MW)
            "coverage_ratio": float,     # ratio de couverture
            "loads_shed": [str, ...],    # noms des loads délestés
            "assets": {
                "lines": [...],
                "loads": [...],
                "generators": [...]
            }
        }, ...
    },
    "non_convergence": {}
}
```

### 6.5 Règles expertes (rules.py)

- **Pas de load shedding hors du graphe de surcharge** : cohérent avec les règles existantes.
- **Pas de load shedding sur le chemin de dispatch** : on ne déleste que sur les noeuds aval du chemin de contrainte.
- **Pas de load shedding si la consommation au noeud est nulle** : filtré naturellement par l'absence de loads.

---

## 7. Algorithme détaillé

```
ENTRÉES :
  - g_distribution_graph : graphe structuré de surcharge
  - g_overflow : graphe de surcharge (avec attributs edges)
  - obs_defaut : observation après contingence N-1
  - lines_overloaded_ids : indices des lignes en surcharge

ALGORITHME find_relevant_load_shedding :

1. Extraire le constrained_path via g_distribution_graph.get_constrained_path()

2. Récupérer les noeuds aval : nodes_aval = constrained_path.n_aval()

3. Calculer l'excès de surcharge :
   rho_max = max(obs_defaut.rho[lines_overloaded_ids])
   max_overload_flow = capacité de la ligne surchargée sur le graphe
   P_overload_excess = (rho_max - 1.0) * max_overload_flow

4. Pour chaque noeud_aval dans nodes_aval :
   a. Récupérer les loads au noeud via get_obj_connect_to(sub_id)
   b. Si pas de loads → passer au suivant

   c. Calculer available_load = sum(obs_defaut.load_p[load_ids])
   d. Si available_load <= 0 → passer au suivant

   e. Récupérer les edges bleus adjacents au noeud dans g_overflow.g
   f. Trouver l'edge avec le report négatif maximum (|capacity| max)
   g. influence_factor = capacity_edge / max_overload_flow

   h. P_shedding_min = P_overload_excess * influence_factor * (1 + MARGIN)
   i. P_shedding = clamp(P_shedding_min, LOAD_SHEDDING_MIN_MW, available_load)

   j. Sélectionner les loads à déconnecter :
      - Trier les loads par puissance décroissante
      - Accumuler jusqu'à atteindre P_shedding
      - Générer une action de déconnexion par load sélectionné (ou combinée)

   k. Calculer le score :
      coverage_ratio = min(1.0, available_load / P_shedding_min) si P_shedding_min > 0
      score = influence_factor * coverage_ratio

   l. Stocker dans identified_load_shedding, scores, params

5. Retourner les actions triées par score décroissant
```

---

## 8. Fichiers impactés

| Fichier | Modification |
|---------|-------------|
| `config.py` | Ajout `MIN_LOAD_SHEDDING`, `LOAD_SHEDDING_MARGIN`, `LOAD_SHEDDING_MIN_MW` |
| `action_evaluation/classifier.py` | Ajout du type `"load_shedding"` |
| `action_evaluation/rules.py` | Règles de filtrage pour load shedding |
| `action_evaluation/discovery.py` | Méthode `find_relevant_load_shedding()` + intégration dans `discover_and_prioritize()` |
| `main.py` | Mise à jour du retour `action_scores` (si nécessaire, normalement automatique) |
| `CLAUDE.md` | Documentation mise à jour |
| `tests/test_ActionDiscoverer.py` | Tests unitaires pour le load shedding |

---

## 9. Limites et évolutions futures

- **Shedding partiel** : dans cette version, on déconnecte des loads entiers. Une évolution future pourrait ajuster la consommation de manière proportionnelle (via `redispatch` ou un mécanisme backend spécifique).
- **Multi-noeuds** : si un seul noeud ne suffit pas, on pourrait combiner plusieurs noeuds aval pour atteindre le volume nécessaire.
- **Coût économique** : intégrer un coût de délestage pour arbitrer entre load shedding et autres actions correctives.
- **Noeuds amont** : dans certains cas, le délestage de charges en amont pourrait aussi être pertinent (boucles de flux).
