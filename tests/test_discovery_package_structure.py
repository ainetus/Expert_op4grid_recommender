# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Tests for the post-P1 :mod:`action_evaluation.discovery` package layout.

These tests lock in the refactor invariants:

* ``ActionDiscoverer`` composes every family mixin on top of
  :class:`DiscovererBase` and exposes the complete public API that the
  orchestration code depends on.
* The MRO places orchestrator/family mixins before the base so helper
  overrides from a family mixin would win if they were added later.
* Every family mixin defines **exactly** the methods belonging to its
  family — guarding against accidental drift in future edits.
* No method name is defined in more than one module (the split was
  intended to be disjoint).
* Cross-family helpers (``_compute_disconnection_flow_bounds``,
  ``_build_line_capacity_map``, ``_asymmetric_bell_score`` …) are still
  reachable through ``ActionDiscoverer``'s method resolution chain, so
  :class:`PSTMixin` / :class:`LineDisconnectionMixin` / the shedding
  mixins can continue to call them via ``self``.
* The pure scoring helpers on :class:`DiscovererBase` behave as
  documented (peak normalization, boundary values, tails).
* Small instance helpers (``_is_sublist``,
  ``_build_path_consecutive_pairs``) still work on a bare instance.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from expert_op4grid_recommender.action_evaluation.discovery import ActionDiscoverer
from expert_op4grid_recommender.action_evaluation.discovery._base import (
    DiscovererBase,
)
from expert_op4grid_recommender.action_evaluation.discovery._line_disconnection import (
    LineDisconnectionMixin,
)
from expert_op4grid_recommender.action_evaluation.discovery._line_reconnection import (
    LineReconnectionMixin,
)
from expert_op4grid_recommender.action_evaluation.discovery._load_shedding import (
    LoadSheddingMixin,
)
from expert_op4grid_recommender.action_evaluation.discovery._node_merging import (
    NodeMergingMixin,
)
from expert_op4grid_recommender.action_evaluation.discovery._node_splitting import (
    NodeSplittingMixin,
)
from expert_op4grid_recommender.action_evaluation.discovery._orchestrator import (
    OrchestratorMixin,
)
from expert_op4grid_recommender.action_evaluation.discovery._pst import PSTMixin
from expert_op4grid_recommender.action_evaluation.discovery._injection_base import (
    InjectionDiscoveryBase,
)
from expert_op4grid_recommender.action_evaluation.discovery._renewable_curtailment import (
    RenewableCurtailmentMixin,
)
from expert_op4grid_recommender.action_evaluation.discovery._redispatch import (
    RedispatchMixin,
)


# ---------------------------------------------------------------------------
# Expected layout — single source of truth for the refactor.
# ---------------------------------------------------------------------------

MIXIN_EXPECTED_METHODS: dict[type, set[str]] = {
    LineReconnectionMixin: {"verify_relevant_reconnections"},
    LineDisconnectionMixin: {
        "compute_disconnection_score",
        "find_relevant_disconnections",
    },
    NodeMergingMixin: {
        "compute_node_merging_score",
        "find_relevant_node_merging",
    },
    PSTMixin: {"find_relevant_pst_actions"},
    LoadSheddingMixin: {"find_relevant_load_shedding"},
    RenewableCurtailmentMixin: {"find_relevant_renewable_curtailment"},
    RedispatchMixin: {"find_relevant_redispatch"},
    InjectionDiscoveryBase: {
        "_injection_overload_context",
        "_injection_influence_factor",
        "_evacuation_antenna_attachment",
        "_evacuation_antenna_reference",
        "_evacuation_antenna_aggregates",
    },
    OrchestratorMixin: {"discover_and_prioritize"},
    NodeSplittingMixin: {
        "identify_bus_of_interest_in_node_splitting_",
        "computing_buses_values_of_interest",
        "identify_node_splitting_type",
        "compute_node_splitting_action_bus_score",
        "compute_node_splitting_action_score_value",
        "compute_node_splitting_action_score",
        "identify_and_score_node_splitting_actions",
        "find_relevant_node_splitting",
    },
}


EXPECTED_PUBLIC_METHODS: set[str] = set().union(*MIXIN_EXPECTED_METHODS.values())


BASE_SHARED_HELPERS: set[str] = {
    "__init__",
    "_build_lookup_caches",
    "_get_blue_edge_names_set",
    "_get_subs_with_loads",
    "_get_subs_with_renewable_gens",
    "_get_subs_with_dispatchable_gens",
    "_get_voltage_level_metadata",
    "_get_site_higher_voltage_map",
    "_get_evacuation_antennas_by_sub",
    "_build_node_flow_cache",
    "_build_active_edges_cache",
    "_get_active_edges_between_cached",
    "_is_sublist",
    "_get_line_substations",
    "_find_paths_for_line",
    "_get_active_edges_between",
    "_has_blocking_disconnected_line",
    "_build_path_consecutive_pairs",
    "_check_other_reconnectable_line_on_path",
    "_asymmetric_bell_score",
    "_unconstrained_linear_score",
    "_build_line_capacity_map",
    "_get_edge_data_cache",
    "_compute_disconnection_flow_bounds",
    "_get_assets_on_bus_for_sub",
    "_get_subs_impacted_from_action_desc",
    "_get_action_topo_vect",
    "_edge_names_buses_dict",
    "_edge_names_buses_dict_new",
}


def _class_defined_methods(cls: type) -> set[str]:
    """All non-dunder *methods* defined locally on ``cls`` (excluding inherited).

    Counts only callables (functions / static- / classmethods). This excludes
    the per-family ``FamilyResult`` ``@property`` bridges installed on
    :class:`DiscovererBase` (``identified_reconnections``, ``scores_splits_dict``,
    …) and the ``MARGIN_KEY`` / ``MIN_MW_KEY`` class constants on the injection
    mixins — data, not methods — which would otherwise inflate the invariants
    below.
    """
    import inspect

    return {
        name
        for name, value in cls.__dict__.items()
        if not name.startswith("__")
        and (
            inspect.isfunction(value)
            or isinstance(value, (staticmethod, classmethod))
        )
    }


# ---------------------------------------------------------------------------
# Package composition
# ---------------------------------------------------------------------------

def test_action_discoverer_composes_every_mixin():
    """``ActionDiscoverer`` must inherit from every family mixin and the base."""
    bases = set(ActionDiscoverer.__mro__)
    for mixin in list(MIXIN_EXPECTED_METHODS.keys()) + [DiscovererBase]:
        assert mixin in bases, f"{mixin.__name__} missing from ActionDiscoverer MRO"


def test_mro_places_orchestrator_and_mixins_before_base():
    """Family mixins and the orchestrator must all precede :class:`DiscovererBase`
    in the MRO so that cooperative overrides would be possible; the base (which
    owns ``__init__`` and all shared caches) must sit at the end of the chain.
    """
    mro = list(ActionDiscoverer.__mro__)
    base_index = mro.index(DiscovererBase)
    for mixin in MIXIN_EXPECTED_METHODS:
        assert mro.index(mixin) < base_index, (
            f"{mixin.__name__} must precede DiscovererBase in MRO"
        )


def test_action_discoverer_exposes_every_expected_public_method():
    missing = EXPECTED_PUBLIC_METHODS - set(dir(ActionDiscoverer))
    assert not missing, f"ActionDiscoverer is missing methods: {sorted(missing)}"


def test_init_resolves_from_discoverer_base():
    """``__init__`` is only defined on :class:`DiscovererBase`, so its MRO
    resolution must land on the base class."""
    # All family mixins must not shadow ``__init__``
    for mixin in MIXIN_EXPECTED_METHODS:
        assert "__init__" not in mixin.__dict__, (
            f"{mixin.__name__} unexpectedly defines __init__"
        )
    assert "__init__" in DiscovererBase.__dict__


# ---------------------------------------------------------------------------
# Mixin scope — each family owns exactly its methods, no overlap.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "mixin, expected_methods",
    list(MIXIN_EXPECTED_METHODS.items()),
    ids=[m.__name__ for m in MIXIN_EXPECTED_METHODS],
)
def test_mixin_defines_exactly_its_family_methods(mixin, expected_methods):
    """Guard against drift: each mixin must define **exactly** the methods
    belonging to its action family (no more, no less)."""
    defined = _class_defined_methods(mixin)
    assert defined == expected_methods, (
        f"{mixin.__name__} defines {defined}, expected {expected_methods}"
    )


def test_action_discoverer_satisfies_discoverer_protocol_methods():
    """A5: the shared surface the family mixins depend on is now declared in
    ``DiscovererProtocol``. Guard that the base keeps providing every required
    helper — so "the base lost a method a mixin needs" fails here, not with an
    AttributeError deep inside discovery."""
    from expert_op4grid_recommender.action_evaluation.discovery._protocols import (
        DISCOVERER_REQUIRED_METHODS,
    )

    missing = [m for m in DISCOVERER_REQUIRED_METHODS if not callable(getattr(ActionDiscoverer, m, None))]
    assert not missing, f"ActionDiscoverer no longer provides: {missing}"


def test_discoverer_base_owns_init_and_shared_helpers():
    """The helpers that multiple families call (caches, scoring primitives,
    generic lookups) must remain on :class:`DiscovererBase`."""
    defined = _class_defined_methods(DiscovererBase) | {"__init__"}
    missing = BASE_SHARED_HELPERS - defined
    assert not missing, f"Missing helpers on DiscovererBase: {sorted(missing)}"


def test_no_method_is_defined_in_two_places():
    """The split must be disjoint: no callable name can appear in the
    ``__dict__`` of more than one family mixin / base class."""
    all_classes = [DiscovererBase, *MIXIN_EXPECTED_METHODS.keys()]
    seen: dict[str, type] = {}
    for cls in all_classes:
        for name in _class_defined_methods(cls):
            assert name not in seen, (
                f"Method {name!r} defined in both {seen[name].__name__} "
                f"and {cls.__name__}"
            )
            seen[name] = cls


def test_method_count_matches_original_class():
    """Sanity check: the new package distributes exactly 57 methods
    across the base + mixins. The base contributes ``__init__`` plus 33
    helpers (the original 24 plus ``_get_subs_with_dispatchable_gens``,
    ``_get_voltage_level_metadata`` and ``_get_site_higher_voltage_map``
    added for redispatching, ``_cap_candidates_for_simulation`` that
    bounds the per-candidate simulation of the generator-targeting passes,
    ``_get_simulation_baseline`` that shares the candidate-check baseline LF
    across discovery passes as a :class:`BaselineContext`,
    ``_shared_baseline_check`` that routes the topological passes through that
    shared baseline on pypowsybl — replacing the old ``main.py`` monkey-patch —,
    ``_release_simulation_baseline`` that frees the shared baseline's retained
    variant at the end of the run (R4), and ``_get_disconnection_bounds`` that
    memoises the disconnection/PST flow bounds once per run, replacing the
    order-sensitive ``_disco_bounds`` del/lazy protocol (R5), and
    ``_get_evacuation_antennas_by_sub`` that indexes the evacuation antennas
    used as levers); the nine mixins together contribute the remaining 18
    family methods (1 + 2 + 8 + 2 + 1 + 1 + 1 + 1 + 1), and
    InjectionDiscoveryBase adds 5 shared injection helpers (2 for the overload
    preamble, 3 for the evacuation-antenna levers). The FamilyResult @property
    bridges and the injection
    MARGIN_KEY / MIN_MW_KEY class constants are excluded by
    ``_class_defined_methods`` (they are data, not methods)."""
    non_dunder = sum(
        len(_class_defined_methods(cls))
        for cls in [DiscovererBase, *MIXIN_EXPECTED_METHODS.keys()]
    )
    # _class_defined_methods filters out dunder names, so __init__ is
    # excluded from the per-class counts. Add it back explicitly to get
    # the full 57 (42 original + redispatch mixin + 3 redispatch helpers +
    # the candidate-simulation cap helper + the shared baseline helper +
    # the shared-baseline topological-check helper + _get_disconnection_bounds
    # + InjectionDiscoveryBase's 2 shared injection helpers (R5) + the baseline
    # release helper (R4) + the 4 evacuation-antenna lever helpers).
    assert "__init__" in DiscovererBase.__dict__
    assert non_dunder + 1 == 57


# ---------------------------------------------------------------------------
# Cross-mixin helper accessibility (composition via MRO)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "helper_name",
    [
        "_compute_disconnection_flow_bounds",  # used by PST & line disco
        "_build_line_capacity_map",            # used by PST, load shed, curtail
        "_asymmetric_bell_score",              # used by line disconnection
        "_unconstrained_linear_score",         # used by line disconnection
        "_get_assets_on_bus_for_sub",          # used by node split & merge
        "_get_subs_impacted_from_action_desc", # used by node splitting
        "_get_blue_edge_names_set",            # used by load shed / curtail
        "_build_node_flow_cache",              # used by load shed / curtail
        "_get_subs_with_loads",                # used by load shedding
        "_get_subs_with_renewable_gens",       # used by renewable curtailment
    ],
)
def test_cross_family_helpers_reachable_on_composed_class(helper_name):
    assert hasattr(ActionDiscoverer, helper_name), (
        f"{helper_name} unreachable from ActionDiscoverer — the mixin that "
        f"calls it via self.{helper_name} would fail at runtime."
    )


# ---------------------------------------------------------------------------
# Scoring helpers — pure @staticmethod on DiscovererBase.
# ---------------------------------------------------------------------------

class TestAsymmetricBellScore:
    """Behavioral tests for :meth:`DiscovererBase._asymmetric_bell_score`."""

    def test_returns_zero_when_bounds_collapse(self):
        assert DiscovererBase._asymmetric_bell_score(5.0, 10.0, 10.0) == 0.0
        assert DiscovererBase._asymmetric_bell_score(5.0, 10.0, 5.0) == 0.0

    def test_returns_zero_at_min_and_max_boundaries(self):
        # Beta(alpha=3, beta=1.5) kernel evaluates to 0 at both endpoints.
        assert DiscovererBase._asymmetric_bell_score(0.0, 0.0, 10.0) == 0.0
        assert DiscovererBase._asymmetric_bell_score(10.0, 0.0, 10.0) == 0.0

    def test_peak_normalized_to_one_near_max(self):
        # For alpha=3, beta=1.5 the peak lies at x = (alpha-1)/(alpha+beta-2)
        # = 2/2.5 = 0.8, so on [0,10] the score peaks at 8.
        peak = DiscovererBase._asymmetric_bell_score(8.0, 0.0, 10.0)
        assert peak == pytest.approx(1.0, rel=1e-6)

    def test_scores_interior_points_strictly_below_peak(self):
        for observed in (1.0, 4.0, 9.5):
            score = DiscovererBase._asymmetric_bell_score(observed, 0.0, 10.0)
            assert 0.0 <= score < 1.0

    def test_quadratic_tails_go_negative_outside_range(self):
        below = DiscovererBase._asymmetric_bell_score(-1.0, 0.0, 10.0)
        above = DiscovererBase._asymmetric_bell_score(15.0, 0.0, 10.0)
        assert below < 0.0
        assert above < 0.0

    def test_tails_grow_quadratically_with_distance(self):
        """The tail penalty scales with the squared normalized distance from
        the interval boundary."""
        s1 = DiscovererBase._asymmetric_bell_score(-1.0, 0.0, 10.0)
        s2 = DiscovererBase._asymmetric_bell_score(-2.0, 0.0, 10.0)
        # x² grows 4× when |x| doubles
        assert s2 == pytest.approx(4 * s1, rel=1e-6)


class TestUnconstrainedLinearScore:
    """Behavioral tests for :meth:`DiscovererBase._unconstrained_linear_score`."""

    def test_zero_when_bounds_collapse(self):
        assert DiscovererBase._unconstrained_linear_score(5.0, 10.0, 10.0) == 0.0

    def test_linear_ramp_between_bounds(self):
        assert DiscovererBase._unconstrained_linear_score(0.0, 0.0, 10.0) == 0.0
        assert DiscovererBase._unconstrained_linear_score(5.0, 0.0, 10.0) == pytest.approx(0.5)
        assert DiscovererBase._unconstrained_linear_score(10.0, 0.0, 10.0) == 1.0

    def test_capped_at_one_above_max(self):
        assert DiscovererBase._unconstrained_linear_score(50.0, 0.0, 10.0) == 1.0

    def test_negative_quadratic_tail_below_min(self):
        s = DiscovererBase._unconstrained_linear_score(-5.0, 0.0, 10.0)
        assert s < 0.0

    def test_tail_matches_bell_shape_below_min(self):
        """Below ``min_flow`` both scoring regimes share the same quadratic
        penalty (``-tail_scale * x**2``)."""
        bell = DiscovererBase._asymmetric_bell_score(-3.0, 0.0, 10.0)
        linear = DiscovererBase._unconstrained_linear_score(-3.0, 0.0, 10.0)
        assert bell == pytest.approx(linear, rel=1e-6)


# ---------------------------------------------------------------------------
# Small instance-method helpers: patch __init__ and call them directly.
# ---------------------------------------------------------------------------

@pytest.fixture
def bare_discoverer():
    """A bare ``ActionDiscoverer`` with ``__init__`` patched to a no-op,
    suitable for exercising pure helpers that do not touch ``self``-state."""
    with patch.object(ActionDiscoverer, "__init__", return_value=None):
        yield ActionDiscoverer()


class TestIsSublist:
    def test_contiguous_match_true(self, bare_discoverer):
        assert bare_discoverer._is_sublist(["B", "C"], ["A", "B", "C", "D"]) is True

    def test_single_element_match_true(self, bare_discoverer):
        assert bare_discoverer._is_sublist(["A"], ["A", "B", "C"]) is True
        assert bare_discoverer._is_sublist(["C"], ["A", "B", "C"]) is True

    def test_non_contiguous_returns_false(self, bare_discoverer):
        assert bare_discoverer._is_sublist(["A", "C"], ["A", "B", "C"]) is False

    def test_reversed_pair_returns_false(self, bare_discoverer):
        assert bare_discoverer._is_sublist(["C", "A"], ["A", "B", "C", "D"]) is False

    def test_absent_element_returns_false(self, bare_discoverer):
        assert bare_discoverer._is_sublist(["X"], ["A", "B"]) is False


class TestBuildPathConsecutivePairs:
    def test_builds_adjacent_pairs_per_path(self, bare_discoverer):
        pairs = bare_discoverer._build_path_consecutive_pairs(
            [["A", "B", "C"], ["X", "Y"]]
        )
        assert pairs == [{("A", "B"), ("B", "C")}, {("X", "Y")}]

    def test_empty_path_produces_empty_set(self, bare_discoverer):
        assert bare_discoverer._build_path_consecutive_pairs([[]]) == [set()]

    def test_single_node_path_produces_empty_set(self, bare_discoverer):
        assert bare_discoverer._build_path_consecutive_pairs([["A"]]) == [set()]

    def test_triangle_with_repeated_edges_dedups_in_set(self, bare_discoverer):
        # The function uses a set, so duplicate consecutive pairs collapse.
        pairs = bare_discoverer._build_path_consecutive_pairs([["A", "B", "A", "B"]])
        assert pairs == [{("A", "B"), ("B", "A")}]
