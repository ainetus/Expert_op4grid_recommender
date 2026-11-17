# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Expert-system action discovery (rule filter + AlphaDeesp discovery).

Extracted out of the historical ``main.py`` so the expert model no longer
imports the pipeline entry point (dissolving the ``models.expert →
main._run_expert_discovery`` cycle). Everything here depends only on lower
layers (``action_evaluation`` / ``graph_analysis`` / ``utils`` / ``config``)
and reads the :class:`AnalysisContext` handed in by the pipeline.

The pypowsybl shared-baseline routing that ``main.py`` used to install by
monkey-patching private methods of a live ``ActionDiscoverer`` is now expressed
declaratively: the :class:`SimulationBackend` on the context exposes the
candidate-check / baseline callables and the two configuration flags, and they
are passed straight into the :class:`ActionDiscoverer` constructor.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, Tuple

from expert_op4grid_recommender import config
from expert_op4grid_recommender.action_evaluation.discovery import ActionDiscoverer
from expert_op4grid_recommender.action_evaluation.rules import ActionRuleValidator
from expert_op4grid_recommender.graph_analysis.processor import (
    get_constrained_and_dispatch_paths,
    pre_process_antenna_graph,
    pre_process_graph_alphadeesp,
)
from expert_op4grid_recommender.utils.helpers import Timer, print_filtered_out_action

if TYPE_CHECKING:  # avoid a runtime models → pipeline edge
    from expert_op4grid_recommender.pipeline import AnalysisContext


def _run_expert_action_filter(context: "AnalysisContext") -> None:
    """Path analysis + expert rule validation.

    Populates ``context["filtered_candidate_actions"]`` with the action
    IDs retained by the expert rules (overflow-graph paths + per-action
    domain logic in :class:`ActionRuleValidator`). Idempotent: returns
    immediately when the field is already populated.

    Requires the overflow-graph artefacts (``g_distribution_graph``,
    ``hubs``) to be in ``context`` — i.e. callers must run
    ``run_analysis_step2_graph`` first.

    Surfaced as a standalone helper so non-expert recommenders that
    declare ``requires_overflow_graph=True`` (e.g.
    ``RandomOverflowRecommender``) can sample from the same reduced action
    space the expert sees, without paying for the full
    :class:`ActionDiscoverer` scoring pass.
    """
    if context.get("filtered_candidate_actions"):
        return

    env = context["env"]
    obs = context["obs"]
    current_timestep = context["current_timestep"]
    current_lines_defaut = context["current_lines_defaut"]
    lines_overloaded_ids = context["lines_overloaded_ids"]
    lines_overloaded_ids_kept = context["lines_overloaded_ids_kept"]
    maintenance_to_reco_at_t = context["maintenance_to_reco_at_t"]
    lines_we_care_about = context["lines_we_care_about"]
    classifier = context["classifier"]
    dict_action = context["dict_action"]
    hubs = context["hubs"]
    g_distribution_graph = context["g_distribution_graph"]

    with Timer("Path Analysis & Rule Validation"):
        lines_blue_paths, nodes_blue_path, lines_dispatch, nodes_dispatch_path = get_constrained_and_dispatch_paths(
            g_distribution_graph, obs, lines_overloaded_ids, lines_overloaded_ids_kept
        )

        validator = ActionRuleValidator(
            obs=obs,
            action_space=env.action_space,
            classifier=classifier,
            hubs=hubs,
            paths=((lines_blue_paths, nodes_blue_path), (lines_dispatch, nodes_dispatch_path)),
            by_description=config.CHECK_WITH_ACTION_DESCRIPTION,
        )

        actions_to_filter, actions_unfiltered = validator.categorize_actions(
            dict_action=dict_action,
            timestep=current_timestep,
            defauts=current_lines_defaut,
            overload_ids=lines_overloaded_ids,
            lines_reco_maintenance=maintenance_to_reco_at_t,
            lines_we_care_about=lines_we_care_about,
            do_simulation_checks=config.CHECK_ACTION_SIMULATION,
        )

        print_filtered_out_action(len(dict_action), actions_to_filter)

    context["filtered_candidate_actions"] = list(actions_unfiltered.keys())


def _run_expert_discovery(context: "AnalysisContext", n_action_max: int = None
                          ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Path analysis + rule validation + AlphaDeesp pre-processing + expert discovery.

    Filtering of candidate actions is delegated to
    :func:`_run_expert_action_filter` (idempotent) so the same logic
    feeds both the expert discoverer AND non-expert recommenders that
    request the filtered action set.

    Side-effect: when the overflow graph was built in DC mode, switches
    ``context["env"]`` and ``context["obs"]`` to fresh AC instances so
    the downstream reassessment runs against the AC state.

    Returns ``(prioritized_actions, action_scores)`` from the underlying
    :class:`ActionDiscoverer`.
    """
    antenna_mode = bool(context.get("antenna_mode"))
    if not antenna_mode:
        # Topological candidate filtering is irrelevant in antenna mode (only
        # injection actions are discovered, and those are created directly).
        _run_expert_action_filter(context)

    backend = context["backend"]
    env = context["env"]
    obs = context["obs"]
    obs_simu_defaut = context["obs_simu_defaut"]
    analysis_date = context["analysis_date"]
    current_timestep = context["current_timestep"]
    current_lines_defaut = context["current_lines_defaut"]
    lines_overloaded_ids = context["lines_overloaded_ids"]
    act_reco_maintenance = context["act_reco_maintenance"]
    lines_non_reconnectable = context["lines_non_reconnectable"]
    lines_we_care_about = context["lines_we_care_about"]
    classifier = context["classifier"]
    non_connected_reconnectable_lines = context["non_connected_reconnectable_lines"]
    dict_action = context["dict_action"]

    g_overflow = context["g_overflow"]
    overflow_sim = context["overflow_sim"]
    hubs = context["hubs"]
    node_name_mapping = context["node_name_mapping"]
    use_dc = context["use_dc"]

    if n_action_max is None:
        n_action_max = config.N_PRIORITIZED_ACTIONS

    with Timer("Pre-process for AlphaDeesp"):
        if antenna_mode:
            g_overflow_processed, g_distribution_graph_processed, simulator_data = pre_process_antenna_graph(
                g_overflow, node_name_mapping
            )
        else:
            g_overflow_processed, g_distribution_graph_processed, simulator_data = pre_process_graph_alphadeesp(
                g_overflow, overflow_sim, node_name_mapping
            )

    if use_dc:
        print("Warning: you have used the DC load flow, so results are more approximate")
        env, obs, path_chronic = backend.get_env_first_obs(
            config.ENV_FOLDER, config.ENV_NAME, config.USE_EVALUATION_CONFIG,
            analysis_date, is_DC=False,
        )
        context["env"] = env
        context["obs"] = obs

    actions_unfiltered_keys = set(context.get("filtered_candidate_actions") or [])

    with Timer("Action Discovery"):
        # The per-backend candidate-check / baseline callables and the two
        # shared-baseline configuration flags come straight from the backend —
        # no monkey-patching of the live discoverer (previously done in main.py
        # for pypowsybl). On grid2op these fall back to the grid2op simulation
        # defaults inside ``DiscovererBase``; on pypowsybl they carry fast_mode
        # and route topological candidates through the single shared baseline.
        discoverer = ActionDiscoverer(
            env=env,
            obs=obs,
            obs_defaut=obs_simu_defaut,
            timestep=current_timestep,
            lines_defaut=current_lines_defaut,
            lines_overloaded_ids=lines_overloaded_ids,
            act_reco_maintenance=act_reco_maintenance,
            classifier=classifier,
            non_connected_reconnectable_lines=non_connected_reconnectable_lines,
            all_disconnected_lines=lines_non_reconnectable + non_connected_reconnectable_lines,
            dict_action=dict_action,
            actions_unfiltered=actions_unfiltered_keys,
            hubs=hubs,
            g_overflow=g_overflow_processed,
            g_distribution_graph=g_distribution_graph_processed,
            simulator_data=simulator_data,
            check_action_simulation=config.CHECK_ACTION_SIMULATION,
            lines_we_care_about=lines_we_care_about,
            check_rho_reduction_func=backend.check_rho_reduction,
            create_default_action_func=backend.create_default_action,
            compute_baseline_func=backend.compute_baseline,
            check_rho_with_baseline_func=backend.check_rho_with_baseline,
            branch_candidates_from_baseline=backend.branch_candidates_from_baseline,
            use_shared_baseline_for_topological=backend.use_shared_baseline_for_topological,
            obs_linecut=getattr(overflow_sim, 'obs_linecut', None),
            antenna_mode=antenna_mode,
            antenna_meta=context.get("antenna_meta"),
            structural_antennas=context.get("structural_antennas"),
        )

        prioritized_actions, action_scores = discoverer.discover_and_prioritize(
            n_action_max=n_action_max
        )

    print("\nPrioritized actions are: " + str(list(prioritized_actions.keys())))
    return prioritized_actions, action_scores
