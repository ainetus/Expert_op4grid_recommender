# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Core state, caches, and shared helpers for action discovery."""
import numpy as np
import networkx as nx
from collections import defaultdict
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from alphaDeesp.core.graphsAndPaths import (
    OverFlowGraph,
    Structured_Overload_Distribution_Graph,
)

from expert_op4grid_recommender import config
from expert_op4grid_recommender.action_evaluation.classifier import ActionClassifier
from expert_op4grid_recommender.action_evaluation.discovery._results import (
    DisconnectionBounds,
    install_family_result_properties,
    new_results,
)
from expert_op4grid_recommender.utils.simulation import (
    check_rho_reduction as _default_check_rho_reduction,
)
from expert_op4grid_recommender.utils.simulation import (
    create_default_action as _default_create_default_action,
)
from expert_op4grid_recommender.utils.simulation import (
    compute_baseline_simulation as _default_compute_baseline,
    check_rho_reduction_with_baseline as _default_check_with_baseline,
)
from expert_op4grid_recommender.utils.simulation import BaselineContext

class DiscovererBase:
    """Core state, caches, and shared helpers for action discovery."""

    def __init__(
        self,
        env: Any,
        obs: Any,
        obs_defaut: Any,
        timestep: int,
        lines_defaut: List[str],
        lines_overloaded_ids: List[int],
        act_reco_maintenance: Any,
        classifier: ActionClassifier,  # Accept classifier instance
        non_connected_reconnectable_lines: List[str],
        all_disconnected_lines: List[str],
        dict_action: Dict,
        actions_unfiltered: Set[str],
        hubs: List[str],
        g_overflow: OverFlowGraph,
        g_distribution_graph: Structured_Overload_Distribution_Graph,
        simulator_data: Dict,
        check_action_simulation: bool = True,
        lines_we_care_about: Optional[List[str]] = None,
        check_rho_reduction_func: Optional[Callable] = None,
        create_default_action_func: Optional[Callable] = None,
        compute_baseline_func: Optional[Callable] = None,
        check_rho_with_baseline_func: Optional[Callable] = None,
        branch_candidates_from_baseline: bool = False,
        use_shared_baseline_for_topological: bool = False,
        obs_linecut: Optional[Any] = None,
        antenna_mode: bool = False,
        antenna_meta: Optional[Dict] = None,
        structural_antennas: Optional[List[Any]] = None,
    ):
        """
        Initializes the ActionDiscoverer with the necessary context and parameters.

        Args:
            env: The Grid2Op environment instance.
            obs: Observation before corrective action.
            obs_defaut: Observation after initial contingency.
            timestep: Current simulation timestep.
            lines_defaut: Lines defining the initial contingency.
            lines_overloaded_ids: Indices of overloaded lines.
            act_reco_maintenance: Action for maintenance reconnections.
            classifier: An initialized ActionClassifier instance. # Added classifier
            non_connected_reconnectable_lines: Allowed lines for reconnection.
            all_disconnected_lines: All currently disconnected lines.
            dict_action: Dictionary of candidate action descriptions.
            actions_unfiltered: Set of action IDs to consider initially.
            hubs: List of hub substation names.
            g_overflow: Processed overflow graph (integer nodes).
            g_distribution_graph: Processed distribution graph.
            simulator_data: Data required by AlphaDeesp ranker.
            check_action_simulation: Flag to enable/disable simulation checks.
            lines_we_care_about: Specific lines to monitor.
            check_rho_reduction_func: Function to check if an action reduces line loading.
                                      Defaults to grid2op version if not provided.
            create_default_action_func: Function to create a default (contingency) action.
                                        Defaults to grid2op version if not provided.
            obs_linecut: Observation after alphaDeesp cuts the overloaded lines from obs_defaut
                         (the N-2 state). Used in disconnection scoring to determine whether a
                         disconnection creates new overloads. When None, the disconnection scoring
                         falls back to the unconstrained regime (no upper redispatch bound).
            structural_antennas: ``StructuralAntenna`` list detected by step 2. The
                         injection families use the evacuation ones as levers when
                         ``config.EVACUATION_ANTENNA_LEVERS`` is on.
        """
        self.env = env
        self.obs = obs
        self.obs_defaut = obs_defaut
        self.obs_linecut = obs_linecut
        # Antenna mode: the overflow graph is a synthetic downstream graph of an
        # islanded radial pocket. Discovery is restricted to injection actions
        # (load shedding / renewable curtailment / redispatch); topological
        # actions are skipped (see OrchestratorMixin.discover_and_prioritize).
        self.antenna_mode = antenna_mode
        self.antenna_meta = antenna_meta
        self.structural_antennas = list(structural_antennas or [])
        self.action_space = env.action_space
        self.timestep = timestep
        self.lines_defaut = lines_defaut
        self.lines_overloaded_ids = lines_overloaded_ids
        self.act_reco_maintenance = act_reco_maintenance
        self.classifier = classifier  # Store the classifier instance
        self.non_connected_reconnectable_lines = non_connected_reconnectable_lines
        self.all_disconnected_lines = all_disconnected_lines
        self.dict_action = dict_action
        self.actions_unfiltered = actions_unfiltered
        self.hubs = hubs
        self.g_overflow = g_overflow
        self.g_distribution_graph = g_distribution_graph
        self.simulator_data = simulator_data
        self.check_action_simulation = check_action_simulation
        self.lines_we_care_about = lines_we_care_about

        # Backend-specific simulation callables (default to the grid2op module
        # helpers when not injected). Previously ``main.py`` monkey-patched
        # these onto the live instance for the pypowsybl backend; they now
        # arrive as constructor arguments derived from the SimulationBackend.
        self._per_candidate_check = (
            check_rho_reduction_func or _default_check_rho_reduction
        )
        self._create_default_action = (
            create_default_action_func or _default_create_default_action
        )
        self._compute_baseline = compute_baseline_func or _default_compute_baseline
        self._check_rho_with_baseline = (
            check_rho_with_baseline_func or _default_check_with_baseline
        )
        self._branch_candidates_from_baseline = branch_candidates_from_baseline
        # Route the topological candidate rho-checks either per-candidate
        # (grid2op — re-applies the contingency and recomputes the baseline for
        # every candidate) or through the single shared baseline (pypowsybl —
        # one baseline load flow per run). Stored as an instance attribute so
        # the family mixins keep calling ``self._check_rho_reduction(...)``.
        if use_shared_baseline_for_topological:
            self._check_rho_reduction = self._shared_baseline_check
        else:
            self._check_rho_reduction = self._per_candidate_check

        # Per-family discovery outcome — one typed FamilyResult per family
        # (revision R5), keyed by family token. The legacy per-family attribute
        # names (``identified_reconnections``, ``scores_splits_dict``,
        # ``scores_pst_actions``, …) are @property bridges installed on the class
        # (see the bottom of this module), so every existing mixin write and test
        # read transparently targets ``self.results[key].<field>``. PST is a
        # family like any other now — always present in ``self.results`` — so the
        # orchestrator's ``getattr`` special-casing is gone.
        self.results = new_results()
        # Two family-specific extras that are not part of the unified quintuplet
        # and are read directly by the node-splitting / disconnection mixins:
        # ``scores_splits`` is a sorted score LIST (distinct from the dict
        # ``scores_splits_dict``); ``ignored_*`` collect candidates dropped
        # before scoring.
        self.scores_splits = []
        self.ignored_splits = []
        self.ignored_disconnections = []
        self.prioritized_actions = {}
        # Shared per-run baseline (BaselineContext), lazily built by
        # _get_simulation_baseline and released by _release_simulation_baseline.
        self._cached_simulation_baseline = None

    def _get_disconnection_bounds(self) -> DisconnectionBounds:
        """Return the disconnection/PST flow bounds, computed once per run.

        Both line-disconnection scoring and PST scoring need the same reference
        flows (``max_overload_flow`` + the per-line ``capacity_map``). They are a
        pure function of immutable per-instance state (``obs``, ``obs_defaut``,
        ``obs_linecut``, ``lines_overloaded_ids``) and the discoverer is created
        fresh per Step-2 run, so a single memoised value is always correct.

        This replaces the previous ``self._disco_bounds`` / ``_disco_capacity_map``
        pair that PST lazily created and disconnection *deleted* at entry and
        exit — a cross-file, order-sensitive protocol (A5) that made PST scoring
        silently depend on disconnections running immediately before it. Any
        family may now call this in any order and get the identical value.
        """
        if not hasattr(self, "_cached_disconnection_bounds"):
            max_overload_flow, min_redispatch, max_redispatch = (
                self._compute_disconnection_flow_bounds()
            )
            self._cached_disconnection_bounds = DisconnectionBounds(
                max_overload_flow=max_overload_flow,
                min_redispatch=min_redispatch,
                max_redispatch=max_redispatch,
                capacity_map=self._build_line_capacity_map(),
            )
        return self._cached_disconnection_bounds

    def _cap_candidates_for_simulation(self, identified, scores_map):
        """Return the subset of ``identified`` actions to validate by simulation.

        Used by the generator-targeting discovery passes (redispatching,
        renewable curtailment) whose candidate count scales with the number of
        reachable generators. Their per-candidate AC load-flow verdict
        (effective / ineffective) does not feed prioritization — the per-type
        scores already rank candidates and the final rho is recomputed in the
        reassessment phase — so simulating candidates here is informational
        only. ``config.MAX_CANDIDATE_SIMULATIONS`` controls how many to run:
        0 (default) skips the simulation entirely, N > 0 keeps the top-N by
        descending score, and a negative value simulates every candidate
        (legacy behaviour).

        Returns a list of ``(action_id, action)`` pairs.
        """
        cap = config.MAX_CANDIDATE_SIMULATIONS
        if cap == 0:
            return []
        items = list(identified.items())
        if cap < 0 or len(items) <= cap:
            return items
        ranked = sorted(
            items, key=lambda kv: scores_map.get(kv[0], 0.0), reverse=True
        )
        return ranked[:cap]

    def _get_simulation_baseline(self):
        """Return ``(act_defaut, baseline_rho, branch_obs)`` for candidate rho checks.

        Computed once per discovery run and shared across *all* discovery
        passes — the injection passes (load shedding, redispatching, renewable
        curtailment) call this directly, and on the pypowsybl backend the
        topological passes reach it through the shared-baseline wrapper wired in
        ``main.py`` (see ``_branch_candidates_from_baseline``). Each pass
        previously rebuilt the identical contingency action and re-ran the same
        baseline load flow (2 LFs + 1 leaked kept-variant per candidate); the
        shared baseline reduces that to a single baseline load flow per run. The
        discoverer instance is created fresh for every Step-2 run, so
        instance-level caching cannot go stale.

        ``branch_obs`` is the observation a candidate action should be simulated
        *on top of*. The two backends have opposite contracts: grid2op re-applies
        the contingency itself, so candidates branch from the healthy N-state
        (``self.obs``); pypowsybl branches from the contingency-applied kept
        variant (``obs_baseline``) and applies only the candidate. The
        ``_branch_candidates_from_baseline`` flag (set for pypowsybl in
        ``main.py``) selects between them.

        Returns a :class:`BaselineContext`; it iterates as
        ``(act_defaut, baseline_rho, branch_obs)`` so existing unpacking call
        sites are unchanged. :meth:`_release_simulation_baseline` frees its
        retained variant at the end of the run.
        """
        if self._cached_simulation_baseline is None:
            act_defaut = self._create_default_action(
                self.action_space, self.lines_defaut
            )
            baseline_rho, obs_baseline = self._compute_baseline(
                self.obs, self.timestep, act_defaut,
                self.act_reco_maintenance, self.lines_overloaded_ids,
            )
            branch_obs = (
                obs_baseline
                if getattr(self, "_branch_candidates_from_baseline", False)
                else self.obs
            )
            self._cached_simulation_baseline = BaselineContext(
                act_defaut=act_defaut,
                baseline_rho=baseline_rho,
                obs_baseline=obs_baseline,
                branch_obs=branch_obs,
            )
        return self._cached_simulation_baseline

    def _release_simulation_baseline(self) -> None:
        """Release the shared baseline's retained variant (best-effort).

        Called once at the end of ``discover_and_prioritize``. Frees the single
        kept baseline variant the pypowsybl backend retained for this run so it
        does not linger on the ``NetworkManager`` across analyses (review C4).
        The ``NetworkManager`` variant registry is the harder backstop; this is
        the explicit, timely release.
        """
        ctx = self._cached_simulation_baseline
        if isinstance(ctx, BaselineContext):
            ctx.release()
        self._cached_simulation_baseline = None

    def _shared_baseline_check(self, obs, timestep, act_defaut, action,
                               overload_ids, act_reco_maintenance,
                               lines_we_care_about=None):
        """Topological candidate rho-check routed through the shared baseline.

        Selected in ``__init__`` when the backend sets
        ``use_shared_baseline_for_topological`` (pypowsybl). Ignores the
        per-call ``obs`` / ``act_defaut`` and reuses the single cached
        contingency baseline (one baseline load flow per run instead of one per
        candidate), branching each candidate from the contingency-applied
        observation. Previously this lived as a closure monkey-patched onto the
        instance in ``main.py``.
        """
        cached_act_defaut, baseline_rho, branch_obs = self._get_simulation_baseline()
        if baseline_rho is None:
            return False, None
        return self._check_rho_with_baseline(
            branch_obs, timestep, cached_act_defaut, action, overload_ids,
            act_reco_maintenance, baseline_rho, lines_we_care_about,
        )

    def _build_lookup_caches(self):
        """Pre-computes name-to-index lookup dictionaries to avoid repeated np.where calls."""
        if not hasattr(self, "_line_name_to_id"):
            self._line_name_to_id = {
                name: idx for idx, name in enumerate(self.obs.name_line)
            }
            self._sub_name_to_id = {
                name: idx for idx, name in enumerate(self.obs.name_sub)
            }
            # Vectorized pre-computation of line -> (sub_or_name, sub_ex_name) mapping
            or_subs = self.obs.name_sub[self.obs.line_or_to_subid]
            ex_subs = self.obs.name_sub[self.obs.line_ex_to_subid]
            self._line_to_subs = dict(zip(self.obs.name_line, zip(or_subs, ex_subs)))

    def _get_blue_edge_names_set(self) -> Set[str]:
        """Cached set of blue (constrained path) edge names."""
        if not hasattr(self, "_cached_blue_edge_names_set"):
            constrained_edges_names, _, other_blue_names, _ = (
                self.g_distribution_graph.get_constrained_edges_nodes()
            )
            self._cached_blue_edge_names_set = set(constrained_edges_names + other_blue_names)
        return self._cached_blue_edge_names_set

    def _get_subs_with_loads(self) -> Dict[int, List[int]]:
        """Cached mapping of substation_id -> list of load_ids with positive power.
        Built once using vectorized operations instead of per-node get_obj_connect_to."""
        if hasattr(self, "_cached_subs_with_loads"):
            return self._cached_subs_with_loads
        obs = self.obs_defaut
        result: Dict[int, List[int]] = {}
        if hasattr(obs, "load_to_subid"):
            load_p = obs.load_p
            for lid in range(len(load_p)):
                if load_p[lid] > 0:
                    sub_id = int(obs.load_to_subid[lid])
                    if sub_id not in result:
                        result[sub_id] = []
                    result[sub_id].append(lid)
        else:
            # Fallback: iterate substations
            for sub_id in range(len(obs.name_sub)):
                obj = obs.get_obj_connect_to(substation_id=sub_id)
                load_ids = obj.get("loads_id", [])
                positive = [lid for lid in load_ids if lid < len(obs.load_p) and obs.load_p[lid] > 0]
                if positive:
                    result[sub_id] = positive
        self._cached_subs_with_loads = result
        return result

    def _get_subs_with_renewable_gens(self) -> Dict[int, List[int]]:
        """Cached mapping of substation_id -> list of renewable generator_ids with non-zero power.
        Built once using vectorized operations instead of per-node get_obj_connect_to."""
        if hasattr(self, "_cached_subs_with_renewable_gens"):
            return self._cached_subs_with_renewable_gens
        obs = self.obs_defaut
        renewable_sources = set(
            s.upper()
            for s in config.RENEWABLE_ENERGY_SOURCES
        )
        gen_energy_sources = getattr(obs, "gen_energy_source", getattr(obs, "gen_type", None))
        min_mw = config.RENEWABLE_CURTAILMENT_MIN_MW
        result: Dict[int, List[int]] = {}

        if gen_energy_sources is not None and hasattr(obs, "gen_to_subid"):
            gen_p_array = getattr(obs, "gen_p", getattr(obs, "prod_p", None))
            if gen_p_array is None:
                self._cached_subs_with_renewable_gens = result
                return result
            for gid in range(len(gen_energy_sources)):
                if str(gen_energy_sources[gid]).upper() not in renewable_sources:
                    continue
                if gid >= len(gen_p_array):
                    continue
                gen_p = float(gen_p_array[gid])
                if gen_p > 0 or abs(gen_p) < min_mw:
                    continue
                sub_id = int(obs.gen_to_subid[gid])
                if sub_id not in result:
                    result[sub_id] = []
                result[sub_id].append(gid)
        elif gen_energy_sources is not None:
            # Fallback without gen_to_subid
            gen_p_array = getattr(obs, "gen_p", getattr(obs, "prod_p", None))
            for sub_id in range(len(obs.name_sub)):
                obj = obs.get_obj_connect_to(substation_id=sub_id)
                gen_ids = obj.get("generators_id", [])
                renewable = []
                for gid in gen_ids:
                    if gid >= len(gen_energy_sources):
                        continue
                    if str(gen_energy_sources[gid]).upper() not in renewable_sources:
                        continue
                    if gen_p_array is not None and gid < len(gen_p_array):
                        gen_p = float(gen_p_array[gid])
                        if gen_p > 0 or abs(gen_p) < min_mw:
                            continue
                    renewable.append(gid)
                if renewable:
                    result[sub_id] = renewable
        self._cached_subs_with_renewable_gens = result
        return result

    def _get_subs_with_dispatchable_gens(self) -> Dict[int, List[int]]:
        """Cached mapping of substation_id -> list of dispatchable generator_ids
        with non-zero power.

        A generator is considered *dispatchable* when its energy source is NOT
        in ``RENEWABLE_ENERGY_SOURCES`` (i.e. the complement of the renewable
        set used by curtailment). Built once using vectorized operations
        instead of per-node ``get_obj_connect_to``."""
        if hasattr(self, "_cached_subs_with_dispatchable_gens"):
            return self._cached_subs_with_dispatchable_gens
        obs = self.obs_defaut
        renewable_sources = set(
            s.upper()
            for s in config.RENEWABLE_ENERGY_SOURCES
        )
        gen_energy_sources = getattr(obs, "gen_energy_source", getattr(obs, "gen_type", None))
        min_mw = config.REDISPATCH_MIN_MW
        result: Dict[int, List[int]] = {}

        if gen_energy_sources is not None and hasattr(obs, "gen_to_subid"):
            gen_p_array = getattr(obs, "gen_p", getattr(obs, "prod_p", None))
            if gen_p_array is None:
                self._cached_subs_with_dispatchable_gens = result
                return result
            for gid in range(len(gen_energy_sources)):
                if str(gen_energy_sources[gid]).upper() in renewable_sources:
                    continue
                if gid >= len(gen_p_array):
                    continue
                gen_p = float(gen_p_array[gid])
                # Active producer (production stored as negative target terminal
                # power in pypowsybl); ignore idle/trivial generators.
                if gen_p > 0 or abs(gen_p) < min_mw:
                    continue
                sub_id = int(obs.gen_to_subid[gid])
                if sub_id not in result:
                    result[sub_id] = []
                result[sub_id].append(gid)
        elif gen_energy_sources is not None:
            # Fallback without gen_to_subid
            gen_p_array = getattr(obs, "gen_p", getattr(obs, "prod_p", None))
            for sub_id in range(len(obs.name_sub)):
                obj = obs.get_obj_connect_to(substation_id=sub_id)
                gen_ids = obj.get("generators_id", [])
                dispatchable = []
                for gid in gen_ids:
                    if gid >= len(gen_energy_sources):
                        continue
                    if str(gen_energy_sources[gid]).upper() in renewable_sources:
                        continue
                    if gen_p_array is not None and gid < len(gen_p_array):
                        gen_p = float(gen_p_array[gid])
                        if gen_p > 0 or abs(gen_p) < min_mw:
                            continue
                    dispatchable.append(gid)
                if dispatchable:
                    result[sub_id] = dispatchable
        self._cached_subs_with_dispatchable_gens = result
        return result

    def _get_evacuation_antennas_by_sub(
        self, antenna_type: str = "evacuation"
    ) -> Dict[int, Any]:
        """Cached ``{substation_id: StructuralAntenna}`` over the substations of
        the antennas of one type: ``"evacuation"`` (upstream) or
        ``"evacuation_downstream"``.

        Empty unless ``config.EVACUATION_ANTENNA_LEVERS`` is on, so the injection
        families behave exactly as before by default
        (docs/recommender/evacuation_antennas.md, phase 4)."""
        if not hasattr(self, "_cached_evacuation_antennas_by_sub"):
            by_type: Dict[str, Dict[int, Any]] = {}
            if config.EVACUATION_ANTENNA_LEVERS:
                for antenna in self.structural_antennas:
                    if antenna.attachment_sub_ids:
                        subs = by_type.setdefault(antenna.antenna_type, {})
                        for sub_id in antenna.sub_ids:
                            subs[int(sub_id)] = antenna
            self._cached_evacuation_antennas_by_sub = by_type
        return self._cached_evacuation_antennas_by_sub.get(antenna_type, {})

    def _get_voltage_level_metadata(self) -> Dict[int, Tuple[str, float]]:
        """Cached ``{node_idx: (site_id, nominal_v_kV)}``.

        Each discovery node is a *voltage level* (pypowsybl semantics:
        ``name_sub`` are voltage-level ids). This maps every node to the
        physical site it belongs to and its nominal voltage, so generator
        discovery can reason about "the same site at a higher voltage".

        Source of truth is the pypowsybl network
        (``get_voltage_levels()[['substation_id', 'nominal_v']]``); when the
        network is unreachable (e.g. grid2op backend) we fall back to the RTE
        naming convention (site = name without a numeric prefix, truncated to
        5 chars; voltage encoded by the trailing character — ``7``→400 kV,
        ``6``→225 kV, ``3``→63 kV, …)."""
        if hasattr(self, "_cached_vl_metadata"):
            return self._cached_vl_metadata
        obs = self.obs_defaut
        # Hoist once: ``obs.name_sub`` rebuilds a numpy string array on every
        # access, so reading it inside the per-substation loops below would be
        # O(n_subs^2). This helper is built lazily on the first generator pass
        # (renewable curtailment), which is why that pass alone paid the cost.
        name_sub_arr = obs.name_sub
        n_subs = len(name_sub_arr)
        meta: Dict[int, Tuple[str, float]] = {}

        network = None
        env = getattr(self, "env", None)
        nm = getattr(env, "network_manager", None)
        if nm is not None:
            network = getattr(nm, "network", None)
        if network is not None:
            try:
                # Fetch only the two columns we need — fetching all VL columns
                # (name, voltage limits, …) is materially slower on large grids.
                try:
                    vl_df = network.get_voltage_levels(
                        attributes=["substation_id", "nominal_v"]
                    )
                except TypeError:
                    # Older pypowsybl without the ``attributes`` kwarg.
                    vl_df = network.get_voltage_levels()
                site_map = vl_df["substation_id"].to_dict()
                nomv_map = vl_df["nominal_v"].to_dict()
                for i in range(n_subs):
                    name = str(name_sub_arr[i])
                    site = site_map.get(name)
                    nomv = nomv_map.get(name)
                    if site is not None and nomv is not None:
                        meta[i] = (str(site), float(nomv))
            except Exception as e:
                print(f"Warning: could not read voltage-level metadata from network: {e}")
                meta = {}

        if not meta:
            # RTE naming fallback (grid2op backend or missing network).
            import re
            digit_to_v = {
                "7": 400.0, "6": 225.0, "5": 90.0, "4": 150.0,
                "3": 63.0, "2": 45.0, "1": 20.0, "0": 20.0,
            }
            for i in range(n_subs):
                name = str(name_sub_arr[i])
                stripped = re.sub(r"^\d+", "", name)
                site = stripped[:5] if stripped else name
                nomv = digit_to_v.get(name[-1], 0.0) if name else 0.0
                meta[i] = (site, nomv)

        self._cached_vl_metadata = meta
        return meta

    def _get_site_higher_voltage_map(self) -> Dict[int, List[int]]:
        """Cached ``{node_idx: [higher-voltage same-site node_idx, ...]}``.

        For each voltage-level node, lists the nodes of the **same physical
        site** whose nominal voltage is strictly higher AND at an EHV/HV
        reference level (>= 225 kV), sorted by descending nominal voltage.

        Generators usually sit on a radial ("antenne") voltage level that is
        absent from the influence graph; this map lets generator-targeting
        discovery borrow the influence of the same site's 400/225 kV busbar
        when that higher node *is* in the graph."""
        if hasattr(self, "_cached_site_higher_v"):
            return self._cached_site_higher_v
        meta = self._get_voltage_level_metadata()
        ref_threshold = 220.0  # EHV/HV reference levels: 225 kV and 400 kV
        by_site: Dict[str, List[Tuple[float, int]]] = {}
        for idx, (site, nomv) in meta.items():
            by_site.setdefault(site, []).append((nomv, idx))

        result: Dict[int, List[int]] = {}
        for idx, (site, nomv) in meta.items():
            candidates = [
                (v, n_idx) for (v, n_idx) in by_site.get(site, ())
                if n_idx != idx and v > nomv and v >= ref_threshold
            ]
            if candidates:
                candidates.sort(key=lambda vc: vc[0], reverse=True)
                result[idx] = [n_idx for (_v, n_idx) in candidates]
        self._cached_site_higher_v = result
        return result

    def _build_node_flow_cache(self, blue_edge_names_set: Set[str],
                                dispatch_loop_set: Optional[Set[str]] = None
                                ) -> Dict[int, Dict[str, float]]:
        """Build node influence flow cache from edge data in a single pass.
        Returns {node_idx: {neg_in, neg_out, pos_in, pos_out}}."""
        edge_names = self._cached_edge_names if hasattr(self, "_cached_edge_names") else nx.get_edge_attributes(self.g_overflow.g, "name")
        edge_labels = self._cached_edge_labels if hasattr(self, "_cached_edge_labels") else {
            edge: float(val) for edge, val in nx.get_edge_attributes(self.g_overflow.g, "label").items()
        }
        node_flow_cache: Dict[int, Dict[str, float]] = {}
        for edge, name in edge_names.items():
            flow_val = edge_labels.get(edge, 0.0)
            u, v = edge[0], edge[1]
            is_blue = name in blue_edge_names_set
            is_dispatch = dispatch_loop_set is not None and name in dispatch_loop_set

            if not is_blue and not is_dispatch:
                continue

            abs_flow = abs(flow_val)
            if abs_flow == 0.0:
                continue

            if u not in node_flow_cache:
                node_flow_cache[u] = {"neg_in": 0.0, "neg_out": 0.0, "pos_in": 0.0, "pos_out": 0.0}
            if v not in node_flow_cache:
                node_flow_cache[v] = {"neg_in": 0.0, "neg_out": 0.0, "pos_in": 0.0, "pos_out": 0.0}

            if is_blue and flow_val < 0:
                node_flow_cache[u]["neg_out"] += abs_flow
                node_flow_cache[v]["neg_in"] += abs_flow
            if is_dispatch and flow_val > 0:
                node_flow_cache[u]["pos_in"] += abs_flow
                node_flow_cache[v]["pos_out"] += abs_flow

        return node_flow_cache

    def _build_active_edges_cache(self):
        """Pre-computes which node pairs have active (non-dashed/dotted) edges."""
        if hasattr(self, "_active_edges_cache"):
            return

        self._active_edges_cache = defaultdict(list)
        graph = self.g_overflow.g
        for u, v, data in graph.edges(data=True):
            style = data.get("style", "")
            if style not in ("dashed", "dotted") and "name" in data:
                # Use a stable tuple key for undirected lookup
                pair = (u, v) if u < v else (v, u)
                self._active_edges_cache[pair].append(data["name"])

    def _get_active_edges_between_cached(
        self, sub_or_name: str, sub_ex_name: str
    ) -> List[str]:
        """Fast lookup of active edges between two substations using pre-computed cache."""
        node_a = self._sub_name_to_id.get(sub_or_name)
        node_b = self._sub_name_to_id.get(sub_ex_name)
        if node_a is None or node_b is None:
            return []
        pair = (min(node_a, node_b), max(node_a, node_b))
        return self._active_edges_cache.get(pair, [])

    def _is_sublist(self, small: List, large: List) -> bool:
        """Checks if 'small' is a contiguous sublist of 'large'."""
        n = len(small)
        return any(small == large[i : i + n] for i in range(len(large) - n + 1))

    def _get_line_substations(self, line_name: str) -> Tuple[str, str]:
        """Gets substation names for a given line name."""
        self._build_lookup_caches()
        if line_name not in self._line_to_subs:
            raise ValueError(f"Line name '{line_name}' not found in observation.")
        return self._line_to_subs[line_name]

    def _find_paths_for_line(
        self, line_subs: Tuple[str, str], red_loop_paths: List[List[str]]
    ) -> List[List[str]]:
        """Finds paths containing the given line's substations."""
        sub_or, sub_ex = line_subs
        return [
            path
            for path in red_loop_paths
            if self._is_sublist([sub_or, sub_ex], path)
            or self._is_sublist([sub_ex, sub_or], path)
        ]

    def _get_active_edges_between(self, node_a: str, node_b: str) -> List[str]:
        """Retrieves active edges between two nodes (names) in the graph."""
        active_edges = []
        graph_to_check = self.g_overflow.g  # Use the processed graph (integer nodes)

        try:
            # Map names back to indices for graph lookup
            node_a_idx = np.where(self.obs.name_sub == node_a)[0][0]
            node_b_idx = np.where(self.obs.name_sub == node_b)[0][0]
        except IndexError:
            print(
                f"Warning: Could not find index for substations {node_a} or {node_b}."
            )
            return []  # Cannot check edges if nodes not found

        for u, v in [(node_a_idx, node_b_idx), (node_b_idx, node_a_idx)]:
            if graph_to_check.has_edge(u, v):
                edge_data_dict = graph_to_check.get_edge_data(u, v)
                if edge_data_dict:
                    for e_dict in edge_data_dict.values():
                        if "style" not in e_dict or e_dict["style"] not in [
                            "dashed",
                            "dotted",
                        ]:
                            if "name" in e_dict:
                                active_edges.append(e_dict["name"])
        return active_edges

    def _has_blocking_disconnected_line(
        self, found_path: List[str], line_reco: str
    ) -> Tuple[bool, Optional[str]]:
        """Checks if a path is blocked by other disconnected lines."""
        for line in self.all_disconnected_lines:
            if line == line_reco:
                continue
            try:
                sub_or, sub_ex = self._get_line_substations(line)
            except ValueError:
                continue  # Skip if line not found
            if not (
                self._is_sublist([sub_or, sub_ex], found_path)
                or self._is_sublist([sub_ex, sub_or], found_path)
            ):
                continue
            if not self._get_active_edges_between(sub_or, sub_ex):  # Pass names
                return True, line
        return False, None

    def _build_path_consecutive_pairs(
        self, paths: List[List[str]]
    ) -> List[Set[Tuple[str, str]]]:
        """Pre-computes set of consecutive substation pairs for each path for fast membership checks."""
        path_pairs = []
        for path in paths:
            pairs = set()
            for i in range(len(path) - 1):
                pairs.add((path[i], path[i + 1]))
            path_pairs.append(pairs)
        return path_pairs

    def _check_other_reconnectable_line_on_path(
        self, line_reco: str, red_loop_paths: List[List[str]]
    ) -> Tuple[bool, Optional[str]]:
        """Checks path blockage for a specific reconnection candidate.

        Uses pre-computed caches when available (set by verify_relevant_reconnections):
        - _reco_pair_to_paths: reverse index from substation pairs to path indices (O(1) lookup)
        - _reco_path_blockers: pre-computed blocking disconnected lines per path (O(1) check)
        """
        # Fast path: use pre-computed caches if available
        if hasattr(self, "_reco_pair_to_paths") and hasattr(
            self, "_reco_path_blockers"
        ):
            line_subs = self._line_to_subs.get(line_reco)
            if line_subs is None:
                return False, None, None

            sub_or, sub_ex = line_subs
            # O(1) lookup: which paths contain this line?
            found_path_indices = self._reco_pair_to_paths.get(
                (sub_or, sub_ex), set()
            ) | self._reco_pair_to_paths.get((sub_ex, sub_or), set())

            if not found_path_indices:
                return False, None, None

            blocker = None
            for path_idx in sorted(found_path_indices):
                path = red_loop_paths[path_idx]
                # O(1) lookup: which lines block this path?
                blocking_lines = self._reco_path_blockers[path_idx]
                # The only blocker that doesn't count is the line we're reconnecting
                actual_blockers = blocking_lines - {line_reco}

                if not actual_blockers:
                    return True, path, None
                if blocker is None:
                    blocker = next(iter(actual_blockers))

            return False, None, blocker

        # Slow fallback path (used when caches are not set, e.g. in unit tests)
        try:
            line_subs = self._get_line_substations(line_reco)
        except ValueError:
            return False, None, None

        found_paths = self._find_paths_for_line(line_subs, red_loop_paths)
        if not found_paths:
            return False, None, None

        blocker = None
        for path in found_paths:
            is_blocked, current_blocker = self._has_blocking_disconnected_line(
                path, line_reco
            )
            if not is_blocked:
                return True, path, None
            if blocker is None:
                blocker = current_blocker
        return False, None, blocker

    @staticmethod
    def _asymmetric_bell_score(
        observed_flow: float,
        min_flow: float,
        max_flow: float,
        alpha: float = 3.0,
        beta: float = 1.5,
        tail_scale: float = 2.0,
    ) -> float:
        """
        Computes an asymmetric bell-curve score for a line disconnection action.

        The score is based on the Beta(alpha, beta) kernel inside [min_flow, max_flow],
        which peaks closer to max_flow (since alpha > beta). Outside this range the score
        becomes negative via quadratic tails.

        Args:
            observed_flow: The redispatch flow of the line being evaluated.
            min_flow: Lower bound (minimum redispatch to relieve worst overload).
            max_flow: Upper bound (maximum redispatch before creating new overloads).
            alpha: Beta distribution first shape parameter (controls right skew). Default 3.0.
            beta: Beta distribution second shape parameter. Default 1.5.
            tail_scale: Multiplier for the negative quadratic tails. Default 2.0.

        Returns:
            float: Score in [-inf, 1]. Positive inside the acceptable range,
                   peaking closer to max_flow; zero at boundaries; negative outside.
        """
        if max_flow <= min_flow:
            return 0.0

        # Normalize to [0, 1] where 0=min_flow, 1=max_flow
        x = (observed_flow - min_flow) / (max_flow - min_flow)

        if 0.0 <= x <= 1.0:
            # Beta kernel: x^(alpha-1) * (1-x)^(beta-1), zero at boundaries
            score = (x ** (alpha - 1)) * ((1.0 - x) ** (beta - 1))
            # Normalize so peak value = 1
            x_peak = (alpha - 1) / (alpha + beta - 2)
            peak_val = (x_peak ** (alpha - 1)) * ((1.0 - x_peak) ** (beta - 1))
            score = score / peak_val if peak_val > 0 else 0.0
        else:
            # Negative quadratic tails
            if x < 0.0:
                score = -tail_scale * (x**2)
            else:
                score = -tail_scale * ((x - 1.0) ** 2)

        return score

    def _build_line_capacity_map(self) -> Dict[str, float]:
        """
        Builds a mapping from line name to its maximum absolute capacity on the overflow graph.
        Cached after first call since the overflow graph doesn't change.

        Returns:
            Dict[str, float]: line_name -> max absolute capacity (redispatch flow in MW).
        """
        if hasattr(self, "_cached_line_capacity_map"):
            return self._cached_line_capacity_map
        edge_data = self._get_edge_data_cache()
        name_to_capacity: Dict[str, float] = {}
        for name, _, cap in edge_data:
            cap = abs(cap)
            if name not in name_to_capacity or cap > name_to_capacity[name]:
                name_to_capacity[name] = cap
        self._cached_line_capacity_map = name_to_capacity
        return name_to_capacity

    def _get_edge_data_cache(self) -> List[Tuple[str, float, float]]:
        """
        Single-pass extraction of all edge attributes (name, label, capacity) from the overflow graph.
        Returns a list of (name, label_float, capacity_float) tuples.
        Cached after first call.
        """
        if hasattr(self, "_cached_edge_data"):
            return self._cached_edge_data
        g = self.g_overflow.g
        result = []
        # The overflow graph is a Multi(Di)Graph: parallel circuits between the
        # same two substations (twin RTE lines, e.g. L61/L62) share a (u, v)
        # pair. Key the name/label lookup dicts by the full edge id (u, v, k) so
        # parallel edges do not overwrite each other in flow-influence scoring.
        # This matches nx.get_edge_attributes (3-tuple keys on a multigraph),
        # which _build_node_flow_cache uses on the non-cached fallback path.
        is_multi = g.is_multigraph()
        edges_iter = g.edges(keys=True, data=True) if is_multi else g.edges(data=True)
        # Also cache name->edges and label->edges dicts for fast lookup.
        edge_names = {}
        edge_labels = {}
        for edge in edges_iter:
            if is_multi:
                u, v, k, data = edge
                key = (u, v, k)
            else:
                u, v, data = edge
                key = (u, v)
            name = data.get("name", "")
            label = float(data.get("label", 0.0))
            cap = float(data.get("capacity", 0.0))
            result.append((name, label, cap))
            edge_names[key] = name
            edge_labels[key] = label
        self._cached_edge_data = result
        self._cached_edge_names = edge_names
        self._cached_edge_labels = edge_labels
        return result

    def _compute_disconnection_flow_bounds(self) -> Tuple[float, float, float]:
        """
        Computes the min/max acceptable redispatch flow bounds for scoring disconnection actions.

        The bounds define the window of "useful" redispatch:
        - max_overload_flow: the capacity of the overloaded line(s) in the overflow graph,
          i.e. the flow they were carrying before being cut.  Disconnecting the overloaded
          line relieves exactly this flow → reference score of 1.0.
        - min_redispatch: the minimum flow needed to bring the worst overload below 100%.
          ``(max_rho_overloaded - 1) * max_overload_flow``
          Computed from ``obs_defaut`` (the N-1 contingency state).
        - max_redispatch: the maximum flow the system can absorb without creating new overloads.
          Requires ``obs_linecut`` (the N-2 state after alphaDeesp cuts the overloaded lines).
          Only lines that are NEWLY OVERLOADED in obs_linecut (rho_after > 1.0 AND
          rho_before < 1.0) create a binding constraint:
          ``capacity_l * (1 - rho_before) / (rho_after - rho_before)``
          where ``rho_before`` comes from ``obs_defaut`` and ``rho_after`` from ``obs_linecut``.
          The binding constraint (minimum across all such newly-overloaded lines) gives
          max_redispatch. If no line is newly overloaded, or if ``obs_linecut`` is not
          available, max_redispatch stays at inf (unconstrained regime).

        Returns:
            Tuple[float, float, float]:
                - max_overload_flow: the absolute capacity of the most-loaded overloaded line
                  in the overflow graph (i.e. the flow it was carrying before being cut).
                  This is the natural reference for scoring: disconnecting the overloaded line
                  itself relieves exactly this amount and therefore scores 1.0.
                  Falls back to the global max edge capacity if the overloaded line is absent
                  from the overflow graph.
                - min_redispatch: the minimum useful redispatch flow (MW).
                - max_redispatch: the maximum safe redispatch flow (MW).
        """
        name_to_capacity = self._build_line_capacity_map()
        if not name_to_capacity:
            return 0.0, 0.0, 0.0

        # max_overload_flow: capacity of the overloaded line(s) in the overflow graph.
        # The overloaded line's capacity equals the flow it was carrying before being cut.
        # Disconnecting it relieves exactly this flow → score = 1.0 for that action.
        overloaded_line_names = {
            self.obs_defaut.name_line[i] for i in self.lines_overloaded_ids
        }
        overloaded_caps = [
            name_to_capacity[n] for n in overloaded_line_names if n in name_to_capacity
        ]
        max_overload_flow = (
            max(overloaded_caps) if overloaded_caps else max(name_to_capacity.values())
        )

        # --- min_redispatch: excess loading on worst overloaded line (in N-1 state) ---
        rho_overloaded = self.obs_defaut.rho[self.lines_overloaded_ids]
        if len(rho_overloaded) > 0:
            max_rho_overloaded = float(np.max(rho_overloaded))
            min_redispatch = (max_rho_overloaded - 1.0) * max_overload_flow
        else:
            min_redispatch = 0.0

        # --- max_redispatch: binding flow margin before any line hits 100% ---
        # Compare obs_defaut (N-1 baseline) with obs_linecut (N-2, after disconnecting
        # the overloaded lines). A line creates a binding constraint when rho_after > 1.0:
        #   - rho_before < 1.0  (newly overloaded): use formula to find exact binding margin.
        #   - rho_before >= 1.0 (existing overload not relieved): fully constrained → 0.
        # The good case (rho_after < 1.0, rho_before >= 1.0) means the overload was relieved
        # and does NOT constrain. Lines that stay below 100% also do NOT constrain.
        # If obs_linecut is unavailable, leave max_redispatch at inf (unconstrained).
        self._build_lookup_caches()
        max_redispatch = float("inf")
        if self.obs_linecut is not None:
            for line_name, capacity_l in name_to_capacity.items():
                if (
                    self.lines_we_care_about is not None
                    and len(self.lines_we_care_about) > 0
                ):
                    if line_name not in self.lines_we_care_about:
                        continue

                line_id = self._line_name_to_id.get(line_name)
                if line_id is None or capacity_l < 1e-6:
                    continue
                rho_before = float(self.obs_defaut.rho[line_id])
                rho_after = float(self.obs_linecut.rho[line_id])
                if rho_after > rho_before:
                    # Skip pre-existing overloads unless they worsen beyond threshold
                    n_state_rho = float(self.obs.rho[line_id])
                    worsening_threshold = getattr(
                        config, "PRE_EXISTING_OVERLOAD_WORSENING_THRESHOLD", 0.02
                    )

                    if n_state_rho >= 1.0:
                        if rho_after > n_state_rho * (1 + worsening_threshold):
                            # Worsened pre-existing: consider with ratio = capacity_l (per user request)
                            max_redispatch = min(max_redispatch, capacity_l)
                        else:
                            continue
                    elif rho_after > 1.0:
                        if rho_before >= 1.0:
                            # Existing overload not relieved in obs_linecut: fully constrained
                            max_redispatch = 0.0
                        else:
                            # Newly overloaded line: compute binding flow margin using user-revised formula
                            # ratio = capacity_l * (rho_after - 1) / (rho_after - rho_before)
                            ratio = (
                                max_overload_flow / (rho_after - rho_before)*(1- rho_before)
                                #capacity_l*
                                #* (rho_after - 1.0)
                                #/ (rho_after - rho_before)
                            )
                            if ratio > 0:
                                max_redispatch = min(max_redispatch, ratio)

        # Fallback: if no line provided a binding constraint (or obs_linecut is None),
        # max_redispatch stays at inf — this signals the "unconstrained" regime where all
        # disconnections are safe and scoring should use a linear ramp.

        return max_overload_flow, min_redispatch, max_redispatch

    @staticmethod
    def _unconstrained_linear_score(
        observed_flow: float, min_flow: float, max_flow: float, tail_scale: float = 2.0
    ) -> float:
        """
        Linear score for the unconstrained disconnection regime.

        All disconnections above ``min_flow`` are useful (no upper overload risk).
        Score = 1 at ``max_flow``, linearly decreasing to 0 at ``min_flow``.
        Below ``min_flow`` a negative quadratic tail penalises insufficient flow.

        Args:
            observed_flow: The redispatch flow of the action being evaluated.
            min_flow: Minimum useful redispatch (score = 0 boundary).
            max_flow: Maximum redispatch flow on the graph (score = 1).
            tail_scale: Multiplier for the negative quadratic tail. Default 2.0.

        Returns:
            float: Score in [-inf, 1].
        """
        if max_flow <= min_flow:
            return 0.0

        x = (observed_flow - min_flow) / (max_flow - min_flow)

        if x >= 0.0:
            # Linear ramp: 0 at min_flow, 1 at max_flow, >1 beyond (capped at 1)
            return min(x, 1.0)
        else:
            # Negative quadratic tail below min_flow (same convention as bell)
            return -tail_scale * (x**2)

    def _get_action_topo_vect(self, sub_impacted_id, action):
        """
        Retrieves the topology vector for a specific substation after an action is applied.

        This handles the Grid2Op topology vector format, merging the action's changes
        with the default state.

        Args:
            sub_impacted_id (int): The ID of the impacted substation.
            action (Action): The Grid2Op action being evaluated.

        Returns:
            tuple:
                - action_topo_vect (np.array): The new topology vector for the substation.
                - is_single_node (bool): True if the substation remains a single bus (no split).
        """
        topo_vect_init = self.obs_defaut.sub_topology(sub_id=sub_impacted_id)
        is_single_node = len(set(topo_vect_init) - {0, -1}) == 1

        impact_obs = self.obs_defaut + action
        sub_info = self.obs_defaut.sub_info

        # Calculate start and end indices in the global topology vector
        start = int(np.sum(sub_info[:sub_impacted_id]))
        length = int(sub_info[sub_impacted_id])

        action_topo_vect = impact_obs.topo_vect[start : start + length]

        # Preserve disconnected status from initial state if applicable
        action_topo_vect[topo_vect_init <= 0] = topo_vect_init[topo_vect_init <= 0]

        return action_topo_vect, is_single_node

    def _get_assets_on_bus_for_sub(self, sub_id, bus, bus_assignments=None):
        """Get asset names connected to a specific bus at a substation.

        Uses the element ordering from ``get_obj_connect_to`` / ``sub_topology``
        (loads, generators, lines_or, lines_ex) to match each element to its bus.

        Args:
            sub_id: Substation index.
            bus: Target bus number (1 or 2).
            bus_assignments: Optional array of bus assignments for all elements at
                the substation (e.g. from an action's topology vector).
                If *None*, the current observation's ``sub_topology`` is used.

        Returns:
            dict with keys ``"lines"``, ``"loads"``, ``"generators"``,
            each containing a list of element name strings.
        """
        obs = self.obs_defaut
        obj = obs.get_obj_connect_to(substation_id=sub_id)

        if bus_assignments is None:
            bus_assignments = obs.sub_topology(sub_id=sub_id)

        # sub_topology order: loads, gens, lines_or, lines_ex
        load_ids = obj.get("loads_id", [])
        gen_ids = obj.get("generators_id", [])
        line_or_ids = obj.get("lines_or_id", [])
        line_ex_ids = obj.get("lines_ex_id", [])

        assets = {"lines": [], "loads": [], "generators": []}
        pos = 0

        for load_idx in load_ids:
            if pos < len(bus_assignments) and int(bus_assignments[pos]) == bus:
                if hasattr(obs, "name_load") and load_idx < len(obs.name_load):
                    assets["loads"].append(str(obs.name_load[load_idx]))
            pos += 1

        for gen_idx in gen_ids:
            if pos < len(bus_assignments) and int(bus_assignments[pos]) == bus:
                if hasattr(obs, "name_gen") and gen_idx < len(obs.name_gen):
                    assets["generators"].append(str(obs.name_gen[gen_idx]))
            pos += 1

        for line_idx in line_or_ids:
            if pos < len(bus_assignments) and int(bus_assignments[pos]) == bus:
                line_name = str(obs.name_line[line_idx])
                if line_name not in assets["lines"]:
                    assets["lines"].append(line_name)
            pos += 1

        for line_idx in line_ex_ids:
            if pos < len(bus_assignments) and int(bus_assignments[pos]) == bus:
                line_name = str(obs.name_line[line_idx])
                if line_name not in assets["lines"]:
                    assets["lines"].append(line_name)
            pos += 1

        return assets

    def _edge_names_buses_dict(self, obs, action_topo_vect, sub_impacted_id):
        """
        Creates a mapping between line names and their bus assignments.

        Args:
            obs (Observation): The Grid2Op observation object.
            action_topo_vect (np.array): The topology vector of the substation.
            sub_impacted_id (int): The ID of the substation.

        Returns:
            dict: Keys are line names, Values are bus IDs (e.g., {'Line_A': 1, 'Line_B': 2}).
        """
        dict_edge_names_buses = {}

        length = len(action_topo_vect)
        sub_info = obs.sub_info
        start = int(np.sum(sub_info[:sub_impacted_id]))

        for i in range(length):
            topo_vect_pos = start + i
            element = obs.topo_vect_element(topo_vect_pos)

            # Check if the element corresponds to a line (not a load or gen)
            if "line_id" in element:
                if "line_or_id" in element:
                    line_id = element["line_or_id"]  # Origin
                else:
                    line_id = element["line_ex_id"]  # Extremity

                line_name = obs.name_line[line_id]
                dict_edge_names_buses[line_name] = action_topo_vect[i]

        return dict_edge_names_buses

    def _edge_names_buses_dict_new(self, action_dict):
        """
        Creates a mapping between line names and their bus assignments.

        Args:
            obs (Observation): The Grid2Op observation object.
            action_topo_vect (np.array): The topology vector of the substation.
            sub_impacted_id (int): The ID of the substation.

        Returns:
            dict: Keys are line names, Values are bus IDs (e.g., {'Line_A': 1, 'Line_B': 2}).
        """
        dict_edge_names_buses = {}
        dict_edge_names_buses.update(action_dict["set_bus"]["lines_or_id"])
        dict_edge_names_buses.update(action_dict["set_bus"]["lines_ex_id"])

        return dict_edge_names_buses

    def _get_subs_impacted_from_action_desc(self, action_desc: Dict) -> List[int]:
        """
        Extract impacted substation IDs from an action description.

        This is a backend-agnostic way to find which substations are affected
        by a topology action, without relying on grid2op's impact_on_objects().

        Args:
            action_desc: Action description dictionary with 'content' key

        Returns:
            List of substation IDs that are impacted by this action
        """
        subs_impacted = set()
        content = action_desc.get("content", {})
        set_bus = content.get("set_bus", {})

        # Check for substations_id (direct substation topology changes)
        if "substations_id" in set_bus:
            for sub_id, _ in set_bus["substations_id"]:
                subs_impacted.add(sub_id)

        # Check for lines_or_id (line origin bus changes)
        if "lines_or_id" in set_bus:
            for line_name, bus in set_bus["lines_or_id"].items():
                # Find substation for this line's origin
                line_id_array = np.where(self.obs_defaut.name_line == line_name)[0]
                if line_id_array.size > 0:
                    line_id = line_id_array[0]
                    sub_id = self.obs_defaut.line_or_to_subid[line_id]
                    subs_impacted.add(sub_id)

        # Check for lines_ex_id (line extremity bus changes)
        if "lines_ex_id" in set_bus:
            for line_name, bus in set_bus["lines_ex_id"].items():
                # Find substation for this line's extremity
                line_id_array = np.where(self.obs_defaut.name_line == line_name)[0]
                if line_id_array.size > 0:
                    line_id = line_id_array[0]
                    sub_id = self.obs_defaut.line_ex_to_subid[line_id]
                    subs_impacted.add(sub_id)

        # Check for loads_id (load bus changes)
        if "loads_id" in set_bus:
            for load_name, bus in set_bus["loads_id"].items():
                # Find substation for this load
                load_id_array = np.where(self.obs_defaut.name_load == load_name)[0]
                if load_id_array.size > 0 and hasattr(self.obs_defaut, "load_to_subid"):
                    load_id = load_id_array[0]
                    sub_id = self.obs_defaut.load_to_subid[load_id]
                    subs_impacted.add(sub_id)

        # Check for generators_id (generator bus changes)
        if "generators_id" in set_bus:
            for gen_name, bus in set_bus["generators_id"].items():
                # Find substation for this generator
                gen_id_array = np.where(self.obs_defaut.name_gen == gen_name)[0]
                if gen_id_array.size > 0 and hasattr(self.obs_defaut, "gen_to_subid"):
                    gen_id = gen_id_array[0]
                    sub_id = self.obs_defaut.gen_to_subid[gen_id]
                    subs_impacted.add(sub_id)

        return list(subs_impacted)


# Install the back-compat @property bridges (identified_reconnections,
# scores_splits_dict, scores_pst_actions, …) onto DiscovererBase so every legacy
# per-family attribute read/write proxies onto self.results[key].<field>. The
# composed ActionDiscoverer inherits them via the MRO.
install_family_result_properties(DiscovererBase)
