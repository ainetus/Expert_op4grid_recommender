#!/usr/bin/python3
# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Analysis pipeline: the typed spine of the expert-system analysis.

This module owns the three-step pipeline (``run_analysis_step1`` →
``run_analysis_step2_graph`` → ``run_analysis_step2_discovery``) and the two
typed payloads that thread through it:

- :class:`AnalysisContext` — replaces the historical ~41-key context dict. It
  carries typed fields plus a single :class:`SimulationBackend`, instead of the
  eight backend-selected function pointers the dict used to hold.
- :class:`AnalysisResult` — replaces the untyped result dict.

Both mix in :class:`DictCompatMixin` so existing dict-style consumers keep
working (``ctx["obs"]``, ``result.get("action_scores", {})``) while the
pipeline itself reads them as typed objects.

``run_analysis_step1`` returns ``AnalysisContext | AnalysisResult`` (an
``AnalysisResult`` short-circuits when there is nothing actionable) instead of
the old ``(Optional, Optional)`` sentinel tuple.

Layering: ``cli → pipeline → models → action_evaluation/graph_analysis →
utils``. The expert discovery lives under :mod:`models` (imported here, never
the reverse); the per-backend physics lives in :mod:`backends`.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

import numpy as np

from expert_op4grid_recommender import config
from expert_op4grid_recommender.backends import Backend, SimulationBackend, make_backend
from expert_op4grid_recommender.models.base import DictCompatMixin
from expert_op4grid_recommender.graph_analysis.processor import (
    identify_overload_lines_to_keep_overflow_graph_connected,
    extract_antenna_context,
)
from expert_op4grid_recommender.graph_analysis.antenna_graph import (
    build_antenna_overflow_graph,
    focus_overflow_graph_on_pocket,
)
from expert_op4grid_recommender.graph_analysis.visualization import (
    make_overflow_graph_visualization,
    get_graph_file_name,
)
from expert_op4grid_recommender.action_evaluation.classifier import ActionClassifier
from expert_op4grid_recommender.utils.helpers import (
    Timer,
    get_maintenance_timestep,
    save_data_for_test,
)
from expert_op4grid_recommender.data_loader import enrich_actions_lazy


# =============================================================================
# TYPED PIPELINE PAYLOADS
# =============================================================================

@dataclass(eq=False, repr=False)
class AnalysisContext(DictCompatMixin):
    """State threaded through the analysis pipeline (typed, dict-compatible).

    Replaces the historical context dict. ``backend`` (a
    :class:`SimulationBackend`) supersedes the eight backend-selected function
    pointers the dict used to carry. Fields populated by
    ``run_analysis_step1`` have no meaningful default; those added by
    ``run_analysis_step2_graph`` default to ``None`` so a step-1 context is
    valid on its own.
    """

    # --- populated by step 1 ------------------------------------------------
    backend: Optional[SimulationBackend] = None
    env: Any = None
    obs: Any = None
    obs_simu_defaut: Any = None
    analysis_date: Any = None
    current_timestep: int = 0
    current_lines_defaut: List[str] = field(default_factory=list)
    lines_overloaded_ids: List[int] = field(default_factory=list)
    lines_overloaded_ids_kept: List[int] = field(default_factory=list)
    maintenance_to_reco_at_t: Any = None
    act_reco_maintenance: Any = None
    lines_non_reconnectable: Any = None
    lines_we_care_about: Any = None
    classifier: Any = None
    custom_layout: Any = None
    chronic_name: Any = None
    pre_existing_rho: Dict[int, float] = field(default_factory=dict)
    lines_overloaded_names: List[str] = field(default_factory=list)
    non_connected_reconnectable_lines: List[str] = field(default_factory=list)
    extra_lines_to_cut_ids: List[int] = field(default_factory=list)
    dict_action: dict = field(default_factory=dict)
    is_bare_env: bool = False
    is_pypowsybl: bool = False
    actual_fast_mode: bool = False
    antenna_mode: bool = False
    antenna_info: Any = None
    # Network handles — left None so build_recommender_inputs extracts them
    # from the environment/observation exactly as before.
    n_grid: Any = None
    n_grid_defaut: Any = None

    # --- populated by step 2 (graph) ---------------------------------------
    df_of_g: Any = None
    overflow_sim: Any = None
    g_overflow: Any = None
    hubs: Any = None
    g_distribution_graph: Any = None
    node_name_mapping: Any = None
    antenna_meta: Any = None
    use_dc: bool = False

    # --- populated lazily by the expert filter / reassessment --------------
    filtered_candidate_actions: Optional[List[str]] = None
    reassessment_parallelism: Any = None


@dataclass(eq=False, repr=False)
class AnalysisResult(DictCompatMixin):
    """Final analysis payload (typed, dict-compatible).

    Replaces the untyped result dict returned by ``run_analysis`` /
    ``run_analysis_step2_discovery``. Also used as the short-circuit return of
    ``run_analysis_step1`` when there is no actionable overload.
    """

    lines_overloaded_names: List[str] = field(default_factory=list)
    prioritized_actions: dict = field(default_factory=dict)
    action_scores: dict = field(default_factory=dict)
    pre_existing_overloads: list = field(default_factory=list)
    combined_actions: dict = field(default_factory=dict)
    antenna_meta: Any = None
    prediction_time: Any = None
    assessment_time: Any = None
    reassessment_parallelism: Any = None


#: The two possible outcomes of ``run_analysis_step1``.
Step1Outcome = Union[AnalysisContext, AnalysisResult]


def _empty_action_scores() -> Dict[str, dict]:
    """The empty per-category score table used by the no-overload short-circuit."""
    return {
        "line_reconnection": {"scores": {}, "params": {}},
        "line_disconnection": {"scores": {}, "params": {}},
        "open_coupling": {"scores": {}, "params": {}},
        "close_coupling": {"scores": {}, "params": {}},
        "renewable_curtailment": {"scores": {}, "params": {}},
    }


# =============================================================================
# THERMAL LIMITS
# =============================================================================

def set_thermal_limits(n_grid, env, thresold_thermal_limit=0.95):
    """Set thermal limits from network operational limits."""
    default_th_lim_value = 9999.

    thermal_limits_df = n_grid.get_operational_limits().reset_index()
    branches = thermal_limits_df.element_id.unique()

    all_branches = set(
        list(n_grid.get_lines().index) + list(n_grid.get_2_windings_transformers().index))
    dict_thermal_limits = {name: default_th_lim_value for name in all_branches}

    for branch in branches:
        thermal_limits_branch = thermal_limits_df[thermal_limits_df.element_id == branch]
        dict_thermal_limits[branch] = np.round(
            thresold_thermal_limit * thermal_limits_branch[thermal_limits_branch.name == "permanent_limit"][
                "value"].min())

    th_lim_dict_day_arr = [dict_thermal_limits[l_name] for l_name in env.name_line]
    env.set_thermal_limit(th_lim_dict_day_arr)

    return env


# =============================================================================
# STEP 1 — overload detection & selection
# =============================================================================

def run_analysis_step1(analysis_date: Optional[datetime],
                       current_timestep: int,
                       current_lines_defaut: List[str],
                       env_path: Optional[str] = None,
                       env_name: Optional[str] = None,
                       backend: Backend = Backend.GRID2OP,
                       fast_mode: Optional[bool] = None,
                       dict_action: Optional[Dict[str, Any]] = None,
                       prebuilt_env_context: Optional[Dict[str, Any]] = None,
                       prebuilt_obs_simu_defaut: Optional[Any] = None) -> Step1Outcome:
    """First part of the expert system analysis up to detection and selection of overloads.

    Returns an :class:`AnalysisContext` to continue into step 2, or an
    :class:`AnalysisResult` short-circuit when the overload has no topological
    solution (islands the grid, no antenna pocket). This replaces the old
    ``(Optional[result], Optional[context])`` sentinel tuple.

    ``prebuilt_obs_simu_defaut`` lets a caller skip the contingency
    load-flow when the post-contingency observation has already been
    computed elsewhere (e.g. for the N-1 diagram view). The caller
    vouches for convergence; the function trusts the provided
    observation and proceeds straight to overload detection.
    """
    # Validate/announce the backend before parsing the date — preserves the
    # original ordering (an unknown backend raises before a bad date does).
    if backend == Backend.GRID2OP:
        print("Using Grid2Op backend")
    elif backend == Backend.PYPOWSYBL:
        print("Using pure pypowsybl backend")
    else:
        raise ValueError(f"Unknown backend: {backend}")

    if analysis_date is None:
        is_bare_env = True
    elif isinstance(analysis_date, str):
        try:
            analysis_date = datetime.strptime(analysis_date, "%Y-%m-%d")
            is_bare_env = False
        except ValueError:
            raise ValueError("Error: Date format must be YYYY-MM-DD")
    elif isinstance(analysis_date, datetime):
        is_bare_env = False
    else:
        raise TypeError("analysis_date must be a datetime object, string, or None")

    if fast_mode:
        actual_fast_mode = True
    else:
        actual_fast_mode = config.PYPOWSYBL_FAST_MODE if fast_mode is None else fast_mode

    sim_backend = make_backend(backend, fast_mode=actual_fast_mode)
    is_pypowsybl = sim_backend.is_pypowsybl

    date_str = analysis_date.strftime('%Y-%m-%d') if analysis_date else "None"
    print(f"Running Step 1 analysis for Date: {date_str}, Timestep: {current_timestep}, Contingency: {current_lines_defaut}")

    # Route env overrides through the validated accessor rather than mutating
    # module attributes: overriding ENV_NAME recomputes the derived ENV_PATH /
    # ACTION_FILE_PATH (no staleness — review finding A3). ``env_path`` is a rare
    # direct-path escape hatch (only read by save_data_for_test); ENV_PATH is a
    # computed field, so it is set as a module attribute here after the override.
    if env_name is not None:
        config.override_settings(ENV_NAME=env_name)
    if env_path is not None:
        config.ENV_PATH = env_path

    with Timer("Environment Setup"):
        if prebuilt_env_context is not None and is_pypowsybl:
            env = prebuilt_env_context['env']
            # Sanitize a reused env before starting a new analysis. A prior
            # analysis whose overload-disconnection load flow diverged calls
            # switch_to_dc_load_flow_pypowsybl, which escalates this *shared* env
            # to DC (NetworkManager._default_dc=True) and DC-solves the base
            # variant (leaving every bus v_mag=NaN — DC computes no voltage
            # magnitudes). Left in place, that escalation leaks across analyses:
            # this analysis would run every load flow in DC, where branch
            # currents are not populated, so rho≈0 and real overloads silently
            # vanish (issue #6). When the env was escalated, restore the
            # configured baseline LF mode + a valid (energised) base state;
            # otherwise just reset the working variant, exactly as before.
            if getattr(env, "_dc_escalation_pending", False):
                env.reset_loadflow_mode_to_baseline(config.USE_DC_LOAD_FLOW)
            else:
                env.network_manager.reset_to_base()
            obs = env.get_obs()
            path_chronic = prebuilt_env_context['path_chronic']
            chronic_name = prebuilt_env_context['chronic_name']
            custom_layout = prebuilt_env_context.get('custom_layout')
            lines_non_reconnectable = prebuilt_env_context['lines_non_reconnectable']
            lines_we_care_about = prebuilt_env_context['lines_we_care_about']
            n_grid = env.network_manager.network
            raw_dict_action = None
        else:
            env, obs, path_chronic, chronic_name, custom_layout, raw_dict_action, lines_non_reconnectable, lines_we_care_about = sim_backend.setup_environment(
                analysis_date)

            n_grid = sim_backend.get_network(env)

            if np.mean(env.get_thermal_limit()) >= 10 ** 4:
                env = set_thermal_limits(n_grid, env, thresold_thermal_limit=config.MONITORING_FACTOR_THERMAL_LIMITS)
                obs = env.reset() if backend == Backend.GRID2OP else env.get_obs()

        if is_pypowsybl:
            if dict_action is None:
                dict_action = enrich_actions_lazy(raw_dict_action, n_grid)
        else:
            dict_action = raw_dict_action

    if is_pypowsybl:
        _branch_names = set(n_grid.get_lines().index) | set(n_grid.get_2_windings_transformers().index)
        _load_names = set(n_grid.get_loads(attributes=[]).index)
        classifier = ActionClassifier(grid2op_action_space=env.action_space,
                                      branch_names=_branch_names, load_names=_load_names)
    else:
        classifier = ActionClassifier(grid2op_action_space=env.action_space)

    if is_bare_env:
        act_reco_maintenance = env.action_space({})
        maintenance_to_reco_at_t = []
    else:
        if is_pypowsybl:
            from expert_op4grid_recommender.utils.helpers_pypowsybl import get_maintenance_timestep_pypowsybl
            act_reco_maintenance, maintenance_to_reco_at_t = get_maintenance_timestep_pypowsybl(
                env, obs, lines_non_reconnectable, config.DO_RECO_MAINTENANCE
            )
        else:
            act_reco_maintenance, maintenance_to_reco_at_t = get_maintenance_timestep(
                current_timestep, lines_non_reconnectable, env, config.DO_RECO_MAINTENANCE
            )

    with Timer("Initial Contingency Simulation"):
        if prebuilt_obs_simu_defaut is not None:
            # Caller pre-computed the post-contingency observation (e.g.
            # the host app built it while serving the N-1 diagram). Skip
            # the redundant LF / variant clone; the caller vouches for
            # convergence.
            obs_simu_defaut = prebuilt_obs_simu_defaut
            has_converged = True
        else:
            obs_simu_defaut, has_converged = sim_backend.simulate_contingency(
                env, obs, current_lines_defaut, act_reco_maintenance, current_timestep
            )
    if not has_converged:
        raise RuntimeError("Initial contingency simulation failed. Cannot proceed.")

    lines_overloaded_ids = []
    pre_existing_overloads = []
    pre_existing_rho = {}
    worsening_threshold = config.PRE_EXISTING_OVERLOAD_WORSENING_THRESHOLD

    for i, l_name in enumerate(obs_simu_defaut.name_line):
        if l_name in lines_we_care_about and obs_simu_defaut.rho[i] >= 1:
            if obs.rho[i] >= 1:
                if obs_simu_defaut.rho[i] > obs.rho[i] * (1 + worsening_threshold):
                    lines_overloaded_ids.append(i)
                else:
                    pre_existing_overloads.append(
                        f"{l_name} (rho_N={obs.rho[i]:.3f}, rho_N-1={obs_simu_defaut.rho[i]:.3f})"
                    )
                pre_existing_rho[i] = float(obs.rho[i])
            else:
                lines_overloaded_ids.append(i)
    if pre_existing_overloads:
        print(f"Ignoring {len(pre_existing_overloads)} pre-existing overload(s) "
              f"(already overloaded before contingency): {pre_existing_overloads}")
    non_connected_reconnectable_lines = [
        l_name for i, l_name in enumerate(env.name_line)
        if
        l_name not in current_lines_defaut and l_name not in lines_non_reconnectable and not
        obs_simu_defaut.line_status[i]
    ]
    if config.IGNORE_RECONNECTIONS:
        non_connected_reconnectable_lines = []

    lines_overloaded_ids_kept, prevent_islanded_subs = identify_overload_lines_to_keep_overflow_graph_connected(
        obs_simu_defaut, lines_overloaded_ids, config.DO_FORCE_OVERLOAD_GRAPH_EVEN_IF_GRAPH_BROKEN_APART
    )
    if prevent_islanded_subs:
        print(f"Warning: Not all overloads considered, as they would island these substations: {prevent_islanded_subs}")

    lines_overloaded_names = [obs_simu_defaut.name_line[i] for i in lines_overloaded_ids]

    antenna_info = None
    if not lines_overloaded_ids_kept:
        if config.ENABLE_ANTENNA_RECOMMENDATIONS:
            antenna_info = extract_antenna_context(obs_simu_defaut, lines_overloaded_ids)
        if antenna_info is None:
            print("Overload breaks the grid apart. No topological solution without load shedding.")
            return AnalysisResult(
                lines_overloaded_names=lines_overloaded_names,
                prioritized_actions={},
                action_scores=_empty_action_scores(),
            )
        print(
            "Overload islands a radial pocket — switching to antenna mode: building a "
            "downstream overflow graph on the islanded substations "
            f"{antenna_info['antenna_sub_names']} and restricting recommendations to "
            "injection actions (load shedding / curtailment / redispatch)."
        )

    return AnalysisContext(
        backend=sim_backend,
        env=env,
        obs=obs,
        obs_simu_defaut=obs_simu_defaut,
        analysis_date=analysis_date,
        current_timestep=current_timestep,
        current_lines_defaut=current_lines_defaut,
        lines_overloaded_ids=lines_overloaded_ids,
        lines_overloaded_ids_kept=lines_overloaded_ids_kept,
        maintenance_to_reco_at_t=maintenance_to_reco_at_t,
        act_reco_maintenance=act_reco_maintenance,
        lines_non_reconnectable=lines_non_reconnectable,
        lines_we_care_about=lines_we_care_about,
        classifier=classifier,
        custom_layout=custom_layout,
        chronic_name=chronic_name,
        pre_existing_rho=pre_existing_rho,
        lines_overloaded_names=lines_overloaded_names,
        non_connected_reconnectable_lines=non_connected_reconnectable_lines,
        extra_lines_to_cut_ids=[],
        dict_action=dict_action,
        is_bare_env=is_bare_env,
        is_pypowsybl=is_pypowsybl,
        actual_fast_mode=actual_fast_mode,
        antenna_mode=antenna_info is not None,
        antenna_info=antenna_info,
    )


# =============================================================================
# STEP 2 (graph) — overflow-graph build & visualization
# =============================================================================

def _make_antenna_visualization(context: AnalysisContext, df_of_g, g_overflow, hubs,
                                g_distribution_graph, obs_simu_defaut):
    """Render the antenna overflow graph (PDF/HTML), best-effort.

    Mirrors the regular step-2 visualization but for the antenna graph: no
    ``overflow_sim`` (so no before/after rho annotation) and no red dispatch
    loops (a radial pocket is a pure constrained tree). The analysis graph spans
    the full grid (the gray healthy lines anchor the root); here we render a copy
    focused on the pocket. Fully guarded — a rendering failure must never abort
    the analysis.
    """
    with Timer("Antenna Visualization"):
        graph_file_name = get_graph_file_name(
            context.current_lines_defaut, context.chronic_name,
            context.current_timestep, False,
        )
        save_folder = config.SAVE_FOLDER_VISUALIZATION
        lines_constrained_path = None
        nodes_constrained_path = None
        try:
            cp_lines, cp_nodes, _ob_e, _ob_n = g_distribution_graph.get_constrained_edges_nodes()
            lines_constrained_path = list(cp_lines)
            nodes_constrained_path = list(cp_nodes)
        except Exception as exc:
            print("Could not pre-compute constrained path for antenna viewer: " + str(exc))
        # Render a pocket-focused copy: the analysis graph is built over the full
        # grid (the gray healthy lines anchor the root for find_hubs), but the
        # operator only wants to see the islanded pocket. The visualization never
        # rebuilds the structured-overload graph, so trimming here is safe.
        antenna_info = context.antenna_info
        g_overflow_for_viz = focus_overflow_graph_on_pocket(
            g_overflow, obs_simu_defaut,
            antenna_info["root_sub_id"], antenna_info["antenna_sub_ids"],
        )
        try:
            make_overflow_graph_visualization(
                context.env, None, g_overflow_for_viz, hubs, obs_simu_defaut, save_folder,
                graph_file_name, lines_swapped=[], custom_layout=None,
                lines_we_care_about=context.get("lines_we_care_about"),
                monitoring_factor_thermal_limits=config.MONITORING_FACTOR_THERMAL_LIMITS,
                lines_constrained_path=lines_constrained_path,
                nodes_constrained_path=nodes_constrained_path,
                lines_red_loops=None, nodes_red_loops=None,
            )
        except Exception as exc:
            print(
                "Antenna overflow-graph visualization failed (continuing without it): "
                f"{type(exc).__name__}: {exc}"
            )


def _run_antenna_step2_graph(context: AnalysisContext) -> AnalysisContext:
    """Step-2 graph build for the islanded-pocket (antenna) case.

    The regular alphaDeesp pipeline cannot run a meaningful load-flow
    redistribution on a grid that the contingency breaks apart, so we feed the
    standard ``OverFlowGraph`` machinery the post-disconnection state implied by
    the islanding (initial flows, zeroed on the pocket) and let it build the
    graph (see ``graph_analysis.antenna_graph.build_antenna_overflow_graph``).
    The result is shaped exactly like the regular path so the downstream
    discovery / reassessment is unchanged — except ``overflow_sim`` is ``None``
    and the context carries ``antenna_meta``.
    """
    obs_simu_defaut = context.obs_simu_defaut
    antenna_info = context.antenna_info

    with Timer("Antenna Graph Building"):
        (df_of_g, overflow_sim, g_overflow, hubs, g_distribution_graph,
         node_name_mapping, antenna_meta) = build_antenna_overflow_graph(
            obs_simu_defaut,
            antenna_info["constraint_line_id"],
            antenna_info["antenna_sub_ids"],
            antenna_info["root_sub_id"],
        )

    if config.DO_VISUALIZATION:
        _make_antenna_visualization(context, df_of_g, g_overflow, hubs,
                                    g_distribution_graph, obs_simu_defaut)

    context.update({
        "df_of_g": df_of_g,
        "overflow_sim": overflow_sim,  # None — no Grid2opSimulation in antenna mode
        "g_overflow": g_overflow,
        "hubs": hubs,
        "g_distribution_graph": g_distribution_graph,
        "node_name_mapping": node_name_mapping,
        "antenna_meta": antenna_meta,
        "use_dc": False,
    })
    return context


def run_analysis_step2_graph(context: AnalysisContext) -> AnalysisContext:
    """Second part of the expert system analysis, focusing on graph generation and visualization."""
    if context.get("antenna_mode"):
        return _run_antenna_step2_graph(context)

    backend = context.backend
    env = context.env
    obs = context.obs
    obs_simu_defaut = context.obs_simu_defaut
    analysis_date = context.analysis_date
    current_timestep = context.current_timestep
    current_lines_defaut = context.current_lines_defaut
    lines_overloaded_ids_kept = context.lines_overloaded_ids_kept
    maintenance_to_reco_at_t = context.maintenance_to_reco_at_t
    lines_non_reconnectable = context.lines_non_reconnectable
    lines_we_care_about = context.lines_we_care_about
    custom_layout = context.custom_layout
    chronic_name = context.chronic_name
    non_connected_reconnectable_lines = context.non_connected_reconnectable_lines
    extra_lines_to_cut_ids = context.get("extra_lines_to_cut_ids") or []

    with Timer("Graph Building & DC Switch"):
        has_converged, has_lost_load = backend.check_simu_overloads(
            obs, obs_simu_defaut, env.action_space, current_timestep, current_lines_defaut,
            lines_overloaded_ids_kept, maintenance_to_reco_at_t,
        )

        # The overflow graph is a linear flow-transfer estimate: run it in DC
        # (fast) when configured, reserving AC for the per-action reassessment.
        use_dc = config.USE_DC_LOAD_FLOW or config.USE_DC_FOR_OVERFLOW_GRAPH
        if not has_converged and not config.DO_FORCE_OVERLOAD_GRAPH_EVEN_IF_GRAPH_BROKEN_APART:
            use_dc = True
            env, obs, obs_simu_defaut = backend.switch_to_dc(
                env, analysis_date, current_timestep, current_lines_defaut, lines_overloaded_ids_kept,
                maintenance_to_reco_at_t
            )

        df_of_g, overflow_sim, g_overflow, hubs, g_distribution_graph, node_name_mapping = backend.build_overflow_graph(
            env, obs_simu_defaut, lines_overloaded_ids_kept, non_connected_reconnectable_lines, lines_non_reconnectable,
            current_timestep, do_consolidate_graph=config.DO_CONSOLIDATE_GRAPH, use_dc=use_dc,
            extra_lines_to_cut_ids=extra_lines_to_cut_ids,
        )

    if config.DO_VISUALIZATION:
        with Timer("Visualization"):
            graph_file_name = get_graph_file_name(current_lines_defaut, chronic_name, current_timestep, use_dc)
            save_folder = config.SAVE_FOLDER_VISUALIZATION
            lines_swapped = list(df_of_g[df_of_g.new_flows_swapped].line_name)
            lines_constrained_path = None
            nodes_constrained_path = None
            lines_red_loops = None
            nodes_red_loops = None
            try:
                cp_lines, cp_nodes, _other_blue_edges, _other_blue_nodes = (
                    g_distribution_graph.get_constrained_edges_nodes()
                )
                lines_constrained_path = list(cp_lines)
                nodes_constrained_path = list(cp_nodes)
            except Exception as exc:
                print("Could not pre-compute constrained path for overflow viewer: " + str(exc))
            try:
                rl_lines, rl_nodes = g_distribution_graph.get_dispatch_edges_nodes(only_loop_paths=True)
                lines_red_loops = list(rl_lines)
                nodes_red_loops = list(rl_nodes)
            except Exception as exc:
                print("Could not pre-compute red-loop dispatch paths for overflow viewer: " + str(exc))
            # The overflow-graph visualization is a presentational artifact
            # (PDF/HTML overlay). Its rendering goes through alphaDeesp +
            # external tooling (graphviz `dot`/`neato`); when that tooling
            # fails or is missing, alphaDeesp raises (e.g. AssertionError in
            # display_geo). That must NOT abort step-2: the downstream action
            # discovery only needs the graph data structures, not the picture.
            try:
                make_overflow_graph_visualization(
                    env, overflow_sim, g_overflow, hubs, obs_simu_defaut, save_folder, graph_file_name, lines_swapped,
                    custom_layout, lines_we_care_about=lines_we_care_about,
                    monitoring_factor_thermal_limits=config.MONITORING_FACTOR_THERMAL_LIMITS,
                    lines_constrained_path=lines_constrained_path,
                    nodes_constrained_path=nodes_constrained_path,
                    lines_red_loops=lines_red_loops,
                    nodes_red_loops=nodes_red_loops,
                    extra_lines_to_cut_ids=extra_lines_to_cut_ids,
                )
            except Exception as exc:
                print(
                    "Overflow-graph visualization failed (continuing without it): "
                    f"{type(exc).__name__}: {exc}"
                )
    else:
        print("Skipping visualization (DO_VISUALIZATION=False)")

    if config.DO_SAVE_DATA_FOR_TEST:
        with Timer("Saving Test Data"):
            case_name = f"defaut_{'_'.join(current_lines_defaut)}_t{current_timestep}"
            save_data_for_test(config.ENV_PATH, case_name, df_of_g, overflow_sim, obs_simu_defaut,
                                lines_non_reconnectable, non_connected_reconnectable_lines, lines_overloaded_ids_kept)

    context.update({
        "env": env,
        "obs": obs,
        "obs_simu_defaut": obs_simu_defaut,
        "df_of_g": df_of_g,
        "overflow_sim": overflow_sim,
        "g_overflow": g_overflow,
        "hubs": hubs,
        "g_distribution_graph": g_distribution_graph,
        "node_name_mapping": node_name_mapping,
        "use_dc": use_dc,
    })
    return context


# =============================================================================
# STEP 2 (discovery) — recommendation model + reassessment
# =============================================================================

def run_analysis_step2_discovery(context: AnalysisContext,
                                 recommender=None,
                                 params: Optional[Dict[str, Any]] = None,
                                 ) -> AnalysisResult:
    """Run the chosen recommendation model and reassess its actions.

    Defaults to :class:`ExpertRecommender` so every existing caller
    behaves exactly like before. Pass any :class:`RecommenderModel`
    instance to swap in a different model (random, ML, ...).

    Whenever the overflow graph is available in the context (= step-2
    graph has run, either because the model required it or because the
    operator opted in via ``compute_overflow_graph=True``), the expert
    rule-validation filter (:func:`_run_expert_action_filter`) runs and
    populates ``inputs.filtered_candidate_actions``. The model is then
    free to use the expert-reduced action space if it wants — the data
    is just there. The filter is idempotent, so the Expert path stays
    a free no-op.

    The reassessment + combined-pair phase always runs and is shared
    across models.
    """
    from expert_op4grid_recommender.models.expert import ExpertRecommender
    from expert_op4grid_recommender.models._expert_discovery import (
        _run_expert_action_filter,
    )
    from expert_op4grid_recommender.utils.reassessment import (
        build_recommender_inputs,
        compute_combined_pairs,
        propagate_non_convergence_to_scores,
        reassess_prioritized_actions,
    )

    if recommender is None:
        recommender = ExpertRecommender()
    if params is None:
        params = {"n_prioritized_actions": config.N_PRIORITIZED_ACTIONS}

    # Run the expert rule-validation filter whenever the overflow graph
    # is available — be it because the model required it, or because
    # the operator opted in (``compute_overflow_graph=True``). Idempotent
    # — free no-op when already populated (Expert path).
    if context.get("g_distribution_graph") is not None and not context.get("antenna_mode"):
        _run_expert_action_filter(context)

    inputs = build_recommender_inputs(context)
    _t_predict = time.time()
    output = recommender.recommend(inputs, params)
    prediction_time = time.time() - _t_predict

    _t_assess = time.time()
    detailed_actions, pre_existing_info = reassess_prioritized_actions(
        output.prioritized_actions, context
    )
    action_scores = propagate_non_convergence_to_scores(
        detailed_actions, output.action_scores
    )
    combined_actions = compute_combined_pairs(detailed_actions, context)
    assessment_time = time.time() - _t_assess

    return AnalysisResult(
        lines_overloaded_names=context.lines_overloaded_names,
        prioritized_actions=detailed_actions,
        action_scores=action_scores,
        pre_existing_overloads=pre_existing_info,
        combined_actions=combined_actions,
        # Present only for the islanded-pocket (antenna) case: describes the
        # disconnected radial pocket and its net direction so callers can flag
        # it to the operator. ``None`` for the regular path.
        antenna_meta=context.get("antenna_meta"),
        # Per-stage execution times (seconds). ``prediction_time`` is the
        # model's intrinsic recommend() call; ``assessment_time`` is the
        # re-simulation + combined-pair estimation that scales with the number
        # of prioritized actions.
        prediction_time=prediction_time,
        assessment_time=assessment_time,
        # How the per-action reassessment was parallelised (workers / cores).
        reassessment_parallelism=context.get("reassessment_parallelism"),
    )


def run_analysis_step2(context: AnalysisContext) -> AnalysisResult:
    """Compatibility wrapper for the previous monolithic Step 2."""
    context = run_analysis_step2_graph(context)
    return run_analysis_step2_discovery(context)


def run_analysis(analysis_date: Optional[datetime],
                 current_timestep: int,
                 current_lines_defaut: List[str],
                 env_path: Optional[str] = None,
                 env_name: Optional[str] = None,
                 backend: Backend = Backend.GRID2OP,
                 fast_mode: Optional[bool] = None) -> AnalysisResult:
    """Runs the expert system analysis for a given date, timestep, and contingency."""
    outcome = run_analysis_step1(
        analysis_date=analysis_date,
        current_timestep=current_timestep,
        current_lines_defaut=current_lines_defaut,
        env_path=env_path,
        env_name=env_name,
        backend=backend,
        fast_mode=fast_mode
    )

    if isinstance(outcome, AnalysisResult):
        return outcome

    context = run_analysis_step2_graph(outcome)
    return run_analysis_step2_discovery(context)
