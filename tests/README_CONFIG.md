# Test Configuration

The test suite runs against a dedicated configuration module
(`tests/config_test.py`) instead of the package configuration
(`expert_op4grid_recommender/config.py`), so tests use stable, reproducible
parameters and never touch production settings.

## How it works

`tests/conftest.py` installs the override **at import time** (before pytest
loads any test module): it places `tests/config_test.py` into
`sys.modules['expert_op4grid_recommender.config']`. From then on, any

```python
from expert_op4grid_recommender import config
from expert_op4grid_recommender.config import SOME_VARIABLE
```

resolves to the test config — no test code or imports need to change.

> Doing the swap at import time (rather than in a fixture) matters: several test
> modules import `expert_op4grid_recommender.main` at module level, which pulls
> in `config` immediately. A fixture would run too late and the package config
> would already be cached.

### Files

| File | Role |
|---|---|
| `config_test.py` | Configuration values used during tests |
| `conftest.py` | Installs the config override at import time |
| `test_config_override.py` | Verifies the override is active |

## Running and verifying

```bash
pytest                                   # normal run — override is automatic
pytest tests/test_config_override.py -v  # confirm the override is installed
```

`test_config_override.py` checks that `config.__file__` points at
`tests/config_test.py` and that the expected values (e.g.
`DO_VISUALIZATION = False`) are in effect.

## Modifying test configuration

Edit `tests/config_test.py`; every test picks up the change automatically.
Commonly adjusted values:

```python
DATE = datetime(2024, 12, 7)
TIMESTEP = 9
LINES_DEFAUT = ["CHALOL61CPVAN"]
DO_VISUALIZATION = False     # skip graph rendering in tests (see below)
DO_CONSOLIDATE_GRAPH = False
DO_SAVE_DATA_FOR_TEST = False
USE_DC_LOAD_FLOW = False
N_PRIORITIZED_ACTIONS = 5
```

## Skipping visualization in tests

Rendering overflow graphs is slow and produces stray artifact files, so the test
config sets `DO_VISUALIZATION = False` while the package default
(`expert_op4grid_recommender/config.py`) keeps `DO_VISUALIZATION = True`.
`run_analysis()` in `main.py` honors the flag:

```python
if config.DO_VISUALIZATION:
    with Timer("Visualization"):
        ...        # rendering
else:
    print("Skipping visualization (DO_VISUALIZATION=False)")
```

So tests skip rendering automatically, while running `main.py` directly still
produces visualizations. To force visualization on for a specific test, set
`DO_VISUALIZATION = True` in `config_test.py` (or within that test).

## Troubleshooting

| Symptom | Fix |
|---|---|
| No override applied / wrong config used | Ensure `tests/conftest.py` exists; delete `tests/__pycache__` and stale `.pyc`; run `pytest --cache-clear`. |
| Visualization still runs | Confirm `config_test.py` has `DO_VISUALIZATION = False` and that `main.py` keeps the `if config.DO_VISUALIZATION:` guard. |
| Override verified but values look wrong | Make sure no test module caches config values at import time — read `config.X` inside the test/function, not at module top. |

Inspect the override manually:

```python
from expert_op4grid_recommender import config
print(config.__file__)          # -> .../tests/config_test.py
print(config.DO_VISUALIZATION)  # -> False
```

## Notes

- The override is session-scoped and applies to the whole test run.
- The package config file is never modified — only import resolution changes.
