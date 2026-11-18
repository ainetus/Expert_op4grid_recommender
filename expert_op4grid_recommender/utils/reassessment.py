# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Per-action reassessment + combined-pair estimation.

This is the "action-card preparation" stage. It runs AFTER a
recommendation model has produced a flat
``{action_id: action_object}`` mapping and turns each entry into the
rich :class:`SimulatedAction` payload the frontend renders as a card.

Extracted from the historical tail of ``run_analysis_step2_discovery``
so ANY pluggable :class:`RecommenderModel` can reuse it instead of
re-implementing simulation, rho-evolution math, and superposition
combinations.
"""
from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from expert_op4grid_recommender import config
from expert_op4grid_recommender.models.base import SimulatedAction
from expert_op4grid_recommender.utils.helpers import Timer

logger = logging.getLogger(__name__)

#: Default cap on reassessment worker threads, i.e. on private network copies
#: (see ``_reassessment_worker_count``). The ``REASSESSMENT_MAX_WORKERS``
#: config knob (env ``EXPERT_OP4GRID_REASSESSMENT_MAX_WORKERS``) overrides it.
_MAX_REASSESSMENT_WORKERS = 10

#: cgroup control files that expose a container CPU quota (module-level so
#: tests can re-point them at fixtures).
_CGROUP_V2_CPU_MAX = "/sys/fs/cgroup/cpu.max"
_CGROUP_V1_CPU_QUOTA = "/sys/fs/cgroup/cpu/cpu.cfs_quota_us"
_CGROUP_V1_CPU_PERIOD = "/sys/fs/cgroup/cpu/cpu.cfs_period_us"

#: Host and cgroup memory files read by ``_available_memory_bytes`` (same
#: fixture convention as the CPU ones above).
_PROC_MEMINFO = "/proc/meminfo"
_CGROUP_V2_MEMORY_MAX = "/sys/fs/cgroup/memory.max"
_CGROUP_V2_MEMORY_CURRENT = "/sys/fs/cgroup/memory.current"
_CGROUP_V1_MEMORY_LIMIT = "/sys/fs/cgroup/memory/memory.limit_in_bytes"
_CGROUP_V1_MEMORY_USAGE = "/sys/fs/cgroup/memory/memory.usage_in_bytes"

#: cgroup v1 reports "no limit" as a page-aligned ``LLONG_MAX``; anything at or
#: above this is treated as unlimited.
_CGROUP_V1_UNLIMITED = 1 << 60


def _read_cgroup_cpu_quota() -> Optional[float]:
    """Return the cgroup CPU quota expressed in whole CPUs, or ``None``.

    A container CPU limit (e.g. a 2-vCPU cloud box on a 16-core host) is
    invisible to :func:`os.cpu_count`, which reports the *host* core count.
    The cgroup CFS quota is the only reliable signal on such a host. Reads
    cgroup v2 (``/sys/fs/cgroup/cpu.max``) first, then cgroup v1
    (``cpu.cfs_quota_us`` / ``cpu.cfs_period_us``). Returns ``None`` when the
    quota is unset / unlimited or the files are unreadable (e.g. non-Linux).
    """
    # cgroup v2 — "<quota> <period>"; quota == "max" means unlimited.
    try:
        with open(_CGROUP_V2_CPU_MAX, encoding="ascii") as fh:
            parts = fh.read().split()
        if parts and parts[0] != "max":
            quota = float(parts[0])
            period = float(parts[1]) if len(parts) > 1 else 100000.0
            if quota > 0 and period > 0:
                return quota / period
    except (OSError, ValueError, IndexError):
        pass
    # cgroup v1 — separate quota / period files; quota == -1 means unlimited.
    try:
        with open(_CGROUP_V1_CPU_QUOTA, encoding="ascii") as fh:
            quota = float(fh.read().strip())
        with open(_CGROUP_V1_CPU_PERIOD, encoding="ascii") as fh:
            period = float(fh.read().strip())
        if quota > 0 and period > 0:
            return quota / period
    except (OSError, ValueError):
        pass
    return None


def _effective_cpu_count() -> int:
    """Best-effort count of CPUs *actually usable* by this process.

    :func:`os.cpu_count` reports the host core count, which over-counts inside
    a CPU-limited container. We take the minimum of every signal we can read:
    the host count, the scheduler affinity mask (cpuset pinning), and the
    cgroup CPU quota (cfs). Floored at 1.
    """
    candidates = [os.cpu_count() or 1]
    if hasattr(os, "sched_getaffinity"):
        try:
            candidates.append(len(os.sched_getaffinity(0)))
        except OSError:
            pass
    quota = _read_cgroup_cpu_quota()
    if quota is not None:
        candidates.append(max(1, int(quota)))
    return max(1, min(candidates))


def _read_cgroup_memory_headroom() -> Optional[int]:
    """Return the cgroup memory limit minus the current usage in bytes, or ``None``.

    Reads cgroup v2 (``memory.max`` / ``memory.current``) first, then cgroup v1
    (``memory.limit_in_bytes`` / ``memory.usage_in_bytes``). Returns ``None``
    when no limit is set or the files are unreadable (e.g. outside a
    container). The usage includes the page cache, so the headroom errs low.
    """
    # cgroup v2 — memory.max == "max" means unlimited.
    try:
        with open(_CGROUP_V2_MEMORY_MAX, encoding="ascii") as fh:
            raw = fh.read().strip()
        if raw != "max":
            with open(_CGROUP_V2_MEMORY_CURRENT, encoding="ascii") as fh:
                return max(0, int(raw) - int(fh.read().strip()))
    except (OSError, ValueError):
        pass
    # cgroup v1 — an unlimited limit reads as a page-aligned LLONG_MAX.
    try:
        with open(_CGROUP_V1_MEMORY_LIMIT, encoding="ascii") as fh:
            limit = int(fh.read().strip())
        if limit < _CGROUP_V1_UNLIMITED:
            with open(_CGROUP_V1_MEMORY_USAGE, encoding="ascii") as fh:
                return max(0, limit - int(fh.read().strip()))
    except (OSError, ValueError):
        pass
    return None


def _available_memory_bytes() -> Optional[int]:
    """Best-effort memory still available to this process, in bytes.

    Like :func:`os.cpu_count`, ``/proc/meminfo`` describes the *host* inside a
    container, so we take the minimum of its ``MemAvailable`` and the cgroup
    headroom. Returns ``None`` when neither is readable (e.g. non-Linux); the
    memory then does not cap the reassessment pool.
    """
    candidates = []
    try:
        with open(_PROC_MEMINFO, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    candidates.append(int(line.split()[1]) * 1024)
                    break
    except (OSError, ValueError, IndexError):
        pass
    headroom = _read_cgroup_memory_headroom()
    if headroom is not None:
        candidates.append(headroom)
    return min(candidates) if candidates else None


#: Default minimum worker count below which the parallel reassessment is NOT
#: worth it. The parallel path clones the whole network per worker
#: (``save``/``load_from_binary_buffer`` + ``SimulationEnvironment`` + obs
#: build) — a fixed overhead the serial path never pays. That overhead is only
#: amortized above ~3-4 usable cores; v0.2.6 measured **+1.43x on a 4-core
#: host**, but on a **2-vCPU** host (e.g. a small HuggingFace Space) the 2
#: clones + GIL contention on the Python-side observation build make it a net
#: *loss* vs. serial. So we stay serial below the threshold.
_DEFAULT_MIN_PARALLEL_WORKERS = 4

#: Environment override for operators who know their hardware. Takes precedence
#: over the ``REASSESSMENT_MIN_PARALLEL_CORES`` config knob.
_MIN_PARALLEL_WORKERS_ENV = "EXPERT_OP4GRID_MIN_PARALLEL_REASSESS_WORKERS"

#: Auto mode parallelises only when every worker re-simulates at least this
#: many actions, in fast and slow mode alike (it was fast mode only until
#: 0.3.4). Before its first action, the parallel path pays a tax the serial
#: path never pays: it serialises the N-1 network to a binary buffer on one
#: thread, then every worker loads a private copy, builds a
#: ``SimulationEnvironment`` and solves a baseline LF. On the full-France grid
#: (Vendée case ``MERLAL62SOULL``, ~1 s per action in fast and slow mode) that
#: tax is 7.5-8.5 s for the buffer plus 5.7 s (2 workers) to 13-15 s (10
#: workers: the copies load concurrently but contend), and the per-action LFs
#: slow down 1.1x (2 workers) to 2.8x (10). 15 actions on 10 workers took 25 s
#: instead of 15 s serially, with a 9.2 GB peak instead of 3 GB, and 24
#: actions on 4 workers (6 each) only broke even: 23.4 s against 24.0 s. On
#: the small test grid the per-action work is Python-bound (GIL) and parallel
#: never broke even. Realistic prioritized-action counts (10-20) thus stay
#: serial on 4 workers or more.
_MIN_ACTIONS_PER_WORKER = 6

#: Memory one worker costs, estimated from the size of the binary buffer its
#: network copy is loaded from: a multiple of the buffer (the in-memory
#: network, the worker's ``SimulationEnvironment`` and observations, and the
#: loader's garbage until the next collection) plus a fixed part. The binary
#: format is compact: full France serialises to 7.5 MB but costs 0.7-1.25 GB
#: per worker (peak-RSS growth), the small test grid 0.13 MB and 50-75 MB. The
#: estimate gives ~1.25 GB and ~85 MB.
_WORKER_MEMORY_PER_BUFFER_BYTE = 160
_WORKER_MEMORY_FIXED_BYTES = 64 * 1024 ** 2

#: Share of the available memory the worker copies may take. The copies
#: outlive the reassessment: each action's observation keeps the network copy
#: it was simulated on, so they live as long as the analysis result.
_WORKER_MEMORY_BUDGET = 0.5


def _min_parallel_workers() -> int:
    """Minimum worker count to enable parallel reassessment.

    Resolution order: the ``EXPERT_OP4GRID_MIN_PARALLEL_REASSESS_WORKERS`` env
    var when set, else the ``REASSESSMENT_MIN_PARALLEL_CORES`` config knob
    (default :data:`_DEFAULT_MIN_PARALLEL_WORKERS`). Floored at 2 (a single
    worker is strictly worse than serial: it pays the clone overhead for zero
    concurrency). Set the env var high (e.g. 99) to force serial on any host, or
    to 2 to restore the pre-0.2.7.post1 aggressive behaviour.
    """
    raw = os.environ.get(_MIN_PARALLEL_WORKERS_ENV)
    if raw is not None:
        try:
            return max(2, int(raw))
        except ValueError:
            pass
    return max(2, int(getattr(config, "REASSESSMENT_MIN_PARALLEL_CORES",
                              _DEFAULT_MIN_PARALLEL_WORKERS)))


def _max_reassessment_workers() -> int:
    """Cap on the pool size from the ``REASSESSMENT_MAX_WORKERS`` knob.

    Floored at 1, which keeps the reassessment serial whatever the other knobs
    say.
    """
    return max(1, int(getattr(config, "REASSESSMENT_MAX_WORKERS",
                              _MAX_REASSESSMENT_WORKERS)))


def _should_parallelize_reassessment(is_pypowsybl: bool, workers: int,
                                     n_actions: int) -> bool:
    """Whether to run the reassessment in parallel on ``workers`` workers.

    ``workers`` is the container-aware pool size from
    :func:`_reassessment_worker_count`, possibly lowered by the memory cap
    (:func:`_memory_worker_cap`). The ``REASSESSMENT_PARALLEL`` config knob
    forces the decision (``True`` = parallel when ≥2 workers, ``False`` =
    serial). In the default ``None`` (auto) mode parallel is engaged only when
    the pool reaches the core threshold (:data:`_DEFAULT_MIN_PARALLEL_WORKERS`
    / :func:`_min_parallel_workers`) AND every worker gets at least
    :data:`_MIN_ACTIONS_PER_WORKER` actions, in fast and slow mode alike. Below
    either, the per-worker network copy makes it slower than the serial path.
    """
    if not is_pypowsybl or n_actions < 2:
        return False
    force = getattr(config, "REASSESSMENT_PARALLEL", None)
    if force is not None:
        return bool(force) and workers >= 2
    if workers < _min_parallel_workers():
        return False
    return n_actions >= workers * _MIN_ACTIONS_PER_WORKER


def _reassessment_worker_count(n_actions: int) -> Tuple[int, int]:
    """Return ``(effective_cores, workers)`` — the container-aware pool size.

    ``effective_cores`` is **container-aware** (respects the cgroup CPU quota
    and scheduler affinity), unlike :func:`os.cpu_count`. ``workers`` is the raw
    pool size ``min(REASSESSMENT_MAX_WORKERS, effective_cores, n_actions)``
    (default cap 10); whether to actually run with more than one worker is
    decided by :func:`_should_parallelize_reassessment` (the
    ``REASSESSMENT_PARALLEL`` force knob, the ``REASSESSMENT_MIN_PARALLEL_CORES``
    / env threshold and the actions-per-worker gate), and the available memory
    lowers it once the network size is known (:func:`_memory_worker_cap`).
    Every worker clones a full private pypowsybl network, so on a CPU-starved
    host (e.g. a 2-vCPU cloud container) parallel over-subscribes the CPU *and*
    pays that per-worker clone tax — measurably SLOWER than a serial
    re-simulation, which is exactly what the gate guards against.
    """
    effective = _effective_cpu_count()
    workers = max(1, min(_max_reassessment_workers(), effective, n_actions))
    return effective, workers


def _estimate_worker_memory_bytes(buffer_nbytes: int) -> int:
    """Memory one worker costs for a network serialised to ``buffer_nbytes``
    (see :data:`_WORKER_MEMORY_PER_BUFFER_BYTE`)."""
    return int(_WORKER_MEMORY_PER_BUFFER_BYTE * buffer_nbytes
               + _WORKER_MEMORY_FIXED_BYTES)


def _memory_worker_cap(buffer_nbytes: int) -> Optional[int]:
    """How many workers the memory budget holds, or ``None`` when unknown.

    Each worker loads its own copy of the network serialised to
    ``buffer_nbytes``; the copies may take :data:`_WORKER_MEMORY_BUDGET` of
    the available memory (:func:`_available_memory_bytes`).
    """
    available = _available_memory_bytes()
    if available is None:
        return None
    return int(available * _WORKER_MEMORY_BUDGET
               // _estimate_worker_memory_bytes(buffer_nbytes))


def _make_worker_baseline_obs(binary_buffer, thermal_limits):
    """Build a self-contained N-1 observation on a *private* network copy.

    Each reassessment worker owns an independent pypowsybl network (cloned from
    ``binary_buffer``) so their load flows run truly in parallel — pypowsybl
    releases the GIL during the load flow, but the *working variant* is network
    global, so concurrent ``simulate()`` on one shared network would race.

    ``binary_buffer`` is captured from the main network's *contingency* variant
    (N-1 topology + maintenance already applied), and pypowsybl's binary buffer
    preserves the working-variant topology, so the worker loads straight into
    the N-1 baseline — no per-worker contingency load flow needed. Candidate
    actions are then simulated on top of it, exactly as the serial path does on
    ``obs_simu_defaut``.
    """
    import pypowsybl as pp

    from expert_op4grid_recommender.pypowsybl_backend.simulation_env import (
        SimulationEnvironment,
    )

    net = pp.network.load_from_binary_buffer(binary_buffer)
    wenv = SimulationEnvironment(network=net, thermal_limits=thermal_limits)
    return wenv, wenv.get_obs()


def _extract_pypowsybl_network(env: Any) -> Any:
    """Best-effort extraction of the underlying pypowsybl ``Network``
    from a simulation environment.

    The two supported backends expose it in different places:

    - pypowsybl backend: ``env.network_manager.network``
    - grid2op backend:   ``env.backend._grid.network``

    Returns ``None`` when neither path is present.
    """
    if env is None:
        return None
    nm = getattr(env, "network_manager", None)
    if nm is not None:
        net = getattr(nm, "network", None)
        if net is not None:
            return net
    backend = getattr(env, "backend", None)
    if backend is not None:
        grid = getattr(backend, "_grid", None)
        if grid is not None:
            return getattr(grid, "network", None)
    return None


def _extract_pypowsybl_network_from_obs(obs: Any) -> Any:
    """Best-effort extraction of the pypowsybl ``Network`` from an
    observation. Grid2Op observations return ``None``.
    """
    if obs is None:
        return None
    nm = getattr(obs, "_network_manager", None)
    if nm is not None:
        return getattr(nm, "network", None)
    return None


def _extract_overloaded_rho(
    obs_defaut: Any, lines_overloaded_ids: List[int],
) -> Optional[List[float]]:
    """Pre-extract the loading rate (rho) of each constrained line under
    the N-K state, paired with ``lines_overloaded_ids``.

    Returns a plain Python ``list[float]`` for serialisation friendliness
    (the underlying ``obs_defaut.rho`` may be a numpy array). Returns
    ``None`` when either the observation has no ``rho`` attribute or the
    indices cannot be resolved — callers treat that as "data not
    pre-computed".
    """
    if obs_defaut is None or not lines_overloaded_ids:
        return None
    rho = getattr(obs_defaut, "rho", None)
    if rho is None:
        return None
    try:
        return [float(rho[i]) for i in lines_overloaded_ids]
    except (TypeError, IndexError, KeyError):
        return None


def reassess_prioritized_actions(
    prioritized_actions: Dict[str, Any],
    context: Dict[str, Any],
) -> Tuple[Dict[str, dict], List[dict]]:
    """Simulate each prioritised action and compute rho metrics.

    Args:
        prioritized_actions: ``{action_id: action_obj}`` from a recommender.
        context: pipeline context built by :func:`run_analysis_step1` and
            updated by :func:`run_analysis_step2_graph`.

    Returns:
        ``(detailed_actions, pre_existing_info)``.
    """
    backend = context["backend"]
    env = context["env"]
    obs = context["obs"]
    obs_simu_defaut = context["obs_simu_defaut"]
    current_timestep = context["current_timestep"]
    current_lines_defaut = context["current_lines_defaut"]
    lines_overloaded_ids = context["lines_overloaded_ids"]
    act_reco_maintenance = context["act_reco_maintenance"]
    lines_we_care_about = context["lines_we_care_about"]
    pre_existing_rho = context["pre_existing_rho"]
    dict_action = context["dict_action"]
    is_pypowsybl = context["is_pypowsybl"]
    actual_fast_mode = context["actual_fast_mode"]

    with Timer("Reassessment"):
        # ``backend`` is the SimulationBackend threaded through the context; it
        # holds fast_mode so the re-simulation needs no per-call fast_mode.
        act_defaut = backend.create_default_action(env.action_space, current_lines_defaut)

        if is_pypowsybl:
            if obs_simu_defaut._network_manager._default_dc:
                obs_simu_defaut, _ = backend.simulate_contingency(
                    env, obs, current_lines_defaut, act_reco_maintenance, current_timestep,
                )
                obs_simu_defaut._network_manager._default_dc = False
        else:
            obs_simu_defaut, _ = backend.simulate_contingency(
                env, obs, current_lines_defaut, act_reco_maintenance, current_timestep,
            )
        baseline_rho = obs_simu_defaut.rho[lines_overloaded_ids]

        num_lines = len(obs.name_line)
        pre_existing_baseline = np.zeros(num_lines)
        is_pre_existing = np.zeros(num_lines, dtype=bool)
        for idx, rho_val in pre_existing_rho.items():
            pre_existing_baseline[idx] = rho_val
            is_pre_existing[idx] = True

        if lines_we_care_about is not None and len(lines_we_care_about) > 0:
            care_mask = np.isin(obs.name_line, list(lines_we_care_about))
        else:
            care_mask = np.ones(num_lines, dtype=bool)

        worsening_threshold = getattr(
            config, "PRE_EXISTING_OVERLOAD_WORSENING_THRESHOLD", 0.02,
        )

        def _build_detail(action_id, action, obs_simu_action, info_action):
            """Turn one (action, simulated observation) into the card payload."""
            description_unitaire = None
            if action_id in dict_action:
                description_unitaire = dict_action[action_id].get("description_unitaire")

            rho_before = baseline_rho
            rho_after = None
            max_rho = 0.0
            max_rho_line = "N/A"
            is_rho_reduction = False

            if not info_action["exception"]:
                rho_after = obs_simu_action.rho[lines_overloaded_ids]
                if rho_before is not None:
                    is_rho_reduction = bool(np.all(rho_after + 0.01 < rho_before))

                action_rho = obs_simu_action.rho
                worsened_mask = action_rho > pre_existing_baseline * (1 + worsening_threshold)
                eligible_mask = care_mask & (~is_pre_existing | worsened_mask)

                if np.any(eligible_mask):
                    masked_rho = action_rho[eligible_mask]
                    max_idx_in_masked = int(np.argmax(masked_rho))
                    max_rho = float(masked_rho[max_idx_in_masked])
                    max_rho_line = obs_simu_action.name_line[
                        np.where(eligible_mask)[0][max_idx_in_masked]
                    ]

            sim_exception = info_action.get("exception")
            non_convergence = None
            if sim_exception:
                if isinstance(sim_exception, list):
                    non_convergence = "; ".join(str(e) for e in sim_exception)
                else:
                    non_convergence = str(sim_exception)

            print(f"{action_id}")
            if description_unitaire:
                print(f"  {description_unitaire}")
            if rho_before is not None and rho_after is not None:
                print(f"  Rho reduction from {np.round(rho_before, 2)} to {np.round(rho_after, 2)}")
                print(f"  New max rho is {max_rho:.2f} on line {max_rho_line}")

            # Typed action-card payload. ``SimulatedAction`` mixes in
            # ``DictCompatMixin`` so every existing card consumer that indexes
            # the payload (``card["rho_before"]``, ``card.get("observation")``)
            # keeps working unchanged.
            return SimulatedAction(
                action=action,
                description_unitaire=description_unitaire,
                rho_before=rho_before,
                rho_after=rho_after,
                max_rho=max_rho,
                max_rho_line=max_rho_line,
                is_rho_reduction=is_rho_reduction,
                observation=obs_simu_action,
                non_convergence=non_convergence,
            )

        action_items = list(prioritized_actions.items())
        n_actions = len(action_items)
        cores, workers = _reassessment_worker_count(n_actions)

        # ``{action_id: (obs_simu_action, info_action)}`` — the raw simulation
        # outputs, filled either in parallel (pypowsybl, each worker on its own
        # network copy) or serially, then turned into cards in original order.
        sim_out: Dict[str, tuple] = {}
        used_workers = 1
        memory_note = ""

        if _should_parallelize_reassessment(is_pypowsybl, workers, n_actions):
            try:
                main_nm = obs_simu_defaut._network_manager
                # Capture the N-1 baseline (contingency + maintenance) variant so
                # each worker loads straight into it; restore the main network to
                # base afterwards so nothing downstream is disturbed.
                main_nm.set_working_variant(obs_simu_defaut._variant_id)
                binary_buffer = main_nm.network.save_to_binary_buffer()
                main_nm.set_working_variant(main_nm.base_variant_id)
                # Every worker loads its own copy of the network: start no more
                # copies than the memory budget holds, then re-run the gate on
                # that pool (auto mode may now stay serial).
                memory_cap = _memory_worker_cap(binary_buffer.getbuffer().nbytes)
                if memory_cap is not None and memory_cap < workers:
                    memory_note = f", pool capped from {workers} by the available memory"
                    workers = memory_cap
                if _should_parallelize_reassessment(is_pypowsybl, workers, n_actions):
                    thermal_limits = obs_simu_defaut._thermal_limits
                    buckets = [action_items[i::workers] for i in range(workers)]

                    def _run_bucket(bucket):
                        _, wobs_defaut = _make_worker_baseline_obs(
                            binary_buffer, thermal_limits,
                        )
                        local = {}
                        for aid, act in bucket:
                            oa, _, _, info = wobs_defaut.simulate(
                                act, time_step=current_timestep, keep_variant=True,
                                fast_mode=actual_fast_mode,
                            )
                            local[aid] = (oa, info)
                        return local

                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        for part in pool.map(_run_bucket, buckets):
                            sim_out.update(part)
                    used_workers = workers
            except Exception as exc:  # pragma: no cover - defensive fallback
                logger.warning(
                    "Parallel reassessment failed (%s); falling back to serial.",
                    exc, exc_info=True,
                )
                sim_out = {}
                used_workers = 1

        if not sim_out:
            for action_id, action in action_items:
                if is_pypowsybl:
                    oa, _, _, info = obs_simu_defaut.simulate(
                        action, time_step=current_timestep, keep_variant=True,
                        fast_mode=actual_fast_mode,
                    )
                else:
                    oa, _, _, info = obs.simulate(
                        action + act_defaut + act_reco_maintenance,
                        time_step=current_timestep,
                    )
                sim_out[action_id] = (oa, info)

        detailed_actions: Dict[str, dict] = {}
        for action_id, action in action_items:
            obs_simu_action, info_action = sim_out[action_id]
            detailed_actions[action_id] = _build_detail(
                action_id, action, obs_simu_action, info_action,
            )

        # Surface how the reassessment was parallelised so callers can show it
        # in the reassessment-time tooltip.
        context["reassessment_parallelism"] = {
            "parallel": used_workers > 1,
            "workers": used_workers,
            "cores_available": cores,
            "n_actions": n_actions,
        }
        print(
            f"[Reassessment] {n_actions} action(s) re-simulated on "
            f"{used_workers}/{cores} effective core(s) "
            f"({'parallel' if used_workers > 1 else 'serial'}{memory_note})."
        )

    pre_existing_info = [
        {"name": str(obs.name_line[i]), "rho_N": pre_existing_rho[i]}
        for i in sorted(pre_existing_rho.keys())
    ]
    return detailed_actions, pre_existing_info


def propagate_non_convergence_to_scores(
    detailed_actions: Dict[str, dict],
    action_scores: Dict[str, Any],
) -> Dict[str, Any]:
    """Mirror each action's non-convergence reason into the score table."""
    for action_id, details in detailed_actions.items():
        nc = details.get("non_convergence")
        for category in action_scores:
            if action_id in action_scores[category].get("scores", {}):
                action_scores[category].setdefault("non_convergence", {})
                action_scores[category]["non_convergence"][action_id] = nc
    return action_scores


def compute_combined_pairs(
    detailed_actions: Dict[str, dict],
    context: Dict[str, Any],
) -> Dict[str, dict]:
    """Run the superposition theorem on every detailed-action pair.

    Returns ``{}`` on any failure — superposition is decorative metadata
    and must never break the main flow.
    """
    with Timer("Combined Action Pairs (Superposition)"):
        try:
            from expert_op4grid_recommender.utils.superposition import (
                compute_all_pairs_superposition,
            )
            return compute_all_pairs_superposition(
                obs_start=context["obs_simu_defaut"],
                detailed_actions=detailed_actions,
                classifier=context["classifier"],
                env=context["env"],
                lines_overloaded_ids=context["lines_overloaded_ids"],
                lines_we_care_about=context["lines_we_care_about"],
                pre_existing_rho=context["pre_existing_rho"],
                dict_action=context["dict_action"],
            )
        except Exception as e:
            logger.warning(
                "Failed to compute combined action pairs: %s", e, exc_info=True,
            )
            return {}


def build_recommender_inputs(context: Dict[str, Any]):
    """Project the analysis ``context`` into a :class:`RecommenderInputs`.

    Surfaces three classes of data:

    1. **Observations + networks**: ``(obs, network)`` for the N state
       and ``(obs_defaut, network_defaut)`` for the N-K state.
    2. **Step-1 outcome already computed**: ``lines_overloaded_names``,
       ``lines_overloaded_ids``, ``lines_overloaded_rho`` (pre-extracted
       from ``obs_defaut.rho``), ``lines_overloaded_ids_kept`` (post-
       island-guard subset), and ``pre_existing_rho`` (N-state rho of
       lines that were already overloaded before the contingency).
       Models read these instead of recomputing.
    3. **Overflow-graph artefacts** when step-2 produced them, including
       ``filtered_candidate_actions`` — the action IDs retained by the
       expert :class:`ActionRuleValidator`. Forwarded so non-expert
       models that declare ``requires_overflow_graph=True`` (e.g.
       :class:`RandomOverflowRecommender`) can sample inside the same
       reduced action space the expert sees.
    """
    from expert_op4grid_recommender.models.base import RecommenderInputs

    env = context.get("env")
    obs_defaut = context["obs_simu_defaut"]
    lines_overloaded_ids = list(context["lines_overloaded_ids"])

    # --- Network handles (paired with the two observations) -----------
    network = context.get("n_grid")
    if network is None:
        network = _extract_pypowsybl_network(env)

    network_defaut = context.get("n_grid_defaut")
    if network_defaut is None:
        network_defaut = _extract_pypowsybl_network_from_obs(obs_defaut)
    if network_defaut is None:
        network_defaut = _extract_pypowsybl_network(env)

    # --- Pre-computed step-1 outcome ----------------------------------
    lines_overloaded_rho = _extract_overloaded_rho(obs_defaut, lines_overloaded_ids)

    ids_kept_raw = context.get("lines_overloaded_ids_kept")
    lines_overloaded_ids_kept = list(ids_kept_raw) if ids_kept_raw is not None else None

    pre_existing_rho_raw = context.get("pre_existing_rho")
    pre_existing_rho = (
        dict(pre_existing_rho_raw) if pre_existing_rho_raw is not None else None
    )

    # --- Expert-rule-filtered candidate set ---------------------------
    # Populated by ``_run_expert_action_filter`` whenever a recommender
    # that declares ``requires_overflow_graph=True`` enters
    # ``run_analysis_step2_discovery``. None when the filter never ran
    # (e.g. step-2 graph was skipped); empty list when it ran but
    # nothing passed — downstream models distinguish those two cases.
    filtered_raw = context.get("filtered_candidate_actions")
    filtered_candidate_actions = (
        list(filtered_raw) if filtered_raw is not None else None
    )

    return RecommenderInputs(
        obs=context["obs"],
        obs_defaut=obs_defaut,
        lines_defaut=list(context["current_lines_defaut"]),
        lines_overloaded_names=list(context["lines_overloaded_names"]),
        lines_overloaded_ids=lines_overloaded_ids,
        dict_action=context["dict_action"],
        env=env,
        classifier=context["classifier"],
        network=network,
        network_defaut=network_defaut,
        lines_overloaded_rho=lines_overloaded_rho,
        lines_overloaded_ids_kept=lines_overloaded_ids_kept,
        pre_existing_rho=pre_existing_rho,
        timestep=context["current_timestep"],
        overflow_graph=context.get("g_overflow"),
        distribution_graph=context.get("g_distribution_graph"),
        overflow_sim=context.get("overflow_sim"),
        hubs=context.get("hubs"),
        node_name_mapping=context.get("node_name_mapping"),
        non_connected_reconnectable_lines=context.get("non_connected_reconnectable_lines"),
        lines_non_reconnectable=context.get("lines_non_reconnectable"),
        lines_we_care_about=context.get("lines_we_care_about"),
        maintenance_to_reco_at_t=context.get("maintenance_to_reco_at_t"),
        act_reco_maintenance=context.get("act_reco_maintenance"),
        use_dc=context.get("use_dc", False),
        is_pypowsybl=context.get("is_pypowsybl", True),
        fast_mode=context.get("actual_fast_mode", False),
        filtered_candidate_actions=filtered_candidate_actions,
        structural_antennas=context.get("structural_antennas"),
        _context=context,
    )
