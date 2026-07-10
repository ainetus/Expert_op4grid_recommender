# CLAUDE.md — Module Manoeuvre

> Contexte pour les assistants IA travaillant sur ce module.

## Vue d'ensemble

Portage Python de `libTOPO` (C++, RTE). Analyse la topologie NODE_BREAKER
des postes electriques via pypowsybl pour identifier cellules de depart et
de couplage.

## Fichiers

| Fichier        | Role                                             |
|---------------|--------------------------------------------------|
| `models.py`    | Enums (`NodeType`, `EquipmentType`, `SwitchKind`, `CelluleType`) et dataclasses (`NodeAttrs`, `EdgeAttrs`) |
| `graph.py`     | Etape 1.1 : `build_vl_graph()` — graphe NetworkX depuis un voltage level pypowsybl |
| `cellules.py`  | Etape 1.2 : `detecter_cellules()` — BFS structurel + connectivite electrique |
| `troncons.py`  | Etapes 1.3-1.4 : `construire_tronconnement()` — barres, troncons, attribution |
| `topologie.py` | Etapes 1.5-1.6 : `TopologieNodale`, `PosteTopologique`, `attribuer_noeuds()` |
| `algo/`        | Phase 2 : `determiner_topo_complete_cible()` — package en couches (voir ci-dessous) |
| `plugins/`     | Couche **pluggable** : contrats des 3 phases (identification nodale→détaillée, séquencement détaillée→manœuvres, planification bout-en-bout), registre + entry points, façade `PlanificateurTopologie` avec vérification indépendante. Doc : `docs/architecture/plugins.md` |
| `__init__.py`  | API publique                                     |

### Package `algo/` (Phase 2, eclate depuis l'ancien `algo.py`)

Sous-modules en couches, **dependances strictement descendantes** (sans cycle) :

| Sous-module          | Role                                                       |
|----------------------|------------------------------------------------------------|
| `algo/_constants.py` | Poids de placement et garde-fous combinatoires             |
| `algo/results.py`    | `Manoeuvre`, `ResultatManoeuvres` (structures de sortie)   |
| `algo/graph_ops.py`  | Helpers bas niveau : index O(1), `_is_open`/`_set_switch`, chemins SA, `_inter_sjb_couplers` |
| `algo/placement.py`  | Placement noeud → sections (`_placement_automatique`, best-effort, glouton, `_main_busbar_sjb`) |
| `algo/verification.py`| Regle du sectionneur (`_rejeu_securite`, `sectionneurs_sous_charge_par_manoeuvre`), `_optimiser_sequence` |
| `algo/conformite.py` | Conformite « art de la manoeuvre » (R20-R25) : classification des consequences par rejeu (`classifier_manoeuvres`), matrice d'autorisation CCRT (`verifier_matrice_autorisation`), machine a etats des departs (`suivre_etats_departs`), temporisations ACT 104 (`calculer_temporisations`), controles SCADA attendus. Point d'entree `analyser_conformite` -> `ConformiteSequence`, attache a `ResultatManoeuvres.conformite` par `verifier_sequence` (champ **separe** d'`ecarts`/`alertes` : goldens iso). Doc : `docs/manoeuvre/art_de_la_manoeuvre.md` |
| `algo/sequencing.py` | Sequenceur general (`determiner_manoeuvres_avec_sections`), re-aiguillages |
| `algo/targets.py`    | Points d'entree : `determiner_topo_complete_cible`, `determiner_manoeuvres_cible_detaillee`, modes smooth/aggressive/multi-barres |
| `algo/__init__.py`   | **Reexporte toute la surface** de l'ancien module (publics + prives) : `manoeuvre.algo.X` et `from ...algo import X` restent inchanges |

Couches : `_constants`/`results` → `graph_ops` → `placement`/`verification` →
`sequencing` → `targets`.

### Package `plugins/` (algorithmes substituables)

Permet de plugger des algorithmes tiers sur l'une ou toutes les phases :
**A** identification (nodale → détaillée, contrat
`IdentificateurTopologieDetaillee`), **B** séquencement (détaillée → manœuvres,
`SequenceurManoeuvres`), **C** bout-en-bout (`PlanificateurNodal`). Type pivot
sérialisable `CibleDetaillee` (`switch_id -> ouvert ?`). Les natifs libTOPO
sont enregistrés sous `"libtopo"` (adaptateurs minces dans `plugins/builtin.py`).
La façade `PlanificateurTopologie` compose les phases manquantes et applique
`verifier_sequence` (vérification **indépendante** : partition, écarts
détaillés, règle du sectionneur, alertes R10ter) à tout résultat, y compris
tiers. Plugins externes par entry points (groupe
`expert_op4grid_recommender.manoeuvre`, nom `<phase>.<nom>`). Détails et
exemples : `docs/architecture/plugins.md` ; tests :
`tests/manoeuvre/test_plugins_interface.py`.

L'**IHM est migrée sur cette façade** : sélecteurs « Algo » par phase
(`GET/POST /api/algos`), payloads portant l'algo utilisé, verdicts recalculés
indépendamment (tests : `tests/manoeuvre/test_ihm_algo_selection.py`).

## Commandes

```bash
# Tests du module
pytest tests/manoeuvre/ -v

# Extraction de fixtures depuis un .xiidm
python scripts/extract_test_fixtures.py --xiidm path/to/grid.xiidm

# Rendu SLD avant/apres d'un scenario CARRIP3 (couleurs natives pypowsybl)
python scripts/render_carrip3_sld.py --grid path/to/grid.xiidm

# IHM web de test interactif (Flask, dependance optionnelle) :
#   choisir un poste, modifier DJ/SA, valider/sauver la cible, calculer +
#   animer la sequence, sauvegarder scenarios et sequences. Doc complete :
#   docs/manoeuvre/ihm.md
#
# R7 (partiel) : le script expose une fabrique create_app(config) + serve()
#   (waitress, threads=1 pour serialiser l'etat pypowsybl partage ; repli sur
#   le serveur Flask de dev) et une route /healthz. La promotion physique dans
#   un package manoeuvre/ihm/ (blueprints, decoupage de Session, shim) et les
#   points M8 restent a faire (differes : la verification exige les 12 tests
#   IHM sous pypowsybl). Cf. docs/reviews/2026-07_full_code_review.md (R7/M8).
pip install -e ".[ihm]"   # guillemets requis sous zsh (sinon: pip install flask)
python scripts/manoeuvre_ihm.py --grid path/to/grid.xiidm   # http://localhost:8000
curl http://localhost:8000/healthz                          # sonde liveness/readiness

# Mode dataset RTE 7000 (sans --grid) : source les situations par date/heure
# dans le dataset HF OpenSynth/D-GITT-RTE7000-* (telechargement a la demande).
# Couche dans manoeuvre/dataset/source.py ; deploiement HF Space dans
# Dockerfile + deploy/huggingface/. Doc : docs/manoeuvre/ihm.md (S 1bis).
python scripts/manoeuvre_ihm.py --dataset                   # http://localhost:8000

# « Explorer la journee » (IHM, onglet RTE7000) : carte des postes localises +
# bilan des changements d'OC sur 3 situations (minuit/midi/23h). Cœur de calcul
# dans manoeuvre/dataset/exploration.py (changements par poste, par type d'OC) ;
# coordonnees resolues par manoeuvre/dataset/geographie.py : plan de masse RTE
# committe (grid_layout_rte.json, par VL, ~98%, hors-ligne, PRIMAIRE) -> snapshot
# -> OSM/Overpass (ref:FR:RTE = substation_id). ODRE est tabulaire (sans geometrie).
# Doc : docs/manoeuvre/ihm.md (S 1ter).
```

### Sous-package `dataset/` (historique + exploration)

| Fichier            | Role                                                       |
|--------------------|------------------------------------------------------------|
| `source.py`        | Source par date/heure du dataset HF (download a la demande)|
| `dgitt.py`         | Lecture XIIDM -> etats d'organes par VL ; chronologies     |
| `timeline.py`      | Chronologie de topologies, blocs de transition             |
| `tagging.py`       | Tags d'intervention (consignation, scission, fusion…)      |
| `exploration.py`   | **Intérêt d'une journée** : OC changés par poste/type + **re-groupements de nœuds** (scissions/fusions : ouvrages déplacés), top-10 |
| `geographie.py`    | **Coordonnées des postes** (plan de masse committé → snapshot → OSM) |
| `grid_layout_rte.json` | Plan de masse RTE committé (`{nom_VL: [x, y]}`, ~98 % des postes) |

## Conventions critiques

### pypowsybl `all_attributes=True`

**Toujours** appeler les getters pypowsybl avec `all_attributes=True` :
- `get_voltage_levels(all_attributes=True)` pour `topology_kind`
- `get_switches(all_attributes=True)` pour `node1`, `node2`
- `get_busbar_sections(all_attributes=True)` pour `node`
- `get_loads(...)`, `get_generators(...)`, etc. pour `node`

Sans ce flag, ces colonnes sont absentes et le code leve `KeyError`.
L'utilitaire `_safe_get()` dans `graph.py` gere cela automatiquement
pour les equipements ; les appels directs doivent le specifier.

### Structurel vs. Electrique

Deux niveaux d'analyse distincts :
- **Structurel** (BFS traversant tous les switches) -> `busbar_nodes`
- **Electrique** (uniquement switches fermes) -> `connected_busbars`

Ne pas confondre : une cellule peut atteindre 4 barres structurellement
mais n'etre connectee qu'a 1 electriquement.

### Topologies RTE specifiques

- **Re-aiguillage** : equipement raccorde directement a la barre via un
  seul sectionneur (SA), sans disjoncteur propre. Detecte par
  `CelluleDepart.is_reaiguillage`. Ne **pas** exiger de DJ sur ces cellules.

- **Omnibus / departs multiples** : equipements partageant les memes noeuds
  intermediaires. Signale par `CelluleDepart.is_multiple` et
  `shared_equipment_ids`.

- **Postes >= 3 barres** : le **placement** (`_placement_automatique`) gere N jeux
  de barres (Etape 1+2) et le **sequenceur** realise une cible > 2 noeuds, y
  compris via des **couplages multi-barres partages** (un meme DJ atteignant
  plusieurs barres, ex. COUPL.A/LIAIS) : Phase 0 evite de re-ponter via un organe
  partage (`bridge_breakers`) et la **Phase F** ouvre le lot minimal de DJ de
  couplage pour separer les noeuds, en raisonnant sur la **connectivite reelle**
  (no-op si la separation est deja acquise). Limite restante : le modele de cellule
  `CelluleCouplage` ne retient encore que 2 SJB par composante (warning sur > 2 SJB,
  cosmetique — le sequenceur utilise `_inter_sjb_couplers`, pas `CelluleCouplage`).

## Tests

### Fixtures

Les tests `test_postes_reels.py` utilisent des fixtures JSON dans
`tests/manoeuvre/fixtures/`. Elles sont generees par
`scripts/extract_test_fixtures.py` et reconstruisent le graphe NX
**sans pypowsybl** a l'execution (independance CI).

Le `fixture_loader.py` contient les fonctions de chargement :
`build_graph_from_fixture(vl_name)` est le point d'entree principal (tolere le
nommage point/espace -> underscore : VL `.OBER 7` <-> fixture `_OBER_7.json`).

**Corpus « caracteristiques particulieres »** (`test_postes_caracteristiques_
particulieres.py`) : 10 postes reels issus d'un balayage des 4018 VL NODE_BREAKER
du reseau France, couvrant des cas rares — **8 JdB** (`_OBER_7`), **7 JdB**
(`_VANY_7`), **24 SJB** sectionnement extreme (`_LAUF_7`), **26 departs**
(`P_GASP6`), organe interne 2 bornes (`CPNIEP6`), omnibus (`ROMAIP6`), departs
**deconnectes** (`_MUHL_6`)… Caracterisation structurelle + **innocuite** (graphe
non mute, organes existants, `is_verified` => topologie exacte).

### Reseau de reference

Les tests unitaires (`test_graph_cellules.py`) utilisent le reseau standard
`pp.network.create_four_substations_node_breaker_network()`, cible `S1VL2`.

### Filets de regression (refactors a iso-comportement)

Poses avant la campagne d'optimisation (cf. `docs/manoeuvre/optimisations.md`),
ces tests garantissent l'invariance du comportement :

- `test_golden_sequences.py` — **golden** de bout en bout : sequence ordonnee
  exacte + verifications + ecarts + partition, pour chaque `scenarios/*.json` x
  {smooth, aggressive}. Reference dans `tests/manoeuvre/goldens/`.
  **Regenerer consciemment** apres un changement de comportement assume :
  `UPDATE_GOLDENS=1 pytest tests/manoeuvre/test_golden_sequences.py`.
- `test_verificateurs_exact.py` — sorties exactes des verificateurs du
  sectionneur (alignement, indices, dedup, idempotence, non-mutation).
- `test_lookup_helpers.py` — contrat de `_is_open`/`_set_switch`/`_eq_node`
  (id inconnu, validite sur copie de graphe).
- `test_couplers_memoisation.py` — invariance a l'etat des couplers, purete,
  non-mutation de `poste.graph` par le pipeline.

### Filets pre-eclatement d'`algo.py` (#7)

Poses avant de scinder `algo.py` en sous-modules (un refactor structurel
**deplace** des symboles) ; couverture du module ~92 % :

- `test_public_api.py` — **verrou de surface publique** : chaque symbole de
  `__all__` reste importable depuis le package *et* depuis son sous-module
  d'origine (meme objet), entrypoints appelables, alias prive du verificateur
  conserve. Un oubli de reexport casse la CI immediatement.
- `test_multibarres_placement.py` — caracterisation du chemin **> 2 jeux de
  barres** (CORNIP3/GUARBP6/MORBRP6, 4 barres), non couvert par les goldens :
  `determiner_topo_complete_cible` (cible identite + cible scindee),
  `_main_busbar_sjb`, degradation gracieuse (`noeuds_non_realisables`), rendu
  `resume()`, non-mutation du graphe, organes emis existants.
- `test_algo_entrypoint_guards.py` — garde-fous de faisabilite
  (`determiner_topo_complete_cible`) : graphe absent, departs cibles absents.
- `test_graph_helpers.py` — `graph._safe_get` (repli `all_attributes`, erreurs).

## Performance & invariants internes

Plusieurs chemins chauds sont **memoises** ; toute modification doit preserver
ces invariants (sinon les goldens cassent). Detail et gains :
`docs/manoeuvre/optimisations.md`.

- **Index lookups** (`_switch_edge_index`, `_equipment_node_index`) : caches
  `G.graph` construits en une passe ; coordonnees **topologiques** (valides sur
  les copies). Garde-fou **O(1)** sur `number_of_nodes()` — ne **jamais**
  rebrancher sur `number_of_edges()` (O(aretes), annule le gain).
- **Couplers** (`_inter_sjb_couplers`) : memoise sur le poste — invariant a
  l'etat ouvert/ferme des organes (ne lit que la topologie + le tronconnement).
- **Verificateurs** : une **passe de rejeu unique** (`_rejeu_securite`) ; les
  fonctions publiques sont de minces delegateurs.
- **Placement** (`_placement_automatique`) : enumeration par **partitions
  connexes** (`_assignations_connexes`) + connexite memoisee + tie-break
  **lex-min** reproduisant l'ancienne enumeration `itertools.product`.
- **Chemins SA** (`CelluleDepart.disconnectors_vers_barre`) : memoise par SJB
  (sous-graphe de cellule fige -> resultat structurel invariant).

Invariant cle exploite partout : **`poste.graph` n'est jamais mute
structurellement** ; le sequenceur travaille sur des **copies** et ne bascule
que l'attribut `open`.

## Etat algo (phase 2)

> **Postes à N jeux de barres (≥ 3 JdB)** : état d'avancement et reste à faire
> consolidés dans **`docs/manoeuvre/postes_n_jeux_de_barres.md`**.

`determiner_topo_complete_cible(poste, topo_cible)` traite :
- postes 1 barre (cas trivial) ;
- postes 2 barres standard : evaluation de couplage (`nbNoeuds`/`nbBarres`),
  affectation noeud->barre par cout minimal, re-aiguillage boucle courte
  (couplage ferme) ou longue, ouverture/fermeture de couplage, verification
  post-manoeuvre par recalcul de `TopologieNodale.from_graph`.

Etape 1+2 (placement N jeux de barres) :
- le **scoping 2-JdB** de `_placement_automatique` a ete retire : la recherche
  exacte (`_recherche_exhaustive`, lex-min) tourne sur **toutes** les SJB et
  **tous** les couplers — couvre les postes 3B/4B reels dans le garde-fou
  (`MAX_COMBINAISONS_PLACEMENT`). Comportement 2-barres strictement preserve.
- au-dela du garde-fou : **decomposition recursive** (`_placement_decompose`)
  le long du graphe de couplage — par **composantes connexes** (exacte,
  separable) puis **bissection au niveau barre** (best-effort) ; tout placement
  declare complet est revérifie faisable (`_placement_est_faisable`).
- **Pénalité multi-barres** (`POIDS_NOEUD_MULTIBARRE`, postes > 2 barres
  uniquement) : `_recherche_exhaustive` pénalise (dominant) les nœuds dont les SJB
  couvrent plusieurs barres. Sur les postes à **faisceau de couplage partagé**
  (un DJ atteignant > 2 barres), `_inter_sjb_couplers` rend faussement réalisables
  des nœuds « exotiques » (demi-rames croisées, ex. `{1A,2B}`) ; la pénalité
  oriente le placement vers des nœuds **mono-barre / barres entières**,
  réalisables. Optimalité-coût préservée hors pénalité → **cas 2-JdB inchangés**.
- **Double candidat de placement (origin-preserving)** : la pénalité étant
  dominante, elle peut forcer un placement mono-barre qui **multiplie les
  ré-aiguillages** là où un nœud multi-barres *légitime* (barres entièrement
  couplées) serait plus économique. `_placement_automatique(..., penaliser_
  multibarre=False)` calcule le placement de **coût brut minimal** ;
  `determiner_topo_complete_cible` réalise **les deux** candidats et retient
  **transactionnellement** la réalisation **vérifiée** la moins coûteuse en
  manœuvres (le placement exotique non réalisable n'étant jamais vérifié, il est
  écarté → **aucune régression possible**). Effet : sur les postes 3 JdB testés
  (`determiner_topo_complete_cible` cible nodale d'un scénario détaillé), la cible
  détaillée auto-déterminée est **≤** en manœuvres que la cible faite main —
  `TAVELP7` converge **exactement** vers la cible visée (7 manœuvres vs 13 avant),
  `CHESNP7` 11→8, `TRI.PP7` 15→12
  (cf. `tests/manoeuvre/test_cible_detaillee_optimalite.py`).

Sequenceur N barres :
- Phase 0 (`bridge_breakers`) : ne ferme pas un coupler « meme noeud » via un
  organe partage avec une liaison inter-noeuds (eviter de re-ponter des barres).
- Phase F (separation) : tant que deux SJB de noeuds **differents** restent
  reliees, ouvre le **sous-ensemble minimal** de DJ de couplage qui les separe
  sans casser une connexite intra-noeud. Connectivite reelle (robuste a la
  mauvaise attribution des couplages partages par `_inter_sjb_couplers`).
  **No-op** quand la separation est deja acquise (postes 2 barres) -> goldens iso.
- **Realisateur connectivite-based** (`determiner_manoeuvres_par_connectivite`,
  etape 2/2 du redesign couplage) : repli pour le chemin **nodal > 2 barres** a
  faisceaux de couplage **partages**. (1) re-aiguillage des departs (maintien si
  deja dans le bon noeud), (2) sectionnements **intra-barre** par etat direct,
  (3) separation par ouverture du lot minimal de DJ (connectivite), (4) fusion.
  Branche **transactionnellement** par `determiner_topo_complete_cible` : retenu
  seulement s'il vERIFIE exactement la cible -> ne degrade jamais. **Realise les
  manoeuvres operationnelles** (separer les barres couplees ; tronconner un JdB en
  demi-rames) sur **les 7 postes 400 kV a 3 barres** testes (SSV.OP7, TAVELP7,
  TRI.PP7, ARGOEP7, CHESNP7, COR.PP7, CERGYP7) — cibles 3 a 7 noeuds, etats de
  service preserves (cf. `tests/manoeuvre/test_postes_3barres_400kv.py`).
  Non encore couverts : les regroupements **arbitraires** (round-robin) exigeant
  la *reconnexion* de departs deja deconnectes, et la cible **2 noeuds** sur un
  poste a barre de reserve fusionnee (exige un controle fin des SA de faisceau).
  Le realisateur est aussi branche en **repli only-on-failure** sur le chemin
  **detaille** multi-barres (`_sequence_detaillee_multibarres`) : si le
  re-aiguillage SJB-par-SJB n'atteint pas la partition, on tente la realisation
  par connectivite, transactionnellement.
- **Alignement detaille des faisceaux** (`_aligner_couplers_sur_cible`, etape 2/2
  bis) : une fois la **partition nodale** atteinte, ramene chaque **faisceau** de
  couplage a l'**etat d'organes exact** de la cible detaillee (de-energiser le DJ
  → SA hors charge → DJ a l'etat cible ; faisceaux actifs d'abord). Garde
  **transactionnelle de preservation de partition** (revert en bloc si la
  partition change). Supprime les ecarts de faisceau « cosmetiques » (faisceau
  electriquement equivalent mais d'organes differents) : en mode `aggressive` les
  7 postes 400 kV 3 JdB atteignent la **topologie detaillee exacte** (0 ecart) ;
  en `smooth`, partition exacte + alerte sectionneur (R18) possible sur faisceau
  partage. Applique dans les deux branches (repli realisateur **et** nodal verifie).
- **Sectionneur de ligne partage** (R9bis) : `_reaiguiller_vers_sjb` n'ouvre pas
  un sectionneur (`SL`) commun au chemin de la barre **cible** (sinon on
  deconnecte le depart de sa propre barre cible — manoeuvre parasite + cible non
  atteinte). Corrige le `OPEN … SL` parasite observe sur TAVELP7.
- **Mode agressif — boucle courte** (`_sequence_detaillee_aggressive` =
  wrapper transactionnel ; `_aggressive_impl(boucle_courte=…)`) : switche un départ
  d'une barre à une autre **au même potentiel** en **boucle courte** (CLOSE SA cible
  / OPEN ancien SA) **sans ouvrir son DJ** ; ne dé-énergise en lot que les ouvrages
  non équipotentiels + ceux à isoler pour ouvrir un sectionnement. Si la variante
  boucle courte n'est pas exacte (section devant être morte) → repli
  dé-énergisation groupée (jamais de régression). CPNIEP6 5 au lieu de 17.
- **Vérificateur « un seul ouvrage hors tension à la fois »** (R10ter,
  `ouvrages_simultanement_hors_tension`, public) : alerte **non bloquante** (mode
  smooth) listée dans `ResultatManoeuvres.alertes` quand > 1 ouvrage est
  **temporairement** hors tension (DJ propre initialement fermé, ouvert puis
  refermé) — **ré-aiguillage OU dé-énergisation de section**. Exclus : ouvrages
  **déjà déconnectés** et **mis hors service** (DJ final ouvert). Robuste (capte
  les voies multi-barres/connectivité, pas seulement le tag « boucle longue »).
  Mode agressif exempté. Séquence experte « 1 à la fois » → 0 alerte.
- **Vidage one-by-one + réduction cross-feed du côté à isoler**
  (`determiner_manoeuvres_avec_sections`, phase C) : pour ouvrir un sectionnement à
  deux côtés sous tension, (a) si le côté à isoler est un **nœud couplé**, on ouvre
  **temporairement** les couplages DJ frontières (hors charge) pour réduire la
  section morte à la **seule section adjacente** au sectionnement, puis on les
  referme ; (b) les ouvrages restants sont garés **un par un** sur le côté survivant
  (boucle courte) puis ramenés (boucle longue) ; sinon repli dé-énergisation en
  place. → **ROMAIP6 6 coupures simultanées → 0** (séquence ≈ experte 42 manœuvres).
  Limite : départs confinés à des sections mono-barre sans cross-feed → batch
  résiduel (signalé par l'alerte).
- **Candidat « diff minimal »** (`_sequence_detaillee_minimal_delta`, anti
  ferme-puis-rouvre) : `determiner_manoeuvres_cible_detaillee` combine la voie
  principale (renommee `_determiner_manoeuvres_cible_detaillee_principal`) avec un
  candidat qui ne manoeuvre **que les organes differents** de la cible
  (re-aiguillage des departs concernes + manoeuvre directe des couplers/
  sectionnements restants). Retenu **transactionnellement** s'il atteint
  **exactement** la cible detaillee et qu'il est **plus court** (un OPEN dangereux
  est consigne en ecart -> rejete ; les CLOSE de fusion sont surs). Supprime le
  « ferme-puis-rouvre » de couplers quand la cible n'est qu'un petit delta de
  l'etat courant : **MUHLBP7 9 au lieu de 37**, CARRIP3 1 noeud 2 au lieu de 20,
  TAVELP7 7 au lieu de 29 (cf. `test_muhlbp7_sequence_minimale.py`). Cohérence
  **omnibus** : `_ecarts_detailles` compare la barre cible sur la cellule
  **primaire** du départ (plus d'écart fantôme pour un organe partagé).

Limites connues (cf. docstring `algo.py`) :
- re-aiguillage d'omnibus complexes : partiel ;
- pas de verification fine de court-circuit avant fermeture de couplage ;
- la bissection de placement best-effort peut degrader gracieusement sur des
  postes a demi-rames tres maillees au-dela du garde-fou (la voie exacte les gere) ;
- modele `CelluleCouplage` : encore limite a 2 SJB par composante (cosmetique).

### Specification des regles

Toutes les regles metier du sequencement (R1-R19 : faisabilite, distinction
sectionnement/couplage, tronconnement, placement noeud->SJB, boucle courte/
longue, DJ d'ensemble de cellule, regle du sectionneur de barre, ordonnancement
listeDordre, controle court-circuit, verification) sont tracees avec leurs
fonctions et tests dans **`docs/manoeuvre/regles.md`**, ainsi que les regles
de **conformite « art de la manoeuvre »** (R20-R25, `algo/conformite.py`).
L'analyse critique de couverture face aux referentiels RTE (CCRT C3-3, ACT 104,
methode experte CCO), les regles specifiees non implementees (R26 validations
electriques, R27 orchestration multi-postes, R28 automates) et le plan de
restructuration module/algo/verificateur sont dans
**`docs/manoeuvre/art_de_la_manoeuvre.md`**.

### Convention de detection des barres

`troncons.py` distingue **sectionnement** (SA reliant deux SJB d'une meme barre)
de **couplage** (travee a DJ reliant deux barres). La barre d'une SJB est
deduite en priorite du **nommage RTE** (entier de tete apres `VL_id_`, ex.
`CARRIP3_1.1` -> barre 1) ; repli structurel par connectivite sectionnement
(chemins sans BREAKER) si le nommage est inexploitable.

## Dependances internes

```
models.py  <--  graph.py  <--  cellules.py  <--  troncons.py  <--  topologie.py
                                                                       ^
                                                                       |
   algo/  (results <- graph_ops <- placement/verification <- sequencing <- targets)
                                                                       |
                                                  __init__.py  (reexporte tout)
```

Les seules dependances externes du module sont `pypowsybl`, `networkx`,
et `pandas`. Pas de dependance a `grid2op` ni au reste du recommandeur.
