# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Line reconnection discovery mixin."""
import networkx as nx
from typing import List, Set

from expert_op4grid_recommender.utils.helpers import (
    get_delta_theta_line,
    sort_actions_by_score,
)

class LineReconnectionMixin:
    """Line reconnection discovery mixin."""

    def verify_relevant_reconnections(
        self,
        lines_to_reconnect: Set[str],
        red_loop_paths: List[List[str]],
        percentage_threshold_min_dispatch_flow=0.1,
    ):
        """
        Finds and evaluates relevant line reconnections based on path checks and scoring.

        Populates `self.identified_reconnections`, `self.effective_reconnections`,
        and `self.ineffective_reconnections`.

        Args:
            lines_to_reconnect (Set[str]): Set of candidate line names for reconnection.
            red_loop_paths (List[List[str]]): List of relevant network paths (substation names).
        """
        if not lines_to_reconnect:
            return

        map_action_score = {}
        red_loop_paths_sorted = sorted(red_loop_paths, key=len)
        capacity_dict = nx.get_edge_attributes(self.g_overflow.g, "capacity")
        max_dispatch_flow = (
            max(abs(val) for val in capacity_dict.values()) if capacity_dict else 0.0
        )

        # --- Pre-compute data structures for O(1) lookups ---
        self._build_lookup_caches()
        self._build_active_edges_cache()

        # Pre-compute consecutive pair sets for each path
        path_pairs_list = self._build_path_consecutive_pairs(red_loop_paths_sorted)

        # Build reverse index: substation pair -> set of path indices that contain it
        self._reco_pair_to_paths = {}
        for path_idx, pairs_set in enumerate(path_pairs_list):
            for pair in pairs_set:
                if pair not in self._reco_pair_to_paths:
                    self._reco_pair_to_paths[pair] = set()
                self._reco_pair_to_paths[pair].add(path_idx)

        # Optimization: Pre-compute blocking status for all relevant segments globally
        # Identify which segments are "blocked" (no active bypass) for EACH disconnected line
        segment_to_blockers = {}
        for disc_line in self.all_disconnected_lines:
            disc_subs = self._line_to_subs.get(disc_line)
            if not disc_subs:
                continue
            u, v = disc_subs
            # Check if there's any active edge between these substations
            if not self._get_active_edges_between_cached(u, v):
                # This segment is blocked by this line
                pair = tuple(sorted((u, v)))
                if pair not in segment_to_blockers:
                    segment_to_blockers[pair] = set()
                segment_to_blockers[pair].add(disc_line)

        # Pre-compute blocking lines per path by unioning blocked segments
        self._reco_path_blockers = {}
        for path_idx, pairs_set in enumerate(path_pairs_list):
            path_blockers = set()
            for pair in pairs_set:
                sorted_pair = tuple(sorted(pair))
                if sorted_pair in segment_to_blockers:
                    path_blockers.update(segment_to_blockers[sorted_pair])
            self._reco_path_blockers[path_idx] = path_blockers

        # Pre-compute max dispatch flow per node in O(E) instead of O(V*deg)
        max_dispatch_flow_per_node = {node: 0.0 for node in self.g_overflow.g.nodes()}
        for (u, v, key), cap in capacity_dict.items():
            abs_cap = abs(cap)
            if abs_cap > max_dispatch_flow_per_node.get(u, 0.0):
                max_dispatch_flow_per_node[u] = abs_cap
            if abs_cap > max_dispatch_flow_per_node.get(v, 0.0):
                max_dispatch_flow_per_node[v] = abs_cap

        dispatch_flow_threshold = (
            max_dispatch_flow * percentage_threshold_min_dispatch_flow
        )

        print(f"Evaluating {len(lines_to_reconnect)} potential reconnections...")
        for line_reco in lines_to_reconnect:
            has_found_effective_path, path, other_line = (
                self._check_other_reconnectable_line_on_path(
                    line_reco, red_loop_paths_sorted
                )
            )

            if has_found_effective_path:
                line_id = self._line_name_to_id[line_reco]
                # check dispatch flow at substation extremities are significant enough
                start_path_sub_id = self._sub_name_to_id[path[0]]
                end_path_sub_id = self._sub_name_to_id[path[-1]]
                max_dispatch_flow_line_nodes = max(
                    max_dispatch_flow_per_node.get(start_path_sub_id, 0.0),
                    max_dispatch_flow_per_node.get(end_path_sub_id, 0.0),
                )

                if max_dispatch_flow_line_nodes <= dispatch_flow_threshold:
                    print(
                        f"  Skipping {line_reco}: Path not significant enough with max redispatch flow of only blocked by {max_dispatch_flow_line_nodes} MW."
                    )
                else:
                    try:
                        # Directional delta-theta and path-extremity dispatch-flow heuristics
                        # are tracked in marota/expert_op4grid_recommender#81.
                        # Example on contingency P.SAOLRONCI 20240828T0100Z, reco_line 'CREYSL71G.ILE',
                        # Theta CRESEY:-0.012, Theta G.ILE: -0.10. Reconnection will create a flow
                        # from CRESEY to G.ILE while the other direction would have brought more
                        # flow on CRESEY loop path which would have been beneficial. So this action
                        # should have been filtered out. Also most influential reconnections are the
                        # ones connecting constrained path and loop path, directly in parallel to
                        # the constrained path.
                        delta_theta = abs(
                            get_delta_theta_line(self.obs_defaut, line_id)
                        )
                        map_action_score["reco_" + line_reco] = {
                            "action": self.action_space(
                                {
                                    "set_bus": {
                                        "lines_or_id": {line_reco: 1},
                                        "lines_ex_id": {line_reco: 1},
                                    }
                                }
                            ),  # self.action_space({"set_line_status": [(line_reco, 1)]}),
                            "score": delta_theta,
                            "line_impacted": line_reco,
                        }
                    except (IndexError, ValueError) as e:
                        print(f"Warning: Could not process line {line_reco}: {e}")
            else:
                print(
                    f"  Skipping {line_reco}: Path potentially blocked by {other_line}."
                )

        # Sort by descending |delta-theta| (larger angle gap = more flow drawn
        # onto the reconnected line), consistent with every other family: the
        # prioritization phases take the first N entries in insertion order.
        actions, lines_impacted, scores = sort_actions_by_score(map_action_score)
        # scores_rev = list(reversed(scores))

        effective, ineffective, identified = (
            [],
            [],
            actions,
        )  # identified holds all passing path filter

        if self.check_action_simulation:
            print("  Simulating effectiveness...")
            act_defaut = self._create_default_action(
                self.action_space, self.lines_defaut
            )
            # Iterate using original descending score order from sort_actions_by_score
            for action_id, line_reco, score in zip(
                actions.keys(), lines_impacted, scores
            ):
                action = actions[action_id]
                is_rho_reduction, obs_simu_action = self._check_rho_reduction(
                    self.obs,
                    self.timestep,
                    act_defaut,
                    action,
                    self.lines_overloaded_ids,
                    self.act_reco_maintenance,
                    self.lines_we_care_about,
                )
                if is_rho_reduction:
                    print(f"    Effective: {line_reco} (Score: {score:.2f})")
                    effective.append(line_reco)
                else:
                    print(f"    Ineffective: {line_reco} (Score: {score:.2f})")
                    ineffective.append(line_reco)

        self.identified_reconnections = identified
        self.effective_reconnections = effective
        self.ineffective_reconnections = ineffective
        self.scores_reconnections = {
            action_id: map_action_score[action_id]["score"]
            for action_id in map_action_score
        }
        self.params_reconnections = {
            "percentage_threshold_min_dispatch_flow": percentage_threshold_min_dispatch_flow,
            "max_dispatch_flow": max_dispatch_flow,
        }

        # Clean up temporary caches specific to this call
        if hasattr(self, "_reco_pair_to_paths"):
            del self._reco_pair_to_paths
        if hasattr(self, "_reco_path_blockers"):
            del self._reco_path_blockers
