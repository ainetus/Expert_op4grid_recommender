# Superposition Module Documentation

> **File**: `expert_op4grid_recommender/utils/superposition.py`
> **Origin**: Adapted from [Topology_Superposition_Theorem](https://github.com/marota/Topology_Superposition_Theorem)
> **Purpose**: Infer the combined effect of two remedial actions on a contingency state without running a full load flow simulation for every pair.

---

## 1. Core Concept

The superposition theorem exploits the (approximate) linearity of power flows with respect to voltage angles in the DC approximation. Given a base contingency state and two individual remedial actions whose effects have been simulated independently, the module estimates what would happen if **both** actions were applied simultaneously.

### Key Formula

```
p_or_combined = (1 - sum(betas)) * obs_start.p_or + sum(betas[i] * obs_unit[i].p_or)
```

Where:
- **`obs_start`**: The N-1 observation (post-contingency, before any remedial action)
- **`obs_unit[i]`**: The observation after applying remedial action *i* alone
- **`betas`**: Coupling coefficients solved from a 2x2 linear system

The same formula applies to `p_ex` (active power at extremity side), `theta_or`, `theta_ex`, and in the approximate mode, directly to `rho`.

---

## 2. Architecture

```
superposition.py
├── Low-level helpers
│   ├── _get_theta_node()          # Median voltage angle at a substation bus
│   ├── get_delta_theta_line()     # theta_or - theta_ex for a line (works for disconnected lines)
│   ├── get_delta_theta_sub_2nodes()  # theta_bus2 - theta_bus1 at a split substation
│   ├── get_sub_node1_idsflow()    # Identify elements on bus 1 of a split substation
│   ├── get_virtual_line_flow()    # KCL-based flow between buses of a split substation
│   └── _is_sub_reference_topology()  # Check if substation is single-bus
│
├── Beta computation
│   └── get_betas_coeff()          # Solve A * betas = [1, 1] for coupling coefficients
│
├── Action element identification
│   └── _identify_action_elements()  # Map action ID -> (line_indices, sub_indices)
│
├── Rho estimation
│   ├── _estimate_rho_from_p()           # Rho from superposed P (for disconnection/splitting)
│   ├── _estimate_rho_from_delta_theta() # Rho from superposed delta_theta (for reconnection/merging)
│   └── _compute_delta_theta_all_lines() # Vectorized delta_theta computation
│
├── Pair computation
│   └── compute_combined_pair_superposition()  # Core: betas + superposed p_or/p_ex for one pair
│
└── Main entry point
    └── compute_all_pairs_superposition()  # Iterate over all C(n,2) pairs of converged actions
```

---

## 3. Beta Coefficient Computation

### Linear System

The betas satisfy `A * betas = [1, 1]` where:

```
A[j][i] = 1 - (feature_unit_act[i][j] / feature_start[j])   for i != j
A[j][j] = 1.0
```

The **feature** at element *j* can be either:
- **`p_or`** (active power): used when `|p_or_start[j]| > 1.0 MW` (physically meaningful flow)
- **`delta_theta`** (voltage angle difference): used as fallback when flow is too small (`< 1.0 MW`) but `|delta_theta| > 1e-6 rad`

### Physical Interpretation

Each beta represents how much of a unit action's effect is "active" in the combined state. For two independent (uncoupled) actions, `betas = [1, 1]` (both fully active). When actions interact (e.g., both affect the same line), betas deviate from 1 to account for the coupling.

### Validity Checks

- **Singular matrix**: Falls back to `betas = [1, 1]` with a warning
- **NaN betas**: Returns an error
- **Out-of-range betas**: `[-2.0, 3.0]` is the accepted range. Outside this, the linear approximation is too inaccurate; the pair is skipped with an error

---

## 4. Action Element Identification

Each action must be mapped to its **characteristic element** — the single line or substation whose physical quantity (p_or or delta_theta) changes most due to the action.

### Element Types by Action Type

| Action Type | Element Type | Feature Used in Beta System |
|---|---|---|
| `open_line` (disconnection) | Line index | `p_or` at that line |
| `close_line` (reconnection) | Line index | `delta_theta` at that line |
| `open_coupling` (node splitting) | Substation index | Virtual line flow (KCL) or `delta_theta` between buses |
| `close_coupling` (node merging) | Substation index | `delta_theta` between buses or virtual line flow |
| `pst` (phase shifter tap) | Line index (the PST branch) | `p_or` at that branch |

### Substation Elements: The "Virtual Line" Concept

For topology actions at substations (splitting/merging), the superposition theorem treats the connection between the two buses as a **virtual line**:
- **Split substation (2 buses)**: The virtual line carries flow computed by KCL at bus 1. Its delta_theta is `theta_bus2 - theta_bus1`.
- **Reference topology (1 bus)**: The virtual line has zero flow and zero delta_theta (no separation between buses).

The feature used depends on the **starting state**:
- If the substation **starts merged** (reference topology) and the action **splits** it → the action creates a non-zero delta_theta → use **virtual flow** in obs_start and **delta_theta** in obs_act
- If the substation **starts split** and the action **merges** it → the action collapses delta_theta to zero → use **delta_theta** in obs_start and **virtual flow** in obs_act

---

## 5. No-Op Detection

Before computing betas, the module checks whether each action actually changes the grid state:

| Action Type | No-Op Condition |
|---|---|
| Line action | Line status unchanged between `obs_start` and `obs_act` |
| Substation action | `delta_theta` relative change < 1% AND absolute change < 1e-6 rad |

No-op actions produce a degenerate A-matrix and are skipped with an error.

---

## 6. Rho Estimation Methods

After computing superposed `p_or_combined` and `p_ex_combined`, the module needs to convert these back to **rho** (loading ratio = current / thermal limit). Two methods are available:

### 6.1 Direct Rho Superposition (default, `use_p_based_rho=False`)

```python
rho_combined = abs((1 - sum(betas)) * obs_start.rho + betas[0] * obs_act1.rho + betas[1] * obs_act2.rho)
```

Simple and fast but **biased** because:
- `rho = max(rho_or, rho_ex)` and `max()` is convex (not linear)
- Reactive power doesn't superpose linearly

### 6.2 P-Based Rho Estimation (`use_p_based_rho=True`)

Computes per-extremity conversion factors:
```
factor_or = rho_or_start / |P_or_start|
rho_or_estimated = |P_or_combined| * factor_or
```

Then `rho = max(rho_or, rho_ex)` if `MAX_RHO_BOTH_EXTREMITIES=True`.

**Fallback chain** for lines with `|P_start| < 0.1 MW` (disconnected or lightly loaded):
1. Try `obs_act1` to get a usable factor
2. Try `obs_act2` to get a usable factor
3. If both fail, set `rho = 0` (negligible flow)

### 6.3 Delta-Theta-Based Rho Estimation

Used when either action involves **reconnection** or **node merging** (where the line transitions from disconnected to connected, making P-based factors unreliable in obs_start):

```
factor = rho_start / |delta_theta_start|
rho_estimated = |delta_theta_combined| * factor
```

Same fallback chain using action observations.

### Routing Logic

When `use_p_based_rho=True`, the method is chosen automatically:
- **Reconnection** (line disconnected in obs_start) → delta-theta method
- **Node merging** (substation split in obs_start) → delta-theta method
- **Disconnection** (line connected in obs_start) → P-based method
- **Node splitting** (substation merged in obs_start) → P-based method

---

## 7. Main Entry Point: `compute_all_pairs_superposition()`

### Inputs

| Parameter | Description |
|---|---|
| `obs_start` | N-1 observation (post-contingency state) |
| `detailed_actions` | Dict of `{action_id: {action, observation, description_unitaire, non_convergence, ...}}` |
| `classifier` | `ActionClassifier` instance for determining action types |
| `env` | Environment with `name_line`, `name_sub`, `action_space` |
| `lines_overloaded_ids` | Indices of initially overloaded lines |
| `lines_we_care_about` | Line names subject to monitoring |
| `pre_existing_rho` | Dict of `{line_idx: rho_value}` for pre-existing overloads |
| `dict_action` | Full action dictionary for classification |

### Processing Steps

1. **Filter** to converged actions only (where `non_convergence is None`)
2. **Pre-compute element indices** for each action via `_identify_action_elements()`
3. **Iterate** over all C(n,2) pairs
4. For each pair:
   a. Call `compute_combined_pair_superposition()` to get betas + superposed p_or/p_ex
   b. Estimate `rho_combined` using the appropriate method
   c. Compute `max_rho` among monitored lines (excluding pre-existing overloads unless worsened)
   d. Determine `is_rho_reduction` (whether all overloaded lines improve)
   e. Detect islanding (increased connected components)

### Output

```python
{
    "action1_id+action2_id": {
        "betas": [float, float],
        "p_or_combined": [float, ...],       # per line
        "p_ex_combined": [float, ...],       # per line
        "max_rho": float,                    # worst loading among monitored lines
        "max_rho_line": str,                 # name of worst-loaded line
        "is_rho_reduction": bool,            # True if all overloaded lines improve
        "description": str,                  # "desc1 + desc2"
        "action1_id": str,
        "action2_id": str,
        "is_islanded": bool,
        "disconnected_mw": float,
        "rho_after": [float, ...],           # rho on overloaded lines after combination
        "rho_before": [float, ...],          # rho on overloaded lines before (obs_start)
    },
    ...
}
```

---

## 8. Current PST Support in Superposition

PST actions are **already partially supported** in `_identify_action_elements()`:
- PST actions are identified by action type `"pst"` or ID prefixes `"pst_tap_"` / `"pst_"`
- The affected **line index** (the PST branch) is extracted from `pst_tap` dict or action ID
- PST elements are treated as **line elements** in the beta system (using `p_or` at the PST branch)

However, there is a gap: the **no-op detection** currently only checks line status changes for line-type elements. For PST actions, the line status doesn't change — only the flow magnitude changes due to the tap change. This means PST actions pass through the line-based no-op check (status unchanged → flagged as no-op) unless special handling is added.

---

## 9. Analogy: Line Disconnection vs. Phase Shifter Tap Change

Both action types **redistribute existing flow** on other lines via the superposition principle:

| Aspect | Line Disconnection | PST Tap Change |
|---|---|---|
| **Mechanism** | Removes line entirely; full original flow redistributes | Changes impedance via tap; flow difference redistributes |
| **Flow redistributed** | Full `p_or` of the disconnected line | `delta_p_or = p_or_after_tap - p_or_before_tap` |
| **Element in beta system** | The disconnected line (p_or) | The PST branch (p_or) |
| **obs_start feature** | `p_or` on the line (non-zero, line is connected) | `p_or` on the PST branch (non-zero, branch is connected) |
| **obs_act feature** | `p_or = 0` (line disconnected) | `p_or = new_value` (different from obs_start) |
| **Rho estimation** | P-based (line connected in obs_start) | P-based (PST branch connected in obs_start) |
| **No-op detection** | Line status change check | Flow change check needed (status doesn't change) |

The key difference is that a disconnection sets `p_or → 0` while a PST tap change sets `p_or → p_or + delta`. Both are captured correctly by the beta system since it uses the actual feature values from `obs_start` and `obs_act`.

---

## 10. Generalized Superposition Theorem (GST): injection-aware pairs

The Extended Superposition Theorem (EST) above combines two **topology** actions.
The **GST** extends it to pairs where at least one action is an **injection**
change — load shedding (`set_load_p`), renewable curtailment / redispatch
(`set_gen_p`). These change only nodal injections, not the topology.

### Detection

`is_injection_action(action_id, action_desc, classifier)` flags an action as an
injection change by id prefix (`load_shedding_` / `curtail_` / `redispatch_`) or
classifier type (`load_power_reduction` / `gen_power_reduction` / `gen_redispatch`
/ `open_load` / `open_gen`). `compute_combined_pair_superposition` routes a pair
to `compute_combined_pair_gst` whenever either `act*_is_injection` is set, and
`compute_all_pairs_superposition` now **keeps** injection actions (previously
skipped) and pairs them with topology actions and with each other.

### Formula (adapted from Topology_Superposition_Theorem)

An injection action enters in **pure superposition** — its flow response
`obs_inj.p_or − obs_start.p_or` is added in full. A topology action keeps an EST
beta that the injection shifts **only through the right-hand side** of the
(unchanged) 1×1 EST system:

```
beta_T = x(P_tgt, T_ref) / x(P_ref, T_ref)
       = x(obs_inj) / x(obs_start)        # single injection on the switched asset
```

with `x = p_or` for disconnection / node split (non-zero reference flow) and
`x = delta_theta` for reconnection / node merge (zero reference flow).

### The `beta = 1.0` convention (why nothing downstream changes)

`compute_combined_pair_gst` reports an injection action with **`beta = 1.0`** and
a topology action with its solved `beta_T`. Because each injection term
`(obs_inj − obs_start)` equals `1.0·obs_inj − 1.0·obs_start`, the `−obs_start`
parts fold into the `(1 − sum(betas))` weight, so the **standard EST
reconstruction** reproduces the exact GST flows:

```
p_combined = (1 − sum(betas)) · obs_start.p + betas[0]·p_act1 + betas[1]·p_act2
```

| Pair shape | betas | reconstruction |
|---|---|---|
| topology + injection | `[beta_T, 1.0]` | `−beta_T·start + beta_T·topo + inj` |
| injection + injection | `[1.0, 1.0]` | `inj1 + inj2 − start` (DC-exact) |

This identity means the rho estimators (`_estimate_rho_from_p`, the default
direct rho superposition) **and Co-Study4Grid's `compute_combined_rho`** need no
GST-specific code path.

### AC anchoring (which state values feed the GST)

The GST is **AC-anchored**: every quantity it reads comes from the *AC* load-flow
observations, not from a recomputed DC model.

- The **injection superposition term** is `obs_inj.p_or − obs_start.p_or` (and the
  same for `p_ex`), with both observations being AC states.
- The **injection-shifted beta** is `beta_T = x(obs_inj) / x(obs_start)`, where `x`
  is the AC `p_or` (disconnection / node split) or the AC `delta_theta` from
  `theta_or − theta_ex` (reconnection / node merge), read on the switched element.

So the estimate is anchored at the true AC operating point. **But using AC inputs
does not make it AC-exact** — the superposition *law* it applies
(`PF = α·ref + Σβ·unit + Σ injection`, betas from a linear ratio system) is exact
only under **DC linearity**. Feeding AC values into a DC-derived linear law yields
an *AC-anchored linear superposition*: better than a pure DC estimate, but it
cannot reproduce AC nonlinearities (reactive / voltage coupling, loss
redistribution, operating-point-dependent flow splits). There is also a second,
AC-independent approximation: the injection response is captured at the
**reference topology** and added in full, while in the combined target the
topology has also changed — the topology betas absorb that coupling through the
right-hand side (first-order-exact in DC, approximate in AC). Re-evaluating the
injection in the combined topology would need an extra simulation, defeating the
estimate's purpose.

### Accuracy

- **DC: exact.** Validated against ground-truth combined simulations (grid2op DC,
  `l2rpn_case14_sandbox`) to ~1e-6 MW for line disco/reco, node split/merge, and
  injection+injection pairs (`test_superposition_gst.py`). A direct pypowsybl
  `run_dc` check on the small grid reconstructs the flagged
  `disco_BEON L31CPVAN + load_shedding_P.SAO3TR312` pair to **0.0000 MW** on every
  line (`Co-Study4Grid/scripts/gst_estimation_vs_simulation_small_grid.py`).
- **AC topology + injection: same accuracy as topology-only EST.** On the small
  AC grid, a load-shedding (GST) pair and a pure-topology (EST) pair share the
  same per-line rho error (~1–2 pts mean, occasionally ~15 pts). The injection
  term (β = 1.0) adds nothing on top of the inherent EST/AC limit.
- **Where the visible gap comes from.** A few-MW AC flow-split error is negligible
  on heavily-loaded high-limit lines but, on **low-flow lines of a meshed parallel
  corridor** (where disconnecting a loaded line dumps its flow), it becomes a
  sizable rho-% error. When two such lines sit at near-equal loading, it **flips
  which is reported as the global max** (e.g. estimate picks `C.FOUL31MERVA`,
  simulation picks `PYMONL31SAISS`, both ~70–75 %). The decision-relevant number —
  the effect on the operator's actual overload — is predicted correctly (that is
  what `target_max_rho` surfaces). This is *not* GST-specific: pure-topology EST
  pairs show the identical effect.
- **AC injection + injection: weaker.** Two large injection changes compound the
  AC nonlinearity, so the combined relief is over-predicted (DC-exact, but the AC
  gap is larger — mean ~4 pts, and the on-target line can be mispredicted). Treat
  these estimates as lower-confidence and prefer a full simulation.

### Known larger-error cases

All errors below are **DC-exact** (verified to 0 MW by a direct `run_dc` check)
and are therefore **AC-nonlinearity, not bugs**. They are catalogued so the
estimate can be read correctly. Reproduce with
`Co-Study4Grid/scripts/gst_estimation_vs_simulation_small_grid.py` (small grid,
contingency `P.SAOL31RONCI`).

| # | Trigger | Symptom | Measured (small grid) | Root cause | How to read it |
|---|---|---|---|---|---|
| 1 | Disconnecting a **heavily-loaded** line that dumps its flow onto a **low-flow meshed parallel corridor** | global `max_rho` **line flips** between two near-equally-loaded lines; few-% gap on the max | `disco_BEON L31CPVAN + load_shedding_P.SAO3TR312`: est **74.5 % on C.FOUL31MERVA** vs sim **70.0 % on PYMONL31SAISS**. Per-line: C.FOUL over-estimated ~15 pts, PYMON/FRON5 under-estimated. mean rho gap ~1.6 pts, max ~15 pts | BEON↔C.FOUL are tightly coupled (DC LODF = 1.0); the AC split of BEON's ~25 MW across the low-flow corridor (lines at ~7–25 MW) is operating-point dependent | Trust `target_max_rho` (on-target overload `BEON` is correctly predicted resolved, ~0.4 %); treat the off-target global max as indicative. **Not GST-specific** — pure-topology EST pairs (`reco + disco`) show the identical ~+4.7 MW C.FOUL error |
| 2 | **Injection + injection** (two large `load_shedding` / `curtail` together), especially when both target the same constrained area | combined relief **over-predicted**; the on-target line itself can be mispredicted | `load_shedding_P.SAO3TR312 + load_shedding_BEON3 TR311`: est `BEON` **5.6 %** vs sim **38.1 %**. mean rho gap ~4.1 pts, max ~32.8 pts; flow gap up to ~18 MW | Two large injections each set a load/gen to 0; the AC responses compound (voltage/reactive), and pure superposition (β = 1.0 each) misses the sub-additivity of stacked injections | **Lower-confidence — prefer a full simulation.** A `low_confidence` flag for injection+injection pairs is a reasonable UI follow-up |

The shared driver of #1 is the **disconnection redistribution onto low-flow
lines**, not the injection: a few-MW AC error is invisible on a 400 MW line but
is ~15 rho-points on a 30 MW line, which is exactly what reorders two ~75 %
neighbours. #2 is specific to stacking two large injection changes.

---

## 11. Test Coverage

| Test File | Focus |
|---|---|
| `test_superposition.py` | Basic pair computation with mocks |
| `test_superposition_action_types.py` | All action type pair combinations (line+line, sub+sub, line+sub), reversed ordering, helper functions, no-op detection |
| `test_superposition_rho_estimation.py` | P-based and delta-theta-based rho estimation, fallbacks, action-type-aware routing |
| `test_superposition_extended.py` | Edge cases: no elements, multiple elements, singular systems, beta range validation |
| `test_superposition_gst.py` | GST: topology+injection and injection+injection pairs vs ground truth (DC-exact), AC-anchoring (`TestGstIsAcAnchored`: beta RHS + reconstruction use the AC observation values), `is_injection_action` detection, `compute_all_pairs` inclusion of injection pairs |
