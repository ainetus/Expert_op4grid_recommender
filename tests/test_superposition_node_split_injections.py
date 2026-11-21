# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# SPDX-License-Identifier: MPL-2.0
"""Node split that moves a generator to bus 2, on a real pypowsybl network.

``PypowsyblObservation.sub_topology()`` used to read the load and generator
buses lazily from the network's *current* working variant. ``simulate()``
hands the working variant back to the base one, so after a coupler opening
every load and generator of the split substation stayed on bus 1 while its
line ends followed the split. ``get_sub_node1_idsflow`` then counted them on
node 1, the virtual-line flow of the split was wrong, and so were the
superposition betas of every pair involving that split.

The network: voltage level ``A`` has two busbar sections joined by
``A_COUPLER``; ``L_A1`` and the lines to ``B`` / ``D`` sit on section 1,
``G_A2`` and the line to ``C`` on section 2. Opening the coupler leaves both
halves meshed through ``B``, ``C`` and ``D``.
"""
from __future__ import annotations

import numpy as np
import pytest

pp = pytest.importorskip("pypowsybl")
pd = pytest.importorskip("pandas")

from expert_op4grid_recommender.pypowsybl_backend import (  # noqa: E402
    ActionSpace,
    NetworkManager,
    PypowsyblObservation,
)
from expert_op4grid_recommender.utils.superposition import (  # noqa: E402
    compute_combined_pair_superposition,
    get_sub_node1_idsflow,
)


def _split_network():
    n = pp.network.create_empty("split_injections")
    n.create_substations(id=["SA", "SB", "SC", "SD"])
    n.create_voltage_levels(id=["A", "B", "C", "D"], substation_id=["SA", "SB", "SC", "SD"],
                            topology_kind=["NODE_BREAKER"] * 4, nominal_v=[400.0] * 4)
    n.create_busbar_sections(id=["A_BBS1", "A_BBS2", "B_BBS", "C_BBS", "D_BBS"],
                             voltage_level_id=["A", "A", "B", "C", "D"], node=[0, 1, 0, 0, 0])
    n.create_switches(id=["A_COUPLER"], voltage_level_id=["A"], node1=[0], node2=[1],
                      kind=["BREAKER"], open=[False])
    lines = [("L_AB", "A_BBS1", "B_BBS", 10.0), ("L_AD", "A_BBS1", "D_BBS", 12.0),
             ("L_AC", "A_BBS2", "C_BBS", 8.0), ("L_BC", "B_BBS", "C_BBS", 9.0),
             ("L_CD", "C_BBS", "D_BBS", 11.0), ("L_BD", "B_BBS", "D_BBS", 15.0)]
    pp.network.create_line_bays(n, pd.DataFrame.from_records(index="id", data=[
        dict(id=lid, r=0.5, x=x, g1=0.0, b1=0.0, g2=0.0, b2=0.0,
             bus_or_busbar_section_id_1=bbs1, position_order_1=10 * (k + 1),
             bus_or_busbar_section_id_2=bbs2, position_order_2=10 * (k + 1))
        for k, (lid, bbs1, bbs2, x) in enumerate(lines)]))
    pp.network.create_generator_bay(n, pd.DataFrame.from_records(index="id", data=[
        dict(id="G_A2", bus_or_busbar_section_id="A_BBS2", position_order=100, target_p=300.0,
             min_p=0.0, max_p=500.0, target_v=400.0, voltage_regulator_on=True),
        dict(id="G_D", bus_or_busbar_section_id="D_BBS", position_order=100, target_p=200.0,
             min_p=0.0, max_p=1000.0, target_v=400.0, voltage_regulator_on=True)]))
    pp.network.create_load_bay(n, pd.DataFrame.from_records(index="id", data=[
        dict(id="L_A1", bus_or_busbar_section_id="A_BBS1", position_order=110, p0=150.0, q0=10.0),
        dict(id="L_B", bus_or_busbar_section_id="B_BBS", position_order=110, p0=200.0, q0=10.0),
        dict(id="L_C", bus_or_busbar_section_id="C_BBS", position_order=110, p0=150.0, q0=10.0)]))
    return n


@pytest.fixture
def dc_setup():
    """N-state DC observation of the test network (superposition is exact in DC)."""
    nm = NetworkManager(network=_split_network())
    nm._default_dc = True
    action_space = ActionSpace(nm)
    nm.run_load_flow()
    obs = PypowsyblObservation(nm, action_space)
    sub_a = list(nm.name_sub).index("A")
    return nm, action_space, obs, sub_a


@pytest.mark.parametrize("keep_variant", [True, False])
def test_split_observation_puts_generator_on_bus_2(dc_setup, keep_variant):
    nm, action_space, obs, sub_a = dc_setup
    split = action_space({"switches": {"A_COUPLER": True}})
    obs_split, _, _, info = obs.simulate(split, keep_variant=keep_variant)
    assert not info["exception"]
    # simulate() returned the network to the base variant (coupler closed)
    assert nm.network.get_working_variant_id() == nm.base_variant_id

    # sub_topology order: loads [L_A1], gens [G_A2], line origins [L_AB, L_AD, L_AC]
    np.testing.assert_array_equal(obs_split.sub_topology(sub_a), [1, 2, 1, 1, 2])
    np.testing.assert_array_equal(obs.sub_topology(sub_a), [1, 1, 1, 1, 1])

    loads, gens, lines_or, lines_ex = get_sub_node1_idsflow(obs_split, sub_a)
    assert [nm.name_load[i] for i in loads] == ["L_A1"]
    assert gens == []  # G_A2 is on node 2
    assert [nm.name_line[i] for i in lines_or] == ["L_AB", "L_AD"]
    assert lines_ex == []


@pytest.mark.parametrize("line", ["L_AB", "L_BD", "L_CD"])
def test_split_pair_is_exact_in_dc(dc_setup, line):
    nm, action_space, obs, sub_a = dc_setup
    line_idx = list(nm.name_line).index(line)
    obs_split = obs.simulate(action_space({"switches": {"A_COUPLER": True}}), keep_variant=True)[0]
    obs_line = obs.simulate(action_space({"set_line_status": [(line, -1)]}), keep_variant=True)[0]
    obs_pair = obs.simulate(action_space({"switches": {"A_COUPLER": True},
                                          "set_line_status": [(line, -1)]}), keep_variant=True)[0]

    result = compute_combined_pair_superposition(obs, obs_split, obs_line, [], [sub_a], [line_idx], [])

    assert "error" not in result, result.get("error")
    np.testing.assert_allclose(result["p_or_combined"], obs_pair.p_or, atol=1e-6)
