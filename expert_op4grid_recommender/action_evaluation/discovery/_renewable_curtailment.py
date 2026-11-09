# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Renewable curtailment discovery mixin."""
from typing import List

from expert_op4grid_recommender.action_evaluation.discovery._injection_base import (
    InjectionDiscoveryBase,
)


class RenewableCurtailmentMixin(InjectionDiscoveryBase):
    """Renewable curtailment discovery mixin."""

    MARGIN_KEY = "RENEWABLE_CURTAILMENT_MARGIN"
    MIN_MW_KEY = "RENEWABLE_CURTAILMENT_MIN_MW"

    def find_relevant_renewable_curtailment(
        self, nodes_indices: List[int], nodes_dispatch_loop_names: List[str] = []
    ):
        """
        Discovers renewable curtailment candidates on upstream (amont) nodes or loop nodes.
        Mirroring load shedding logic but for generators (WIND/SOLAR) on the opposite side of the flow.

        Optimized for large networks (>500 nodes, >1000 lines) with:
        - Pre-computed substation-to-renewable-generator mapping (avoids per-node get_obj_connect_to)
        - Cached single-pass edge attribute extraction
        - Cached blue edge names and node flow calculations
        - Pre-computed baseline simulation for batch action checking
        """
        # Shared overload preamble (InjectionDiscoveryBase): warm caches, read
        # the RENEWABLE_CURTAILMENT margin / min-MW knobs, and compute
        # max_overload_flow + P_overload_excess. None => early return.
        _ctx = self._injection_overload_context()
        if _ctx is None:
            return
        obs = _ctx.obs

        # Hoist observation arrays to locals ONCE. ``obs.name_sub`` /
        # ``obs.name_gen`` rebuild a fresh numpy string array from a Python
        # list on EVERY access (network_manager.name_* -> np.array(...)), and
        # ``obs.gen_p`` returns a full ``.copy()`` each time. Reading them per
        # candidate inside the loops below turned candidate identification into
        # an O(candidates x n_elements) array-rebuild storm — the dominant cost
        # of this pass on grids with many renewables. Read them once here.
        name_sub_arr = obs.name_sub
        name_gen_arr = obs.name_gen
        gen_p_array = getattr(obs, "gen_p", getattr(obs, "prod_p", None))

        margin = _ctx.margin
        min_mw = _ctx.min_mw
        max_overload_flow = _ctx.max_overload_flow
        P_overload_excess = _ctx.P_overload_excess

        # Use cached blue edge names (fixes double call to get_constrained_edges_nodes)
        blue_edge_names_set = self._get_blue_edge_names_set()

        # Pre-compute dispatch loop names as set for O(1) lookup
        dispatch_loop_set = (
            set(nodes_dispatch_loop_names) if nodes_dispatch_loop_names else None
        )

        # Use cached node flow computation (single pass over edges)
        node_flow_cache = self._build_node_flow_cache(blue_edge_names_set, dispatch_loop_set)

        # Pre-filter: only iterate substations that have renewable generators (huge win on 500+ node networks)
        subs_with_renewable = self._get_subs_with_renewable_gens()
        nodes_set = set(nodes_indices)
        # Same-site higher-voltage (400/225 kV) reference nodes, used to reach
        # renewable generators on a radial ("antenne") voltage level absent
        # from the influence graph. Rare for small renewables (often connected
        # directly on the meshed-network busbars), so only build it (and hit
        # the network) when a renewable generator actually sits off-graph.
        needs_higher_ref = any(
            sub_id not in node_flow_cache for sub_id in subs_with_renewable
        )
        higher_refs = self._get_site_higher_voltage_map() if needs_higher_ref else {}

        identified, effective, ineffective = {}, [], []
        scores_map, params_map = {}, {}

        for sub_id, renewable_gen_ids in subs_with_renewable.items():
            sub_name = str(name_sub_arr[sub_id])

            # 1) Direct: the generator's own voltage level is a graph node.
            ref_idx = (
                sub_id
                if (sub_id in node_flow_cache and sub_id in nodes_set)
                else None
            )
            via_higher = False
            # 2) Antenna: borrow the same site's higher-voltage busbar.
            if ref_idx is None:
                best_idx, best_flow = None, 0.0
                for cand in higher_refs.get(sub_id, ()):
                    if cand in node_flow_cache and cand in nodes_set:
                        cf = node_flow_cache[cand]
                        cand_flow = max(
                            cf["neg_in"], cf["neg_out"], cf["pos_in"], cf["pos_out"]
                        )
                        if cand_flow > best_flow:
                            best_flow, best_idx = cand_flow, cand
                if best_idx is not None:
                    ref_idx, via_higher = best_idx, True
            if ref_idx is None:
                continue

            ref_name = str(name_sub_arr[ref_idx])

            # Get pre-computed influence flows (O(1) lookup) on the reference node
            node_flows = node_flow_cache[ref_idx]
            total_neg_in = node_flows["neg_in"]
            total_neg_out = node_flows["neg_out"]
            total_pos_in = node_flows["pos_in"]
            total_pos_out = node_flows["pos_out"]

            influence_flow = max(
                total_neg_in, total_neg_out, total_pos_in, total_pos_out
            )
            if influence_flow <= 0:
                continue

            influence_factor = self._injection_influence_factor(
                influence_flow, max_overload_flow
            )
            if influence_factor <= 0:
                continue

            # Pre-compute common values for all generators at this node
            mw_required = (
                (P_overload_excess * (1 + margin)) / influence_factor
                if influence_factor > 0
                else P_overload_excess * (1 + margin)
            )

            for gen_id in renewable_gen_ids:
                try:
                    gen_name = name_gen_arr[gen_id]
                except (IndexError, KeyError):
                    continue

                if gen_p_array is None or gen_id >= len(gen_p_array):
                    continue

                gen_p = float(gen_p_array[gen_id])
                if gen_p > 0:
                    continue
                gen_p = abs(gen_p)
                if gen_p < min_mw:
                    continue

                coverage_ratio = (
                    min(1.0, gen_p / mw_required) if mw_required > 0 else 1.0
                )
                score = influence_factor * coverage_ratio

                action_id = f"curtail_{gen_name}"
                # Reduce active power to 0 MW instead of disconnecting
                identified[action_id] = self.action_space(
                    {"set_gen_p": {gen_name: 0.0}}
                )
                scores_map[action_id] = round(score, 2)
                params_map[action_id] = {
                    "substation": sub_name,
                    "node_type": "aval",
                    "gen_name": gen_name,
                    "action_mode": "power_reduction",
                    "target_p_MW": 0.0,
                    "reduction_MW": round(gen_p, 2),
                    "influence_factor": round(influence_factor, 2),
                    "mw_required": round(mw_required, 2),
                    "gen_p": round(gen_p, 2),
                    "coverage_ratio": round(coverage_ratio, 2),
                    "influence_ref_substation": ref_name,
                    "via_higher_voltage": via_higher,
                }

        candidates_to_check = (
            self._cap_candidates_for_simulation(identified, scores_map)
            if self.check_action_simulation and identified
            else []
        )
        if candidates_to_check:
            try:
                # Baseline shared across discovery passes (one LF per run).
                act_defaut, baseline_rho, branch_obs = self._get_simulation_baseline()
                if baseline_rho is not None:
                    for action_id, action in candidates_to_check:
                        is_reduced, _ = self._check_rho_with_baseline(
                            branch_obs,
                            self.timestep,
                            act_defaut,
                            action,
                            self.lines_overloaded_ids,
                            self.act_reco_maintenance,
                            baseline_rho,
                            self.lines_we_care_about,
                        )
                        if is_reduced:
                            effective.append(action_id)
                        else:
                            ineffective.append(action_id)
            except Exception as e:
                print(
                    f"Warning: Simulation check failed for renewable curtailment: {e}"
                )

        # Descending score, like load shedding / PST: the prioritization phases
        # take the first N entries in insertion order (generator index order
        # otherwise).
        self.identified_renewable_curtailment = dict(
            sorted(
                identified.items(), key=lambda x: scores_map.get(x[0], 0), reverse=True
            )
        )
        self.effective_renewable_curtailment = effective
        self.ineffective_renewable_curtailment = ineffective
        self.scores_renewable_curtailment = scores_map
        self.params_renewable_curtailment = params_map
