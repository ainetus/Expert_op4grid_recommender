# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Redispatching discovery mixin.

Mirrors the renewable-curtailment logic but targets *dispatchable*
generators (energy source not in ``RENEWABLE_ENERGY_SOURCES``) and applies a
signed active-power delta instead of forcing the setpoint to 0 MW:

* **raise** production on generators downstream (aval) of the constrained
  path or on the parallel red dispatch loops, and
* **lower** production on generators upstream (amont) of the constrained
  path.

With ``EVACUATION_ANTENNA_LEVERS``, the dispatchable generators of an
evacuation antenna join through its attachment node, lowered upstream and
raised downstream, plus one MW-sized change per antenna
(docs/recommender/evacuation_antennas.md).

The default delta (``REDISPATCH_DEFAULT_DELTA_MW``) is editable downstream
(Co-Study4Grid); the real target setpoint ``current ± delta`` is encoded in
the ``set_gen_p`` action so the variation is actually simulated.
"""
from typing import List

from expert_op4grid_recommender import config
from expert_op4grid_recommender.action_evaluation.discovery._injection_base import (
    InjectionDiscoveryBase,
)


class RedispatchMixin(InjectionDiscoveryBase):
    """Redispatching discovery mixin."""

    MARGIN_KEY = "REDISPATCH_MARGIN"
    MIN_MW_KEY = "REDISPATCH_MIN_MW"

    def find_relevant_redispatch(
        self,
        nodes_up_indices: List[int],
        nodes_down_indices: List[int],
        nodes_dispatch_loop_names: List[str] = [],
    ):
        """Discover redispatching candidates.

        Args:
            nodes_up_indices: substation indices where production should be
                *raised* (downstream / aval of the constrained path, plus the
                parallel red dispatch-loop nodes).
            nodes_down_indices: substation indices where production should be
                *lowered* (upstream / amont of the constrained path).
            nodes_dispatch_loop_names: names of the red dispatch-loop edges,
                used to account for parallel-path influence when raising.
        """
        # Shared overload preamble (InjectionDiscoveryBase): warm caches, read
        # the REDISPATCH margin / min-MW knobs, and compute max_overload_flow +
        # P_overload_excess. None => early return. ``delta`` (the redispatch
        # step) is family-specific and read here.
        _ctx = self._injection_overload_context()
        if _ctx is None:
            return
        obs = _ctx.obs

        # Hoist observation arrays to locals ONCE — ``obs.name_sub`` /
        # ``obs.name_gen`` rebuild a fresh numpy string array on every access
        # and ``obs.gen_p`` copies its array each time, so reading them per
        # candidate inside the loops below is O(candidates x n_elements).
        name_sub_arr = obs.name_sub
        name_gen_arr = obs.name_gen
        gen_p_array = getattr(obs, "gen_p", getattr(obs, "prod_p", None))

        margin = _ctx.margin
        min_mw = _ctx.min_mw
        delta = getattr(config, "REDISPATCH_DEFAULT_DELTA_MW", 10.0)
        max_overload_flow = _ctx.max_overload_flow
        P_overload_excess = _ctx.P_overload_excess

        blue_edge_names_set = self._get_blue_edge_names_set()
        dispatch_loop_set = (
            set(nodes_dispatch_loop_names) if nodes_dispatch_loop_names else None
        )
        # Single combined cache: blue (constrained) edges + red dispatch loops.
        node_flow_cache = self._build_node_flow_cache(
            blue_edge_names_set, dispatch_loop_set
        )

        subs_with_dispatchable = self._get_subs_with_dispatchable_gens()

        identified, effective, ineffective = {}, [], []
        scores_map, params_map = {}, {}

        # Same-site higher-voltage (400/225 kV) reference nodes, used to reach
        # generators sitting on a radial ("antenne") voltage level absent from
        # the influence graph. Only build it (and hit the network) when at
        # least one dispatchable generator sits on a node outside the graph —
        # avoids any added cost when every generator is on a meshed busbar.
        needs_higher_ref = any(
            sub_id not in node_flow_cache for sub_id in subs_with_dispatchable
        )
        higher_refs = self._get_site_higher_voltage_map() if needs_higher_ref else {}

        def _influence_of(node_flows, direction):
            if direction == "up":
                # Raising downstream / on parallel paths: any influence
                # component (constrained or dispatch loop) is relevant.
                return max(
                    node_flows["neg_in"], node_flows["neg_out"],
                    node_flows["pos_in"], node_flows["pos_out"],
                )
            # Lowering upstream: only the constrained (blue) flow.
            return max(node_flows["neg_in"], node_flows["neg_out"])

        def _process(nodes_indices, direction):
            nodes_set = set(nodes_indices)
            for sub_id, gen_ids in subs_with_dispatchable.items():
                sub_name = str(name_sub_arr[sub_id])

                # 1) Direct: the generator's own voltage level is a graph node
                #    on the correct side of the constraint.
                ref_idx = (
                    sub_id
                    if (sub_id in node_flow_cache and sub_id in nodes_set)
                    else None
                )
                via_higher = False

                # 1b) Evacuation antenna: the production reaches the constraint
                #     through the antenna's (best) attachment node on the
                #     constrained path — an upstream antenna when lowering, a
                #     downstream one when raising.
                evacuation_antenna = None
                if ref_idx is None:
                    ref_idx, evacuation_antenna = self._evacuation_antenna_reference(
                        sub_id, node_flow_cache, nodes_set,
                        lambda f: _influence_of(f, direction), direction,
                    )

                # 2) Antenna: the generator's voltage level is NOT in the
                #    influence graph — borrow the same site's higher-voltage
                #    (400/225 kV) busbar if it is in the graph on the right
                #    side, using it as the influence/score reference.
                if ref_idx is None:
                    best_idx, best_flow = None, 0.0
                    for cand in higher_refs.get(sub_id, ()):
                        if cand in node_flow_cache and cand in nodes_set:
                            cand_flow = _influence_of(node_flow_cache[cand], direction)
                            if cand_flow > best_flow:
                                best_flow, best_idx = cand_flow, cand
                    if best_idx is not None:
                        ref_idx, via_higher = best_idx, True

                if ref_idx is None:
                    continue

                node_flows = node_flow_cache[ref_idx]
                influence_flow = _influence_of(node_flows, direction)
                if influence_flow <= 0:
                    continue

                influence_factor = self._injection_influence_factor(
                    influence_flow, max_overload_flow
                )
                if influence_factor <= 0:
                    continue

                ref_name = str(name_sub_arr[ref_idx])

                mw_required = (
                    (P_overload_excess * (1 + margin)) / influence_factor
                    if influence_factor > 0
                    else P_overload_excess * (1 + margin)
                )

                for gen_id in gen_ids:
                    try:
                        gen_name = name_gen_arr[gen_id]
                    except (IndexError, KeyError):
                        continue

                    if gen_p_array is None or gen_id >= len(gen_p_array):
                        continue
                    gen_p = float(gen_p_array[gen_id])
                    if gen_p > 0:
                        continue
                    # Current production magnitude (positive MW).
                    prod = abs(gen_p)
                    if prod < min_mw:
                        continue

                    if direction == "up":
                        target_p = prod + delta
                        delta_signed = delta
                    else:
                        target_p = max(0.0, prod - delta)
                        delta_signed = -delta
                    # Actual change applied (bounded by reaching 0 on a lower).
                    applied_mw = round(abs(target_p - prod), 2)

                    coverage_ratio = (
                        min(1.0, prod / mw_required) if mw_required > 0 else 1.0
                    )
                    score = influence_factor * coverage_ratio

                    action_id = f"redispatch_{gen_name}"
                    # Encode the real target setpoint (production-positive units,
                    # pypowsybl target_p convention) so the variation simulates.
                    identified[action_id] = self.action_space(
                        {"set_gen_p": {gen_name: target_p}}
                    )
                    scores_map[action_id] = round(score, 2)
                    params_map[action_id] = {
                        "substation": sub_name,
                        "node_type": "aval" if direction == "up" else "amont",
                        "direction": direction,
                        "gen_name": gen_name,
                        "action_mode": "redispatch",
                        "target_p_MW": round(target_p, 2),
                        "delta_MW": round(delta_signed, 2),
                        "applied_MW": applied_mw,
                        "influence_factor": round(influence_factor, 2),
                        "mw_required": round(mw_required, 2),
                        "gen_p": round(prod, 2),
                        "coverage_ratio": round(coverage_ratio, 2),
                        # Reference node used to estimate influence: the gen's
                        # own VL, or a same-site higher-voltage busbar for
                        # antenna sites.
                        "influence_ref_substation": ref_name,
                        "via_higher_voltage": via_higher,
                    }
                    if evacuation_antenna is not None:
                        params_map[action_id]["via_evacuation_antenna"] = True
                        params_map[action_id]["evacuation_antenna_id"] = (
                            evacuation_antenna.antenna_id
                        )

        _process(nodes_up_indices, "up")
        _process(nodes_down_indices, "down")

        # Evacuation antennas: one MW-sized change per antenna, spread pro rata
        # over its dispatchable generators (unlike the fixed per-generator step
        # above, sized by what the overload needs) — a cut upstream, a rise
        # within the units' headroom downstream.
        for direction, nodes_indices in (("down", nodes_down_indices),
                                         ("up", nodes_up_indices)):
            sign = 1.0 if direction == "up" else -1.0
            for agg in self._evacuation_antenna_aggregates(
                subs_with_dispatchable, gen_p_array, name_gen_arr, _ctx, node_flow_cache,
                set(nodes_indices), lambda f, d=direction: _influence_of(f, d), direction,
            ):
                action_id = f"redispatch_{agg.antenna.antenna_id}"
                ref_name = str(name_sub_arr[agg.ref_sub_id])
                identified[action_id] = self.action_space({"set_gen_p": agg.target_p})
                scores_map[action_id] = round(agg.score, 2)
                params_map[action_id] = {
                    "substation": ref_name,
                    "node_type": "aval" if direction == "up" else "amont",
                    "direction": direction,
                    "gen_names": sorted(agg.gen_p),
                    "action_mode": "redispatch",
                    "target_p_MW": {n: round(p, 2) for n, p in sorted(agg.target_p.items())},
                    "delta_MW": round(sign * agg.applied_mw, 2),
                    "applied_MW": round(agg.applied_mw, 2),
                    "influence_factor": round(agg.influence_factor, 2),
                    "mw_required": round(agg.mw_required, 2),
                    "gen_p": round(sum(agg.gen_p.values()), 2),
                    "coverage_ratio": round(agg.coverage_ratio, 2),
                    "influence_ref_substation": ref_name,
                    "via_higher_voltage": False,
                    "via_evacuation_antenna": True,
                    "evacuation_antenna_id": agg.antenna.antenna_id,
                }
                if direction == "up":
                    params_map[action_id]["headroom_MW"] = round(agg.capacity_mw, 2)

        candidates_to_check = (
            self._cap_candidates_for_simulation(identified, scores_map)
            if self.check_action_simulation and identified
            else []
        )
        if candidates_to_check:
            try:
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
                print(f"Warning: Simulation check failed for redispatch: {e}")

        self.identified_redispatch = dict(
            sorted(
                identified.items(),
                key=lambda x: scores_map.get(x[0], 0),
                reverse=True,
            )
        )
        self.effective_redispatch = effective
        self.ineffective_redispatch = ineffective
        self.scores_redispatch = scores_map
        self.params_redispatch = params_map
