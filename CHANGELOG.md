# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### IHM de manœuvre : source dataset RTE 7000 + déploiement HuggingFace Space

L'IHM de manœuvre (`scripts/manoeuvre_ihm.py`) peut désormais **sourcer ses
situations réseau directement dans le dataset RTE 7000** par date/heure, et se
**déploie en HuggingFace Docker Space** (sur le modèle de Co-Study4Grid).

- **Couche source par date** (`manoeuvre/dataset/source.py`, nouveau) :
  `lister_instantanes(repo, date)` (liste les instantanés HH:MM d'une journée via
  l'API tree HuggingFace), `choisir_instantane(insts, heure)` (le plus proche de
  l'heure visée, **midi par défaut**), `telecharger_instantane` / `charger_situation`
  (téléchargement **à la demande** + cache local + vérif md5, puis chargement
  pypowsybl). Stdlib pur, jeton `HF_TOKEN` optionnel. Ré-exporté par
  `manoeuvre.dataset`.
- **Mode dataset de l'IHM** : `--grid` devient **optionnel** ; sans lui (ou avec
  `--dataset`), l'IHM démarre sur le dataset. Nouveaux endpoints
  `GET /api/dataset/config`, `GET /api/dataset/timestamps`,
  `POST /api/dataset/load` ; garde-fous « aucune situation chargée »
  (`/api/postes` renvoie `needs_date`). Options `--host` / `--port` (+ env `PORT`,
  `DGITT_REPO`, `DGITT_CACHE_DIR`, `DGITT_DEFAULT_DATE`, `HF_TOKEN`).
- **Frontend** : bandeau **📅 Dataset RTE7000** (date + heure midi-par-défaut +
  dates échantillons) ; le poste courant est préservé lors d'un changement de
  date. Le flux « relever les VL → choisir un poste → éditer/séquencer » est
  inchangé.
- **Déploiement** : `Dockerfile` (mono-conteneur Flask léger sur `:7860`, mode
  dataset), `.dockerignore`, `deploy/huggingface/{README.md, SETUP.md}`,
  `.github/workflows/deploy-huggingface.yml` (redéploiement auto, inerte sans
  `HF_TOKEN`/`HF_SPACE`).
- **Tests** : `tests/manoeuvre/test_dataset_source.py` (logique de résolution /
  téléchargement, frontière réseau mockée) et `tests/manoeuvre/test_ihm_dataset.py`
  (endpoints + garde-fous). Le mode local (`--grid`) est strictement préservé.

### IHM de manœuvre : refonte UX du panneau + volet nodal

Refonte ergonomique de l'IHM de manœuvre (`scripts/manoeuvre_ihm_assets/index.html`
+ `scripts/manoeuvre_ihm.py`). Doc : `docs/manoeuvre/ihm.md` ; figure annotée :
`docs/manoeuvre/manoeuvre_ihm_overview.svg`.

- **Situation réseau en deux onglets** *📁 Local* (chemin `.xiidm` + **sélecteur de
  fichier natif**, `GET /api/pick_grid_file`) et *📅 RTE7000* (date/heure), avec un
  **unique bouton Charger** ; RTE7000 mis en avant par défaut sur le Space.
- **Dates d'accès rapide 2021-2023** (7 journées de la « Table de campagne »),
  l'**année sélectionnant le dataset** (`dataset_source.repo_pour_date`,
  `…-2021/-2022/-2023`) ; bulles d'information chiffrées.
- **Champ poste unifié** : une recherche sur tous les postes NODE_BREAKER +
  **liste browsable** sous le titre *« Pré-sélection de postes typiques »*
  (exploration curée par typologie). **Plus d'auto-chargement de poste**.
- **« 🗺 Scénario Topologique »** en 3 étapes (Poste → Topologie cible → Séquence).
  **✓ Valider** (active le calcul) et **💾 Sauvegarder** séparés ; sur le Space
  (`hosted`), les scénarios/séquences sauvegardés sont **aussi téléchargés en
  local** (`/api/save` + `/api/save_sequence` renvoient `content`).
- **En-têtes de schéma** : **↺ État d'origine** (départ) et **⇧ Nouvelle Topologie
  Départ** (cible → promeut la cible courante en départ, `POST /api/promote_cible`).
  **⟳ Recharger** (modale) remplace la section « Scénarios sauvegardés ».
- **Volet nodal** : sections **Départ/Cible repliables**, **ouvrages isolés en tête
  de cadre**, **↺ Réinitialiser** (reset complet détaillé + nodal), et **＋ Nœud**
  crée un nœud vide **persistant** (cible de dépose ; nœuds vides ignorés au calcul).
- **Tests** : garde-fou de **structure du front** (`test_ihm_frontend_asset.py` :
  marqueurs requis présents / retirés absents, bloc script équilibré) ;
  `test_ihm_cache_and_api.py` (sélecteur de fichier, `promote_cible`, `content` de
  `/api/save` + `/api/save_sequence`, nœuds vides ignorés) ; `test_ihm_dataset.py`
  (`repo_pour_date`, dérivation du repo par année sur timestamps **et** load,
  drapeau `hosted`).

---

## [0.2.5] - 2026-06-19

### Antenna (islanded-pocket) recommendations

A new analysis mode for the case where a contingency leaves a **radial pocket**
of substations fed by a single overloaded line — so disconnecting that line
**breaks the grid apart** (no `lines_overloaded_ids_kept`). Previously the
analysis gave up ("Overload breaks the grid apart. No topological solution
without load shedding."). It now describes the islanded pocket, builds a proper
overflow graph for it, and recommends the **injection actions** that can relieve
the overload.

- **Detection** (`graph_analysis/processor.py`): `extract_antenna_context`
  identifies the pocket islanded by removing the max overload — its constraint
  line, root (main-grid) and entry (pocket) substations, and the full pocket
  substation set. Gated by `config.ENABLE_ANTENNA_RECOMMENDATIONS` (default
  `True`); falls back to the legacy "no solution" message when off or when the
  pocket is not a clean single-feed antenna.
- **Overflow graph** (`graph_analysis/antenna_graph.py`): the pocket graph is
  built through the **standard ExpertOp4Grid machinery** (`OverFlowGraph` +
  `Structured_Overload_Distribution_Graph`), fed the post-disconnection state
  implied by the islanding — the initial post-contingency flows with every line
  incident to the pocket zeroed — and the same per-line `delta_flows` frame as
  `alphaDeesp.Simulation.create_df`. alphaDeesp decides edge colour, orientation
  and the amont/aval split from the **real signed flows**, so a **consumer
  pocket** (downstream / aval) and a **producer pocket** feeding the grid up
  through the overload (upstream / amont) both render with physical flow
  directions — no inversion, no looping.
- **Injection-only discovery** (`action_evaluation/discovery/_orchestrator.py`):
  in antenna mode the topological families are filtered out and only load
  shedding / renewable curtailment / redispatch are discovered, targeting the
  **pocket substations directly** (via `antenna_meta`) so a producer pocket —
  correctly classified amont — still gets candidates. Both redispatch directions
  are offered; the per-action simulation check keeps only the ones that help.
- **Result payload**: `run_analysis` results carry `antenna_meta` (pocket
  substations, total prod/load/net MW, `direction`) so UIs can phrase the
  recommendation; `antenna_mode` flags the case.
- **Visualization** (`main.py`): the analysis graph spans the full grid (the
  gray healthy lines anchor the root for `find_hubs`); the viewer renders a
  pocket-focused copy via `focus_overflow_graph_on_pocket`.
- See `docs/recommender/antenna_overflow_graph.md`. Tests in `tests/test_antenna_graph.py`.

---

## [0.2.4.post1] - 2026-06-17

### Readable voltage-level names as overflow-graph node labels

For PyPSA-derived networks the substation/voltage-level IDs are opaque
(`VL_way_...`) while a human-readable name (e.g. `"Saucats 400kV"`) is carried
in the network's voltage-level `name` column. The overflow-graph visualization
now renders that readable name as the node label.

- **New** `get_zone_voltage_level_names(env_path)` in
  `graph_analysis/visualization.py` — returns `{vl_id: readable_name}` from the
  network, keeping only entries whose `name` is non-empty and differs from the
  ID (cached per file path + mtime, like `get_zone_voltage_levels`).
- `make_overflow_graph_visualization` sets the Graphviz `label` node attribute
  from that mapping. **Node identity is left untouched** — the SVG `<title>` /
  `data-name` keep the stable VL ID, so Co-Study4Grid pin overlays, geo-layout
  matching and SLD lookups keep working; only the rendered text changes.
- **New** config flag `USE_VOLTAGE_LEVEL_NAMES_IN_GRAPH` (default `True`).
  Networks without separate readable names (the usual RTE case, `name == id`)
  yield an empty mapping and are rendered unchanged.

### Bug Fixes

- **Overflow graph blank on zipped (`.zip`) network paths**: the visualization
  re-loads the network from `config.ENV_PATH`, which for the game-mode
  `network.xiidm.zip` is a raw zip that `pypowsybl.network.load` cannot read —
  the resolver fell through to a bogus `<path>/grid.xiidm` and raised, aborting
  the whole render (so suggestions appeared but no graph). New
  `_resolve_network_file` / `_extract_network_zip` decompress a zipped network
  to a cached sibling `.xiidm` (temp-dir fallback if read-only), and also
  resolve a directory / companion `.zip`. Both `get_zone_voltage_levels` and
  `get_zone_voltage_level_names` route through it.
- **Readable labels never break the render**: label text is sanitized
  (`_sanitize_graph_label`) so embedded double quotes / backslashes / newlines
  can't produce malformed DOT (older `pydot` mis-escapes them and crashes
  Graphviz). As a safety net, if `plot()` still fails with labels applied they
  are stripped and the plot retried once, so a presentational nicety can never
  remove the core graph.

### Tests

- `test_visualization_filtering.py`: name-loader filtering, label application
  with preserved identity, the sanitizer, the retry-without-labels fallback,
  and zip / directory / companion-zip path resolution + zip extraction reuse.

---

## [0.2.4] - 2026-06-15

### Generalized Superposition Theorem (GST) — injection-aware action pairs

`utils/superposition.py` now combines a topology action with an **injection**
change (load shedding / renewable curtailment / redispatch), and two injection
changes with each other — previously these pairs were skipped. Based on
`compute_flows_GST_from_unit_act_obs` in
[Topology_Superposition_Theorem](https://github.com/marota/Topology_Superposition_Theorem).

- **New** `is_injection_action(action_id, action_desc, classifier)` — detects
  load shedding / curtailment / redispatch by id prefix or classifier type.
- **New** `compute_combined_pair_gst(...)` — the injection's flow response enters
  in pure superposition while the topology action keeps an injection-shifted EST
  beta (RHS-only shift of the 1×1 EST system). `compute_combined_pair_superposition`
  gains `act1_is_injection` / `act2_is_injection` and routes to the GST when set.
- **Key identity**: an injection action is reported with `beta = 1.0`, so the
  standard `(1 − Σβ)·start + β₁·act1 + β₂·act2` reconstruction reproduces the
  exact GST flows — the rho estimators (and downstream consumers) are unchanged.
- `compute_all_pairs_superposition` no longer filters out injection actions and
  passes the injection flags per pair.
- **Tests**: `tests/test_superposition_gst.py` validates every injection-bearing
  pair shape against ground-truth grid2op DC simulations (~1e-6 MW), plus a
  `TestGstIsAcAnchored` class pinning that the beta RHS and the reconstruction
  read the (AC) observation values verbatim.
- **Docs**: `docs/recommender/superposition_module.md` §10 now documents the AC-anchoring of
  the GST (AC values used throughout; the superposition law is DC-exact only, so
  the AC residual is structural) and the accuracy profile (topology+injection ≡
  topology-only EST; the global max-rho line can flip between near-equal low-flow
  corridor lines while the on-target overload is predicted correctly;
  injection+injection is lower-confidence), plus a **"Known larger-error cases"**
  catalog tabulating the two larger-error patterns with their measured small-grid
  examples, root cause and how to read them.

---

## [0.2.3.post2] - 2026-06-11

### Performance

- **Discovery hot-path optimizations** for large grids (`action_evaluation/discovery/`):
  - Fixed an `O(n_subs^2)` `name_sub` rebuild in the voltage-level metadata construction (`692b55b`).
  - Hoisted observation arrays out of the per-candidate loops, removing repeated re-indexing in the real hotspot (`dd489ed`).
  - Capped per-candidate simulation for redispatch and curtailment discovery to bound work on large candidate sets (`9a251bd`).
- **Step 2 caching** (`main.py` / step 2): zone voltage levels are now cached and unused candidate load flows are skipped (`5730a80`).

### Notes

- The experimental two-speed outer-loop cap on warm-start load flow attempts was introduced and then reverted (`e0c5578`, `78401c7`) after evaluation; behavior is unchanged from `0.2.3.post1` on that front.

---

## [0.2.3.post1] - 2026-06-09

### Fixed

- **Per-type `MIN_*` floors are no longer starved when their sum exceeds `n_action_max`** (`action_evaluation/discovery/_orchestrator.py`). The per-type `MIN_*` counts are GUARANTEED floors, but the minimum-enforcement phase previously capped each `add_prioritized_actions` call at `n_action_max`. When the floors summed above `n_action_max` (e.g. reco 2 + close 3 + open 2 + disco 3 + ls 2 = 12 > 10), the types added last (load shedding, redispatch) were silently starved once the earlier types filled the budget — so `load_shedding_*` never surfaced despite `MIN_LOAD_SHEDDING=2`. The minimum phase now uses `min_phase_cap = max(n_action_max, sum of all floors)` so every floor is honored; only the fill phase keeps `n_action_max` as the target. The result may exceed `n_action_max` when floors demand it (correct semantics). Reproduced on the `small_grid` demo (Co-Study4Grid PR #162 CI).
- **Overflow-graph visualization is now non-fatal in `run_analysis_step2_graph`** (`main.py`). Its rendering goes through alphaDeesp + external tooling (graphviz); when that fails (e.g. `AssertionError` in `display_geo`) it must not abort step 2 — action discovery only needs the graph data, not the picture. The visualization call is now wrapped in `try`/`except` with a warning.

### Tests

- `tests/test_min_action_counts.py`: new `test_floors_honored_when_min_sum_exceeds_total` and a `_run_two_pass` helper that mirrors `min_phase_cap`. Existing `MIN_*` and discoverer tests still pass (132).

---

## [0.2.3] - 2026-06-08

### Added

- **Redispatching action type** (`action_evaluation/discovery/_redispatch.py`, `RedispatchMixin`). `find_relevant_redispatch` discovers candidates that **raise** dispatchable production downstream (aval) of the constrained path or on the parallel red dispatch loops, and **lower** it upstream (amont). Dispatchable generators are the complement of `RENEWABLE_ENERGY_SOURCES` (new helper `_get_subs_with_dispatchable_gens` on `DiscovererBase`). Unlike curtailment (which forces `target_p = 0`), redispatching encodes the **real target setpoint** `current ± delta` in `set_gen_p` so the variation is actually simulated. The default delta is `REDISPATCH_DEFAULT_DELTA_MW` (10 MW) and is meant to be edited downstream (Co-Study4Grid).
  - New config: `MIN_REDISPATCH` (2), `REDISPATCH_DEFAULT_DELTA_MW` (10.0), `REDISPATCH_MARGIN` (0.05), `REDISPATCH_MIN_MW` (1.0), mirrored in `config_basic.py` and `tests/config_test.py`, and exposed in `models/expert.py` `params_spec` (`min_redispatch`, `redispatch_default_delta_mw`).
  - New `action_scores["redispatch"]` bucket (`scores` + `params` with `direction`, `target_p_MW`, `delta_MW`, `influence_factor`, `coverage_ratio`, …).
  - `ActionClassifier.identify_action_type` now returns `gen_redispatch` for `redispatch_`-prefixed ids (or `action_mode == "redispatch"`), disambiguating them from renewable `gen_power_reduction`.
  - **Antenna sites** (`_base.py` `_get_voltage_level_metadata` / `_get_site_higher_voltage_map`): generators sit on a radial voltage level usually absent from the influence graph. When the **same physical site** has a higher-voltage busbar (400/225 kV) that IS in the graph, the generator is now considered of interest and that higher node is used as the influence/score reference (`params["influence_ref_substation"]`, `params["via_higher_voltage"]`). Site grouping + nominal voltage come from the pypowsybl network (`get_voltage_levels()` `substation_id` / `nominal_v`), with an RTE-naming fallback for the grid2op backend. The same same-site higher-voltage reference is also applied to `find_relevant_renewable_curtailment` (less critical there, as small renewables are often connected directly on meshed busbars). Optimized for large grids: the network query is narrowed to `substation_id`/`nominal_v`, the site map is built lazily (and cached) only when a generator actually sits on an off-graph node, so meshed-only cases add no cost.
- **`ALLOWED_ACTION_TYPES` recommender restriction** (`config.py`, `action_evaluation/discovery/_orchestrator.py`). When the list is non-empty, the orchestrator only discovers/prioritizes the listed action families (tokens: `reco` / `close` / `open` / `disco` / `pst` / `ls` / `rc` / `redispatch`); all others are skipped entirely. An empty list (the default) keeps every family. Unlike the `MIN_*` knobs — which are *floors* on each family — this is an exclusive filter, letting an operator focus the recommender on, e.g., redispatch only. Mirrored in `tests/config_test.py`.

### Tests

- `tests/test_ActionDiscoverer.py`: new redispatch section (9 tests) — up/down candidate discovery, renewable skip, signed-delta targets, real setpoint encoding, params structure, score range, `action_scores` presence.
- `tests/test_discovery_package_structure.py`: updated structure guard for the new `RedispatchMixin` + `_get_subs_with_dispatchable_gens` helper (method count 42 → 44).
- `tests/test_ActionDiscoverer.py`: `ALLOWED_ACTION_TYPES` coverage — restricting discovery to `redispatch` skips disco / load-shedding / node-splitting; an empty list keeps all families.

---

## [0.2.2.post2] - 2026-05-19

### Fixed

- **AC load flow now retries with `DC_VALUES` init on any non-converged status, not just on synchronous exceptions** (`pypowsybl_backend/network_manager.py`, `_run_ac_with_init_fallback`). The previous fallback (commit 22e8a39e, v0.2.0) only triggered when `pypowsybl.loadflow.run_ac` raised a `PowsyblException`. But OpenLoadFlow can also return a `ComponentResult` whose `status` is `FAILED` ("Unrealistic state" reached by the voltage-control consistency check) or `MAX_ITERATION_REACHED` — *without* raising — so the bad result was propagated up the stack and surfaced as a `non_convergence` flag on action cards in downstream UIs. This was reproducibly hit on PyPSA-EUR / France 400 kV grid for `node_merging_PYMONP3` on contingency `P.SAOL31RONCI`: the two pre-merge coupler buses sat 8.4° apart in angle, so NR seeded from those stale `PREVIOUS_VALUES` diverged in ~13 iterations with `FAILED`. Seeded from `DC_VALUES`, the same LF converges in 11 outer iterations (fast) / 48 outer iterations (slow). The fallback now inspects the returned status and re-runs the LF with `DC_VALUES` whenever the first attempt did not converge and was seeded with `PREVIOUS_VALUES`. The pre-existing exception-based path is preserved unchanged.

### Changed

- **`maxOuterLoopIterations` default raised from 20 → 100** in `NetworkManager._create_default_lf_parameters` provider parameters. OpenLoadFlow's stock 20-iteration cap on the outer loop was tripping `MAX_ITERATION_REACHED` on the `IncrementalTransformerVoltageControl` loop after a node-merging action even once seeded correctly with `DC_VALUES`. Empirically the post-merge slow-mode LF needs ~40-50 outer iterations on the French grid; 100 leaves a comfortable margin and has no measurable cost on the normal warm-start path (which converges in single-digit outer iterations).

### Tests

- `tests/test_lf_fallback_non_converged.py` (8 tests):
  - `TestDefaultLfParametersOuterLoopCap` guards the bumped `maxOuterLoopIterations ≥ 40` default;
  - `TestInitFallbackOnNonConvergedStatus` covers the new retry on `FAILED` and `MAX_ITERATION_REACHED`, the no-retry-on-CONVERGED warm-start path, the no-retry-when-already-DC-init guard, the preserved synchronous-exception retry from v0.2.0, the exception propagation on DC_VALUES init, and the no-mutation invariant on shared `lf_parameters`.

---

## [0.2.2.post1] - 2026-05-17

### Added

- **`run_analysis_step1(prebuilt_obs_simu_defaut=...)`** in `main.py`: optional kwarg letting a host application (typically a UI that already produced the post-contingency observation while rendering an N-1 diagram) skip the redundant `simulate_contingency_pypowsybl` call. When provided, the function trusts the caller's observation and proceeds straight to overload detection. Default `None` preserves the legacy behaviour for every existing call site. Saves ~1-3 s on the French grid when the host already ran the contingency LF for its own purposes (see Co-Study4Grid `_cached_obs_n1` integration).
- **`run_analysis_step2_discovery(...)` now returns per-stage timings** (`prediction_time`, `assessment_time`) alongside the result payload. `prediction_time` is the model's intrinsic `recommend()` call (which for Expert-style models still includes the internal candidate simulation done to score topology actions); `assessment_time` is `reassess_prioritized_actions` + `propagate_non_convergence_to_scores` + `compute_combined_pairs` — the re-simulation step that scales linearly with the number of prioritized actions. Lets callers expose an honest per-stage breakdown without re-timing inside their own wrappers.

### Changed

- **`get_maintenance_timestep_pypowsybl(do_reco_maintenance=False)` fast-exits** (`utils/helpers_pypowsybl.py`): returns an empty action and an empty list immediately when the flag is off, skipping the disconnected-line scan + the formatted `print` of the result. On large grids with many pre-disconnected lines this saves ~150-300 ms per analysis run (the previous version unconditionally iterated and printed even though the returned list was unused). Behaviour when `do_reco_maintenance=True` is unchanged.

### Tests

- `tests/test_helpers_pypowsybl_maintenance.py` covers the fast-exit semantics (empty action, no scan, no print) and the full path (scan + filter + action build when the flag is True).
- `tests/test_run_analysis_step1_prebuilt_obs.py` is a static contract guard: `prebuilt_obs_simu_defaut` exists with `default=None` so host applications can introspect the signature with `inspect.signature` before forwarding the kwarg.

---

## [0.2.2] - 2026-05-12

### Added

- **Pluggable `RecommenderModel` contract** (`expert_op4grid_recommender/models/base.py`, PR #90): new abstract base class with the `recommend(inputs, params) -> RecommenderOutput` contract and a class-level `params_spec()` introspection hook. Any third-party model (random baselines, ML policies, …) can now plug into the analysis pipeline without modifying the library. Class attributes `name`, `label` and `requires_overflow_graph` advertise registry id, UI label and capability needs so callers can skip expensive graph builds when the model doesn't consume them.
- **DTO layer for model inputs / outputs** (`expert_op4grid_recommender/models/base.py`, PR #90):
  - `RecommenderInputs` — paired N / N-K data (`obs`, `obs_defaut`, `network`, `network_defaut`), pre-computed step-1 outputs (`lines_overloaded_names`, `lines_overloaded_ids`, `lines_overloaded_ids_kept`, `lines_overloaded_rho`, `pre_existing_rho`), the full `dict_action`, the expert-rule-filtered `filtered_candidate_actions` list, the overflow-graph artefacts (`overflow_graph`, `distribution_graph`, `overflow_sim`, `hubs`, `node_name_mapping`) and a private `_context` escape hatch for advanced models (used internally by `ExpertRecommender`).
  - `RecommenderOutput` — `{action_id: action_object}` plus a free-form `action_scores` dict.
  - `SimulatedAction` — post-reassessment payload (`max_rho`, `rho_after`, simulated observation, non-convergence reason, …).
  - `ParamSpec` — per-parameter introspection (`name`, `label`, `kind` ∈ `{"int", "float", "bool"}`, `default`, optional `min` / `max`) so frontends can render dynamic forms.
- **`ExpertRecommender`** (`expert_op4grid_recommender/models/expert.py`, PR #90): the legacy rule-based system, now exposed as the canonical `RecommenderModel` implementation. Declares `requires_overflow_graph=True`, surfaces all legacy scoring knobs (`n_prioritized_actions`, `min_*`, `monitoring_factor`, `pre_existing_overload_threshold`, `ignore_reconnections`, …) via `params_spec()`, and delegates to `_run_expert_discovery` through `inputs._context`. Remains the default model — every existing call site sees identical behaviour.
- **Reassessment + combined-pair phase extracted into a reusable module** (`expert_op4grid_recommender/utils/reassessment.py`, PR #90):
  - `build_recommender_inputs(context)` constructs the DTO from any pipeline context, including network handle extraction for both grid2op and pypowsybl backends (`_extract_pypowsybl_network`, `_extract_pypowsybl_network_from_obs`), pre-extraction of `lines_overloaded_rho` as a plain Python list, and propagation of the expert-rule-filtered candidate set.
  - `reassess_prioritized_actions(...)` simulates every action emitted by `recommender.recommend(...)`, computes `max_rho` / `rho_after` / impacted-line set / simulated observation, and tags non-convergence reasons.
  - `propagate_non_convergence_to_scores(...)` enriches the per-type score dicts with convergence-failure markers so the UI can surface them.
  - `compute_combined_pairs(...)` runs the superposition theorem on the top-K reassessed actions to estimate the best pair without a full simulation.
  Works for *any* model that returns `{action_id: action_object}` — third-party models inherit the same downstream pipeline for free.
- **`run_analysis_step2_discovery(context, recommender=None, params=None)`** (`expert_op4grid_recommender/main.py`, PR #90): new model-aware step-2 entry point. When `recommender` is omitted it defaults to `ExpertRecommender()`. The expert action filter (`_run_expert_action_filter`) runs idempotently whenever the overflow graph is in context, populating `context["filtered_candidate_actions"]` — both for `ExpertRecommender` (which still applies the rule chain internally) and for sampling models that want to restrict their pool.
- **Library-side contract documentation** (`docs/architecture/recommender_models.md`, PR #90): step-by-step guide to writing a third-party recommender — minimal ABC implementation, registry pattern, DTO field reference, capability flags, integration with the reassessment phase.
- **Comprehensive test coverage** (PR #90, all mock-based, no live pypowsybl / grid2op required):
  - `tests/test_models_base.py` — ABC contract enforcement, default `params_spec()` returning `[]`, DTO defaults & private context handling.
  - `tests/test_models_expert.py` — `ExpertRecommender` metadata, `requires_overflow_graph=True`, full `params_spec()` enumeration, fallback to `_context` on `recommend()`.
  - `tests/test_reassessment.py` — `build_recommender_inputs` coverage for both backends, pre-existing rho extraction, `lines_overloaded_rho` plain-list conversion, `reassess_prioritized_actions` happy path / non-convergence / combined-pair fan-out.
  - `tests/test_filtered_candidate_actions_propagation.py` — regression coverage for the wiring of `context["filtered_candidate_actions"]` into the DTO (would have caught the historical `is None despite filter running` bug observed in CoStudy4Grid).

### Changed

- **`main.py` cleaned up around the new dispatch entry point** (PR #90): removed redundant docstrings on wrapper functions, tidied import comments, kept all legacy public entry points (`run_analysis`, `run_analysis_step1`, `run_analysis_step2_graph`) with identical signatures. Callers wanting the new pluggable behaviour opt-in by switching to `run_analysis_step2_discovery`.

### Compatibility

- **No behaviour change for existing callers.** Every existing public function keeps the same signature. The pluggable layer is purely additive: when no `recommender` argument is supplied, `run_analysis_step2_discovery` instantiates `ExpertRecommender()` and the pipeline behaves exactly as in 0.2.1.post1. The DTO's `_context` escape hatch is the bridge: `ExpertRecommender` still reaches into the original pipeline state, so output parity is guaranteed.
- New `tests/test_filtered_candidate_actions_propagation.py` codifies that `filtered_candidate_actions` is forwarded from the context to the DTO — preventing future regressions in the propagation chain.

---

## [0.2.1.post1] - 2026-05-07

### Added

- **`extra_lines_to_cut_ids` plumbing** (`graph_analysis/builder.py`, `pypowsybl_backend/overflow_analysis.py`, `graph_analysis/visualization.py`, `main.py`, PR #89): `build_overflow_graph` (and its grid2op / pypowsybl wrappers) now accept an optional `extra_lines_to_cut_ids` parameter. Operator-supplied indices are appended to `Grid2opSimulation.ltc` / `AlphaDeespAdapter.ltc` so the cut still happens, and forwarded as `extra_lines_to_cut=…` to `OverFlowGraph` so the new `is_extra_cut` tag flows through (and the visualization keeps these edges out of the Overloads / Monitored layers). Implements ExpertAgent's `additionalLinesToCut` semantic. `run_analysis_step2_graph` reads `context["extra_lines_to_cut_ids"]` (default `[]`); `make_overflow_graph_visualization` accepts the parameter for plumbing completeness.

### Compatibility

- Defaults to an empty list / `None` everywhere — existing callers see no behaviour change. Step1 populates `context["extra_lines_to_cut_ids"] = []`; step2 callers (e.g. CoStudy4Grid) can override before invoking `run_analysis_step2_graph`. Extras already present in `overloaded_line_ids` are silently de-duplicated.

---

## [0.2.1] - 2026-05-05

### Added

- **Overflow-graph tagger wiring** (`graph_analysis/visualization.py`, `main.py`, PR #88): `make_overflow_graph_visualization` now accepts optional `lines_constrained_path` / `nodes_constrained_path` / `red_loop_lines` / `red_loop_nodes` / `lines_overloaded` parameters and forwards them to the new `OverflowGraph.tag_constrained_path` and `OverflowGraph.tag_red_loops` taggers (alongside the existing `highlight_significant_line_loading`). The pipeline computes these lists right after the distribution-graph pass and passes them into the three call sites of `make_overflow_graph_visualization`. Result: the serialised overflow graph now carries explicit `is_hub` / `in_red_loop` / `on_constrained_path` / `is_monitored` / `is_overload` boolean flags driving the upstream alphaDeesp interactive viewer's semantic layer toggles.

### Compatibility

- All new parameters default to `None`. Existing callers see no behaviour change — the taggers are no-ops when the recommender does not pass any list. Requires `ExpertOp4Grid >= 0.3.2` to consume the flags in the interactive HTML viewer; older versions still serialise the same numerical / colour content unchanged.

---

## [0.2.0] - 2026-04-14

### Added

- **`PowerReductionAction`** (`pypowsybl_backend/action_space.py`, PR #74): New action class that modifies active power setpoints (`target_p`) for loads and generators without electrically disconnecting them. Enables partial load shedding and renewable curtailment with maintained grid connectivity and voltage support. Integrated via `set_load_p` and `set_gen_p` action dictionary keys with `update_loads()` / `update_generators()` batch calls.
- **Renewable curtailment discovery fully integrated** (`action_evaluation/discovery/`, PR #73): `find_relevant_renewable_curtailment` is now part of the main analysis pipeline. Candidates are identified on upstream nodes of the constrained path among wind/solar generators. Controlled by `ENABLE_RENEWABLE_CURTAILMENT`, `RENEWABLE_CURTAILMENT_MARGIN`, `RENEWABLE_CURTAILMENT_MIN_MW`, and `RENEWABLE_ENERGY_SOURCES` configuration flags.
- **`ENABLE_RENEWABLE_CURTAILMENT` / `ENABLE_LOAD_SHEDDING` config flags** (PR #73): Explicit boolean switches to include or exclude heuristic action types from the analysis without touching `MIN_*` counts.
- **Pydantic-based configuration** (PR #84): `config.py` and `config_basic.py` define a `Settings(BaseSettings)` class with type validation, range/bound checking, and `EXPERT_OP4GRID_*` environment variable overrides. Module-level attribute publishing is preserved, so existing `config.DATE = ...` mutation and `from ... import DATE` call sites continue to work unchanged.
- **`quality` optional dependency group** (PR #84): `pip install -e .[quality]` installs `radon>=6.0`, `vulture>=2.10`, `interrogate>=1.5`, and `ruff>=0.5` for static analysis.
- **Comprehensive discovery caching** (`action_evaluation/discovery/_base.py`, PR #76): Six cache helpers that eliminate repeated expensive traversals on large networks — `_get_edge_data_cache()`, `_get_blue_edge_names_set()`, `_get_subs_with_loads()`, `_get_subs_with_renewable_gens()`, `_build_line_capacity_map()`, and `_build_node_flow_cache()`.
- **Baseline simulation hoisted outside action loops** (PR #76): For load shedding and renewable curtailment, the N-1 baseline rho is computed once per scenario and reused across all candidate actions.
- **`SimulationEnvironment` caching** (PR #72): Avoids redundant environment initialisation on repeated analysis calls.
- **`skip_enrichment` parameter** on the detection phase (PR #72): Bypasses redundant action enrichment during the initial overload detection step.
- **New tests**: `test_graph_analysis.py` (PR #78) for graph analysis helpers and `test_environment_pypowsybl.py` (PR #78) for pypowsybl environment setup logic.
- **Design and quality documents** (PR #71, PR #77): `docs/recommender/renewable_curtailment.md` (algorithm, scoring, data requirements) and `docs/archive/code-quality-analysis.md` (static analysis snapshot: god-module inventory, testing gaps, TODO/FIXME catalogue).
- **Type hints and docstrings** back-filled on `load_training_data.py`, `load_evaluation_data.py`, `repas.py`, `make_env_utils.py`, `make_assistant_env.py`, and `make_training_env.py` (PR #84).

### Changed

- **Discovery module refactored to mixin architecture** (PR #78): The monolithic `discovery.py` (3001 lines, 42+ methods) is split into `action_evaluation/discovery/` with nine focused mixin modules:
  - `_base.py` — `DiscovererBase` with shared state, caches, and simulation plumbing
  - `_line_reconnection.py` — line reconnection discovery
  - `_line_disconnection.py` — line disconnection scoring
  - `_node_merging.py` — bus merge discovery and delta-theta scoring
  - `_node_splitting.py` — bus split discovery (AlphaDeesp)
  - `_load_shedding.py` — load shedding candidate identification
  - `_renewable_curtailment.py` — renewable curtailment candidate identification
  - `_pst.py` — phase-shifter transformer tap discovery
  - `_orchestrator.py` — top-level pipeline orchestration and scoring assembly
- **Load shedding and curtailment emit `PowerReductionAction`** (PR #74): Both discovery methods now produce partial setpoint reductions (`set_load_p` / `set_gen_p`) instead of `set_bus` disconnections. Action metadata includes `action_mode`, `target_p_MW`, and `reduction_MW`.
- **`ActionClassifier` enhanced** (PR #73, PR #74): Now supports `open_load`, `open_gen`, `load_power_reduction`, and `gen_power_reduction` action types; handles `None` description input without raising `AttributeError`.
- **Superposition theorem filtering** (PR #73): `curtail_*` and `load_shedding_*` action IDs are excluded from the beta-coefficient linear solver, which assumes standard topological coupling not applicable to power-setpoint actions.
- **Vectorised topology cache** (PR #72): `NetworkTopologyCache` construction uses vectorised operations instead of per-element Python loops — faster initialisation and update.
- **Environment variable for training data path** (PR #77): `load_training_data.py` reads `EXPERT_OP4GRID_TRAINING_OBS_DIR` instead of a hardcoded developer path.
- **`sys.path` manipulation removed from `main.py`** (PR #77, PR #84): The package now relies on proper editable installation (`pip install -e .`) rather than runtime path hacking.

### Fixed

- **`ActionClassifier` robustness** (PR #73): `None` description no longer raises `AttributeError` during type identification.
- **`NoneType` and `AttributeError` regressions** (PR #73): Fixed during integration of renewable curtailment in `discovery.py` and `classifier.py`.
- **Topology reconstruction for mixed actions** (PR #78): `_build_action_entry_from_topology` robustified for combined topology/switch action formats.
- **Duplicate config definitions** (PR #77): Removed second (silent last-write-wins) definitions of `RENEWABLE_CURTAILMENT_MARGIN`, `RENEWABLE_CURTAILMENT_MIN_MW`, `RENEWABLE_ENERGY_SOURCES`, and `PYPOWSYBL_FAST_MODE`.

### Removed

- **`observation_timers.py`** — 1052-line stale fork of `observation.py` with zero importers; deleted (PR #77).
- **`conversion_actions_repas_original.py`** — 274-line superseded stub with zero importers; deleted (PR #77).

### Dependencies

- Added `pydantic>=2.0` and `pydantic-settings>=2.0` as core runtime dependencies (PR #84).

---

## [0.1.9] - 2026-03-25

### Added

- **Load Shedding Actions**: Automated discovery and scoring of load shedding candidates on downstream nodes of constrained paths to alleviate overloads when topological actions are insufficient.
- **Improved Action Prioritization**: Introduced `MIN_LOAD_SHEDDING` and `MIN_PST` configuration parameters to guarantee a minimum number of prioritized actions for these types.
- **Integrated Pipeline Support**: Load shedding is now fully integrated into the two-step analysis pipeline, with detailed scoring hypotheses included in `action_scores`.

### Changed

- **Path Management Refactor**: Switched to `pathlib.Path` for all base directories and file paths in `config.py`, improving reliability for relative execution and cross-platform compatibility.
- **Enhanced Instrumentation**: Added comprehensive timing blocks for Load Shedding discovery and prioritization steps.

---

## [0.1.8_post1] - 2026-03-20

### Added

- **PST Support in Superposition Theorem**: Added `act1_is_pst` and `act2_is_pst` flags to `compute_combined_pair_superposition` to correctly quantify impacts for phase-shifter actions.
- **Direct XIIDM Loading**: Enhanced `main.py` entry point to allow loading a grid case directly from an `.xiidm` file path, rather than requiring it to reside within a specific directory structure.

### Fixed

- **Robust PST Asset Identification**: Improved ID-based identification logic to handle REPAS-style PST IDs (stripping leading dots and discovery-added suffixes like `_inc1`/`_dec2`).
- **PST Affected Line Detection**: Correctly propagates the `affected_line` (PST branch ID) in PST action details, ensuring branch highlighting in the UI results.

### Documentation

- Added detailed technical documentation for the Superposition Theorem implementation and its application to topological and PST-based remedial actions.

---

## [0.1.8] - 2026-03-16

### Added

- **Superposition Theorem Integration**: Implemented impact quantification for topological actions using the superposition theorem.
- **Islanding Impact Quantization**: Enhanced islanding detection to report disconnected MW, providing better visibility into the severity of grid splits.
- **Superposition Results in Analysis**: Integrated virtual flow and delta-theta computations into the analysis results dictionary.

### Changed

- **Improved Non-Reconnectable Detection**: Switched to OR logic for line isolation detection — a line is now considered non-reconnectable if at least one of its extremities is isolated (all breakers/disconnectors open).

### Fixed

- **Superposition Data Integrity**: Fixed missing data fields in superposition results and resolved `NameError` bugs in calculation modules.
- **Virtual Flow Computations**: Corrected delta-theta and virtual flow logic for more accurate impact estimation.

---

## [0.1.7] - 2026-03-11

### Added

- **Phase Shifter Transformer (PST) Support**: Integrated PST tap variations and atomized PST actions from REPAS JSON.
- **PST Support in Grid2Op conversion**: Added handling for atomized PST actions in Grid2Op format.

### Fixed

- **Analyzer Stability**: Resolved analyzer test failures and improved environment creation robustness.

---

## [0.1.6] - 2026-03-10

### Added

- **Pypowsybl Format for Rebuild Actions**: Added `--pypowsybl-format` option to `--rebuild-actions` for switch-based output.
- **Asset Identification Enhancement**: Inferred `has_line`/`has_load` from switch names in the pypowsybl backend.

### Changed

- **Network Cache Optimization**: Optimized `NetworkTopologyCache` to eliminate O(all_elements) cost per action.

### Fixed

- **Switch Operation Diffing**: Corrected `set_bus` logic to only include assets changed by the switch operation.

---

## [0.1.5] - 2026-03-07

### Added

- **Dynamic Action Content Computation**: Implemented `LazyActionDict` to compute action `content` (bus assignments) on-demand from switch states, significantly reducing action JSON file sizes.
- **Prioritization of Direct Overload Disconnections**: Added a +1.0 score boost for actions that disconnect currently overloaded lines in unconstrained regimes.
- **Thermal Limit Monitoring Factor**: Added support for rescaling thermal limits in overflow graph visualizations via `monitoring_factor_thermal_limits`.
- **Minimum Action Count Enforcement**: Introduced `MIN_*` configuration parameters to guarantee a minimum number of actions per type (reconnection, disconnection, coupling).
- **Flexible Monitoring File Routing**: Improved configuration for `LINES_MONITORING_FILE`.

### Changed

- **Two-Step Analysis Refactor**: Split `run_analysis` into `run_analysis_step1` and `run_analysis_step2` for better decoupling.
- **Improved Parameter Propagation**: Safely propagate `fast_mode` down to all simulation sub-components.

### Fixed

- **Monkey-patching Bug**: Fixed `AttributeError` in `main.py` where `_check_rho_reduction` was incorrectly accessed.

---

## [0.1.4] - 2026-03-04

### Added

- **Pre-Existing Overload Filtering**: Pre-existing overloads (already overloaded in N state) are excluded from N-1 analysis results and `max_rho` prioritization, unless worsened by a configurable threshold. Controlled by `PRE_EXISTING_OVERLOAD_WORSENING_THRESHOLD` (default 0.02).
- **Pypowsybl Backend Optimizations**:
    - **Incremental Simulation Branching**: Remedial actions now branch directly from converged N-1 contingency states, leveraging "hot starts" and ensuring state consistency.
    - **Simulation Fast Mode**: Introduced `PYPOWSYBL_FAST_MODE` (default: true) which disables voltage control for shunts and transformers during variants to significantly boost speed.
    - **Automatic Fallback Mechanism**: Simulations in "fast" mode automatically fallback and retry in standard "slow" mode if they fail to converge or diverge.
    - **Vectorized Observation Creation**: Over 80% reduction in observation initialization time via NumPy-based state extraction.
    - **Batched Topological Changes**: Multiple switch and bus changes are now applied in fewer pypowsybl update calls.
- **Robustness Improvements**:
    - **Flexible Switch ID Matching**: Improved ID matching supporting substation-prefixed switch names.
    - **Unified Initialization Fallback**: Consistent fallback from `PREVIOUS_VALUES` to `DC_VALUES` initialization in `network_manager`.
    - **Consistent Simulation Tuning**: Applied "fast" mode logic consistently across `observation.simulate` and `overflow_analysis` (PTDF-based) passes.

### Fixed

- **Switch Action Test Fix**: Corrected mock registry in `test_switch_action_apply_to_network` to properly verify batch switch updates.

### Tests

- Added `verify_incremental_branching.py` script for end-to-end variant state validation.
- Enhanced `tests/test_pypowsybl_backend.py` with specific test cases for incremental branching and fast mode logic.
- Add `test_pre_existing_overloads_excluded_from_analysis` and `test_pre_existing_overloads_excluded_from_max_rho`.
- Regression test for `run_analysis` to handle pre-existing overloads correctly when all lines are monitored.

---
## [0.1.3] - 2026-02-25

### Added

- **Configurable Line Extremity Loading**: Added `MAX_RHO_BOTH_EXTREMITIES` flag in `config.py` (default: false). When true, the pypowsybl backend evaluates the maximum loading rate (`rho`) from both extremities of a line using potentially distinct thermal limits.
- **Improved Limit Parsing**: `network_manager` `get_thermal_limits` returns a struct that can support separate limits for line origins and extremities.
- **Launch Options for Action Filtering**: Added `MIN_LINE_RECONNECTIONS`, `MIN_CLOSE_COUPLING`, `MIN_OPEN_COUPLING`, and `MIN_LINE_DISCONNECTIONS` to `config.py` to ensure minimum counts of each action type are considered. The `main.py` pipeline is updated to enforce these minimums by pulling up relevant actions if they aren't met naturally.
- **Ignore Monitoring Flag**: Added `IGNORE_LINES_MONITORING` flag to optionally bypass lines monitoring limits under specific configurations.
- Explicit test `test_max_rho_both_extremities` added to the test suite to verify loading calculation bounds behavior.

### Fixed

- **Improved Disconnection Scoring Constraints**: Fixed issue #30 where disconnection constraint formulas used incorrect bounding states. Upgraded `compute_line_disconnection_action_score` to properly evaluate redispatch limits between N-1 baseline (`obs_defaut`) and N-2 (`obs_linecut`) utilizing the actual line capacities.
- **Unconstrained Disconnection Regime**: Scoring logic simplified to use direct flow ratio (`capacity * (1 - rho_before) / (rho_after - rho_before)`) when no new overloads are instantiated.
- **CI Dependency formatting**: Cleaned up trailing commas and spaces in `requirements.txt`.

---

## [0.1.2] - 2026-02-20

### Added

- **Action scores dictionary**: `run_analysis()` now returns an `action_scores` dict alongside `prioritized_actions`. It has four keys — `"line_reconnection"`, `"line_disconnection"`, `"open_coupling"`, `"close_coupling"` — each containing:
  - `"scores"`: `{action_id: float}` sorted by descending score.
  - `"params"`: underlying scoring hypotheses (thresholds, flow bounds, etc.).
- **Line disconnection scoring**: asymmetric bell curve (alpha=3, beta=1.5) centred between the minimum required redispatch and the maximum tolerable redispatch; score is positive inside the acceptable window and negative outside.
- **Node merging scoring**: delta-phase score (`theta2 − theta1`) based on voltage angle difference between the two buses being merged; the red-loop bus (carrying more positive dispatch flow) is used as the reference.
- **Node splitting — per-action details**: `compute_node_splitting_action_score_value` now returns a `(score, details)` tuple. `details` contains `node_type`, `bus_of_interest`, and the four flow components (`in_negative_flows`, `out_negative_flows`, `in_positive_flows`, `out_positive_flows`) for the selected bus; these are stored per-action in `params_splits_dict` and exposed through `action_scores["open_coupling"]["params"]`.
- **Per-action assets for coupling actions**: `action_scores["open_coupling"]["params"]` and `action_scores["close_coupling"]["params"]` now include per-action `"assets"` dictionaries listing the lines, loads, and generators connected to the scored bus.
- **Unconstrained disconnection scoring**: when the overflow graph produces no new overloads after redispatch (i.e. `max_redispatch = ∞`), a linear ramp replaces the bell curve — score = 1 at `max_overload_flow`, linearly decreasing to 0 at `min_redispatch`, and negative quadratic tail below. The `params` field includes a `"regime"` indicator (`"constrained"` or `"unconstrained"`).
- **Score rounding**: all float values in `action_scores` (both scores and params) are rounded to 2 decimal places.

### Fixed

- **Red loop bus identification** in `compute_node_merging_score`: the bus connected to the red loop is now correctly identified as the one with the **most positive** dispatch flow on its overflow graph edges (previously used negative flow, which was inverted).
- **Test tuple unpacking**: `test_integration_full_scoring_pipeline` now correctly unpacks the `(score, details)` tuple returned by `compute_node_splitting_action_score_value`.

### Tests

- `TestNodeSplittingScoreValueReturn` (5 tests): verifies the `(score, details)` tuple return format, required keys in `details`, flow values matching the selected `bus_of_interest`, `node_type` propagation, and the empty-buses edge case.
- Backward compatibility tests: `compute_node_splitting_action_score` wraps a plain-float return as `(float, {})` and passes a tuple through unchanged.
- `TestDiscoveryParamsStorage` (4 tests): verifies that `params_reconnections`, `params_disconnections`, `params_splits_dict`, and `params_merges` are correctly populated after each discovery method.
- `TestActionScoresStructureAndRounding` (7 tests): verifies the assembled `action_scores` structure, descending sort order, 2-decimal rounding for flat and nested params, and graceful handling of empty categories.
- `TestUnconstrainedLinearScore` (7 tests): verifies the linear ramp scoring for the unconstrained disconnection regime — score at min/max/midpoint, capping at 1 above max, zero at min, negative quadratic tail below min, and increasingly negative further below.

---

## [0.1.1.post4] - 2026-02-17

### Changed

- Made `grid2op` fully optional across `make_env_utils` and related modules; importing the package no longer fails when `grid2op` is not installed.

---

## [0.1.1] - 2026-01-xx

### Added

- `run_analysis()` now returns a detailed output dictionary including per-action metadata (type, substation, lines involved, simulation results).

### Fixed

- Variant ID collision in `pypowsybl` `simulate(keep_variant=True)` caused incorrect results when the same variant was reused across simulations.

---

## [0.1.0.post1] - 2025-xx-xx

### Added

- `reco_deco` (reconnect-then-disconnect) composite actions are now included in the default action space.

### Changed

- Optimised reconnectable line detection: uses `expertop4grid` new methods, collapses graph to key components, and retains only the main overflow components.

---

## [0.1.0] - 2025-xx-xx

### Added

- Initial PyPI release.
- Modular package structure (`action_evaluation`, `graph_analysis`, `pypowsybl_backend`, `utils`).
- Pure pypowsybl backend (`--backend pypowsybl`) as an alternative to Grid2Op.
- Expert rule engine for filtering topology actions.
- Action prioritisation: line reconnections, disconnections, node splitting, and node merging.
- Overflow graph construction and visualisation via `alphaDeesp` and `networkx`.
- CLI entry point with `--date`, `--timestep`, `--lines-defaut`, `--backend`, `--rebuild-actions` flags.
