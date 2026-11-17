# expert_op4grid_recommender/pypowsybl_backend/network_manager.py
"""
Network Manager for pypowsybl-based simulation.

Handles loading networks, managing variants, and running load flows.
"""

import pypowsybl as pp
import pypowsybl.loadflow as lf
import numpy as np
import pandas as pd
from collections import OrderedDict
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any, Union


class NetworkManager:
    """
    Manages a pypowsybl network with variant support for efficient what-if analysis.
    
    This class wraps pypowsybl.network.Network and provides:
    - Network loading from various formats (XIIDM, CGMES, etc.)
    - Variant management for hypothetical scenarios
    - Load flow execution with configurable parameters
    - Element naming and topology information extraction
    
    Attributes:
        network (pp.network.Network): The underlying pypowsybl network.
        lf_parameters (pp.loadflow.Parameters): Load flow parameters.
        base_variant_id (str): The ID of the base/reference variant.
    """
    
    BASE_VARIANT = "base"

    #: Upper bound on retained ("kept") variants before the LRU backstop starts
    #: evicting the oldest. Set well above what any single analysis needs
    #: (~1 baseline + 1 N-1 + N prioritized-action variants), so it only bounds
    #: a long-running service that reuses one NetworkManager across analyses
    #: (review C4) and never evicts within a single run.
    DEFAULT_MAX_KEPT_VARIANTS = 256

    def __init__(self,
                 network_path: Optional[Union[str, Path]] = None,
                 network: Optional[pp.network.Network] = None,
                 lf_parameters: Optional[lf.Parameters] = None,
                 max_kept_variants: Optional[int] = None):
        """
        Initialize the NetworkManager.

        Args:
            network_path: Path to network file (XIIDM, CGMES, etc.)
            network: Pre-loaded pypowsybl network (alternative to path)
            lf_parameters: Load flow parameters. If None, uses RTE defaults.
            max_kept_variants: LRU backstop size for retained variants
                (default :attr:`DEFAULT_MAX_KEPT_VARIANTS`).
        """
        if network is not None:
            self.network = network
        elif network_path is not None:
            self.network = pp.network.load(str(network_path))
        else:
            raise ValueError("Either network_path or network must be provided")

        self.lf_parameters = lf_parameters or self._create_default_lf_parameters()
        self.base_variant_id = self.BASE_VARIANT

        # DC mode flag (can be set externally)
        self._default_dc = False

        # Registry of retained ("kept") variants in creation order, so a
        # long-running service that never releases them (review C4) is bounded:
        # once it exceeds ``_max_kept_variants`` the oldest non-active variant is
        # evicted. Each simulate(keep_variant=True) registers here.
        self._kept_variants: "OrderedDict[str, None]" = OrderedDict()
        self._max_kept_variants = (
            self.DEFAULT_MAX_KEPT_VARIANTS if max_kept_variants is None
            else max_kept_variants
        )

        # Ensure we have a working variant
        self._setup_base_variant()
        
        # Cache element information
        self._cache_element_info()
    
    def _create_default_lf_parameters(self) -> lf.Parameters:
        """Create default load flow parameters (RTE-style configuration)."""
        return lf.Parameters(
            read_slack_bus=False,
            write_slack_bus=False,
            voltage_init_mode=lf.VoltageInitMode.PREVIOUS_VALUES,
            transformer_voltage_control_on=True,
            use_reactive_limits=True,
            shunt_compensator_voltage_control_on=True,
            phase_shifter_regulation_on=True,
            distributed_slack=True,
            dc_use_transformer_ratio=False,
            twt_split_shunt_admittance=True,
            provider_parameters={
                "useActiveLimits": "true",
                "svcVoltageMonitoring": "false",
                "voltageRemoteControl": "false",
                "writeReferenceTerminals": "false",
                "slackBusSelectionMode": "MOST_MESHED",
                # Bumped from default 20 to 100 so that AC LFs with active
                # transformer / shunt voltage control on a perturbed topology
                # (e.g. node-merging that closes a coupler with non-trivial
                # angle gap between the two pre-merge buses) have room for the
                # IncrementalTransformerVoltageControl outer loop to converge
                # under DC_VALUES init. Empirically slow + DC_VALUES converges
                # in 8 outer iterations on PYMONP3 / P.SAOL31RONCI; default 20
                # was still hitting MAX_ITERATION_REACHED on the tap-changer
                # outer loop.
                "maxOuterLoopIterations": "100",
            }
        )
    
    def _setup_base_variant(self):
        """Setup the base variant for the network."""
        existing_variants = self.network.get_variant_ids()
        if self.BASE_VARIANT not in existing_variants:
            # Clone from initial variant
            self.network.clone_variant(existing_variants[0], self.BASE_VARIANT)
        self.network.set_working_variant(self.BASE_VARIANT)
    
    def _cache_element_info(self):
        """Cache element information for fast access."""
        # Lines (AC lines + transformers) - cache the DataFrames for reuse
        self._cached_lines_df = self.network.get_lines()
        self._cached_trafos_df = self.network.get_2_windings_transformers()

        self._line_ids = list(self._cached_lines_df.index) + list(self._cached_trafos_df.index)
        self._n_line = len(self._line_ids)

        # Store line/trafo sets for O(1) membership tests
        self._lines_set = set(self._cached_lines_df.index)
        self._trafos_set = set(self._cached_trafos_df.index)

        # Pre-compute line_id to index mapping for fast batch operations
        self._line_id_to_arr_idx = {lid: i for i, lid in enumerate(self._line_ids)}

        # Substations (voltage levels in pypowsybl terminology)
        vl_df = self.network.get_voltage_levels()
        self._substation_ids = list(vl_df.index)
        self._n_sub = len(self._substation_ids)

        # OPTIMIZATION: Pre-compute name -> index mappings for O(1) lookups
        self._sub_name_to_idx = {name: idx for idx, name in enumerate(self._substation_ids)}
        self._line_name_to_idx = {name: idx for idx, name in enumerate(self._line_ids)}

        # Map lines to substations using cached DataFrames
        self._line_or_sub = {}
        self._line_ex_sub = {}

        # Extract as dicts for fast access
        lines_vl1 = self._cached_lines_df['voltage_level1_id'].to_dict()
        lines_vl2 = self._cached_lines_df['voltage_level2_id'].to_dict()
        trafos_vl1 = self._cached_trafos_df['voltage_level1_id'].to_dict()
        trafos_vl2 = self._cached_trafos_df['voltage_level2_id'].to_dict()

        for line_id in self._cached_lines_df.index:
            self._line_or_sub[line_id] = lines_vl1[line_id]
            self._line_ex_sub[line_id] = lines_vl2[line_id]

        for trafo_id in self._cached_trafos_df.index:
            self._line_or_sub[trafo_id] = trafos_vl1[trafo_id]
            self._line_ex_sub[trafo_id] = trafos_vl2[trafo_id]

        # OPTIMIZATION: Pre-compute line_or_subid and line_ex_subid arrays
        self._cached_line_or_subid = np.array([
            self._sub_name_to_idx.get(self._line_or_sub[lid], -1)
            for lid in self._line_ids
        ])
        self._cached_line_ex_subid = np.array([
            self._sub_name_to_idx.get(self._line_ex_sub[lid], -1)
            for lid in self._line_ids
        ])

        # Generators - cache DataFrame for reuse
        self._cached_gen_df = self.network.get_generators()
        self._gen_ids = list(self._cached_gen_df.index)
        self._n_gen = len(self._gen_ids)
        self._gen_name_to_idx = {name: idx for idx, name in enumerate(self._gen_ids)}

        # Cache generator -> substation mapping, power values, and energy source
        self._gen_to_sub = {}
        self._gen_p_values = np.zeros(self._n_gen)
        self._gen_energy_source = np.array(["OTHER"] * self._n_gen, dtype=object)
        if len(self._cached_gen_df) > 0:
            if 'voltage_level_id' in self._cached_gen_df.columns:
                self._gen_to_sub = self._cached_gen_df['voltage_level_id'].to_dict()
            if 'p' in self._cached_gen_df.columns:
                p_arr = self._cached_gen_df['p'].values
                self._gen_p_values = np.where(np.isnan(p_arr), 0.0, p_arr)
            if 'energy_source' in self._cached_gen_df.columns:
                self._gen_energy_source = self._cached_gen_df['energy_source'].fillna('OTHER').values.astype(object)

        # Loads - cache DataFrame for reuse
        self._cached_load_df = self.network.get_loads()
        self._load_ids = list(self._cached_load_df.index)
        self._n_load = len(self._load_ids)
        self._load_name_to_idx = {name: idx for idx, name in enumerate(self._load_ids)}

        # Cache load -> substation mapping and power values
        self._load_to_sub = {}
        self._load_p_values = np.zeros(self._n_load)
        if len(self._cached_load_df) > 0:
            if 'voltage_level_id' in self._cached_load_df.columns:
                self._load_to_sub = self._cached_load_df['voltage_level_id'].to_dict()
            if 'p' in self._cached_load_df.columns:
                p_arr = self._cached_load_df['p'].values
                self._load_p_values = np.where(np.isnan(p_arr), 0.0, p_arr)

        # OPTIMIZATION: Pre-compute elements per substation
        self._cache_elements_per_substation()

        # OPTIMIZATION: Pre-compute thermal limits for all lines
        self._cache_thermal_limits()

        # PSTs - cache phase tap changers
        self._cache_pst_info()

        # P4: materialize the name arrays ONCE. The backing id lists
        # (_line_ids / _substation_ids / _gen_ids / _load_ids) are structural
        # (element identity + order) and never change across variants or during
        # this NetworkManager's lifetime — a network reload builds a fresh
        # manager — so the arrays are safe to cache. Without this, the name_*
        # properties rebuilt a fresh <U numpy array from a Python list on EVERY
        # access, which turned innocent `self.name_line[i]` loops into O(n^2).
        self._cache_name_arrays()

    def _cache_name_arrays(self):
        """Build the immutable name arrays once (P4 — kills the per-access rebuild).

        Marked read-only so an accidental in-place mutation by a consumer raises
        loudly instead of silently corrupting the shared cache. Every current
        caller only reads / reindexes / ``set()`` / ``list()`` / masks them.
        """
        self._cached_name_line = np.array(self._line_ids)
        self._cached_name_sub = np.array(self._substation_ids)
        self._cached_name_gen = np.array(self._gen_ids)
        self._cached_name_load = np.array(self._load_ids)
        for arr in (
            self._cached_name_line,
            self._cached_name_sub,
            self._cached_name_gen,
            self._cached_name_load,
        ):
            arr.flags.writeable = False

    def _cache_pst_info(self):
        """Identify transformers with phase tap changers."""
        self._pst_df = self.network.get_phase_tap_changers()
        self._pst_ids = list(self._pst_df.index)
        self._pst_set = set(self._pst_ids)

    def _cache_elements_per_substation(self):
        """Cache which elements belong to each substation."""
        n_sub = self._n_sub

        # Initialize per-substation element lists
        self._loads_per_sub = [[] for _ in range(n_sub)]
        self._gens_per_sub = [[] for _ in range(n_sub)]
        self._lines_or_per_sub = [[] for _ in range(n_sub)]
        self._lines_ex_per_sub = [[] for _ in range(n_sub)]

        # Map loads to substations
        for i, load_id in enumerate(self._load_ids):
            sub_name = self._load_to_sub.get(load_id, '')
            sub_idx = self._sub_name_to_idx.get(sub_name, -1)
            if sub_idx >= 0:
                self._loads_per_sub[sub_idx].append(i)

        # Map generators to substations
        for i, gen_id in enumerate(self._gen_ids):
            sub_name = self._gen_to_sub.get(gen_id, '')
            sub_idx = self._sub_name_to_idx.get(sub_name, -1)
            if sub_idx >= 0:
                self._gens_per_sub[sub_idx].append(i)

        # Map line origins to substations
        for i, line_id in enumerate(self._line_ids):
            sub_idx = self._cached_line_or_subid[i]
            if sub_idx >= 0:
                self._lines_or_per_sub[sub_idx].append(i)

        # Map line extremities to substations
        for i, line_id in enumerate(self._line_ids):
            sub_idx = self._cached_line_ex_subid[i]
            if sub_idx >= 0:
                self._lines_ex_per_sub[sub_idx].append(i)

        # Pre-compute sub_info (element count per substation)
        self._cached_sub_info = np.array([
            len(self._loads_per_sub[i]) + len(self._gens_per_sub[i]) +
            len(self._lines_or_per_sub[i]) + len(self._lines_ex_per_sub[i])
            for i in range(n_sub)
        ], dtype=int)

    def _cache_thermal_limits(self):
        """Pre-compute thermal limits for all lines in array format."""
        limits_df = self.network.get_operational_limits()
        # Reset index to make 'name' and 'element_id' columns
        limits_df = limits_df.reset_index()
        # Keep only permanent limits
        perm_limits = limits_df[limits_df['name'] == 'permanent_limit']
        
        # Mapping element_id -> limit value
        limit_map = perm_limits.set_index('element_id')['value'].to_dict()
        
        self._cached_thermal_limit_or = np.zeros(self._n_line)
        self._cached_thermal_limit_ex = np.zeros(self._n_line)
        
        default_limit = 9999.0
        for i, line_id in enumerate(self._line_ids):
            limit = limit_map.get(line_id, default_limit)
            self._cached_thermal_limit_or[i] = limit
            self._cached_thermal_limit_ex[i] = limit

    def get_thermal_limits_arrays(self) -> Tuple[np.ndarray, np.ndarray]:
        """Get pre-computed thermal limits for all lines."""
        return self._cached_thermal_limit_or.copy(), self._cached_thermal_limit_ex.copy()
    
    @property
    def name_line(self) -> np.ndarray:
        """Array of line names (compatible with grid2op interface). Cached read-only (P4)."""
        return self._cached_name_line

    @property
    def name_sub(self) -> np.ndarray:
        """Array of substation names (voltage level IDs). Cached read-only (P4)."""
        return self._cached_name_sub

    @property
    def name_gen(self) -> np.ndarray:
        """Array of generator names. Cached read-only (P4)."""
        return self._cached_name_gen

    @property
    def gen_energy_source(self) -> np.ndarray:
        """Array of generator energy source strings (e.g. 'WIND', 'SOLAR', 'THERMAL', ...)."""
        return self._gen_energy_source.copy()

    @property
    def name_load(self) -> np.ndarray:
        """Array of load names. Cached read-only (P4)."""
        return self._cached_name_load
    
    @property
    def n_line(self) -> int:
        """Number of lines (AC lines + transformers)."""
        return self._n_line
    
    @property
    def n_sub(self) -> int:
        """Number of substations."""
        return self._n_sub
    
    def get_line_or_subid(self) -> np.ndarray:
        """Get origin substation index for each line (cached)."""
        return self._cached_line_or_subid.copy()

    def get_line_ex_subid(self) -> np.ndarray:
        """Get extremity substation index for each line (cached)."""
        return self._cached_line_ex_subid.copy()

    def get_sub_idx(self, sub_name: str) -> int:
        """Get substation index by name (O(1) lookup)."""
        return self._sub_name_to_idx.get(sub_name, -1)

    def get_line_idx(self, line_name: str) -> int:
        """Get line index by name (O(1) lookup)."""
        return self._line_name_to_idx.get(line_name, -1)
    
    def create_variant(self, variant_id: str, from_variant: Optional[str] = None) -> str:
        """
        Create a new network variant for what-if analysis.
        
        Args:
            variant_id: ID for the new variant
            from_variant: Source variant to clone from (default: base)
            
        Returns:
            The variant_id
        """
        source = from_variant or self.base_variant_id
        
        # Remove existing variant if it exists
        if variant_id in self.network.get_variant_ids():
            self.network.remove_variant(variant_id)
        
        self.network.clone_variant(source, variant_id)
        return variant_id
    
    def set_working_variant(self, variant_id: str):
        """Set the active working variant."""
        self.network.set_working_variant(variant_id)
    
    def remove_variant(self, variant_id: str):
        """Remove a variant (cannot remove base variant)."""
        if variant_id != self.base_variant_id:
            if variant_id in self.network.get_variant_ids():
                self.network.remove_variant(variant_id)
            self._kept_variants.pop(variant_id, None)

    def register_kept_variant(self, variant_id: str) -> None:
        """Register a retained variant and enforce the LRU backstop.

        Called by ``PypowsyblObservation.simulate(keep_variant=True)`` for every
        variant it keeps alive. Retained variants are the operator-relevant
        states (the N-1 contingency, prioritized-action states, the shared
        discovery baseline) and are normally released explicitly. This registry
        is the backstop for a long-running service that reuses one
        ``NetworkManager`` across many analyses without releasing them (review
        C4): once more than ``_max_kept_variants`` accumulate, the oldest one
        that is neither the base nor the current working variant is evicted.
        """
        # Refresh recency (move to newest) and record.
        self._kept_variants.pop(variant_id, None)
        self._kept_variants[variant_id] = None

        if self._max_kept_variants is None or self._max_kept_variants <= 0:
            return

        try:
            current = self.network.get_working_variant_id()
        except Exception:
            current = None

        while len(self._kept_variants) > self._max_kept_variants:
            # Evict the oldest variant that is safe to drop.
            evicted = None
            for candidate in list(self._kept_variants.keys()):
                if candidate != self.base_variant_id and candidate != current:
                    evicted = candidate
                    break
            if evicted is None:
                break  # nothing safe to evict
            print(
                f"Warning: kept-variant registry exceeded {self._max_kept_variants}; "
                f"evicting oldest retained variant {evicted!r} (backstop)."
            )
            self.remove_variant(evicted)

    def sweep_kept_variants(self) -> None:
        """Remove every retained variant except the base (explicit full reset)."""
        for variant_id in list(self._kept_variants.keys()):
            self.remove_variant(variant_id)
    
    def _run_ac_with_init_fallback(self, params: lf.Parameters):
        """Helper to run AC load flow, retrying with DC_VALUES on failure.

        Two failure modes trigger the DC_VALUES retry (both observed on
        PyPSA-EUR / France grid after a node-merging action):
        1. ``PowsyblException`` raised synchronously (e.g. "Voltage
           magnitude is undefined for bus ...").
        2. A returned ``ComponentResult`` whose ``status`` is not
           ``CONVERGED`` — covers ``FAILED`` ("Unrealistic state"
           reached by OpenLoadFlow's voltage-control consistency check),
           ``MAX_ITERATION_REACHED``, ``SOLVER_FAILED``, etc. On a perturbed
           topology, NR seeded with stale PREVIOUS_VALUES (the pre-action
           voltages of two coupler buses 8° apart) diverges in ~13 iters
           with "Unrealistic state" — only a DC_VALUES seed recovers.
        """
        is_previous = (params.voltage_init_mode ==
                       lf.VoltageInitMode.PREVIOUS_VALUES)
        try:
            results = lf.run_ac(self.network, parameters=params)
        except Exception as e:
            if is_previous:
                fallback_params = lf.Parameters.from_json(params.to_json())
                fallback_params.voltage_init_mode = lf.VoltageInitMode.DC_VALUES
                print(f"Warning: Load flow with PREVIOUS_VALUES failed ({e}). "
                      "Retrying with DC_VALUES...")
                return lf.run_ac(self.network, parameters=fallback_params)
            raise

        # Synchronous call succeeded but the LF didn't converge — retry
        # with DC_VALUES if we used PREVIOUS_VALUES on this attempt.
        if is_previous and results:
            first = results[0]
            if first is not None and first.status != lf.ComponentStatus.CONVERGED:
                fallback_params = lf.Parameters.from_json(params.to_json())
                fallback_params.voltage_init_mode = lf.VoltageInitMode.DC_VALUES
                print(f"Warning: Load flow with PREVIOUS_VALUES did not converge "
                      f"(status={first.status}). Retrying with DC_VALUES...")
                return lf.run_ac(self.network, parameters=fallback_params)
        return results

    def run_load_flow(self, dc: Optional[bool] = None, fast: bool = False,
                      voltage_init_mode: Optional['lf.VoltageInitMode'] = None):
        """
        Run load flow on the current working variant.

        Args:
            dc: If True, run DC load flow instead of AC.
                If None, uses the _default_dc attribute.
            fast: If True, keep transformer/shunt voltage control on but run the
                tap-changer regulation in AFTER_GENERATOR_VOLTAGE_CONTROL mode
                (~6-7x fewer Newton iterations than the default incremental
                outer loop, same currents). See the fast branch below.
            voltage_init_mode: If provided, override the default
                `voltage_init_mode` in the LF parameters. Most callers should
                leave this None and let the default `PREVIOUS_VALUES` apply —
                but the INITIAL LF (at network load time, before any variant
                has a valid state to read from) MUST use `DC_VALUES` since no
                previous voltage magnitudes exist. Without this, pypowsybl
                tries `PREVIOUS_VALUES`, throws, then retries with
                `DC_VALUES` internally — wasted work + spurious warning in
                the logs. See `SimulationEnvironment._ensure_valid_state`.

        Returns:
            Load flow result object
        """
        # Use default if not specified
        use_dc = dc if dc is not None else self._default_dc

        # Prepare parameters
        params = self.lf_parameters
        needs_copy = fast and not use_dc
        if voltage_init_mode is not None and not use_dc:
            needs_copy = True
        if needs_copy:
            # Create a shallow copy via JSON to avoid modifying the main lf_parameters
            params = lf.Parameters.from_json(self.lf_parameters.to_json())
            if fast:
                # "Fast" mode keeps transformer + shunt voltage control ON — so
                # tap positions still shape the reactive flows and branch
                # currents we score — but runs the tap-changer regulation with
                # AFTER_GENERATOR_VOLTAGE_CONTROL instead of the provider's
                # default incremental outer loop. On the full France grid the
                # incremental loop needs ~57 Newton iterations per LF (~4.6 s);
                # after-generator converges in ~20 (~0.7 s) for the same
                # constrained-line current (<0.5 % delta) — a ~6-7x per-LF
                # speedup with no accuracy loss on the overloads. Slow mode
                # (fast=False) keeps the incremental loop as the max-fidelity
                # fallback. (Previously fast mode disabled tap/shunt control
                # entirely, which was faster still but changed the currents.)
                provider = dict(self.lf_parameters.provider_parameters or {})
                provider["transformerVoltageControlMode"] = (
                    "AFTER_GENERATOR_VOLTAGE_CONTROL"
                )
                params.provider_parameters = provider
            if voltage_init_mode is not None:
                params.voltage_init_mode = voltage_init_mode
        
        try:
            if use_dc:
                # DC load flow - run_dc uses its own default parameters
                results = lf.run_dc(self.network)
                return results[0] if results else None
            else:
                try:
                    results = self._run_ac_with_init_fallback(params)
                    result_obj = results[0] if results else None
                except Exception as e:
                    result_obj = None
                    fast_exception = str(e)
                else:
                    fast_exception = None
                    
                # If we didn't converge, retry in slow mode
                #if fast and (result_obj is None or result_obj.status != lf.ComponentStatus.CONVERGED):
                if (result_obj is None or result_obj.status != lf.ComponentStatus.CONVERGED):
                    #reason = fast_exception if result_obj is None else f"status: {result_obj.status}"
                    #print(f"Warning: Load flow failed or diverged ({reason}). Retrying in slow mode...")
                    try:
                        results = self._run_ac_with_init_fallback(self.lf_parameters)
                        result_obj = results[0] if results else None
                    except Exception as e2:
                        print(f"Load flow failed in slow mode: {e2}")
                        return None
                        
                return result_obj
                
        except Exception as e:
            print(f"Load flow failed: {e}")
            return None
    
    def disconnect_line(self, line_id: str):
        """Disconnect a line (open both terminals)."""
        # P3: O(1) membership on the cached sets instead of fetching the full
        # lines / 2wt tables on every call. The element index is structural and
        # never changes across variants, so the sets stay valid.
        if line_id in self._lines_set:
            self.network.update_lines(id=line_id, connected1=False, connected2=False)
        elif line_id in self._trafos_set:
            self.network.update_2_windings_transformers(
                id=line_id, connected1=False, connected2=False
            )

    def reconnect_line(self, line_id: str, bus_or: int = 1, bus_ex: int = 1):
        """
        Reconnect a line to specified buses.

        Args:
            line_id: Line identifier
            bus_or: Bus number at origin (1 or 2)
            bus_ex: Bus number at extremity (1 or 2)
        """
        # P3: cached-set membership instead of full-table fetches (see disconnect_line).
        if line_id in self._lines_set:
            self.network.update_lines(id=line_id, connected1=True, connected2=True)
        elif line_id in self._trafos_set:
            self.network.update_2_windings_transformers(
                id=line_id, connected1=True, connected2=True
            )
    
    def get_line_flows(self, columns: Optional[List[str]] = None) -> pd.DataFrame:
        """
        Get current line flows (P, Q, I) for all lines.
        
        Args:
            columns: Optional list of columns to retrieve. 
                    If None, returns all flow and connectivity columns.
        
        Returns:
            DataFrame with requested columns.
        """
        default_cols = ['p1', 'q1', 'i1', 'p2', 'q2', 'i2', 'connected1', 'connected2']
        if columns is None:
            # Need to ensure connected1/2 are present for the 'connected' calculation below
            cols_to_fetch = default_cols
        else:
            cols_to_fetch = columns
            # But if the user wants 'connected', we might need to fetch the parts
            if 'connected' in cols_to_fetch:
                if 'connected1' not in cols_to_fetch: cols_to_fetch.append('connected1')
                if 'connected2' not in cols_to_fetch: cols_to_fetch.append('connected2')
        
        # Filter columns to only those present in pypowsybl (some transformers might lack connected1/2)
        # Actually pypowsybl columns are usually consistent per type.
        lines_df = self.network.get_lines()[cols_to_fetch]
        trafos_df = self.network.get_2_windings_transformers()[cols_to_fetch]
        
        all_branches = pd.concat([lines_df, trafos_df])
        
        if 'connected' in cols_to_fetch or columns is None:
            all_branches['connected'] = all_branches['connected1'] & all_branches['connected2']
        
        return all_branches
    
    def get_thermal_limits(self) -> Dict[str, Union[float, Tuple[float, float]]]:
        """
        Get thermal limits for all lines.
        
        Returns:
            Dictionary mapping line_id to thermal limit (A)
        """
        limits_df = self.network.get_operational_limits()
        limits_df = limits_df.reset_index()
        
        # Get permanent limits
        perm_limits = limits_df[limits_df['name'] == 'permanent_limit']
        
        thermal_limits = {}
        for _, row in perm_limits.iterrows():
            element_id = row['element_id']
            thermal_limits[element_id] = row['value']
        
        # Fill missing with high value
        default_limit = 9999.0
        for line_id in self._line_ids:
            if line_id not in thermal_limits:
                thermal_limits[line_id] = default_limit
        
        return thermal_limits
    
    def get_bus_voltages(self, columns: Optional[List[str]] = None) -> pd.DataFrame:
        """Get bus voltage magnitudes and angles."""
        cols = columns or ['v_mag', 'v_angle']
        return self.network.get_buses()[cols]
    
    def reset_to_base(self):
        """Reset working variant to base state."""
        self.set_working_variant(self.base_variant_id)

    def disconnect_lines_batch(self, line_ids: List[str]):
        """
        Disconnect multiple lines in a single batch operation.

        This is much faster than calling disconnect_line() in a loop.

        Args:
            line_ids: List of line IDs to disconnect
        """
        if not line_ids:
            return

        # Separate lines and transformers
        lines_to_disconnect = []
        trafos_to_disconnect = []

        for line_id in line_ids:
            if line_id in self._lines_set:
                lines_to_disconnect.append(line_id)
            elif line_id in self._trafos_set:
                trafos_to_disconnect.append(line_id)

        # Batch update lines
        if lines_to_disconnect:
            self.network.update_lines(
                id=lines_to_disconnect,
                connected1=[False] * len(lines_to_disconnect),
                connected2=[False] * len(lines_to_disconnect)
            )

        # Batch update transformers
        if trafos_to_disconnect:
            self.network.update_2_windings_transformers(
                id=trafos_to_disconnect,
                connected1=[False] * len(trafos_to_disconnect),
                connected2=[False] * len(trafos_to_disconnect)
            )

    def get_line_p1_array(self, nan_as_zero: bool = True) -> np.ndarray:
        """
        Get active power at terminal 1 for all lines as a numpy array.

        Args:
            nan_as_zero: If True (default), a branch without a computed flow
                (open, or outside the solved component) reads 0; if False, NaN.

        Returns:
            Array of p1 values in the same order as _line_ids
        """
        lines_df = self.network.get_lines()[['p1']]
        trafos_df = self.network.get_2_windings_transformers()[['p1']]

        # Build result array
        n_lines = len(self._line_ids)
        missing = 0.0 if nan_as_zero else np.nan
        result = np.full(n_lines, missing)

        # Extract p1 as dicts
        lines_p1 = lines_df['p1'].to_dict()
        trafos_p1 = trafos_df['p1'].to_dict()

        # Fill array using cached index mapping
        for line_id, idx in self._line_id_to_arr_idx.items():
            if line_id in lines_p1:
                p1 = lines_p1[line_id]
                result[idx] = p1 if not np.isnan(p1) else missing
            elif line_id in trafos_p1:
                p1 = trafos_p1[line_id]
                result[idx] = p1 if not np.isnan(p1) else missing

        return result

    def get_line_currents_array(self) -> Tuple[np.ndarray, np.ndarray]:
        """
        Get current magnitudes at both terminals for all lines as numpy arrays.

        Returns:
            Tuple of (i1_array, i2_array)
        """
        lines_df = self.network.get_lines()[['i1', 'i2']]
        trafos_df = self.network.get_2_windings_transformers()[['i1', 'i2']]

        # Build result arrays
        n_lines = len(self._line_ids)
        i1_arr = np.zeros(n_lines)
        i2_arr = np.zeros(n_lines)

        # Extract as dicts
        lines_i1 = lines_df['i1'].to_dict()
        lines_i2 = lines_df['i2'].to_dict()
        trafos_i1 = trafos_df['i1'].to_dict()
        trafos_i2 = trafos_df['i2'].to_dict()

        # Fill arrays using cached index mapping
        for line_id, idx in self._line_id_to_arr_idx.items():
            if line_id in lines_i1:
                i1 = lines_i1[line_id]
                i2 = lines_i2[line_id]
                i1_arr[idx] = i1 if not np.isnan(i1) else 0.0
                i2_arr[idx] = i2 if not np.isnan(i2) else 0.0
            elif line_id in trafos_i1:
                i1 = trafos_i1[line_id]
                i2 = trafos_i2[line_id]
                i1_arr[idx] = i1 if not np.isnan(i1) else 0.0
                i2_arr[idx] = i2 if not np.isnan(i2) else 0.0

        return i1_arr, i2_arr

    def _check_line_side_switches(self, line_id: str, voltage_level_id: str) -> Optional[Tuple[bool, bool]]:
        """
        Check the breaker and disconnector states at one extremity of a line.

        Delegates to the standalone function in helpers_pypowsybl.

        Args:
            line_id: The line (or transformer) identifier.
            voltage_level_id: The voltage level at this extremity.

        Returns:
            A tuple (breaker_open, all_disconnectors_open), or None if no
            breaker was found at this extremity.
        """
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _check_line_side_switches
        return _check_line_side_switches(self.network, line_id, voltage_level_id)

    def detect_non_reconnectable_lines(self) -> List[str]:
        """
        Detect non-reconnectable lines based on switch topology in the network.

        A disconnected line is considered non-reconnectable if:
        1. It has at least one open breaker at its extremity (it is disconnected
           via breaker), AND
        2. Line breakers at BOTH extremities are open, AND
        3. All line disconnectors (sectionneurs) at BOTH extremities are also open.

        This means the line is fully isolated at both ends with no path through
        any switch to a busbar, making reconnection impossible without physical
        intervention.

        Delegates to the standalone function in helpers_pypowsybl.

        Returns:
            List of line IDs that are non-reconnectable.
        """
        from expert_op4grid_recommender.utils.helpers_pypowsybl import detect_non_reconnectable_lines
        return detect_non_reconnectable_lines(self.network)

    def get_pst_ids(self) -> List[str]:
        """Get IDs of transformers with phase tap changers."""
        return self._pst_ids

    def get_pst_tap_info(self, pst_id: str) -> Dict[str, Any]:
        """
        Get tap information for a PST.
        
        Args:
            pst_id: The ID of the PST.
            
        Returns:
            Dictionary with 'tap', 'low_tap', 'high_tap', 'step_count'.
        """
        if pst_id not in self._pst_set:
            return {}
        
        # Get fresh data from network to account for variant changes
        df = self.network.get_phase_tap_changers().loc[[pst_id]]
        if df.empty:
            return {}
            
        row = df.iloc[0]
        return {
            'tap': int(row['tap']),
            'low_tap': int(row['low_tap']),
            'high_tap': int(row['high_tap']),
            'step_count': int(row['step_count']),
        }

    def update_pst_tap_step(self, pst_id: str, tap: int):
        """
        Update the tap position of a PST.
        
        Args:
            pst_id: The ID of the PST.
            tap: The new tap step number.
        """
        if pst_id in self._pst_set:
            self.network.update_phase_tap_changers(id=pst_id, tap=tap)
