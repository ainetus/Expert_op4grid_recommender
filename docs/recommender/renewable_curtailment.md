# Design : Actions d'Effacement de Production Renouvelable (Éolien/Solaire)

> **Version** : 0.2.0 (draft)
> **Auteur** : RTE / Expert Op4Grid Recommender
> **Date** : 2026-03-27

---

## 1. Objectif

Introduire un nouveau type d'action corrective : la **déconnexion de production renouvelable** (éolien ou solaire). L'idée est de réduire la production renouvelable sur des nœuds bien choisis du réseau, **en amont de la contrainte** sur le chemin contraint, pour diminuer le flux transitant sur la ligne surchargée et ramener le taux de charge (rho) sous son seuil admissible.

Ce mécanisme est l'analogue **amont** du load shedding (effacement de consommation, voir `load_shedding.md`) qui cible les nœuds **aval**. Les deux se complètent :

| | Load Shedding | Renewable Curtailment |
|---|---|---|
| **Côté** | Aval (downstream) | Amont (upstream) |
| **Levier** | Réduire la consommation | Réduire la production renouvelable |
| **Nœuds cibles** | `constrained_path.n_aval()` | `constrained_path.n_amont()` |
| **Edges bleus ciblés** | Edges bleus en aval de la contrainte | Edges bleus en amont de la contrainte (flux négatifs) |
| **Action Grid2Op** | `set_bus: {loads_id: {name: -1}}` | `set_bus: {generators_id: {name: -1}}` |

---

## 2. Principe de sélection des candidats

### 2.1 Identification des nœuds cibles

Les candidats à l'effacement renouvelable sont les **nœuds en amont de la contrainte** sur le **chemin de contrainte** (edges bleus du graphe de surcharge) :

1. **Extraire le chemin de contrainte** via `g_distribution_graph.get_constrained_edges_nodes()` qui retourne `(lines_constrained_path, nodes_constrained_path, other_blue_edges, other_blue_nodes)`.

2. **Identifier les nœuds amont** via `g_distribution_graph.get_constrained_path().n_amont()`. Ce sont les nœuds situés en amont du goulot d'étranglement (la contrainte, edge noir/jaune).

3. **Filtrer les nœuds avec production renouvelable** : parmi les nœuds amont, ne garder que ceux qui possèdent au moins un générateur de type **WIND** ou **SOLAR** (identifié via la colonne `energy_source` du DataFrame pypowsybl `network.get_generators()`). Un nœud sans production renouvelable n'est pas candidat.

### 2.2 Identification du type de générateur (renouvelable)

Le type de générateur est déterminé via la colonne `energy_source` fournie par pypowsybl (`network.get_generators()`). Les valeurs reconnues comme renouvelables sont :

- `"WIND"` — Éolien
- `"SOLAR"` — Solaire

Les autres types (`HYDRO`, `THERMAL`, `NUCLEAR`, `OTHER`) ne sont **pas** ciblés par cette action car leur déconnexion a des implications opérationnelles et économiques très différentes.

**Implémentation** : une nouvelle propriété `gen_renewable` (ou méthode `is_gen_renewable()`) sera ajoutée à l'observation / network manager, retournant un tableau booléen indiquant pour chaque générateur s'il est renouvelable. Le mapping `energy_source → is_renewable` sera configurable via `config.py`.

### 2.3 Priorisation par report négatif maximum

Même logique que le load shedding, mais sur les nœuds **amont** :

- Pour chaque nœud amont candidat, on examine les **edges bleus adjacents** dans `g_overflow.g`.
- On récupère les flux négatifs (attribut `label` < 0) sur ces edges.
- `influence_flow = max(sum_neg_in_edges, sum_neg_out_edges)` — somme des flux négatifs entrants vs sortants.
- `influence_factor = min(1.0, influence_flow / max_overload_flow)` — ratio d'influence du nœud sur la contrainte.

### 2.4 Visualisation dans le graphe de surcharge

```
   [Amont + Renouvelable] ← candidat
          │
    edge bleu (flux négatif)
          │
          ▼
   [CONTRAINTE edge noir/jaune]
          │
    edge bleu
          │
          ▼
   [Aval + Load] ← candidat load shedding
```

Les nœuds amont avec production renouvelable sont les candidats pour l'effacement renouvelable.

---

## 3. Calcul du volume d'effacement (MW)

### 3.1 Volume minimum nécessaire

Même formulation que le load shedding, adaptée à la production :

```
P_curtailment_min = P_overload_excess / influence_factor * (1 + margin)
```

Avec :
- **`P_overload_excess`** : l'excès de puissance sur la contrainte en MW.
  ```
  P_overload_excess = (rho_max - 1.0) * thermal_limit_MW
  ```
  Où `rho_max` est le rho de la ligne la plus surchargée et `thermal_limit_MW` sa limite thermique.

- **`influence_factor`** : le facteur d'influence du nœud sur la contrainte (cf. section 2.3).

- **`margin`** : marge de sécurité (par défaut **5%**, configurable via `RENEWABLE_CURTAILMENT_MARGIN`).

### 3.2 Volume effectif d'effacement

```
P_curtailment = max(P_curtailment_min, seuil_minimum_MW)
P_curtailment = min(P_curtailment, available_renewable_gen)
```

Avec :
- `seuil_minimum_MW` : seuil configurable (par défaut 1 MW via `RENEWABLE_CURTAILMENT_MIN_MW`) pour éviter des actions insignifiantes.
- `available_renewable_gen` : somme de la production renouvelable active (gen_p > 0) au nœud.

Si la production renouvelable au nœud est insuffisante pour couvrir `P_curtailment_min`, le nœud est marqué comme partiellement efficace (le score reflète cette limitation).

---

## 4. Scoring

Le score de chaque action d'effacement renouvelable est :

```
score = influence_factor * coverage_ratio
```

Avec :
- **`influence_factor`** : ratio du report négatif sur les edges bleus amont / flow max de la contrainte (cf. section 2.3).
- **`coverage_ratio`** : `min(1.0, available_renewable_gen / P_curtailment_min)` — le ratio de couverture de la production renouvelable disponible par rapport au besoin minimum. Vaut 1.0 si le nœud a suffisamment de production renouvelable.

Le score est dans l'intervalle **[0, 1]**. Un score de 1.0 signifie que le nœud est parfaitement positionné et dispose d'assez de production renouvelable pour résoudre entièrement la contrainte.

---

## 5. Construction de l'action Grid2Op

L'action d'effacement est une action `set_bus` déconnectant le générateur renouvelable :

```python
action = env.action_space({
    "set_bus": {
        "generators_id": {gen_name: -1}  # Déconnecter le générateur
    }
})
```

**Une action par générateur renouvelable** au nœud, triés par puissance décroissante. Chaque action est un candidat autonome (comme pour le load shedding).

---

## 6. Intégration dans le pipeline

### 6.1 Nouveau type d'action

- Ajout de `"open_gen"` dans le classifier (`classifier.py`) pour la détection de déconnexion de générateur.
- Ajout de `"renewable_curtailment"` comme catégorie d'action dans le scoring.

### 6.2 Méthode de discovery

Nouvelle méthode `find_relevant_renewable_curtailment(nodes_amont_indices)` dans `ActionDiscoverer` (`discovery.py`), appelée dans `discover_and_prioritize()` après le load shedding, sur les nœuds **amont**.

### 6.3 Configuration

Ajout dans `config.py` :
```python
MIN_RENEWABLE_CURTAILMENT = 0              # Nombre minimum d'actions d'effacement renouvelable
RENEWABLE_CURTAILMENT_MARGIN = 0.05        # Marge de sécurité (5%)
RENEWABLE_CURTAILMENT_MIN_MW = 1.0         # Seuil minimum d'effacement en MW
RENEWABLE_ENERGY_SOURCES = ["WIND", "SOLAR"]  # Types de générateurs ciblés
```

### 6.4 Structure du action_scores

```python
"renewable_curtailment": {
    "scores": {action_id: float, ...},   # trié par score décroissant
    "params": {
        action_id: {
            "substation": str,              # nom du poste
            "node_type": "amont",           # toujours amont pour l'effacement renouvelable
            "generator_name": str,          # nom du générateur
            "energy_source": str,           # "WIND" ou "SOLAR"
            "influence_factor": float,      # facteur d'influence du nœud
            "in_negative_flows": float,     # somme des flux négatifs entrants (edges bleus)
            "out_negative_flows": float,    # somme des flux négatifs sortants (edges bleus)
            "P_curtailment_MW": float,      # volume d'effacement proposé (MW)
            "P_overload_excess_MW": float,  # excès de surcharge (MW)
            "available_gen_MW": float,      # production renouvelable disponible au nœud (MW)
            "coverage_ratio": float,        # ratio de couverture
            "generators_curtailed": [str],  # noms des générateurs effacés
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

- **Pas d'effacement hors du graphe de surcharge** : cohérent avec les règles existantes.
- **Effacement uniquement sur le chemin de contrainte amont** : on n'efface que sur les nœuds amont.
- **Pas d'effacement si la production renouvelable au nœud est nulle** : filtré naturellement.
- **Pas d'effacement des générateurs non-renouvelables** : seuls WIND et SOLAR sont ciblés.

---

## 7. Algorithme détaillé

```
ENTRÉES :
  - g_distribution_graph : graphe structuré de surcharge
  - g_overflow : graphe de surcharge (avec attributs edges)
  - obs_defaut : observation après contingence N-1
  - lines_overloaded_ids : indices des lignes en surcharge
  - gen_energy_sources : mapping gen_name → energy_source (depuis pypowsybl)

ALGORITHME find_relevant_renewable_curtailment :

1. Extraire le constrained_path via g_distribution_graph.get_constrained_path()

2. Récupérer les nœuds amont : nodes_amont = constrained_path.n_amont()

3. Calculer l'excès de surcharge :
   rho_max = max(obs_defaut.rho[lines_overloaded_ids])
   max_overload_flow = capacité de la ligne surchargée sur le graphe
   P_overload_excess = (rho_max - 1.0) * max_overload_flow

4. Pour chaque noeud_amont dans nodes_amont :
   a. Récupérer les generators au nœud via get_obj_connect_to(sub_id)
   b. Filtrer : ne garder que les generators renouvelables
      (energy_source in RENEWABLE_ENERGY_SOURCES)
   c. Si pas de generators renouvelables → passer au suivant

   d. Calculer available_renewable_gen = sum(obs_defaut.gen_p[gen_ids])
      (uniquement les generators renouvelables avec gen_p > 0)
   e. Si available_renewable_gen <= 0 → passer au suivant

   f. Récupérer les edges bleus adjacents au nœud dans g_overflow.g
   g. Calculer influence via somme des flux négatifs :
      total_neg_in = sum(|label| pour edges entrants bleus avec label < 0)
      total_neg_out = sum(|label| pour edges sortants bleus avec label < 0)
      influence_flow = max(total_neg_in, total_neg_out)
   h. influence_factor = min(1.0, influence_flow / max_overload_flow)

   i. P_curtailment_min = P_overload_excess / influence_factor * (1 + MARGIN)
   j. P_curtailment = clamp(P_curtailment_min, MIN_MW, available_renewable_gen)

   k. Sélectionner les generators à déconnecter :
      - Trier les generators renouvelables par puissance décroissante
      - Générer une action de déconnexion par générateur

   l. Calculer le score par générateur :
      gen_coverage = min(1.0, gen_power / P_curtailment_min)
      score = influence_factor * gen_coverage

   m. Stocker dans identified_renewable_curtailment, scores, params

5. Retourner les actions triées par score décroissant
```

---

## 8. Données nécessaires : identification des renouvelables

### 8.1 Via pypowsybl (backend pypowsybl)

pypowsybl expose la colonne `energy_source` dans `network.get_generators()` avec les valeurs :
`HYDRO`, `NUCLEAR`, `WIND`, `THERMAL`, `SOLAR`, `OTHER`.

**Implémentation** :
- Dans `NetworkManager.__init__()`, cacher le mapping `gen_name → energy_source` depuis `self._cached_gen_df['energy_source']`.
- Exposer une propriété `gen_energy_source` (np.ndarray de strings) dans l'observation.
- Ajouter une propriété `gen_renewable` (np.ndarray booléen) : `True` si `energy_source in RENEWABLE_ENERGY_SOURCES`.

### 8.2 Via Grid2Op (backend grid2op)

Grid2Op expose `env.gen_type` (ou `obs.gen_type` selon la version) qui contient des strings comme `"wind"`, `"solar"`, `"thermal"`, `"nuclear"`, `"hydro"`.

**Implémentation** :
- Vérifier `hasattr(obs, 'gen_type')` ou `hasattr(env, 'gen_type')`.
- Mapping : `gen_type.lower() in ["wind", "solar"]` → renouvelable.
- Fallback si `gen_type` non disponible : marquer tous les générateurs comme non-renouvelables (pas d'effacement proposé).

---

## 9. Fichiers impactés

| Fichier | Modification |
|---------|-------------|
| `config.py` | Ajout `MIN_RENEWABLE_CURTAILMENT`, `RENEWABLE_CURTAILMENT_MARGIN`, `RENEWABLE_CURTAILMENT_MIN_MW`, `RENEWABLE_ENERGY_SOURCES` |
| `pypowsybl_backend/network_manager.py` | Cache `energy_source` par générateur, propriété `gen_energy_source` |
| `pypowsybl_backend/observation.py` | Propriétés `gen_energy_source`, `gen_renewable` |
| `action_evaluation/classifier.py` | Ajout du type `"open_gen"` (déconnexion générateur) |
| `action_evaluation/rules.py` | Règles de filtrage pour effacement renouvelable (bypass comme load shedding) |
| `action_evaluation/discovery.py` | Méthode `find_relevant_renewable_curtailment()` + intégration dans `discover_and_prioritize()` |
| `main.py` | Mise à jour du retour `action_scores` (automatique via discover_and_prioritize) |
| `CLAUDE.md` | Documentation mise à jour |
| `tests/test_ActionDiscoverer.py` | Tests unitaires pour l'effacement renouvelable |

---

## 10. Limites et évolutions futures

- **Effacement partiel** : dans cette version, on déconnecte des générateurs entiers. Une évolution future pourrait utiliser le `curtail` (si supporté par le backend) pour réduire la production sans déconnecter complètement.
- **Multi-nœuds** : si un seul nœud ne suffit pas, on pourrait combiner plusieurs nœuds amont pour atteindre le volume nécessaire.
- **Coût économique** : intégrer un coût d'effacement (prix du MWh éolien/solaire perdu) pour arbitrer entre effacement renouvelable et autres actions correctives.
- **Extension aux autres types** : dans certains contextes, l'effacement de production hydraulique ou thermique pourrait aussi être pertinent.
- **Combinaison avec load shedding** : proposer des combinaisons effacement amont + délestage aval pour les cas où ni l'un ni l'autre ne suffit seul.
