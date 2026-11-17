# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# SPDX-License-Identifier: MPL-2.0
"""Tests for the antennas hanging off the constrained path: detection, typing
on the overflow graph and integration in the analysis pipeline.

The overflow graph is built the way the pipeline builds it — flow change after
cutting the overload, ``_compute_delta_flow_frame`` (the twin of alphaDeesp's
``create_df``), ``OverFlowGraph`` and ``Structured_Overload_Distribution_Graph``
— from a DC load flow on a hand-made grid, so most tests need neither
pypowsybl nor a real environment. The two end-to-end tests (IEEE 14 and the
pipeline's small test grid) skip without pypowsybl.

See ``docs/recommender/evacuation_antennas.md``.
"""

import copy
import json
import sys
import types
from unittest.mock import MagicMock

import networkx as nx
import numpy as np
import pandas as pd
import pytest
from alphaDeesp.core.graphsAndPaths import OverFlowGraph, Structured_Overload_Distribution_Graph

from expert_op4grid_recommender import config, pipeline
from expert_op4grid_recommender.graph_analysis.antenna_graph import (
    _build_topo,
    _compute_delta_flow_frame,
    _inhibit_swapped_flows,
)
from expert_op4grid_recommender.graph_analysis.structural_antennas import (
    detect_structural_antennas,
    tag_structural_antennas,
)
from expert_op4grid_recommender.pipeline import (
    AnalysisContext,
    run_analysis_step2_discovery,
    run_analysis_step2_graph,
)

# Topology (A0 is the slack)::
#
#        E3 ─ E2 ┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄┐ E2_R1: open in N-1 (the contingency)
#         ╲  ╱                        ┊
#          E1   C1                    ┊
#          ║    │                     ┊
#    A0 ── A1 ───────── O ══ X ── V1 ── W1
#     │     ╲          ╱          │
#     │      D1 ──────            │        D1: x = 50 on both sides
#     └── R1 ─────────── R2 ──────┘
#
# E1-E2-E3: evacuation antenna (wind + solar), meshed inside, double feeder
#           to A1; C1: upstream consumption antenna; W1: downstream one;
#           D1: insensitive pocket tied to TWO amont nodes (an antenna with two
#           attachments).
SUBS = ["A0", "A1", "O", "X", "V1", "R1", "R2", "E1", "E2", "E3", "C1", "D1", "W1"]
SUB = {name: i for i, name in enumerate(SUBS)}
# (name, origin, extremity, reactance)
LINES = [
    ("A0_A1", "A0", "A1", 1.0),
    ("A1_O", "A1", "O", 1.0),
    ("O_X", "O", "X", 1.0),          # the overload
    ("X_V1", "X", "V1", 1.0),
    ("A0_R1", "A0", "R1", 1.0),      # parallel path A0-R1-R2-V1
    ("R1_R2", "R1", "R2", 1.0),
    ("R2_V1", "R2", "V1", 1.0),
    ("A1_E1", "A1", "E1", 1.0),
    ("A1_E1_b", "A1", "E1", 1.0),
    ("E1_E2", "E1", "E2", 1.0),
    ("E2_E3", "E2", "E3", 1.0),
    ("E3_E1", "E3", "E1", 1.0),
    ("A1_C1", "A1", "C1", 1.0),
    ("A1_D1", "A1", "D1", 50.0),
    ("D1_O", "D1", "O", 50.0),
    ("V1_W1", "V1", "W1", 1.0),
    ("E2_R1", "E2", "R1", 1.0),
]
OPEN_IN_N1 = {"E2_R1"}
# (name, substation, MW, energy source) — the slack G_A0 balances the rest.
GENS = [("G_A0", "A0", None, "THERMAL"), ("G_E2", "E2", 120.0, "WIND"),
        ("G_E3", "E3", 30.0, "SOLAR"), ("G_D1", "D1", 30.0, "THERMAL")]
LOADS = [("LD_V1", "V1", 400.0), ("LD_E1", "E1", 5.0), ("LD_C1", "C1", 40.0),
         ("LD_W1", "W1", 50.0)]


def _injections():
    inj = np.zeros(len(SUBS))
    for _, sub, mw, _ in GENS:
        if mw is not None:
            inj[SUB[sub]] += mw
    for _, sub, mw in LOADS:
        inj[SUB[sub]] -= mw
    return inj


def _dc_flows(status):
    """DC load flow (slack A0); nodes cut off from the slack keep a 0 angle."""
    n = len(SUBS)
    b = np.zeros((n, n))
    graph = nx.Graph()
    graph.add_nodes_from(range(n))
    for (_, o, e, x), on in zip(LINES, status):
        if on:
            i, j = SUB[o], SUB[e]
            b[i, i] += 1 / x
            b[j, j] += 1 / x
            b[i, j] -= 1 / x
            b[j, i] -= 1 / x
            graph.add_edge(i, j)
    keep = sorted(nx.node_connected_component(graph, SUB["A0"]) - {SUB["A0"]})
    theta = np.zeros(n)
    theta[keep] = np.linalg.solve(b[np.ix_(keep, keep)], _injections()[keep])
    return np.array([(theta[SUB[o]] - theta[SUB[e]]) / x if on else 0.0
                     for (_, o, e, x), on in zip(LINES, status)])


def _make_obs(closed=(), pypowsybl=False):
    """N-1 observation; ``closed`` lists open-in-N-1 lines to close instead."""
    status = np.array([name not in OPEN_IN_N1 or name in closed for name, *_ in LINES])
    p_or = _dc_flows(status)
    gen_p = np.array([mw if mw is not None else -_injections().sum()
                      for _, _, mw, _ in GENS])
    gen_sub = np.array([SUB[sub] for _, sub, _, _ in GENS])
    load_sub = np.array([SUB[sub] for _, sub, _ in LOADS])
    fields = dict(
        name_sub=np.array(SUBS), name_line=np.array([line[0] for line in LINES]),
        line_or_to_subid=np.array([SUB[o] for _, o, _, _ in LINES]),
        line_ex_to_subid=np.array([SUB[e] for _, _, e, _ in LINES]),
        line_status=status, p_or=p_or, p_ex=-p_or,
        name_gen=np.array([g[0] for g in GENS]),
        gen_energy_source=np.array([g[3] for g in GENS]),
        name_load=np.array([ld[0] for ld in LOADS]),
        load_p=np.array([ld[2] for ld in LOADS]),
    )
    if not pypowsybl:
        return types.SimpleNamespace(**fields, gen_p=gen_p,
                                     gen_to_subid=gen_sub, load_to_subid=load_sub)

    def get_obj_connect_to(substation_id):
        return {"generators_id": [i for i, s in enumerate(gen_sub) if s == substation_id],
                "loads_id": [i for i, s in enumerate(load_sub) if s == substation_id],
                "lines_or_id": [], "lines_ex_id": []}

    # PypowsyblObservation: receptor sign convention, no *_to_subid arrays.
    return types.SimpleNamespace(**fields, gen_p=-gen_p, _network_manager=object(),
                                 get_obj_connect_to=get_obj_connect_to)


def _overflow_graph(obs, cut=("O_X",)):
    """Overflow graph + structured graph, built as the pipeline builders do."""
    names = list(obs.name_line)
    cut_ids = [names.index(c) for c in cut]
    status = np.asarray(obs.line_status).copy()
    status[cut_ids] = False
    df = _compute_delta_flow_frame(obs, _dc_flows(status), cut_ids[0], 0.05)
    df = _inhibit_swapped_flows(df)
    g_overflow = OverFlowGraph(_build_topo(obs), cut_ids, df, float_precision="%.0f")
    g_overflow.g = nx.relabel_nodes(g_overflow.g, dict(enumerate(obs.name_sub)), copy=True)
    return g_overflow, Structured_Overload_Distribution_Graph(g_overflow.g)


def _colors(g_overflow):
    return {data["name"]: data["color"] for _, _, data in g_overflow.g.edges(data=True)}


def _by_id(antennas):
    return {a.antenna_id: a for a in antennas}


def test_evacuation_antenna_detected_with_its_balance():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    assert {"A1", "O"} <= set(sdg.get_constrained_path().n_amont())

    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_A1_1"]

    assert evac.antenna_type == "evacuation"
    assert evac.side == "amont"
    assert evac.attachment_node == "A1"
    assert evac.attachment_sub_id == SUB["A1"]
    assert evac.sub_names == ["E1", "E2", "E3"]
    assert evac.connection_lines == ["A1_E1", "A1_E1_b"]
    assert evac.internal_lines == ["E1_E2", "E2_E3", "E3_E1"]
    assert evac.export_mw == pytest.approx(145.0)  # 120 + 30 - 5
    assert evac.prod_mw == pytest.approx(150.0)
    assert evac.load_mw == pytest.approx(5.0)
    assert evac.renewable_gens == ["G_E2", "G_E3"]
    assert evac.dispatchable_gens == []
    assert evac.loads == ["LD_E1"]


def test_evacuation_antenna_is_insensitive_and_gray():
    """Single attachment node ⇒ zero report on every antenna branch (DC),
    including the internal mesh — the antenna is invisible to the paths."""
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_A1_1"]

    assert evac.report_ratio_max == pytest.approx(0.0)
    assert evac.is_insensitive
    colors = _colors(g_overflow)
    for line in evac.connection_lines + evac.internal_lines:
        assert colors[line] == "gray", line


def test_upstream_consumption_antenna_is_typed_as_counter_lever():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    cons = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["CONS_UP_A1_1"]

    assert cons.antenna_type == "consumption_upstream"
    assert cons.sub_names == ["C1"]
    assert cons.export_mw == pytest.approx(-40.0)
    assert cons.loads == ["LD_C1"]


def test_insensitive_pocket_tied_to_two_amont_nodes_is_an_antenna():
    """D1 is meshed with the constrained path on two nodes, A1 and O, yet gray
    (report below the threshold): an antenna with two attachment nodes, listed
    by decreasing power exchanged (D1 sends more of its 30 MW to O)."""
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    colors = _colors(g_overflow)
    assert colors["A1_D1"] == "gray" and colors["D1_O"] == "gray"

    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_O_1"]
    assert evac.antenna_type == "evacuation"
    assert evac.nodes == ["D1"]
    assert evac.attachment_node == "O"
    assert evac.attachment_nodes == ["O", "A1"]
    assert evac.attachment_sub_ids == [SUB["O"], SUB["A1"]]
    assert evac.attachment_sub_names == ["O", "A1"]
    assert evac.connection_lines == ["A1_D1", "D1_O"]
    assert evac.export_mw == pytest.approx(30.0)
    assert evac.dispatchable_gens == ["G_D1"]
    assert 0 < evac.report_ratio_max < 0.05  # no longer exactly 0 with two attachments
    assert evac.is_insensitive


def test_sensitive_component_tied_to_several_nodes_is_not_an_antenna():
    """With two attachment nodes the report is no longer 0 by construction, so
    insensitivity becomes the criterion: above the threshold D1 is rejected,
    while the single-attachment antennas stay."""
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    antennas = detect_structural_antennas(g_overflow, sdg, obs, report_threshold=0.005)
    assert all("D1" not in a.nodes for a in antennas)
    assert sorted(a.antenna_id for a in antennas) == ["CONS_UP_A1_1", "EVAC_A1_1"]


def test_antennas_sorted_by_export_magnitude():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    antennas = detect_structural_antennas(g_overflow, sdg, obs)
    assert [a.antenna_id for a in antennas] == ["EVAC_A1_1", "CONS_UP_A1_1", "EVAC_O_1"]


def test_downstream_side_on_request():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    assert all(a.side == "amont" for a in detect_structural_antennas(g_overflow, sdg, obs))

    antennas = detect_structural_antennas(g_overflow, sdg, obs, sides=("amont", "aval"))
    cons = _by_id(antennas)["CONS_V1_1"]
    assert cons.side == "aval"
    assert cons.antenna_type == "consumption"
    assert cons.attachment_node == "V1"
    assert cons.export_mw == pytest.approx(-50.0)
    assert antennas[-1] is cons  # amont first


def test_downstream_evacuation_antenna(monkeypatch):
    """W1 turned into a net producer (80 MW for its 50 MW load): an antenna
    exporting into the downstream side is a downstream evacuation antenna."""
    monkeypatch.setattr(sys.modules[__name__], "GENS",
                        GENS + [("G_W1", "W1", 80.0, "THERMAL")])
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    antennas = detect_structural_antennas(g_overflow, sdg, obs, sides=("amont", "aval"))
    evac = _by_id(antennas)["EVAC_DOWN_V1_1"]
    assert evac.antenna_type == "evacuation_downstream"
    assert evac.side == "aval"
    assert evac.attachment_node == "V1"
    assert evac.export_mw == pytest.approx(30.0)
    assert evac.dispatchable_gens == ["G_W1"]
    assert evac.is_insensitive

    tag_structural_antennas(g_overflow, antennas)
    assert g_overflow.g.nodes["W1"]["in_evacuation_antenna"] is True
    assert g_overflow.g.nodes["V1"]["is_antenna_attachment"] is True


def test_open_line_does_not_mesh_the_antenna():
    """The N-1 contingency E2_R1 is open but still a (gray) overflow-graph edge:
    connectivity must come from line_status, or the antenna would be missed."""
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    assert "E2_R1" in _colors(g_overflow)
    assert "EVAC_A1_1" in _by_id(detect_structural_antennas(g_overflow, sdg, obs))


def test_closed_line_meshes_the_antenna_with_the_grid():
    obs = _make_obs(closed=("E2_R1",))
    g_overflow, sdg = _overflow_graph(obs)
    antennas = detect_structural_antennas(g_overflow, sdg, obs)
    assert all("E1" not in a.nodes for a in antennas)


def test_size_cap_rejects_large_components():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    # 13 nodes: a 2 / 13 cap keeps the 1-node C1 and D1 antennas, drops the
    # 3-node one.
    antennas = detect_structural_antennas(g_overflow, sdg, obs, max_fraction=2 / 13)
    assert [a.antenna_id for a in antennas] == ["CONS_UP_A1_1", "EVAC_O_1"]


@pytest.mark.parametrize("min_mw, expected", [
    (100.0, ["EVAC_A1_1"]),
    (200.0, []),
])
def test_min_mw_filters_small_exports(min_mw, expected):
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    antennas = detect_structural_antennas(g_overflow, sdg, obs, min_mw=min_mw)
    assert [a.antenna_id for a in antennas] == expected


def test_integer_labels_after_alphadeesp_preprocessing():
    """Discovery relabels the graph to substation ids; detection is unchanged."""
    obs = _make_obs()
    g_overflow, _ = _overflow_graph(obs)
    g_overflow.g = nx.relabel_nodes(g_overflow.g, {n: i for i, n in enumerate(SUBS)}, copy=True)
    sdg = Structured_Overload_Distribution_Graph(g_overflow.g)

    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_A1_1"]
    assert evac.attachment_node == SUB["A1"]
    assert evac.nodes == [SUB["E1"], SUB["E2"], SUB["E3"]]
    assert evac.sub_names == ["E1", "E2", "E3"]
    assert evac.export_mw == pytest.approx(145.0)


def test_distribution_graph_built_before_relabeling():
    """The pipeline builds the distribution graph on the name-labelled graph,
    then discovery's pre-processing (``pre_process_graph_alphadeesp``)
    relabels ``g`` to substation ids — so the two are in different label
    domains in a step-2 context kept past discovery (the caller's
    ``_last_step2_context``). Detection must still work (the constrained path
    is matched through ``obs.name_sub``)."""
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)          # sdg built on names
    g_overflow.g = nx.relabel_nodes(
        g_overflow.g, {n: i for i, n in enumerate(SUBS)}, copy=True)

    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_A1_1"]
    assert evac.attachment_node == SUB["A1"]
    assert evac.sub_names == ["E1", "E2", "E3"]
    assert evac.export_mw == pytest.approx(145.0)


def test_pypowsybl_sign_convention():
    obs = _make_obs(pypowsybl=True)
    g_overflow, sdg = _overflow_graph(obs)
    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_A1_1"]
    assert evac.prod_mw == pytest.approx(150.0)
    assert evac.renewable_gens == ["G_E2", "G_E3"]
    assert evac.export_mw == pytest.approx(145.0)


def test_unmapped_node_label_does_not_break_detection():
    """A label that maps to no substation (e.g. a second electrical node) stays
    in the topology; only its injections are not attributed."""
    obs = _make_obs()
    g_overflow, _ = _overflow_graph(obs)
    g_overflow.g = nx.relabel_nodes(g_overflow.g, {"E2": ("E2", 2)}, copy=True)
    sdg = Structured_Overload_Distribution_Graph(g_overflow.g)

    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_A1_1"]
    assert ("E2", 2) in evac.nodes
    assert evac.sub_names == ["E1", "E3"]
    assert evac.export_mw == pytest.approx(145.0)  # read on the feeders
    assert evac.prod_mw == pytest.approx(30.0)     # E2's wind not attributed


def test_several_overloads():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs, cut=("O_X", "X_V1"))
    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_A1_1"]
    assert evac.attachment_node == "A1"
    assert evac.export_mw == pytest.approx(145.0)


def test_real_pypowsybl_ieee14_evacuation_antenna():
    """End to end on a real pypowsybl network (AC): IEEE 14 bus 8 hangs off bus
    7 through L7-8. With production on B8-G and L7-9 as the overload, bus 7 is
    the constraint origin and bus 8 an evacuation antenna."""
    pp = pytest.importorskip("pypowsybl")
    from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment
    from expert_op4grid_recommender.pypowsybl_backend.overflow_analysis import (
        build_overflow_graph_pypowsybl,
    )

    network = pp.network.create_ieee14()
    network.update_generators(id="B8-G", target_p=80.0)
    env = SimulationEnvironment(network=network)
    obs = env.get_obs()
    cut = list(obs.name_line).index("L7-9-1")
    _, _, g_overflow, _, sdg, _ = build_overflow_graph_pypowsybl(
        env, obs, [cut], [], [], 0, do_consolidate_graph=False)

    evac = _by_id(detect_structural_antennas(g_overflow, sdg, obs))["EVAC_VL7_1"]
    b8_output = -float(obs.gen_p[list(obs.name_gen).index("B8-G")])  # after slack sharing
    assert evac.nodes == ["VL8"]
    assert evac.connection_lines == ["L7-8-1"]
    assert evac.prod_mw == pytest.approx(b8_output, abs=0.01)
    assert evac.export_mw == pytest.approx(b8_output, abs=0.1)
    assert evac.dispatchable_gens == ["B8-G"]
    assert evac.is_insensitive


def test_real_pypowsybl_ieee14_downstream_evacuation_antenna():
    """The same bus 8 seen from the other side: with T4-7 (4 -> 7) as the
    overload, bus 7 is downstream and bus 8 a downstream evacuation antenna."""
    pp = pytest.importorskip("pypowsybl")
    from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment
    from expert_op4grid_recommender.pypowsybl_backend.overflow_analysis import (
        build_overflow_graph_pypowsybl,
    )

    network = pp.network.create_ieee14()
    network.update_generators(id="B8-G", target_p=10.0)
    env = SimulationEnvironment(network=network)
    obs = env.get_obs()
    cut = list(obs.name_line).index("T4-7-1")
    _, _, g_overflow, _, sdg, _ = build_overflow_graph_pypowsybl(
        env, obs, [cut], [], [], 0, do_consolidate_graph=False)

    antennas = detect_structural_antennas(g_overflow, sdg, obs, sides=("amont", "aval"))
    evac = _by_id(antennas)["EVAC_DOWN_VL7_1"]
    b8_output = -float(obs.gen_p[list(obs.name_gen).index("B8-G")])  # after slack sharing
    assert evac.antenna_type == "evacuation_downstream"
    assert evac.nodes == ["VL8"]
    assert evac.connection_lines == ["L7-8-1"]
    assert evac.export_mw == pytest.approx(b8_output, abs=0.1)
    assert evac.dispatchable_gens == ["B8-G"]
    assert evac.is_insensitive


def test_detection_leaves_graphs_untouched():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    before = (copy.deepcopy(list(g_overflow.g.nodes(data=True))),
              copy.deepcopy(list(g_overflow.g.edges(keys=True, data=True))))
    detect_structural_antennas(g_overflow, sdg, obs, sides=("amont", "aval"))
    assert list(g_overflow.g.nodes(data=True)) == before[0]
    assert list(g_overflow.g.edges(keys=True, data=True)) == before[1]


def test_to_dict_is_a_plain_payload():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    payload = detect_structural_antennas(g_overflow, sdg, obs)[0].to_dict()
    assert payload["antenna_id"] == "EVAC_A1_1"
    assert payload["antenna_type"] == "evacuation"
    assert payload["connection_lines"] == ["A1_E1", "A1_E1_b"]
    assert payload["attachment_sub_names"] == ["A1"]
    # numpy labels are normalised to plain Python values
    assert type(payload["attachment_node"]) is str
    assert all(type(n) is str for n in payload["nodes"] + payload["attachment_nodes"])
    json.dumps(payload)


# ---------------------------------------------------------------------------
# Typing on the overflow graph (phase 2)
# ---------------------------------------------------------------------------

_TAG_KEYS = ("antenna_id", "antenna_type", "in_evacuation_antenna", "is_antenna_attachment")


def _tagged_overflow_graph():
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    tag_structural_antennas(g_overflow, detect_structural_antennas(g_overflow, sdg, obs))
    return g_overflow


def _alphadeesp_views(sdg):
    cp = sdg.get_constrained_path()
    return (list(cp.n_amont()), list(cp.n_aval()), sdg.get_constrained_edges_nodes(),
            sdg.get_dispatch_edges_nodes(only_loop_paths=True), sdg.get_hubs())


def test_tagging_types_antenna_nodes_and_branches():
    g_overflow = _tagged_overflow_graph()
    nodes = g_overflow.g.nodes
    for sub in ("E1", "E2", "E3"):
        assert nodes[sub]["antenna_id"] == "EVAC_A1_1"
        assert nodes[sub]["antenna_type"] == "evacuation"
        assert nodes[sub]["in_evacuation_antenna"] is True
    assert nodes["C1"]["antenna_id"] == "CONS_UP_A1_1"
    assert nodes["C1"]["in_evacuation_antenna"] is False
    assert nodes["D1"]["antenna_id"] == "EVAC_O_1"
    assert nodes["A1"]["is_antenna_attachment"] is True
    assert nodes["O"]["is_antenna_attachment"] is True  # D1's other attachment
    assert "antenna_id" not in nodes["A1"]

    tagged = {data["name"]: data["antenna_id"] for _, _, data in g_overflow.g.edges(data=True)
              if "antenna_id" in data}
    assert tagged == {"A1_E1": "EVAC_A1_1", "A1_E1_b": "EVAC_A1_1", "E1_E2": "EVAC_A1_1",
                      "E2_E3": "EVAC_A1_1", "E3_E1": "EVAC_A1_1", "A1_C1": "CONS_UP_A1_1",
                      "A1_D1": "EVAC_O_1", "D1_O": "EVAC_O_1"}
    # Only antenna nodes and attachment points carry a tag.
    assert {n for n, d in g_overflow.g.nodes(data=True) if any(k in d for k in _TAG_KEYS)} \
        == {"A1", "C1", "D1", "E1", "E2", "E3", "O"}


def test_tagging_changes_nothing_but_the_tags():
    """Colours, reports, labels and the alphaDeesp views (constrained path,
    red loops, hubs) are the same once the tags are set."""
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    nodes_before = copy.deepcopy(list(g_overflow.g.nodes(data=True)))
    edges_before = copy.deepcopy(list(g_overflow.g.edges(keys=True, data=True)))
    views_before = _alphadeesp_views(sdg)

    tag_structural_antennas(g_overflow, detect_structural_antennas(g_overflow, sdg, obs))

    def untagged(data):
        return {k: v for k, v in data.items() if k not in _TAG_KEYS}

    assert [(n, untagged(d)) for n, d in g_overflow.g.nodes(data=True)] == nodes_before
    assert [(u, v, k, untagged(d)) for u, v, k, d in g_overflow.g.edges(keys=True, data=True)] \
        == edges_before
    assert _alphadeesp_views(Structured_Overload_Distribution_Graph(g_overflow.g)) == views_before


def test_tags_survive_relabelling_to_ids():
    """Discovery's pre-processing renumbers the nodes; the tags follow them."""
    g_overflow = _tagged_overflow_graph()
    g_ids = nx.relabel_nodes(g_overflow.g, {n: i for i, n in enumerate(SUBS)}, copy=True)
    assert g_ids.nodes[SUB["E2"]]["antenna_id"] == "EVAC_A1_1"
    assert g_ids.nodes[SUB["A1"]]["is_antenna_attachment"] is True


def test_tagged_graph_still_converts_to_dot():
    """The visualization goes through pydot: the tags must not break it."""
    pydot = pytest.importorskip("pydot")
    dot = nx.nx_pydot.to_pydot(_tagged_overflow_graph().g).to_string()
    assert "EVAC_A1_1" in dot
    assert pydot.graph_from_dot_data(dot)


# ---------------------------------------------------------------------------
# Pipeline integration (phase 2)
# ---------------------------------------------------------------------------

def _step2_context(obs=None):
    """Step-1 context whose fake backend hands back the fixture overflow graph."""
    obs = obs if obs is not None else _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    cut = [line[0] for line in LINES].index("O_X")

    class _FakeBackend:
        is_pypowsybl = False
        branch_candidates_from_baseline = False
        use_shared_baseline_for_topological = False
        fast_mode = True

        def check_simu_overloads(self, *args, **kwargs):
            return True, False

        def build_overflow_graph(self, *args, **kwargs):
            df = pd.DataFrame({"new_flows_swapped": pd.Series([], dtype=bool),
                               "line_name": pd.Series([], dtype=str)})
            return df, MagicMock(), g_overflow, [], sdg, dict(enumerate(SUBS))

    return AnalysisContext(
        backend=_FakeBackend(), env=MagicMock(), obs=obs, obs_simu_defaut=obs,
        current_lines_defaut=["E2_R1"], lines_overloaded_ids=[cut],
        lines_overloaded_ids_kept=[cut], lines_overloaded_names=["O_X"],
        lines_non_reconnectable=[], non_connected_reconnectable_lines=[],
        dict_action={}, classifier=MagicMock(), chronic_name="test",
    )


def test_step2_graph_detects_and_tags_the_antennas():
    """Both sides of the constrained path are scanned."""
    context = run_analysis_step2_graph(_step2_context())
    assert [a.antenna_id for a in context.structural_antennas] == [
        "EVAC_A1_1", "CONS_UP_A1_1", "EVAC_O_1", "CONS_V1_1"]
    assert context.g_overflow.g.nodes["E2"]["antenna_id"] == "EVAC_A1_1"
    assert context.g_overflow.g.nodes["W1"]["antenna_id"] == "CONS_V1_1"


def test_step2_graph_detection_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "ENABLE_STRUCTURAL_ANTENNAS", False)
    context = run_analysis_step2_graph(_step2_context())
    assert context.structural_antennas == []
    assert not any(k in d for _, d in context.g_overflow.g.nodes(data=True) for k in _TAG_KEYS)


def test_step2_graph_survives_a_detection_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(pipeline, "detect_structural_antennas", _boom)
    context = run_analysis_step2_graph(_step2_context())
    assert context.structural_antennas == []
    assert context.g_distribution_graph is not None


def test_pocket_mode_skips_the_detection(monkeypatch):
    calls = []
    monkeypatch.setattr(pipeline, "detect_structural_antennas",
                        lambda *args, **kwargs: calls.append(args) or [])
    obs = _make_obs()
    g_overflow, sdg = _overflow_graph(obs)
    monkeypatch.setattr(pipeline, "build_antenna_overflow_graph",
                        lambda *args, **kwargs: (None, None, g_overflow, [], sdg, {}, {}))
    context = _step2_context(obs)
    context.update({"antenna_mode": True, "antenna_info": {
        "constraint_line_id": 0, "antenna_sub_ids": [], "root_sub_id": 0}})

    context = run_analysis_step2_graph(context)
    assert context.structural_antennas == []
    assert calls == []


def test_discovery_hands_the_antennas_to_the_model_and_the_result(monkeypatch):
    from expert_op4grid_recommender.models import _expert_discovery
    from expert_op4grid_recommender.models.base import RecommenderModel, RecommenderOutput
    from expert_op4grid_recommender.utils import reassessment

    class _Spy(RecommenderModel):
        name = "spy"
        label = "Spy"

        @classmethod
        def params_spec(cls):
            return []

        def recommend(self, inputs, params):
            self.inputs = inputs
            return RecommenderOutput(prioritized_actions={})

    monkeypatch.setattr(_expert_discovery, "_run_expert_action_filter", lambda context: None)
    monkeypatch.setattr(reassessment, "reassess_prioritized_actions",
                        lambda actions, context: ({}, []))
    monkeypatch.setattr(reassessment, "compute_combined_pairs", lambda actions, context: {})

    context = run_analysis_step2_graph(_step2_context())
    spy = _Spy()
    result = run_analysis_step2_discovery(context, recommender=spy)

    assert spy.inputs.structural_antennas is context.structural_antennas
    assert [a["antenna_id"] for a in result["structural_antennas"]] == [
        "EVAC_A1_1", "CONS_UP_A1_1", "EVAC_O_1", "CONS_V1_1"]
    assert result.structural_antennas[0]["export_mw"] == pytest.approx(145.0)
    json.dumps(result.structural_antennas)


def test_structural_antennas_leave_the_recommendations_unchanged():
    """Non-regression guard: phase 2 only types the antennas. On the pipeline's
    small test grid (pypowsybl), where 5 antennas are detected and tagged, the
    prioritized actions and every score are the same with and without it."""
    pytest.importorskip("pypowsybl")
    from expert_op4grid_recommender.backends import Backend

    saved = config.get_settings()
    results = {}
    try:
        for enabled in (False, True):
            config.override_settings(
                ENV_NAME="bare_env_small_grid_test",
                FILE_ACTION_SPACE_DESC="reduced_model_actions_test_pypowsybl.json",
                ENABLE_STRUCTURAL_ANTENNAS=enabled,
            )
            results[enabled] = pipeline.run_analysis(
                analysis_date=None, current_timestep=0,
                current_lines_defaut=["P.SAOL31RONCI"], backend=Backend.PYPOWSYBL,
            )
    finally:
        config.override_settings(saved)

    off, on = results[False], results[True]
    assert off.structural_antennas == []
    assert [a["antenna_id"] for a in on.structural_antennas[:2]] == ["EVAC_SSV.OP7_1",
                                                                      "EVAC_VIELMP7_1"]
    assert set(on.prioritized_actions) == set(off.prioritized_actions)
    assert any(family["scores"] for family in on.action_scores.values())  # not vacuous
    assert on.action_scores == off.action_scores


@pytest.mark.slow
def test_evacuation_levers_end_to_end():
    """Phase 4 with ``EVACUATION_ANTENNA_LEVERS`` on, on the small test grid:
    the nuclear units of its two evacuation antennas become redispatch levers
    — unit candidates referenced on the attachment node, plus one MW-sized
    aggregate per antenna — and the aggregates (multi-generator ``set_gen_p``)
    go through the reassessment and the superposition pairs."""
    pytest.importorskip("pypowsybl")
    from expert_op4grid_recommender.backends import Backend

    saved = config.get_settings()
    try:
        config.override_settings(
            ENV_NAME="bare_env_small_grid_test",
            FILE_ACTION_SPACE_DESC="reduced_model_actions_test_pypowsybl.json",
            EVACUATION_ANTENNA_LEVERS=True,
            MIN_REDISPATCH=6,  # push every redispatch candidate into the reassessment
        )
        result = pipeline.run_analysis(
            analysis_date=None, current_timestep=0,
            current_lines_defaut=["P.SAOL31RONCI"], backend=Backend.PYPOWSYBL,
        )
    finally:
        config.override_settings(saved)

    params = result.action_scores["redispatch"]["params"]
    bugey = params["redispatch_EVAC_SSV.OP7_1"]
    assert bugey["influence_ref_substation"] == "SSV.OP7"
    assert bugey["gen_names"] == ["BUGEY7G2", "BUGEY7G4"]
    assert params["redispatch_BUGEY7G2"]["evacuation_antenna_id"] == "EVAC_SSV.OP7_1"
    for action_id in ("redispatch_EVAC_SSV.OP7_1", "redispatch_EVAC_VIELMP7_1"):
        assert action_id in result.prioritized_actions
        assert result.prioritized_actions[action_id].get("non_convergence") is None
    assert any("EVAC_" in pair for pair in result.combined_actions)


def test_downstream_evacuation_lever_end_to_end():
    """The downstream lever end to end on a real pypowsybl network (IEEE 14,
    AC). Tripping T4-9 overloads T4-7 (4 -> 7), so bus 8, hanging off bus 7,
    is a downstream evacuation antenna. (Bus 3, a load tied to buses 2 and 4
    upstream, is an insensitive consumption antenna with two attachments.) With
    the levers on, the pipeline raises the unit B8-G: a unit candidate, plus an
    aggregate moved from its 10 MW setpoint rather than from the ~6.5 MW it
    produces after the slack sharing. The reassessment confirms that both
    relieve the overload."""
    pp = pytest.importorskip("pypowsybl")
    from expert_op4grid_recommender.backends import Backend
    from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment

    network = pp.network.create_ieee14()
    network.update_generators(id="B8-G", target_p=10.0)
    env = SimulationEnvironment(network=network)
    limits = np.full(len(env.name_line), 9999.0)
    limits[list(env.name_line).index("T4-7-1")] = 135.0  # A: rho 0.80 in N, 1.09 in N-1
    env.set_thermal_limit(limits)
    prebuilt = {"env": env, "path_chronic": "", "chronic_name": "ieee14",
                "custom_layout": None, "lines_non_reconnectable": [],
                "lines_we_care_about": list(env.name_line)}

    saved = config.get_settings()
    try:
        config.override_settings(EVACUATION_ANTENNA_LEVERS=True,
                                 ALLOWED_ACTION_TYPES=["redispatch"], MIN_REDISPATCH=4)
        context = pipeline.run_analysis_step1(
            analysis_date=None, current_timestep=0, current_lines_defaut=["T4-9-1"],
            backend=Backend.PYPOWSYBL, dict_action={}, prebuilt_env_context=prebuilt)
        result = run_analysis_step2_discovery(run_analysis_step2_graph(context))
    finally:
        config.override_settings(saved)

    assert [(a["antenna_id"], a["antenna_type"], a["attachment_sub_names"])
            for a in result.structural_antennas] == [
        ("CONS_UP_VL2_1", "consumption_upstream", ["VL2", "VL4"]),
        ("EVAC_DOWN_VL7_1", "evacuation_downstream", ["VL7"]),
    ]
    params = result.action_scores["redispatch"]["params"]
    unit = params["redispatch_B8-G"]
    assert unit["direction"] == "up"
    assert unit["influence_ref_substation"] == "VL7"
    assert unit["evacuation_antenna_id"] == "EVAC_DOWN_VL7_1"
    aggregate = params["redispatch_EVAC_DOWN_VL7_1"]
    assert aggregate["direction"] == "up"
    assert aggregate["gen_names"] == ["B8-G"]
    assert aggregate["gen_p"] < 9.0  # what B8-G produces after the slack sharing
    assert aggregate["target_p_MW"]["B8-G"] == pytest.approx(
        10.0 + aggregate["applied_MW"], abs=0.01)
    for action_id in ("redispatch_B8-G", "redispatch_EVAC_DOWN_VL7_1"):
        action = result.prioritized_actions[action_id]
        assert action["is_rho_reduction"]
        assert action["max_rho"] < float(np.max(action["rho_before"]))
