# Configuration Override Setup - Summary

> **Archived** — historical session summary kept for the record. The canonical,
> maintained documentation of the test config override (including how to skip
> visualization and troubleshooting) now lives in
> [`../../tests/README_CONFIG.md`](../../tests/README_CONFIG.md).

## What Was Done

I've set up your PyCharm Expert Recommender project so that tests automatically use `tests/config_test.py` instead of `expert_op4grid_recommender/config.py`.

## Files Created/Modified

### 1. `/tests/conftest.py` (NEW)
This is the key file that makes everything work. It contains a pytest fixture that:
- Runs automatically before any test session
- Replaces `expert_op4grid_recommender.config` in Python's module cache with `tests.config_test`
- Ensures all imports of the config module get the test version

### 2. `/tests/test_config_override.py` (NEW)
A simple verification test that confirms:
- The config override is working
- The test config is being used
- All expected config attributes are accessible

### 3. `/tests/README_CONFIG.md` (NEW)
Documentation explaining:
- How the override works
- Why it's beneficial
- How to use and modify it

## How It Works

When you run any test (e.g., `test_reproducibility`), the conftest.py automatically runs first and modifies Python's import system so that:

```python
# In your test or in any code it calls:
from expert_op4grid_recommender import config

# This now gives you the contents of tests/config_test.py
# instead of expert_op4grid_recommender/config.py
```

This happens transparently - you don't need to change any existing code!

## Testing It

1. **Verify the override works:**
   ```bash
   pytest tests/test_config_override.py -v
   ```

2. **Run your reproducibility test:**
   ```bash
   pytest tests/test_expert_op4grid_analyzer.py::test_reproducibility -v
   ```

Both should now use the test configuration automatically.

## Benefits

✅ **No code changes needed** - Your existing tests work as-is
✅ **Consistent configuration** - All tests use the same test config  
✅ **Easy to modify** - Just edit `tests/config_test.py` to change test parameters
✅ **Isolated from production** - Package config remains untouched
✅ **Automatic** - No manual setup or flags required

## How to Modify Test Configuration

Simply edit `tests/config_test.py` and change any values you need for testing. For example:

```python
# In tests/config_test.py
DATE = datetime(2024, 12, 7)
TIMESTEP = 9
LINES_DEFAUT = ["CHALOL61CPVAN"]
USE_DC_LOAD_FLOW = False
DO_CONSOLIDATE_GRAPH = False
# ... etc
```

All tests will automatically use these values!

## Important Note

The test config now has `DO_VISUALIZATION = False` to skip graph visualization during tests for faster execution. You can customize any other test config parameters independently as needed for different test scenarios.

## Skipping Visualization in Tests

By default, visualization is now **automatically skipped** when running tests:

- `tests/config_test.py` has `DO_VISUALIZATION = False`
- `expert_op4grid_recommender/config.py` has `DO_VISUALIZATION = True`

This means:
- Tests run faster (no time spent creating graph files)
- No test artifacts created
- Main script still creates visualizations normally

See `tests/HOW_TO_SKIP_VISUALIZATION.md` for more details.
