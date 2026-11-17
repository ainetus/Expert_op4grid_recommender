# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Structural contract the family mixins depend on (review A5).

The family mixins (``LineDisconnectionMixin``, ``PSTMixin``, the injection
mixins, …) are plain classes composed onto :class:`DiscovererBase` by
``ActionDiscoverer``; each reaches into ``self`` for shared state (the
observations, the ``FamilyResult`` store, the graph handles) and shared helpers
(the cache accessors, the memoised baselines) **without any of that being
declared anywhere** — a mixin's real dependencies were only discoverable by
reading its body. :class:`DiscovererProtocol` writes that contract down in one
place: it is exactly the surface a family mixin may assume ``self`` provides.

It is intentionally documentation-first (a mixin need not be annotated
``self: DiscovererProtocol`` to benefit), and ``@runtime_checkable`` so a test
can assert :class:`DiscovererBase` / ``ActionDiscoverer`` structurally satisfy
it — turning "the base grew a method a mixin needs but nobody noticed" into a
failing test rather than a runtime ``AttributeError`` deep in discovery.
"""
from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable

from expert_op4grid_recommender.action_evaluation.discovery._results import (
    DisconnectionBounds,
    FamilyResult,
)


@runtime_checkable
class DiscovererProtocol(Protocol):
    """The shared ``self`` surface every discovery family mixin depends on."""

    # --- State populated by DiscovererBase.__init__ ---
    #: N-state and N-K (contingency) observations.
    obs: Any
    obs_defaut: Any
    #: per-family typed discovery outcome (see _results.FamilyResult).
    results: Dict[str, FamilyResult]
    action_space: Any
    lines_defaut: List[str]
    lines_overloaded_ids: List[int]
    dict_action: Dict[str, Any]
    hubs: List[str]
    g_overflow: Any
    g_distribution_graph: Any
    classifier: Any
    check_action_simulation: bool
    timestep: int
    #: antennas hanging off the constrained path (StructuralAntenna list).
    structural_antennas: List[Any]

    # --- Shared cache accessors / helpers on DiscovererBase ---
    def _build_lookup_caches(self) -> None: ...
    def _get_edge_data_cache(self) -> Any: ...
    def _build_line_capacity_map(self) -> Dict[str, float]: ...
    def _get_blue_edge_names_set(self) -> set: ...
    def _build_node_flow_cache(self, blue_edge_names_set: set,
                               dispatch_loop_set: Any = None) -> Dict[int, Dict[str, float]]: ...
    def _get_disconnection_bounds(self) -> DisconnectionBounds: ...
    def _get_simulation_baseline(self) -> Any: ...
    def _get_subs_with_loads(self) -> Dict[int, List[int]]: ...
    def _get_subs_with_renewable_gens(self) -> Dict[int, List[int]]: ...
    def _get_subs_with_dispatchable_gens(self) -> Dict[int, List[int]]: ...
    def _get_site_higher_voltage_map(self) -> Dict[int, Any]: ...
    def _get_evacuation_antennas_by_sub(self,
                                        antenna_type: str = "evacuation") -> Dict[int, Any]: ...
    def _cap_candidates_for_simulation(self, identified: Dict[str, Any],
                                       scores_map: Dict[str, float]) -> list: ...


#: The methods above — used by a structural conformance test to guard that the
#: base keeps providing the surface the mixins assume.
DISCOVERER_REQUIRED_METHODS = (
    "_build_lookup_caches",
    "_get_edge_data_cache",
    "_build_line_capacity_map",
    "_get_blue_edge_names_set",
    "_build_node_flow_cache",
    "_get_disconnection_bounds",
    "_get_simulation_baseline",
    "_get_subs_with_loads",
    "_get_subs_with_renewable_gens",
    "_get_subs_with_dispatchable_gens",
    "_get_site_higher_voltage_map",
    "_get_evacuation_antennas_by_sub",
    "_cap_candidates_for_simulation",
)
