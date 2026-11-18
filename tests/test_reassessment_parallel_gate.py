# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Tests for the reassessment parallel-vs-serial gate (0.2.7.post1).

The parallel per-action reassessment clones the whole network per worker, a
fixed overhead only amortized above ~3-4 cores. Below the threshold the serial
path is faster (measured: ~42 s vs ~9 s for the same 15-action pan-European
case on a 2-vCPU Space vs a many-core Mac). These tests pin the gate so a
2-vCPU host stays serial while ≥4-core hosts keep the speed-up, plus the env
override.

Since 0.3.4 auto mode also needs ``_MIN_ACTIONS_PER_WORKER`` (6) actions per
worker in slow mode as in fast mode, and the pool is capped by the
``REASSESSMENT_MAX_WORKERS`` knob and by the available memory: on the
full-France grid 15 actions on 10 workers took 25 s and peaked at 9.2 GB,
against 15 s and 3 GB serially.
"""
from __future__ import annotations

import io
from types import SimpleNamespace

import numpy as np
import pytest

from expert_op4grid_recommender import config
from expert_op4grid_recommender.utils import reassessment as R
from expert_op4grid_recommender.utils.reassessment import (
    _DEFAULT_MIN_PARALLEL_WORKERS,
    _MIN_ACTIONS_PER_WORKER,
    _MIN_PARALLEL_WORKERS_ENV,
    _min_parallel_workers,
    _should_parallelize_reassessment,
)


class TestMinParallelWorkers:
    def test_default_is_four(self, monkeypatch):
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _min_parallel_workers() == _DEFAULT_MIN_PARALLEL_WORKERS == 4

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(_MIN_PARALLEL_WORKERS_ENV, "2")
        assert _min_parallel_workers() == 2
        monkeypatch.setenv(_MIN_PARALLEL_WORKERS_ENV, "99")
        assert _min_parallel_workers() == 99

    def test_env_floored_at_two(self, monkeypatch):
        # A single worker is strictly worse than serial → never below 2.
        monkeypatch.setenv(_MIN_PARALLEL_WORKERS_ENV, "1")
        assert _min_parallel_workers() == 2
        monkeypatch.setenv(_MIN_PARALLEL_WORKERS_ENV, "0")
        assert _min_parallel_workers() == 2

    def test_invalid_env_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv(_MIN_PARALLEL_WORKERS_ENV, "not-a-number")
        assert _min_parallel_workers() == _DEFAULT_MIN_PARALLEL_WORKERS


class TestShouldParallelize:
    def test_two_cores_stays_serial_by_default(self, monkeypatch):
        # HuggingFace 2-vCPU Space: workers=2 → serial (the fix).
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(True, workers=2, n_actions=15) is False

    def test_four_cores_parallelizes(self, monkeypatch):
        # 4 workers with enough actions to amortize their network copies.
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(True, workers=4, n_actions=24) is True

    def test_many_cores_parallelizes(self, monkeypatch):
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(True, workers=9, n_actions=54) is True

    def test_grid2op_never_parallelizes(self, monkeypatch):
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(False, workers=16, n_actions=15) is False

    def test_single_action_stays_serial(self, monkeypatch):
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(True, workers=8, n_actions=1) is False

    def test_env_can_force_serial_on_any_host(self, monkeypatch):
        monkeypatch.setenv(_MIN_PARALLEL_WORKERS_ENV, "99")
        assert _should_parallelize_reassessment(True, workers=16, n_actions=64) is False

    def test_env_can_restore_aggressive_behaviour(self, monkeypatch):
        # Set to 2 → the pre-0.2.7.post1 behaviour (parallel from 2 workers).
        monkeypatch.setenv(_MIN_PARALLEL_WORKERS_ENV, "2")
        assert _should_parallelize_reassessment(True, workers=2, n_actions=15) is True


@pytest.mark.parametrize("workers,expected", [(1, False), (2, False), (3, False),
                                              (4, True), (8, True)])
def test_default_threshold_boundary(monkeypatch, workers, expected):
    monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
    assert _should_parallelize_reassessment(True, workers=workers, n_actions=100) is expected


class TestActionsPerWorkerGate:
    """The per-worker network copy is only amortized when each worker runs
    enough actions, whatever the LF mode → realistic counts stay serial.
    """

    def test_threshold_is_six(self):
        assert _MIN_ACTIONS_PER_WORKER == 6

    def test_realistic_count_stays_serial(self, monkeypatch):
        # The France MERLAL62SOULL case: 15 actions on 10 workers (20 cores)
        # took 25 s and peaked at 9.2 GB, against 15 s and 3 GB serially.
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(True, workers=10, n_actions=15) is False

    def test_four_workers_need_six_actions_each(self, monkeypatch):
        # 21 actions on 4 workers took 22.4 s against 20.4 s serially.
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(True, workers=4, n_actions=21) is False
        assert _should_parallelize_reassessment(True, workers=4, n_actions=23) is False
        assert _should_parallelize_reassessment(True, workers=4, n_actions=24) is True

    def test_many_actions_parallelise(self, monkeypatch):
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(True, workers=10, n_actions=59) is False
        assert _should_parallelize_reassessment(True, workers=10, n_actions=60) is True

    def test_below_core_threshold_still_serial(self, monkeypatch):
        monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
        assert _should_parallelize_reassessment(True, workers=2, n_actions=100) is False

    def test_force_parallel_overrides_the_gate(self, monkeypatch):
        # An explicit REASSESSMENT_PARALLEL=True still parallelises few actions.
        monkeypatch.setattr(
            "expert_op4grid_recommender.utils.reassessment.config."
            "REASSESSMENT_PARALLEL", True, raising=False)
        assert _should_parallelize_reassessment(True, workers=4, n_actions=15) is True


class TestMaxWorkersKnob:
    """``REASSESSMENT_MAX_WORKERS`` caps the pool, i.e. the network copies."""

    def test_default_cap_is_ten(self):
        assert config.REASSESSMENT_MAX_WORKERS == R._MAX_REASSESSMENT_WORKERS == 10

    def test_knob_caps_the_pool(self, monkeypatch):
        monkeypatch.setattr(R.config, "REASSESSMENT_MAX_WORKERS", 3)
        monkeypatch.setattr(R, "_effective_cpu_count", lambda: 16)
        assert R._reassessment_worker_count(15) == (16, 3)

    def test_one_worker_keeps_it_serial_even_when_forced(self, monkeypatch):
        monkeypatch.setattr(R.config, "REASSESSMENT_MAX_WORKERS", 1)
        monkeypatch.setattr(R.config, "REASSESSMENT_PARALLEL", True)
        monkeypatch.setattr(R, "_effective_cpu_count", lambda: 16)
        _, workers = R._reassessment_worker_count(30)
        assert workers == 1
        assert _should_parallelize_reassessment(True, workers=workers, n_actions=30) is False

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("EXPERT_OP4GRID_REASSESSMENT_MAX_WORKERS", "4")
        assert config.Settings().REASSESSMENT_MAX_WORKERS == 4

    def test_rejects_zero(self, monkeypatch):
        monkeypatch.setenv("EXPERT_OP4GRID_REASSESSMENT_MAX_WORKERS", "0")
        with pytest.raises(ValueError):
            config.Settings()


# ---------------------------------------------------------------------
# reassess_prioritized_actions — the gate and the memory cap end to end
# ---------------------------------------------------------------------

class _FakeObs:
    """Observation stub: ``simulate`` lowers every rho by 10 %."""

    def __init__(self, rho, network_manager=None):
        self.rho = np.asarray(rho, dtype=float)
        self.name_line = np.array([f"L{i}" for i in range(len(self.rho))])
        self._network_manager = network_manager
        self._variant_id = "N-1"
        self._thermal_limits = {}
        self.simulated = []

    def simulate(self, action, time_step=0, keep_variant=False, fast_mode=False):
        self.simulated.append(action)
        return _FakeObs(self.rho * 0.9), 0.0, False, {"exception": None}


class _FakeNetworkManager:
    _default_dc = False
    base_variant_id = "base"

    def __init__(self, buffer_nbytes):
        self.buffer_nbytes = buffer_nbytes
        self.saves = 0
        self.network = self

    def set_working_variant(self, variant_id):
        pass

    def save_to_binary_buffer(self):
        self.saves += 1
        return io.BytesIO(bytes(self.buffer_nbytes))


def _context(n_actions, buffer_nbytes=1000):
    nm = _FakeNetworkManager(buffer_nbytes)
    obs_defaut = _FakeObs([1.2, 0.5, 0.3], network_manager=nm)
    actions = {f"a{i}": f"act{i}" for i in range(n_actions)}
    context = {
        "backend": SimpleNamespace(create_default_action=lambda *a: None),
        "env": SimpleNamespace(action_space=None),
        "obs": _FakeObs([0.8, 0.5, 0.3]),
        "obs_simu_defaut": obs_defaut,
        "current_timestep": 0,
        "current_lines_defaut": ["L0"],
        "lines_overloaded_ids": [0],
        "act_reco_maintenance": None,
        "lines_we_care_about": None,
        "pre_existing_rho": {},
        "dict_action": {},
        "is_pypowsybl": True,
        "actual_fast_mode": False,
    }
    return actions, context, nm


@pytest.fixture
def worker_copies(monkeypatch):
    """Count the private network copies the parallel path builds."""
    copies = []

    def _fake_copy(binary_buffer, thermal_limits):
        copies.append(binary_buffer.getbuffer().nbytes)
        return None, _FakeObs([1.2, 0.5, 0.3])

    monkeypatch.setattr(R, "_make_worker_baseline_obs", _fake_copy)
    monkeypatch.setattr(R, "_effective_cpu_count", lambda: 16)
    monkeypatch.delenv(_MIN_PARALLEL_WORKERS_ENV, raising=False)
    return copies


def _available_for(workers, buffer_nbytes):
    """Available memory whose budget holds ``workers`` copies (and a half)."""
    per_copy = R._estimate_worker_memory_bytes(buffer_nbytes)
    return int(per_copy * (workers + 0.5) / R._WORKER_MEMORY_BUDGET)


def test_auto_few_actions_stays_serial_without_copying(monkeypatch, worker_copies):
    monkeypatch.setattr(R.config, "REASSESSMENT_PARALLEL", None)
    actions, context, nm = _context(15)
    detailed, _ = R.reassess_prioritized_actions(actions, context)
    assert list(detailed) == list(actions)
    assert context["reassessment_parallelism"]["parallel"] is False
    assert nm.saves == 0 and worker_copies == []
    assert context["obs_simu_defaut"].simulated == list(actions.values())


def test_auto_many_actions_runs_the_full_pool(monkeypatch, worker_copies):
    monkeypatch.setattr(R.config, "REASSESSMENT_PARALLEL", None)
    monkeypatch.setattr(R, "_available_memory_bytes", lambda: None)
    actions, context, nm = _context(60)
    detailed, _ = R.reassess_prioritized_actions(actions, context)
    assert list(detailed) == list(actions)
    assert context["reassessment_parallelism"]["workers"] == 10
    assert len(worker_copies) == 10


def test_memory_caps_a_forced_pool(monkeypatch, worker_copies, capsys):
    monkeypatch.setattr(R.config, "REASSESSMENT_PARALLEL", True)
    monkeypatch.setattr(R, "_available_memory_bytes", lambda: _available_for(3, 1000))
    actions, context, nm = _context(15, buffer_nbytes=1000)
    detailed, _ = R.reassess_prioritized_actions(actions, context)
    assert list(detailed) == list(actions)
    assert context["reassessment_parallelism"]["workers"] == 3
    assert worker_copies == [1000] * 3
    assert "pool capped from 10 by the available memory" in capsys.readouterr().out


def test_memory_cap_below_the_core_threshold_goes_serial(monkeypatch, worker_copies):
    # Auto mode: 60 actions would run on 10 workers, but memory holds 3 copies,
    # below the 4-worker threshold → serial on the main network.
    monkeypatch.setattr(R.config, "REASSESSMENT_PARALLEL", None)
    monkeypatch.setattr(R, "_available_memory_bytes", lambda: _available_for(3, 1000))
    actions, context, nm = _context(60, buffer_nbytes=1000)
    detailed, _ = R.reassess_prioritized_actions(actions, context)
    assert list(detailed) == list(actions)
    assert context["reassessment_parallelism"]["parallel"] is False
    assert worker_copies == []
    assert len(context["obs_simu_defaut"].simulated) == 60


def test_no_room_for_two_copies_stays_serial_even_when_forced(monkeypatch, worker_copies):
    monkeypatch.setattr(R.config, "REASSESSMENT_PARALLEL", True)
    monkeypatch.setattr(R, "_available_memory_bytes", lambda: _available_for(1, 1000))
    actions, context, nm = _context(15, buffer_nbytes=1000)
    R.reassess_prioritized_actions(actions, context)
    assert context["reassessment_parallelism"]["workers"] == 1
    assert worker_copies == []
