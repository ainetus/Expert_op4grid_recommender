# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Node merging (bus merge) discovery and scoring mixin."""
import numpy as np
import networkx as nx
from typing import Dict, List, Tuple

from expert_op4grid_recommender.utils.helpers import get_theta_node

class NodeMergingMixin:
    """Node merging (bus merge) discovery and scoring mixin."""

    def compute_node_merging_score(
        self, sub_id: int, connected_buses: list
    ) -> Tuple[float, Dict]:
        """
        Computes a heuristic score for a node merging action based on the voltage angle
        difference (delta phase) between the two buses being merged.

        The bus connected to the red loop (carrying positive dispatch flow) is identified
        as the one with more positive capacity on its overflow graph edges.
        Its phase is theta1. The other bus has phase theta2.

        Score = theta2 - theta1. A positive score means flows would naturally go from
        the higher-phase bus (theta2) towards the lower-phase red loop bus (theta1),
        which is the desired direction to relieve overloads.

        Args:
            sub_id: The integer index of the substation being merged.
            connected_buses: List of connected bus IDs (e.g., [1, 2]).

        Returns:
            Tuple[float, Dict]: The delta phase score and a details dict containing
                ``targeted_node_assets`` (lines, loads, generators on the red loop bus).
        """
        buses = sorted(connected_buses)
        if len(buses) < 2:
            return 0.0, {}

        # Determine which bus carries the red loop (positive dispatch) flow
        # by summing the positive capacity on overflow graph edges per bus
        capacity_dict = nx.get_edge_attributes(self.g_overflow.g, "capacity")
        edge_names = nx.get_edge_attributes(self.g_overflow.g, "name")

        # Build line-to-bus mapping for this substation using line_or/ex_to_subid and bus arrays
        self._build_lookup_caches()
        obs = self.obs_defaut

        line_to_bus = {}
        for line_id, line_name in enumerate(obs.name_line):
            if obs.line_or_to_subid[line_id] == sub_id:
                line_to_bus[line_name] = int(obs.line_or_bus[line_id])
            elif obs.line_ex_to_subid[line_id] == sub_id:
                line_to_bus[line_name] = int(obs.line_ex_bus[line_id])

        # Sum positive capacity per bus from overflow graph edges
        positive_flow_per_bus = {bus: 0.0 for bus in buses}
        all_edges = list(self.g_overflow.g.out_edges(sub_id, keys=True)) + list(
            self.g_overflow.g.in_edges(sub_id, keys=True)
        )

        for edge in all_edges:
            cap = capacity_dict.get(edge, 0.0)
            if cap > 0:
                ename = edge_names.get(edge, "")
                bus = line_to_bus.get(ename)
                if bus in positive_flow_per_bus:
                    positive_flow_per_bus[bus] += cap

        # The red loop bus is the one with more positive dispatch flow
        red_loop_bus = max(buses, key=lambda b: positive_flow_per_bus.get(b, 0.0))
        other_bus = [b for b in buses if b != red_loop_bus][0]

        # Compute theta for each bus
        theta1 = get_theta_node(self.obs_defaut, sub_id, red_loop_bus)
        theta2 = get_theta_node(self.obs_defaut, sub_id, other_bus)

        score = theta2 - theta1

        # Build details with assets on the red loop bus (targeted node)
        assets = self._get_assets_on_bus_for_sub(sub_id, red_loop_bus)
        details = {
            "targeted_node_assets": assets,
        }

        return score, details

    def find_relevant_node_merging(
        self,
        nodes_dispatch_path_names: List[str],
        percentage_threshold_min_dispatch_flow=0.1,
    ):
        """
        Finds and evaluates relevant node merging actions on dispatch paths.

        Only substations that lie on loop dispatch paths and have 2+ connected buses
        are candidates. They are further filtered by requiring a minimum dispatch flow
        (at least ``percentage_threshold_min_dispatch_flow`` of the global max).

        Populates `self.identified_merges`, `self.effective_merges`, `self.ineffective_merges`,
        and `self.scores_merges`.

        Args:
            nodes_dispatch_path_names: List of substation names on dispatch paths.
            percentage_threshold_min_dispatch_flow: float between 0 and 1. threshold to filter out unsignificant node merging actions given not significant enough dispatch flow
        """
        identified, effective, ineffective = {}, [], []
        scores_map = {}
        details_map = {}
        capacity_dict = nx.get_edge_attributes(self.g_overflow.g, "capacity")
        max_dispatch_flow = max([abs(val) for val in capacity_dict.values()])

        print(
            f"Evaluating node merging for {len(nodes_dispatch_path_names)} substations..."
        )
        for sub_name in nodes_dispatch_path_names:
            sub_id_array = np.where(sub_name == self.obs.name_sub)[0]
            if sub_id_array.size == 0:
                continue
            sub_id = sub_id_array[0]
            current_sub_topo = self.obs.sub_topology(sub_id=sub_id)
            connected_buses = set(current_sub_topo) - {-1, 0}

            if len(connected_buses) >= 2:
                # check if significant enough, that is with some minimal redispatch flow
                sub_edges = list(self.g_overflow.g.out_edges(sub_id, keys=True)) + list(
                    self.g_overflow.g.in_edges(sub_id, keys=True)
                )

                max_dispatch_flow_node = max(
                    [abs(capacity_dict[edge]) for edge in sub_edges]
                )

                if (
                    max_dispatch_flow_node
                    >= max_dispatch_flow * percentage_threshold_min_dispatch_flow
                ):
                    topo_target = [
                        1 if bus_id >= 2 else bus_id for bus_id in current_sub_topo
                    ]
                    action = self.action_space(
                        {"set_bus": {"substations_id": [(sub_id, topo_target)]}}
                    )
                    action_id = f"node_merging_{sub_name}"
                    identified[action_id] = action

                    # Compute delta phase score and per-action details (including assets)
                    try:
                        score, details = self.compute_node_merging_score(
                            sub_id, list(connected_buses)
                        )
                        scores_map[action_id] = score
                        details_map[action_id] = details
                        print(
                            f"  Scored node merge {action_id}: delta_phase={score:.4f}"
                        )
                    except Exception as e:
                        print(f"  Warning: Could not score {action_id}: {e}")
                        scores_map[action_id] = 0.0
                        details_map[action_id] = {}

                    if self.check_action_simulation:
                        act_defaut = self._create_default_action(
                            self.action_space, self.lines_defaut
                        )
                        is_rho_reduction, _ = self._check_rho_reduction(
                            self.obs,
                            self.timestep,
                            act_defaut,
                            action,
                            self.lines_overloaded_ids,
                            self.act_reco_maintenance,
                            self.lines_we_care_about,
                        )
                        (effective if is_rho_reduction else ineffective).append(action)
                        print(
                            f"  {'Effective' if is_rho_reduction else 'Ineffective'} node merge: {action_id}"
                        )

        # Descending score (positive delta phase = desired flow direction), like
        # the other families: the prioritization phases take the first N entries
        # in insertion order (dispatch-path substation order otherwise).
        self.identified_merges = dict(
            sorted(
                identified.items(), key=lambda x: scores_map.get(x[0], 0), reverse=True
            )
        )
        self.effective_merges = effective
        self.ineffective_merges = ineffective
        self.scores_merges = scores_map
        self.params_merges = details_map
