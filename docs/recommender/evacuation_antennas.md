# Plan — Antennes d'évacuation : un élément de structure du graphe de report

> **Statut** : plan de conception. **Phases 0 à 2 implémentées** (branche
> `feat/evacuation-antennas`) : helpers d'injection `utils/injections.py`, correctif
> d'`antenna_meta` en pypowsybl, détection `graph_analysis/structural_antennas.py`, typage
> sur le graphe et payload du pipeline. Phases 3 à 7 à faire.
> **Date** : 2026-11-16 · base `main` = `ainetus/main` `377301c3` (0.3.3.post3).
> **Objet** : détecter sur le graphe de report (overflow graph) les antennes latérales
> raccordées en un point du chemin en contrainte amont, les typer « antenne d'évacuation »,
> les intégrer comme élément de structure (graphe, rendu, payload), puis les exploiter comme
> **levier** de recommandation.
> **Liés** : [`antenna_overflow_graph.md`](antenna_overflow_graph.md) (poche îlotée),
> [`renewable_curtailment.md`](renewable_curtailment.md), [`load_shedding.md`](load_shedding.md),
> [`../reviews/2026-07_tht_benchmark_findings.md`](../reviews/2026-07_tht_benchmark_findings.md).

---

## 1. Le besoin

### 1.1 Ce qui existe

| Mécanisme | Code | Couvre | Ne couvre pas |
|---|---|---|---|
| **Mode antenne / poche îlotée** (0.2.5) | `processor.extract_antenna_context`, `antenna_graph.build_antenna_overflow_graph` | La contrainte **est** l'artère d'une poche radiale : couper l'ouvrage en contrainte îlote la poche (consommatrice → aval, productrice → amont). Seules les injections sont proposées. | Les antennes qui ne portent pas la contrainte. |
| **Repli « site haute tension »** | `_base._get_site_higher_voltage_map`, `_renewable_curtailment.py:95`, `_redispatch.py:125` | Un groupe posé sur un poste de tension inférieure absent du graphe emprunte l'influence du nœud 225/400 kV **du même site**. | Heuristique de site / nommage : un seul saut, aucune notion de structure, rien de typé ni de visible. |

### 1.2 Ce qui manque : l'antenne latérale du chemin en contrainte

```
              antenne d'évacuation (report ≈ 0, branches grises)
              [E2 : éolien 120 MW] ── [E1] ──┐
                                             │  point de raccordement « a »
   … ─blue─▶ [A0] ─blue─▶ [a] ─blue─▶ [O] ══black══▶ [X] ─blue─▶ [V1] ─blue─▶ …
              chemin contraint AMONT          contrainte     chemin contraint AVAL
                          ╲__________ boucle rouge (coral) __________╱
```

Une antenne d'évacuation est un sous-réseau relié au reste du réseau **uniquement par un nœud
`a` du chemin contraint amont**. Toute sa production nette sort par `a` puis emprunte le
chemin contraint vers la contrainte. Elle est aujourd'hui invisible à trois niveaux :

1. **Structure** — ses branches ont un report quasi nul → couleur `gray` → absentes des vues
   bleue / rouge de `Structured_Overload_Distribution_Graph` (chemin contraint, boucles, hubs).
2. **Rendu** — `DRAW_ONLY_SIGNIFICANT_EDGES = True` (`config.py:110`) retire les arêtes grises
   du dessin.
3. **Leviers** — les familles d'injection lisent l'influence dans `_build_node_flow_cache`
   (`_base.py:575`), qui ne contient que les nœuds touchés par des arêtes bleues / rouges : les
   groupes de l'antenne n'ont pas de nœud de référence (`ref_idx is None`) et sont ignorés,
   sauf si le repli « site HT » les rattrape par le nommage.

> **« De même qu'on détecte les antennes de consommation »** : l'antenne de consommation
> détectée aujourd'hui est la **poche îlotée** (la contrainte l'alimente). L'antenne de
> consommation **latérale**, raccordée en un point du chemin contraint *aval*, n'est pas
> détectée non plus. C'est le cas symétrique, traité par le même algorithme (Phase 7).

---

## 2. Définition et justification physique

### 2.1 Définition opérationnelle

Notations :

- `H` : graphe des ouvrages **en service à l'état N-1** (nœuds du graphe de report, une arête
  par branche dont `line_status` est vrai) ;
- `A` = `constrained_path.n_amont()` : nœuds du chemin contraint amont (contient l'origine de
  la contrainte) ;
- `V` = `n_aval()` ; `B` : extrémités des arêtes noires (contraintes).

Une **antenne amont** est une composante connexe `K` de `H − A` telle que :

1. `K ∩ (V ∪ B) = ∅` : ce n'est pas « le reste du réseau » ;
2. `K` est adjacente à **exactement un** nœud `a ∈ A` (le **point de raccordement**) ;
3. `|K|` reste sous un plafond de taille (garde-fou contre un « bassin » démesuré).

Elle est typée **antenne d'évacuation** si son export au point `a` (`P_exp`, somme des
transits sortant de `K` vers `a` sur les branches de raccordement) dépasse
`STRUCTURAL_ANTENNA_MIN_MW`.

On retrouve le critère « composants isolés si on supprime tous les nœuds du chemin en
contrainte amont, raccordés par un point ». Une composante de `H − A` adjacente au seul `a`
est exactement une composante de `H − {a}` qui ne touche ni `A ∖ {a}` ni `V ∪ B`. Autrement
dit, `a` est un point d'articulation qui la sépare du reste du réseau.

### 2.2 Pourquoi le report est (quasi) nul, et pourquoi ce n'est pas le critère

En DC, couper la ligne en contrainte revient à injecter et soutirer son transit à ses deux
extrémités, `O ∈ A` et `X ∈ B`, toutes deux **hors de `K`**. Aucune injection de `K` ne
change. Comme `K` n'est reliée au reste que par `a`, tous ses nœuds subissent la même
variation d'angle que `a`. On a donc **Δ = 0 exactement** sur toutes les branches de `K` et de
raccordement, même si `K` est maillée en interne. En AC il reste un résidu (pertes,
compensation répartie sur les groupes de `K`), d'où le « proche de zéro ».

Conséquence pour la conception : la détection est **topologique**. L'insensibilité
(`|Δ| < ThresholdReportOfLine × |Δ_contrainte|`) est une **propriété vérifiée et reportée**
(`report_ratio_max`, `is_insensitive`), pas un filtre. Un seuil sur le seul report
confondrait l'antenne avec toute zone maillée peu sensible.

### 2.3 Pourquoi c'est un levier

Tout MW retiré de `K` est retiré **au nœud `a`**, qui est en amont. L'effet sur la contrainte
est donc celui d'une injection en `a`, quelle que soit la profondeur du groupe dans l'antenne.
Une seule référence d'influence suffit par antenne, avec les formules des familles
d'injection existantes :

```
f_a    = min(1, max(neg_in_a, neg_out_a) / P_ref)    # _injection_influence_factor
P_exc  = (ρ_max − 1) · P_ref                          # InjectionOverloadContext
ΔP_req = P_exc · (1 + marge) / f_a                    # MW à retirer en a
```

### 2.4 Typage (cadre générique, périmètre progressif)

| Raccordement | Export au point de raccordement | Type | Levier |
|---|---|---|---|
| amont | `P_exp > seuil` (exporte) | **`evacuation`** (cible de ce plan) | baisse de production / écrêtement ; ouverture (option) |
| amont | `P_exp < −seuil` (importe) | `consumption_upstream` | **contre-levier** : délester ici aggrave. Informatif. |
| aval | `P_exp < −seuil` | `consumption` (Phase 7) | délestage dimensionné |
| aval | `P_exp > seuil` | `production_downstream` (Phase 7) | hausse de production (redispatch ↑) |

---

## 3. Constats du code qui contraignent la conception

1. **Le graphe de report contient déjà l'antenne.** `OverFlowGraph.build_edges_from_df` crée
   une arête par ligne du `df`, grises comprises ; seules les vues `g_only_blue_components` /
   `g_only_red_components` les retirent. Il ne s'agit pas de réinjecter des arêtes mais de les
   **typer**.
2. **Ne pas recolorer le graphe d'analyse.** `g_only_red_components` retire `black`, `gray`,
   `dimgray` et `blue` (alphaDeesp `structured_overload_graph.py:37-43`). Toute autre couleur y
   resterait : les nœuds de l'antenne entreraient alors dans
   `get_dispatch_edges_nodes(only_loop_paths=False)` (reconnexions, fusions de nœuds…). Le
   typage passe donc par des **attributs**, et la couleur dédiée n'est appliquée que sur une
   **copie de rendu**.
3. **Une ligne ouverte reste une arête grise sans style.** La ligne de la contingence N-1 est
   exclue de `non_connected_reconnectable_lines` (`pipeline.py`). Sauf si elle figure dans
   `lines_non_reconnectable`, elle reste une arête grise ordinaire. Une détection sur le seul
   graphe relierait alors l'antenne au reste par une ligne **ouverte**. La connexité doit donc
   être filtrée par `obs_simu_defaut.line_status`.
4. **`n_amont()` inclut l'origine de la contrainte** (`from_edges_get_nodes`, avec un repli
   sur `[constrained_edge[0]]`). Avec plusieurs surcharges, alphaDeesp ne retient **qu'une**
   arête noire comme référence (`find_constrained_path`).
5. **Les injections par nœud ne sont pas disponibles en pypowsybl, et un bug latent en
   découle.** `PypowsyblObservation` n'expose ni `gen_to_subid` ni `load_to_subid` (seulement
   `line_or/ex_to_subid`, `observation.py:474-481`). De plus, `p` y est négatif en production
   (vérifié sur IEEE14 : `target_p = 232.4`, `p = −232.4`). Résultat :
   `antenna_graph._sub_injections` (`antenna_graph.py:58`) renvoyait des zéros en backend
   pypowsybl (vérifié sur IEEE14 : 0 MW de production et de consommation pour un `gen_p` dont
   la somme vaut −272,4 MW). L'`antenna_meta` du mode poche affichait donc
   `total_prod_mw = total_load_mw = net_mw = 0` et `direction = "consumer"` **pour toute
   poche**, y compris productrice. **Corrigé en Phase 0.**
6. **Points d'insertion des leviers.** rc et redispatch résolvent le nœud de référence en deux
   temps : (1) nœud propre du groupe, puis (2) repli site HT (`_renewable_curtailment.py:88-110`,
   `_redispatch.py:116-140`). L'antenne s'insère entre les deux (étape 1 bis). Le registre
   `FAMILY_SPECS` (`_results.py`) rend l'ajout d'une famille déclaratif (spec, ordres de phase,
   `MIN_*`).
7. **Le viewer HTML déclare ses couches en dur.** Les couches structurelles
   (`on_constrained_path`, `in_red_loop`, …) sont déclarées dans alphaDeesp
   (`interactive_html.py:71`, `_LAYER_SECTIONS` l.106). Une couche « Antennes d'évacuation »
   demande donc une PR amont (ExpertOp4Grid) et un relèvement du plancher `expertop4grid`.
8. **Les familles d'injection fonctionnent de fait en pypowsybl.** Elles ignorent les
   `gen_p > 0` et émettent des actions `set_gen_p`. Le levier antenne suit la même hypothèse.

---

## 4. Architecture cible

```
étape 2 (graphe)  build_overflow_graph ─▶ detect_structural_antennas(...)        ← nouveau
                                          │
                       context.structural_antennas : List[StructuralAntenna]
                          │                     │                        │
               attributs du graphe        visualisation             discovery (leviers)
               (in_evacuation_antenna…)   (copie de rendu,          ├─ rc / redispatch : réf. = a
                                           couche du viewer)        ├─ famille evacuation_antenna
                                                                    └─ ouverture (option)
                          └──────────────▶ AnalysisResult.structural_antennas (payload)
```

Modèle de données (implémenté dans `graph_analysis/structural_antennas.py`) :

```python
@dataclass
class StructuralAntenna:
    antenna_id: str              # stable : f"EVAC_{attachment_sub_name}_{k}"
    antenna_type: str            # "evacuation" | "consumption_upstream" | (aval) ...
    side: str                    # "amont" | "aval"
    attachment_node: Any         # libellé du nœud de raccordement dans le graphe
    attachment_sub_id: Optional[int]
    attachment_sub_name: str
    nodes: List[Any]             # libellés du graphe (noms ou ids)
    sub_ids: List[int]
    sub_names: List[str]
    connection_lines: List[str]  # branches a—K (plusieurs si raccordement en parallèle)
    internal_lines: List[str]
    export_mw: float             # transit sortant de K vers a (> 0 = évacuation)
    prod_mw: float               # production comptée positive
    load_mw: float
    renewable_gens: List[str]
    dispatchable_gens: List[str]
    loads: List[str]
    report_ratio_max: float      # max |Δ| sur ses branches / |Δ| de la contrainte
    is_insensitive: bool         # report_ratio_max < ThresholdReportOfLine
```

Détection (une passe en O(V+E)) ; esquisse du côté amont, l'implémentation traite aussi
l'aval via `sides=` et lit le report sur l'attribut `capacity` des arêtes :

```python
def detect_structural_antennas(g_overflow, g_distribution_graph, obs, sides=("amont",),
                               min_mw=1.0, max_fraction=0.25, report_threshold=None,
                               renewable_sources=None):
    cp = g_distribution_graph.get_constrained_path()
    amont, aval = set(cp.n_amont()), set(cp.n_aval())
    cut_nodes = {n for u, v, c in g_overflow.g.edges(data="color") if c == "black"
                 for n in (u, v)}
    in_service = {n for n, st in zip(obs.name_line, obs.line_status) if st}

    H = nx.Graph()  # seule la connexité compte : multi-arêtes fusionnées
    H.add_nodes_from(g_overflow.g.nodes)
    H.add_edges_from((u, v) for u, v, name in g_overflow.g.edges(data="name")
                     if name in in_service)

    antennas = []
    for comp in nx.connected_components(H.subgraph(H.nodes - amont)):
        if comp & (aval | cut_nodes) or len(comp) > max_fraction * H.number_of_nodes():
            continue
        attach = {a for a in amont if not comp.isdisjoint(H[a])}
        if len(attach) != 1:
            continue  # rattachée en plusieurs points : pas une antenne
        a = attach.pop()
        ...  # bilans (export en a, prod / conso via utils/injections), ratio de report, typage
    return sorted(antennas, key=...)  # ordre déterministe
```

---

## 5. Plan de mise en œuvre

Chaque phase est livrable seule. Ordre : 0 → 1 → 2, puis 3 et 4 en parallèle, puis 5 → 6 ;
la Phase 7 vient après la validation. Tant que la Phase 6 n'a pas validé les leviers, les
valeurs par défaut laissent les recommandations **identiques**.

### Phase 0 — Prérequis : injections par nœud à signe robuste *(S)* — **fait**

- `utils/injections.py` : `production_mw(obs)` / `consumption_mw(obs)` (comptées positives,
  convention détectée par backend via `uses_receptor_sign_convention`), `gen_to_subid(obs)` /
  `load_to_subid(obs)` (vectorisé en grid2op, inversion de `get_obj_connect_to` en
  pypowsybl) et `injections_by_sub(obs)`.
- `antenna_graph._sub_injections` délègue à `injections_by_sub` : `antenna_meta` est juste en
  pypowsybl (constat 5).
- Tests : `tests/test_injections.py` (deux conventions, pompage, NaN, éléments non localisés,
  vrai réseau IEEE14) et, dans `test_antenna_graph.py`, poches consommatrice et productrice
  en convention pypowsybl ; les tests grid2op existants sont inchangés.

### Phase 1 — Détection *(M)* — **fait**

- Nouveau module `graph_analysis/structural_antennas.py` : `StructuralAntenna` et
  `detect_structural_antennas` (§4). Fonction pure, sans effet de bord sur les graphes.
- Bilans :
  - export en `a` depuis `p_or` / `p_ex` des branches de raccordement, côté `K` ;
  - production, consommation et listes de groupes (EnR selon `RENEWABLE_ENERGY_SOURCES`) et
    de charges via la Phase 0 ;
  - `report_ratio_max` lu sur l'attribut `capacity` des arêtes (report en MW), rapporté au
    `max |report|` des arêtes en contrainte : pas besoin de `df_of_g`.
- Typage : côté amont par défaut (`evacuation`, `consumption_upstream`). L'aval
  (`consumption`, `production_downstream`) est déjà détectable avec `sides=("amont", "aval")`
  ; son câblage reste en Phase 7.
- Tests dans `tests/test_structural_antennas.py`. Le graphe de report y est construit comme
  dans le pipeline (répartition DC après coupure, `_compute_delta_flow_frame`,
  `OverFlowGraph`, graphe structuré alphaDeesp) sur un réseau de 13 postes :
  - antenne d'évacuation détectée (point de raccordement, export ≈ production nette, ratio ≈ 0) ;
  - antenne maillée en interne mais raccordée en un point : détectée ;
  - poche raccordée à **deux** nœuds amont : rejetée ;
  - composante du reste du réseau : rejetée ;
  - raccordement par une ligne **ouverte** (la contingence) : ignoré ;
  - antenne amont importatrice : typée `consumption_upstream` ;
  - plafond de taille ; libellés en noms ou en entiers, y compris un graphe de distribution
    resté en noms quand `g` est passé en ids ; convention pypowsybl ;
  - nœuds jumeaux (pas de plantage) ; plusieurs surcharges ;
  - bout en bout sur un vrai réseau pypowsybl en AC (IEEE14, graphe construit par
    `build_overflow_graph_pypowsybl`) : avec de la production sur B8-G et L7-9 en contrainte,
    le bus 8, raccordé au bus 7 par L7-8, est détecté comme antenne d'évacuation. Son export
    égale la production réelle du groupe et son report arrondi est nul.
- Domaines de libellés : le pré-traitement de la découverte (`pre_process_graph_alphadeesp`)
  renumérote les nœuds de `g` en ids, alors que le graphe de distribution, qui porte le chemin
  contraint, garde les noms. Le chemin contraint est donc traduit dans le domaine du graphe via
  `obs.name_sub`. Constaté sur un cas réel : 0 antenne avant ce correctif, 4 après, dont une
  antenne d'évacuation éolienne de 231,5 MW à report nul.
- Reste à mesurer : le coût sur un grand réseau (objectif < 50 ms, Phase 6).

### Phase 2 — Intégration comme élément de structure *(S)* — **fait**

- `config.py` : `ENABLE_STRUCTURAL_ANTENNAS = True`, `STRUCTURAL_ANTENNA_MIN_MW = 1.0`,
  `STRUCTURAL_ANTENNA_MAX_FRACTION = 0.25`. Le drapeau couvre détection, typage et payload,
  **sans effet** sur les recommandations.
- `pipeline.py` : `_detect_structural_antennas` est appelé dans `run_analysis_step2_graph`
  juste après `backend.build_overflow_graph(...)`, donc avant le rendu et avant que la
  découverte ne renumérote les nœuds. Il ne tourne pas en mode poche, où
  `structural_antennas` vaut `[]`. Un échec de la détection est journalisé et n'interrompt
  pas l'analyse.
- Données exposées :
  - `AnalysisContext.structural_antennas` et `RecommenderInputs.structural_antennas` : les
    objets `StructuralAntenna` (`None` tant que l'étape 2 n'a pas tourné) ;
  - `AnalysisResult.structural_antennas` : la liste des `to_dict()`, sérialisable en JSON.
- `tag_structural_antennas` type le graphe d'analyse par **attributs**. Nœuds et branches
  d'une antenne reçoivent `antenna_id`, `antenna_type` et `in_evacuation_antenna` ; le nœud de
  raccordement reçoit `is_antenna_attachment`. Un attribut absent vaut « non ». Les attributs
  survivent au `relabel_nodes` de `pre_process_graph_alphadeesp`.
- Journal : une ligne de synthèse, puis une ligne par antenne d'évacuation, par exemple
  « EVAC_SSV.OP7_1 attached to SSV.OP7: 2 node(s), export 1704 MW, max report 0.0% ».
- Tests dans `tests/test_structural_antennas.py` :
  - typage : nœuds, branches et raccordement ; rien d'autre ne change et les vues alphaDeesp
    (chemin contraint, boucles rouges, hubs) restent identiques ; les attributs survivent à la
    renumérotation ; le graphe se convertit toujours en DOT ;
  - pipeline, avec un faux backend : contexte peuplé et graphe typé, drapeau coupé, échec de
    la détection, mode poche, transmission au modèle et au résultat ;
  - garde de non-régression de bout en bout sur le petit réseau de test (pypowsybl,
    P.SAOL31RONCI) : `prioritized_actions` et `action_scores` **identiques** avec ou sans
    détection, alors que 5 antennes y sont détectées et typées (≈ 14 s, deux analyses).
- Mesuré sur ce cas (93 nœuds) : 5 antennes, dont 3 d'évacuation, en 1,5 à 2,4 ms.
  `EVAC_SSV.OP7_1` (BUGEYP1 et BUGEYP7) exporte 1 704 MW ; `EVAC_VIELMP7_1` (7 postes)
  exporte 795 MW, avec un report maximal de 4 % en AC, sous le seuil de 5 %.

### Phase 3 — Rendu et viewer *(M, dont une PR amont)*

- `visualization.make_overflow_graph_visualization(..., structural_antennas=...)` travaille
  sur une **copie** du graphe (comme `focus_overflow_graph_on_pocket`) :
  - couleur dédiée aux arêtes d'antenne, pour qu'elles survivent à `without_gray_edges` ;
  - étiquette = export (MW) plutôt que Δ ≈ 0 ;
  - le graphe d'analyse n'est pas modifié.
- PR ExpertOp4Grid : couche sémantique `in_evacuation_antenna` (section « Structural paths »,
  glyphe dédié) dans `interactive_html._SEMANTIC_LAYERS` / `_LAYER_SECTIONS`, puis
  relèvement du plancher `expertop4grid` ici.
- Tests : attributs posés ; couleurs du graphe d'analyse inchangées après le rendu ; arêtes
  d'antenne présentes dans le rendu filtré.

### Phase 4 — Levier « modulation de production dans l'antenne » *(M/L)*

- **4a — familles existantes.** Dans rc et redispatch (sens `down`), la résolution du nœud de
  référence devient :
  1. nœud propre du groupe ;
  2. **(nouveau) antenne d'évacuation contenant le poste : référence = `a`** ;
  3. repli site HT.

  Nouveaux params : `via_evacuation_antenna`, `evacuation_antenna_id`,
  `influence_ref_substation = a`.
- **4b — nouvelle famille `evacuation_antenna`.** C'est un levier agrégé, dimensionné en MW.
  Il répond au constat du banc THT selon lequel « l'amplitude unitaire ne mord pas ». Un
  candidat par antenne :
  - `P_mod` = production modulable de l'antenne (EnR seules ou tous groupes, selon un
    paramètre) ;
  - `ΔP = min(P_mod, ΔP_req)` ;
  - répartition au prorata, `P_g' = P_g · (1 − ΔP / P_mod)`, dans une seule action
    `set_gen_p` multi-groupes ;
  - `score = f_a · min(1, P_mod / ΔP_req)`.

  Déclaration :
  - `FamilySpec("evacuation_antenna", …, scores_key="evacuation_antenna")`, ajoutée en fin
    d'`ACTION_SCORES_ORDER` ;
  - `MIN_EVACUATION_ANTENNA` ;
  - jeton `ALLOWED_ACTION_TYPES` `"evac"` ;
  - dans les phases min et fill, en tête des familles d'injection (avant
    `renewable_curtailment`) : une baisse de production est préférée au délestage ;
  - mise à jour de `_empty_action_scores`.
- Interrupteur : `EVACUATION_ANTENNA_LEVERS = False` jusqu'à la Phase 6.
- À vérifier : `ActionClassifier` / `ActionType`, la réévaluation et les paires GST
  (`is_injection_action`) sur une action `set_gen_p` multi-groupes.
- Tests :
  - candidats rc / redispatch pour un groupe d'antenne, avec
    `influence_ref_substation == a` ;
  - aucun candidat quand `EVACUATION_ANTENNA_LEVERS=False` (sorties identiques) ;
  - dimensionnement et répartition au prorata ;
  - une antenne `consumption_upstream` n'est jamais proposée au délestage.

### Phase 5 — Levier « ouverture de l'antenne » *(S/M, opt-in)*

- Candidat `set_line_status = −1` sur **toutes** les `connection_lines`, uniquement si
  `load_mw ≤ EVACUATION_ANTENNA_OPENING_MAX_LOAD_MW`, pour ne pas îloter de clientèle.
- `score = f_a · min(1, P_exp / ΔP_req)`.
- Production perdue = `prod_mw`, reportée dans les params et par la quantification d'îlotage
  existante lors de la réévaluation.
- `EVACUATION_ANTENNA_ALLOW_OPENING = False` par défaut.

### Phase 6 — Validation et bascule des défauts *(M)*

- Bancs : RTE7000-THT (201 contingences notées), Matpower (901) et un cas réseau complet
  (Dijon). Mesures :
  - nombre de contingences avec au moins une antenne d'évacuation ;
  - MW exportés ;
  - taux de résolution avec et sans leviers ;
  - surcoût de temps ;
  - revue d'un échantillon avec un opérateur.
- Attendu : un gain surtout sur réseau **complet**. Sur un réseau THT réduit, les antennes HT
  sont souvent déjà agrégées en injections équivalentes au nœud frontière, donc déjà
  atteignables.
- Si les résultats le justifient, basculer `EVACUATION_ANTENNA_LEVERS = True`. Mettre à jour
  les release notes, `CHANGELOG.md`, `CLAUDE.md`, et transformer ce document en doc de
  conception.

### Phase 7 — Extensions *(optionnel)*

- **Cas symétrique aval** : même algorithme sur `n_aval()`. Il produit les types `consumption`
  (le délestage s'étend aux antennes de consommation latérales, avec pour référence le point
  de raccordement aval) et `production_downstream` (redispatch ↑).
- **Sensibilité exacte** : remplacer le `f_a` heuristique par la sensibilité DC de la
  contrainte à une injection en `a` (`pypowsybl.sensitivity`, une matrice pour toutes les
  antennes).
- **Migration amont** : `Structured_Overload_Distribution_Graph.find_antennas(...)` dans
  alphaDeesp, à côté de `find_constrained_path` / `find_loops` / `find_hubs`.
- **Basculement du raccordement** : ouverture de couplage au poste `a` qui envoie l'antenne sur
  le nœud tourné vers la boucle rouge. Cela demande de pondérer l'injection de l'antenne dans
  le score AlphaDeesp.

---

## 6. Paramètres

| Paramètre | Défaut | Rôle |
|---|---|---|
| `ENABLE_STRUCTURAL_ANTENNAS` | `True` | détection, typage et payload (sans effet sur les recommandations) |
| `STRUCTURAL_ANTENNA_MIN_MW` | `1.0` | export minimal pour typer une antenne |
| `STRUCTURAL_ANTENNA_MAX_FRACTION` | `0.25` | plafond de taille (part des nœuds du graphe) |
| `EVACUATION_ANTENNA_LEVERS` | `False`, puis `True` après la Phase 6 | active 4a et 4b |
| `EVACUATION_ANTENNA_MARGIN` | `0.05` | marge sur `ΔP_req` |
| `EVACUATION_ANTENNA_GEN_SCOPE` | `"renewable"` | périmètre du levier agrégé (`"renewable"` ou `"all"`) |
| `MIN_EVACUATION_ANTENNA` | `0` | plancher de candidats retenus |
| `EVACUATION_ANTENNA_ALLOW_OPENING` | `False` | active la Phase 5 |
| `EVACUATION_ANTENNA_OPENING_MAX_LOAD_MW` | `1.0` | charge maximale îlotée tolérée |

---

## 7. Risques et cas limites

- **Antenne « géante »** : si `a` isole tout un bassin de production, c'est physiquement une
  antenne d'évacuation, mais le levier devient un redispatch régional. D'où le plafond de
  taille et la journalisation des rejets.
- **Contraintes multiples ou en série** : un nœud aval d'une contrainte peut être en amont
  d'une autre. Le typage suit la contrainte de référence d'alphaDeesp ; la vérification par
  simulation (`_check_rho_with_baseline` sur toutes les surcharges) écarte les leviers qui en
  aggravent une.
- **Branches hors graphe** : les transformateurs à trois enroulements, les HVDC et les
  dangling lines ne sont pas des arêtes. Une antenne raccordée ainsi apparaît isolée ; le
  repli site HT reste donc en place, après l'étape antenne.
- **Nœuds jumeaux** (poste à deux nœuds électriques) : rattacher groupes et charges au bon
  nœud via leur bus. Test dédié.
- **AC** : compensation répartie et pertes laissent un petit report dans l'antenne ;
  `is_insensitive` le signale sans exclure l'antenne.
- **Co-Study4Grid** : afficher `structural_antennas` et la famille `evacuation_antenna`. Suivi
  côté UI, dans un dépôt séparé.

---

## 8. Décisions à valider

1. **Payload** : `structural_antennas` générique et typé (recommandé, prépare la Phase 7), ou
   `evacuation_antennas` dédié.
2. **Leviers par défaut** : désactivés jusqu'au banc (recommandé, dans la ligne des releases
   « behaviour-preserving »), ou actifs d'emblée.
3. **Levier agrégé** : famille dédiée `evacuation_antenna` (recommandé), ou fondu dans
   rc / redispatch.
4. **Périmètre du levier agrégé** : EnR seules par défaut (écrêtement), ou tous les groupes
   (baisse via redispatch).
5. **Ouverture de l'antenne** : opt-in, sans charge îlotée. À confirmer côté exploitation.
