# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
"""Pluggable recommendation model interface.

Defines the contract every recommendation model must honour so it can be
plugged into the analysis pipeline interchangeably with the built-in
expert system.

Design principles
-----------------
1. **Strategy pattern** — same input / output contract for every model.
2. **Capability flags** — ``requires_overflow_graph`` lets the caller
   skip the expensive step-1 graph build when the model does not need it.
3. **Param introspection** — :meth:`params_spec` enumerates parameters
   the model actually consumes; clients (UI, REST API) hide everything
   else, so the operator never sets a knob the model will ignore.
4. **Homogeneous output** — ``{action_id -> action_object}``; reassessment
   into action cards lives downstream in
   :mod:`expert_op4grid_recommender.utils.reassessment` and works for any
   model that emits raw actions.
5. **Pass what was already computed** — step-1 (overload detection,
   pre-existing exclusion, island guard) is expensive; its outputs are
   propagated to the model via :class:`RecommenderInputs` instead of
   being recomputed downstream.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar, Dict, List, Literal, Optional


class DictCompatMixin:
    """Backward-compatible mapping view over a dataclass's attributes.

    The analysis pipeline historically threaded plain ``dict`` payloads
    (the ~41-key context dict, the result dict, the per-action card dict).
    The typed dataclasses that replace them (``AnalysisContext``,
    ``AnalysisResult``, :class:`SimulatedAction`) mix this in so that every
    existing dict-style consumer keeps working unchanged: ``obj["key"]``,
    ``obj.get(...)``, ``obj.pop(...)``, ``obj.update(...)``, ``key in obj``
    and iteration all operate on the instance attributes. ``obj.key`` and
    ``obj["key"]`` are therefore the *same* slot — the dataclass fields are
    the single source of truth and the mapping interface is a compatibility
    view over them. Extra keys added dynamically (as the pipeline does when
    it enriches the context between steps) simply become extra attributes.

    Implemented directly over ``__dict__`` (rather than deriving from
    :class:`collections.abc.MutableMapping`) so it does not inherit a
    dict-style ``__eq__`` that would deep-compare numpy-array fields and
    raise "ambiguous truth value"; the dataclasses opt out of a generated
    ``__eq__`` (``eq=False``) and keep object identity.
    """

    __slots__ = ()

    def __getitem__(self, key):
        try:
            return self.__dict__[key]
        except KeyError:
            raise KeyError(key) from None

    def __setitem__(self, key, value):
        setattr(self, key, value)

    def __delitem__(self, key):
        try:
            del self.__dict__[key]
        except KeyError:
            raise KeyError(key) from None

    def __contains__(self, key):
        return key in self.__dict__

    def __iter__(self):
        return iter(self.__dict__)

    def __len__(self):
        return len(self.__dict__)

    def get(self, key, default=None):
        return self.__dict__.get(key, default)

    def setdefault(self, key, default=None):
        if key not in self.__dict__:
            setattr(self, key, default)
        return self.__dict__[key]

    def pop(self, key, *default):
        if key in self.__dict__:
            value = self.__dict__[key]
            del self.__dict__[key]
            return value
        if default:
            return default[0]
        raise KeyError(key)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            setattr(self, key, value)

    def keys(self):
        return self.__dict__.keys()

    def items(self):
        return self.__dict__.items()

    def values(self):
        return self.__dict__.values()


@dataclass
class RecommenderInputs:
    """Every input a recommendation model may consume.

    Always populated by the pipeline:

    - ``obs``                       initial network observation (N state)
    - ``obs_defaut``                post-fault observation (N-K state)
    - ``lines_defaut``              names of the lines forming the
                                    N-K contingency (faulted lines)
    - ``lines_overloaded_names``    names of constrained lines
                                    (overloaded under the N-K state)
    - ``lines_overloaded_ids``      indices into ``obs_defaut.name_line``
                                    matching ``lines_overloaded_names``
                                    one-to-one
    - ``dict_action``               action dictionary
    - ``env``                       simulation environment
    - ``classifier``                :class:`ActionClassifier` instance
    - ``timestep``                  current timestep

    Network handles (each paired with an observation):

    - ``network``         pypowsybl :class:`pypowsybl.network.Network`
                          paired with ``obs`` (N state).
    - ``network_defaut``  pypowsybl :class:`Network` paired with
                          ``obs_defaut`` (post-fault N-K state). On the
                          pypowsybl backend this is the same underlying
                          :class:`Network` as ``network`` but with the
                          contingency variant active.

    Pre-computed N-K outcome — surfaced so models do not recompute it:

    - ``lines_overloaded_rho``      loading rate (rho) of each
                                    constrained line under N-K, in the
                                    same order as ``lines_overloaded_names``
                                    / ``lines_overloaded_ids``. Equivalent
                                    to ``obs_defaut.rho[lines_overloaded_ids]``
                                    but pre-extracted as a plain Python
                                    list of floats.
    - ``lines_overloaded_ids_kept`` subset of ``lines_overloaded_ids``
                                    kept after the island-prevention
                                    guard (some overloads are dropped
                                    when relieving them would disconnect
                                    substations).
    - ``pre_existing_rho``          ``{line_idx: rho_N}`` for lines that
                                    were already overloaded in the N
                                    state. Used by reassessment to
                                    exclude them from worst-case rho
                                    scoring; downstream models can read
                                    it directly instead of re-scanning
                                    ``obs.rho``.

    Optional — set only when the caller asked for the overflow graph
    (``compute_overflow_graph=True``) AND the chosen model declared
    ``requires_overflow_graph=True``:

    - ``overflow_graph``                  alphaDeesp overflow graph
    - ``distribution_graph``              structured overload distribution graph
    - ``overflow_sim``                    associated alphaDeesp simulator
    - ``hubs``                            hub substation names
    - ``node_name_mapping``               internal index → name mapping
    - ``non_connected_reconnectable_lines``
    - ``lines_non_reconnectable``
    - ``lines_we_care_about``
    - ``maintenance_to_reco_at_t``
    - ``act_reco_maintenance``
    - ``use_dc``
    - ``filtered_candidate_actions``      action IDs retained by the expert
                                          rule filter — exposed so downstream
                                          models can sample among them
    - ``structural_antennas``             antennas hanging off the constrained
                                          path (``StructuralAntenna`` list,
                                          e.g. evacuation antennas), also
                                          tagged on ``overflow_graph`` — see
                                          docs/recommender/evacuation_antennas.md
    """

    obs: Any
    obs_defaut: Any
    lines_defaut: List[str]
    lines_overloaded_names: List[str]
    lines_overloaded_ids: List[int]
    dict_action: dict
    env: Any
    classifier: Any

    # --- Optional from here on -----------------------------------------
    # Pypowsybl Network handles, paired with the corresponding observations.
    network: Any = None          # paired with ``obs``         (N state)
    network_defaut: Any = None   # paired with ``obs_defaut``  (N-K state)

    # Pre-computed N-K outcome surfaced from step-1.
    lines_overloaded_rho: Optional[List[float]] = None
    lines_overloaded_ids_kept: Optional[List[int]] = None
    pre_existing_rho: Optional[Dict[int, float]] = None

    timestep: int = 0

    overflow_graph: Any = None
    distribution_graph: Any = None
    overflow_sim: Any = None
    hubs: Optional[List[str]] = None
    node_name_mapping: Any = None
    non_connected_reconnectable_lines: Optional[List[str]] = None
    lines_non_reconnectable: Optional[List[str]] = None
    lines_we_care_about: Any = None
    maintenance_to_reco_at_t: Any = None
    act_reco_maintenance: Any = None
    use_dc: bool = False
    filtered_candidate_actions: Optional[List[str]] = None
    structural_antennas: Optional[List[Any]] = None

    is_pypowsybl: bool = True
    fast_mode: bool = False

    # Private escape hatch for the expert model — it needs many internal
    # helpers (rho-reduction check, baseline simulation, ...) that would
    # otherwise pollute this DTO. External models must not rely on this
    # field.
    _context: Optional[dict] = None


@dataclass(eq=False, repr=False)
class SimulatedAction(DictCompatMixin):
    """A single action enriched with its simulated state.

    Schema preserved verbatim from the historical reassessment output so
    every existing action-card consumer keeps working unchanged. Mixes in
    :class:`DictCompatMixin` so the reassessment stage can emit typed
    ``SimulatedAction`` instances while callers that still index the card
    (``card["rho_before"]``, ``card.get("observation")``) keep working.
    """

    action: Any
    description_unitaire: Optional[str]
    rho_before: Any
    rho_after: Any
    max_rho: float
    max_rho_line: str
    is_rho_reduction: bool
    observation: Any
    non_convergence: Optional[str] = None


@dataclass
class RecommenderOutput:
    """What every model returns.

    ``prioritized_actions`` is ``{action_id: action_object}`` — the raw
    actions selected by the model, NOT enriched. The reassessment step
    in :mod:`expert_op4grid_recommender.utils.reassessment` wraps each
    one with simulated rho / observation / etc.

    ``action_scores`` is free-form and may be empty. When present, it
    follows the legacy expert schema:
    ``{category: {scores: {...}, params: {...}, non_convergence: {...}}}``.
    """

    prioritized_actions: dict
    action_scores: dict = field(default_factory=dict)


@dataclass
class ParamSpec:
    """Declarative spec of a single config parameter consumed by a model.

    Clients (REST API, web UI) read this to decide which controls to
    show and which ones to hide for a given model.
    """

    name: str
    label: str
    kind: Literal["int", "float", "bool"]
    default: Any
    min: Optional[float] = None
    max: Optional[float] = None
    description: Optional[str] = None
    group: Optional[str] = None


class RecommenderModel(ABC):
    """Plug-in interface for recommendation models.

    Concrete subclasses MUST set ``name`` and ``label``, and implement
    :meth:`params_spec` and :meth:`recommend`.
    """

    name: ClassVar[str]
    label: ClassVar[str]
    requires_overflow_graph: ClassVar[bool] = False

    @classmethod
    @abstractmethod
    def params_spec(cls) -> List[ParamSpec]:
        """Parameters this model consumes."""

    @abstractmethod
    def recommend(self, inputs: RecommenderInputs, params: dict) -> RecommenderOutput:
        """Produce a set of candidate actions.

        Args:
            inputs: read-only DTO with everything the pipeline gathered.
                Two paired (observation, network) handles are available
                — ``(obs, network)`` for N and ``(obs_defaut, network_defaut)``
                for N-K — and step-1 outcomes are exposed directly via
                ``lines_overloaded_names`` / ``lines_overloaded_ids`` /
                ``lines_overloaded_rho`` / ``lines_overloaded_ids_kept`` /
                ``pre_existing_rho`` so the model does not recompute them.
            params: ``{ParamSpec.name -> value}`` from the operator.
                Models should look only at keys they declared.

        Returns:
            A :class:`RecommenderOutput` with at least
            ``prioritized_actions``.
        """
