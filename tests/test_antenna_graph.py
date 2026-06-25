# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the antenna (islanded-pocket) overflow graph.

These tests are self-contained: they build a tiny grid2op-compatible
observation by hand, so they need neither pypowsybl nor a real environment.

See ``docs/recommender/antenna_overflow_graph.md`` for the design.
"""

import types

import networkx as nx
import numpy as np
import pytest
from alphaDeesp.core.graphsAndPaths import Structured_Overload_Distribution_Graph

from expert_op4grid_recommender.graph_analysis.antenna_graph import (
    SOURCE_NODE_NAME,
    _build_topo,
    _compute_delta_flow_frame,
    _islanded_line_mask,
    build_antenna_overflow_graph,
    focus_overflow_graph_on_pocket,
)
from expert_op4grid_recommender.graph_analysis.processor import (
    extract_antenna_context,
    pre_process_antenna_graph,
)


def _make_obs(antenna_prod=None):
    """Build a fake observation with a radial pocket fed by one overloaded line.

    Topology::

        main grid:  0 - 5 - 6 - 7 - 8          (sub 0 is the antenna root)
        constraint:    0 == 1                  (overloaded, rho = 1.5)
        antenna:       1 - 2,  1 - 3,  3 - 4

    The pocket {1, 2, 3, 4} is islanded as soon as the 0==1 line is cut.
    Loads sit on subs 2/3/4 → net consumer, unless ``antenna_prod`` injects
    generation to flip the pocket into a net producer.
    """
    name_sub = np.array([f"S{i}" for i in range(9)])
    # lines: (or_sub, ex_sub, name)
    lines = [
        (0, 5, "L0_5"), (5, 6, "L5_6"), (6, 7, "L6_7"), (7, 8, "L7_8"),  # main grid
        (0, 1, "CONSTRAINT"),                                            # feed
        (1, 2, "L1_2"), (1, 3, "L1_3"), (3, 4, "L3_4"),                  # antenna
    ]
    n_lines = len(lines)
    name_line = np.array([ln[2] for ln in lines])
    line_or_to_subid = np.array([ln[0] for ln in lines])
    line_ex_to_subid = np.array([ln[1] for ln in lines])
    line_or_bus = np.ones(n_lines, dtype=int)
    line_ex_bus = np.ones(n_lines, dtype=int)
    line_status = np.ones(n_lines, dtype=bool)
    a_or = np.full(n_lines, 100.0)
    rho = np.full(n_lines, 0.5)
    constraint_id = list(name_line).index("CONSTRAINT")
    rho[constraint_id] = 1.5  # the overload
    # active-power flows (MW), magnitudes only matter for the graph
    p_or = np.full(n_lines, 10.0)
    p_or[constraint_id] = 60.0
    p_or[list(name_line).index("L1_2")] = 30.0
    p_or[list(name_line).index("L1_3")] = 30.0
    p_or[list(name_line).index("L3_4")] = 10.0

    # loads on antenna subs 2/3/4
    load_to_subid = np.array([2, 3, 4])
    load_p = np.array([30.0, 20.0, 10.0])

    if antenna_prod is None:
        gen_to_subid = np.array([0])
        gen_p = np.array([0.0])
        name_gen = np.array(["G0"])
    else:
        gen_to_subid = np.array([antenna_prod[0]])
        gen_p = np.array([antenna_prod[1]])
        name_gen = np.array(["G0"])

    return types.SimpleNamespace(
        name_sub=name_sub, name_line=name_line,
        name_load=np.array(["LD2", "LD3", "LD4"]), name_gen=name_gen,
        line_or_to_subid=line_or_to_subid, line_ex_to_subid=line_ex_to_subid,
        line_or_bus=line_or_bus, line_ex_bus=line_ex_bus,
        line_status=line_status, a_or=a_or, rho=rho, p_or=p_or,
        load_to_subid=load_to_subid, load_p=load_p,
        gen_to_subid=gen_to_subid, gen_p=gen_p,
    )


def _edge_colors_by_name(g):
    return {data["name"]: data["color"]
            for _, _, data in g.edges(data=True)}


def test_extract_antenna_context_identifies_pocket():
    obs = _make_obs()
    overloaded = [list(obs.name_line).index("CONSTRAINT")]
    ctx = extract_antenna_context(obs, overloaded)

    assert ctx is not None
    assert ctx["constraint_line_name"] == "CONSTRAINT"
    assert ctx["root_sub_id"] == 0
    assert ctx["antenna_sub_ids"] == [1, 2, 3, 4]
    assert set(ctx["antenna_sub_names"]) == {"S1", "S2", "S3", "S4"}


def test_extract_antenna_context_none_when_no_overloads():
    obs = _make_obs()
    assert extract_antenna_context(obs, []) is None


def test_antenna_graph_classifies_whole_pocket_as_aval():
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])

    df, sim, g_overflow, hubs, sdg, mapping, meta = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    assert sim is None  # no Grid2opSimulation involved
    cp = sdg.get_constrained_path()
    # The whole pocket is downstream (aval); the root is upstream (amont).
    assert set(cp.n_aval()) == {"S1", "S2", "S3", "S4"}
    assert "S0" in cp.n_amont()

    colors = _edge_colors_by_name(g_overflow.g)
    assert colors["CONSTRAINT"] == "black"
    for branch in ("L1_2", "L1_3", "L3_4"):
        assert colors[branch] == "blue", f"{branch} should be a downstream blue edge"


def test_antenna_meta_consumer_direction_and_net_mw():
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    *_, meta = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    assert meta["direction"] == "consumer"
    assert meta["n_subs"] == 4
    assert meta["total_load_mw"] == pytest.approx(60.0)
    assert meta["total_prod_mw"] == pytest.approx(0.0)
    assert meta["net_mw"] == pytest.approx(-60.0)
    assert set(meta["antenna_sub_names"]) == {"S1", "S2", "S3", "S4"}


def test_antenna_meta_producer_direction():
    # 200 MW of generation on antenna sub 2 outweighs the 60 MW of load.
    obs = _make_obs(antenna_prod=(2, 200.0))
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    *_, meta = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    assert meta["direction"] == "producer"
    assert meta["total_prod_mw"] == pytest.approx(200.0)
    assert meta["net_mw"] == pytest.approx(140.0)


def _make_exporting_obs():
    """A pocket that EXPORTS power up to the main grid (amont-islanded case).

    Same topology as ``_make_obs`` but the constraint and pocket-branch flows
    are reversed (pocket → root), modelling generators inside the pocket feeding
    the rest of the grid through the overloaded line — the case the RTE operator
    flagged (flow rising from the pocket up to the constraint).
    """
    obs = _make_obs()
    nl = list(obs.name_line)
    p = obs.p_or.copy()
    for ln in ("CONSTRAINT", "L1_2", "L1_3", "L3_4"):
        p[nl.index(ln)] *= -1
    obs.p_or = p
    return obs


def test_node_name_mapping_is_full_grid_identity_and_reverts_to_indices():
    """The discovery pipeline reverts node names to *real* grid2op sub indices
    (via pre_process_antenna_graph) and uses them to index obs arrays. The graph
    is now built over the full grid through the standard ExpertOp4Grid pipeline,
    so the mapping is the plain ``{sub_id: name}`` identity (no synthetic node)."""
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g_overflow, _, _, mapping, _ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    assert mapping == {i: name for i, name in enumerate(obs.name_sub)}
    assert SOURCE_NODE_NAME not in g_overflow.g.nodes()

    # Reverting names -> real indices (what pre_process_antenna_graph does) must
    # not crash in integer space and keeps the pocket downstream.
    reverse = {name: sid for sid, name in mapping.items()}
    g_idx = nx.relabel_nodes(g_overflow.g, reverse, copy=True)
    sdg_idx = Structured_Overload_Distribution_Graph(g_idx)
    assert set(sdg_idx.get_constrained_path().n_aval()) == {1, 2, 3, 4}


def test_antenna_graph_uses_real_flow_colours_and_orientation():
    """A consumer pocket: constraint is the black cut edge, pocket branches are
    blue and oriented root → leaf. The analysis graph spans the full grid (the
    healthy lines are gray and anchor the root); the pocket is what matters."""
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g_overflow, _, _, _, _ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    g = g_overflow.g
    assert SOURCE_NODE_NAME not in g.nodes()
    colours = _edge_colors_by_name(g)
    assert colours["CONSTRAINT"] == "black"
    for branch in ("L1_2", "L1_3", "L3_4"):
        assert colours[branch] == "blue"
    # Healthy main-grid lines carry zero delta → gray.
    for healthy in ("L0_5", "L5_6", "L6_7", "L7_8"):
        assert colours[healthy] == "gray"
    # Constraint oriented from the main-grid root (S0) into the pocket (S1).
    constraint_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("name") == "CONSTRAINT"]
    assert constraint_edges == [("S0", "S1")]
    # Radial: acyclic when collapsed to a simple digraph.
    assert nx.is_directed_acyclic_graph(nx.DiGraph(g))


def test_focus_overflow_graph_on_pocket_trims_to_pocket():
    """The visualization helper restricts a copy to root + pocket for a clean
    render, leaving the analysis graph (full grid) untouched."""
    from expert_op4grid_recommender.graph_analysis.antenna_graph import (
        focus_overflow_graph_on_pocket,
    )
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g_overflow, _, _, _, _ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    focused = focus_overflow_graph_on_pocket(
        g_overflow, obs, ctx["root_sub_id"], ctx["antenna_sub_ids"])

    assert set(focused.g.nodes()) == {"S0", "S1", "S2", "S3", "S4"}
    colours = _edge_colors_by_name(focused.g)
    assert colours["CONSTRAINT"] == "black"
    assert not ({"L0_5", "L5_6", "L6_7", "L7_8"} & set(colours))
    # Analysis graph is untouched (still spans the full grid).
    assert "S8" in g_overflow.g.nodes()


def test_amont_islanded_pocket_orients_pocket_to_root():
    """Regression for the amont-islanded case: when the pocket EXPORTS, the
    edges must point pocket → root and alphaDeesp must classify the pocket as
    amont (upstream), not aval. This is what was previously rendered inverted."""
    obs = _make_exporting_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g_overflow, _, sdg, _, _ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    g = g_overflow.g
    # Constraint now points pocket entry (S1) → main-grid root (S0).
    constraint_edges = [(u, v) for u, v, d in g.edges(data=True) if d.get("name") == "CONSTRAINT"]
    assert constraint_edges == [("S1", "S0")]
    # Pocket branches converge toward the entry (real flow direction), no loops.
    branch_edges = {d["name"]: (u, v) for u, v, d in g.edges(data=True)
                    if d.get("name") in {"L1_2", "L1_3", "L3_4"}}
    assert branch_edges == {"L1_2": ("S2", "S1"), "L1_3": ("S3", "S1"), "L3_4": ("S4", "S3")}
    # The exporting pocket is upstream (amont); the main grid is downstream (aval).
    cp = sdg.get_constrained_path()
    assert set(cp.n_amont()) == {"S1", "S2", "S3", "S4"}
    assert "S0" in cp.n_aval()


def test_pre_process_antenna_graph_reverts_and_returns_empty_simulator_data():
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g_overflow, _, _, mapping, _ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    g_proc, sdg, simulator_data = pre_process_antenna_graph(g_overflow, mapping)

    # No Grid2opSimulation in antenna mode → no simulator data.
    assert simulator_data == {}
    # Nodes reverted to real substation indices; pocket stays downstream.
    assert set(sdg.get_constrained_path().n_aval()) == {1, 2, 3, 4}
    assert all(isinstance(n, (int, np.integer)) for n in g_proc.g.nodes())


def test_no_synthetic_node_so_viz_per_node_setters_dont_keyerror():
    """The graph is built over real substations only, so the viewer's per-node
    setters (which index dicts keyed by obs.name_sub for every node) never hit a
    synthetic-node KeyError."""
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g_overflow, _, _, _, _ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    assert set(g_overflow.g.nodes()) <= set(obs.name_sub)
    # Exact alphaDeesp call performed by make_overflow_graph_visualization.
    number_nodal_dict = {sub_name: 1 for sub_name in obs.name_sub}
    g_overflow.set_electrical_node_number(number_nodal_dict)  # must not raise



def _make_injection_only_discoverer(obs, antenna_mode):
    """Build an ActionDiscoverer on the (integer-reverted) antenna graph."""
    from expert_op4grid_recommender.action_evaluation.classifier import ActionClassifier
    from expert_op4grid_recommender.action_evaluation.discovery import ActionDiscoverer

    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g_overflow, _, _, mapping, meta = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])
    g_proc, sdg, _ = pre_process_antenna_graph(g_overflow, mapping)

    fake_env = types.SimpleNamespace(action_space=lambda d: types.SimpleNamespace(spec=d))
    return ActionDiscoverer(
        env=fake_env, obs=obs, obs_defaut=obs,
        classifier=ActionClassifier(fake_env.action_space),
        timestep=0, lines_defaut=["CONSTRAINT"],
        lines_overloaded_ids=[ctx["constraint_line_id"]],
        act_reco_maintenance=None, non_connected_reconnectable_lines=[],
        all_disconnected_lines=[], dict_action={}, actions_unfiltered=set(),
        hubs=[], g_overflow=g_proc, g_distribution_graph=sdg, simulator_data={},
        check_action_simulation=False, antenna_mode=antenna_mode, antenna_meta=meta,
    )


def test_orchestrator_antenna_mode_runs_injection_only():
    obs = _make_obs()
    discoverer = _make_injection_only_discoverer(obs, antenna_mode=True)

    called = []
    topological = {
        "verify_relevant_reconnections": "reco",
        "find_relevant_node_merging": "close",
        "find_relevant_node_splitting": "open",
        "find_relevant_disconnections": "disco",
        "find_relevant_pst_actions": "pst",
    }
    injection = {
        "find_relevant_load_shedding": "ls",
        "find_relevant_renewable_curtailment": "rc",
        "find_relevant_redispatch": "redispatch",
    }
    for meth, token in {**topological, **injection}.items():
        setattr(discoverer, meth, (lambda t: (lambda *a, **k: called.append(t)))(token))

    discoverer.discover_and_prioritize(n_action_max=5)

    # Topological families are filtered out in antenna mode.
    assert not (set(called) & set(topological.values()))
    # Load shedding fires (the pocket is a net consumer with loads).
    assert "ls" in called
    # The redispatch gate is open too (pocket nodes available).
    assert "redispatch" in called


def test_orchestrator_targets_pocket_when_it_is_amont():
    """Producer pocket (amont-islanded): the pocket is classified amont, yet the
    injection methods must still receive the pocket substations (targeted via
    antenna_meta), not the aval root."""
    obs = _make_exporting_obs()
    discoverer = _make_injection_only_discoverer(obs, antenna_mode=True)

    captured = {}
    discoverer.find_relevant_load_shedding = lambda nodes, *a, **k: captured.update(ls=list(nodes))
    discoverer.find_relevant_renewable_curtailment = lambda *a, **k: captured.update(rc=True)
    discoverer.find_relevant_redispatch = lambda up, down, *a, **k: captured.update(
        rd_up=list(up), rd_down=list(down))

    discoverer.discover_and_prioritize(n_action_max=5)

    pocket = set(discoverer.antenna_meta["antenna_sub_ids"])
    # Load shedding targets the pocket (not the aval root sub 0).
    assert set(captured.get("ls", [])) == pocket
    assert 0 not in set(captured.get("ls", []))
    # Redispatch addresses the pocket on both raise/lower sides.
    assert pocket <= set(captured.get("rd_up", [])) | set(captured.get("rd_down", []))


# ---------------------------------------------------------------------------
# Helper-level unit tests (the pieces build_antenna_overflow_graph composes).
# ---------------------------------------------------------------------------

def test_islanded_line_mask_marks_pocket_incident_lines_only():
    """Every line with an endpoint inside the pocket collapses (constraint +
    internal branches); the healthy main-grid lines do not."""
    obs = _make_obs()
    nl = list(obs.name_line)
    ctx = extract_antenna_context(obs, [nl.index("CONSTRAINT")])

    mask = _islanded_line_mask(obs, ctx["antenna_sub_ids"])

    islanded = {nl[i] for i, m in enumerate(mask) if m}
    assert islanded == {"CONSTRAINT", "L1_2", "L1_3", "L3_4"}
    # Healthy main-grid lines are untouched.
    assert not (islanded & {"L0_5", "L5_6", "L6_7", "L7_8"})


def test_build_topo_aggregates_prod_and_load_per_substation():
    """topo['nodes'] carries per-substation prod/load over the full grid."""
    obs = _make_obs(antenna_prod=(2, 200.0))  # 200 MW generation on sub 2
    topo = _build_topo(obs)

    assert len(topo["nodes"]["are_prods"]) == len(obs.name_sub)
    assert len(topo["edges"]["idx_or"]) == len(obs.name_line)
    # Sub 2 has both the 200 MW generator and a 30 MW load.
    assert topo["nodes"]["are_prods"][2] is True
    assert topo["nodes"]["prods_values"][2] == pytest.approx(200.0)
    assert topo["nodes"]["are_loads"][2] is True
    assert topo["nodes"]["loads_values"][2] == pytest.approx(30.0)
    # Original (pre-swap) line orientation is preserved in the topo edges.
    assert topo["edges"]["idx_or"] == [int(s) for s in obs.line_or_to_subid]
    assert topo["edges"]["idx_ex"] == [int(s) for s in obs.line_ex_to_subid]


def test_delta_flow_frame_collapse_yields_minus_initial_on_pocket():
    """With new_flows = initial-with-pocket-zeroed, the pocket branches report
    delta = -|p_or| (blue) and the healthy grid reports delta = 0 (gray)."""
    obs = _make_obs()
    nl = list(obs.name_line)
    constraint = nl.index("CONSTRAINT")

    new_flows = obs.p_or.copy()
    new_flows[_islanded_line_mask(obs, [1, 2, 3, 4])] = 0.0
    df = _compute_delta_flow_frame(obs, new_flows, constraint, threshold=0.05)

    by_name = {r.line_name: r for r in df.itertuples()}
    # Pocket branches: full negative report equal to the initial flow magnitude.
    assert by_name["L1_2"].delta_flows == pytest.approx(-30.0)
    assert by_name["L3_4"].delta_flows == pytest.approx(-10.0)
    assert by_name["CONSTRAINT"].delta_flows == pytest.approx(-60.0)
    # Healthy grid: untouched flow → zero delta.
    assert by_name["L0_5"].delta_flows == pytest.approx(0.0)
    # The frame has one row per line, in line order.
    assert list(df["line_name"]) == nl


def test_delta_flow_frame_gray_threshold_is_relative_to_constraint():
    """A pocket branch whose report falls below ThresholdReportOfLine * the
    constraint report is flagged gray (de-emphasised), like the real builder."""
    obs = _make_obs()
    nl = list(obs.name_line)
    constraint = nl.index("CONSTRAINT")  # report 60
    new_flows = obs.p_or.copy()
    new_flows[_islanded_line_mask(obs, [1, 2, 3, 4])] = 0.0

    # threshold 0.2 → max_overload = 0.2 * 60 = 12 MW. L3_4 (10) < 12 → gray.
    df = _compute_delta_flow_frame(obs, new_flows, constraint, threshold=0.2)
    by_name = {r.line_name: r for r in df.itertuples()}
    assert by_name["L3_4"].gray_edges  # 10 < 12
    assert not by_name["L1_2"].gray_edges  # 30 >= 12


def test_build_returns_full_alphadeesp_frame_columns():
    """df_of_g exposes the same columns the standard builders return."""
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    df, sim, *_ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    assert sim is None  # no Grid2opSimulation in antenna mode
    assert {"idx_or", "idx_ex", "init_flows", "new_flows", "delta_flows",
            "new_flows_swapped", "gray_edges", "line_name"} <= set(df.columns)
    assert len(df) == len(obs.name_line)


def test_producer_pocket_with_real_export_is_amont_and_producer():
    """A pocket whose generation outweighs its load AND physically exports
    (reversed flows) is classified amont with direction='producer'."""
    obs = _make_exporting_obs()
    # Flip the pocket into a net producer: 200 MW gen on sub 2 > 60 MW load.
    obs.gen_to_subid = np.array([2])
    obs.gen_p = np.array([200.0])
    obs.name_gen = np.array(["G0"])

    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g, _, sdg, _, meta = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    assert meta["direction"] == "producer"
    assert meta["total_prod_mw"] == pytest.approx(200.0)
    cp = sdg.get_constrained_path()
    assert set(cp.n_amont()) == {"S1", "S2", "S3", "S4"}
    assert "S0" in cp.n_aval()


def test_focus_helper_preserves_edge_attributes_and_skips_missing_nodes():
    """focus_overflow_graph_on_pocket keeps edge styling and never errors on a
    pocket id that is not in the graph; the source graph stays untouched."""
    obs = _make_obs()
    ctx = extract_antenna_context(obs, [list(obs.name_line).index("CONSTRAINT")])
    _, _, g_overflow, _, _, _, _ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    # Include a bogus pocket id (99) — must be silently skipped.
    focused = focus_overflow_graph_on_pocket(
        g_overflow, obs, ctx["root_sub_id"], ctx["antenna_sub_ids"] + [99])

    assert set(focused.g.nodes()) == {"S0", "S1", "S2", "S3", "S4"}
    # Edge colours / names survive the subgraph copy.
    colours = _edge_colors_by_name(focused.g)
    assert colours["CONSTRAINT"] == "black"
    assert colours["L1_2"] == "blue"
    # The original analysis graph is not mutated.
    assert "S8" in g_overflow.g.nodes()
    assert focused.g is not g_overflow.g


def test_antenna_graph_survives_negative_constraint_flow():
    """The constraint line stored with a negative p_or (ex→or direction) is
    handled by the branch-direction-swap and still ends up black + connecting
    the pocket to the root."""
    obs = _make_obs()
    nl = list(obs.name_line)
    # Force the constraint flow negative without changing topology.
    obs.p_or = obs.p_or.copy()
    obs.p_or[nl.index("CONSTRAINT")] = -60.0

    ctx = extract_antenna_context(obs, [nl.index("CONSTRAINT")])
    _, _, g_overflow, _, sdg, _, _ = build_antenna_overflow_graph(
        obs, ctx["constraint_line_id"], ctx["antenna_sub_ids"], ctx["root_sub_id"])

    colours = _edge_colors_by_name(g_overflow.g)
    assert colours["CONSTRAINT"] == "black"
    # Still a valid radial structure with a usable constrained path.
    assert sdg.get_constrained_path() is not None
