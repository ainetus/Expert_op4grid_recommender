# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Shared base for the three injection discovery families (revision R5).

Load shedding, renewable curtailment and redispatch each opened their
``find_relevant_*`` method with the same ~15-line overload preamble (warm the
caches, read the family margin / minimum-MW knobs, compute the reference
``max_overload_flow`` and the ``P_overload_excess`` in MW) and the same
saturating influence-factor formula — duplicated three times and already
drifting. :class:`InjectionDiscoveryBase` factors out exactly those two shared,
byte-identical pieces.

What is DELIBERATELY left in each family, because the divergence is
load-bearing (documented per family):

* which node-flow cache is built — blue-only (load shedding) vs. blue + red
  dispatch loops (curtailment / redispatch). This is coupled to the number of
  flow components each family's ``_influence_of`` reads (load shedding's
  blue-only cache has structurally-zero positive flows, so its ``max(neg_in,
  neg_out)`` equals the 4-component max; redispatch reads 2 or 4 by direction).
* the candidate loop (node/load-centric vs. generator-centric),
* the per-candidate simulation-check mode (load shedding: inline, uncapped;
  the generator families: capped, post-loop),
* the family-specific ``params`` / ``details`` schema.

The two generator families (curtailment, redispatch) also share the
evacuation-antenna levers (``config.EVACUATION_ANTENNA_LEVERS``,
docs/recommender/evacuation_antennas.md): a generator inside an evacuation
antenna acts on the constraint at the antenna's attachment node — the best one
when it has several (:meth:`InjectionDiscoveryBase._evacuation_antenna_attachment`)
— and each antenna yields one MW-sized change over its generators
(:meth:`InjectionDiscoveryBase._evacuation_antenna_aggregates`). Production is
lowered in an upstream antenna and raised in a downstream one
(:data:`EVACUATION_ANTENNA_TYPE`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Tuple

import numpy as np

from expert_op4grid_recommender import config


@dataclass(frozen=True)
class InjectionOverloadContext:
    """Result of the shared injection preamble.

    ``P_overload_excess`` is the MW the worst overload exceeds its limit by,
    scaled by ``max_overload_flow`` (the reference flow scoring 1.0).
    """

    obs: Any
    margin: float
    min_mw: float
    max_overload_flow: float
    P_overload_excess: float


#: Evacuation-antenna type a production change acts through: lowering
#: production relieves the constraint from an upstream antenna, raising it from
#: a downstream one (curtailing a downstream antenna would aggravate it).
EVACUATION_ANTENNA_TYPE = {"down": "evacuation", "up": "evacuation_downstream"}


@dataclass(frozen=True)
class EvacuationAggregate:
    """MW-sized production change over the generators of one evacuation antenna.

    ``gen_p`` is the current production (MW, positive) of each generator and
    ``target_p`` its new setpoint, moved from the current setpoint ``S_g`` by a
    share of ``applied_mw`` proportional to its room: ``S_g`` itself when
    lowering (``S_g · (1 − ΔP / ΣS_g)``), its headroom ``Pmax_g − S_g`` when
    raising. ``capacity_mw`` is the total room. ``ref_sub_id`` is the
    attachment node the change is sized and scored on.
    """

    antenna: Any
    direction: str
    ref_sub_id: int
    influence_factor: float
    mw_required: float
    gen_p: Dict[str, float]
    target_p: Dict[str, float]
    applied_mw: float
    capacity_mw: float
    coverage_ratio: float

    @property
    def score(self) -> float:
        return self.influence_factor * self.coverage_ratio


class InjectionDiscoveryBase:
    """Mixin providing the shared overload preamble + influence factor.

    Plain mixin (like the family mixins): its methods call ``DiscovererBase``
    helpers through ``self`` on the composed :class:`ActionDiscoverer`. Each
    family sets :data:`MARGIN_KEY` / :data:`MIN_MW_KEY` to its config knobs.
    """

    #: config attribute names for the family's margin / minimum-MW knobs
    #: (overridden per family; the defaults mirror load shedding).
    MARGIN_KEY: str = "LOAD_SHEDDING_MARGIN"
    MIN_MW_KEY: str = "LOAD_SHEDDING_MIN_MW"

    def _injection_overload_context(self) -> Optional[InjectionOverloadContext]:
        """Warm caches, read the family knobs, compute the overload reference.

        Returns ``None`` to signal an early return (no line-capacity map, or no
        active overload) — the caller then returns without recording candidates,
        exactly as each family's inline preamble did.
        """
        self._build_lookup_caches()
        # Ensure the edge-data cache is populated (single pass over all edges).
        self._get_edge_data_cache()
        obs = self.obs_defaut

        margin = getattr(config, self.MARGIN_KEY, 0.05)
        min_mw = getattr(config, self.MIN_MW_KEY, 1.0)

        # Overload excess in MW (uses the cached per-line capacity map).
        name_to_capacity = self._build_line_capacity_map()
        if not name_to_capacity:
            return None

        name_line_arr = obs.name_line
        overloaded_line_names = {name_line_arr[i] for i in self.lines_overloaded_ids}
        overloaded_caps = [
            name_to_capacity[n] for n in overloaded_line_names if n in name_to_capacity
        ]
        max_overload_flow = (
            max(overloaded_caps) if overloaded_caps else max(name_to_capacity.values())
        )

        if len(self.lines_overloaded_ids) == 0:
            return None
        rho_max = float(np.max(obs.rho[self.lines_overloaded_ids]))
        if rho_max <= 1.0:
            return None
        P_overload_excess = (rho_max - 1.0) * max_overload_flow

        return InjectionOverloadContext(
            obs=obs,
            margin=margin,
            min_mw=min_mw,
            max_overload_flow=max_overload_flow,
            P_overload_excess=P_overload_excess,
        )

    @staticmethod
    def _injection_influence_factor(
        influence_flow: float, max_overload_flow: float
    ) -> float:
        """Saturating influence factor shared by all three injection families."""
        return (
            min(1.0, influence_flow / max_overload_flow)
            if max_overload_flow > 0
            else 0.0
        )

    @staticmethod
    def _evacuation_antenna_attachment(
        antenna: Any, node_flow_cache: Dict[int, Dict[str, float]],
        nodes_set: Iterable[int], influence_of: Callable[[Dict[str, float]], float],
    ) -> Optional[int]:
        """Attachment node an evacuation antenna acts through: among its
        attachment nodes that carry influence (in ``node_flow_cache`` and
        ``nodes_set``), the one with the strongest influence flow, so the
        antenna's options take the best score. Most antennas have a single
        attachment; an insensitive component tied to several nodes of one side
        of the path has more. ``None`` when none is usable."""
        best_idx, best_flow = None, 0.0
        for sub_id in antenna.attachment_sub_ids:
            if sub_id in node_flow_cache and sub_id in nodes_set:
                flow = influence_of(node_flow_cache[sub_id])
                if flow > best_flow:
                    best_idx, best_flow = sub_id, flow
        return best_idx

    def _evacuation_antenna_reference(
        self, sub_id: int, node_flow_cache: Dict[int, Dict[str, float]],
        nodes_set: Iterable[int], influence_of: Callable[[Dict[str, float]], float],
        direction: str = "down",
    ) -> Tuple[Optional[int], Any]:
        """Reference node of a generator sitting in an evacuation antenna.

        Its branches are gray, so the generator's own node never carries
        influence; every MW it produces reaches the constraint through the
        antenna's attachment node(s), on the constrained path: upstream for a
        ``"down"`` change, downstream for an ``"up"`` one
        (:data:`EVACUATION_ANTENNA_TYPE`). Returns ``(attachment_sub_id,
        antenna)`` with the best attachment
        (:meth:`_evacuation_antenna_attachment`), or ``(None, None)`` when the
        substation is in no evacuation antenna of that side (always the case
        with ``EVACUATION_ANTENNA_LEVERS`` off) or no attachment carries usable
        influence.
        """
        antenna = self._get_evacuation_antennas_by_sub(
            EVACUATION_ANTENNA_TYPE[direction]).get(sub_id)
        if antenna is None:
            return None, None
        ref_idx = self._evacuation_antenna_attachment(
            antenna, node_flow_cache, nodes_set, influence_of)
        if ref_idx is None:
            return None, None
        return ref_idx, antenna

    def _evacuation_antenna_aggregates(
        self,
        gens_by_sub: Dict[int, List[int]],
        gen_p_array: Any,
        name_gen_arr: Any,
        ctx: InjectionOverloadContext,
        node_flow_cache: Dict[int, Dict[str, float]],
        nodes_set: Iterable[int],
        influence_of: Callable[[Dict[str, float]], float],
        direction: str = "down",
    ) -> Iterator[EvacuationAggregate]:
        """Yield one MW-sized production change per evacuation antenna.

        The change spreads pro rata over the antenna's generators listed in
        ``gens_by_sub`` (one family's generators: renewable or dispatchable) and
        moves what the overload needs at the best attachment node,
        ``ΔP_req = P_exc · (1 + margin) / f_a``, capped by their room: their
        setpoint when lowering an upstream antenna, their headroom up to
        ``obs.gen_pmax`` when raising a downstream one (generators of unknown
        ``Pmax`` are left out). The new setpoints are moved from the current
        ones (``obs.gen_target_p``) rather than from the produced power, which
        also carries the generator's share of the load-flow slack: a small
        sized change could otherwise go the wrong way. ``influence_of`` reads
        the family's influence flow off a node-flow entry. Antennas whose
        attachment is not a usable reference, or whose room is below the family
        minimum, yield nothing.
        """
        gen_pmax = getattr(ctx.obs, "gen_pmax", None) if direction == "up" else None
        if direction == "up" and gen_pmax is None:
            return
        setpoints = getattr(ctx.obs, "gen_target_p", None)
        seen = set()
        for antenna in self._get_evacuation_antennas_by_sub(
                EVACUATION_ANTENNA_TYPE[direction]).values():
            if antenna.antenna_id in seen:
                continue
            seen.add(antenna.antenna_id)
            ref_idx = self._evacuation_antenna_attachment(
                antenna, node_flow_cache, nodes_set, influence_of)
            if ref_idx is None:
                continue
            influence_factor = self._injection_influence_factor(
                influence_of(node_flow_cache[ref_idx]), ctx.max_overload_flow
            )
            if influence_factor <= 0:
                continue

            gen_p: Dict[str, float] = {}
            setpoint: Dict[str, float] = {}
            room: Dict[str, float] = {}
            for sub_id in antenna.sub_ids:
                for gen_id in gens_by_sub.get(sub_id, ()):
                    if gen_p_array is None or gen_id >= len(gen_p_array):
                        continue
                    p = float(gen_p_array[gen_id])
                    if p > 0:  # producing generators are negative (receptor convention)
                        continue
                    s = (float(setpoints[gen_id])
                         if setpoints is not None and gen_id < len(setpoints) else np.nan)
                    if not np.isfinite(s):
                        s = abs(p)
                    if direction == "up":
                        pmax = float(gen_pmax[gen_id]) if gen_id < len(gen_pmax) else np.nan
                        gen_room = pmax - s if np.isfinite(pmax) else 0.0
                    else:
                        gen_room = s
                    if gen_room <= 0:
                        continue
                    name = str(name_gen_arr[gen_id])
                    gen_p[name] = abs(p)
                    setpoint[name] = s
                    room[name] = gen_room
            capacity = sum(room.values())
            if capacity < ctx.min_mw:
                continue

            mw_required = ctx.P_overload_excess * (1 + ctx.margin) / influence_factor
            applied = min(capacity, mw_required)
            sign = 1.0 if direction == "up" else -1.0
            yield EvacuationAggregate(
                antenna=antenna,
                direction=direction,
                ref_sub_id=ref_idx,
                influence_factor=influence_factor,
                mw_required=mw_required,
                gen_p=gen_p,
                target_p={name: setpoint[name] + sign * room[name] * applied / capacity
                          for name in gen_p},
                applied_mw=applied,
                capacity_mw=capacity,
                coverage_ratio=min(1.0, capacity / mw_required) if mw_required > 0 else 1.0,
            )
