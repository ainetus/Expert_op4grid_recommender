# expert_op4grid_recommender/graph_analysis/antenna_graph.py
#!/usr/bin/python3
# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of expert_op4grid_recommender, Expert system analyzer based on ExpertOp4Grid principles.
"""Overflow graph for an islanded radial ("antenne") pocket.

When a contingency islands a radial pocket of substations — disconnecting even
the single most-loaded overloaded line breaks the grid apart — the regular
``build_overflow_graph`` pipeline cannot run a meaningful flow *redistribution*:
the load flow on the cut grid diverges (the pocket has no slack and simply
blacks out).

Rather than hand-building a synthetic graph, we feed the **standard ExpertOp4Grid
machinery** the post-disconnection state implied by the islanding, modelled as:

    new_flows = initial post-contingency flows, with every line incident to the
                islanded pocket set to 0 (the pocket blacks out; the healthy
                grid is left untouched — the operator-chosen approximation).

From that we compute the exact same per-line ``delta_flows`` frame as
``alphaDeesp.Grid2opSimulation.create_df`` (signed report, swapped-flow
handling, gray-edge threshold), then build the graph with
``OverFlowGraph`` + ``Structured_Overload_Distribution_Graph`` — identical to
``build_overflow_graph`` / ``build_overflow_graph_pypowsybl`` minus the load
flow. Edge colour (blue / coral), orientation and the amont/aval split are
therefore decided by ExpertOp4Grid from the *real signed* flows, so a producer
pocket (amont of the overload islanded) and a consumer pocket (aval islanded)
both render with physical directions. The healthy grid carries ``delta_flows =
0`` → gray → trimmed by ``keep_overloads_components``, leaving the pocket plus
the black constraint edge.

The function returns the same 6-tuple shape as
:func:`expert_op4grid_recommender.graph_analysis.builder.build_overflow_graph`
(with ``overflow_sim = None``) plus an ``antenna_meta`` dict describing the
pocket.
"""

from typing import Any, Dict, List, Tuple

import networkx as nx
import numpy as np
import pandas as pd
from alphaDeesp.core.graphsAndPaths import OverFlowGraph, Structured_Overload_Distribution_Graph

from expert_op4grid_recommender.config import PARAM_OPTIONS_EXPERT_OP
from expert_op4grid_recommender.utils.injections import injections_by_sub

# Retained for backward compatibility (older consumers / the viewer's optional
# node-hiding hook). The current builder no longer injects a synthetic source
# node, so this name is not present in the produced graph.
SOURCE_NODE_NAME = "__GRID_SOURCE__"


def _sub_injections(obs: Any) -> Tuple[np.ndarray, np.ndarray]:
    """Per-substation production and load (MW), indexed by substation id.

    Production is counted positive and elements are located on both backends.
    Reading ``gen_to_subid`` / raw ``gen_p`` directly zeroed every pocket on
    pypowsybl (no ``gen_to_subid`` there, and producing generators have a
    negative ``p``), so ``antenna_meta`` always said 0 MW / ``"consumer"``.
    """
    return injections_by_sub(obs)


def _build_topo(obs: Any) -> Dict[str, Any]:
    """Full-grid topology dict in the shape ``OverFlowGraph`` expects.

    Mirrors ``AlphaDeespAdapter._build_topo`` / ``Grid2opSimulation.topo``:
    one entry per line (original orientation) and per substation (prod/load).
    """
    n = len(obs.name_line)
    idx_or = [int(obs.line_or_to_subid[i]) for i in range(n)]
    idx_ex = [int(obs.line_ex_to_subid[i]) for i in range(n)]
    prod, load = _sub_injections(obs)
    return {
        "edges": {"idx_or": idx_or, "idx_ex": idx_ex},
        "nodes": {
            "are_prods": (prod > 0).tolist(),
            "are_loads": (load > 0).tolist(),
            "prods_values": prod.tolist(),
            "loads_values": load.tolist(),
            "names": list(obs.name_sub),
        },
    }


def _islanded_line_mask(obs: Any, antenna_sub_ids: List[int]) -> np.ndarray:
    """Lines that lose all flow once the pocket islands.

    Any line with at least one endpoint inside the pocket (the internal pocket
    branches AND the constraint line bridging the pocket to the main grid) goes
    to zero flow when the pocket blacks out.
    """
    pocket = set(int(s) for s in antenna_sub_ids)
    n = len(obs.name_line)
    or_sub = np.array([int(obs.line_or_to_subid[i]) for i in range(n)])
    ex_sub = np.array([int(obs.line_ex_to_subid[i]) for i in range(n)])
    return np.array([(or_sub[i] in pocket) or (ex_sub[i] in pocket) for i in range(n)])


def _compute_delta_flow_frame(obs: Any, new_flows_arr: np.ndarray,
                              constraint_line_id: int, threshold: float) -> pd.DataFrame:
    """Replicate ``alphaDeesp.Simulation.create_df`` from explicit new flows.

    Vectorised twin of ``OverflowSimulator.compute_flow_changes_after_disconnection``
    (pypowsybl backend) — the only difference is that ``new_flows_arr`` is
    supplied by the caller (the islanding "collapse" state) instead of read from
    a load flow that would diverge. The output columns and sign conventions are
    byte-for-byte what ``OverFlowGraph`` consumes.
    """
    line_names = list(obs.name_line)
    n = len(line_names)
    idx_or = np.array([int(obs.line_or_to_subid[i]) for i in range(n)], dtype=int)
    idx_ex = np.array([int(obs.line_ex_to_subid[i]) for i in range(n)], dtype=int)

    init_flows = np.asarray(obs.p_or, dtype=float).copy()
    init_flows = np.where(np.isnan(init_flows), 0.0, init_flows)
    new_arr = np.where(np.isnan(new_flows_arr), 0.0, new_flows_arr.astype(float))

    # STEP 2: branch_direction_swaps — canonicalise so init_flows >= 0.
    swap_mask = (init_flows < 0) & (init_flows != 0.0)
    idx_or_s = np.where(swap_mask, idx_ex, idx_or)
    idx_ex_s = np.where(swap_mask, idx_or, idx_ex)
    idx_or = idx_or_s
    idx_ex = idx_ex_s
    init_abs = np.where(swap_mask, np.abs(init_flows), init_flows)

    # STEP 3: apply the same swap to new flows.
    new_flows = np.where(swap_mask, -new_arr, new_arr)

    # STEP 4: new_flows_swapped — reversed and stronger.
    new_flows_swapped = (new_flows < 0) & (np.abs(new_flows) > np.abs(init_abs))

    # STEP 5: delta_flows.
    abs_new = np.abs(new_flows)
    abs_init = np.abs(init_abs)
    delta = abs_new - abs_init
    case1 = new_flows_swapped
    delta = np.where(case1, abs_new + abs_init, delta)
    idx_or = np.where(case1, idx_ex, idx_or)
    idx_ex = np.where(case1, idx_or_s, idx_ex)
    sign_new = np.sign(new_flows)
    sign_init = np.sign(init_abs)
    case2 = (sign_new != sign_init) & (new_flows != 0) & (init_abs != 0) & ~case1
    delta = np.where(case2, -(abs_new + abs_init), delta)

    # STEP 6: gray edges, relative to the constraint (cut) line's report.
    ltc_report = np.abs(delta[constraint_line_id]) if 0 <= constraint_line_id < n else np.abs(delta).max()
    max_overload = ltc_report * float(threshold)
    gray_edges = np.abs(delta) < max_overload

    df = pd.DataFrame({
        "idx_or": idx_or,
        "idx_ex": idx_ex,
        "init_flows": init_abs,
        "line_name": line_names,
        "swapped": swap_mask,
        "new_flows": new_flows,
        "new_flows_swapped": new_flows_swapped,
        "delta_flows": delta,
        "gray_edges": gray_edges,
    })
    return df


def _inhibit_swapped_flows(df: pd.DataFrame) -> pd.DataFrame:
    """Negate delta + swap or/ex for rows whose new flow reversed direction.

    Same correction as ``builder.inhibit_swapped_flows`` /
    ``overflow_analysis._inhibit_swapped_flows``.
    """
    mask = df.new_flows_swapped
    if not mask.any():
        return df
    df.loc[mask, "delta_flows"] = -df.loc[mask, "delta_flows"]
    idx_or = df.loc[mask, "idx_or"].copy()
    df.loc[mask, "idx_or"] = df.loc[mask, "idx_ex"]
    df.loc[mask, "idx_ex"] = idx_or
    return df


def focus_overflow_graph_on_pocket(g_overflow: Any, obs: Any, root_sub_id: int,
                                   antenna_sub_ids: List[int]) -> Any:
    """Return a copy of ``g_overflow`` restricted to the root + pocket nodes.

    The analysis graph is built over the full grid (the gray healthy lines
    anchor the root for ``find_hubs``); for the operator-facing render we want a
    clean pocket view. Visualization never rebuilds the structured-overload
    graph, so trimming the root down to its single black constraint edge here is
    safe (it would crash ``find_hubs``, which is why the analysis graph keeps the
    full grid).

    ``g_overflow.g`` nodes must already be substation *names*. Substation ids
    out of range, and names absent from the graph (e.g. a node-renaming step
    ran), are silently skipped.
    """
    import copy as _copy

    n_sub = len(obs.name_sub)
    keep = {obs.name_sub[s] for s in [root_sub_id, *antenna_sub_ids]
            if 0 <= int(s) < n_sub}
    keep &= set(g_overflow.g.nodes())
    focused = _copy.copy(g_overflow)
    focused.g = g_overflow.g.subgraph(keep).copy()
    return focused


def build_antenna_overflow_graph(obs: Any, constraint_line_id: int,
                                 antenna_sub_ids: List[int], root_sub_id: int,
                                 float_precision: str = "%.0f"):
    """Build the overflow graph for an islanded pocket via the standard pipeline.

    Args:
        obs: Grid2Op-compatible observation (post-contingency state, where the
            constraint line is still connected and overloaded).
        constraint_line_id (int): index of the overloaded line feeding the pocket.
        antenna_sub_ids (list[int]): substation ids inside the pocket.
        root_sub_id (int): the constraint endpoint on the main grid (kept for the
            returned ``antenna_meta``; orientation is now derived from the flows).
        float_precision (str): edge-label precision passed to ``OverFlowGraph``.

    Returns:
        tuple: ``(df_of_g, overflow_sim, g_overflow, real_hubs,
        g_distribution_graph, node_name_mapping, antenna_meta)`` — mirrors
        ``build_overflow_graph``'s return (``overflow_sim is None``) with
        ``antenna_meta`` appended. ``g_overflow.g`` nodes are substation names;
        ``node_name_mapping`` is ``{real_substation_id: name}`` so the downstream
        ``pre_process_antenna_graph`` reverts to real substation indices.
    """
    threshold = float(PARAM_OPTIONS_EXPERT_OP.get("ThresholdReportOfLine", 0.05))

    # Post-disconnection collapse state: initial flows everywhere, zeroed on
    # every line incident to the islanded pocket.
    init_flows = np.asarray(obs.p_or, dtype=float).copy()
    init_flows = np.where(np.isnan(init_flows), 0.0, init_flows)
    new_flows = init_flows.copy()
    new_flows[_islanded_line_mask(obs, antenna_sub_ids)] = 0.0

    df_of_g = _compute_delta_flow_frame(obs, new_flows, constraint_line_id, threshold)
    df_of_g = _inhibit_swapped_flows(df_of_g)

    topo = _build_topo(obs)

    # The constraint line is the cut (black) edge. Row index == line index, so
    # ``OverFlowGraph`` paints row ``constraint_line_id`` black.
    g_overflow = OverFlowGraph(topo, [int(constraint_line_id)], df_of_g,
                               float_precision=float_precision)

    node_name_mapping: Dict[int, str] = {i: name for i, name in enumerate(obs.name_sub)}
    g_overflow.g = nx.relabel_nodes(g_overflow.g, node_name_mapping, copy=True)

    # NB: the graph is intentionally kept over the full grid. The healthy lines
    # carry zero delta (gray); they anchor the root substation so alphaDeesp's
    # ``find_hubs`` (run by the downstream discovery via
    # ``pre_process_antenna_graph``) does not drop it as an isolate. The
    # visualization focuses a *copy* on the pocket for a clean operator view
    # (see ``main._make_antenna_visualization`` / ``focus_overflow_graph_on_pocket``).
    g_distribution_graph = Structured_Overload_Distribution_Graph(g_overflow.g)
    real_hubs = g_distribution_graph.get_hubs()

    prod, load = _sub_injections(obs)
    total_prod = float(sum(prod[s] for s in antenna_sub_ids))
    total_load = float(sum(load[s] for s in antenna_sub_ids))
    net_mw = total_prod - total_load
    antenna_meta = {
        "constraint_line_id": int(constraint_line_id),
        "constraint_line_name": obs.name_line[constraint_line_id],
        "root_sub_id": int(root_sub_id),
        "root_sub_name": obs.name_sub[root_sub_id],
        "antenna_sub_ids": list(antenna_sub_ids),
        "antenna_sub_names": [obs.name_sub[s] for s in antenna_sub_ids],
        "n_subs": len(antenna_sub_ids),
        "total_prod_mw": round(total_prod, 2),
        "total_load_mw": round(total_load, 2),
        "net_mw": round(net_mw, 2),
        # Net exporter (prod > load) → curtailment / redispatch-down help;
        # net importer (load > prod) → load shedding / redispatch-up help.
        "direction": "producer" if net_mw > 0 else "consumer",
    }

    return df_of_g, None, g_overflow, real_hubs, g_distribution_graph, node_name_mapping, antenna_meta
