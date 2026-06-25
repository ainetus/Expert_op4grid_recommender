# Architecture pluggable des phases de calcul de manœuvres

> **Objectif** : permettre à des équipes tierces de **plugger leur propre
> algorithme** sur l'une, l'autre ou toutes les phases du passage « topologie
> nodale cible → séquence de manœuvres », sans toucher au cœur du module
> `manoeuvre` ni perdre les vérifications de sûreté.

## 1. Les trois phases de calcul

Le problème complet se décompose en trois calculs, chacun substituable :

```
                    Phase A                          Phase B
topologie nodale ──────────────► topologie ──────────────────► séquence de
cible (partition   identification  détaillée cible   séquencement   manœuvres
des départs en     de topologie    (état OUVERT/                   ordonnées
nœuds électriques) détaillée       FERMÉ de chaque                 (OPEN/CLOSE
                                   organe)                          par organe)
        │                                                              ▲
        └──────────────────────────────────────────────────────────────┘
                                Phase C
                  planification bout-en-bout (directe)
```

| Phase | Entrée | Sortie | Contrat |
|-------|--------|--------|---------|
| **A — Identification** | `PosteTopologique` + `TopologieNodale` cible | `CibleDetaillee` (+ diagnostic) | `IdentificateurTopologieDetaillee` |
| **B — Séquencement** | `PosteTopologique` + `CibleDetaillee` | `ResultatManoeuvres` (séquence ordonnée) | `SequenceurManoeuvres` |
| **C — Bout-en-bout** | `PosteTopologique` + `TopologieNodale` cible | `ResultatPlanification` (séquence **et** détaillée atteinte) | `PlanificateurNodal` |

Un fournisseur d'algorithme implémente **une seule phase, deux, ou les trois**.
L'orchestrateur compose les phases manquantes :

- pas de phase C ? → il enchaîne A puis B ;
- pas de phase A ? → il dérive la cible détaillée de la phase C (rejeu de la
  séquence) ;
- les implémentations natives (portage **libTOPO**) sont enregistrées sous le
  nom `"libtopo"` pour les trois phases et restent le défaut.

## 2. Restructuration du code

La couche est **purement additive** : l'API historique
(`determiner_topo_complete_cible`, `determiner_manoeuvres_cible_detaillee`)
est inchangée ; les adaptateurs natifs sont de minces ponts vers elle.

```
expert_op4grid_recommender/manoeuvre/
├── models.py / graph.py / cellules.py / troncons.py / topologie.py   (inchangés)
├── algo/                          (inchangé : implémentation native libTOPO)
└── plugins/                       (NOUVEAU : couche pluggable)
    ├── interfaces.py     # contrats des 3 phases + type pivot CibleDetaillee
    │                     #   + ResultatIdentification / ResultatPlanification
    ├── registry.py       # registre par phase, décorateur @register,
    │                     #   chargement des plugins externes par entry points
    ├── pipeline.py       # PlanificateurTopologie (façade/orchestrateur)
    │                     #   + verifier_sequence (vérification indépendante)
    └── builtin.py        # adaptateurs "libtopo" (A, B, C) auto-enregistrés
```

Tout est réexporté par `expert_op4grid_recommender.manoeuvre` (et verrouillé
par `tests/manoeuvre/test_public_api.py`).

## 3. Le type pivot : `CibleDetaillee`

La topologie détaillée cible circule entre phases sous une forme unique,
**sérialisable** et indépendante de l'algorithme :

```python
@dataclass
class CibleDetaillee:
    voltage_level_id: str
    etats_organes: dict[str, bool]   # switch_id -> True si OUVERT
```

C'est exactement le format des scénarios sauvegardés par l'IHM
(`scripts/manoeuvre_ihm.py`) et il est convertible depuis/vers le graphe
node/breaker du module :

- `CibleDetaillee.from_graph(G, vl)` — capture l'état d'un graphe ;
- `CibleDetaillee.from_manoeuvres(poste, manoeuvres)` — état atteint par rejeu
  d'une séquence ;
- `cible.to_graph(poste)` — graphe cible (les organes non mentionnés gardent
  leur état courant : une cible **partielle** est valide) ;
- `cible.topologie_nodale(poste)` — partition nodale induite ;
- `cible.diff(autre)`, `cible.organes_inconnus(poste)` — diagnostics.

## 4. Les contrats (modèle d'interface)

Typage **structurel** (`typing.Protocol`, PEP 544) : aucun héritage requis, il
suffit d'exposer la bonne méthode et un attribut `nom`. Les trois contrats
sont définis dans `manoeuvre/plugins/interfaces.py` :

```python
class IdentificateurTopologieDetaillee(Protocol):          # Phase A
    nom: str
    def identifier(self, poste: PosteTopologique, topo_cible: TopologieNodale,
                   **options) -> ResultatIdentification: ...

class SequenceurManoeuvres(Protocol):                      # Phase B
    nom: str
    def sequencer(self, poste: PosteTopologique, cible: CibleDetaillee,
                  **options) -> ResultatManoeuvres: ...

class PlanificateurNodal(Protocol):                        # Phase C
    nom: str
    def planifier(self, poste: PosteTopologique, topo_cible: TopologieNodale,
                  **options) -> ResultatPlanification: ...
```

Règles communes (détaillées dans les docstrings) :

1. **Ne jamais muter** `poste` ni `poste.graph` (travailler sur copie —
   invariant clé du module, cf. `manoeuvre/CLAUDE.md`) ;
2. retourner toujours la structure de résultat (jamais `None`) — la
   dégradation gracieuse passe par `is_realisable=False` / `message` /
   `noeuds_non_realisables` ;
3. accepter `**options` et **ignorer les options inconnues** (p. ex.
   `mode="smooth"|"aggressive"` est une option du séquenceur natif) ;
4. les verdicts déclarés ne sont **pas crus sur parole** : l'orchestrateur
   revérifie tout (cf. §6).

Structures de résultat :

- `ResultatManoeuvres` (existant, `algo/results.py`) : séquence ordonnée de
  `Manoeuvre(switch_id, action, raison)` + verdicts (`is_verified`,
  `is_verified_detaillee`, `ecarts`, `alertes`, `noeuds_non_realisables`…) ;
- `ResultatIdentification` : `cible` (+ `is_realisable`, diagnostics, et un
  champ optionnel `sequence` si l'algorithme l'a produite en sous-produit) ;
- `ResultatPlanification` : `cible_detaillee` + `sequence` (avec délégations
  de confort `is_verified`, `nb_manoeuvres`…).

## 5. L'orchestrateur : `PlanificateurTopologie`

Façade unique pour les consommateurs (IHM, recommandeur, notebooks) :

```python
from expert_op4grid_recommender.manoeuvre import PlanificateurTopologie

pipe = PlanificateurTopologie()                 # tout libTOPO (défaut)

# Phase A seule : nodale -> détaillée
ident = pipe.identifier_topologie_detaillee(poste, topo_cible)

# Phase B seule : détaillée -> séquence (cible = CibleDetaillee, graphe,
# ou dict switch_id -> ouvert)
seq = pipe.sequencer(poste, ident.cible, mode="smooth")

# Phase C : nodale -> séquence + détaillée (directe ou composée A+B)
plan = pipe.planifier(poste, topo_cible)
plan.cible_detaillee     # CibleDetaillee atteinte
plan.sequence.manoeuvres # séquence ordonnée
```

Chaque phase se configure par **nom du registre**, **instance**, ou `None`
(= phase désactivée, l'orchestrateur compose) :

```python
# Mon séquenceur, identification libTOPO conservée (planifier => A puis B) :
pipe = PlanificateurTopologie(sequenceur=MonSequenceur(), planificateur=None)

# Mon identificateur seul (la séquence reste libTOPO) :
pipe = PlanificateurTopologie(identificateur="mon_ident", planificateur=None)

# Mon algo bout-en-bout :
pipe = PlanificateurTopologie(planificateur="mon_algo")
```

## 6. Vérification indépendante (sûreté uniforme)

`verifier_sequence(poste, res, topo_cible=None, cible=None, mode=None)`
recalcule les verdicts **par rejeu** sur une copie du graphe, quel que soit
l'algorithme :

- `topo_obtenue` / `is_verified` : la partition nodale atteinte est comparée à
  la cible (`meme_topologie`, isomorphisme de partition) ;
- `ecarts` : organes **inconnus du poste**, écarts détaillés vs la cible
  (barre câblée de chaque départ, état des couplers et DJ — réutilise
  `_ecarts_detailles`), et **règle du sectionneur** (`_verifier_regles` :
  jamais de SA manœuvré sous charge) ;
- `is_verified_detaillee` : nodale atteinte **et** zéro écart ;
- `alertes` : « un seul ouvrage temporairement hors tension à la fois »
  (`ouvrages_simultanement_hors_tension`), sauf `mode="aggressive"` qui
  dé-énergise en lot par construction.

La façade l'applique par défaut (`verification_independante=True`) : un plugin
qui déclare `is_verified=True` à tort est **démasqué** (testé par
`test_verification_independante_demasque_un_plugin`). Les algorithmes tiers
bénéficient ainsi gratuitement des mêmes verdicts et garde-fous que
l'implémentation native — et le pattern **transactionnel** du module (réaliser
plusieurs candidats, retenir le meilleur vérifié) s'étend naturellement à des
candidats venus de plugins différents.

## 7. Enregistrer et distribuer un plugin

### Dans le même process (application, notebook, tests)

```python
from expert_op4grid_recommender.manoeuvre.plugins import register

@register("sequenceur", "mon_algo")
class MonSequenceur:
    nom = "mon_algo"

    def sequencer(self, poste, cible, **options):
        ...
        return ResultatManoeuvres(...)
```

`register(phase, nom)` refuse les doublons (sauf `remplacer=True`) ;
`disponibles()` liste les algorithmes par phase ; `get(phase, nom)` instancie.

### Par paquet externe (entry points)

Un paquet tiers se déclare dans son `pyproject.toml`, **sans modifier ce
dépôt** — le nom de l'entry point encode la phase (`<phase>.<nom>`) :

```toml
[project.entry-points."expert_op4grid_recommender.manoeuvre"]
"sequenceur.mon_algo"     = "mon_pkg.sequenceur:MonSequenceur"
"planificateur.mon_algo"  = "mon_pkg.planif:MonPlanificateur"
```

Les entry points sont chargés paresseusement au premier `get()` /
`disponibles()` ; un plugin cassé est ignoré avec un warning (il ne bloque ni
les autres plugins ni les natifs).

## 8. Intégration aux consommateurs existants

- **IHM** (`scripts/manoeuvre_ihm.py`) : **migrée sur la façade**.
  `/api/nodale_to_detaillee` appelle `pipe.identifier_topologie_detaillee`
  (phase A) et `/api/sequence` appelle `pipe.sequencer` (phase B), avec
  l'algorithme **sélectionné par phase** dans l'interface (sélecteurs « Algo »
  du volet nodal et du panneau Séquence, alimentés par `GET /api/algos` ;
  sélection via `POST /api/algos`). Les plugins tiers enregistrés (registre /
  entry points) y apparaissent automatiquement ; les payloads portent l'`algo`
  utilisé et les verdicts affichés sont ceux de la vérification indépendante.
  Tests : `tests/manoeuvre/test_ihm_algo_selection.py`.
- **Recommandeur** : une action nodale priorisée (partition `set_bus` /
  groupes de départs) se convertit en cible via
  `TopologieNodale.from_node_groups(vl, groups)` ou `from_bus_assignment`,
  puis `pipe.planifier(poste, topo_cible)` fournit la séquence opérationnelle
  et la topologie détaillée associées à l'action.

## 9. Tests

`tests/manoeuvre/test_plugins_interface.py` couvre : registre (natifs,
doublons, erreurs), aller-retour `CibleDetaillee`, les trois phases sur le
poste réel CARRIP3, la composition A+B, l'injection par nom, la détection d'un
plugin mensonger et d'un organe inconnu. Le verrou de surface publique
(`test_public_api.py`) intègre les nouveaux symboles.
