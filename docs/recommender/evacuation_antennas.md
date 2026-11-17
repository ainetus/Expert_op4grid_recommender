# Plan — Antennes d'évacuation : un élément de structure du graphe de report

> **Statut** : plan de conception. **Phases 0, 1, 2 et 4 implémentées** (branche
> `feat/evacuation-antennas`) : helpers d'injection `utils/injections.py`, correctif
> d'`antenna_meta` en pypowsybl, détection `graph_analysis/structural_antennas.py` des deux
> côtés du chemin contraint, typage sur le graphe et payload du pipeline, leviers derrière
> `EVACUATION_ANTENNA_LEVERS` (coupé par défaut) : écrêtement et redispatch à la baisse en
> amont, redispatch à la hausse en aval. Phases 3, 5, 6 et 7 à faire.
> **Date** : 2026-11-16 · base `main` = `ainetus/main` `377301c3` (0.3.3.post3).
> **Objet** : détecter sur le graphe de report (overflow graph) les antennes latérales
> raccordées en un point du chemin en contrainte, en amont comme en aval, les typer
> « antenne d'évacuation »,
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
> détectée jusqu'ici est la **poche îlotée** (la contrainte l'alimente). Les antennes
> **latérales** du chemin contraint *aval* relèvent du même algorithme, et le pipeline les
> détecte aussi. Une antenne aval qui exporte est une **antenne d'évacuation aval** (levier :
> redispatch à la hausse, Phase 4). Une antenne aval qui importe est une antenne de
> consommation latérale (levier : délestage, Phase 7).

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
2. `K` est adjacente à au moins un nœud de `A` (ses **points de raccordement**). Si elle l'est
   à plusieurs, elle doit en plus être **insensible** : report sous le seuil des arêtes
   grises (§2.2, décision 7) ;
3. `|K|` reste sous un plafond de taille (garde-fou contre un « bassin » démesuré).

Elle est typée **antenne d'évacuation** si son export aux points de raccordement (`P_exp`,
somme des transits sortant de `K` sur les branches de raccordement) dépasse
`STRUCTURAL_ANTENNA_MIN_MW`. Les points de raccordement sont classés par puissance échangée
avec `K` ; le premier, noté `a`, donne son nom à l'antenne.

Une **antenne aval** se définit de même en échangeant `A` et `V`. Si elle exporte, c'est une
**antenne d'évacuation aval** (`evacuation_downstream`) : sa production entre dans la zone
aval par son point de raccordement `v ∈ V`.

On retrouve le critère « composants isolés si on supprime tous les nœuds du chemin en
contrainte amont, raccordés par un point ». Une composante de `H − A` adjacente au seul `a`
est exactement une composante de `H − {a}` qui ne touche ni `A ∖ {a}` ni `V ∪ B`. Autrement
dit, `a` est un point d'articulation qui la sépare du reste du réseau. Une composante
raccordée en plusieurs nœuds du même côté n'a plus de point d'articulation : elle relie ces
nœuds entre eux, d'où la condition d'insensibilité.

### 2.2 Pourquoi le report est (quasi) nul, et pourquoi ce n'est pas le critère

En DC, couper la ligne en contrainte revient à injecter et soutirer son transit à ses deux
extrémités, `O ∈ A` et `X ∈ B`, toutes deux **hors de `K`**. Aucune injection de `K` ne
change. Comme `K` n'est reliée au reste que par `a`, tous ses nœuds subissent la même
variation d'angle que `a`. On a donc **Δ = 0 exactement** sur toutes les branches de `K` et de
raccordement, même si `K` est maillée en interne. En AC il reste un résidu (pertes,
compensation répartie sur les groupes de `K`), d'où le « proche de zéro ».

Conséquence pour la conception : la détection est **topologique**. Pour une antenne raccordée
en un point, l'insensibilité (`|Δ| < ThresholdReportOfLine × |Δ_contrainte|`) est une
**propriété vérifiée et reportée** (`report_ratio_max`, `is_insensitive`), pas un filtre. Un
seuil sur le seul report confondrait l'antenne avec toute zone maillée peu sensible.

Une composante raccordée en plusieurs nœuds porte en revanche la différence de variation
d'angle entre ces nœuds : son report n'est plus nul par construction. L'insensibilité devient
alors le **critère** : la composante n'est une antenne que si ses branches restent grises.

### 2.3 Pourquoi c'est un levier

Tout MW retiré de `K` est retiré **au nœud `a`**, qui est en amont. L'effet sur la contrainte
est donc celui d'une injection en `a`, quelle que soit la profondeur du groupe dans l'antenne.
Une seule référence d'influence suffit par antenne : son point de raccordement ou, si elle en
a plusieurs, **le meilleur**, c'est-à-dire celui dont le flux d'influence est le plus fort
parmi ceux qui sont dans le graphe d'influence du bon côté (décision 7). Les formules sont
celles des familles d'injection existantes :

```
f_a    = min(1, max(neg_in_a, neg_out_a) / P_ref)    # _injection_influence_factor
P_exc  = (ρ_max − 1) · P_ref                          # InjectionOverloadContext
ΔP_req = P_exc · (1 + marge) / f_a                    # MW à retirer en a
```

Symétriquement, tout MW **ajouté** dans une antenne d'évacuation aval l'est au nœud `v` : il
alimente la zone aval et soulage la contrainte. Le levier est la **hausse** de production
(redispatch ↑), dans la limite de la marge `Pmax − consigne` des groupes, avec la même
référence d'influence (en `v`). L'écrêtement y aggraverait la contrainte : il n'est jamais
proposé.

### 2.4 Typage (cadre générique, périmètre progressif)

| Raccordement | Export au point de raccordement | Type | Levier |
|---|---|---|---|
| amont | `P_exp > seuil` (exporte) | **`evacuation`** (`EVAC_`) | baisse de production / écrêtement (Phase 4) ; ouverture (option, Phase 5) |
| amont | `P_exp < −seuil` (importe) | `consumption_upstream` (`CONS_UP_`) | **contre-levier** : délester ici aggrave. Informatif. |
| aval | `P_exp < −seuil` (importe) | `consumption` (`CONS_`) | délestage dimensionné (Phase 7) |
| aval | `P_exp > seuil` (exporte) | **`evacuation_downstream`** (`EVAC_DOWN_`) | hausse de production (redispatch ↑, Phase 4), jamais d'écrêtement |

Les quatre types sont détectés et typés ; les deux types d'évacuation portent
`in_evacuation_antenna` sur le graphe. `evacuation_downstream` s'appelait
`production_downstream` dans les premières versions de ce plan, quand le pipeline ne
scannait pas encore l'aval.

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
                                           couche du viewer)        ├─ options agrégées par antenne
                                                                    └─ ouverture (option)
                          └──────────────▶ AnalysisResult.structural_antennas (payload)
```

Modèle de données (implémenté dans `graph_analysis/structural_antennas.py`) :

```python
@dataclass
class StructuralAntenna:
    antenna_id: str              # stable : f"{préfixe}_{attachment_sub_name}_{k}" (EVAC_…)
    antenna_type: str            # "evacuation" | "consumption_upstream" | "consumption"
                                 # | "evacuation_downstream"
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
    attachment_nodes: List[Any]  # tous les points de raccordement, par puissance échangée
    attachment_sub_ids: List[int]  #   décroissante (le premier = attachment_node)
    attachment_sub_names: List[str]
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
- Typage : `detect_structural_antennas` scanne l'amont par défaut (`evacuation`,
  `consumption_upstream`) et l'aval sur demande avec `sides=("amont", "aval")`
  (`consumption`, `evacuation_downstream`). Le pipeline scanne les deux côtés (Phase 2).
- Tests dans `tests/test_structural_antennas.py`. Le graphe de report y est construit comme
  dans le pipeline (répartition DC après coupure, `_compute_delta_flow_frame`,
  `OverFlowGraph`, graphe structuré alphaDeesp) sur un réseau de 13 postes :
  - antenne d'évacuation détectée (point de raccordement, export ≈ production nette, ratio ≈ 0) ;
  - antenne maillée en interne mais raccordée en un point : détectée ;
  - poche raccordée à **deux** nœuds amont (D1, branches grises) : antenne à deux points de
    raccordement, classés par puissance échangée ; rejetée si son report dépasse le seuil ;
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
  découverte ne renumérote les nœuds. Il scanne les deux côtés du chemin contraint. Il ne tourne pas en mode poche, où
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
- Journal : une ligne de synthèse, puis une ligne par antenne d'évacuation, amont ou aval,
  par exemple « EVAC_SSV.OP7_1 attached to SSV.OP7 (amont): 2 node(s), export 1704 MW, max
  report 0.0% ».
- Tests dans `tests/test_structural_antennas.py` :
  - typage : nœuds, branches et raccordement ; rien d'autre ne change et les vues alphaDeesp
    (chemin contraint, boucles rouges, hubs) restent identiques ; les attributs survivent à la
    renumérotation ; le graphe se convertit toujours en DOT ;
  - pipeline, avec un faux backend : contexte peuplé et graphe typé, drapeau coupé, échec de
    la détection, mode poche, transmission au modèle et au résultat ;
  - garde de non-régression de bout en bout sur le petit réseau de test (pypowsybl,
    P.SAOL31RONCI) : `prioritized_actions` et `action_scores` **identiques** avec ou sans
    détection, alors que 6 antennes y sont détectées et typées (≈ 14 s, deux analyses).
- Mesuré sur ce cas (93 nœuds) : 6 antennes, dont 3 d'évacuation, en 1,5 à 2,4 ms. Toutes
  sont en amont : le chemin aval, en 63 kV, n'en porte aucune. La sixième, `CONS_UP_CHALOP6_1`,
  est apparue avec le multi-raccordement (décision 7) : une région 63 kV de 20 postes
  (175 MW de charge) raccordée à trois nœuds 225 kV amont, CHALOP6, COUCHP6 et VIELMP6, avec
  un report nul. Sur les 139 contingences du
  petit réseau, une seule autre crée une surcharge, et sa seule antenne aval est une
  antenne de consommation.
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

### Phase 4 — Levier « modulation de production dans l'antenne » *(M/L)* — **fait**

Un groupe d'une antenne d'évacuation agit sur la contrainte au point de raccordement. Le
levier donne donc des options d'**écrêtement** ou de **redispatching**, selon le type de
production évacuée et le côté de la contrainte :

| Antenne | EnR | Groupes pilotables (thermique, nucléaire, hydraulique…) |
|---|---|---|
| `evacuation` (amont) | écrêtement (rc) | redispatch à la baisse |
| `evacuation_downstream` (aval) | rien : l'écrêtement aggraverait la contrainte | redispatch à la hausse |

Ces options vont dans les familles existantes plutôt que dans une famille dédiée (décisions
3, 4 et 6 du §8). La correspondance entre sens et type d'antenne est explicite
(`EVACUATION_ANTENNA_TYPE`, `_injection_base.py`). Une antenne amont n'est jamais montée,
même quand son point de raccordement est aussi un nœud de hausse (hub d'une boucle rouge),
et une antenne aval n'est jamais baissée.

- **4a — options unitaires.** Dans rc (amont seulement) et redispatch (les deux sens), la
  résolution du nœud de référence devient :
  1. nœud propre du groupe ;
  2. **antenne d'évacuation du bon côté contenant le poste : référence = son meilleur point
     de raccordement** (`_evacuation_antenna_reference`, `_evacuation_antenna_attachment`) ;
  3. repli site HT.

  Ces candidats gagnent les params `via_evacuation_antenna` et `evacuation_antenna_id`, et
  `influence_ref_substation` vaut le point de raccordement. Sans l'étape 2, ces groupes sont
  invisibles : leurs arêtes sont grises, et le repli site HT échoue dès que le poste HT du
  site est lui aussi dans l'antenne (Bugey : `BUGEYP1` et `BUGEYP7`). Le pas reste celui de
  la famille (`REDISPATCH_DEFAULT_DELTA_MW`, voir §7).
- **4b — options agrégées, dimensionnées en MW.** Une option par antenne et par famille,
  répartie au prorata sur les groupes de ce type (`_evacuation_antenna_aggregates`) :
  - `ΔP_req = P_exc · (1 + marge) / f_a`, avec la marge de la famille et `f_a` pris au
    meilleur point de raccordement, qui devient la `substation` de l'option ;
  - la variation part de la **consigne** `S_g` des groupes (`gen_target_p`), pas de leur
    production observée. Celle-ci porte aussi leur part de la compensation répartie de la
    charge : sur IEEE 14, cette part (3,5 MW) inversait une hausse dimensionnée de 3 MW.
    Sans consigne connue, la production observée sert de repli ;
  - en amont (baisse) : marge `M_g = S_g` et `P_g' = S_g · (1 − ΔP / ΣS_g)` ;
  - en aval (hausse) : marge `M_g = Pmax_g − S_g` et `P_g' = S_g + M_g · ΔP / ΣM_g`.
    `Pmax` vient de `gen_pmax`, que l'observation pypowsybl expose désormais (`max_p`). Les
    groupes de `Pmax` inconnu sont écartés, et sans `gen_pmax` l'option agrégée n'est pas
    émise ;
  - `ΔP = min(ΣM_g, ΔP_req)` et `score = f_a · min(1, ΣM_g / ΔP_req)` ;
  - une seule action `set_gen_p` multi-groupes ;
  - identifiants `curtail_<antenna_id>` et `redispatch_<antenna_id>` : les préfixes des
    familles, que le classifieur (`gen_power_reduction`, `gen_redispatch`) et les paires GST
    (`is_injection_action`) reconnaissent déjà ;
  - params : `gen_names`, `target_p_MW` par groupe, `reduction_MW` (rc) ou `delta_MW` signé
    et `applied_MW` (redispatch), `headroom_MW` (hausse), `via_evacuation_antenna`,
    `evacuation_antenna_id` ;
  - une option agrégée identique à une option unitaire (un seul groupe EnR, coupé
    entièrement) n'est pas émise.
- Interrupteur : `EVACUATION_ANTENNA_LEVERS = False` jusqu'à la Phase 6. Coupé, les familles
  sont strictement inchangées (`_get_evacuation_antennas_by_sub` est vide). Seules les
  antennes d'évacuation servent de levier, jamais une antenne de consommation. Le module de
  découverte reçoit `structural_antennas` du contexte.
- Tests :
  - `tests/test_ActionDiscoverer.py`, section « Evacuation antennas as levers » :
    - amont : interrupteur coupé, référence `a`, dimensionnement et prorata, pas de doublon,
      antenne `consumption_upstream` exclue, priorisation, reconnaissance comme injection,
      jamais de hausse même quand le raccordement est un nœud de hausse ;
    - aval : interrupteur coupé, référence `v` et sens `up`, prorata sur la marge à partir de
      la consigne, plafond `Pmax`, pas d'option agrégée sans `gen_pmax`, repli sur la
      production observée sans consigne, jamais d'écrêtement ;
    - plusieurs points de raccordement : référence et score sur le meilleur, ou sur le
      suivant quand le meilleur n'est pas un nœud du bon sens ;
  - `tests/test_structural_antennas.py` :
    - `test_evacuation_levers_end_to_end` (marqué `slow`) : sur le petit réseau de test, les
      groupes nucléaires des deux antennes amont donnent 4 options unitaires et 2 agrégées,
      qui passent la réévaluation et les paires GST ;
    - `test_downstream_evacuation_lever_end_to_end` (IEEE 14 en AC, 0,2 s, en CI) : le
      déclenchement de T4-9 surcharge T4-7 (sens 4 → 7), et le bus 8 devient une antenne
      d'évacuation aval (B8-G : consigne 10 MW, 6,5 MW produits après compensation). Le
      pipeline propose l'option unitaire et l'option agrégée (+3,06 MW de consigne), et la
      réévaluation confirme qu'elles soulagent la contrainte : ρ passe de 1,086 à 0,991 et
      à 1,041 ;
  - `tests/test_pypowsybl_backend.py` : `gen_pmax` et `gen_target_p` suivent `max_p` et
    `target_p` du réseau.
- **Constat : le dimensionnement par `f_a` est optimiste.**
  - Petit réseau, en amont : la contrainte, « BEON L31CPVAN » (ρ = 1,036), est loin des
    antennes 400 kV. `f_a` vaut 0,28 au raccordement de Bugey, d'où `ΔP_req` ≈ 3,4 MW, alors
    que la sensibilité réelle est de l'ordre de 1 % : −10 MW sur un groupe de Bugey ne font
    descendre ρ qu'à 1,032. La coupe agrégée de 3,4 MW est sans effet mesurable.
  - IEEE 14, en aval : `f_a` vaut 1,0 au raccordement (VL7, extrémité de la contrainte), mais
    la hausse dimensionnée de 3,06 MW ne ramène ρ qu'à 1,041 : la sensibilité réelle est
    environ deux fois plus faible.

  C'est l'heuristique de toutes les familles d'injection ; à mesurer en Phase 6, et à
  remplacer par une sensibilité calculée (Phase 7).

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
- Calibrer le dimensionnement : comparer `f_a` à la sensibilité réelle de la contrainte (sur
  le petit réseau de test, 0,28 contre environ 1 % en amont ; sur IEEE 14, 1,0 contre
  environ 0,5 en aval). Si l'écart est systématique, dimensionner sur une sensibilité
  calculée : une simulation par antenne, ou la sensibilité DC de la Phase 7.
- Si les résultats le justifient, basculer `EVACUATION_ANTENNA_LEVERS = True`. Mettre à jour
  les release notes, `CHANGELOG.md`, `CLAUDE.md`, et transformer ce document en doc de
  conception.

### Phase 7 — Extensions *(optionnel)*

- **Antennes de consommation latérales aval** : le pipeline les détecte et les type
  `consumption` depuis la Phase 4. Reste le levier : étendre le délestage à ces antennes, avec
  pour référence le point de raccordement aval. (Les antennes d'évacuation aval, elles,
  servent déjà de levier : redispatch ↑, Phase 4.)
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
| `EVACUATION_ANTENNA_LEVERS` | `False`, puis `True` après la Phase 6 | options unitaires (4a) et agrégées (4b) : écrêtement et redispatch ↓ en amont, redispatch ↑ en aval |
| `EVACUATION_ANTENNA_ALLOW_OPENING` | `False` | active la Phase 5 |
| `EVACUATION_ANTENNA_OPENING_MAX_LOAD_MW` | `1.0` | charge maximale îlotée tolérée |

Les options agrégées reprennent la marge et le minimum en MW de leur famille
(`RENEWABLE_CURTAILMENT_*`, `REDISPATCH_*`), ainsi que ses planchers `MIN_*` et son jeton
`ALLOWED_ACTION_TYPES`. Les paramètres envisagés pour une famille dédiée
(`EVACUATION_ANTENNA_MARGIN`, `EVACUATION_ANTENNA_GEN_SCOPE`, `MIN_EVACUATION_ANTENNA`) n'ont
donc plus lieu d'être.

---

## 7. Risques et cas limites

- **Antenne « géante »** : si `a` isole tout un bassin de production, c'est physiquement une
  antenne d'évacuation, mais le levier devient un redispatch régional. D'où le plafond de
  taille et la journalisation des rejets. Le multi-raccordement rend ce cas plus fréquent : sur
  le petit réseau de test, une région 63 kV de 20 postes raccordée à trois nœuds 225 kV
  (`CONS_UP_CHALOP6_1`, ≈ 21 % des nœuds) passe sous le plafond de 25 %.
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
- **Options unitaires de redispatch** (famille existante) : elles partent de la production
  observée et non de la consigne. Leur variation réelle de consigne diffère donc du pas de la
  part de compensation du groupe : sur IEEE 14, les +10 MW demandés sur B8-G font +6,5 MW de
  consigne. Elles ne contrôlent pas non plus `Pmax`. Les options agrégées partent de la
  consigne et respectent `Pmax`. Corriger les options unitaires changerait les sorties
  actuelles de la famille, leviers coupés compris : à traiter à part.
- **Co-Study4Grid** :
  - le backend transmet `structural_antennas` dans l'événement NDJSON `result`
    (`expert_backend/services/analysis_mixin.py`, garde de non-régression
    `test_result_event_forwards_structural_antennas_from_discovery`). C'est vérifié de bout
    en bout sur un cas réel (3 antennes dans l'événement). Cet événement sélectionne les clés
    de `AnalysisResult` une à une : tout nouveau champ doit y être ajouté ;
  - reste à afficher les antennes, et à prendre en charge les options agrégées avant
    d'activer les leviers. Leurs params portent `gen_names`, un `target_p_MW` par groupe et,
    pour une hausse, `headroom_MW`, là où les options unitaires ont `gen_name` et un
    `target_p_MW` scalaire.

---

## 8. Décisions à valider

1. **Payload** : `structural_antennas` générique et typé (recommandé, prépare la Phase 7), ou
   `evacuation_antennas` dédié.
2. **Leviers par défaut** : désactivés jusqu'au banc (recommandé, dans la ligne des releases
   « behaviour-preserving »), ou actifs d'emblée.
3. **Levier agrégé** : tranché le 2026-11-16. Les options vont dans rc et redispatch, selon
   le type de production évacuée, plutôt que dans une famille dédiée.
4. **Périmètre du levier agrégé** : tranché avec la décision 3. Les EnR sont proposées en
   écrêtement, les groupes pilotables en redispatch ; il n'y a plus de paramètre de
   périmètre.
5. **Ouverture de l'antenne** : opt-in, sans charge îlotée. À confirmer côté exploitation.
6. **Antennes d'évacuation aval** : tranché le 2026-11-16. Elles sont détectées comme les
   antennes amont, et leur levier est la hausse des groupes pilotables (redispatch ↑),
   jamais l'écrêtement.
7. **Plusieurs points de raccordement** : tranché le 2026-11-17. Une composante raccordée en
   plusieurs nœuds du même côté du chemin contraint est une antenne si elle est insensible
   (branches grises). Ses options prennent la référence et le score du meilleur de ces
   nœuds.
