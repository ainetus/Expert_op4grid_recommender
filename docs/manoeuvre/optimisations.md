# Module Manœuvre — Revue de code & campagne d'amélioration

> Rapport de la revue critique (architecture / qualité / performance) du module
> `manoeuvre` et de son IHM : **diagnostic**, **filets de sécurité** posés avant
> refactor, **refactors à iso‑comportement** réalisés, **gains mesurés** et
> **résultats**.
>
> Doc connexes : [`manoeuvre_module.md`](module.md) ·
> [`manoeuvre_regles.md`](regles.md) ·
> [`manoeuvre_ihm.md`](ihm.md) ·
> [`expert_op4grid_recommender/manoeuvre/CLAUDE.md`](../../expert_op4grid_recommender/manoeuvre/CLAUDE.md)

---

## 0. Synthèse — état final

Les **10 propositions** de la revue initiale sont **traitées**. Détail et
résultats dans les sections suivantes.

| # | Proposition | Catégorie | Statut | Résultat clé |
|---|---|---|---|---|
| 1 | Index lookups O(1) (`_switch_edge_index` / `_equipment_node_index`) | Perf | ✅ | scans O(E)→O(1) ; PALUNP3 ~2,2× |
| 2 | Mémoïsation `_inter_sjb_couplers` | Perf | ✅ | recalcul ~10×→1× par poste |
| 3 | Passe de rejeu unique des vérificateurs | Perf/Qualité | ✅ | 3–4 rejeux→1 ; fonctions publiques = délégateurs |
| 4 | Vérificateur de règle **public** | Qualité IHM | ✅ | `sectionneurs_sous_charge_par_manoeuvre` exporté ; IHM ne franchit plus la frontière privée |
| 5 | Gestionnaire de contexte `applied(state)` (IHM) | Qualité IHM | ✅ | restauration d'état garantie (même sur exception) |
| 6 | Load flow paresseux + cache graphe (IHM) | Perf IHM | ✅ | graphe/topo/load‑flow mémoïsés par état |
| 7 | Éclatement d'`algo.py` | Architecture | ✅ | package `algo/` en couches (8 modules), surface inchangée |
| 8 | Externalisation du front‑end de l'IHM | Architecture | ✅ | HTML/CSS/JS → asset ; serveur 1650→1050 lignes |
| 9 | Constantes nommées + imports remontés | Qualité | ✅ | poids/garde‑fous documentés |
| 10 | `ruff`/`interrogate`/`radon` en CI | Qualité outillée | ✅ | job `quality` bloquant ; **`ruff` élargi à tout le dépôt** |

**Au‑delà des 10 items** : campagne de **placement combinatoire** (P1/P1+/Q1,
SSAVOP3 **~27×**), **renforcement de la couverture** (~92 % sur le module) et
**baseline `ruff` « ratchet »** pour le legacy.

État de la suite : **650 tests manœuvre verts**, goldens **stables sur ≥ 3
graines de hash**, `ruff check .` **vert**, `interrogate` ≥ 80 %.

---

## 1. Méthode

Chaque refactor a suivi la même discipline :

1. **Diagnostic mesuré** : profilage (`cProfile`) plutôt que lecture statique
   seule — à deux reprises le profil a corrigé l'intuition.
2. **Filet de sécurité d'abord** : tests posés et **verts sur le code actuel**
   *avant* de toucher au code, de sorte que tout écart de comportement soit
   détecté immédiatement.
3. **Refactor à iso‑comportement**, validé par les filets (notamment des
   *goldens* comparés **octet pour octet**), puis commité isolément.
4. **Gain / résultat mesuré** en A/B (état pré‑refactor vs après).

---

## 2. Diagnostic initial (revue critique)

### Architecture
- `algo.py` est un module « god » (~2 450 lignes, 45 symboles, 4 points
  d'entrée) mêlant placement, séquencement, vérification et helpers bas niveau.
- **Pas d'index** : `_is_open` / `_set_switch` / `_eq_node` retrouvaient un
  organe/nœud par **scan linéaire** du graphe (82 sites d'appel).
- `_inter_sjb_couplers` **recalculé ~10×/analyse** (all‑pairs SJB + retraits
  d'arêtes), alors qu'il ne dépend que de la topologie.
- IHM : god‑file mêlant Flask, rendu pypowsybl, parsing SVG et ~600 lignes de
  HTML/JS embarqué ; franchit la frontière privée (`_sectionneurs_sous_charge_…`) ;
  paires `apply(state)`/`apply(current)` disséminées ; graphe et load flow
  reconstruits à chaque vue.

### Qualité
- Modes d'échec **silencieux** (`_set_switch` no‑op, `_is_open`→True sur id
  inconnu).
- Vérificateurs rejouant chacun la séquence (3–4 rejeux).
- Constantes magiques de coût/seuils non nommées ; imports locaux dispersés.
- Lint/docstrings non outillés en CI ; couverture non mesurée.

### Performance
- **O(E) dans des boucles combinatoires** : `_placement_automatique` énumère
  k^(nb SJB) affectations en testant `nx.is_connected` à répétition.
- Reconstructions de graphes et **recalculs de chemins structurels** dans des
  boucles chaudes.

---

## 3. Filets de sécurité (posés avant tout refactor)

| Filet | Fichier | Rôle |
|---|---|---|
| **Déterminisme** | `algo.py` (`a5c7de6`) | Canonicalisation de l'ordre d'émission (itérations d'ensembles triées) — **prérequis** pour qu'un golden d'ordre exact soit stable d'un process à l'autre (`PYTHONHASHSEED`). |
| **T1 — Golden** | `tests/manoeuvre/test_golden_sequences.py` | Caractérisation de bout en bout sur `scenarios/*.json` × {smooth, aggressive} (24 goldens) : séquence ordonnée exacte + vérifications + écarts + partition. Régénération consciente via `UPDATE_GOLDENS=1`. |
| **T2 — Vérificateurs exacts** | `tests/manoeuvre/test_verificateurs_exact.py` | Égalité de liste exacte (alignement, indices d'infraction, dédup) + idempotence + non‑mutation du graphe. |
| **T3 — Contrat lookups** | `tests/manoeuvre/test_lookup_helpers.py` | Contrat de `_is_open`/`_set_switch`/`_eq_node` (id inconnu, validité sur copie, « tout switch_id émis existe »). |
| **T4 — Invariants couplers** | `tests/manoeuvre/test_couplers_memoisation.py` | Préconditions du cache : invariance à l'état des organes, idempotence, non‑mutation de `poste.graph` par le pipeline. |

> Le golden a immédiatement révélé un **non‑déterminisme réel** (ordre de blocs
> de dé‑énergisation dépendant du hash) — d'où le fix `a5c7de6` *avant* les
> refactors.

---

## 4. Performance — refactors à iso‑comportement (#1–#3, P1, P1+, Q1)

| # | Commit | Diagnostic | Action | Garantie |
|---|---|---|---|---|
| **#1** | `bda3234` | scans O(E) des lookups | index `switch_id→arête` / `equipment_id→nœud` mémoïsés sur `G.graph` (valides sur copies : coordonnées topologiques) | contrat id‑inconnu préservé (T3) |
| **#2** | `90cd0c2` | `_inter_sjb_couplers` recalculé ~10× | mémoïsation sur le poste (invariant à l'état — T4) | goldens inchangés |
| **#3** | `ba67820` | 3 rejeux des vérificateurs | passe unique `_rejeu_securite` + agrégateur `_verifier_regles` ; fonctions publiques = délégateurs | sortie exacte (T1+T2) |
| **P1** | `b37ed42` | `nx.is_connected` recalculé ~k^n fois | connexité **mémoïsée** par sous‑ensemble + hoist de l'invariant `currently_closed` | même ordre, même tie‑break |
| **P1+** | `ff6a14f` | énumération k^n filtrée sur la connexité | génération **directe** des partitions connexes × bijections ; **tie‑break lex‑min** reproduisant `product` | ensemble de candidats identique → coût min identique ; sortie prouvée identique |
| **Q1** | `12de96b` | garde‑fou d'index `number_of_edges()` O(E) ; `_edges_of_switches` O(E) ; `disconnectors_vers_barre` recalculé 27 k× | garde O(1) (`number_of_nodes`) ; index ; **mémoïsation** du chemin SA structurel sur la cellule | structurel/invariant → sortie identique |

### Note méthodologique
Pour **P1** comme pour **Q1**, le **profil a corrigé la lecture statique** :
- la « recherche combinatoire » était bien le coût dominant des gros postes
  (P1), mais le hotspot transverse réel n'était **pas** les reconstructions de
  graphes (`_equipotentiel`…), négligeables au profilage, **mais** le
  recalcul d'un **plus‑court‑chemin structurel** (`disconnectors_vers_barre`)
  appelé des dizaines de milliers de fois (Q1) ;
- le garde‑fou O(E) introduit par **#1** lui‑même (`number_of_edges()`) annulait
  une partie de son bénéfice — corrigé en Q1.

### Gains mesurés

A/B sur fixtures (moyenne sur N analyses, `networkx` réel) :

| Cas | pré‑#1 | post #1‑#3 | P1 | P1+ | Q1 |
|---|---|---|---|---|---|
| **SSAVOP3** (8 SJB, 62 manœuvres) | 1201 ms | 1184 ms | 200 ms | 76 ms | **44 ms** |
| **PALUNP3** (4 SJB) | 22,9 ms | 17,2 ms | 15 ms | 20 ms | **10,5 ms** |
| **Corpus** (12 scénarios × 2 modes) | — | — | — | 349,6 ms | **232,7 ms** |

- **SSAVOP3 : ~27×** (1201 → 44 ms) — porté par P1/P1+ (placement) puis Q1.
- **Corpus : ~1,5×** sur le seul Q1 (mémoïsation du chemin SA, transverse).
- **PALUNP3 : ~2,2×** — porté par les lookups O(1) (#1 + garde Q1).

### Garanties d'iso‑comportement (perf)

- **Goldens octet‑pour‑octet** sur 12 scénarios × 2 modes, **stables sur ≥ 3
  graines de hash** — rejoués après *chaque* refactor.
- **P1+** : l'énumération par partitions connexes explore **le même ensemble de
  candidats** que `itertools.product` filtré (donc même coût minimal) ; un
  **tie‑break lex‑min** reproduit le choix de `product` même en cas d'égalité de
  coût hors corpus → équivalence **prouvée sur toute entrée**, pas seulement
  constatée.
- Mémoïsations (#2, Q1) : portées par des **invariants vérifiés par tests**
  (indépendance à l'état des organes, non‑mutation des structures).

---

## 5. Qualité de l'IHM (#4, #5, #6)

Refactors de `scripts/manoeuvre_ihm.py` ; filets dans
`tests/manoeuvre/test_ihm_cache_and_api.py`.

- **#4 — vérificateur de règle public** : le contrôle « sectionneur sous charge,
  manœuvre par manœuvre » est exposé sous
  `manoeuvre.sectionneurs_sous_charge_par_manoeuvre` (réexporté par `__init__`,
  testé par `test_public_api.py`). L'IHM ne franchit plus la frontière privée ;
  l'alias `_sectionneurs_sous_charge_par_manoeuvre` est conservé pour compat.
- **#5 — gestionnaire de contexte `applied(state)`** (`Session.applied`) :
  applique temporairement un état détaillé au réseau puis **restaure l'état
  d'affichage courant en sortie, même sur exception**. Remplace les paires
  `apply(state)` … `apply(self.current)  # restaurer` disséminées (sources
  d'oublis et de fuites d'état entre requêtes).
- **#6 — load flow paresseux + cache graphe** (`Session._graph` / `_topo` /
  `_flows`) : le graphe NX, la topologie nodale et le **load flow** d'un VL ne
  dépendent que du VL et de l'état des organes (injections constantes dans
  l'IHM) ; ils sont **mémoïsés par état**, invalidés au chargement d'un poste
  (`load()`). Le load flow n'est exécuté qu'à la demande (vue nodale détaillée)
  et **une seule fois par état** — la navigation pas‑à‑pas et les re‑rendus
  successifs n'entraînent plus de reconstruction de graphe ni de recalcul de
  flux.

**Résultat** : code IHM plus sûr (pas de fuite d'état entre requêtes), plus
rapide (mémoïsation), et découplé de l'interne du module (#4). Tests IHM verts.

---

## 6. Architecture (#7, #8)

### #7 — Éclatement d'`algo.py` en package `algo/`

L'ancien module « god » `algo.py` (~2 450 lignes, 45 symboles, 4 points d'entrée)
est éclaté en un **package `algo/` en couches** à dépendances strictement
descendantes (sans cycle) :

```
_constants / results  →  graph_ops  →  placement / verification  →  sequencing  →  targets
```

| Sous‑module | Rôle |
|---|---|
| `_constants.py` | poids de placement, garde‑fous combinatoires |
| `results.py` | `Manoeuvre`, `ResultatManoeuvres` |
| `graph_ops.py` | index O(1), `_is_open`/`_set_switch`, chemins SA, `_inter_sjb_couplers` |
| `placement.py` | `_placement_automatique` (+ best‑effort/glouton), `_main_busbar_sjb` |
| `verification.py` | `_rejeu_securite`, `sectionneurs_sous_charge_par_manoeuvre`, `_optimiser_sequence` |
| `sequencing.py` | `determiner_manoeuvres_avec_sections`, ré‑aiguillages |
| `targets.py` | `determiner_topo_complete_cible` / `_cible_detaillee` (smooth/aggressive/multibarres) |

Le package **réexporte intégralement** la surface de l'ancien module (publics
*et* privés, ré‑exports explicites `X as X`), si bien que `manoeuvre.algo.X` et
`from …algo import X` restent **inchangés pour les 23 sites d'import** (tests,
scripts, IHM, `__init__`).

**Méthode (iso‑comportement)** : layering **dérivé du graphe d'appels réel**
(DAG vérifié acyclique) ; chaque symbole **déplacé verbatim**, corps **vérifiés
identiques par comparaison d'AST symbole‑par‑symbole** ; aucun golden ni test
modifié. Le filet `test_public_api.py` (verrou de surface) aurait attrapé tout
oubli de réexport.

### #8 — Externalisation du front‑end de l'IHM

Les ~600 lignes de HTML/CSS/JS embarquées dans la constante `PAGE` de
`scripts/manoeuvre_ihm.py` sont déplacées **verbatim** dans
`scripts/manoeuvre_ihm_assets/index.html`, chargé au démarrage du module et
servi tel quel par la route `GET /`. **Le serveur Python passe de ~1650 à ~1050
lignes** ; le front s'édite sans toucher au code serveur. Filet :
`test_ihm_frontend_asset.py` (asset présent, `PAGE == asset`, `GET /` sert
l'asset, plus aucun bloc HTML dans le `.py`).

---

## 7. Qualité du code & outillage (#9, #10)

- **#9 — constantes nommées + imports remontés** (`algo/`, `manoeuvre_ihm.py`) :
  poids de coût (`POIDS_*`) et garde‑fous combinatoires (`MAX_COMBINAISONS_*`)
  extraits en constantes documentées (`algo/_constants.py`) ;
  `itertools`/`Counter`/`re` remontés en tête de module.
- **#10 — qualité en CI** : job `quality` (CircleCI), sans dépendances lourdes —
  `ruff` (lint, **bloquant**), `interrogate` (docstrings ≥ 80 %, **bloquant**),
  `radon` (complexité, **indicatif**). Configuration dans `pyproject.toml`
  (`[tool.ruff.lint]`, `[tool.interrogate]`).

### Élargissement du gate `ruff` au dépôt entier

Le gate `ruff` passe de « scopé `manoeuvre` » à **`ruff check .` (tout le
dépôt)** ; `interrogate`/`radon` restent scopés `manoeuvre`.

- **~105 violations triviales corrigées automatiquement** (imports/variables
  inutiles, `f""` sans champ, `not x in` → `x not in`…) — fixes **sûrs**,
  iso‑comportement, **aucun fichier source du module `manoeuvre` touché**.
- **Dette legacy résiduelle (171 violations** : `E701/E702` multi‑statements,
  `F403/F405` star‑imports, `F841` variables inutilisées…**) grandfatherée** via
  une **baseline « ratchet »** (`[tool.ruff.lint.per-file-ignores]`, **38
  fichiers**) : le gate reste vert **tout en capturant toute NOUVELLE
  violation** — nouveau fichier, ou nouveau code dans un fichier listé (vérifié
  empiriquement).
- **Invariant** : le module `manoeuvre` (code, tests, IHM) n'a **aucune** entrée
  dans la baseline → reste **strictement propre**. Ne jamais l'y ajouter.

> La baseline a aussi mis en évidence des défauts latents dans le legacy (ex.
> un test silencieusement masqué — `F811` redefinition ; des noms indéfinis —
> `F821`), désormais tracés et capturés pour la suite.

---

## 8. Couverture de tests

Avant l'éclatement d'`algo.py` (#7, refactor structurel), la **couverture a été
mesurée puis renforcée** pour servir de filet :

- Couverture du module portée à **~92 %** (`algo.py` 89→92 %, `graph.py`
  74→81 %).
- Filets ajoutés :
  - `test_public_api.py` — **verrou de surface publique** : chaque symbole de
    `__all__` importable depuis le package *et* son sous‑module d'origine (même
    objet), entrypoints appelables, alias privé conservé.
  - `test_multibarres_placement.py` — caractérisation du chemin **> 2 jeux de
    barres** (CORNIP3/GUARBP6/MORBRP6, 4 barres), **non couvert par les
    goldens** : `determiner_topo_complete_cible` (identité + cible scindée),
    `_main_busbar_sjb`, dégradation gracieuse, `resume()`, non‑mutation du
    graphe, organes émis existants.
  - `test_algo_entrypoint_guards.py` — gardes de faisabilité (graphe absent,
    départs cibles inconnus).
  - `test_graph_helpers.py` — `graph._safe_get` (repli `all_attributes`,
    erreurs).
  - `test_ihm_cache_and_api.py`, `test_ihm_frontend_asset.py` — IHM (#4–#6, #8).

**Résultat** : **650 tests manœuvre verts**, déterministes sur plusieurs graines
de hash.

---

## 9. Reste à faire / limites

- **Performance** : le coût résiduel des gros postes est l'**énumération
  combinatoire** de `_placement_automatique` (intrinsèque) ; la franchir
  demanderait un placement non exhaustif (heuristique/branch‑and‑bound), qui
  **pourrait** changer la sortie — non entrepris pour préserver l'iso‑comportement.
- **Couverture** : branches de **dégradation profonde** non testées
  (`_placement_greedy` ; certaines branches de reconnexion/infaisabilité de
  `determiner_manoeuvres_cible_detaillee`) — ROI faible et risque de tests
  fragiles ; les goldens bout‑à‑bout restent le garde‑fou principal.
- **Dette `ruff`** : **résorption progressive de la baseline** (171 violations
  legacy hors `manoeuvre`), désormais sous contrôle du gate « ratchet ».
- **Postes ≥ 3 barres** : le placement nodal‑only reste **partiel** (scoping aux
  2 JdB principaux + best‑effort) ; le chemin détaillé multi‑barres est géré par
  `_sequence_detaillee_multibarres`.
