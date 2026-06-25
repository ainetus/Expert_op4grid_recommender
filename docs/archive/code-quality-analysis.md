# Code Quality & Maintainability Analysis

**Repository**: `expert_op4grid_recommender`
**Version analyzed**: `0.1.9`
**Date**: 2026-04-12
**Scope**: Static diagnostic across the `expert_op4grid_recommender/` package, tests, config, and repository root.

This document captures a snapshot of code-quality and maintainability findings. Items marked **P0** are low-risk immediate cleanups, **P1** are structural improvements, **P2** are quality-of-life upgrades.

> **Status**: All P0, P1 **and P2 items** have been completed — see [Cleanup log](#cleanup-log) at the bottom of this document. Findings below reflect the state **before** P0/P1/P2 cleanup.
>
> **Automation**: The checks captured in this document are now re-run on
> every push and pull request by the `code-quality` GitHub Actions
> workflow (`.github/workflows/code-quality.yml`), which shells out to
> `scripts/code_quality_report.py`. See
> [Automation in CI/CD](#automation-in-cicd) at the bottom of this
> document for the full set of checks, the metrics surfaced in the job
> summary, and the strict-mode regression gates.

---

## 1. Dead / duplicate code

| Finding | Location | Evidence |
|---|---|---|
| Stale fork of `observation.py` | `expert_op4grid_recommender/pypowsybl_backend/observation_timers.py` (1052 lines) | `diff` vs `observation.py` (1108 lines) shows ~589 differing lines. The "timers" copy is missing the `variant_id` parameter and the pre-computed thermal-limit arrays added later to `observation.py`. Repo-wide grep finds **zero importers**. |
| Pre-rewrite REPAS conversion module | `expert_op4grid_recommender/utils/conversion_actions_repas_original.py` (273 lines) | Superseded by `conversion_actions_repas.py` (1309 lines). Grep finds **zero importers**. Still contains a stub TODO on line 103. |
| Stray validation script at repo root | `verify_incremental_branching.py` | One-off end-to-end validation helper. Belongs under `scripts/` (which already exists). |

**Action (P0)**: Delete the two dead modules; move the validation script into `scripts/`.

---

## 2. God modules / complexity hotspots

| File | LOC | Structure | Concern |
|---|---|---|---|
| `action_evaluation/discovery.py` | **3001** | 1 class, **42+ methods** | `ActionDiscoverer` handles all seven action families in one class. `discover_and_prioritize`, `find_relevant_node_splitting`, `find_relevant_load_shedding`, `find_relevant_renewable_curtailment` are each ~200+ lines. |
| `utils/conversion_actions_repas.py` | 1309 | Flat module | REPAS parsing, Union-Find topology, and vectorized pandas transforms jumbled together. |
| `utils/superposition.py` | 1107 | 15 functions | Analytical flow calculations with no intermediate abstractions. |
| `pypowsybl_backend/observation.py` | 1108 | 1 class | Mixes constructor, vectorized init, `simulate`, thermal-limit handling. |
| `pypowsybl_backend/overflow_analysis.py` | 987 | 2 classes | `build_graph` ~100 lines; large parameter objects. |
| `main.py` | 1026 | Mixed | `run_analysis_step1` ~250 lines; backend-abstraction branching by `if/else`. |

`discovery.py` is the single biggest ongoing maintenance tax in the repo.

**Action (P1)**: Split `ActionDiscoverer` along action-type axes — one discoverer per family (`LineActionsDiscoverer`, `CouplingDiscoverer`, `PSTDiscoverer`, `LoadSheddingDiscoverer`, `RenewableCurtailmentDiscoverer`, `NodeSplittingDiscoverer`, `NodeMergingDiscoverer`) with a shared cache/context object via composition.

---

## 3. Testing gaps

- **23 test files** cover **~60 source modules**.
- Well covered: action evaluation (classifier, rules, discoverer, node splitting), PST, superposition (5 files), pypowsybl backend, LazyActionDict, min-action counts, islanding MW, config override, switch/substation extraction, visualization filtering.
- **No dedicated tests** for:
  - `graph_analysis/builder.py`
  - `graph_analysis/processor.py`
  - `graph_analysis/visualization.py`
  - `environment_pypowsybl.py`
  - `data_loader.py`
  - `utils/simulation.py`, `utils/simulation_pypowsybl.py`
  - `utils/repas.py`

Graph analysis being untested is notable given it sits on the critical path between overflow detection and action discovery.

**Action (P1)**: Add unit tests for `graph_analysis/*` and `environment_pypowsybl.py`; smoke tests for `utils/simulation*.py`.

---

## 4. Error-handling smells

### `expert_op4grid_recommender/__init__.py:33-38`

```python
except (ImportError, Exception) as e:
    try:
        import sys
        print(f"DEBUG: Failed to patch grid2op: {e}", file=sys.stderr)
    except:
        pass
```

- `except (ImportError, Exception)` is equivalent to `except Exception` — the `ImportError` is redundant.
- The nested bare `except: pass` silently swallows any error inside the error handler.
- Runtime monkey-patching `grid2op.Backend.Backend.get_shunt_setpoint` at package import time is a code smell; it also becomes dead weight once the `grid2op → pypowsybl` migration (see `docs/archive/MIGRATION_PLAN.md`) lands.

**Action (P1)**: Narrow the exception type, route through `logging`, and reassess whether the monkey-patch is still needed.

---

## 5. TODO / FIXME inventory

**14 markers across 5 files**:

| File | Count | Context |
|---|---|---|
| `utils/repas.py` | 6 | attribute handling |
| `utils/make_env_utils.py` | 3 | config consistency |
| `action_evaluation/discovery.py` | 3 | flow-analysis heuristics |
| `graph_analysis/processor.py` | 1 | topology simplification |
| `utils/conversion_actions_repas_original.py` | 1 | irrelevant — delete file |

**Action (P2)**: Promote to GitHub issues, reference issue numbers in the code.

---

## 6. Hardcoded paths / magic numbers

| Finding | Location |
|---|---|
| **Developer-specific absolute path committed to source** | `utils/load_training_data.py:341` — `"/home/donnotben/Documents/assistflux/read_history/20250228_livraison_LJN/time_series/20250527"` |
| Default thermal-limit sentinel `9999.0` | `main.py::set_thermal_limits` |
| ~7 lines of commented-out `datetime(2024, …)` scenario snippets | `config.py` header |
| Default action-space path literal | `main.py` argparse defaults |

**Action (P0)**: Remove the hardcoded `/home/donnotben/...` path (make it a function argument or config value). Move scenario snippets into a `docs/scenarios.md` or a dict.

---

## 7. Config sprawl

Three near-parallel module-level configs:

- `expert_op4grid_recommender/config.py` (122 lines) — active configuration with hardcoded `DATE`, `TIMESTEP`, `LINES_DEFAUT`, `ENV_NAME`.
- `expert_op4grid_recommender/config_basic.py` (90 lines) — alternative snapshot config.
- `tests/config_test.py` — test overrides applied via attribute assignment (`config.DO_VISUALIZATION = False`) from `conftest.py`.

**Duplicate definitions** inside `config.py` (silent-win behavior):

- `RENEWABLE_CURTAILMENT_MARGIN`
- `RENEWABLE_CURTAILMENT_MIN_MW`
- `RENEWABLE_ENERGY_SOURCES`
- `PYPOWSYBL_FAST_MODE`

Each appears twice in the file — whichever definition comes last wins, silently.

**Action (P0)**: Remove duplicate definitions.
**Action (P2)**: Migrate to `pydantic.BaseSettings` or `python-dotenv`; add schema validation and environment-variable support.

---

## 8. Root-level cleanliness

Files at the repository root that should live elsewhere:

| File | Recommended destination |
|---|---|
| `verify_incremental_branching.py` | `scripts/` |
| `SETUP_SUMMARY.md` | `docs/` |
| `SKIP_VISUALIZATION_GUIDE.md` | `docs/` |
| `pr_body_config.md` | `docs/` or delete if obsolete |
| `MIGRATION_PLAN.md` | Keep at root while migration is active |

**Action (P0)**: Reorganize into `docs/` and `scripts/`.

---

## 9. Import hygiene

- No wildcard imports detected.
- No obvious circular dependencies. Layering: `utils/` → `pypowsybl_backend/` → `action_evaluation/` → `main.py`.
- `main.py:22-26` manually inserts `project_root` into `sys.path`. Since `pyproject.toml` installs the package, this is unnecessary after `pip install -e .` and is a carry-over from script-style execution.

**Action (P2)**: Drop the manual `sys.path` insertion once all entry points use the installed package.

---

## 10. Type hints & docstrings

Rough coverage: **~40%** of functions have type hints.

- **Newer modules** (post-v0.1.5 `discovery.py` additions, `superposition.py`, `pypowsybl_backend/*`) have hints and docstrings.
- **Older utils** (`load_training_data.py`, `make_env_utils.py`, `repas.py`, `load_evaluation_data.py`, `make_assistant_env.py`) have minimal-to-zero docstrings and sparse hints.

**Action (P2)**: Back-fill type hints and docstrings on utils modules; prioritize public APIs consumed by `main.py` and tests.

---

## Priority-ordered action list

### P0 — low-risk, immediate wins ✅ done
1. ✅ Delete `expert_op4grid_recommender/pypowsybl_backend/observation_timers.py`.
2. ✅ Delete `expert_op4grid_recommender/utils/conversion_actions_repas_original.py`.
3. ✅ Remove the hardcoded developer path in `utils/load_training_data.py:341`.
4. ✅ Remove duplicate definitions in `config.py` (`RENEWABLE_*`, `PYPOWSYBL_FAST_MODE`).
5. ✅ Move root-level `.md` guides and `verify_incremental_branching.py` into `docs/` and `scripts/`.

### P1 — structural ✅ done
6. ✅ Split `action_evaluation/discovery.py` into one module per action-type family, sharing common caches via composition.
7. ✅ Add tests for `graph_analysis/*` and `environment_pypowsybl.py`.
8. ✅ Replace the bare `except` nest in `expert_op4grid_recommender/__init__.py`; narrow exception types and route through `logging`.

### P2 — quality-of-life ✅ done
9. ✅ Migrate `config.py` / `config_basic.py` to `pydantic.BaseSettings` with env-var support and validation.
10. ✅ Back-fill type hints and docstrings on `utils/` modules.
11. ✅ Convert TODO markers to GitHub issues and reference issue numbers from the code.
12. ✅ Drop the manual `sys.path` insertion in `main.py`.

---

## Appendix: module line-count inventory

| Module | LOC |
|---|---|
| `action_evaluation/discovery.py` | 3001 |
| `utils/conversion_actions_repas.py` | 1309 |
| `pypowsybl_backend/observation.py` | 1108 |
| `utils/superposition.py` | 1107 |
| ~~`pypowsybl_backend/observation_timers.py`~~ | ~~1052~~ *(deleted in P0 cleanup)* |
| `main.py` | 1026 |
| `pypowsybl_backend/overflow_analysis.py` | 987 |
| `pypowsybl_backend/network_manager.py` | 690 |
| `utils/action_rebuilder.py` | 514 |
| `action_evaluation/rules.py` | 424 |
| `utils/helpers_pypowsybl.py` | 415 |
| `pypowsybl_backend/action_space.py` | 398 |
| `utils/load_training_data.py` | 389 |
| `action_evaluation/classifier.py` | 367 |
| `environment_pypowsybl.py` | 338 |
| `pypowsybl_backend/simulation_env.py` | 339 |
| `pypowsybl_backend/topology.py` | 333 |
| `utils/helpers.py` | 332 |
| `utils/simulation.py` | 308 |
| `graph_analysis/processor.py` | 303 |
| `utils/simulation_pypowsybl.py` | 291 |
| ~~`utils/conversion_actions_repas_original.py`~~ | ~~273~~ *(deleted in P0 cleanup)* |
| `data_loader.py` | 245 |
| `environment.py` | 236 |
| `graph_analysis/visualization.py` | 222 |
| `utils/load_evaluation_data.py` | 198 |
| `utils/make_env_utils.py` | 142 |
| `config.py` | 122 |
| `graph_analysis/builder.py` | 111 |
| `data_utils.py` | 104 |
| `config_basic.py` | 90 |
| `utils/make_assistant_env.py` | 85 |
| `utils/make_training_env.py` | 68 |
| **Package total (pre-P0)** | **17439** |
| **Package total (post-P0)** | **16114** *(-1325 LOC: dead-code removal)* |

---

## Cleanup log

### P0 cleanup — completed

All five P0 items have been applied to the codebase on branch `claude/code-quality-analysis-9x1tq`.

#### 1. Deleted dead modules
- **`expert_op4grid_recommender/pypowsybl_backend/observation_timers.py`** (1052 LOC).
  Stale fork of `observation.py`; repo-wide grep confirmed zero importers.
- **`expert_op4grid_recommender/utils/conversion_actions_repas_original.py`** (273 LOC).
  Pre-rewrite REPAS converter superseded by `conversion_actions_repas.py`; zero importers.

**Net impact**: -1325 LOC from the package.

#### 2. Removed hardcoded developer path
- **`expert_op4grid_recommender/utils/load_training_data.py`**: the `__main__` demo block
  previously used a literal path `/home/donnotben/Documents/assistflux/…`. Replaced with
  a read from the `EXPERT_OP4GRID_TRAINING_OBS_DIR` environment variable, raising a
  clear `RuntimeError` if unset. `os` was already imported, so no new imports were added.

  ```python
  training_obs_dir = os.environ.get("EXPERT_OP4GRID_TRAINING_OBS_DIR")
  if not training_obs_dir:
      raise RuntimeError(
          "EXPERT_OP4GRID_TRAINING_OBS_DIR is not set. "
          "Point it at a directory of gzipped training observations before running this script."
      )
  all_obs_files = list_all_obs_files(training_obs_dir, sort_results=True, ext=".gz")
  ```

#### 3. Removed duplicate definitions in `config.py`
The following keys were each defined twice in `expert_op4grid_recommender/config.py`
(silent last-wins behavior). Removed the second definition in every case:

- `RENEWABLE_CURTAILMENT_MARGIN`
- `RENEWABLE_CURTAILMENT_MIN_MW`
- `RENEWABLE_ENERGY_SOURCES`
- `PYPOWSYBL_FAST_MODE`

Verified post-cleanup with `grep`: each key now appears exactly once.

#### 4. Root-level file reorganization
Moved the following files out of the repository root using `git mv` (preserving history):

| From (root) | To |
|---|---|
| `verify_incremental_branching.py` | `scripts/verify_incremental_branching.py` |
| `SETUP_SUMMARY.md` | `docs/SETUP_SUMMARY.md` |
| `SKIP_VISUALIZATION_GUIDE.md` | `docs/SKIP_VISUALIZATION_GUIDE.md` |
| `pr_body_config.md` | `docs/pr_body_config.md` |

`MIGRATION_PLAN.md` was left at the repo root — it is still active while the
grid2op → pypowsybl migration is ongoing.

> **Update (documentation reorganization)**: the `docs/` tree was later
> reorganized into topical subfolders. `MIGRATION_PLAN.md` now lives at
> [`MIGRATION_PLAN.md`](MIGRATION_PLAN.md) (this file's sibling in `docs/archive/`),
> and the redundant test-config guides (`SETUP_SUMMARY.md`,
> `HOW_TO_SKIP_VISUALIZATION.md`, `DEBUG_CONFIG_OVERRIDE.md`) were consolidated
> into [`../../tests/README_CONFIG.md`](../../tests/README_CONFIG.md). See the
> documentation index at [`../README.md`](../README.md).

The moved script uses absolute package imports (`from expert_op4grid_recommender.pypowsybl_backend import …`), so it continues to work from its new location under an editable install (`pip install -e .`).

### Verification
- `python -c "import expert_op4grid_recommender.config as c"` imports cleanly and
  reports the expected values for `PYPOWSYBL_FAST_MODE`, `RENEWABLE_CURTAILMENT_MARGIN`,
  and `ENV_NAME`.
- Repo-wide grep confirms no remaining references to the deleted modules.

### P1 cleanup — completed

All three P1 items have been applied on branch `claude/refactor-discovery-module-BhhI0`.

#### 6. Split `action_evaluation/discovery.py` into per-family modules

The single 3001-LOC `discovery.py` file has been converted into a
`action_evaluation/discovery/` **package**. `ActionDiscoverer` is now
assembled from eight focused mixins composed on top of a shared base class
that owns the constructor, cached lookup state (observations, graph
references, node/edge caches, line capacity maps, blue-edge sets…) and the
generic helper methods used across families.

```
expert_op4grid_recommender/action_evaluation/discovery/
├── __init__.py                 # class ActionDiscoverer(OrchestratorMixin, ..., DiscovererBase)
├── _base.py                    # DiscovererBase — __init__, caches, shared helpers (25 methods)
├── _line_reconnection.py       # LineReconnectionMixin         (1 method)
├── _line_disconnection.py      # LineDisconnectionMixin        (2 methods)
├── _node_splitting.py          # NodeSplittingMixin            (8 methods)
├── _node_merging.py            # NodeMergingMixin              (2 methods)
├── _pst.py                     # PSTMixin                      (1 method)
├── _load_shedding.py           # LoadSheddingMixin             (1 method)
├── _renewable_curtailment.py   # RenewableCurtailmentMixin     (1 method)
└── _orchestrator.py            # OrchestratorMixin (discover_and_prioritize, 1 method)
```

Design notes:
- **Verbatim method bodies.** Each method was relocated unchanged (the split
  was scripted via AST extraction to guarantee behavior equivalence), so this
  is a pure reorganization — no logic edits, no renames, no signature changes.
- **Composition via mixin MRO.** The shared state lives on `DiscovererBase`;
  every family mixin accesses it through ``self.`` on the composed
  `ActionDiscoverer`. Cross-family helpers like
  `_compute_disconnection_flow_bounds`, `_build_line_capacity_map`,
  `_asymmetric_bell_score`, `_unconstrained_linear_score`,
  `_get_assets_on_bus_for_sub`, `_get_subs_impacted_from_action_desc` remain
  on the base so PST, load shedding, and renewable curtailment can reuse them
  without reaching into another family's module.
- **Public API preserved.** `from expert_op4grid_recommender.action_evaluation.discovery import ActionDiscoverer`
  still works; no call sites in `main.py`, `migration_guide.py`, or the test
  suite were touched. The package `__init__` also re-exports all eight mixin
  classes so callers who want family-level subsets (e.g. for narrow unit
  tests) can import them directly.
- **Count sanity check.** AST inspection confirms the new package defines
  exactly the same 42 methods as the original class (25 in `_base.py`, 17
  distributed across family mixins).

#### 7. Tests for `graph_analysis/*` and `environment_pypowsybl.py`

Added two new test modules covering the previously untested critical-path
code:

- **`tests/test_graph_analysis.py`** — exercises the pure helpers in
  `graph_analysis/`:
  - `builder.inhibit_swapped_flows`: verifies that flagged rows have their
    delta flows negated and origin/extremity indices swapped, while
    untouched rows are left intact.
  - `processor.get_n_connected_components_graph_with_overloads`: single-chain
    topology (overload removal splits the grid) and triangle topology
    (overload removal keeps it connected) regression cases.
  - `processor.identify_overload_lines_to_keep_overflow_graph_connected`:
    three regimes — keep-all (triangle), islanding → `None` (single bridge
    line), and empty-overload short-circuit.
  - `processor.get_subs_islanded_by_overload_disconnections`: both the
    integer-node and `subid_X_bus_Y` string-node code paths.
  - `visualization.get_graph_file_name`: DC vs AC suffix behavior and input
    propagation.

  All tests use `SimpleNamespace` or `MagicMock` observations, so no Grid2Op
  or alphaDeesp environment is required.

- **`tests/test_environment_pypowsybl.py`** — exercises
  `environment_pypowsybl.py` with the `SimulationEnvironment` dependency
  patched out:
  - `get_env_first_obs_pypowsybl`: four file-discovery regimes (direct
    `.xiidm` file, `<env>/grid.xiidm`, `<env>/grid/*.iidm` subdirectory,
    missing network → `FileNotFoundError`), thermal-limits auto-detection,
    DC-mode flag propagation to the network manager.
  - `set_thermal_limits_from_network`: threshold multiplication plus the
    `9999.0` sentinel fallback for lines missing a thermal limit.
  - Compatibility wrappers `get_env_first_obs` and
    `setup_environment_configs`: verified to delegate cleanly to their
    pypowsybl counterparts and drop the legacy `date` parameter.

#### 8. Narrowed `__init__.py` exception handling

`expert_op4grid_recommender/__init__.py:33-38` previously had
`except (ImportError, Exception) as e: try: print(...) except: pass`, which
collapsed to "swallow everything silently". Replaced with:

- Module-level `logging.getLogger(__name__)` (no more `print`-to-stderr).
- The `grid2op` import is isolated in its own `try/except ImportError:
  ...else:` block so a missing optional dependency no-ops cleanly.
- The actual monkey-patching is guarded by `except AttributeError` so
  unexpected API drift in future grid2op versions gets logged at debug
  level without masking genuine programmer errors.
- No more bare `except:` nest — the only exceptions caught are the two
  concrete types we expect (`ImportError`, `AttributeError`).

### P2 cleanup — completed

All four P2 items have been applied on branch
`claude/migrate-config-pydantic-4X3bX`.

#### 9. `config.py` / `config_basic.py` → `pydantic.BaseSettings`

`expert_op4grid_recommender/config.py` now defines a
`Settings(BaseSettings)` class (from `pydantic-settings`) that validates
every runtime knob (types, `ge=`/`gt=` bounds on the thermal-limit factor
and minimum-action counts, a `mode="before"` validator on `LINES_DEFAUT`
that accepts either a JSON list or a bare line name). The instance is
built once at import time and its fields are promoted to module-level
attributes through a small `apply_settings_to_namespace` helper, so every
existing `from expert_op4grid_recommender.config import DATE`,
`config.ENV_NAME`, and runtime mutation like
`config.ENV_NAME = env_name` keeps working unchanged.

Each knob can now be overridden from the environment via an
`EXPERT_OP4GRID_`-prefixed variable, for example:

```bash
EXPERT_OP4GRID_TIMESTEP=12 \
EXPERT_OP4GRID_LINES_DEFAUT='["BEON L31CPVAN","FOO"]' \
EXPERT_OP4GRID_PYPOWSYBL_FAST_MODE=true \
  python -m expert_op4grid_recommender.main
```

`expert_op4grid_recommender/config_basic.py` was rewritten to import the
shared `Settings` class and instantiate it with the basic-scenario
overrides (assistant environment, 5 prioritized actions, all `MIN_*`
counters zeroed), then publish the same module-level attribute layout as
`config.py`. The test override mechanism in `tests/conftest.py`
(`sys.modules['expert_op4grid_recommender.config'] = config_test`)
continues to work without modification because the substitute
`tests/config_test.py` is still a flat attribute module.

`pyproject.toml` gained `pydantic>=2.0` and `pydantic-settings>=2.0` as
core dependencies.

Verification:
- `python -c "from expert_op4grid_recommender import config; print(config.DATE, config.CASE_NAME)"` prints the expected values.
- `EXPERT_OP4GRID_TIMESTEP=42 EXPERT_OP4GRID_LINES_DEFAUT=FOO python -c "from expert_op4grid_recommender import config; print(config.CASE_NAME)"` → `defaut_FOO_t42`.
- `EXPERT_OP4GRID_LINES_DEFAUT='["A","B"]' python -c "from expert_op4grid_recommender import config; print(config.LINES_DEFAUT)"` → `['A', 'B']`.
- `Settings(MONITORING_FACTOR_THERMAL_LIMITS=-1.0)` raises `ValidationError` thanks to the `gt=0.0` bound.
- `pytest tests/test_config_override.py` (9 passed, 2 skipped — the remaining failure is unrelated, caused by a missing optional `pypowsybl` dependency in the test environment).

#### 10. Type hints and docstrings on `utils/` modules

Back-filled hints and docstrings on the older utils modules flagged in the
analysis (`load_training_data.py`, `load_evaluation_data.py`, `repas.py`,
`make_env_utils.py`, `make_assistant_env.py`, `make_training_env.py`).
Each module now has:

- A module-level docstring explaining what the module does.
- Class docstrings (`repas.Action`).
- Function signatures typed end-to-end (arguments + return annotations).
- One-line (or short-paragraph) docstrings on every public function,
  covering the contract and any mutation / thread-safety notes.

The changes are purely additive — no logic was touched — so no tests
needed updating. `python -m py_compile` is green on every modified file.

#### 11. TODO markers → GitHub issues

The 13 remaining TODO markers (one was already stale and was removed in
P0) were grouped into five issues and each in-code marker now references
the corresponding issue number instead of the bare `TODO` keyword:

| Issue | Title | Replaced markers |
|---|---|---|
| marota/expert_op4grid_recommender#79 | REPAS parser: handle unimplemented action types in `utils/repas.py` | `utils/repas.py` × 6 (`GeneratorModification` attrs, `GeneratorGroupVariation`, `LoadGroupVariation`, `LoadShedding`, `LoadSheddingElement`, `parse_json` violation-element filter) |
| marota/expert_op4grid_recommender#80 | Harden pypowsybl loader kwargs in `utils/make_env_utils.py` | `utils/make_env_utils.py` × 3 (`reconnect_disco_gen`, `reconnect_disco_load`, `gen_slack_id`) |
| marota/expert_op4grid_recommender#81 | Improve line-reconnection scoring heuristics (direction + extremity dispatch) | `action_evaluation/discovery/_line_reconnection.py` × 2 |
| marota/expert_op4grid_recommender#82 | Add regression cases for node-splitting scoring edge scenarios | `action_evaluation/discovery/_node_splitting.py` × 1 |
| marota/expert_op4grid_recommender#83 | Simplify overflow graph construction in `graph_analysis/processor.py` | `graph_analysis/processor.py` × 1 |

Verification: `grep -rn "TODO\|FIXME"` on the five touched files returns
no results.

#### 12. Dropped manual `sys.path` insertion in `main.py`

`expert_op4grid_recommender/main.py` no longer contains the
`project_root = os.path.abspath(...); sys.path.insert(0, project_root)`
block. The package is always consumed as an installed module (`pip install
-e .`) in every entry point (CLI, tests, notebooks), so the insertion was
redundant. `sys` is still imported (it's used a few lines later for
`sys.stderr` / `sys.exit` in `__main__`).

---

## Automation in CI/CD

The cleanup checklist above has been turned into a repeatable, automated
workflow so regressions on the findings in this document show up on
every pull request and every push to `main`.

### Moving parts

| Component | Location | Purpose |
|---|---|---|
| Quality-report aggregator | `scripts/code_quality_report.py` | Runs every static check, writes a markdown report, exposes `--strict` for regression gates and `--github-summary` for CI summaries. |
| GitHub Actions workflow | `.github/workflows/code-quality.yml` | Installs the tools, runs the aggregator, publishes the report to the job summary and as an artifact, then re-runs it in strict mode to fail the job on critical regressions. |
| Optional dependency group | `pyproject.toml` → `[project.optional-dependencies]` `quality` | `pip install -e .[quality]` installs the same tools locally for developers. |

The CI job does **not** install the package itself. The report is a pure
static-analysis pass so we avoid pulling in `grid2op` / `pypowsybl` and
keep the workflow fast and robust.

### What is checked

`scripts/code_quality_report.py` emits ten sections, mapped 1:1 to the
findings in this document:

1. **Module LOC inventory** — tracks the package-wide LOC and lists the
   ten fattest modules (originally §2 *God modules / complexity
   hotspots* and the appendix table).
2. **Cyclomatic complexity** — via `radon cc -a -j`. Reports the true
   average across every block, plus the top-15 hotspots (grade C and
   worse).
3. **Maintainability index** — via `radon mi`. Counts modules by grade
   and prints the ones below grade A.
4. **Dead-code suspects** — via `vulture --min-confidence 80`.
   Originally §1 *Dead / duplicate code*.
5. **Docstring coverage** — via `interrogate`. Originally §10 *Type
   hints & docstrings*.
6. **Lint findings** — via `ruff check --exit-zero --output-format
   concise`. Informational only.
7. **TODO / FIXME markers** — AST-free regex scan, split between bare
   markers and markers that reference a GitHub issue number. Mirrors
   §5 *TODO / FIXME inventory*; the P2 cleanup rewrote every bare
   marker to reference an issue so this counter should stay at zero.
8. **Hardcoded absolute paths** — regex scan for `"/home/<user>/..."`
   literals. Mirrors §6 *Hardcoded paths / magic numbers*; the P0
   cleanup removed the last offender.
9. **Duplicate config definitions** — parses every `config*.py` with
   Python's `ast` module and flags any module-level name assigned more
   than once. Mirrors §7 *Config sprawl* which listed four duplicate
   definitions silently winning last-assignment.
10. **Type-hint coverage** — custom AST walk that counts the functions
    whose every non-`self` argument *and* return type carry an
    annotation. Mirrors the §10 hand-rolled `~40%` estimate and gives
    us a trendable metric.

### Strict-mode regression gates

Three of the ten sections are wired as *blocking* gates. When the
`code-quality` workflow re-runs the aggregator with `--strict` the job
fails if any of these counters goes above zero:

- **Bare TODO / FIXME markers** (§5). Every todo must reference an
  issue; anything else is a regression on the P2 work.
- **Hardcoded `/home/<user>/…` literals** (§6). Would regress the P0
  removal of the developer-specific path in `load_training_data.py`.
- **Duplicate config definitions** (§7). Would regress the P0 de-dup
  of `RENEWABLE_*` / `PYPOWSYBL_FAST_MODE`.

The other seven sections are informational. They appear in the job
summary and in the `code-quality-report.md` artifact so the team can
watch trends, but they do not block PRs — dialing them up to blocking
requires another explicit decision once the baselines have stabilised.

### Running it locally

```bash
pip install -e .[quality]
python scripts/code_quality_report.py              # print the report
python scripts/code_quality_report.py -o report.md # write to a file
python scripts/code_quality_report.py --strict     # emulate the CI gate
```
