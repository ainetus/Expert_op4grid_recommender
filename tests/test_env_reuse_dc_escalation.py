# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Regression tests for the reused-env DC-escalation contamination bug (issue #6).

When an overload-disconnection load flow diverges, the analysis pipeline calls
``switch_to_dc_load_flow_pypowsybl``, which escalates the *shared* pypowsybl env
to DC for a single analysis: it flips ``NetworkManager._default_dc = True`` and
DC-solves the base variant (leaving every bus ``v_mag = NaN`` — a DC load flow
computes no voltage magnitudes). Because one ``SimulationEnvironment`` is reused
across analyses (batch classifiers, long-lived services, the Co-Study4Grid game
backend), that escalation used to persist: the next analysis ran every load flow
in DC (branch currents not populated → ``rho ≈ 0``), so real overloads silently
vanished — a silent-correctness bug.

The fix marks the escalation (``env._dc_escalation_pending``) and has
``run_analysis_step1`` restore the configured baseline load-flow mode and an
AC-solved (energised) base variant when it reuses an escalated env, via
``SimulationEnvironment.reset_loadflow_mode_to_baseline``.

These tests use the built-in IEEE-9 network so they need neither the France
grid fixture nor the (deprecated) ``pypowsybl2grid`` bridge.
"""
from __future__ import annotations

import numpy as np
import pytest


def _base_vmag_nan_count(nm) -> int:
    """NaN ``v_mag`` count on the base variant (the corruption signature)."""
    cur = nm.network.get_working_variant_id()
    nm.set_working_variant(nm.base_variant_id)
    try:
        return int(np.isnan(nm.network.get_buses()["v_mag"].values).sum())
    finally:
        nm.network.set_working_variant(cur)


def _make_ieee9_env():
    pp = pytest.importorskip("pypowsybl")
    from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment
    return SimulationEnvironment(network=pp.network.create_ieee9())


# ---------------------------------------------------------------------------
# SimulationEnvironment.reset_loadflow_mode_to_baseline
# ---------------------------------------------------------------------------

def test_fresh_env_starts_ac_and_energised():
    """A freshly built env is in AC mode with an energised base variant."""
    env = _make_ieee9_env()
    assert env._use_dc is False
    assert env.network_manager._default_dc is False
    assert env._dc_escalation_pending is False
    # AC load flow computes voltage magnitudes for every bus.
    assert _base_vmag_nan_count(env.network_manager) == 0


def test_reset_loadflow_mode_to_baseline_reenergises_base_after_dc():
    """DC-solving the base de-energises it (v_mag=NaN); the baseline restore
    puts the env back into AC mode with an energised base variant."""
    env = _make_ieee9_env()
    nm = env.network_manager
    n_buses = len(nm.network.get_buses())
    assert n_buses > 0

    # Emulate the transient DC escalation switch_to_dc performs on the SHARED env.
    nm._default_dc = True
    env._use_dc = True
    env._dc_escalation_pending = True
    nm.reset_to_base()
    env._ensure_valid_state()  # runs a DC load flow (respects _default_dc)
    # DC load flow leaves every bus v_mag NaN.
    assert _base_vmag_nan_count(nm) == n_buses

    # The fix: restore the configured baseline (AC) mode + a valid base state.
    env.reset_loadflow_mode_to_baseline(dc=False)

    assert env._use_dc is False
    assert nm._default_dc is False
    assert env._dc_escalation_pending is False
    # Base variant is energised again — AC solved, no NaN voltages.
    assert _base_vmag_nan_count(nm) == 0


def test_reset_loadflow_mode_to_baseline_honours_dc_baseline():
    """When the configured baseline is DC, the restore keeps DC mode (it does
    not force AC) — it only undoes an *unexpected* escalation relative to the
    caller-supplied baseline."""
    env = _make_ieee9_env()
    nm = env.network_manager
    env.reset_loadflow_mode_to_baseline(dc=True)
    assert env._use_dc is True
    assert nm._default_dc is True
    assert env._dc_escalation_pending is False


# ---------------------------------------------------------------------------
# switch_to_dc_load_flow_pypowsybl flags the escalation
# ---------------------------------------------------------------------------

def test_switch_to_dc_flags_escalation_and_deenergises_base():
    """switch_to_dc escalates a (reused) env to DC and marks it so a later
    analysis can undo it. It also DC-solves base (v_mag=NaN)."""
    from expert_op4grid_recommender.environment_pypowsybl import (
        switch_to_dc_load_flow_pypowsybl,
    )

    env = _make_ieee9_env()
    nm = env.network_manager
    assert env._dc_escalation_pending is False
    assert _base_vmag_nan_count(nm) == 0

    first_line = nm.name_line[0]
    # No overloads to keep, no maintenance — just exercise the DC switch.
    switch_to_dc_load_flow_pypowsybl(env, [first_line], [], [])

    assert nm._default_dc is True
    assert env._dc_escalation_pending is True
    # Base variant is DC-solved → de-energised (all v_mag NaN).
    assert _base_vmag_nan_count(nm) == len(nm.network.get_buses())


# ---------------------------------------------------------------------------
# End-to-end: run_analysis_step1 sanitises a reused, escalated env
# ---------------------------------------------------------------------------

def test_step1_restores_reused_escalated_env():
    """The core bug: after a prior analysis escalated a reused env to DC,
    run_analysis_step1 on the SAME env must restore the AC baseline (energised
    base + _default_dc=False) instead of silently inheriting DC mode."""
    from expert_op4grid_recommender.environment_pypowsybl import (
        switch_to_dc_load_flow_pypowsybl,
    )
    from expert_op4grid_recommender.pipeline import run_analysis_step1
    from expert_op4grid_recommender.backends import Backend

    env = _make_ieee9_env()
    nm = env.network_manager

    # A prior analysis escalated this shared env to DC (the contamination).
    switch_to_dc_load_flow_pypowsybl(env, [nm.name_line[0]], [], [])
    assert env._dc_escalation_pending is True
    assert nm._default_dc is True
    assert _base_vmag_nan_count(nm) == len(nm.network.get_buses())

    # Reuse the env for a NEW analysis via prebuilt_env_context.
    prebuilt = {
        "env": env,
        "path_chronic": "",
        "chronic_name": "ieee9",
        "custom_layout": None,
        "lines_non_reconnectable": [],
        "lines_we_care_about": list(env.name_line),
    }
    run_analysis_step1(
        analysis_date=None,
        current_timestep=0,
        current_lines_defaut=[nm.name_line[1]],
        backend=Backend.PYPOWSYBL,
        dict_action={},
        prebuilt_env_context=prebuilt,
    )

    # The escalation has been undone: AC mode, energised base, flag cleared.
    assert env._dc_escalation_pending is False
    assert nm._default_dc is False
    assert env._use_dc is False
    assert _base_vmag_nan_count(nm) == 0


def test_step1_leaves_unescalated_reused_env_untouched():
    """The sanitisation only fires for an escalated env: a normal reused env
    keeps the fast path (plain reset_to_base, no extra baseline re-solve)."""
    from expert_op4grid_recommender.pipeline import run_analysis_step1
    from expert_op4grid_recommender.backends import Backend

    env = _make_ieee9_env()
    nm = env.network_manager
    assert env._dc_escalation_pending is False

    prebuilt = {
        "env": env,
        "path_chronic": "",
        "chronic_name": "ieee9",
        "custom_layout": None,
        "lines_non_reconnectable": [],
        "lines_we_care_about": list(env.name_line),
    }
    run_analysis_step1(
        analysis_date=None,
        current_timestep=0,
        current_lines_defaut=[nm.name_line[1]],
        backend=Backend.PYPOWSYBL,
        dict_action={},
        prebuilt_env_context=prebuilt,
    )

    assert env._dc_escalation_pending is False
    assert nm._default_dc is False
    assert _base_vmag_nan_count(nm) == 0
