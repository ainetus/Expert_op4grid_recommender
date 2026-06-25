# Migration Plan: Grid2Op to Pure pypowsybl

## Executive Summary

This document outlines the migration strategy to remove the grid2op dependency from `expert_op4grid_recommender` and rely solely on pypowsybl for power system simulation.

> **Related docs**: [`../architecture/simulation-pipeline.md`](../architecture/simulation-pipeline.md)
> (the pypowsybl simulation pipeline this migration targets) and
> [`../../expert_op4grid_recommender/pypowsybl_backend/README.md`](../../expert_op4grid_recommender/pypowsybl_backend/README.md)
> (backend quick-start + API). Documentation index: [`../README.md`](../README.md).

## New Module Structure

```
expert_op4grid_recommender/
├── pypowsybl_backend/           # NEW: Pure pypowsybl implementation
│   ├── __init__.py              # Public API exports
│   ├── network_manager.py       # Network loading, variants, load flow
│   ├── observation.py           # Grid2op-compatible observation interface
│   ├── action_space.py          # Action creation (line switching, topology)
│   ├── simulation_env.py        # Main environment class
│   ├── topology.py              # Topology vector management
│   ├── overflow_analysis.py     # Overflow graph building (replaces alphaDeesp parts)
│   └── migration_guide.py       # Documentation and examples
├── environment_pypowsybl.py     # NEW: Migrated environment setup
├── utils/
│   ├── simulation_pypowsybl.py  # NEW: Migrated simulation utilities
│   └── ...
└── ...
```

## Key Changes

### 1. Environment Creation

**Before (grid2op):**
```python
from pypowsybl2grid import PyPowSyBlBackend
import grid2op

backend = PyPowSyBlBackend(n_busbar_per_sub=12, ...)
env = grid2op.make(path, backend=backend, ...)
obs = env.reset()
```

**After (pure pypowsybl):**
```python
from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment

env = SimulationEnvironment(
    network_path="/path/to/network.xiidm",
    thermal_limits_path="/path/to/thermal_limits.json"
)
obs = env.get_obs()
```

### 2. Actions (No Change in Interface!)

```python
# SAME SYNTAX - works with both backends
action = env.action_space({"set_line_status": [("LINE_NAME", -1)]})
action = env.action_space({
    "set_bus": {
        "lines_or_id": {"LINE_NAME": 1},
        "lines_ex_id": {"LINE_NAME": 2}
    }
})

# Action combination still works
combined = action1 + action2
```

### 3. Simulation (No Change in Interface!)

```python
# SAME SYNTAX
obs_simu, reward, done, info = obs.simulate(action, time_step=timestep)

if info["exception"]:
    print("Simulation failed")
    
# Access results same way
print(f"Line loadings: {obs_simu.rho}")
print(f"Line status: {obs_simu.line_status}")
```

### 4. Overflow Graph Building

**Before:**
```python
from alphaDeesp.core.grid2op.Grid2opSimulation import Grid2opSimulation

overflow_sim = Grid2opSimulation(obs, env.action_space, ...)
```

**After:**
```python
from expert_op4grid_recommender.pypowsybl_backend import (
    OverflowSimulator, 
    build_overflow_graph_pypowsybl
)

# Option 1: Direct use
overflow_sim = OverflowSimulator(env.network_manager, obs, use_dc=False)

# Option 2: Drop-in replacement function
df_of_g, overflow_sim, g_overflow, hubs, g_dist, mapping = build_overflow_graph_pypowsybl(
    env, obs, overloaded_line_ids, ...
)
```

## Migration Steps

### Phase 1: Install New Backend (Immediate)

1. The `pypowsybl_backend/` module is already created
2. No changes to existing code required
3. Can use new backend alongside grid2op

### Phase 2: Update Imports (Gradual)

Replace imports file by file:

```python
# Old
from expert_op4grid_recommender.environment import get_env_first_obs

# New
from expert_op4grid_recommender.environment_pypowsybl import get_env_first_obs
```

### Phase 3: Update pyproject.toml (Final)

Remove dependencies:
```toml
dependencies = [
    "numpy",
    "pandas", 
    "networkx",
    "pypowsybl",
    # REMOVED: "grid2op",
    # REMOVED: "pypowsybl2grid",
    # REMOVED: "lightsim2grid",
    "expertop4grid>=0.2.8",  # May need adaptation
]
```

## Compatibility Matrix

| Feature | Grid2Op | pypowsybl Backend | Notes |
|---------|---------|-------------------|-------|
| Line disconnection | ✅ | ✅ | Same interface |
| Line reconnection | ✅ | ✅ | Same interface |
| Topology changes | ✅ | ✅ | Same interface |
| AC Load Flow | ✅ | ✅ | Uses pypowsybl OpenLoadFlow |
| DC Load Flow | ✅ | ✅ | `use_dc=True` parameter |
| Simulate method | ✅ | ✅ | Uses network variants |
| Rho (line loading) | ✅ | ✅ | Computed from I/I_max |
| Voltage angles | ✅ | ✅ | From bus results |
| Thermal limits | ✅ | ✅ | From operational limits |
| Chronics (time series) | ✅ | ⚠️ | Static analysis only |
| Observation history | ✅ | ❌ | Not implemented |

## Testing Strategy

1. **Unit Tests**: `tests/test_pypowsybl_backend.py`
   - Test each component independently
   - Verify interface compatibility

2. **Comparison Tests**: 
   - Run same scenarios with both backends
   - Compare `obs.rho` values (should be close)
   - Compare simulation convergence

3. **Integration Tests**:
   - Run full analysis pipeline
   - Verify action discovery works
   - Check overflow graph construction

## Known Limitations

1. **No Chronics Support**: The pypowsybl backend is designed for static analysis. Time-series data handling would need separate implementation.

2. **Simplified Topology**: The bus/breaker model in pypowsybl may differ slightly from grid2op's representation. Complex multi-bus topologies may need verification.

3. **alphaDeesp Integration**: Some alphaDeesp functions expect grid2op objects. The `AlphaDeespAdapter` class provides compatibility, but full integration may need alphaDeesp modifications.

## Performance Considerations

- **Variant-based simulation**: pypowsybl's variant system is efficient for what-if analysis
- **No Python overhead**: pypowsybl calls native C++ code directly
- **Batch operations**: Can process multiple scenarios using variant cloning

## Files Created

1. `expert_op4grid_recommender/pypowsybl_backend/__init__.py`
2. `expert_op4grid_recommender/pypowsybl_backend/network_manager.py`
3. `expert_op4grid_recommender/pypowsybl_backend/observation.py`
4. `expert_op4grid_recommender/pypowsybl_backend/action_space.py`
5. `expert_op4grid_recommender/pypowsybl_backend/simulation_env.py`
6. `expert_op4grid_recommender/pypowsybl_backend/topology.py`
7. `expert_op4grid_recommender/pypowsybl_backend/overflow_analysis.py`
8. `expert_op4grid_recommender/pypowsybl_backend/migration_guide.py`
9. `expert_op4grid_recommender/environment_pypowsybl.py`
10. `expert_op4grid_recommender/utils/simulation_pypowsybl.py`
11. `tests/test_pypowsybl_backend.py`

## Next Steps

1. **Test with your actual network**: Load your XIIDM file and verify results
2. **Compare with grid2op**: Run same contingencies and compare
3. **Adapt alphaDeesp**: Modify or fork alphaDeesp for direct pypowsybl support
4. **Remove grid2op**: Once verified, update dependencies
