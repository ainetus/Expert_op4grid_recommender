# Bug: overflow-graph build "contaminates" a reused pypowsybl env (step2 → later step1 "vanishes" overloads)

**Severity:** correctness (silent) — affects any workflow that reuses one
`SimulationEnvironment` across **multiple** analyses (batch classifiers,
long-lived services, the Co-Study4Grid game backend serving several
contingencies from one loaded network).

**Status:** root-caused and **fixed in-place** (issue #6). The original
hypothesis below (a *deep, unrecoverable* pypowsybl network-global corruption)
turned out to be **wrong** — it was an artifact of a hidden confounder (a leaked
`_default_dc` flag that silently turned every "recovery" load flow back into DC).
The true cause is an ordinary state leak on the reused env, and it is fully
recoverable without any network reload. See "Root cause (corrected)".

## Symptom

Run a full analysis (`step1` → `step2_graph` → `step2_discovery`) for one
contingency on a pypowsybl env, then run `step1` for a **different** contingency
on the **same** env: the second `step1` no longer detects the overloads it
detects on a freshly built env — it returns an `AnalysisResult` short-circuit
("Overload breaks the grid apart") with an empty overload set. The analysis
silently reports *no actionable overload* where there is one.

The trigger is contingency-specific: it only fires after an analysis whose
overload-disconnection load flow **diverges** (`MAX_ITERATION_REACHED`), which
happens when disconnecting the kept overloads islands part of a stressed grid.
An "easy" contingency whose overload-disconnection stays solvable never trips it.

## Root cause (corrected)

The corruption is **not** produced by the divergent load flow, and it is **not**
below the variant layer. It is a plain state leak on the *shared* env:

1. In `run_analysis_step2_graph`, `check_simu_overloads` disconnects all kept
   overloads on top of the contingency and runs an AC load flow. On a stressed
   operating point that islands the grid and the solve **diverges**
   (`MAX_ITERATION_REACHED`). This diverging solve runs on a *transient cloned
   variant* and — verified by instrumentation — leaves the base variant
   untouched (still 35 NaN `v_mag` buses on the `hiver_pic_2021` snapshot).

2. Because the check reports non-convergence, step2 calls
   **`switch_to_dc_load_flow_pypowsybl`**, which escalates the analysis to DC by
   setting **`env.network_manager._default_dc = True`** on the *shared, reused*
   NetworkManager and then calling `env.reset()`. `reset()` re-solves the **base
   variant**, and because `_default_dc` is now `True` it runs a **DC** load flow
   — which computes no voltage magnitudes, so every bus `v_mag` becomes `NaN`
   (35 → 1701). That is the "de-energized base" that looked like deep corruption.

3. `switch_to_dc` **never resets `_default_dc`**, so it leaks past the end of the
   analysis. The next `step1` that reuses the env runs *all* its load flows in DC
   (`run_load_flow(dc=None)` → `use_dc = _default_dc = True`). A DC load flow
   does not populate branch currents (`i1/i2 = 0`), so `rho ≈ 0` on every line →
   no line reads as overloaded → the short-circuit fires with an empty set.

### Why the earlier investigation concluded "only a full reload recovers"

The earlier write-up reported that `reset_to_base()`, variant purge, re-cloning
`base` from `InitialState`, **and a cold AC load flow on `base`** all failed to
recover, and inferred deep pypowsybl-global corruption. The confounder was
`_default_dc`: with it still `True`, every "cold AC load flow" issued through
`NetworkManager.run_load_flow(...)` was silently a **DC** solve, so of course it
never re-energized the base. Clearing the flag first makes recovery trivial:

```python
nm._default_dc = False
nm.reset_to_base()
nm.run_load_flow(voltage_init_mode=DC_VALUES)   # a real AC solve
# base NaN v_mag: 1701 -> 35 again; the next step1 detects the overload.
```

No network reload is needed. The divergent AC solve leaves no residual
network-global damage — confirmed by instrumenting the base `v_mag` NaN count
after every load flow: it stays at 35 through the divergent solve and only jumps
to 1701 on the DC solve of `base` inside `switch_to_dc`.

## Minimal reproduction

```python
# env built with the default config (IGNORE_LINES_MONITORING=True,
# MONITORING_FACTOR_THERMAL_LIMITS=0.95); ctx0 = prebuilt_env_context
def probe(cid):
    out = run_analysis_step1(analysis_date=None, current_timestep=0,
        current_lines_defaut=[cid], backend=Backend.PYPOWSYBL,
        dict_action={}, prebuilt_env_context=ctx0)
    return "CONTEXT" if isinstance(out, AnalysisContext) else "no-context"

print(probe("AVALLL61VNOL"))                      # CONTEXT (overload detected)
out = run_analysis_step1(..., current_lines_defaut=["AVALLL61J.VIL"],
                         dict_action=full, prebuilt_env_context=ctx0)
run_analysis_step2_graph(out)                     # diverges -> switch_to_dc

# BEFORE the fix: _default_dc leaked True and base was DC-solved.
print(nm._default_dc)                             # True  (leaked)
nm.set_working_variant(nm.base_variant_id)
np.isnan(net.get_buses()["v_mag"].values).sum()   # 35 fresh -> 1701 after (DC solve)
print(probe("AVALLL61VNOL"))                      # no-context  <-- BUG (runs in DC)

# AFTER the fix: run_analysis_step1 restores the env on reuse.
print(probe("AVALLL61VNOL"))                      # CONTEXT again; nm._default_dc == False
```

## Fix (applied)

`switch_to_dc_load_flow_pypowsybl`'s DC escalation is *analysis-scoped* but was
written onto *shared, long-lived* env state. The fix records the escalation and
undoes it at the next analysis boundary, so a reused env always starts a new
analysis in the configured baseline mode with a valid (energized) base — exactly
like a freshly built env:

- `switch_to_dc_load_flow_pypowsybl` sets `env._dc_escalation_pending = True`
  alongside `_default_dc = True`.
- `SimulationEnvironment.reset_loadflow_mode_to_baseline(dc)` restores the LF
  mode flags (`_default_dc` / `_use_dc`) to the configured baseline
  (`config.USE_DC_LOAD_FLOW`), re-solves the base variant in that mode
  (`_ensure_valid_state`), and clears the escalation flag.
- `run_analysis_step1`'s reused-env (`prebuilt_env_context`) branch calls
  `reset_loadflow_mode_to_baseline(...)` when it finds `_dc_escalation_pending`,
  and otherwise keeps the previous plain `reset_to_base()` fast path.

This keeps the overflow-graph flow numbers **byte-identical on the non-divergent
path** (the restore only runs when a prior analysis actually escalated to DC),
costs a single baseline load flow instead of the ~4 s full env rebuild the
work­around used, and removes the need for that workaround entirely.

Regression test: `tests/test_env_reuse_dc_escalation.py` (built on the IEEE-9
network — no France-grid fixture or `pypowsybl2grid` needed).

## Historical workaround (no longer required)

The RTE7000 game-mode classifier rebuilt the env after every analysis that ran
`step2` (~4 s each). With the fix this is unnecessary — a reused env self-heals
at the start of the next `run_analysis_step1`:

```python
rec = process(cid, ctx0, dict_action)     # step1 (+ step2 for candidates)
# (previously) del env; env, ctx0, dict_action, net = build_env(lab)
```

## Alternative fix directions (considered, not needed)

For the record, the isolation / guard directions from the original write-up
would also work but are heavier than needed given the corrected root cause:

1. **Isolate the flow-change load flow** on a dedicated network object. Robust,
   but adds a second network to memory and only matters if the divergent solve
   actually corrupted shared state — which it does not.
2. **Guard the divergence** (skip the LF when disconnecting the overloads
   islands the grid). Reasonable defense-in-depth, but the divergence itself is
   harmless; the leak is the DC-mode flag, which the applied fix addresses
   directly.
