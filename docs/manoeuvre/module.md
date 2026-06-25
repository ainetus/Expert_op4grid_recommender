# Module Manoeuvre — Documentation

> **Package** : `expert_op4grid_recommender.manoeuvre`
> **Origine** : Portage Python de la bibliothèque C++ `libTOPO` (projet TOPO Apogee, RTE)
> **Backend reseau** : pypowsybl (topologie NODE_BREAKER)
> **Statut** : Etapes 1.1 a 1.6 + phase 2 (sequencement) implementees ; IHM de test
> disponible. Campagne d'optimisation a iso-comportement : voir
> [`manoeuvre_optimisations.md`](optimisations.md).

---

## 1. Objectif

Le module **manoeuvre** analyse la topologie detaillee des postes electriques
au format IIDM/CGMES (via pypowsybl). A partir du graphe node/breaker d'un
voltage level, il identifie :

- les **cellules de depart** : sous-graphes reliant un equipement (ligne,
  transformateur, groupe, charge...) a ses jeux de barres accessibles ;
- les **cellules de couplage** : organes de coupure reliant deux sections
  de jeux de barres entre elles.

Ces structures sont les briques de base de l'algorithme *nodale -> detaillee*
qui, a terme, generera les manoeuvres topologiques candidates pour le
recommandeur expert.

---

## 2. Pipeline

```
  reseau pypowsybl (.xiidm / .cgmes)
          |
          v
  [Etape 1.1]  build_vl_graph()          graph.py
          |    Extraction du graphe NX
          |    (noeuds = connectivity nodes,
          |     aretes = switches + internal connections)
          v
  [Etape 1.2]  detecter_cellules()       cellules.py
          |    Detection structurelle BFS
          |    + connectivite electrique
          v
  CellulesVL
    .cellules_depart     : list[CelluleDepart]
    .cellules_couplage   : list[CelluleCouplage]
    .noeuds_non_attribues: set[int]
          |
          v
  [Etapes 1.3+]  (a venir)
    troncons.py   — Attribution des noeuds, tronconnement
    topologie.py  — TopologieNodale, PosteTopologique
    algo/         — Algorithme nodale -> detaillee (package en couches)
```

---

## 3. Architecture des fichiers

```
expert_op4grid_recommender/manoeuvre/
  __init__.py      Public API (19 symboles exportes)
  models.py        Enumerations et dataclasses partagees
  graph.py         Etape 1.1 — construction du graphe NetworkX
  cellules.py      Etape 1.2 — detection des cellules

tests/manoeuvre/
  fixture_loader.py       Chargement des fixtures JSON (sans pypowsybl)
  fixtures/               Topologies reelles RTE serialisees
    index.json            Index des fixtures disponibles
    CARRIP3.json, ...     15 voltage levels de postes RTE
  test_graph_cellules.py  Tests unitaires (reseau 4-postes pypowsybl)
  test_postes_reels.py    Tests parametrises sur postes RTE reels

scripts/
  extract_test_fixtures.py  Extraction de fixtures depuis un .xiidm
```

---

## 4. Modeles de donnees (`models.py`)

### Enumerations

| Enum            | Valeurs principales                                       | Usage                              |
|-----------------|-----------------------------------------------------------|-------------------------------------|
| `NodeType`      | `BUSBAR_SECTION`, `EQUIPMENT`, `INTERNAL`                 | Type de noeud dans le graphe NX     |
| `EquipmentType` | `LOAD`, `GENERATOR`, `LINE_SIDE1/2`, `TRANSFORMER_SIDE1/2`, `SHUNT_COMPENSATOR`, `BATTERY`, ... (11 valeurs) | Type d'equipement                   |
| `SwitchKind`    | `BREAKER` (DJ), `DISCONNECTOR` (SA), `LOAD_BREAK_SWITCH`, `INTERNAL` | Type d'organe de coupure            |
| `CelluleType`   | `DEPART`, `COUPLAGE`, `INTERNE`                           | Classification des cellules         |

### Dataclasses

```python
@dataclass
class NodeAttrs:
    node_type: NodeType
    busbar_section_id: Optional[str]   # si BUSBAR_SECTION
    equipment_id: Optional[str]        # si EQUIPMENT
    equipment_type: Optional[EquipmentType]

@dataclass
class EdgeAttrs:
    switch_id: Optional[str]           # None pour internal connection
    kind: SwitchKind
    open: bool                         # True = non conducteur
    # Proprietes : is_closed, is_breaker, is_disconnector, is_internal
```

---

## 5. Etape 1.1 — Construction du graphe (`graph.py`)

### Fonction principale

```python
build_vl_graph(network: pp.network.Network, voltage_level_id: str) -> nx.Graph
```

Construit un graphe non oriente dont :
- **Noeuds** = connectivity nodes IIDM (entiers) ;
- **Aretes** = switches (BREAKER, DISCONNECTOR, LOAD_BREAK_SWITCH) et internal connections.

#### Deroulement

1. `_assert_node_breaker()` — verifie que le VL est en topologie NODE_BREAKER.
2. `_add_switch_edges()` — ajoute les aretes switch avec attributs `kind`, `open`, `switch_id`.
3. `_add_internal_connection_edges()` — ajoute les connexions internes (toujours fermees, `kind=INTERNAL`).
4. `_tag_busbar_section_nodes()` — marque les noeuds SJB (`node_type=BUSBAR_SECTION`).
5. `_tag_equipment_nodes()` — marque les bornes d'equipements (`node_type=EQUIPMENT`).

#### Equipements traites

| Categorie    | Getters pypowsybl                                               | Noeuds                |
|-------------|------------------------------------------------------------------|-----------------------|
| Injections   | `get_loads`, `get_generators`, `get_shunt_compensators`, `get_static_var_compensators`, `get_batteries`, `get_dangling_lines` | 1 noeud (`node`)      |
| Branches     | `get_lines`, `get_2_windings_transformers`                       | 2 noeuds (`node1`, `node2`) |

> **Note** : tous les appels utilisent `all_attributes=True` pour acceder aux
> colonnes `node`, `node1`, `node2`, `topology_kind` qui ne sont pas exposees
> par defaut dans pypowsybl.

### Fonctions utilitaires

```python
busbar_nodes(G) -> list[int]       # Noeuds BUSBAR_SECTION
equipment_nodes(G) -> list[int]    # Noeuds EQUIPMENT
get_node_attrs(G, node) -> NodeAttrs
get_edge_attrs(G, u, v) -> EdgeAttrs
```

### Exception

```python
class TopologyError(Exception):
    """Leve si le VL n'est pas en NODE_BREAKER ou si l'API ne retourne pas
    les colonnes attendues."""
```

---

## 6. Etape 1.2 — Detection des cellules (`cellules.py`)

### Fonction principale

```python
detecter_cellules(G: nx.Graph, voltage_level_id: str) -> CellulesVL
```

#### Algorithme en 3 phases

**Phase A — Cellules de depart** (BFS structurel)

Pour chaque noeud `EQUIPMENT` :
1. BFS depuis le noeud d'equipement, traversant **tous** les switches
   (ouverts ou fermes) et internal connections ;
2. Arret a chaque noeud `BUSBAR_SECTION` atteint (sans le traverser) ;
3. Les noeuds visites forment le sous-graphe de la cellule.

Si deux cellules partagent des noeuds intermediaires (topologie omnibus),
elles sont fusionnees et marquees via `shared_equipment_ids`.

**Phase B — Cellules de couplage**

1. Graphe restreint = noeuds SJB + noeuds internes non attribues en phase A ;
2. Composantes connexes avec >= 2 noeuds SJB = cellules de couplage ;
3. Le disjoncteur principal est identifie (`main_breaker`).

**Phase C — Noeuds non attribues**

Noeuds restants (ni depart, ni couplage) — typiquement neutres ou reserves.

### Classes de sortie

#### `CelluleDepart`

```python
@dataclass
class CelluleDepart:
    equipment_id: str
    equipment_type: EquipmentType
    all_nodes: set[int]
    busbar_nodes: set[int]           # accessibilite structurelle
    switches: list[SwitchInfo]
    connected_busbars: set[int]      # accessibilite electrique (switches fermes)
    shared_equipment_ids: set[str]   # vide sauf cellule omnibus
    subgraph: Optional[nx.Graph]
```

Proprietes notables :

| Propriete                 | Type         | Description                                              |
|---------------------------|-------------|----------------------------------------------------------|
| `nb_barres_accessibles`   | `int`        | Nombre de SJB atteignables (structurellement)             |
| `is_connected`            | `bool`       | Au moins une barre connectee electriquement               |
| `is_multiple`             | `bool`       | Cellule partagee (omnibus)                                |
| `breakers`                | `list`       | Disjoncteurs (DJ) de la cellule                           |
| `disconnectors`           | `list`       | Sectionneurs (SA) de la cellule                           |
| `is_reaiguillage`         | `bool`       | Cellule de re-aiguillage (1 seul SA, pas de DJ)           |
| `busbar_section_ids`      | `set[str]`   | IDs (string) des SJB atteintes                            |
| `connected_busbar_section_ids` | `set[str]` | IDs des SJB effectivement connectees                 |

#### `CelluleCouplage`

```python
@dataclass
class CelluleCouplage:
    switches: list[SwitchInfo]
    busbar_node_1: int
    busbar_node_2: int
    subgraph: Optional[nx.Graph]
```

| Propriete      | Type   | Description                               |
|----------------|--------|-------------------------------------------|
| `is_closed`    | `bool` | Tous les OC actifs fermes                 |
| `main_breaker` | `Optional[SwitchInfo]` | DJ principal du couplage |

#### `CellulesVL`

```python
@dataclass
class CellulesVL:
    voltage_level_id: str
    cellules_depart: list[CelluleDepart]
    cellules_couplage: list[CelluleCouplage]
    noeuds_non_attribues: set[int]
```

| Methode                          | Retour                  | Description                                |
|----------------------------------|-------------------------|--------------------------------------------|
| `get_cellule_depart(eq_id)`      | `Optional[CelluleDepart]` | Recherche par ID d'equipement           |
| `resume()`                       | `str`                   | Chaine de synthese lisible                 |

### Connectivite electrique

```python
calculer_connected_busbars(cellule: CelluleDepart) -> set[int]
```

Calcule les SJB connectees via un chemin de switches **tous fermes**
(composante connexe dans le sous-graphe restreint aux aretes fermees).
Met a jour `cellule.connected_busbars` en place.

---

## 7. Concepts cles du domaine

### Structurel vs. Electrique

| Dimension     | Traversee             | Resultat                  | Usage                              |
|---------------|----------------------|---------------------------|------------------------------------|
| **Structurelle** | Tous switches (ouverts/fermes) | `busbar_nodes`          | Quelles barres l'equipement *peut* atteindre |
| **Electrique**   | Uniquement switches fermes     | `connected_busbars`     | Quelles barres l'equipement *atteint actuellement* |

### Topologies RTE specifiques

#### Re-aiguillage

Raccordement direct d'un equipement a une barre via un unique sectionneur
d'aiguillage (SA), sans disjoncteur propre. Identifiable via :
- `CelluleDepart.is_reaiguillage == True`
- 1 seul switch de type `DISCONNECTOR`, 0 `BREAKER`
- Nommage typique : `*SA_F` (sectionneur d'aiguillage, fictif)

#### Cellule omnibus / departs multiples

Plusieurs equipements partagent les memes noeuds intermediaires et le meme
disjoncteur. Identifies via `CelluleDepart.is_multiple` et
`shared_equipment_ids`.

#### Postes >= 3 barres

Les cellules de couplage avec > 2 SJB emettent un warning. Seules les
2 premieres SJB sont enregistrees dans `busbar_node_1`/`busbar_node_2`.

---

## 8. Usage rapide

```python
import pypowsybl as pp
from expert_op4grid_recommender.manoeuvre import (
    build_vl_graph,
    detecter_cellules,
    busbar_nodes,
    equipment_nodes,
)

# Charger un reseau
network = pp.network.load("grid.xiidm")

# Construire le graphe d'un voltage level
G = build_vl_graph(network, "CARRIP3")

# Detecter les cellules
cellules = detecter_cellules(G, "CARRIP3")
print(cellules.resume())

# Inspecter une cellule de depart
for c in cellules.cellules_depart:
    print(f"{c.equipment_id} -> {c.nb_barres_accessibles} barres, "
          f"connecte={c.is_connected}, reaig={c.is_reaiguillage}")

# Inspecter les couplages
for coup in cellules.cellules_couplage:
    print(f"Couplage {coup.busbar_node_1}-{coup.busbar_node_2}, "
          f"ferme={coup.is_closed}")
```

---

## 9. Exemples sur postes RTE reels

Les sorties ci-dessous proviennent de l'execution du module sur 15 voltage
levels extraits d'un reseau RTE reel (`.xiidm`). Les fixtures sont
serialisees dans `tests/manoeuvre/fixtures/` et peuvent etre rejouees sans
pypowsybl via `build_graph_from_fixture()`.

### 9.1 Vue d'ensemble des 15 postes

```
POSTE      nœuds  arêtes   SJB  équip  départ  coupl  réaig
------------------------------------------------------------------------
CARRIP3       40      54     4     17      15      1      0
CARRIP6       32      43     6     12      12      1      0
CZTRYP6       30      39     8      9       9      1      0   ← max SJB
COMPIP3       28      37     6     10      10      1      0
BXTO5P3       46      61     6     19      15      1      0
BXTO5P6       30      41     4     11      11      1      0
CZBEVP3       12      11     3      6       6      1      0   ← plus petit
PALUNP3       46      65     4     19      19      1      0
NOVIOP3       30      39     4     10      10      1      0
SSAVOP3       80     107     8     34      27      1      0   ← plus grand
VIELMP6       29      39     4     11      11      1      0
CORNIP3       39      54     4     17      17      1      2
GUARBP6       23      29     6      8       8      1      2
MORBRP6       47      63     6     17      17      1      2
RAN_PP6       29      39     4     12      12      1      2
```

Colonnes : `nœuds` = connectivity nodes du graphe, `SJB` = sections de jeux de
barres, `départ` = cellules de depart detectees, `réaig` = dont ré-aiguillages.

---

### 9.2 Poste standard : CARRIP3 (225 kV, 4 SJB)

Topologie double barre classique. Chaque depart = 1 DJ + 2 SA, acces a 2 SJB.

```
VL 'CARRIP3': 15 départ(s), 1 couplage(s), 0 nœud(s) non attribué(s)

Cellules de départ (15) :
  CARRIL31VALES          LINE_SIDE1         2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRIL31RANTI          LINE_SIDE1         2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRIL31V.PAU          LINE_SIDE1         2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRIL31U.MON          LINE_SIDE1         2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRIL31PERSA          LINE_SIDE1         2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRIL32U.MON          LINE_SIDE1         2 SJB, 1 conn.  (1 DJ, 2 SA)
  BERT L31CARRI          LINE_SIDE2         2 SJB, 1 conn.  (1 DJ, 2 SA)
  BARR6L31CARRI          LINE_SIDE2         2 SJB, 1 conn.  (1 DJ, 2 SA)
  BRENOL31CARRI          LINE_SIDE2         2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRI3T314             LOAD               2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRI3T312             LOAD               2 SJB, 1 conn.  (3 DJ, 2 SA)  [OMNIBUS]
  CARRI3T313             LOAD               2 SJB, 1 conn.  (3 DJ, 2 SA)  [OMNIBUS]
  CARRIY632              TRANSFORMER_SIDE2  2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRIY633              TRANSFORMER_SIDE2  2 SJB, 1 conn.  (1 DJ, 2 SA)
  CARRIY631              TRANSFORMER_SIDE2  2 SJB, 1 conn.  (1 DJ, 2 SA)

Cellules de couplage (1) :
  SJB 0 ↔ SJB 1  fermé=True  1 DJ + 4 SA  DJ: CARRI3COUPL.1 DJ_OC
```

**Cellule omnibus `CARRI3T312`** : 3 DJ detectes car deux transformateurs
(TR312 et l'auxiliaire CARRIINF) partagent les memes noeuds intermediaires.
`shared_equipment_ids = {'CARRIINF'}`.

---

### 9.3 Poste avec re-aiguillages : CORNIP3 (225 kV)

```
VL 'CORNIP3': 17 départ(s), 1 couplage(s), 0 nœud(s) non attribué(s)

  CORNIL31REAC.          LINE_SIDE1        1 SJB, 1 conn.  (0 DJ, 1 SA)  [RÉAIG]
  CORNIL31REAC.          LINE_SIDE2        1 SJB, 1 conn.  (0 DJ, 1 SA)  [RÉAIG]
  BORLYL31CORNI          LINE_SIDE2        3 SJB, 1 conn.  (1 DJ, 3 SA)   ← 3 barres
  ...
```

La ligne `CORNIL31REAC.` est raccordee directement a la barre via un unique
sectionneur fictif `CORNIP3.SJB.3.1-CORNI3REAC.1SA_F` — pas de disjoncteur
propre. Les deux cotes (SIDE1 et SIDE2) apparaissent comme deux cellules
independantes, chacune avec 1 noeud d'equipement + 1 noeud SJB.

Detail de la cellule de re-aiguillage (SIDE1) :

```
Equipement : CORNIL31REAC.  (LINE_SIDE1)
  Nœuds : [2, 11]
  SJB accessibles (struct.) : {2}
  SJB connectées (électr.)  : {2}
  Switch unique : CORNIP3_CORNIP3-CORNIP3.SJB.3.1-CORNI3REAC.1SA_F
    kind=DISCONNECTOR  node1=2  node2=11  open=False
  is_reaiguillage=True   breakers=[]
```

---

### 9.4 Poste avec generateurs : SSAVOP3 (225 kV, 8 SJB, 34 equipements)

Plus grand poste du jeu de donnees. Abrite trois groupes de production avec
un schema de raccordement complexe (jusqu'a 5 DJ + 2 SA par cellule groupe).

```
VL 'SSAVOP3': 27 départ(s), 1 couplage(s), 0 nœud(s) non attribué(s)

Nœuds totaux : 80  |  Arêtes totales : 107
Taux de connectivité : 96%  |  Cellules multi-barres : 27/27

Top 5 cellules par nb switches :
  SSAVOIN1               GENERATOR         7 switches  (5 DJ, 2 SA)
  SSAVOIN2               GENERATOR         6 switches  (4 DJ, 2 SA)
  SSAVOIN3               GENERATOR         6 switches  (4 DJ, 2 SA)
  SSAVOL32SYNTH          LINE_SIDE1        3 switches  (1 DJ, 2 SA)
  CREUTL31SSAVO          LINE_SIDE2        3 switches  (1 DJ, 2 SA)
```

Le couplage comporte 4 DJ paralleles + 8 SA (schema de by-pass complet
sur 8 SJB). Un groupe (`SSAVOIN2`) est detecte comme electriquement
deconnecte (`connected_busbars=0`).

---

### 9.5 Postes 63 kV avec re-aiguillages : GUARBP6, MORBRP6, RAN_PP6

Les trois postes 63 kV du jeu de donnees presentent le meme motif :
un transformateur HTB/HTA (ou une ligne reactance) raccorde directement
a la barre sans disjoncteur propre.

**GUARBP6** — transformateur `GUARBY661` en re-aiguillage :
```
  GUARBY661  TRANSFORMER_SIDE1  1 SJB, 1 conn.  (0 DJ, 1 SA)  [RÉAIG]
  GUARBY661  TRANSFORMER_SIDE2  1 SJB, 1 conn.  (0 DJ, 1 SA)  [RÉAIG]
```
Le couplage de GUARBP6 a pour DJ principal `GUARB6TR661 DJ_OC` — c'est le
DJ de la cellule de couplage qui sert de protection pour ce transformateur.

**MORBRP6** — ligne reactance `MORBRL61REAC.` en re-aiguillage :
```
  MORBRL61REAC.  LINE_SIDE1  1 SJB, 1 conn.  (0 DJ, 1 SA)  [RÉAIG]
  MORBRL61REAC.  LINE_SIDE2  1 SJB, 1 conn.  (0 DJ, 1 SA)  [RÉAIG]
```
Couplage le plus riche : 5 DJ + 10 SA (poste multi-departs sur 6 SJB).

**RAN_PP6** — transformateur `RAN.PY661` en re-aiguillage :
```
  RAN.PY661  TRANSFORMER_SIDE2  1 SJB, 1 conn.  (0 DJ, 1 SA)  [RÉAIG]
  RAN.PY661  TRANSFORMER_SIDE1  1 SJB, 1 conn.  (0 DJ, 1 SA)  [RÉAIG]
```

---

### 9.6 Cas particuliers notables

#### CZBEVP3 — poste simplifie (3 SJB, couplage sans DJ)

```
VL 'CZBEVP3': 6 départ(s), 1 couplage(s), 0 nœud(s) non attribué(s)

  CZBEVL31DANT5  LINE_SIDE1  1 SJB, 1 conn.  (1 DJ, 1 SA)
  CZBEV3T311     LOAD        1 SJB, 1 conn.  (1 DJ, 0 SA)   ← pas de SA
  ...
  Couplage :  SJB 0 ↔ SJB 1  fermé=True  0 DJ + 2 SA        ← pas de DJ
```

Seul poste avec des cellules sans sectionneur (charges directement sur une
seule barre) et un couplage purement sectionneur (0 DJ). Topologie
volontairement simplifiee pour un poste de moindre criticite.

#### NOVIOP3 — couplage a 3 DJ (poste de transit)

```
Couplage :  SJB 0 ↔ SJB 1  fermé=False  3 DJ + 6 SA  DJ: NOVIO3TRO.1AB DJ_OC
```

Trois disjoncteurs en parallele dans la cellule de couplage, jeu de barres
ouvert en exploitation. Typique d'un poste de transit 225 kV avec by-pass.

#### BXTO5P3 — poste avec equipements hors service (5 cellules deconnectees)

```
Cellules déconnectées (connected_busbars=0) :
  BXTO5L31TERGN   LINE_SIDE1        → ligne ouverte
  BXTO5L32TERGN   LINE_SIDE1        → ligne ouverte
  BXTO5L31HAM     LINE_SIDE1        → ligne ouverte
  BXTO5Y638       TRANSFORMER_SIDE2 → transformateur deconnecte
  BXTO5IN2        GENERATOR         → groupe non engage
```

5 equipements hors service sur 19 — le poste est en configuration
partielle au moment de la capture.

---

### 9.7 Observations transversales sur les 15 postes

**Couplages ouverts** (jeux de barres separes en exploitation) :
COMPIP3, PALUNP3, NOVIOP3, SSAVOP3, CORNIP3 — soit 5 postes sur 15.

**Cellules a 3 SJB accessibles** (topologie de transit de barre) :
4 postes presentent au moins une cellule avec 3 SA (acces a 3 barres) :
CORNIP3 (`BORLYL31CORNI`), GUARBP6 (`GUARBL61ZWOES`),
MORBRP6 (`MORBRL61V.GEO`), RAN_PP6 (`LAUNAL61RAN.P`).

**Equipements fictifs** :
`fict_PALUNP3_1.1` (LOAD) dans PALUNP3 — injection ajoutee par le modele
pour equilibrer le bilan du poste.

**Taux de connectivite** :
Sur l'ensemble des 15 postes, 14 cellules presentent `connected_busbars=0`
(lignes hors service, condensateurs ouverts, groupes non engages) — soit
~5% des equipements, coherent avec un etat reseau en exploitation normale.

---

## 10. Tests

### Tests unitaires (`test_graph_cellules.py`)

Reseau de reference : `pp.network.create_four_substations_node_breaker_network()`
cible le voltage level `S1VL2` (380 kV, 2 barres).

| Classe                   | Tests | Couverture                                       |
|--------------------------|-------|--------------------------------------------------|
| `TestBuildVlGraph`       | 9     | Construction, tagging, attributs, erreurs         |
| `TestDetecterCellules`   | 11    | Detection, proprietes, sous-graphes               |
| `TestConnectedBusbars`   | 4     | Connectivite electrique, sous-ensemble SJB        |
| `TestCoherenceGlobale`   | 3     | Coherence graphe/cellules, pas de recouvrement    |

### Tests sur postes reels (`test_postes_reels.py`)

15 voltage levels de postes RTE reels, serialises en fixtures JSON.
Pas de dependance a pypowsybl a l'execution des tests (reconstruction
du graphe NX depuis le JSON).

| Classe                       | Tests | Couverture                                    |
|------------------------------|-------|-----------------------------------------------|
| `TestGraphConstruction`      | 5     | Validite du graphe, SJB, equipements, types   |
| `TestCellulesDepart`         | 8     | Couverture equipements, SJB, switches, DJ     |
| `TestPasDeChevaucement`      | 1     | Pas de recouvrement de noeuds internes        |
| `TestCellulesCouplage`       | 3     | Structure couplage, DJ, liens >= 2 SJB        |
| `TestConnectiviteElectrique` | 3     | Sous-ensemble SJB, taux connectivite > 50%    |
| `TestDepartsMultiples`       | 1     | Detection cellules omnibus                    |
| `TestDiagnostic`             | 2     | Resume, coherence stats/fixtures              |

### Extraction des fixtures

```bash
python scripts/extract_test_fixtures.py \
    --xiidm path/to/grid.xiidm
```

Genere un fichier JSON par voltage level dans `tests/manoeuvre/fixtures/`
avec un `index.json` de synthese.

---

## 11. Prochaines etapes (roadmap)

| Etape   | Fichier          | Description                                              |
|---------|-----------------|----------------------------------------------------------|
| 1.3     | `troncons.py`    | Attribution des noeuds aux cellules, tronconnement        |
| 1.4     | `troncons.py`    | Regroupement en troncons electriques                      |
| 1.5     | `topologie.py`   | Construction de `TopologieNodale`                         |
| 1.6     | `topologie.py`   | Construction de `PosteTopologique`                        |
| Phase 2 | `algo/`          | Algorithme nodale -> detaillee (generation de manoeuvres ; package en couches) |

---

## 12. Dependances

| Package      | Role                                        | Version min |
|-------------|---------------------------------------------|-------------|
| `pypowsybl` | Modele de donnees reseau (IIDM/CGMES)       | >= 1.13.0   |
| `networkx`   | Representation et algorithmes de graphes     | —           |
| `pandas`     | DataFrames pypowsybl                        | —           |
