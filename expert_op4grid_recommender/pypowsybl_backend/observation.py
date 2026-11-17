# expert_op4grid_recommender/pypowsybl_backend/observation.py
"""
Observation class providing grid2op-compatible interface for pypowsybl networks.

This module provides a read-only view of the network state with properties
matching the grid2op observation interface.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Tuple, Optional, Any, TYPE_CHECKING
import pypowsybl.loadflow as lf
# from expert_op4grid_recommender.utils.helpers import Timer

if TYPE_CHECKING:
    from .network_manager import NetworkManager
    from .action_space import ActionSpace


class PypowsyblObservation:
    """
    Grid2op-compatible observation interface for pypowsybl networks.

    Provides read-only access to network state including:
    - Line flows and loading ratios (rho)
    - Line status (connected/disconnected)
    - Bus voltage magnitudes and angles
    - Load and generation values
    - Topology information

    The simulate() method creates a temporary variant, applies an action,
    runs load flow, extracts results, and cleans up the variant.

    Attributes:
        _network_manager: Reference to the NetworkManager
        _thermal_limits: Cached thermal limits for rho calculation
    """

    _kept_variant_counter = 0
    
    def __init__(self, 
                 network_manager: 'NetworkManager',
                 action_space: 'ActionSpace',
                 thermal_limits: Optional[Dict[str, float]] = None,
                 variant_id: Optional[str] = None):
        """
        Initialize observation from network state.
        
        Args:
            network_manager: The NetworkManager instance
            action_space: The ActionSpace instance for simulate()
            thermal_limits: Optional override for thermal limits
        """
        self._network_manager = network_manager
        self._action_space = action_space
        self._thermal_limits = thermal_limits or network_manager.get_thermal_limits()
        self._variant_id = variant_id

        # Pre-calculate thermal limit arrays for efficient access
        all_lines = self._network_manager.name_line
        limit_or, limit_ex = self._network_manager.get_thermal_limits_arrays()
        self._limit_or = pd.Series(limit_or, index=all_lines)
        self._limit_ex = pd.Series(limit_ex, index=all_lines)
        
        # Apply overrides if any
        if thermal_limits:
            overrides = pd.Series(thermal_limits)
            # Find lines that are in both all_lines and overrides
            common = overrides.index.intersection(all_lines)
            if not common.empty:
                self._limit_or.update(overrides[common])
                self._limit_ex.update(overrides[common])
        
        # Cache current state
        self._refresh_state()
    
    def _refresh_state(self):
        """Refresh cached state from network. OPTIMIZED with vectorized operations."""
        nm = self._network_manager
        net = nm.network

        # Line and transformer information - fetch all at once including terminal buses
        # Columns needed for: flows, status, angles, and bus assignments
        line_cols = ['p1', 'q1', 'i1', 'p2', 'q2', 'i2', 'connected1', 'connected2',
                     'bus1_id', 'bus2_id', 'voltage_level1_id', 'voltage_level2_id']
        # nm.get_line_flows doesn't yet support bus1/2_id easily without internal logic change
        # Let's just do it here to ensure absolute control and minimal calls
        lines_df = net.get_lines()[line_cols]
        trafos_df = net.get_2_windings_transformers()[line_cols]
        all_terminals_df = pd.concat([lines_df, trafos_df])
        reindexed_flows = all_terminals_df.reindex(nm.name_line)

        # Bus voltages and angles - fetch with voltage_level_id for bus assignments.
        # Fetch connected_component here too so the component/main-load computation
        # below reuses this single get_buses() call instead of re-fetching it twice.
        bus_cols = ['v_mag', 'v_angle', 'voltage_level_id', 'connected_component']
        bus_data_df = net.get_buses()[bus_cols]
        bus_df_aligned = bus_data_df.reindex(nm.name_sub)

        # Cache line currents and powers as arrays for fast access
        self._cache_line_arrays(reindexed_flows)

        # Compute rho (loading ratio)
        self._compute_rho()

        # Line status - robustly convert to bool to avoid warnings
        self._line_status = reindexed_flows['connected1'].fillna(True).astype(bool).values & \
                            reindexed_flows['connected2'].fillna(True).astype(bool).values

        # Bus voltages and angles
        self._v_mag = bus_df_aligned['v_mag'].values
        self._v_angle = bus_df_aligned['v_angle'].values

        # Get angles per line terminal - pass pre-fetched bus data (full DataFrame for VL fallback)
        self._compute_line_angles(reindexed_flows, bus_data_df)

        # Compute bus assignments for line terminals - pass pre-fetched data
        self._compute_line_buses(reindexed_flows, bus_data_df)

        # Compute line terminal voltages (with VL fallback for disconnected terminals)
        self._compute_line_voltages(reindexed_flows, bus_data_df)

        # Compute line impedances (R, X) from network data
        self._compute_line_impedances(reindexed_flows)

        # Compute effective terminal power factor cos_phi = |X|/|Z|
        self._compute_line_cos_phi()

        # Load and generation - ensure alignment with name_load/name_gen.
        # Single get_loads() call (with bus_id) reused for both the aligned
        # load arrays and the per-component load below.
        loads_full = net.get_loads()[['p', 'q', 'bus_id']]
        loads_df = loads_full[['p', 'q']].reindex(nm.name_load)
        gens_df = net.get_generators()[['p', 'q', 'energy_source', 'target_p']].reindex(nm.name_gen)
        self._load_p = loads_df['p'].fillna(0.0).values
        self._load_q = loads_df['q'].fillna(0.0).values
        self._gen_p = gens_df['p'].fillna(0.0).values
        self._gen_q = gens_df['q'].fillna(0.0).values
        self._gen_type = gens_df['energy_source'].fillna("UNKNOWN").values
        self._gen_target_p = gens_df['target_p'].values.astype(float)

        # Connected components and main component load - reuse bus_data_df
        # (fetched once above) instead of re-calling get_buses() twice.
        self._n_components = bus_data_df['connected_component'].nunique()

        loads_p_bus = loads_full[['p', 'bus_id']]
        if loads_p_bus.empty:
            self._main_component_load_mw = 0.0
        else:
            buses_comp = bus_data_df[['connected_component']]
            load_with_comp = loads_p_bus.merge(buses_comp, left_on='bus_id', right_index=True)
            if load_with_comp.empty:
                self._main_component_load_mw = 0.0
            else:
                comp_loads = load_with_comp.groupby('connected_component')['p'].sum()
                self._main_component_load_mw = float(comp_loads.max()) if not comp_loads.empty else 0.0
    
    def _cache_line_arrays(self, reindexed_flows: pd.DataFrame):
        """Cache line current and power arrays for fast property access. OPTIMIZED with reindex."""
        # reindexed_flows is already aligned with nm.name_line
        self._cached_i_or = reindexed_flows['i1'].fillna(0.0).values
        self._cached_i_ex = reindexed_flows['i2'].fillna(0.0).values
        self._cached_p_or = reindexed_flows['p1'].fillna(0.0).values
        self._cached_p_ex = reindexed_flows['p2'].fillna(0.0).values

    def _compute_rho(self):
        """Compute line loading ratios. OPTIMIZED with vectorized operations."""
        from expert_op4grid_recommender.config import MAX_RHO_BOTH_EXTREMITIES
        
        # Use pre-calculated thermal limits (including overrides)
        limit_or = self._limit_or.values
        limit_ex = self._limit_ex.values

        # Avoid division by zero
        limit_or = np.where(limit_or < 1e-6, 1e-6, limit_or)
        limit_ex = np.where(limit_ex < 1e-6, 1e-6, limit_ex)

        # Use cached current arrays (cached in _cache_line_arrays via _refresh_state)
        i_or = np.abs(self._cached_i_or)
        
        if MAX_RHO_BOTH_EXTREMITIES:
            i_ex = np.abs(self._cached_i_ex)
            rho_or = i_or / limit_or
            rho_ex = i_ex / limit_ex
            self._rho = np.maximum(rho_or, rho_ex)
        else:
            self._rho = i_or / limit_or
    
    def _compute_line_angles(self, terminals: pd.DataFrame, bus_data_df: pd.DataFrame):
        """Compute voltage angles at line terminals with VL fallback for disconnected lines.

        For connected terminals, maps directly from bus_id to bus angle.
        For disconnected terminals (bus_id is NaN), falls back to the average
        angle of buses in the same voltage level - this gives the correct
        bus angle even when the terminal itself is disconnected.
        """
        bus_angles = bus_data_df['v_angle']
        theta_or = terminals['bus1_id'].map(bus_angles)
        theta_ex = terminals['bus2_id'].map(bus_angles)

        # Fallback for disconnected terminals: use voltage level average angle
        vl_angle = bus_data_df.groupby('voltage_level_id')['v_angle'].mean()
        theta_or = theta_or.fillna(terminals['voltage_level1_id'].map(vl_angle))
        theta_ex = theta_ex.fillna(terminals['voltage_level2_id'].map(vl_angle))

        self._theta_or = theta_or.fillna(0.0).values / 360 * 2 * np.pi
        self._theta_ex = theta_ex.fillna(0.0).values / 360 * 2 * np.pi
    
    def _compute_line_voltages(self, terminals: pd.DataFrame, bus_data_df: pd.DataFrame):
        """Compute voltage magnitudes at line terminals with VL fallback.

        Same pattern as _compute_line_angles: for disconnected terminals where
        bus_id is NaN, falls back to the average voltage magnitude of buses
        in the same voltage level.
        """
        bus_vmag = bus_data_df['v_mag']
        v_or = terminals['bus1_id'].map(bus_vmag)
        v_ex = terminals['bus2_id'].map(bus_vmag)

        # Fallback for disconnected terminals: use voltage level average V
        vl_vmag = bus_data_df.groupby('voltage_level_id')['v_mag'].mean()
        v_or = v_or.fillna(terminals['voltage_level1_id'].map(vl_vmag))
        v_ex = v_ex.fillna(terminals['voltage_level2_id'].map(vl_vmag))

        self._v_or = v_or.fillna(0.0).values
        self._v_ex = v_ex.fillna(0.0).values

    def _compute_line_impedances(self, terminals: pd.DataFrame):
        """Assign line resistance and reactance from network branch data.

        R and X are structural branch parameters — invariant across variants and
        across the operating point — so they are fetched from the network once
        and cached on the NetworkManager (keyed by the fixed ``name_line``
        ordering), instead of re-fetching ``get_lines()`` / ``get_2wt()`` on every
        observation. On a large network this removes two cross-JNI DataFrame
        fetches per observation (a hot path during action reassessment).
        """
        nm = self._network_manager
        cached = getattr(nm, "_cached_line_rx", None)
        if cached is None:
            net = nm.network
            lines_rx = net.get_lines()[['r', 'x']]
            trafos_rx = net.get_2_windings_transformers()[['r', 'x']]
            all_rx = pd.concat([lines_rx, trafos_rx]).reindex(nm.name_line)
            cached = (all_rx['r'].fillna(0.0).values, all_rx['x'].fillna(1e-6).values)
            nm._cached_line_rx = cached
        self._line_r, self._line_x = cached

    def _compute_line_cos_phi(self):
        """Compute effective terminal power factor for each line.

        For HV transmission lines, the terminal apparent power relationship is:
          P ≈ √3 · V · I · sin(φ_Z) where φ_Z = arctan(X/R) is the impedance angle.
        Since sin(φ_Z) = |X|/|Z|, we use |X|/|Z| as the effective "cos_phi"
        factor that converts between P and S = √3·V·I.

        This is NOT the traditional power factor cos(φ) = R/|Z|, but rather
        the factor that satisfies: P = √3 · V · I · factor, which empirically
        gives errors < 3% vs actual rho.
        """
        z = np.sqrt(self._line_r**2 + self._line_x**2)
        z = np.where(z < 1e-10, 1e-10, z)
        self._line_cos_phi = np.abs(self._line_x) / z

    def _compute_line_buses(self, terminals: pd.DataFrame, buses_df: pd.DataFrame):
        """Compute bus assignments for line terminals. OPTIMIZED with vectorization."""
        # terminals is already reindexed to nm.name_line
        
        # 1. Pre-calculate local bus ranks (1, 2, ...) per Voltage Level
        if not buses_df.empty:
            # Sort by ID within each VL to ensure consistent 1, 2 numbering
            buses_sorted = buses_df.sort_index()
            # cumcount() gives 0, 1, ... so add 1 for bus numbers
            buses_sorted['rank'] = buses_sorted.groupby('voltage_level_id').cumcount() + 1
            bus_to_rank = buses_sorted['rank']
        else:
            bus_to_rank = pd.Series(dtype=int)

        # 2. Map ranks and handle disconnections
        # Default connected to True if missing (transformers often connected)
        # Fix downcasting warnings by casting to bool after fillna
        conn1 = terminals['connected1'].fillna(True).astype(bool).values
        conn2 = terminals['connected2'].fillna(True).astype(bool).values
        
        # Map bus ranks
        rank1 = terminals['bus1_id'].map(bus_to_rank).fillna(1).astype(int).values
        rank2 = terminals['bus2_id'].map(bus_to_rank).fillna(1).astype(int).values
        
        # Disconnected terminals set to -1
        # Also handle cases where bus_id is NaN but connected is True (should not happen in converged net)
        self._line_or_bus = np.where(conn1 & terminals['bus1_id'].notna(), rank1, -1)
        self._line_ex_bus = np.where(conn2 & terminals['bus2_id'].notna(), rank2, -1)
    
    # ========== Grid2op-compatible properties ==========
    
    @property
    def rho(self) -> np.ndarray:
        """Line loading ratios (I / I_max)."""
        return self._rho.copy()
    
    @property
    def line_status(self) -> np.ndarray:
        """Boolean array of line connection status."""
        return self._line_status.copy()
    
    @property
    def line_or_bus(self) -> np.ndarray:
        """
        Bus assignment at line origin terminals.
        
        Values:
        - 1 or 2: Local bus number within the substation
        - -1: Disconnected
        """
        return self._line_or_bus.copy()
    
    @property
    def line_ex_bus(self) -> np.ndarray:
        """
        Bus assignment at line extremity terminals.
        
        Values:
        - 1 or 2: Local bus number within the substation
        - -1: Disconnected
        """
        return self._line_ex_bus.copy()
    
    @property
    def a_or(self) -> np.ndarray:
        """
        Current intensity at line origin terminals (A).
        """
        return self._get_line_i_or()
    
    @property
    def a_ex(self) -> np.ndarray:
        """
        Current intensity at line extremity terminals (A).
        """
        return self._get_line_i_ex()
    
    @property
    def p_or(self) -> np.ndarray:
        """Active power at line origin terminals (MW)."""
        return self._get_line_p_or()
    
    @property
    def p_ex(self) -> np.ndarray:
        """Active power at line extremity terminals (MW)."""
        return self._get_line_p_ex()
    
    def _get_line_i_or(self) -> np.ndarray:
        """Get current intensity at line origins (A). Returns cached array."""
        return self._cached_i_or.copy()

    def _get_line_i_ex(self) -> np.ndarray:
        """Get current intensity at line extremities (A). Returns cached array."""
        return self._cached_i_ex.copy()

    def _get_line_p_or(self) -> np.ndarray:
        """Get active power at line origins (MW). Returns cached array."""
        return self._cached_p_or.copy()

    def _get_line_p_ex(self) -> np.ndarray:
        """Get active power at line extremities (MW). Returns cached array."""
        return self._cached_p_ex.copy()
    
    @property
    def theta_or(self) -> np.ndarray:
        """Voltage angles at line origin terminals (degrees)."""
        return self._theta_or.copy()
    
    @property
    def theta_ex(self) -> np.ndarray:
        """Voltage angles at line extremity terminals (degrees)."""
        return self._theta_ex.copy()

    @property
    def v_or(self) -> np.ndarray:
        """Voltage magnitude at line origin terminals (kV)."""
        return self._v_or.copy()

    @property
    def v_ex(self) -> np.ndarray:
        """Voltage magnitude at line extremity terminals (kV)."""
        return self._v_ex.copy()

    @property
    def line_r(self) -> np.ndarray:
        """Line resistance (ohms) per branch."""
        return self._line_r.copy()

    @property
    def line_x(self) -> np.ndarray:
        """Line reactance (ohms) per branch."""
        return self._line_x.copy()

    @property
    def line_cos_phi(self) -> np.ndarray:
        """Effective terminal power factor |X|/|Z| per branch.

        This is the factor that satisfies P ≈ √3·V·I·cos_phi for HV lines,
        where the dominant power flow is driven by reactance (X >> R).
        """
        return self._line_cos_phi.copy()

    @property
    def load_p(self) -> np.ndarray:
        """Active power consumption at loads (MW)."""
        return self._load_p.copy()
    
    @property
    def load_q(self) -> np.ndarray:
        """Reactive power consumption at loads (MVAr)."""
        return self._load_q.copy()
    
    @property
    def gen_p(self) -> np.ndarray:
        """Active power generation (MW)."""
        return self._gen_p.copy()
    
    @property
    def gen_q(self) -> np.ndarray:
        """Reactive power generation (MVAr)."""
        return self._gen_q.copy()

    @property
    def gen_target_p(self) -> np.ndarray:
        """Active power setpoint of the generators (MW, ``target_p``, production
        positive). It differs from ``-gen_p`` by the generator's share of the
        load-flow slack compensation; ``set_gen_p`` actions set it."""
        return self._gen_target_p.copy()

    @property
    def gen_type(self) -> np.ndarray:
        """Generator type (energy source)."""
        return self._gen_type.copy()
    
    @property
    def name_line(self) -> np.ndarray:
        """Array of line names."""
        return self._network_manager.name_line
    
    @property
    def name_sub(self) -> np.ndarray:
        """Array of substation names."""
        return self._network_manager.name_sub
    
    @property
    def name_gen(self) -> np.ndarray:
        """Array of generator names."""
        return self._network_manager.name_gen

    @property
    def gen_energy_source(self) -> np.ndarray:
        """Array of generator energy source strings (e.g. 'WIND', 'SOLAR', 'THERMAL', ...)."""
        return self._network_manager.gen_energy_source

    @property
    def gen_pmax(self) -> np.ndarray:
        """Generator maximum active power (MW), like grid2op's ``gen_pmax``."""
        return self._network_manager.gen_pmax

    @property
    def gen_renewable(self) -> np.ndarray:
        """Boolean array: True if generator is renewable (WIND or SOLAR by default)."""
        from expert_op4grid_recommender import config
        renewable_sources = set(config.RENEWABLE_ENERGY_SOURCES)
        return np.array([str(src).upper() in renewable_sources for src in self.gen_energy_source])

    @property
    def name_load(self) -> np.ndarray:
        """Array of load names."""
        return self._network_manager.name_load

    @property
    def n_components(self) -> int:
        """Number of connected components (islands) in the network."""
        return self._n_components

    @property
    def main_component_load_mw(self) -> float:
        """Total active power of loads (MW) in the main (largest load) connected component."""
        return self._main_component_load_mw
    
    @property
    def line_or_to_subid(self) -> np.ndarray:
        """Substation index at line origin."""
        return self._network_manager.get_line_or_subid()
    
    @property
    def line_ex_to_subid(self) -> np.ndarray:
        """Substation index at line extremity."""
        return self._network_manager.get_line_ex_subid()
    
    @property
    def n_line(self) -> int:
        """Number of lines."""
        return self._network_manager.n_line
    
    @property
    def n_sub(self) -> int:
        """Number of substations."""
        return self._network_manager.n_sub
    
    def _count_sub_elements(self, sub_id: int) -> int:
        """
        Count the number of elements in a substation. OPTIMIZED using cached data.

        Elements include loads, generators, line origins, and line extremities.
        This must match the ordering used in sub_topology().

        Args:
            sub_id: Substation index

        Returns:
            Number of elements in this substation
        """
        nm = self._network_manager
        return (len(nm._loads_per_sub[sub_id]) + len(nm._gens_per_sub[sub_id]) +
                len(nm._lines_or_per_sub[sub_id]) + len(nm._lines_ex_per_sub[sub_id]))

    @property
    def sub_info(self) -> np.ndarray:
        """
        Number of elements per substation. OPTIMIZED using cached data.

        Counts all elements (gens, loads, lines_or, lines_ex) per substation,
        matching the ordering in sub_topology().
        """
        return self._network_manager._cached_sub_info.copy()

    @property
    def topo_vect(self) -> np.ndarray:
        """
        Topology vector (bus assignment for all elements).

        Concatenates the topology vectors for all substations in order.
        Each element's value indicates which bus (1 or 2) it is connected to,
        or -1 if disconnected.
        """
        topo_arrays = [self.sub_topology(sub_id) for sub_id in range(self.n_sub)]
        return np.concatenate(topo_arrays) if topo_arrays else np.array([], dtype=int)
    
    def sub_topology(self, sub_id: int) -> np.ndarray:
        """
        Get topology vector for a specific substation. OPTIMIZED using cached data.

        Returns bus assignments for all elements connected to this substation.
        Elements are ordered as: loads, generators, lines_or, lines_ex

        Args:
            sub_id: Substation index

        Returns:
            Array of bus assignments for elements in this substation.
            Values: 1 or 2 for connected buses, -1 for disconnected.
        """
        nm = self._network_manager
        sub_name = nm._substation_ids[sub_id]

        # Use cached element lists
        load_indices = nm._loads_per_sub[sub_id]
        gen_indices = nm._gens_per_sub[sub_id]
        line_or_indices = nm._lines_or_per_sub[sub_id]
        line_ex_indices = nm._lines_ex_per_sub[sub_id]

        bus_assignments = []

        # Ensure we have cached bus info for loads and gens
        if not hasattr(self, '_load_bus_cache'):
            self._cache_element_buses()

        # Get bus assignments for loads
        for load_idx in load_indices:
            bus_assignments.append(self._load_bus_cache[load_idx])

        # Get bus assignments for generators
        for gen_idx in gen_indices:
            bus_assignments.append(self._gen_bus_cache[gen_idx])

        # Get bus assignments for line origins
        for line_idx in line_or_indices:
            bus_assignments.append(int(self._line_or_bus[line_idx]))

        # Get bus assignments for line extremities
        for line_idx in line_ex_indices:
            bus_assignments.append(int(self._line_ex_bus[line_idx]))

        return np.array(bus_assignments, dtype=int)

    def _cache_element_buses(self):
        """Cache bus assignments for loads and generators."""
        nm = self._network_manager
        net = nm.network

        # Get DataFrames once
        loads_df = net.get_loads()
        gens_df = net.get_generators()
        buses_df = net.get_buses()

        # Build voltage level -> sorted bus list mapping
        bus_to_vl = buses_df['voltage_level_id'].to_dict() if len(buses_df) > 0 else {}
        vl_to_buses = {}
        for bus_id, vl_id in bus_to_vl.items():
            if vl_id not in vl_to_buses:
                vl_to_buses[vl_id] = []
            vl_to_buses[vl_id].append(bus_id)

        # Build local bus number lookup
        vl_bus_to_local = {}
        for vl_id, bus_list in vl_to_buses.items():
            sorted_buses = sorted(bus_list)
            for idx, bus_id in enumerate(sorted_buses):
                vl_bus_to_local[(vl_id, bus_id)] = idx + 1

        # Cache load bus assignments
        self._load_bus_cache = np.ones(nm._n_load, dtype=int)
        if len(loads_df) > 0:
            load_bus_ids = loads_df['bus_id'].to_dict() if 'bus_id' in loads_df.columns else {}
            load_connected = loads_df['connected'].to_dict() if 'connected' in loads_df.columns else {}
            load_vl_ids = loads_df['voltage_level_id'].to_dict() if 'voltage_level_id' in loads_df.columns else {}

            for i, load_id in enumerate(nm._load_ids):
                bus_id = load_bus_ids.get(load_id)
                connected = load_connected.get(load_id, True)
                vl_id = load_vl_ids.get(load_id, '')

                if pd.isna(bus_id) or not connected:
                    self._load_bus_cache[i] = -1
                else:
                    self._load_bus_cache[i] = vl_bus_to_local.get((vl_id, bus_id), 1)

        # Cache generator bus assignments
        self._gen_bus_cache = np.ones(nm._n_gen, dtype=int)
        if len(gens_df) > 0:
            gen_bus_ids = gens_df['bus_id'].to_dict() if 'bus_id' in gens_df.columns else {}
            gen_connected = gens_df['connected'].to_dict() if 'connected' in gens_df.columns else {}
            gen_vl_ids = gens_df['voltage_level_id'].to_dict() if 'voltage_level_id' in gens_df.columns else {}

            for i, gen_id in enumerate(nm._gen_ids):
                bus_id = gen_bus_ids.get(gen_id)
                connected = gen_connected.get(gen_id, True)
                vl_id = gen_vl_ids.get(gen_id, '')

                if pd.isna(bus_id) or not connected:
                    self._gen_bus_cache[i] = -1
                else:
                    self._gen_bus_cache[i] = vl_bus_to_local.get((vl_id, bus_id), 1)

    def _get_local_bus_number(self, bus_id: str, vl_id: str) -> int:
        """
        Convert a pypowsybl bus_id to a local bus number (1 or 2) within a voltage level.
        OPTIMIZED - uses cached data when possible.

        Args:
            bus_id: The pypowsybl bus ID
            vl_id: The voltage level (substation) ID

        Returns:
            Local bus number (1 or 2)
        """
        # Try to use cached lookup if available
        if hasattr(self, '_vl_bus_to_local_cache'):
            return self._vl_bus_to_local_cache.get((vl_id, bus_id), 1)

        # Fallback to original implementation
        net = self._network_manager.network
        buses_df = net.get_buses()

        # Build cache for future use
        bus_to_vl = buses_df['voltage_level_id'].to_dict() if len(buses_df) > 0 else {}
        vl_to_buses = {}
        for bid, v in bus_to_vl.items():
            if v not in vl_to_buses:
                vl_to_buses[v] = []
            vl_to_buses[v].append(bid)

        self._vl_bus_to_local_cache = {}
        for v, bus_list in vl_to_buses.items():
            sorted_buses = sorted(bus_list)
            for idx, bid in enumerate(sorted_buses):
                self._vl_bus_to_local_cache[(v, bid)] = idx + 1

        return self._vl_bus_to_local_cache.get((vl_id, bus_id), 1)
    
    def get_obj_connect_to(self, substation_id: int) -> Dict[str, List[int]]:
        """
        Get objects connected to a substation. OPTIMIZED using cached data.

        Args:
            substation_id: Substation index

        Returns:
            Dictionary with keys 'loads_id', 'generators_id', 'lines_or_id',
            'lines_ex_id' containing lists of element indices.
        """
        nm = self._network_manager

        return {
            'loads_id': list(nm._loads_per_sub[substation_id]),
            'generators_id': list(nm._gens_per_sub[substation_id]),
            'lines_or_id': list(nm._lines_or_per_sub[substation_id]),
            'lines_ex_id': list(nm._lines_ex_per_sub[substation_id])
        }
    
    def topo_vect_element(self, topo_vect_pos: int) -> Dict[str, Any]:
        """
        Get element information for a topology vector position. OPTIMIZED using cached data.

        The topology vector ordering matches sub_topology():
        loads, generators, lines_or, lines_ex for each substation.

        Args:
            topo_vect_pos: Position in the topology vector

        Returns:
            Dictionary with element type and ID. For lines:
            - {'line_id': True, 'line_or_id': line_idx} for line origins
            - {'line_id': True, 'line_ex_id': line_idx} for line extremities
        """
        nm = self._network_manager

        # Find which substation this position belongs to using cached sub_info
        sub_info = nm._cached_sub_info
        cumsum = 0
        sub_id = 0
        for i, count in enumerate(sub_info):
            if cumsum + count > topo_vect_pos:
                sub_id = i
                break
            cumsum += count

        # Local position within the substation
        local_pos = topo_vect_pos - cumsum

        # Use cached element lists for this substation
        load_ids_in_sub = nm._loads_per_sub[sub_id]
        gen_ids_in_sub = nm._gens_per_sub[sub_id]
        line_or_ids_in_sub = nm._lines_or_per_sub[sub_id]
        line_ex_ids_in_sub = nm._lines_ex_per_sub[sub_id]

        # Check loads
        load_count = len(load_ids_in_sub)
        if local_pos < load_count:
            return {'type': 'load', 'load_id': load_ids_in_sub[local_pos]}
        local_pos -= load_count

        # Check generators
        gen_count = len(gen_ids_in_sub)
        if local_pos < gen_count:
            return {'type': 'gen', 'gen_id': gen_ids_in_sub[local_pos]}
        local_pos -= gen_count

        # Check line origins
        line_or_count = len(line_or_ids_in_sub)
        if local_pos < line_or_count:
            return {'line_id': True, 'line_or_id': line_or_ids_in_sub[local_pos]}
        local_pos -= line_or_count

        # Check line extremities
        if local_pos < len(line_ex_ids_in_sub):
            return {'line_id': True, 'line_ex_id': line_ex_ids_in_sub[local_pos]}

        # Fallback
        return {'type': 'unknown', 'id': topo_vect_pos}
    
    def get_time_stamp(self):
        """Get timestamp of the observation (placeholder)."""
        from datetime import datetime
        return datetime.now()
    
    def simulate(self,
                 action: 'PypowsyblAction',
                 time_step: int = 0,
                 keep_variant: bool = False,
                 fast_mode: bool = True) -> Tuple['PypowsyblObservation', float, bool, Dict]:
        """
        Simulate the effect of an action without modifying the base network.

        This is the key method that replaces grid2op's obs.simulate().
        It uses pypowsybl variants to create a temporary copy of the network,
        apply the action, run load flow, and return results.

        Args:
            action: Action to simulate (PypowsyblAction or combined action)
            time_step: Simulation timestep (for compatibility, not used in static analysis)
            keep_variant: If True, the network variant is kept alive after simulation
                         and stored as ``_variant_id`` on the returned observation.
                         The caller is responsible for cleanup via
                         ``nm.remove_variant(obs._variant_id)`` when done.
            fast_mode: If True, runs the tap-changer voltage control in the
                cheaper AFTER_GENERATOR_VOLTAGE_CONTROL mode (~6-7x fewer Newton
                iterations, same currents); see NetworkManager.run_load_flow.

        Returns:
            Tuple of (new_observation, reward, done, info)
            - new_observation: PypowsyblObservation after action
            - reward: Always 0.0 (not used in this application)
            - done: True if simulation failed
            - info: Dictionary with 'exception' key if errors occurred
        """
        nm = self._network_manager
        if keep_variant:
            PypowsyblObservation._kept_variant_counter += 1
            variant_id = f"simulate_kept_{PypowsyblObservation._kept_variant_counter}_{time_step}"
        else:
            variant_id = f"simulate_{id(action)}_{time_step}"
        info = {"exception": []}

        try:
            # Create a new variant branching from the current observation's variant
            # (or from the base variant if self._variant_id is None)
            nm.create_variant(variant_id, from_variant=self._variant_id)
            nm.set_working_variant(variant_id)

            # Apply action
            action.apply(nm)

            # Run load flow in fast mode (disabled voltage control) for variants if requested
            result = nm.run_load_flow(fast=fast_mode)

            if result is None or result.status != lf.ComponentStatus.CONVERGED:
                info["exception"].append(
                    Exception(f"Load flow did not converge: {result.status if result else 'No result'}")
                )
                # Return observation with NaN values
                obs_simu = PypowsyblObservation(nm, self._action_space, self._thermal_limits, 
                                               variant_id=variant_id if keep_variant else None)
                return obs_simu, 0.0, True, info

            # Create observation from simulated state
            obs_simu = PypowsyblObservation(nm, self._action_space, self._thermal_limits,
                                           variant_id=variant_id if keep_variant else None)

            return obs_simu, 0.0, False, info

        except Exception as e:
            print(f"Warning: Action simulation failed for action {action}: {e}")
            info["exception"].append(e)
            # Try to create an observation anyway
            try:
                obs_simu = PypowsyblObservation(nm, self._action_space, self._thermal_limits,
                                               variant_id=variant_id if keep_variant else None)
            except Exception as inner_e:
                print(f"Error: Failed to create fallback observation: {inner_e}")
                obs_simu = self  # Return self if we can't create new obs
            return obs_simu, 0.0, True, info

        finally:
            # Always return to base variant
            nm.set_working_variant(nm.base_variant_id)
            if keep_variant:
                # Track the retained variant so the NetworkManager LRU backstop
                # can bound accumulation across analyses (review C4). Under
                # normal operation the caller (BaselineContext.release /
                # reassessment cleanup) still releases it explicitly.
                nm.register_kept_variant(variant_id)
            else:
                # Transient variant — drop it immediately.
                nm.remove_variant(variant_id)

    def __add__(self, action: 'PypowsyblAction') -> 'PypowsyblObservation':
        """
        Support obs + action syntax (returns observation with action applied).

        Creates a wrapper observation that reflects the action's topology changes
        without actually modifying the network.
        """
        return ObservationWithTopologyOverride(self, action)


class ObservationWithTopologyOverride(PypowsyblObservation):
    """
    Observation wrapper that applies topology overrides from an action.

    This class doesn't actually modify the network - it just presents a view
    of what the topology would look like after the action is applied.
    Used by grid2op-style code that does `obs + action` to see the impact.
    """

    def __init__(self, base_obs: 'PypowsyblObservation', action: 'PypowsyblAction'):
        """
        Initialize with base observation and action.

        Args:
            base_obs: The original observation
            action: The action whose topology changes should be reflected
        """
        # Don't call super().__init__ - we're wrapping base_obs instead
        self._base_obs = base_obs
        self._action = action
        self._network_manager = base_obs._network_manager
        self._action_space = base_obs._action_space
        self._thermal_limits = base_obs._thermal_limits

        # Extract topology overrides from the action
        self._lines_or_bus_override = {}
        self._lines_ex_bus_override = {}
        self._loads_bus_override = {}
        self._gens_bus_override = {}
        self._substations_override = {}

        self._extract_topology_overrides(action)

    def _extract_topology_overrides(self, action: 'PypowsyblAction'):
        """Extract topology changes from the action.

        All PypowsyblAction objects now store topology info directly as attributes,
        and the __add__ method merges these when actions are combined.
        """
        if hasattr(action, 'lines_or_bus'):
            self._lines_or_bus_override = dict(action.lines_or_bus) if action.lines_or_bus else {}
        if hasattr(action, 'lines_ex_bus'):
            self._lines_ex_bus_override = dict(action.lines_ex_bus) if action.lines_ex_bus else {}
        if hasattr(action, 'loads_bus'):
            self._loads_bus_override = dict(action.loads_bus) if action.loads_bus else {}
        if hasattr(action, 'gens_bus'):
            self._gens_bus_override = dict(action.gens_bus) if action.gens_bus else {}
        if hasattr(action, 'substations'):
            self._substations_override = dict(action.substations) if action.substations else {}

    @property
    def topo_vect(self) -> np.ndarray:
        """
        Topology vector with action overrides applied.
        """
        topo_arrays = [self.sub_topology(sub_id) for sub_id in range(self.n_sub)]
        return np.concatenate(topo_arrays) if topo_arrays else np.array([], dtype=int)

    def sub_topology(self, sub_id: int) -> np.ndarray:
        """
        Get topology vector for a substation with action overrides applied.
        OPTIMIZED using cached data.
        """
        # Check if there's a full substation override
        if sub_id in self._substations_override:
            return np.array(self._substations_override[sub_id], dtype=int)

        # Get base topology from the underlying observation
        base_topo = self._base_obs.sub_topology(sub_id)

        nm = self._network_manager

        # Use cached element lists for this substation
        load_indices = nm._loads_per_sub[sub_id]
        gen_indices = nm._gens_per_sub[sub_id]
        line_or_indices = nm._lines_or_per_sub[sub_id]
        line_ex_indices = nm._lines_ex_per_sub[sub_id]

        # Track current position in the topology vector
        pos = 0

        # Process loads in this substation
        for load_idx in load_indices:
            load_id = nm._load_ids[load_idx]
            if load_id in self._loads_bus_override:
                base_topo[pos] = self._loads_bus_override[load_id]
            pos += 1

        # Process generators in this substation
        for gen_idx in gen_indices:
            gen_id = nm._gen_ids[gen_idx]
            if gen_id in self._gens_bus_override:
                base_topo[pos] = self._gens_bus_override[gen_id]
            pos += 1

        # Process line origins in this substation
        for line_idx in line_or_indices:
            line_id = nm._line_ids[line_idx]
            if line_id in self._lines_or_bus_override:
                base_topo[pos] = self._lines_or_bus_override[line_id]
            pos += 1

        # Process line extremities in this substation
        for line_idx in line_ex_indices:
            line_id = nm._line_ids[line_idx]
            if line_id in self._lines_ex_bus_override:
                base_topo[pos] = self._lines_ex_bus_override[line_id]
            pos += 1

        return base_topo

    # Delegate all other properties to base observation
    @property
    def rho(self) -> np.ndarray:
        return self._base_obs.rho

    @property
    def line_status(self) -> np.ndarray:
        return self._base_obs.line_status

    @property
    def name_line(self) -> List[str]:
        return self._base_obs.name_line

    @property
    def name_sub(self) -> List[str]:
        return self._base_obs.name_sub

    @property
    def name_gen(self) -> List[str]:
        return self._base_obs.name_gen

    @property
    def name_load(self) -> List[str]:
        return self._base_obs.name_load

    @property
    def n_line(self) -> int:
        return self._base_obs.n_line

    @property
    def n_sub(self) -> int:
        return self._base_obs.n_sub

    @property
    def sub_info(self) -> np.ndarray:
        return self._base_obs.sub_info

    @property
    def n_components(self) -> int:
        """Number of connected components (islands) in the network."""
        return self._n_components

    @property
    def main_component_load_mw(self) -> float:
        """Total active power of loads (MW) in the main (largest load) connected component."""
        return self._main_component_load_mw

    @property
    def a_or(self) -> np.ndarray:
        return self._base_obs.a_or

    @property
    def a_ex(self) -> np.ndarray:
        return self._base_obs.a_ex

    @property
    def p_or(self) -> np.ndarray:
        return self._base_obs.p_or

    @property
    def p_ex(self) -> np.ndarray:
        return self._base_obs.p_ex

    @property
    def q_or(self) -> np.ndarray:
        return self._base_obs.q_or

    @property
    def q_ex(self) -> np.ndarray:
        return self._base_obs.q_ex

    @property
    def v_or(self) -> np.ndarray:
        return self._base_obs.v_or

    @property
    def v_ex(self) -> np.ndarray:
        return self._base_obs.v_ex

    @property
    def line_r(self) -> np.ndarray:
        return self._base_obs.line_r

    @property
    def line_x(self) -> np.ndarray:
        return self._base_obs.line_x

    @property
    def line_cos_phi(self) -> np.ndarray:
        return self._base_obs.line_cos_phi

    @property
    def theta_or(self) -> np.ndarray:
        return self._base_obs.theta_or

    @property
    def theta_ex(self) -> np.ndarray:
        return self._base_obs.theta_ex

    @property
    def line_or_to_subid(self) -> np.ndarray:
        return self._base_obs.line_or_to_subid

    @property
    def line_ex_to_subid(self) -> np.ndarray:
        return self._base_obs.line_ex_to_subid


class PypowsyblAction:
    """
    Represents an action that can be applied to a pypowsybl network.

    This is the base class for actions. Actions are created by ActionSpace
    and can be combined using the + operator.

    Topology info (lines_or_bus, lines_ex_bus, etc.) is stored as attributes
    so that ObservationWithTopologyOverride can extract it for computing
    the impact of the action on topology.
    """

    def __init__(self):
        self._modifications = []
        # Topology info for observation + action operations
        self.lines_or_bus = {}
        self.lines_ex_bus = {}
        self.loads_bus = {}
        self.gens_bus = {}
        self.pst_tap = {}
        self.substations = {}
        # Power reduction info (active power setpoint changes)
        self.loads_p = {}   # load_name -> target_p (MW)
        self.gens_p = {}    # gen_name -> target_p (MW)

    def apply(self, network_manager: 'NetworkManager'):
        """Apply this action to the network."""
        for modification in self._modifications:
            modification(network_manager)

    def __add__(self, other: 'PypowsyblAction') -> 'PypowsyblAction':
        """Combine two actions, merging their topology info."""
        combined = PypowsyblAction()
        combined._modifications = self._modifications + other._modifications
        # Merge topology info from both actions
        combined.lines_or_bus = {**self.lines_or_bus, **other.lines_or_bus}
        combined.lines_ex_bus = {**self.lines_ex_bus, **other.lines_ex_bus}
        combined.loads_bus = {**self.loads_bus, **other.loads_bus}
        combined.gens_bus = {**self.gens_bus, **other.gens_bus}
        combined.pst_tap = {**self.pst_tap, **other.pst_tap}
        combined.substations = {**self.substations, **other.substations}
        # Merge power reduction info
        combined.loads_p = {**self.loads_p, **other.loads_p}
        combined.gens_p = {**self.gens_p, **other.gens_p}
        return combined

    def __radd__(self, other):
        """Support other + action syntax."""
        if other == 0 or other is None:
            return self
        return other.__add__(self)
