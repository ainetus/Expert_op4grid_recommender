# Plan d'implementation — Module Manoeuvre

> **Package** : `expert_op4grid_recommender.manoeuvre`
> **Origine** : Portage Python de `libTOPO` (projet TOPO Apogee, RTE)
> **Backend** : pypowsybl (topologie NODE_BREAKER) + NetworkX
> **Date** : avril 2026

---

## 1. Contexte et objectif

Le module **manoeuvre** porte en Python l'algorithme C++ de passage d'une
topologie nodale a une topologie detaillee, implemente dans la bibliotheque
`libTOPO` du projet TOPO Apogee. L'objectif final est de generer, pour un
poste electrique donne, la sequence de manoeuvres (ouvertures/fermetures
d'organes de coupure) permettant d'atteindre une topologie nodale cible a
partir de l'etat detaille courant du poste.

### Probleme resolu

**Entree** : topologie nodale cible (quels equipements sur quels noeuds
electriques) + etat detaille courant (etat de chaque OC du poste).

**Sortie** : liste ordonnee de manoeuvres sur les organes de coupure (DJ, SA,
couplages) pour atteindre la cible, en respectant les contraintes de securite
(pas de court-circuit, continuite de service quand possible).

### Choix d'implementation

Le C++ offrait deux approches :

- **Option 1 — Catalogue explicite** : stocker toutes les topologies detaillees
  observees par topologie nodale. Simple mais couteux en memoire (2^n
  combinaisons) et limite aux historiques.

- **Option 2 — Algorithme en ligne** : calculer la topologie detaillee cible
  a partir de la structure du poste (SJB, troncons, couplages). Generique et
  adaptatif.

Le portage Python suit l'**option 2** (algorithme en ligne), identique au C++.

---

## 2. Vue d'ensemble des phases

```
Phase 1 — Modele de donnees topologiques
  Etape 1.1  graph.py         Graphe node/breaker d'un VL        [FAIT]
  Etape 1.2  cellules.py      Cellules de depart et couplage     [FAIT]
  Etape 1.3  troncons.py      Tronconnement des SJB
  Etape 1.4  troncons.py      Attribution noeuds -> troncons
  Etape 1.5  topologie.py     TopologieNodale
  Etape 1.6  topologie.py     PosteTopologique

Phase 2 — Algorithme nodale -> detaillee
  Etape 2.1  algo.py          Nettoyage (connect/deconnect)
  Etape 2.2  algo.py          Evaluation couplages
  Etape 2.3  algo.py          Super-troncons et reaiguillages
  Etape 2.4  algo.py          Generation sequence de manoeuvres
  Etape 2.5  algo.py          Verification

Phase 3 — Integration au recommandeur  (hors scope immediat)
  Etape 3.1  integration.py   Interface avec ActionDiscoverer
  Etape 3.2  integration.py   Generation d'actions Grid2Op/pypowsybl
```

---

## 3. Etat d'avancement

| Etape | Fichier        | Statut         | Description                         |
|-------|----------------|----------------|-------------------------------------|
| 1.1   | `graph.py`     | **Termine**    | Graphe NX depuis pypowsybl         |
| 1.2   | `cellules.py`  | **Termine**    | Detection cellules depart/couplage  |
| 1.3   | `troncons.py`  | **Termine**    | Tronconnement des SJB               |
| 1.4   | `troncons.py`  | **Termine**    | Attribution noeuds aux troncons     |
| 1.5   | `topologie.py` | **Termine**    | TopologieNodale                     |
| 1.6   | `topologie.py` | **Termine**    | PosteTopologique                    |
| 2.1   | `algo.py`      | **Termine**    | Nettoyage initial                   |
| 2.2   | `algo.py`      | **Termine**    | Evaluation couplages                |
| 2.3   | `algo.py`      | **Termine** *  | Super-troncons / reaiguillages      |
| 2.4   | `algo.py`      | **Termine** *  | Sequence de manoeuvres              |
| 2.5   | `algo.py`      | **Termine**    | Verification                        |

> *\* Couverture : postes 1 barre, 2 barres standard, et **multi-sections**
> (ex. CARRIP6, 2 barres × 3 sections) — couplage, ré-aiguillage boucle
> courte/longue, **règle du sectionneur** (anti court-circuit), création de
> nœuds par ouverture de sectionnement, **cible détaillée imposée** (barre
> exacte + vérification détaillée), **dégradation gracieuse** (écarts consignés).
> Règles détaillées et tracées : `docs/manoeuvre/regles.md` (R1–R16). Limites
> restantes : omnibus complexes, contrôle de court-circuit fin (déphasage),
> couplers non chaînés (≥ 3 barres en anneau).*

> **Phase 3 (intégration / outillage), réalisée :**
> - point d'entrée unique `determiner_topo_complete_cible` (placement
>   automatique nœud→SJB) et `determiner_manoeuvres_cible_detaillee` (cible
>   détaillée + vérification + écarts) ;
> - **IHM web** de test interactif (`scripts/manoeuvre_ihm.py`, Flask) —
>   cf. `docs/manoeuvre/ihm.md` ;
> - rendu SLD avant/après (`scripts/render_carrip3_sld.py`).

### Tests disponibles

| Fichier                          | Cible       | Nb tests |
|----------------------------------|-------------|----------|
| `test_graph_cellules.py`         | Etapes 1.1-1.2 | 27   |
| `test_postes_reels.py`           | Etapes 1.1-1.2 | ~105 (15 VL x 7 classes) |
| `test_troncons.py`               | Etapes 1.3-1.4 | 10   |
| `test_topologie.py`              | Etapes 1.5-1.6 | 8    |
| `test_algo.py`                   | Phase 2, cible détaillée, anti court-circuit | 10 |
| `test_carrip3_manoeuvre.py`      | CARRIP3 cible 2 nœuds | 5 |
| `test_carrip3_3noeuds.py`        | Sectionneur de barre, boucle longue, équipotentialité | 11 |
| `test_scenarios_sauvegardes.py`  | Scénarios IHM (CARRIP3, CARRIP6) — cible détaillée + requinçonçage | 4 |
| `fixtures/` (15 JSON)            | Données de test sans pypowsybl |
| `scenarios/` (JSON)              | Scénarios sauvegardés (départ/cible détaillés) |

Total module `manoeuvre` : **382 tests** verts.

---

## 4. Phase 1 — Modele de donnees topologiques

### 4.1 Etape 1.1 — Graphe node/breaker [FAIT]

**Fichier** : `graph.py`
**Correspondance C++** : `CelluleDepartTopo::buildCellGraph`, `CelluleBarresTopo::buildCellGraph`

Construit un graphe NetworkX non oriente pour un voltage level :
- Noeuds = connectivity nodes IIDM (entiers)
- Aretes = switches (DJ, SA, LBS) + internal connections
- Annotations : `NodeType` (BUSBAR_SECTION, EQUIPMENT, INTERNAL), `SwitchKind`, etat open/closed

**API** : `build_vl_graph(network, vl_id) -> nx.Graph`

**Point technique** : les getters pypowsybl (`get_loads`, `get_lines`, etc.) necessitent
`all_attributes=True` pour exposer la colonne `node` en topologie NODE_BREAKER.

### 4.2 Etape 1.2 — Cellules de depart et couplage [FAIT]

**Fichier** : `cellules.py`
**Correspondance C++** : `CelluleDepartTopo`, `CelluleBarresTopo`

Algorithme en 3 phases :
- **Phase A** : BFS structurel depuis chaque EQUIPMENT, arret aux SJB. Fusion des cellules partageant des noeuds intermediaires (omnibus).
- **Phase B** : Sous-graphe residuel (SJB + noeuds non attribues). Composantes connexes avec >= 2 SJB = couplages.
- **Phase C** : Noeuds restants non attribues.

**API** : `detecter_cellules(G, vl_id) -> CellulesVL`

### 4.3 Etape 1.3 — Tronconnement des SJB

**Fichier a creer** : `troncons.py`
**Correspondance C++** : `Topologie::buildTronconnement()` (TOPOPoste.cc:1947), `CelluleBarresTopo::tronconneGraph()` (TOPOPosteCellElement.cc:639)

#### Concept

Un **troncon** est une partition des sections de jeux de barres (SJB) d'un
voltage level. Deux SJB appartiennent au meme troncon si elles sont reliees
par un chemin de sectionneurs fermes sur la barre (sans passer par un
sectionneur ouvert). Chaque troncon represente un segment de barre
electriquement continu.

#### Structures de donnees

```python
@dataclass
class Troncon:
    numero: int                              # Identifiant du troncon
    nb_jeux_barres: int                      # Nombre de barres accessibles
    busbar_nodes: set[int]                   # SJB de ce troncon
    departs: set[str]                        # Equipements rattaches
    departs_fixes: dict[str, int]            # Equipement -> n° barre fixe
    departs_couplage: dict[str, set[int]]    # Equipement servant de couplage
    departs_multiples: list[set[str]]        # Groupes omnibus
    is_couplage: bool                        # Troncon de couplage (sans depart propre)
    noeuds_electriques: set[str]             # Noeuds assigns (etape 1.4)

@dataclass
class Tronconnement:
    voltage_level_id: str
    troncons: dict[int, Troncon]
    troncon_par_depart: dict[str, int]       # Equipement -> n° troncon
    departs_par_troncon: dict[int, set[str]] # N° troncon -> equipements
```

#### Algorithme

```
Entree : CellulesVL (etape 1.2)
Sortie : Tronconnement

1. Construire le graphe des SJB :
   - Noeuds = busbar_nodes du graphe VL
   - Aretes = sectionneurs (DISCONNECTOR) reliant deux SJB
     (chemins directs ou via noeuds intermediaires du coupler_subgraph)

2. Partitionner les SJB par etat des sectionneurs :
   - Parcourir les aretes DISCONNECTOR du graphe coupler
   - BFS/composantes connexes sur les aretes fermees uniquement
   - Chaque composante = 1 troncon

3. Attribuer les departs aux troncons :
   - Pour chaque CelluleDepart :
     a. Identifier les SJB structurellement accessibles (busbar_nodes)
     b. Determiner le troncon de chaque SJB
     c. Troncon du depart = troncon de sa SJB connectee
        (ou du premier troncon atteignable si multi-barre)

4. Classifier les departs :
   - departs_fixes : depart accessible depuis une seule barre
     (1 seul SA, pas de reaiguillage possible)
   - departs_couplage : depart accessible depuis SJB de troncons differents
     (peut servir de couplage inter-troncons)
   - departs_multiples : groupes omnibus (CelluleDepart.is_multiple)

5. Marquer les troncons de couplage :
   - Troncon sans depart propre, ne servant qu'a relier deux segments
```

#### Correspondance avec le C++

| C++ (`Troncon`)            | Python (`Troncon`)         |
|---------------------------|---------------------------|
| `numero_`                  | `numero`                  |
| `nbJeuxBarres_`            | `nb_jeux_barres`          |
| `nomDeparts_`              | `departs`                 |
| `departsFixes_`            | `departs_fixes`           |
| `departsCouplage_`         | `departs_couplage`        |
| `departsMultiples_`        | `departs_multiples`       |
| `isCouplage_`              | `is_couplage`             |
| `sJBsTronconnement_`       | (implicite via `busbar_nodes`) |

#### Tests prevus

- Poste 1 barre (ex: CZBEVP3 simplifie) : 1 seul troncon contenant toutes les SJB
- Poste 2 barres couplage ferme (ex: CARRIP3) : 1 troncon (barres couplees)
- Poste 2 barres couplage ouvert (ex: NOVIOP3) : 2+ troncons separes
- Poste multi-barres (ex: SSAVOP3, 8 SJB) : troncons multiples
- Coherence : chaque depart appartient a exactement un troncon
- Departs fixes : verifie sur postes avec reaiguillage (CORNIP3)

### 4.4 Etape 1.4 — Attribution noeuds -> troncons

**Fichier** : `troncons.py` (suite)
**Correspondance C++** : `Topologie::getNoeudTronconnement()` (TOPOPoste.cc)

#### Concept

Attribuer a chaque troncon ses noeuds electriques a partir de la topologie
nodale courante. Un noeud electrique regroupe les equipements qui sont sur le
meme potentiel (meme bus dans pypowsybl).

#### Algorithme

```
Entree : Tronconnement + TopologieNodale (etape 1.5)
Sortie : Tronconnement avec noeuds_electriques remplis

1. Pour chaque noeud electrique N de la topologie nodale :
   a. Lister les departs rattaches a N
   b. Identifier les troncons contenant ces departs
   c. Marquer ces troncons comme contenant N

2. Consolidation (pont inter-troncons) :
   - Si un noeud N apparait sur les troncons T1 et T3 mais pas T2
     (troncons consecutifs), ajouter N a T2
   - Raison : pas de dispositif physique pour isoler T2 de T1 et T3
     si T2 est entre les deux
   - Methode : pour chaque noeud, parcourir les troncons par numero
     et combler les gaps
```

#### Correspondance C++

`Noeud::consolidateTroncon()` : remplit les troncons intermediaires.

### 4.5 Etape 1.5 — TopologieNodale

**Fichier a creer** : `topologie.py`
**Correspondance C++** : `Topologie`, `Noeud`, `Depart` (TOPOPoste.h)

#### Concept

Representation simplifiee de l'etat electrique d'un poste : quels equipements
sont connectes a quels noeuds electriques (bus). Ne decrit pas l'etat individuel
de chaque OC.

#### Structures de donnees

```python
@dataclass
class DepartInfo:
    """Depart (equipement) dans une topologie nodale."""
    equipment_id: str
    equipment_type: EquipmentType
    noeud: str                    # Nom du noeud electrique ("N1", "N2", ...)
    troncon: Optional[int]        # Numero de troncon (apres etape 1.4)
    is_connected: bool            # Connecte electriquement

@dataclass
class NoeudElectrique:
    """Noeud electrique (bus) regroupant des departs au meme potentiel."""
    nom: str                      # "N1", "N2", ...
    departs: list[DepartInfo]
    troncons: set[int]            # Troncons contenant ce noeud

@dataclass
class TopologieNodale:
    """Topologie nodale d'un voltage level."""
    voltage_level_id: str
    noeuds: dict[str, NoeudElectrique]   # nom -> noeud
    depart_par_noeud: dict[str, str]     # equipment_id -> nom noeud

    @classmethod
    def from_pypowsybl(cls, network, vl_id) -> TopologieNodale:
        """Extrait la topologie nodale depuis l'etat courant du reseau."""
        ...

    @classmethod
    def from_bus_assignment(cls, vl_id, bus_map: dict[str, int]) -> TopologieNodale:
        """Construit depuis une assignation equipement -> bus."""
        ...

    def noeuds_par_depart(self) -> dict[str, str]:
        """Retourne le mapping equipement -> noeud."""
        ...

    def meme_topologie(self, other: TopologieNodale) -> bool:
        """Compare deux topologies nodales (isomorphisme de partition)."""
        ...
```

#### Extraction depuis pypowsybl

La topologie nodale courante s'obtient en interrogeant le bus calculator de
pypowsybl : `network.get_bus_breaker_topology(vl_id)` retourne les bus
calcules et les equipements connectes a chacun. Alternativement, on peut
deduire les noeuds depuis le graphe NX en calculant les composantes connexes
du sous-graphe restreint aux aretes fermees.

### 4.6 Etape 1.6 — PosteTopologique

**Fichier** : `topologie.py` (suite)
**Correspondance C++** : `Poste` (TOPOPoste.h)

#### Structures de donnees

```python
@dataclass
class PosteTopologique:
    """Representation complete d'un poste avec sa topologie."""
    voltage_level_id: str
    nom_poste: str
    tension_nominale: float           # kV
    nb_jeux_barres: int
    cellules: CellulesVL              # Etape 1.2
    tronconnement: Tronconnement      # Etape 1.3
    topologie_nodale: TopologieNodale # Etape 1.5

    @classmethod
    def from_network(cls, network, vl_id) -> PosteTopologique:
        """Construction complete depuis un reseau pypowsybl."""
        G = build_vl_graph(network, vl_id)
        cellules = detecter_cellules(G, vl_id)
        tronconnement = construire_tronconnement(cellules)
        topo = TopologieNodale.from_pypowsybl(network, vl_id)
        attribuer_noeuds(tronconnement, topo)
        ...

    def resume(self) -> str:
        """Synthese du poste."""
        ...
```

---

## 5. Phase 2 — Algorithme nodale -> detaillee

**Fichier a creer** : `algo.py`
**Correspondance C++** : `Topologie::determineTopoCompleteCible()` (TOPOPoste.cc:3944)

### 5.0 Point d'entree

```python
def determiner_topo_complete_cible(
    poste: PosteTopologique,
    topo_cible: TopologieNodale,
) -> ResultatManoeuvres:
    """
    Calcule la sequence de manoeuvres pour passer de l'etat detaille
    courant du poste a la topologie nodale cible.
    """
```

### 5.1 Etape 2.1 — Nettoyage (connect/deconnect)

**Correspondance C++** : `connectAndDeconnectOuvrageHS()`

```
1. Comparer departs presents dans l'etat courant vs. departs de la cible
2. Deconnect : departs presents dans le courant mais absents de la cible
   -> ouvrir le DJ du depart
3. Reconnect : departs presents dans la cible mais absents du courant
   -> fermer le DJ du depart
4. Si echec (depart absent physiquement) -> abandon, retourner isChanged=False
```

### 5.2 Etape 2.2 — Evaluation des couplages

**Correspondance C++** : `Topologie::evalueEtatCouplage()` (TOPOPoste.cc)

```
Pour chaque troncon de la segmentation :
  Compter le nombre de noeuds electriques distincts dans le troncon
  Compter le nombre de jeux de barres accessibles

  Si nb_noeuds < nb_jeux_barres :
    -> FERMER le couplage de ce troncon
    -> Les barres restent au meme potentiel, les departs du meme noeud
       peuvent etre repartis sur differentes barres
  
  Si nb_noeuds >= nb_jeux_barres :
    -> OUVRIR le couplage de ce troncon
    -> Chaque barre porte un noeud distinct
```

### 5.3 Etape 2.3 — Super-troncons et reaiguillages (postes 2 barres)

**Correspondance C++** : `identifySuperTronconnement()`, `getTronconnementBesoinReaiguillage2barres()`

Etape specifique aux postes a 2 jeux de barres (cas le plus courant sur le
reseau RTE 225 kV et 400 kV).

```
1. Identifier les super-troncons :
   - Grouper les troncons consecutifs (non-couplage) partageant un noeud commun
   - Contrainte : dans un super-troncon, tous les departs du meme noeud
     doivent etre sur la meme barre

2. Pour chaque super-troncon, evaluer les 2 configurations possibles :
   Config A : Noeud_1 sur Barre_1, Noeud_2 sur Barre_2
   Config B : Noeud_2 sur Barre_1, Noeud_1 sur Barre_2

3. Pour chaque config, compter les reaiguillages necessaires :
   - Un depart doit etre reaiguille si sa barre actuelle != barre cible
   - Verifier la faisabilite : les departs_fixes ne peuvent pas changer de barre

4. Choisir la configuration avec le moins de reaiguillages
   - En cas d'egalite, privilegier la config respectant les departs_fixes

5. Resultat : set[str] des departs a reaiguiller
```

### 5.4 Etape 2.4 — Generation de la sequence de manoeuvres

**Correspondance C++** : `Topologie::listeDordre()` (TOPOPoste.cc:3644-3850)

La sequence respecte un ordre precis pour minimiser les risques :

```
Sequence de manoeuvres ordonnee :

1. Fermer les couplages necessaires (DJ de couplage)
   - Prepare la reception des departs reaiguilles

2. Reaiguillages par boucle courte (si possible) :
   - Fermer le SA vers la barre cible
   - Ouvrir le SA vers la barre d'origine
   -> Le depart reste sous tension pendant toute l'operation
   -> Possible uniquement si le couplage est ferme

3. Reaiguillages par boucle longue (sinon) :
   a. Ouvrir le DJ du depart (mise hors tension)
   b. Ouvrir le SA vers la barre d'origine
   c. Fermer le SA vers la barre cible
   d. Fermer le DJ du depart (remise sous tension)

4. Ouvrir les couplages devant etre ouverts
   - Separer les barres selon la topologie cible

5. Optimiser : fusionner les manoeuvres redondantes
   - Ex : un depart reaiguille 2 fois = annuler les 2 operations
```

#### Verification court-circuit

Avant chaque manoeuvre de couplage ou reaiguillage, verifier qu'elle ne cree
pas de court-circuit. La methode C++ `isBoucleCourteOCBarrePossible()` verifie
si la fermeture d'un DJ de couplage met en parallele deux sources a potentiels
differents.

#### Structures de sortie

```python
@dataclass
class Manoeuvre:
    """Une operation unitaire sur un organe de coupure."""
    switch_id: str
    action: Literal["OPEN", "CLOSE"]
    raison: str                        # Description lisible
    type_boucle: Optional[Literal["COURTE", "LONGUE"]]

@dataclass
class ResultatManoeuvres:
    """Resultat complet de l'algorithme."""
    voltage_level_id: str
    topo_initiale: TopologieNodale
    topo_cible: TopologieNodale
    manoeuvres: list[Manoeuvre]        # Sequence ordonnee
    nb_manoeuvres: int
    departs_reaiguilles: set[str]
    couplages_modifies: list[str]
    is_changed: bool                   # True si des manoeuvres sont necessaires
    is_verified: bool                  # True si la verification post-algo confirme
```

### 5.5 Etape 2.5 — Verification

**Correspondance C++** : fin de `determineTopoCompleteCible()`

```
1. Appliquer toutes les manoeuvres au graphe NX (modifier les attributs 'open')
2. Recalculer la topologie nodale resultante
   (composantes connexes du sous-graphe ferme)
3. Comparer avec la topologie cible
4. Si correspondance -> is_verified = True
5. Sinon -> diagnostic : quels departs sont sur le mauvais noeud ?
```

---

## 6. Limites connues de l'algorithme C++

L'algorithme C++ ne gere pas certains cas particuliers. Le portage Python devra
soit les traiter, soit les documenter comme limites :

| Cas                                        | Exemple       | Statut prevu  |
|--------------------------------------------|---------------|---------------|
| Postes a 3+ jeux de barres                 | —             | Non traite (partiel en C++) |
| Ponts de barre entre troncons              | G.CAIP6       | Non traite    |
| Couplage via depart (pas via OC de barre)  | P.VIZP3       | Non traite    |
| Omnibus complexes (3+ departs fusionnes)   | REICHP3       | A evaluer     |
| Liaisons internes (bypass sans SJB)        | —             | Non traite    |
| Postes 1 barre (cas degenere)              | —             | A traiter (simple) |

---

## 7. Architecture fichiers cible

```
expert_op4grid_recommender/manoeuvre/
  __init__.py          API publique (a etendre)
  models.py            Enumerations et dataclasses          [FAIT]
  graph.py             Etape 1.1                            [FAIT]
  cellules.py          Etape 1.2                            [FAIT]
  troncons.py          Etapes 1.3 + 1.4                     [A FAIRE]
  topologie.py         Etapes 1.5 + 1.6                     [A FAIRE]
  algo.py              Phase 2 complete                      [A FAIRE]

tests/manoeuvre/
  test_graph_cellules.py     Tests unitaires 1.1-1.2        [FAIT]
  test_postes_reels.py       Tests postes RTE 1.1-1.2       [FAIT]
  test_troncons.py           Tests tronconnement             [A FAIRE]
  test_topologie.py          Tests TopologieNodale           [A FAIRE]
  test_algo.py               Tests algorithme complet        [A FAIRE]
  fixture_loader.py          Chargement fixtures JSON        [FAIT]
  fixtures/                  15 postes RTE serialises        [FAIT]

scripts/
  extract_test_fixtures.py   Extraction depuis .xiidm        [FAIT]
```

---

## 8. Dependances

| Package      | Role                                     | Version min |
|-------------|------------------------------------------|-------------|
| `pypowsybl` | Modele de donnees reseau (IIDM/CGMES)    | >= 1.13.0   |
| `networkx`   | Graphes et algorithmes (BFS, CC, ...)     | —           |
| `pandas`     | DataFrames pypowsybl                     | —           |

Aucune dependance additionnelle prevue pour les phases 1-2.

---

## 9. Strategie de test

### Tests unitaires (reseau synthetique)

Utiliser `pypowsybl.network.create_four_substations_node_breaker_network()`
pour les tests unitaires ne necessitant pas de donnees RTE.

### Tests sur postes reels (fixtures JSON)

15 postes RTE extraits d'un reseau `.xiidm`, serialises en JSON dans
`tests/manoeuvre/fixtures/`. Pas de dependance a pypowsybl a l'execution
des tests (reconstruction du graphe NX depuis le JSON).

| Poste     | Tension | SJB | Eq. | Interet                            |
|-----------|---------|-----|-----|------------------------------------|
| CARRIP3   | 225 kV  | 4   | 17  | Standard double barre              |
| CARRIP6   | 63 kV   | 6   | 12  | 3 paires de barres                 |
| CZTRYP6   | 225 kV  | 8   | 9   | Maximum SJB                        |
| COMPIP3   | 63 kV   | 6   | 10  | Standard 3 barres                  |
| BXTO5P3   | 225 kV  | 6   | 19  | Equipements hors service           |
| BXTO5P6   | 63 kV   | 4   | 11  | Standard                           |
| CZBEVP3   | 63 kV   | 3   | 6   | Simplifie (couplage sans DJ)       |
| PALUNP3   | 225 kV  | 4   | 19  | Couplage ouvert                    |
| NOVIOP3   | 225 kV  | 4   | 10  | 3 DJ de couplage en parallele      |
| SSAVOP3   | 225 kV  | 8   | 34  | Plus grand, 3 generateurs          |
| VIELMP6   | 225 kV  | 4   | 11  | Standard                           |
| CORNIP3   | 225 kV  | 4   | 17  | Reaiguillages                      |
| GUARBP6   | 63 kV   | 6   | 8   | Transformateur en reaiguillage     |
| MORBRP6   | 225 kV  | 6   | 17  | Ligne reactance en reaiguillage    |
| RAN_PP6   | 63 kV   | 4   | 12  | Transformateur en reaiguillage     |

### Matrice de couverture prevue

| Fonctionnalite                      | Unitaire | Postes reels |
|-------------------------------------|----------|-------------|
| Tronconnement 1 barre              | x        |             |
| Tronconnement 2 barres couplees    | x        | CARRIP3     |
| Tronconnement 2 barres separees    | x        | NOVIOP3     |
| Attribution noeuds (consolidation)  | x        | Tous        |
| TopologieNodale extraction          | x        | Tous        |
| Algo : couplage ferme -> ouvert    | x        | PALUNP3     |
| Algo : reaiguillage boucle courte  | x        | CORNIP3     |
| Algo : reaiguillage boucle longue  | x        | MORBRP6     |
| Algo : verification post-manoeuvre | x        | Tous        |

---

## 10. Ordre d'implementation recommande

### Iteration 1 : Tronconnement (etapes 1.3-1.4)

1. Implementer `Troncon` et `Tronconnement` dans `troncons.py`
2. Implementer `construire_tronconnement(cellules) -> Tronconnement`
3. Ecrire les tests `test_troncons.py` sur le reseau synthetique
4. Valider sur les 15 fixtures
5. Implementer `attribuer_noeuds(tronconnement, topo)` (necessite etape 1.5 a minima)

### Iteration 2 : Topologie nodale (etapes 1.5-1.6)

1. Implementer `TopologieNodale.from_pypowsybl()` dans `topologie.py`
2. Implementer `TopologieNodale.from_graph()` (depuis le graphe NX + etats fermes)
3. Implementer `PosteTopologique.from_network()` (construction bout en bout)
4. Tests sur reseau synthetique + fixtures

### Iteration 3 : Algorithme (phase 2)

1. Implementer les etapes 2.1 a 2.5 dans `algo.py`
2. Commencer par les postes 1 barre (cas degenere, utile pour la mise au point)
3. Etendre aux postes 2 barres (cas courant)
4. Validation sur les 15 fixtures avec des topologies cibles variees

---

## 11. References

### Documentation algorithme original

- `/topo_Apogee_10022017/docs/algo_topo_nodale_vers_detaillee.md` — Description technique complete
- `/topo_Apogee_10022017/Docs Algos APogee/Algo manoeuvre - nodale vers detaille/Algo passage de topo nodale a topo détaillée_06042017.docx` — Document de conception avec options d'implementation
- `/topo_Apogee_10022017/Docs Algos APogee/Algo manoeuvre - nodale vers detaille/DebugPosteTopoComplete_07102016.docx` — Exemples de validation sur 40+ postes

### Code C++ source

- `TOPOPoste.h/cc` — Classes `Poste`, `Topologie`, `Noeud`, `Depart`
- `TOPOPosteCellElement.h/cc` — Classes `Troncon`, `CelluleDepartTopo`, `CelluleBarresTopo`
- `TOPOActionTopo.h/cc` — Classes `DefTopo`, `TOPOActionTopo`, `TOPONoeud`, `TOPODepart`
- `PosteAManoeuvrer.h/cc` — Classes `NoeudAManoeuvrer`, `PostesAManoeuvrer`

### Documentation module Python

- `docs/manoeuvre/module.md` — Documentation technique du module Python (etapes 1.1-1.2 + exemples sur postes reels)
