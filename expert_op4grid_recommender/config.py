#!/usr/bin/python3
# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of expert_op4grid_recommender, Expert system analyzer based on
# ExpertOp4Grid principles.
"""Pydantic-backed configuration module.

All runtime knobs used to live as bare module-level constants. They are now
validated by a :class:`Settings` (``pydantic_settings.BaseSettings``) class
and re-exported at module level for backwards compatibility, so existing
``from expert_op4grid_recommender.config import DATE`` and
``config.ENV_NAME = ...`` call sites keep working unchanged.

Every field on :class:`Settings` can be overridden at process startup via an
environment variable with the prefix ``EXPERT_OP4GRID_``, for example::

    EXPERT_OP4GRID_TIMESTEP=12 python -m expert_op4grid_recommender.main
    EXPERT_OP4GRID_LINES_DEFAUT='["BEON L31CPVAN"]' python ...

Lists and dictionaries accept JSON payloads from env vars;
``LINES_DEFAUT`` additionally accepts a bare line name.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, MutableMapping, Optional

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# --- Get Project Root Directory ---
CONFIG_DIR: Path = Path(__file__).parent.resolve()
PROJECT_ROOT: Path = CONFIG_DIR.parent.resolve()

_DEFAULT_PARAM_OPTIONS_EXPERT_OP: Dict[str, Any] = {
    # 0.05 is 5 percent of the max overload flow
    "ThresholdReportOfLine": 0.05,
    # 10 percent de la surcharge max
    "ThersholdMinPowerOfLoop": 0.1,
    # If at least a loop is detected, only keep the ones with a flow of at
    # least 25 percent of the biggest one.
    "ratioToKeepLoop": 0.25,
    # Ratio percentage for reconsidering the flow direction.
    "ratioToReconsiderFlowDirection": 0.75,
    # Max unused lines.
    "maxUnusedLines": 3,
    # Total number of simulated topologies at the final simulation step.
    "totalnumberofsimulatedtopos": 30,
    # Number of simulated topologies per node at the final simulation step.
    "numberofsimulatedtopospernode": 10,
}


class Settings(BaseSettings):
    """Validated configuration for :mod:`expert_op4grid_recommender`.

    Instances pick up values from (in order of precedence): explicit
    constructor arguments, environment variables prefixed with
    ``EXPERT_OP4GRID_``, values loaded from the file pointed at by
    ``EXPERT_OP4GRID_ENV_FILE`` (if set), and finally the defaults below.
    """

    model_config = SettingsConfigDict(
        env_prefix="EXPERT_OP4GRID_",
        env_file=None,
        case_sensitive=True,
        extra="ignore",
        validate_assignment=True,
    )

    # -------------------
    #  Case configuration
    # -------------------
    DATE: datetime = datetime(2024, 12, 7)
    TIMESTEP: int = Field(default=9, ge=0)
    LINES_DEFAUT: Annotated[List[str], NoDecode] = Field(
        default_factory=lambda: ["CHALOL61CPVAN"]
    )

    # -------------------
    #  Environment & paths
    # -------------------
    ENV_NAME: str = "bare_env_small_grid_test"
    FILE_ACTION_SPACE_DESC: str = "reduced_model_actions_test.json"
    LINES_MONITORING_FILE: Optional[str] = None

    # -------------------
    #  User parameters
    # -------------------
    USE_EVALUATION_CONFIG: bool = True
    USE_DC_LOAD_FLOW: bool = False
    # Run the overflow-graph flow-transfer in DC even when the rest of the
    # pipeline is AC. The overflow graph only needs the *delta flows* (a linear
    # flow-transfer estimate); AC is reserved for the per-action reassessment.
    # On a numerically stiff operating point one AC solve can cost ~10 s vs
    # ~0.2 s in DC. The N-1 state is solved in DC before and after the cut,
    # and that DC transfer is applied on the AC base flows of the observation
    # (comparing DC post-cut flows to AC base flows would put the AC/DC gap on
    # every branch). The graph's rho is then derived from the post-cut active
    # power (|P|/(√3·Vnom·Ilim)) instead of the (unpopulated) DC branch currents.
    # Opt-in (default off) to preserve the AC graph behaviour until validated
    # across all environments.
    USE_DC_FOR_OVERFLOW_GRAPH: bool = False
    DO_CONSOLIDATE_GRAPH: bool = False
    DO_RECO_MAINTENANCE: bool = False
    CHECK_WITH_ACTION_DESCRIPTION: bool = True
    DRAW_ONLY_SIGNIFICANT_EDGES: bool = True
    USE_GRID_LAYOUT: bool = False
    DO_FORCE_OVERLOAD_GRAPH_EVEN_IF_GRAPH_BROKEN_APART: bool = False
    # When the contingency islands a radial ("antenne") pocket of substations
    # — i.e. disconnecting even the single max overload breaks the grid apart —
    # build a synthetic downstream overflow graph of that pocket (each branch
    # carrying the lost pre-disconnection flow as a negative delta) and let the
    # recommender propose injection actions (load shedding / renewable
    # curtailment / redispatch) on it. Topological actions are filtered out for
    # this case. Set to False to restore the legacy "no solution" early return.
    ENABLE_ANTENNA_RECOMMENDATIONS: bool = True
    # Structural antennas (docs/recommender/evacuation_antennas.md): parts of
    # the grid tied to the constrained path through a single node (or several
    # when insensitive to the overload), detected on both sides of it once the
    # overflow graph is built (not in the islanded-pocket case) and typed on it
    # — "evacuation" / "evacuation_downstream" for an upstream / downstream
    # antenna that exports power. Typing and payload only: the levers are
    # EVACUATION_ANTENNA_LEVERS.
    ENABLE_STRUCTURAL_ANTENNAS: bool = True
    # Minimum |export| (MW) at the attachment node to type an antenna.
    STRUCTURAL_ANTENNA_MIN_MW: float = Field(default=1.0, ge=0.0)
    # A component holding more than this share of the graph nodes is not an
    # antenna (guard against a whole region hanging off a single node).
    STRUCTURAL_ANTENNA_MAX_FRACTION: float = Field(default=0.25, gt=0.0, le=1.0)
    # Evacuation antennas as levers: the renewable curtailment and redispatch
    # families reach the generators of an evacuation antenna through its
    # attachment node (one candidate per generator), and each family adds one
    # MW-sized candidate per antenna spread pro rata over its generators of
    # that production type. Upstream antennas are lowered (curtailment,
    # downward redispatch); downstream ones are only raised (upward redispatch
    # within the units' headroom), never curtailed. On by default since 0.3.4;
    # needs ENABLE_STRUCTURAL_ANTENNAS (no antenna, no lever).
    EVACUATION_ANTENNA_LEVERS: bool = True
    DO_SAVE_DATA_FOR_TEST: bool = False
    CHECK_ACTION_SIMULATION: bool = False
    N_PRIORITIZED_ACTIONS: int = Field(default=20, ge=0)
    IGNORE_RECONNECTIONS: bool = False
    IGNORE_LINES_MONITORING: bool = True
    DO_VISUALIZATION: bool = True
    # Output format for the overflow graph visualization. "pdf" saves the
    # static PDF (default); "html" saves the interactive HTML viewer
    # introduced by ExpertOp4Grid PR #74.
    VISUALIZATION_FORMAT: Literal["pdf", "html"] = "pdf"
    # Use human-readable voltage-level names (the ``name`` column of the
    # network's voltage levels) as the displayed node labels in the
    # overflow graph instead of the raw voltage-level IDs. Useful for
    # PyPSA-derived networks where the IDs look like ``VL_way_...`` while a
    # readable name (e.g. ``"Saucats 400kV"``) is available. The node
    # identity stays the VL ID (so pin overlays / SLD lookups keep working);
    # only the rendered text changes. When a VL has no name, or its name
    # equals its ID, the ID is kept as-is.
    USE_VOLTAGE_LEVEL_NAMES_IN_GRAPH: bool = True
    # Only possible for now with the pypowsybl backend.
    MAX_RHO_BOTH_EXTREMITIES: bool = True
    # Factor applied to permanent thermal limits when loading from
    # operational limits.
    MONITORING_FACTOR_THERMAL_LIMITS: float = Field(default=0.95, gt=0.0)
    # 2% — pre-existing overloads are excluded from the analysis unless the
    # current increased by more than this fraction.
    PRE_EXISTING_OVERLOAD_WORSENING_THRESHOLD: float = Field(default=0.02, ge=0.0)
    # Default fast mode: keeps transformer/shunt voltage control on but runs the
    # tap-changer regulation in AFTER_GENERATOR_VOLTAGE_CONTROL (~6-7x fewer
    # Newton iters than the incremental outer loop, same currents — see
    # NetworkManager.run_load_flow). Slow mode (False) keeps the incremental
    # loop as a max-fidelity fallback.
    PYPOWSYBL_FAST_MODE: bool = True

    # -------------------
    #  Minimum prioritized actions per type
    # -------------------
    MIN_LINE_RECONNECTIONS: int = Field(default=2, ge=0)
    MIN_CLOSE_COUPLING: int = Field(default=3, ge=0)
    MIN_OPEN_COUPLING: int = Field(default=2, ge=0)
    MIN_LINE_DISCONNECTIONS: int = Field(default=3, ge=0)
    MIN_PST: int = Field(default=2, ge=0)
    MIN_LOAD_SHEDDING: int = Field(default=2, ge=0)
    MIN_RENEWABLE_CURTAILMENT: int = Field(default=2, ge=0)
    MIN_REDISPATCH: int = Field(default=2, ge=0)

    # -------------------
    #  Allowed action types (recommender restriction)
    # -------------------
    # When non-empty, the recommender ONLY discovers/prioritizes the listed
    # action families (others are skipped entirely). Empty list = all families
    # (default behaviour). Tokens match the UI filter tokens:
    #   "reco", "close", "open", "disco", "pst", "ls", "rc", "redispatch".
    ALLOWED_ACTION_TYPES: List[str] = Field(default_factory=list)

    # -------------------
    #  Load shedding parameters
    # -------------------
    # 5% safety margin on top of the minimum shedding volume.
    LOAD_SHEDDING_MARGIN: float = Field(default=0.05, ge=0.0)
    # Ignore trivial shedding below this volume (MW).
    LOAD_SHEDDING_MIN_MW: float = Field(default=1.0, ge=0.0)

    # -------------------
    #  Renewable curtailment parameters
    # -------------------
    RENEWABLE_CURTAILMENT_MARGIN: float = Field(default=0.05, ge=0.0)
    RENEWABLE_CURTAILMENT_MIN_MW: float = Field(default=1.0, ge=0.0)
    RENEWABLE_ENERGY_SOURCES: List[str] = Field(
        default_factory=lambda: ["WIND", "SOLAR"]
    )

    # -------------------
    #  Redispatching parameters
    # -------------------
    # Default active-power delta (MW) applied when raising/lowering a
    # dispatchable generator. The operator can edit this volume downstream
    # (Co-Study4Grid). Raising acts on generators downstream of the
    # constrained path (or on parallel red dispatch loops); lowering acts on
    # generators upstream of the constrained path. Dispatchable generators are
    # those whose energy source is NOT in RENEWABLE_ENERGY_SOURCES.
    REDISPATCH_DEFAULT_DELTA_MW: float = Field(default=10.0, gt=0.0)
    REDISPATCH_MARGIN: float = Field(default=0.05, ge=0.0)
    REDISPATCH_MIN_MW: float = Field(default=1.0, ge=0.0)

    # -------------------
    #  Candidate-simulation cap
    # -------------------
    # The redispatching and renewable-curtailment discovery passes identify a
    # candidate per dispatchable / renewable generator reachable from the
    # constrained path (including antenna sites reached via a higher-voltage
    # busbar). On large grids this is hundreds of candidates, and each one was
    # validated with a full AC load flow whose effective/ineffective verdict
    # does NOT influence prioritization (the per-type ``scores`` already rank
    # them, and the final rho is recomputed in the reassessment phase) — pure
    # discovery-time overhead. Semantics:
    #   0 (default)  -> skip the per-candidate simulation entirely
    #   N > 0        -> simulate only the top-N candidates by score
    #   negative     -> simulate every candidate (legacy behaviour)
    MAX_CANDIDATE_SIMULATIONS: int = Field(default=0, ge=-1)

    # -------------------
    #  Reassessment parallelism
    # -------------------
    # The per-action reassessment can re-simulate the prioritized actions on
    # private pypowsybl network copies across worker threads. Each worker clones
    # a full network, so on a CPU-limited host (e.g. a 2-vCPU cloud container,
    # where ``os.cpu_count()`` still reports the *host* core count) the pool
    # over-subscribes the CPU and is SLOWER than a serial run. CPU detection is
    # container-aware (cgroup CPU quota + scheduler affinity).
    #   REASSESSMENT_PARALLEL:
    #     None  (default) -> auto: parallelise only when enough effective cores
    #                        are available (see REASSESSMENT_MIN_PARALLEL_CORES)
    #                        and every worker gets at least 6 actions — below
    #                        that the network copies cost more than they save
    #     True            -> force parallel (still bounded by cores / n_actions
    #                        / REASSESSMENT_MAX_WORKERS / available memory)
    #     False           -> force serial (recommended on 2-vCPU deployments;
    #                        set env EXPERT_OP4GRID_REASSESSMENT_PARALLEL=0)
    REASSESSMENT_PARALLEL: Optional[bool] = None
    # In auto mode, the minimum number of *effective* (container-aware) cores
    # required before the reassessment parallelises. Below it, serial.
    REASSESSMENT_MIN_PARALLEL_CORES: int = Field(default=4, ge=1)
    # Cap on the worker threads, i.e. on the private network copies held in
    # memory at once (1 = always serial). The available memory caps the pool
    # further: the copies may take half of it, at an estimated ~1.25 GB each
    # for full France (160x the network's 7.5 MB binary size, plus 64 MB).
    REASSESSMENT_MAX_WORKERS: int = Field(default=10, ge=1)

    # -------------------
    #  Expert system parameters
    # -------------------
    PARAM_OPTIONS_EXPERT_OP: Dict[str, Any] = Field(
        default_factory=lambda: dict(_DEFAULT_PARAM_OPTIONS_EXPERT_OP)
    )

    @field_validator("LINES_DEFAUT", mode="before")
    @classmethod
    def _normalize_lines_defaut(cls, value: Any) -> Any:
        """Accept both a bare line name and a JSON list from env vars.

        ``LINES_DEFAUT`` is annotated with :class:`NoDecode` so
        ``pydantic-settings`` hands the raw env-var string to this validator
        unchanged. We fall back to JSON parsing when the value looks like a
        JSON list, otherwise we treat it as a single line name. Python values
        (lists already, ``None``) pass through untouched.
        """
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return []
            if stripped.startswith("["):
                import json

                return json.loads(stripped)
            return [stripped]
        return value

    # -------------------
    #  Derived values (single source of truth)
    # -------------------
    # These used to be recomputed by hand at module level *after* the Settings
    # instance was built, which meant an override of e.g. ``ENV_NAME`` left the
    # stale ``ENV_PATH`` behind (review finding A3). Expressing them as
    # ``@computed_field`` properties on the authoritative Settings instance keeps
    # them in lock-step with the primary fields: ``model_dump()`` includes them,
    # so :func:`apply_settings_to_namespace` promotes them alongside the plain
    # fields, and :func:`override_settings` recomputes them on every override.

    @computed_field  # type: ignore[prop-decorator]
    @property
    def CASE_NAME(self) -> str:
        return "defaut_" + "_".join(map(str, self.LINES_DEFAUT)) + "_t" + str(self.TIMESTEP)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ENV_FOLDER(self) -> Path:
        return PROJECT_ROOT / "data"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ENV_PATH(self) -> Path:
        return self.ENV_FOLDER / self.ENV_NAME

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ACTION_SPACE_FOLDER(self) -> Path:
        return self.ENV_FOLDER / "action_space"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def ACTION_FILE_PATH(self) -> Path:
        return self.ACTION_SPACE_FOLDER / self.FILE_ACTION_SPACE_DESC

    @computed_field  # type: ignore[prop-decorator]
    @property
    def SAVE_FOLDER_VISUALIZATION(self) -> Path:
        return PROJECT_ROOT / "Overflow_Graph"


def apply_settings_to_namespace(
    settings: Settings, namespace: MutableMapping[str, Any]
) -> None:
    """Promote each Settings field (including derived ``@computed_field``\\ s) to a
    module-level attribute.

    This is the compatibility shim that lets pre-pydantic call sites such as
    ``from expert_op4grid_recommender.config import DATE`` or
    ``config.ENV_NAME = env_name`` continue to work unchanged. ``model_dump()``
    includes the computed derived paths (``ENV_PATH``, ``ACTION_FILE_PATH`` …),
    so they are promoted here too and never need a separate hand-maintained
    block.
    """
    for name, value in settings.model_dump().items():
        namespace[name] = value


# --- Instantiate the default Settings object and populate module globals ---
#: The process-wide authoritative configuration. Read it through
#: :func:`get_settings`; change it through :func:`override_settings` (validated,
#: staleness-free) rather than by mutating module attributes directly.
settings: Settings = Settings()
apply_settings_to_namespace(settings, globals())


def get_settings() -> Settings:
    """Return the current authoritative :class:`Settings` instance."""
    return settings


def override_settings(_new: Optional[Settings] = None, /, **overrides: Any) -> Settings:
    """Replace the process-wide :class:`Settings` and refresh module attributes.

    This is the sanctioned runtime-override path. It runs full pydantic
    validation, recomputes the derived paths (so overriding ``ENV_NAME`` keeps
    ``ENV_PATH`` in sync — no staleness, review finding A3), and re-promotes the
    fields so existing ``config.X`` reads observe the new values.

    Pass either a ready :class:`Settings` positionally, or field overrides as
    keyword arguments (merged onto the current settings). Unknown keys raise
    ``ValueError``. Returns the new active :class:`Settings`.
    """
    global settings
    if _new is not None:
        if overrides:
            raise TypeError(
                "override_settings: pass either a Settings instance or keyword "
                "overrides, not both"
            )
        new_settings = _new
    else:
        unknown = set(overrides) - set(Settings.model_fields)
        if unknown:
            raise ValueError(
                f"override_settings: unknown setting(s) {sorted(unknown)}"
            )
        merged = {name: getattr(settings, name) for name in Settings.model_fields}
        merged.update(overrides)
        new_settings = Settings(**merged)
    settings = new_settings
    apply_settings_to_namespace(settings, globals())
    return settings


def reset_settings() -> Settings:
    """Rebuild :class:`Settings` from defaults + environment (drops overrides)."""
    return override_settings(Settings())
