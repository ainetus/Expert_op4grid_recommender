# Postes à N jeux de barres (≥ 3 JdB) — état & reste à faire

> Extension du module `manoeuvre` des postes à **2 jeux de barres** vers **N JdB**
> (3 et plus). Voir aussi `expert_op4grid_recommender/manoeuvre/CLAUDE.md`
> (détails internes) et `docs/manoeuvre/ihm.md` (interface de test).
>
> Suivi de la PR *« Postes à N jeux de barres : placement, séquenceur
> connectivité-based et IHM »*.

## Contexte

Le module `manoeuvre` calcule la séquence de manœuvres réalisant une topologie
nodale cible dans un poste **node-breaker**. Il était historiquement limité aux
**postes à 2 jeux de barres** : le placement nœud → section de barre était bridé
aux 2 barres principales (les nœuds touchant une 3ᵉ barre étaient laissés à
l'opérateur). Cette extension le généralise à **3 barres et plus**, en
**réutilisant** la primitive 2-JdB plutôt qu'en la dupliquant.

Sur le réseau **France 28/08/2024**, ce n'est pas un cas marginal :
**59 postes 400 kV sur 562 ont ≥ 3 barres** (36 à 3 JdB, 17 à 4, jusqu'à 8).

## Postes de référence (fixtures)

7 postes 400 kV à 3 JdB réels, extraits dans `tests/manoeuvre/fixtures/` :

> `SSV.OP7`, `TAVELP7`, `TRI.PP7`, `ARGOEP7`, `CHESNP7`, `COR.PP7`, `CERGYP7`

Topologie type : **3 barres × 2 demi-rames (A/B)**, 6 SJB, et des **faisceaux de
couplage partagés** — un même disjoncteur (ex. `COUPL.A`, `LIAIS`) atteint
plusieurs barres par sélection de sectionneurs. C'est ce partage qui met en
défaut la décomposition par paires de `_inter_sjb_couplers` et a motivé le
redesign ci-dessous.

## Ce qui est fait

### 1. Placement N-barres — `algo/placement.py`, `algo/_constants.py`
- **Retrait du scoping « 2 JdB »** : `_placement_automatique` tourne sur **toutes**
  les SJB ; recherche exacte (`_recherche_exhaustive`, lex-min).
- **Décomposition récursive** `_placement_decompose` au-delà du garde-fou
  combinatoire : par **composantes connexes** du graphe de couplage (exacte,
  séparable) puis **bissection au niveau barre** (best-effort).
- **Pénalité `POIDS_NOEUD_MULTIBARRE`** (gated > 2 barres) : oriente le placement
  vers des nœuds **mono-barre / barres entières**, en évitant les nœuds
  « exotiques » (demi-rames croisées) faussement réalisables via les faisceaux
  partagés. **Comportement 2-JdB strictement préservé.**
- **Double candidat « origin-preserving »** : la pénalité dominante pouvait forcer
  un placement mono-barre **multipliant les ré-aiguillages** là où un nœud
  multi-barres légitime (barres entièrement couplées) serait moins coûteux.
  `determiner_topo_complete_cible` réalise désormais **aussi** le placement de
  coût brut minimal (`penaliser_multibarre=False`) et retient
  **transactionnellement** la réalisation **vérifiée** la moins coûteuse en
  manœuvres → la cible détaillée auto-déterminée est **toujours ≤** en manœuvres
  que la cible faite main (à partition nodale égale). **Aucune régression
  possible** (un placement exotique non réalisable n'est jamais vérifié).

### 2. Séquenceur bay-aware — `algo/sequencing.py`, `algo/targets.py`
- **Phase 0** (`bridge_breakers`) : ne ferme pas un coupler « même nœud » via un
  organe partagé avec une liaison inter-nœuds (pas de re-pontage de barres).
- **Phase F** : ouverture du **lot minimal** de DJ de couplage par **connectivité
  réelle**, **transactionnelle** (annulée si la cible n'est pas exacte) →
  *no-op* sur les postes 2 barres.
- **`determiner_manoeuvres_par_connectivite`** : réalisateur connectivité-based
  (ré-aiguillage avec maintien en place + sectionnements intra-barre par état
  direct + séparation/fusion par connectivité). Branché **transactionnellement**
  par `determiner_topo_complete_cible` (chemin nodal) **et** en **repli
  only-on-failure** sur le chemin **détaillé** (`_sequence_detaillee_multibarres`)
  : retenu seulement s'il vérifie exactement la cible → **ne peut jamais dégrader**
  un résultat correct.
- **`_aligner_couplers_sur_cible`** (alignement détaillé des faisceaux) : une fois
  la partition nodale atteinte, ramène chaque **faisceau** de couplage à l'**état
  d'organes exact** de la cible (dé-énergiser le DJ → SA hors charge → DJ à l'état
  cible ; faisceaux actifs d'abord), avec **garde transactionnelle de préservation
  de partition** (revert en bloc). Supprime les écarts de faisceau « cosmétiques »
  (faisceau électriquement équivalent mais d'organes différents). En mode
  `aggressive`, les cibles 3 JdB sont atteintes **exactement** (0 écart).
- **Sectionneur de ligne partagé** (R9bis) : `_reaiguiller_vers_sjb` n'ouvre plus
  un sectionneur (`SL`) commun au chemin de la barre **cible** → supprime le
  `OPEN … SL` parasite observé sur `TAVELP7` (le départ restait sur sa barre cible).
- **Candidat « diff minimal »** (`_sequence_detaillee_minimal_delta`) : quand la
  cible n'est qu'un **petit delta** de l'état courant, la voie placement
  **fermait** des couplers pour reconstruire la partition puis les **rouvrait** à
  l'alignement (manœuvres inutiles). On calcule désormais **aussi** un candidat
  qui ne manœuvre **que les organes différents** (ré-aiguillage des départs +
  manœuvre directe des couplers/sectionnements restants), retenu
  **transactionnellement** s'il est **exact** et **plus court** → **MUHLBP7 : 9
  manœuvres au lieu de 37**, CARRIP3 1 nœud : 2 au lieu de 20, TAVELP7 : 7 au lieu
  de 29. Cohérence **omnibus** : plus d'écart fantôme pour un organe partagé
  (`_ecarts_detailles` compare sur la cellule primaire).

### 2bis. Modes de manœuvre smooth / agressif (cf. R10ter)
- **Agressif — boucle courte** (`_sequence_detaillee_aggressive`) : switche un
  départ d'une barre à l'autre **au même potentiel** **sans ouvrir son DJ** ;
  ne dé-énergise en lot que le non-équipotentiel + l'isolation de section.
  **Transactionnel** : repli dé-énergisé si la boucle courte n'est pas exacte
  (jamais de régression). Ex. **CPNIEP6** : 5 manœuvres au lieu de 17.
- **Smooth — « un seul ouvrage hors tension à la fois »** : pour ouvrir un
  sectionnement, (a) on **réduit la section morte** en ouvrant **temporairement**
  les couplages DJ de cross-feed (nœud couplé), (b) on **gare un par un** les
  départs restants sur le côté survivant (boucle courte) puis on les **ramène**
  (boucle longue). Ex. **ROMAIP6** : **6 coupures simultanées → 0** (séquence ≈
  experte, 42 manœuvres). Limite : départs confinés à des sections mono-barre sans
  cross-feed → batch résiduel signalé par l'alerte.
- **Vérificateur + alerte** `ouvrages_simultanement_hors_tension` (public,
  `ResultatManoeuvres.alertes`, **non bloquant**) : signale tout moment où > 1
  ouvrage est **temporairement** hors tension (hors ouvrages déjà déconnectés / mis
  hors service). Séquence experte « 1 à la fois » → 0 alerte.

### 3. IHM — `scripts/manoeuvre_ihm.py`, `…_assets/index.html`
- Les 7 postes 3-JdB sont **épinglés** (`POSTES_TEST`) ; **sélecteur par typologie**
  📂 (`POSTES_CATALOG` : ≥5 JdB, sectionnement extrême, faisceau partagé, organes
  internes, omnibus, départs déconnectés…) — `/api/postes` renvoie `catalog`.
- `Session.all_postes` = **tous** les VL NODE_BREAKER ; champ de **recherche**
  pour inspecter/tester n'importe quel poste de la situation.
- Endpoint **`POST /api/load_grid`** : charge dynamiquement une autre situation
  `.xiidm` sans relancer le serveur.
- **Topologie nodale qui suit l'étape** : `GET /api/step` renvoie désormais la
  **partition nodale de l'état détaillé de l'étape** (`step_view` → 6 valeurs) ;
  le volet « cible » se met à jour au fil de l'animation départ → … → cible.
- **Alerte « 1 ouvrage à la fois »** : `/api/sequence` renvoie `alertes` ; l'IHM
  affiche un badge ⚠ (bonne pratique, non bloquant).

### Couverture vérifiée (cibles 3 ET 4 nœuds, manœuvres opérationnelles)

| Manœuvre | Nœuds | Postes vérifiés |
|---|---|---|
| **Séparer les barres couplées** | 3 à 7 | 6/7 (skip documenté : `SSV.OP7` à 2 nœuds, barre de réserve) |
| **Tronçonnage** (demi-rames) | 4 à 8 | **7/7** |
| **Topologie détaillée exacte** (0 écart, mode `aggressive`) | 3 | `CHESNP7`, `TRI.PP7`, `TAVELP7` (goldens) |
| **Optimalité nodale→détaillée** (`T_ALGO ≤ T_VISÉE`) | 3 à 7 | `CHESNP7` (8 vs 23), `TAVELP7` (7 = 7, identique), `TRI.PP7` (12 vs 33) |
| **Agressif boucle courte** (sans déconnexion) | 2 | `CPNIEP6` (5 vs 17) |
| **Smooth « 1 ouvrage à la fois »** (0 coupure simultanée) | 3 | `ROMAIP6` (= experte, 42) |

Tests : `tests/manoeuvre/test_ssv_op7_3jdb.py`, `test_placement_decomposition.py`,
`test_postes_3barres_400kv.py`, `test_ihm_postes_3barres.py`,
`test_ihm_step_nodale.py`, `test_sequences_sauvegardees_3barres.py`,
`test_cible_detaillee_optimalite.py`, `test_muhlbp7_sequence_minimale.py`,
`test_mode_agressif_et_un_seul_ouvrage.py`, `test_un_seul_ouvrage_hors_tension.py`,
`test_romaip6_nodale.py`, `test_postes_caracteristiques_particulieres.py`,
`test_golden_sequences.py` (goldens `CHESNP7_cible_3noeuds__*`,
`TRI.PP7_cible_3_noeuds__*`, `TAVELP7_cible_3noeuds__*`).
**Suite `manoeuvre/` : 873 passed, 7 skipped, 0 régression** (goldens régénérés
consciemment — réductions de manœuvres / vidage one-by-one, exactitude préservée ;
dont `MORBRP6` 4-barres).

### Corpus « caractéristiques particulières » (10 postes réels)

Balayage des **4018 VL NODE_BREAKER** du réseau France → 10 postes choisis pour
couvrir des caractéristiques rares non testées par ailleurs, extraits en fixtures
et couverts par `tests/manoeuvre/test_postes_caracteristiques_particulieres.py`
(caractérisation structurelle + **innocuité** sur cibles séparer/round-robin) :

| Fixture | VL réel | Caractéristique |
|---|---|---|
| `_OBER_7` | `.OBER 7` | **8 jeux de barres** (max réseau), faisceaux partagés massifs (66 couplers) |
| `_VANY_7` | `.VANY 7` | **7 jeux de barres**, anneau de couplers |
| `_ZAND_7` | `.ZAND 7` | 6 barres, **12 SJB** multi-section, organes internes |
| `MUHLBP7` | `MUHLBP7` | 5 barres, 10 SJB, faisceaux partagés |
| `_LAUF_7` | `.LAUF 7` | **24 SJB** (sectionnement extrême), 4 barres, anneau |
| `P_GASP6` | `P.GASP6` | **26 départs** (max), 225 kV — séparer-barres **réalisé** |
| `CPNIEP6` | `CPNIEP6` | **organe interne 2 bornes** (self/réactance), 4 barres |
| `ROMAIP6` | `ROMAIP6` | **omnibus** (départs multiples ×4), 8 SJB |
| `REICHP3` | `REICHP3` | **14 SJB** (sectionnement extrême), 63 kV |
| `_MUHL_6` | `.MUHL 6` | **10 départs déconnectés** (nœuds 0-barre), 3 barres |

> **Réalisation mode-dépendante** : le mode `smooth` garantit la **partition
> nodale** exacte (`is_verified`) ; le mode `aggressive` (alignement d'organes)
> atteint la **topologie détaillée exacte** (`is_verified_detaillee`, 0 écart) sur
> les cibles 3 JdB. Le test de scénario est désormais **mode-conscient** (partition
> exigée des deux modes ; exactitude détaillée d'au moins un —
> `test_scenarios_sauvegardes.py::test_scenario_atteint_topologie_detaillee`).
> Le chargeur de fixtures tolère le **nommage point/underscore**
> (`TRI.PP7` ↔ `TRI_PP7.json`).

## Reste à faire

### Séquenceur (raffinements)
- **Mode `smooth` sur faisceau partagé** : l'alignement détaillé
  (`_aligner_couplers_sur_cible`) atteint la partition nodale exacte mais peut
  émettre une **alerte sectionneur** (R18) consignée en écart plutôt que de
  reconfigurer un faisceau partagé sous charge. Le mode `aggressive` atteint la
  topologie détaillée **exacte** (0 écart). → Les écarts de faisceau « cosmétiques »
  des cibles 3 JdB testées (`CHESNP7`, `TRI.PP7`) sont **résolus** en `aggressive`.
- **Reconnexion de départs déconnectés** : une cible regroupant des départs déjà
  hors-service exige une *mise en service* (DJ + SA) que le réalisateur ne fait
  pas — les regroupements arbitraires (round-robin) ne sont donc pas tous réalisés.
- **Contrôle SA-par-SA des faisceaux partagés** : le cas dégénéré « 2 nœuds +
  barre de réserve fusionnée » (`SSV.OP7` *séparation*) exige d'ouvrir/fermer des
  SA de faisceau **individuels** ; le réalisateur ne bascule que les DJ en bloc.
  (1 test skippé.)
- **Smooth « 1 ouvrage à la fois » — batch résiduel** : `ROMAIP6` est désormais à
  **0** coupure simultanée (parking one-by-one + réduction cross-feed). Restent des
  postes (ex. `SSAVOP3` 12, `CZBEVP3` 2) où les départs à isoler sont **confinés à
  des sections mono-barre** sans côté survivant atteignable **ni** couplage de
  cross-feed à ouvrir → dé-énergisation en place groupée inévitable avec les
  mécanismes actuels (parking / ouverture temporaire). Le **vérificateur les
  signale** (alerte non bloquante). Résolution : modéliser les points de **piquage**
  (PIQU) ou fournir une séquence experte de référence par poste.

### Modèle de données
- **`CelluleCouplage`** n'enregistre encore que **2 SJB** par composante de
  couplage (`cellules.py:474`, warning) — cosmétique : le séquenceur utilise
  `_inter_sjb_couplers`, pas `CelluleCouplage`.
- **Bug de nommage `RAN.PP6` / `RAN_PP6`** : `troncons._barres_par_nommage`
  sous-compte la fixture à 2 barres au lieu de 4 (mismatch point/underscore entre
  l'id du VL et le *stem* de la fixture). Le **chargeur de fixtures** tolère
  désormais ce mismatch (`fixture_loader.load_fixture_json`, `TRI.PP7` ↔
  `TRI_PP7.json`) ; la déduction de barre par nommage dans `troncons.py` reste à
  durcir.

### Discovery — chaînon **end-to-end** manquant pour le recommandeur
- `discovery/_node_splitting.py` est **binaire** (`buses = [1, 2]`, un seul
  `bus_of_interest`) : le recommandeur ne *propose* pas encore de cible 3 nœuds —
  il ne saurait que l'*exécuter*.
- `discovery/_node_merging.py` code en dur un collapse total
  (`topo_target = [1 if bus_id >= 2 else bus_id …]`) → fusions **par paires** à
  introduire.
- Alimenter `dict_action` en actions **3-bus** (le backend supporte déjà
  `n_busbar_per_sub ≥ 3`).

### IHM (ergonomie)
- **Upload `.xiidm` côté client** : `/api/load_grid` prend pour l'instant un
  chemin **côté serveur**.
- **Filtres de recherche** (par tension P3/P6/P7, par nombre de barres) sur les
  milliers de postes d'une situation complète.

## Commandes

```bash
# Tests du module (postes N barres inclus)
pytest tests/manoeuvre/ -q

# IHM : inspecter / modifier / tester n'importe quel poste d'une situation
python scripts/manoeuvre_ihm.py --grid /chemin/vers/grid.xiidm   # http://localhost:8000
```
