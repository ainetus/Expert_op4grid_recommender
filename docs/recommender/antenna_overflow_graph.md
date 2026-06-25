# Design: Antenna (islanded-pocket) overflow graph

> **Version**: 0.2.4+ (unreleased)
> **Author**: RTE / Expert Op4Grid Recommender
> **Modules**: `graph_analysis/antenna_graph.py`, `graph_analysis/processor.py`,
> `action_evaluation/discovery/_orchestrator.py`, `main.py`

---

## 1. Problem

The normal overflow-graph pipeline (`build_overflow_graph` /
`build_overflow_graph_pypowsybl`) simulates **disconnecting the overloaded
line** and reads how power *redistributes* across the rest of the grid. That
redistribution is what colours the edges and feeds action discovery.

When a contingency leaves a **radial pocket** of substations fed by a single
overloaded line, disconnecting that line **breaks the grid apart** — the pocket
islands. The load flow on the cut grid then diverges (the pocket has no slack
and simply blacks out), so there is no meaningful redistribution to read. This
is detected upstream in step 1:

```
identify_overload_lines_to_keep_overflow_graph_connected(...) -> ids_kept is empty
extract_antenna_context(obs, lines_overloaded_ids)            -> pocket description
```

`extract_antenna_context` returns the pocket: the **constraint line**, its
**root** substation (on the main grid) and its **entry** substation (inside the
pocket), plus the full list of pocket substation ids. When it finds a pocket the
analysis switches to **antenna mode** (`context["antenna_mode"] = True`).

There are **two** physical sub-cases, and both must be handled:

| Sub-case | Pocket net | Power flow on the constraint | Pocket side |
|----------|-----------|------------------------------|-------------|
| **Consumer pocket** | load > prod | main grid → pocket | downstream (**aval**) |
| **Producer pocket** | prod > load | pocket → main grid | upstream (**amont**) |

A producer pocket feeds the rest of the grid *up* through the overload (the case
an RTE operator flagged: "flow comes from G.ROUP6 up to SOULLP6").

---

## 2. Principle: reuse the standard pipeline with a synthetic "after" state

Rather than hand-build a graph, we feed the **standard ExpertOp4Grid machinery**
the post-disconnection state implied by the islanding:

> **new_flows = initial post-contingency flows, with every line incident to the
> islanded pocket set to 0.**

Modelling the pocket as simply blacking out (no internal redistribution; the
healthy grid untouched) gives, per line:

```
delta_flows = new_flows - init_flows
            = 0                         on the healthy grid   (gray, trimmed)
            = -init_flows (signed)      on the pocket lines   (blue / coral)
```

From these explicit `new_flows` we compute **the exact same per-line
`delta_flows` frame** as `alphaDeesp.Simulation.create_df` — branch-direction
swap, `new_flows_swapped` handling, the gray-edge threshold — and then build the
graph with the genuine `OverFlowGraph` + `Structured_Overload_Distribution_Graph`,
identical to the standard builders minus the (diverging) load flow.

Because the frame carries the **real signed flows**, alphaDeesp decides edge
colour, orientation and the amont/aval split itself — so consumer and producer
pockets both come out with physical directions, no looping, no inversion.

```
   Consumer pocket (aval):    [root] ──black──▶ [entry] ──blue──▶ [pocket loads]
   Producer pocket (amont):   [pocket gens] ──blue──▶ [entry] ──black──▶ [root]
```

---

## 3. Implementation

### 3.1 `build_antenna_overflow_graph(obs, constraint_line_id, antenna_sub_ids, root_sub_id)`

`graph_analysis/antenna_graph.py`. Returns the same 7-tuple as the standard
builders **plus** `antenna_meta`:

```
(df_of_g, overflow_sim=None, g_overflow, real_hubs,
 g_distribution_graph, node_name_mapping, antenna_meta)
```

Steps:

1. `new_flows = obs.p_or` copied, then zeroed on `_islanded_line_mask(...)`
   (every line with **at least one endpoint in the pocket** — the internal
   branches *and* the constraint line).
2. `_compute_delta_flow_frame(...)` — vectorised twin of alphaDeesp's
   `create_df` (and of the pypowsybl backend's
   `compute_flow_changes_after_disconnection`): produces the `idx_or, idx_ex,
   init_flows, new_flows, new_flows_swapped, delta_flows, gray_edges, line_name`
   frame, one row per line, in line order.
3. `_inhibit_swapped_flows(df)` — same correction as the standard builders.
4. `OverFlowGraph(topo, [constraint_line_id], df)` — the constraint row is the
   black cut edge; relabel nodes to substation names.
5. `Structured_Overload_Distribution_Graph(...)` + `get_hubs()`.
6. Build `antenna_meta` (pocket prod/load/net MW, `direction`).

`node_name_mapping` is the plain full-grid identity `{sub_id: name}` (no
synthetic node), so `pre_process_antenna_graph` reverts node names back to real
substation indices for action discovery.

### 3.2 Full graph for analysis, focused copy for the viewer

The analysis graph is kept over the **full grid**. The healthy lines carry zero
delta (gray) but they **anchor the root** so alphaDeesp's `find_hubs` does not
drop it as an isolate (a root left with only its single black constraint edge
crashes `find_hubs`). This is why we do **not** trim the analysis graph.

For the operator-facing render, `focus_overflow_graph_on_pocket(g_overflow, obs,
root_sub_id, antenna_sub_ids)` returns a **copy** restricted to `{root} ∪
pocket`. The visualization never rebuilds the structured-overload graph, so
trimming the root down to its black edge there is safe. `main._make_antenna_visualization`
renders this focused copy.

### 3.3 Action targeting (`_orchestrator.py`)

In antenna mode only injection families are discovered (`ls`, `rc`,
`redispatch`); topological families are filtered out (a radial pocket has no
loops / couplings to act on).

Injection candidates are taken **directly from the pocket** substation ids
(`antenna_meta["antenna_sub_ids"]`), *not* from `n_aval()` — because a producer
pocket is now correctly classified **amont**, so aval-only targeting would miss
it. Both redispatch directions (raise / lower) are offered; the per-action
simulation check keeps only the ones that actually relieve the overload.

---

## 4. `antenna_meta`

```python
{
  "constraint_line_id":  int,
  "constraint_line_name": str,
  "root_sub_id":   int,            # main-grid endpoint of the constraint
  "root_sub_name": str,
  "antenna_sub_ids":   List[int],  # pocket substation ids
  "antenna_sub_names": List[str],
  "n_subs":   int,
  "total_prod_mw": float,          # summed over the pocket
  "total_load_mw": float,
  "net_mw":   float,               # prod - load
  "direction": "producer" | "consumer",
}
```

`direction` is derived from the pocket's net injection (`net_mw > 0` →
producer). It is independent of the *graph* orientation (which alphaDeesp derives
from the flows); both should agree on a physically consistent case, but
`direction` is the robust signal the UI uses to phrase the recommendation
("curtail / redispatch-down" for a producer, "load-shed / redispatch-up" for a
consumer). Surfaced to Co-Study4Grid via the result payload (`antenna_meta`).

---

## 5. Tests

`tests/test_antenna_graph.py` (self-contained — hand-built grid2op-compatible
observation, no pypowsybl / real env):

- **Pocket detection** — `extract_antenna_context` finds the pocket / returns
  `None` when there is no overload.
- **Graph correctness** — consumer pocket: constraint black, branches blue,
  oriented root→pocket, healthy grid gray; producer (exporting) pocket:
  branches oriented pocket→root, pocket classified **amont**.
- **Helpers** — `_islanded_line_mask`, `_build_topo` (prod/load per sub, full
  edge list), `_compute_delta_flow_frame` (collapse → `-init` on the pocket,
  zero elsewhere; gray threshold relative to the constraint report), full
  alphaDeesp column set on `df_of_g`.
- **Robustness** — negative constraint `p_or` (branch-direction swap),
  `focus_overflow_graph_on_pocket` preserves edge styling, skips out-of-range
  ids and leaves the analysis graph untouched.
- **Discovery** — antenna mode runs injection families only; injection
  targeting hits the pocket even when it is amont; `pre_process_antenna_graph`
  reverts to integer ids without losing the constrained path; the full analysis
  graph survives `find_hubs`.

---

## 6. Limitations / future work

- The "blackout collapse" model ignores any redistribution **within** the
  healthy grid when the pocket is lost — the healthy grid is shown at zero
  delta. This is intentional (it focuses the graph on the pocket) and matches
  the operator's mental model, but it is an approximation of the true post-cut
  flows on the main grid.
- A single-feed pocket is assumed (`extract_antenna_context` bails out if the
  constraint does not cleanly bridge exactly one pocket-to-grid boundary).
  Multi-feed islanded regions are out of scope.
