# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# SPDX-License-Identifier: MPL-2.0
"""Unit tests for the backend-agnostic injection helpers (``utils/injections``).

grid2op and pypowsybl observations disagree on the generator sign convention
and on how generators / loads are located; these tests pin both flavours, plus
one check on a real pypowsybl network (IEEE 14).
"""

import types

import numpy as np
import pytest

from expert_op4grid_recommender.utils.injections import (
    consumption_mw,
    gen_to_subid,
    injections_by_sub,
    load_to_subid,
    production_mw,
    uses_receptor_sign_convention,
)


def _grid2op_like_obs():
    """3 substations; generators on subs 0 and 2, loads on subs 1 and 2."""
    return types.SimpleNamespace(
        name_sub=np.array(["S0", "S1", "S2"]),
        gen_to_subid=np.array([0, 2]), gen_p=np.array([100.0, 40.0]),
        load_to_subid=np.array([1, 2]), load_p=np.array([90.0, 50.0]),
    )


def _pypowsybl_like_obs(gen_p=(-100.0, -40.0)):
    """Same grid as ``_grid2op_like_obs`` as PypowsyblObservation exposes it:
    receptor sign convention, no ``gen_to_subid`` / ``load_to_subid``."""
    gens = {0: [0], 1: [], 2: [1]}
    loads = {0: [], 1: [0], 2: [1]}

    def get_obj_connect_to(substation_id):
        return {"generators_id": gens[substation_id], "loads_id": loads[substation_id],
                "lines_or_id": [], "lines_ex_id": []}

    return types.SimpleNamespace(
        name_sub=np.array(["S0", "S1", "S2"]),
        gen_p=np.array(gen_p), load_p=np.array([90.0, 50.0]),
        get_obj_connect_to=get_obj_connect_to,
        _network_manager=object(),
    )


def test_grid2op_convention_counts_production_positive():
    obs = _grid2op_like_obs()
    assert not uses_receptor_sign_convention(obs)
    np.testing.assert_allclose(production_mw(obs), [100.0, 40.0])
    np.testing.assert_allclose(consumption_mw(obs), [90.0, 50.0])
    prod, load = injections_by_sub(obs)
    np.testing.assert_allclose(prod, [100.0, 0.0, 40.0])
    np.testing.assert_allclose(load, [0.0, 90.0, 50.0])


def test_pypowsybl_convention_flips_sign_and_locates_elements():
    obs = _pypowsybl_like_obs()
    assert uses_receptor_sign_convention(obs)
    np.testing.assert_allclose(production_mw(obs), [100.0, 40.0])
    np.testing.assert_array_equal(gen_to_subid(obs), [0, 2])
    np.testing.assert_array_equal(load_to_subid(obs), [1, 2])
    prod, load = injections_by_sub(obs)
    np.testing.assert_allclose(prod, [100.0, 0.0, 40.0])
    np.testing.assert_allclose(load, [0.0, 90.0, 50.0])


def test_absorbing_generator_counts_as_negative_production():
    """A pump (pypowsybl ``p > 0``) withdraws power: negative production."""
    obs = _pypowsybl_like_obs(gen_p=(-100.0, 25.0))
    np.testing.assert_allclose(production_mw(obs), [100.0, -25.0])


def test_nan_injections_count_as_zero():
    obs = _grid2op_like_obs()
    obs.gen_p = np.array([np.nan, 40.0])
    prod, _ = injections_by_sub(obs)
    np.testing.assert_allclose(prod, [0.0, 0.0, 40.0])


def test_unlocatable_elements_are_skipped():
    """No ``*_to_subid`` and no ``get_obj_connect_to``: located as -1, not summed."""
    obs = types.SimpleNamespace(name_sub=np.array(["S0", "S1"]),
                                gen_p=np.array([10.0]), load_p=np.array([5.0]))
    np.testing.assert_array_equal(gen_to_subid(obs), [-1])
    prod, load = injections_by_sub(obs)
    np.testing.assert_allclose(prod, [0.0, 0.0])
    np.testing.assert_allclose(load, [0.0, 0.0])


def test_real_pypowsybl_observation_ieee14():
    """On a real PypowsyblObservation the totals match the network's own
    production / consumption (raw ``gen_p`` sums negative)."""
    pp = pytest.importorskip("pypowsybl")
    from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment

    obs = SimulationEnvironment(network=pp.network.create_ieee14()).get_obs()
    assert not hasattr(obs, "gen_to_subid")
    assert float(np.sum(obs.gen_p)) < 0.0

    prod, load = injections_by_sub(obs)
    assert prod.sum() == pytest.approx(-float(np.sum(obs.gen_p)))
    assert load.sum() == pytest.approx(float(np.sum(obs.load_p)))
    assert prod.sum() > load.sum() > 0.0  # production covers load + losses
    for sub_id in range(len(obs.name_sub)):
        gens = obs.get_obj_connect_to(substation_id=sub_id)["generators_id"]
        assert prod[sub_id] == pytest.approx(-float(np.sum(obs.gen_p[gens])))
