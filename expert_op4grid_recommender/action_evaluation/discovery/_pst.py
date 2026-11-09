# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Phase-shifter transformer tap discovery mixin."""
from typing import List

class PSTMixin:
    """Phase-shifter transformer tap discovery mixin."""

    def find_relevant_pst_actions(
        self, nodes_blue_path_names: List[str], red_loop_paths_names: List[List[str]]
    ):
        """
        Identifies and proposes PST tap variations for overloads on blue paths and red loops.

        Args:
            nodes_blue_path_names: List of substation names on the blue (constrained) path.
            red_loop_paths_names: List of paths (list of substation names) forming red loops.
        """
        # 1. Identify all PSTs and their current tap info
        try:
            pst_ids = self.obs._network_manager.get_pst_ids()
        except AttributeError:
            # Fallback if nm doesn't support get_pst_ids (e.g. grid2op backend)
            print(
                "Warning: Network manager does not support get_pst_ids, skipping PST discovery."
            )
            return

        if not pst_ids:
            return

        identified, effective, ineffective = {}, [], []
        scores_map = {}
        details_map = {}

        # Flatten red loop nodes for faster O(1) lookup
        red_loop_nodes_set = set()
        for path in red_loop_paths_names:
            red_loop_nodes_set.update(path)
        blue_path_nodes_set = set(nodes_blue_path_names)

        # Shared disconnection/PST flow bounds, memoised once per run. Replaces
        # the order-sensitive ``_disco_bounds`` del/lazy dance that made PST
        # scoring silently depend on line-disconnection running (and self-
        # deleting the cache) immediately before it (A5).
        _bounds = self._get_disconnection_bounds()
        max_overload_flow = _bounds.max_overload_flow

        nm = self.obs._network_manager

        print(f"Evaluating PST tap variations for {len(pst_ids)} PSTs...")

        for pst_id in pst_ids:
            # Map PST to its substations
            # From NetworkManager, we know transformers are in _line_ids
            # and we have _line_or_sub / _line_ex_sub caches
            sub1_name = nm._line_or_sub.get(pst_id)
            sub2_name = nm._line_ex_sub.get(pst_id)

            if not sub1_name or not sub2_name:
                continue

            # Decide if it's on a blue path or red loop
            is_blue = (
                sub1_name in blue_path_nodes_set or sub2_name in blue_path_nodes_set
            )
            is_red = sub1_name in red_loop_nodes_set or sub2_name in red_loop_nodes_set

            if not (is_blue or is_red):
                continue

            tap_info = nm.get_pst_tap_info(pst_id)
            if not tap_info:
                continue

            tap = tap_info["tap"]
            low = tap_info["low_tap"]
            high = tap_info["high_tap"]

            # Assume reference tap is in the middle
            ref_tap = (low + high) // 2

            # Variation: 2 steps when possible
            variation = 0
            if is_blue:
                # Rule: Increase Impedance (Move away from reference)
                if tap >= ref_tap:
                    target_tap = min(high, tap + 2)
                    if target_tap > tap:
                        variation = target_tap - tap
                else:  # tap < ref_tap
                    target_tap = max(low, tap - 2)
                    if target_tap < tap:
                        variation = target_tap - tap
            elif is_red:
                # Rule: Decrease Impedance (Move towards reference)
                if tap > ref_tap:
                    target_tap = max(ref_tap, tap - 2)
                    if target_tap < tap:
                        variation = target_tap - tap
                elif tap < ref_tap:
                    target_tap = min(ref_tap, tap + 2)
                    if target_tap > tap:
                        variation = target_tap - tap

            if variation != 0:
                new_tap = tap + variation
                action_id = f"pst_tap_{pst_id}_{'inc' if variation > 0 else 'dec'}{abs(variation)}"
                action_dict = {"pst_tap": {pst_id: new_tap}}
                action_desc = {
                    "description": f"Variation de slot de {variation} pour le PST {pst_id} (tap: {tap} -> {new_tap})",
                    "description_unitaire": f"Variation de slot de {variation} pour le PST {pst_id}",
                    "content": action_dict,
                }

                # In Step 2, prioritized_actions expects Action objects
                action = self.action_space(action_dict)
                identified[action_id] = action

                # User-requested score: ratio of dispatch flow on the pst branch over the maximum overload dispatch flow
                dispatch_flow = _bounds.capacity_map.get(pst_id, 0.0)
                if max_overload_flow > 1e-6:
                    score = abs(dispatch_flow / max_overload_flow)
                else:
                    score = 0.5 * abs(variation)  # Fallback to original simple score

                scores_map[action_id] = score
                details_map[action_id] = {
                    "pst_id": pst_id,
                    "previous_tap": tap,
                    "target_tap": new_tap,
                    "affected_line": pst_id,
                    "variation": variation,
                    "is_blue": is_blue,
                    "is_red": is_red,
                    "max_reachable_tap": high,
                    "min_reachable_tap": low,
                    "dispatch_flow_on_pst": dispatch_flow,
                }

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
                        f"  {'Effective' if is_rho_reduction else 'Ineffective'} PST tap: {action_id}"
                    )

        # Store candidates by descending score: the orchestrator's min/fill
        # phases take the first N entries per family (add_prioritized_actions
        # walks insertion order), so leaving the raw network (IIDM) order here
        # let a low-score PST win the MIN_PST slot over a far better one.
        self.identified_pst_actions = dict(
            sorted(
                identified.items(), key=lambda x: scores_map.get(x[0], 0), reverse=True
            )
        )
        self.effective_pst_actions = effective
        self.ineffective_pst_actions = ineffective
        self.scores_pst_actions = scores_map
        self.params_pst_actions = details_map
