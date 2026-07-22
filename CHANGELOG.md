# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

## [0.3.1.post1] - 2026-07-22

### Fixed

- **Reused pypowsybl env no longer "contaminated" by a DC escalation (issue #6).**
  When an overload-disconnection load flow diverges, `run_analysis_step2_graph`
  calls `switch_to_dc_load_flow_pypowsybl`, which escalated the *shared* env to
  DC by setting `NetworkManager._default_dc = True` and DC-solving the base
  variant (leaving every bus `v_mag = NaN`, since a DC load flow computes no
  voltage magnitudes). That escalation was never reset, so a subsequent analysis
  reusing the same `SimulationEnvironment` (batch classifiers, long-lived
  services, the Co-Study4Grid game backend) ran *every* load flow in DC — where
  branch currents are not populated, so `rho ≈ 0` and real overloads **silently
  vanished** (`run_analysis_step1` returned an empty "Overload breaks the grid
  apart" short-circuit). The escalation is now recorded
  (`SimulationEnvironment._dc_escalation_pending`) and undone at the next
  analysis boundary: `run_analysis_step1`'s reused-env branch calls
  `SimulationEnvironment.reset_loadflow_mode_to_baseline(config.USE_DC_LOAD_FLOW)`,
  restoring the configured baseline load-flow mode and an AC-solved (energized)
  base variant — exactly like a freshly built env. Behaviour-preserving on the
  non-divergent path (the restore only fires after an actual DC escalation) and
  it removes the need for the game-mode classifier's env-rebuild workaround. The
  earlier "deep, unrecoverable pypowsybl network-global corruption" diagnosis in
  `docs/reviews/2026-07_env_corruption_overflow_graph.md` was an artifact of the
  leaked `_default_dc` flag (which silently turned every "recovery" AC solve back
  into DC); the write-up is corrected there. Regression test:
  `tests/test_env_reuse_dc_escalation.py` (IEEE-9 based — no France-grid fixture).

## [0.3.1] - 2026-07-21

### Changed

- **Cap `pypowsybl<1.16.0`.** pypowsybl 1.16.0 makes a bare `import pypowsybl`
  eagerly import its new `opf` submodule, which hard-imports `pyoptinterface`
  (an OPF solver this package neither uses nor depends on) → `import pypowsybl`
  raises `ModuleNotFoundError: No module named 'pyoptinterface'`. This broke
  the `grid2op-optional` CI leg (which `pip install -e .` picks the latest
  pypowsybl). 1.13–1.15 import cleanly; the cap is lifted once pypowsybl makes
  the `opf`/`pyoptinterface` import lazy — tracked in
  marota/Expert_op4grid_recommender#5.

### Added

- **RTE7000-THT benchmark findings documented**
  (`docs/reviews/2026-07_tht_benchmark_findings.md` + « Constats de banc »
  sections in `docs/recommender/{superposition_module,load_shedding,antenna_overflow_graph}.md`):
  with a curated topological action space (real couplers + line
  disconnections) **94 % of resolutions are topology-only** — load shedding
  demotes to a true last resort (39/43 of its resolutions requalify);
  8 contingencies are resolvable **only by an action pair** (true-simulation
  validated; GST bias +0.05 median but large over-promises on
  high-rerouting pairs → true-sim validation of pairs is mandatory);
  the antenna regime dominates the unresolved set (112/137) and calls for
  MW-sized load shedding. Bench lives in `marota/Grid_snapshot_reconstruct`
  (`benchmarks/expert_op4grid_recommender/`).
- **alphaDeesp Dijkstra overflow-graph optimisation — now upstream, patch
  removed.** The vendored `patched_alphadeesp.py` (precomputed edge-attribute
  weight in `_compute_sssp_paths`, −27 % on
  `add_relevant_null_flow_lines_all_paths`, bit-identical output) is dropped:
  both fixes it carried landed in **expertop4grid 0.3.3** — the precomputed
  string-weight `_compute_sssp_paths` (ExpertOp4Grid_marota#1, "bless or
  fix", default bit-identical to the old hop-cost behaviour) and the
  empty-red-loops guard in `get_dispatch_edges_nodes`
  (ExpertOp4Grid_marota#2). The dependency floor is bumped to
  `expertop4grid>=0.3.3` accordingly and the import-time patch wiring +
  its test are removed. (The patch's secondary target-side reverse-Dijkstra
  variant is not upstream; it only helped when `|targets| < |sources|` and is
  left for a possible separate upstream contribution.)
- **`USE_DC_FOR_OVERFLOW_GRAPH` config flag** (opt-in, default off — AC
  observations remain the default): runs the overflow-graph flow transfer
  in DC with a power-derived rho; −59 % graph build on a numerically stiff
  operating point, same best `max_rho`.

## [0.3.0.post1] - 2026-07-08

A `.post1` release on top of `0.3.0` (no breaking changes; performance
follow-up). Cuts the per-action reassessment cost on large grids without
sacrificing accuracy. See `docs/release-notes/v0.3.0.post1.md`.

### Changed

- **"Fast mode" redefined — accurate *and* fast.** `run_load_flow(fast=True)`
  no longer disables transformer/shunt voltage control (which changed the
  branch currents). It keeps tap regulation on but runs the tap-changer outer
  loop in `AFTER_GENERATOR_VOLTAGE_CONTROL` instead of the provider default
  (incremental). On the full France grid one post-contingency AC load flow
  drops from ~57 Newton iterations (~4.6 s) to ~20 (~0.7 s) for the same
  constrained-line current (<0.5 % delta). Slow mode (`fast=False`) keeps the
  incremental loop as the max-fidelity fallback.
- **`PYPOWSYBL_FAST_MODE` now defaults to `True`.** Now that fast mode is
  accuracy-preserving, it is the sensible default.
- **Reassessment parallelism is fast-mode aware.** The parallel per-action
  reassessment clones the whole network per worker and builds a fresh
  `SimulationEnvironment` (whose `_ensure_valid_state` baseline LF now runs in
  fast mode: ~4.6 s → ~0.7 s per worker). Because fast per-action LFs no longer
  amortize that per-worker clone + baseline tax, `_should_parallelize_reassessment`
  now stays **serial** in auto mode until `n_actions >= workers *
  _FAST_MODE_MIN_ACTIONS_PER_WORKER` (6). Net: on a 20-core host the France
  `P.SAOL31RONCI` 15-action case now auto-selects serial and lands at ~21 s
  (~1.4 s/action) with no env var; a memory-bandwidth-limited host that was
  paying ~38 s for the parallel clones is the biggest winner. Slow mode and the
  explicit `REASSESSMENT_PARALLEL=True` force are unchanged.

### Tests

- `tests/test_reassessment_parallel_gate.py` — new `TestFastModeGate` (fast-mode
  serial gate, many-actions escape hatch, force-parallel override).

## [0.3.0] - 2026-07-07

Follow-up slice of the 2026-07 full code review (findings M3–M6, M8, P3–P4,
and the R7 IHM promotion), on top of 0.2.9. Behaviour-preserving for the
analysis pipeline; the maneuver IHM gains a production serving path and the
dependency floors are reconciled. See `docs/release-notes/v0.3.0.md`.

### Added

- **Vendored, import-time pypowsybl integer-value patch (M5).**
  `expert_op4grid_recommender/patched_backend.py` applies the
  `update_integer_value` `0 → −1` fix on `pypowsybl.grid2op.Backend` via an
  idempotent, version-guarded class patch at package import
  (`apply_pypowsybl_integer_value_patch()`). No site-packages edit is required;
  the standalone `scripts/patch_pypowsybl2grid_file.py` remains only as a manual
  fallback. It is a no-op when pypowsybl is absent and self-disables if a future
  upstream ships the fix. New coverage: `tests/test_patched_backend.py`. (The
  `make_patched_pypowsybl_backend` factory depends on `pypowsybl2grid`, now
  deprecated — see below.)
- **Maneuver IHM as a serveable app (R7 partial / M8).** `scripts/manoeuvre_ihm.py`
  gains a `create_app()` application factory, a production `waitress` serving
  path, and a `/healthz` endpoint, so the Flask IHM can be launched as a proper
  WSGI app rather than only via the debug server.

### Changed

- **Observability: stop hijacking the root logger (M6).** The package no longer
  reconfigures the root logger on import; it logs through its own named logger,
  so importing `expert_op4grid_recommender` no longer changes a host
  application's logging. `utils/action_rebuilder.py` now re-raises on failure
  instead of swallowing the error and returning a partial result.
- **Test infrastructure hardening (M3).** `--strict-markers` plus a registered
  `slow` marker (an unregistered mark now errors at collection instead of
  silently running as a fast test), opt-in coverage config
  (`pytest --cov=expert_op4grid_recommender`), and repair of two dead/broken
  tests — including the `get_maintenance_timestep` mock test, which had never
  run because it shared a name with the `@slow` real-env test that shadowed it.
- **Dependency declarations synced + grid2op-optional contract enforced (M4).**
  `pyproject.toml` / `requirements.txt` floors reconciled (`expertop4grid >= 0.3.2`,
  explicit `pydantic` / `pydantic-settings`), the Grid2Op backend's direct deps
  moved to a `[grid2op]` extra, and a CI leg that removes the grid2op family and
  asserts the pure-pypowsybl pipeline still imports/runs (the PR #26 optionality
  contract).

### Deprecated / Removed

- **`pypowsybl2grid` deprecated and dropped as a dependency.** Its `0.3.0` wheel
  pins `numpy==1.26.4`, which is unsatisfiable against this package's
  `numpy>=2.0.0` and broke the install resolve (in CI and anywhere numpy 2 is
  required). It is only used by the legacy grid2op+pypowsybl "assistant env"
  bridge (`utils/make_assistant_env.create_pypowsybl_backend` →
  `patched_backend.make_patched_pypowsybl_backend`); the pure-pypowsybl and
  grid2op(lightsim2grid) analysis paths never needed it. It is removed from
  `requirements.txt` and `pyproject.toml`, the CI `pypowsybl2grid` patch step is
  dropped, the import-time version guard in `utils/make_env_utils.py` is softened
  to a `DeprecationWarning`, and the two bridge entry points now emit a
  `DeprecationWarning`. Install `pypowsybl2grid` manually into a `numpy<2`
  environment if you still need that legacy path. The generic
  `apply_pypowsybl_integer_value_patch()` (on `pypowsybl.grid2op.Backend`) is
  unaffected.

### Performance

- **NetworkManager name-array caching + `set_thermal_limit` O(n²) fix (P4).**
  The `name_line` / `name_sub` / … arrays are cached instead of rebuilt on every
  property access, and `set_thermal_limit` no longer rescans all branches per
  element.
- **Cached membership sets in action application (P3).** Repeated per-element
  `in` scans over full element lists are replaced by precomputed sets in the
  pypowsybl action-application path.

### Development

- **SessionStart git-sync init check.** `.claude/hooks/init-sync-check.sh`
  (+ `.claude/settings.json`) is a read-only, non-fatal check that, when a
  session starts on the `marota` fork, fetches and reports how far the branch is
  from upstream and reminds that PRs target `ainetus`. Runtime-generated
  `Overflow_Graph/` artifacts are now gitignored.

## [0.2.9] - 2026-07-06

### Changed

- **Discovery restructured around data (deep revisions R5 + A5).** Action
  discovery no longer carries its per-family outcome as ~40 hand-repeated
  instance attributes. `action_evaluation/discovery/_results.py` holds one typed
  `FamilyResult` per family in `self.results`, described by a declarative
  `FAMILY_SPECS` registry that also generates back-compat `@property` bridges
  (zero mixin/test churn). Consequences:
  - `__init__` boilerplate, the PST `getattr` special-casing (PST is now a
    normal family in `self.results`), the 8× `action_scores` literal, and the
    18 hand-written prioritization calls all collapse to data-driven loops
    (`MIN_PHASE_ORDER` / `FILL_PHASE_ORDER`, one entry per family). This removes
    a `renewable_curtailment` add that had slipped in **twice** per phase — a
    real latent double-add.
  - PST scoring's cross-file, order-sensitive `_disco_bounds` del/lazy protocol
    is replaced by a memoised `_get_disconnection_bounds()` returning a frozen
    `DisconnectionBounds`.
  - `InjectionDiscoveryBase` factors the shared overload preamble + influence
    factor out of the three injection families (the divergent cache / flow-arity
    pairing is intentionally kept per-family).
  - `DiscovererProtocol` declares the shared surface the mixins depend on.
  Behaviour-preserving (mock discovery suite green; byte-identical `action_scores`).
- **`ActionType` enum + declarative classifier; C7 rule-bypass fix (R5 / C7).**
  `action_evaluation/action_types.py` gives the action-type vocabulary a typed
  home (values byte-identical to the historical strings) and turns the
  classifier's description cascade into one ordered keyword table. `rules.py`
  now localizes grid2op-format couplings (substation in
  `content['set_bus']['substations_id']`) backend-agnostically — previously they
  fell through to `"unknown"` and escaped every expert rule (silent, backend-
  specific bypass).
- **R6 (partial):** verified all four C6 `utils/` bugs are fixed and gave the two
  previously-untested data modules their first tests. The `utils/` file-move
  reorganization (`repas/` subpackage, environment merge, superposition /
  reassessment promotion, `__main__` → `scripts/`) remains to be landed as its
  own PR.
- **Container-aware reassessment CPU detection (completes the 2-vCPU fix).**
  `0.2.7.post1` gated parallel reassessment behind
  `min(10, cores, n_actions) >= 4`, but `cores` still came from
  `os.cpu_count()`, which reports the **host** core count inside a CPU-limited
  container (e.g. a 2-vCPU box on a 16-core host) — so the gate never fired and
  the reassessment still over-subscribed the 2 vCPUs and ran *slower* than
  serial (a reported ~47 s). CPU detection is now container-aware
  (`_effective_cpu_count()` = min of `os.cpu_count()`, scheduler affinity, and
  the cgroup CPU quota `cpu.max` / `cpu.cfs_quota_us`), so the effective core
  count reflects the container and the existing worker-count gate correctly
  stays serial. Two new config knobs complement the retained `0.2.7.post1` env
  var (`EXPERT_OP4GRID_MIN_PARALLEL_REASSESS_WORKERS`), all env-overridable via
  `EXPERT_OP4GRID_*`:
  - `REASSESSMENT_PARALLEL`: `None` = auto (default), `True` = force parallel,
    `False` = force serial (recommended on 2-vCPU deployments).
  - `REASSESSMENT_MIN_PARALLEL_CORES` (default 4): in auto mode, the minimum
    effective cores required before parallelising.

  Behaviour-preserving otherwise. New unit coverage:
  `tests/test_reassessment_parallelism.py`.

## [0.2.8] - 2026-07-04

### Changed

- **Config single source of truth (deep revision R3)** — the pydantic `Settings`
  instance is now authoritative, without locking the module down (Co-Study4Grid
  still drives the recommender by mutating `config.*` attributes directly):
  - Derived paths (`CASE_NAME`, `ENV_FOLDER`, `ENV_PATH`, `ACTION_SPACE_FOLDER`,
    `ACTION_FILE_PATH`, `SAVE_FOLDER_VISUALIZATION`) are `@computed_field`s on
    `Settings`, so overriding `ENV_NAME` / `FILE_ACTION_SPACE_DESC` recomputes
    `ENV_PATH` / `ACTION_FILE_PATH` (fixes the review-A3 staleness bug). They are
    still promoted to module attributes via `model_dump()` for the many
    `config.X` readers.
  - New `config.get_settings()` / `config.override_settings()` /
    `config.reset_settings()` accessors: `override_settings` runs full pydantic
    validation, recomputes the derived paths, and re-promotes to the module
    namespace. `pipeline.py` routes its `ENV_NAME` override through it instead of
    raw module mutation.
  - The 29 defensive `getattr(config, 'X', default)` sites collapsed to direct
    attribute access — every key is a guaranteed `Settings` field.
  - Tests: the hand-forked `tests/config_test.py` and the `sys.modules` swap are
    deleted; `conftest.py` applies the test deltas via
    `config.override_settings(**TEST_CONFIG_DELTAS)`, so `Settings` validation now
    runs in CI (closes review-M2).
- **Unified per-backend simulation seam behind `BaselineContext` (deep revision
  R4)** — behaviour-preserving (grid2op output byte-identical; real pypowsybl
  end-to-end verified):
  - `utils/simulation_pypowsybl.py` is deleted; `utils/simulation.py` is the
    single backend-agnostic module. The two real backend differences are explicit
    parameters (`simulate_kwargs` for pypowsybl `keep_variant` / `fast_mode`;
    `reapply_contingency` for the grid2op-branches-from-N-state vs.
    pypowsybl-branches-from-kept-variant contract) instead of a forked file.
    `check_rho_reduction_with_baseline` now takes the branch observation and the
    contract flag explicitly, so the opposite-first-argument-contract trap that
    bred the C-diag bug is gone by construction (and the three injection mixins
    route through the same explicit seam).
  - New `BaselineContext` (`act_defaut` / `baseline_rho` / `obs_baseline` /
    `branch_obs` / `release()`), built once per run by `_get_simulation_baseline`
    and freed by `_release_simulation_baseline` at the end of
    `discover_and_prioritize`.
  - `NetworkManager` gained a kept-variant registry (`register_kept_variant` /
    `sweep_kept_variants`, `max_kept_variants=256`) with an LRU backstop that
    never evicts the base or the working variant, bounding a long-running service
    that reuses one `NetworkManager` across analyses (closes the cross-run half of
    review-C4).

## [0.2.7.post1] - 2026-07-03

### Fixed

- **Reassessment: stay serial on low-core hosts** (`utils/reassessment.py`). The
  parallel per-action reassessment (0.2.6) clones the whole network per worker
  (`save`/`load_from_binary_buffer` + `SimulationEnvironment` + observation
  build) — a fixed overhead the serial path never pays, only amortized above
  ~3-4 usable cores. On a **2-vCPU host** (e.g. a small HuggingFace Space) the
  two clones + GIL contention on the Python-side observation build made the
  "parallel" path **slower than serial** (observed ~42 s vs ~9 s on a many-core
  Mac for the same 15-action pan-European case). Parallel reassessment is now
  gated behind `min(10, cores, n_actions) >= 4` (env-tunable via
  `EXPERT_OP4GRID_MIN_PARALLEL_REASSESS_WORKERS`; floored at 2). 2-vCPU
  deployments fall back to the faster serial path; ≥4-core hosts keep the
  speed-up. Behaviour and output are otherwise unchanged.

## [0.2.7] - 2026-07-03

### Changed

- **Typed pipeline spine + backend protocol (deep revisions R1 + R2)** — a
  behaviour-preserving restructure of the analysis entry point (verified
  byte-identical prioritized-action output on the bare-env golden):
  - **`SimulationBackend` protocol** (`backends.py`, new): the 18 `*_grid2op` /
    `*_pypowsybl` delegation wrappers, the 8 backend-selected function pointers
    that used to live in the context dict, the `if is_pypowsybl:` call-site forks
    and the `discoverer._*` monkey-patching are all replaced by a single
    protocol with two implementations (`Grid2opBackend`, `PypowsyblBackend`) that
    hold `fast_mode` as constructor state. The discovery pass is now configured
    from the backend's flags (`branch_candidates_from_baseline`,
    `use_shared_baseline_for_topological`) passed to `ActionDiscoverer.__init__`
    instead of being monkey-patched; the shared-baseline routing became a real
    `DiscovererBase._shared_baseline_check` method.
  - **`AnalysisContext` / `AnalysisResult` dataclasses** (`pipeline.py`, new):
    replace the ~41-key untyped context dict and the untyped result dict. Both
    keep a dict-compatible view (`DictCompatMixin`) so existing item-access
    consumers are unaffected. `run_analysis_step1` now returns
    `AnalysisContext | AnalysisResult` (the no-overload short-circuit) instead
    of the `(Optional, Optional)` sentinel tuple.
  - **`SimulatedAction` is now actually used**: the reassessment stage emits the
    typed `SimulatedAction` (previously defined, exported, tested — never
    instantiated) for each action card.
  - **`main.py` split into `cli.py` + `pipeline.py`**, and `_run_expert_discovery`
    moved under `models/_expert_discovery.py`. The dependency direction is now
    `cli → pipeline → models → action_evaluation/graph_analysis → utils`,
    dissolving the three import cycles (`models.expert ↔ main`,
    `reassessment ↔ main`, `main ↔ models/reassessment`). `main.py` remains as a
    thin re-export facade so every `from expert_op4grid_recommender.main import …`
    keeps resolving.

## [0.2.6] - 2026-07-03

### Performance

- **Parallel action reassessment** (`utils/reassessment.py`): the end-of-run
  per-action re-simulation now runs across worker threads, each on a private
  pypowsybl network copy cloned from the N-1 baseline variant (pypowsybl
  releases the GIL during the load flow, but the working variant is
  network-global, so shared-network concurrency would race). Workers =
  `min(10, cpu_count, n_actions)`; results are bit-identical to serial with a
  robust serial fallback. The worker/core count used is exposed in
  `result["reassessment_parallelism"]` (for the reassessment-time tooltip).
  Measured ~1.43× on a 4-core host (Co-Study scenario-1 pypsa-eur, 15 actions);
  larger on higher-core hosts.
- **Observation construction** (`pypowsybl_backend/observation.py`): removed
  redundant cross-JNI DataFrame fetches (`get_buses` ×3→×1, `get_loads` ×2→×1)
  and cached the variant-invariant line R/X on the `NetworkManager` instead of
  re-fetching `get_lines()` / `get_2wt()` per observation (~0.47→0.25 s per
  observation build).
- **Shared discovery baseline** (`action_evaluation/discovery`): the topological
  discovery passes share one contingency baseline load flow instead of
  recomputing it per candidate (active when `CHECK_ACTION_SIMULATION` is on),
  halving discovery load flows and removing a per-candidate kept-variant leak.

### Fixed

- **Security (maneuver IHM)**: `/api/load_scenario` no longer allows path
  traversal — the client-supplied scenario name is sanitized and confined to the
  scenarios directory (HTTP 400/404 on invalid names); `/api/config` only accepts
  scenario/sequence directories under an allowed root, closing a zip-archive
  exfiltration / arbitrary-write vector.
- **Load-flow divergence no longer terminates the host process**: `environment.py`
  and `environment_pypowsybl.py` raise `LoadFlowDivergedError` (a `RuntimeError`
  subclass caught by the CLI) instead of `sys.exit(0)`.
- **`utils/load_training_data.py` / `load_evaluation_data.py`**: fixed a
  `StateInfo` indexing `TypeError`, a `NameError` on a `__main__`-only global, a
  `raise "string"`, and a maintenance "reconnect" action that actually
  disconnected the lines.
- **Overflow-graph edge cache** (`action_evaluation/discovery`): keyed by the
  full `(u, v, key)` id so parallel circuits are no longer collapsed in
  flow-influence scoring (regression test added).

### Changed

- **CI migrated from CircleCI to GitHub Actions** (`.github/workflows/ci.yml`):
  faithful port of the `build-and-test` (pytest + pypowsybl2grid patch) and
  `quality` (ruff / interrogate / radon) jobs; `.circleci/` removed.

### Added

- `scripts/benchmark_pipeline.py` — per-phase load-flow benchmark of the
  pypowsybl analysis pipeline.
- `docs/reviews/2026-07_full_code_review.md` — comprehensive architecture /
  performance / maintainability review of the repository.

### IHM de manœuvre : « Explorer la journée » — carte des postes + intérêt

Sous l'onglet **RTE7000**, après le choix d'une date, le bouton **🗺 Explorer la
journée** ouvre une **carte du réseau France** (postes localisés) qui met en
évidence les postes **les plus actifs** de la journée, et permet de basculer en
vue topologique d'un poste **à l'heure souhaitée**.

- **Estimation de l'intérêt** (`manoeuvre/dataset/exploration.py`, nouveau) :
  charge **3 situations** (minuit / midi / 23 h) et compte, **par poste**, le
  **nombre d'OC dont l'état change** sur la journée, **ventilé par type d'OC**
  (`BREAKER` / `DISCONNECTOR` / `LOAD_BREAK_SWITCH`). Cœur d'agrégation **Python
  pur** (`changements_par_vl`). **Classement et mise en évidence au niveau voltage
  level** (granularité fine) ; les **10 premiers VL** sont haloés sur la carte.
- **Re-groupements de nœuds (scissions / fusions) comptabilisés** : au-delà des
  OC, le décompte intègre le **nombre minimal d'ouvrages** séparés dans un nouveau
  nœud ou ayant rejoint un nœud au cours de la journée — des configurations tout
  aussi intéressantes à inspecter. La structure topologique invariante (arêtes +
  ouvrages par nœud) est extraite une fois (`extraire_structure_topo`) ; le cœur
  pur calcule la **partition nodale** par situation (`partition_ouvrages`) et la
  **distance de transfert** entre situations consécutives (appariement de blocs
  maximisant les ouvrages conservés → `noeuds_deplaces`), en retenant le **max**
  sur les transitions (`changements_nodaux_par_vl`, stable face à une scission
  suivie d'une fusion). Intégré au `total` (`fusionner_nodaux`) et affiché (badge
  ⚇ dans le classement, ligne dédiée dans la bulle).
  - **Ouvrages isolés ignorés** : un ouvrage **déconnecté** (composante sans jeu de
    barres) n'est **pas** un nœud électrique → exclu de la partition
    (`partition_ouvrages(..., barres)`, jeux de barres relevés par
    `extraire_structure_topo`) ; (dé)connecter un ouvrage isolé ne compte pas comme
    re-groupement (seules les vraies scissions/fusions de jeux de barres comptent).
    Corrige une inflation du décompte (ex. un poste affichant ⊝7 dû à des ouvrages
    déconnectés).
- **Vue topologie : décompte de nœuds à l'affichage (ouvrages isolés exclus)** —
  le badge « X nœud(s) » (départ / cible / étape) et la ligne « obtenu / visé » ne
  comptent plus que les **nœuds réels** (composantes avec jeu de barres ;
  `_nb_noeuds_reels`), cohérent avec l'éditeur nodal qui présente les ouvrages
  isolés à part. **Affichage uniquement** : le moteur de séquencement conserve
  `TopologieNodale.nb_noeuds` (isolés inclus, requis p. ex. en mise en service) et
  le verdict de réalisabilité est inchangé.
- **Coordonnées des postes** (`manoeuvre/dataset/geographie.py`, nouveau) — le
  dataset RTE 7000 ne portant **pas** de coordonnées, chaîne de résolution :
  (1) **plan de masse RTE committé** (`manoeuvre/dataset/grid_layout_rte.json`,
  `{nom_VL: [x, y]}`) — **primaire, hors-ligne, ~98 %** des postes, **rien à
  configurer** ; (2) instantané committé `data/postes_rte_geo.json` ;
  (3) **OpenStreetMap / Overpass** — repli runtime, postes RTE taggués
  `power=substation` + **`ref:FR:RTE` = `substation_id`** + lat/lon (Web Mercator
  côté serveur). Résultat OSM **persisté** + **téléchargeable**
  (`GET /api/explore_coords_file`, bouton « ⬇ coordonnées »). Toggle
  `MANOEUVRE_ENABLE_OSM`. ODRE (`postes-electriques-rte`) est **tabulaire sans
  géométrie** → inutilisable pour la carte. Sans aucune coordonnée, l'IHM reste
  utile : **classement en liste** + **diagnostic** d'appariement. Approche
  détaillée (plan de masse → projection écran → fond calibré) :
  **`docs/manoeuvre/carte_geographique.md`**.
- **Carte** (frontend) : SVG **autonome** (sans tuiles ni librairie externe),
  disques **colorés par niveau de tension** sur un **fond géographique réel**
  (frontières départements + pays voisins, calibrées dans le repère du plan de
  masse — `scripts/build_france_basemap.py`, `france_basemap.json`,
  `GET /api/explore_basemap`), **zoom/déplacement** par `viewBox`, **sélecteur
  d'heure** d'ouverture en en-tête. **Clic** →
  bulle d'information ; **double-clic** → vue topologique du poste, avec une barre
  d'exploration (**Départ** 00 h/12 h/23 h, **Retenir comme cible** 00 h/12 h/23 h,
  sélecteur de niveau de tension) ; l'**heure** et le **champ du poste** sont
  synchronisés.
- **Légende des tensions filtrante** : clic sur une bande de tension de la
  légende **affiche/masque** les disques correspondants ; boutons **« tout »** /
  **« aucun »** pour (dé)sélectionner toutes les bandes (`voltToggle`, `voltAll`,
  `voltBand`, `MAP.voltOff`). Permet d'isoler un niveau (p. ex. 400 kV) sur la
  carte.
- **Sauvegarde de scénario : anti-doublon + nom unique indexé** — à l'enregistrement,
  si un scénario **identique** (même départ **et** même cible) existe déjà (parmi
  `name`, `name_0`, `name_1`…), il **n'est pas réécrit** et l'utilisateur est informé ;
  sinon le scénario est écrit sous `name` (s'il est libre) ou sous le **premier
  index libre** `name_0`, `name_1`… (`Session.save_scenario` → `{status}` ; plus de
  invite « écraser/renommer »).
- **Base de scénarios partagée + recherche par métadonnées** — les scénarios
  enregistrés sont **mutualisés** (dossier configurable `MANOEUVRE_SCENARIOS_DIR` ;
  sur le Space, sous le cache → persistable via un stockage `/data`) : toutes les
  sauvegardes alimentent la **même base**, relisible par tous. Chaque scénario porte
  des **métadonnées de recherche** (`Session.scenario_meta` : tension, nb de jeux de
  barres, nœuds départ→cible, OC changés **DJ / SA / INT**, **ouvrages déplacés**
  en changement de nœud). La modale « Recharger » devient une **recherche
  filtrante** (texte poste/nom + tension + seuils min barres/DJ/SA/INT/nœud), chaque
  résultat affichant ses métadonnées (`/api/scenarios` renvoie les objets).
  - **Tag date/heure de départ** : chaque scénario logge sa **date + heure de
    départ** — en **RTE7000** la date/heure choisies, en **local** l'horodatage du
    fichier (`net.case_date`) — d'où **année / saison / jour de semaine**
    (`Session._date_tags`). Filtres dédiés **année / saison / jour** dans la
    recherche. Au **rechargement** d'un scénario RTE7000, les sélecteurs **Date** et
    **Heure** sont **re-synchronisés** sur son contexte (`syncDepartFromScenario`).
  - **Téléchargement de toute la base** : bouton **« ⬇ Tout (zip) »** dans la modale
    de rechargement → archive ZIP de tous les scénarios (`GET /api/scenarios_archive`),
    pour exporter/versionner la base partagée en un clic.
- **Légende des tensions — double-clic d'isolement** : un **double-clic** sur une
  bande **isole** ce niveau (masque tous les autres) ; un nouveau double-clic
  lorsqu'il est déjà seul affiché **réaffiche tout** (`voltClick`/`voltDouble`,
  débounce simple/double clic).
- **Connexions inter-postes (lignes)** : la carte trace les **lignes électriques
  entre postes**, **colorées par niveau de tension** et **en fondu** (trait fin
  d'épaisseur constante au zoom, faible opacité) sous les disques — pour visualiser
  la structure du réseau sans gêner la lecture. Extraction générique
  (`exploration.extraire_connexions`, lignes/liaisons reliant deux **postes
  distincts**, dédupliquées par couple + tension) → fonctionne pour une situation
  **locale** comme **RTE7000**. **Bascule dédiée dans la légende** (« Lignes »,
  `linesToggle`, `MAP.showLines`) ; les lignes respectent aussi le filtre par
  tension. Restreintes aux extrémités **géolocalisées** (`connexions` du payload).
- **Orientation nord en haut** : le plan de masse RTE ayant déjà le nord en haut
  dans son repère (y croissant vers le sud), disques **et** fond de carte sont
  servis **sans inversion d'axe** (les sources lon/lat OSM restent projetées
  Web Mercator avec inversion). Corrige un rendu précédemment « à l'envers ».
- **Mise en évidence des écarts départ/cible** : en vue topologique, les organes
  dont l'état **diffère entre la topologie de départ et la cible** sont colorés
  **uniquement sur le schéma cible**, en **couleurs vives** — **vert flashy** =
  fermé à la cible (était ouvert), **orange flashy** = ouvert à la cible. Diff
  calculé côté serveur (`Session.diff_states`, champ `changes` des réponses) et
  appliqué par `highlightChanges` (classes `.octog-closed` / `.octog-opened`). Se
  met à jour à chaque bascule d'organe, changement d'heure ou rétention de cible.
- **Heure cible mise en évidence dès l'ouverture** : à l'ouverture d'un poste (ou
  changement de l'heure de départ), la cible vaut le départ → le **bouton heure
  cible est surligné en bleu** immédiatement (`MAP.cibleHour = heure`).
- **Changement de niveau de tension : heure cible conservée** — basculer entre les
  VL d'un même poste (boutons « Niveau ») **préserve** l'heure de départ **et**
  l'heure cible retenue (la cible est ré-appliquée sur le nouveau VL) au lieu de
  réinitialiser la cible au départ (`mapToTopo(sub, vl, hour, cibleHour)`).
- **Sélection d'un poste depuis la recherche en mode carte** (régression corrigée) :
  choisir un poste via le champ de recherche (ou la liste) **quitte la carte** et
  affiche sa topologie (`load` appelle désormais `exitMapMode`) — auparavant la
  topologie restait masquée derrière la carte.
- **Recherche de poste utilisable pendant l'exploration** : après « Explorer la
  journée », la liste de postes (recherche + présélection à gauche) est **peuplée**
  depuis le réseau de référence de l'exploration (`populatePostes` après
  `/api/postes`) — auparavant le champ restait vide (« Aucun poste »), seule la
  liste des plus actifs à droite était sélectionnable. Sélectionner un poste en
  exploration l'**ouvre à l'heure courante de la carte** (comme un double-clic),
  via `selectPoste` (route vers `mapToTopo` si une journée est explorée, sinon
  `load`).
- **Nom de scénario par défaut formaté** : le champ « nom du scénario » est
  pré-rempli selon le contexte (recalculé tant que l'utilisateur ne l'a pas édité).
  - **RTE7000** :
    `poste_AAAAMMJJ_hDepart_topoDepart{n}Noeud_hCible_topoCible{n}Noeud_(observee|modifiee)`
    — `observee` si la cible est la topologie **observée** à l'heure cible,
    `modifiee` si l'utilisateur l'a éditée. Ex. :
    `CONCAP3_20210103_1200_topoDepart1Noeud_2300_topoCible1Noeud_observee`.
  - **Local** : `poste_topoDepart{n}Noeud_topoCible{n}Noeud_nomFichier` (la cible
    est forcément modifiée vs le départ). Ex. :
    `CARRIP6_topoDepart1Noeud_topoCible3Noeud_pf_20240828T0100Z_20240828T0100Z`.
- **Fond de carte plus lisible** : pays voisins assombris (`.nbr`) pour les
  distinguer nettement des zones maritimes.
- **Endpoints** : `POST /api/explore_day`, `POST /api/explore_poste`,
  `POST /api/explore_retain_target`. **Tests** :
  `tests/manoeuvre/test_exploration.py`, `tests/manoeuvre/test_geographie.py`,
  `tests/manoeuvre/test_ihm_explore.py`. Doc : `docs/manoeuvre/ihm.md` (§ 1ter).

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
`docs/manoeuvre/manoeuvre_ihm_overview.png`.

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
