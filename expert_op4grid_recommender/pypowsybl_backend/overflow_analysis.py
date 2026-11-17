# expert_op4grid_recommender/pypowsybl_backend/overflow_analysis.py
"""
Overflow graph analysis using pure pypowsybl.

This module provides the overflow graph building functionality that was
previously dependent on alphaDeesp's Grid2opSimulation. It uses pypowsybl's
sensitivity analysis for PTDF-based flow change calculations.

The core idea:
- When a line disconnects, power redistributes according to Power Transfer 
  Distribution Factors (PTDFs)
- We can compute these factors using DC sensitivity analysis
- The overflow graph shows how flows change after disconnecting overloaded lines

Supports both AC and DC load flow for flexibility.
"""

import numpy as np
import pandas as pd
import networkx as nx
from typing import Dict, List, Tuple, Optional, Any, TYPE_CHECKING
import pypowsybl.loadflow as lf

if TYPE_CHECKING:
    from .network_manager import NetworkManager
    from .observation import PypowsyblObservation
    from .simulation_env import SimulationEnvironment


class OverflowSimulator:
    """
    Computes flow changes when lines are disconnected.
    
    Uses pypowsybl's load flow (AC or DC) to compute flow redistribution 
    after line outages.
    
        This replaces alphaDeesp's Grid2opSimulation for overflow graph building.
    
    Attributes:
        use_dc: If True, uses DC load flow. If False, uses AC load flow. On an
            observation solved in AC, the DC cut is applied as a flow transfer
            on the AC base flows (see ``_solve_after_cut``).
        fast_mode: If True, uses fast mode for load flow (tap-changer control in
            AFTER_GENERATOR_VOLTAGE_CONTROL mode — same currents, ~6-7x fewer iters).
    """
    
    def __init__(self, 
                 network_manager: 'NetworkManager',
                 obs: 'PypowsyblObservation',
                 use_dc: bool = False,
                 param_options: Optional[Dict] = None,
                 fast_mode: bool = True):
        """
        Initialize the overflow simulator.
        
        Args:
            network_manager: NetworkManager instance
            obs: Current observation (after initial contingency)
            use_dc: If True, use DC load flow. If False, use AC (default).
            param_options: Configuration parameters (thresholds, etc.)
            fast_mode: If True, uses fast mode for load flow.
        """
        self._nm = network_manager
        self._obs = obs
        self._net = network_manager.network
        self._use_dc = use_dc
        self._param_options = param_options or {}
        self._fast_mode = fast_mode

        # The base flows below come from the observation. When the graph runs
        # in DC on an AC observation (USE_DC_FOR_OVERFLOW_GRAPH alone), DC
        # post-cut flows cannot be compared to them: the difference would carry
        # the whole AC/DC gap (losses, slack distribution) on every branch. The
        # cut is then measured as a DC flow transfer, against a DC solve of the
        # same N-1 state, and applied on the AC base flows. An observation
        # already solved in DC (USE_DC_LOAD_FLOW, divergence fallback) needs none.
        self._dc_transfer = use_dc and not network_manager._default_dc

        # Get current line flows FROM THE OBSERVATION (not network)
        # This ensures we have the correct base flows even if the observation
        # was captured at a different network state
        # OPTIMIZATION: Store as arrays, create dict only when needed
        self._base_flows_arr = obs.p_or.copy()
        self._base_currents_arr = np.maximum(np.abs(obs.a_or), np.abs(obs.a_ex))

        # Create dicts for backward compatibility (used in compute_ptdf_for_line)
        line_names = self._nm.name_line
        self._base_flows = dict(zip(line_names, self._base_flows_arr))
        self._base_currents = dict(zip(line_names, self._base_currents_arr))

        # Per-branch origin-side nominal voltage (kV), aligned to name_line —
        # used to derive a current magnitude from the DC active-power flow when
        # the overflow graph runs in DC (currents are not populated by run_dc):
        #   |I| ≈ |P|·1000 / (√3·Vnom).  Computed once (cheap).
        self._vnom_or_arr = self._build_vnom_or_arr(line_names)

    def _build_vnom_or_arr(self, line_names) -> np.ndarray:
        """Origin-side nominal voltage (kV) per branch, aligned to ``name_line``."""
        vl_nom = self._net.get_voltage_levels()['nominal_v']
        lines_vl1 = self._nm._cached_lines_df['voltage_level1_id'].to_dict()
        trafos_vl1 = self._nm._cached_trafos_df['voltage_level1_id'].to_dict()
        out = np.full(len(line_names), np.nan)
        for i, lid in enumerate(line_names):
            vl_id = trafos_vl1.get(lid) if lid in self._nm._trafos_set else lines_vl1.get(lid)
            if vl_id is not None and vl_id in vl_nom.index:
                out[i] = float(vl_nom.loc[vl_id])
        return out

    def _dc_currents_from_power(self, p_arr: np.ndarray) -> np.ndarray:
        """Approximate current magnitude (A) from DC active power (MW):
        ``|I| = |P|·1000 / (√3·Vnom)`` (DC assumption Q≈0, V≈Vnom). Returns 0
        where Vnom is unknown. Lets the DC overflow graph produce a meaningful
        ``rho`` (fixes the DC ``rho=0`` behaviour for graph purposes)."""
        with np.errstate(divide='ignore', invalid='ignore'):
            i = np.abs(p_arr) * 1000.0 / (np.sqrt(3.0) * self._vnom_or_arr)
        return np.where(np.isfinite(i), i, 0.0)
    
    def _run_load_flow(self) -> lf.ComponentResult:
        """Run load flow with configured mode (AC or DC) and fast optimization."""
        return self._nm.run_load_flow(dc=self._use_dc, fast=self._fast_mode)

    def _solve_after_cut(self, already_disconnected: List[str],
                         lines_to_disconnect: List[str]) -> Tuple[bool, np.ndarray]:
        """Open the observation's open lines and the lines to cut on the working
        variant, solve it, and return ``(converged, p1 per branch)``.

        With a DC flow transfer (see ``__init__``), the N-1 state is first solved
        in DC, before the cut. The flows returned are then the AC base flows
        plus the change of the DC flows across the cut, and a branch left
        without flow after the cut reads 0.
        """
        n_lines = len(self._nm.name_line)
        to_cut = list(already_disconnected) + list(lines_to_disconnect)
        dc_base = None
        if self._dc_transfer:
            if already_disconnected:
                self._nm.disconnect_lines_batch(list(already_disconnected))
            base = self._run_load_flow()
            if base is None or base.status != lf.ComponentStatus.CONVERGED:
                return False, np.zeros(n_lines)
            dc_base = np.nan_to_num(self._nm.get_line_p1_array(nan_as_zero=False))
            to_cut = list(lines_to_disconnect)

        # Batch disconnect instead of a loop
        if to_cut:
            self._nm.disconnect_lines_batch(to_cut)
        result = self._run_load_flow()
        if result is None or result.status != lf.ComponentStatus.CONVERGED:
            return False, np.zeros(n_lines)
        if dc_base is None:
            return True, self._nm.get_line_p1_array()

        dc_cut = self._nm.get_line_p1_array(nan_as_zero=False)
        ac_base = np.nan_to_num(np.asarray(self._base_flows_arr, dtype=float))
        return True, np.where(np.isnan(dc_cut), 0.0, ac_base + dc_cut - dc_base)

    def _get_line_flows(self) -> Dict[str, float]:
        """Get active power flows for all lines (MW at terminal 1)."""
        lines_df = self._net.get_lines()[['p1']]
        trafos_df = self._net.get_2_windings_transformers()[['p1']]
        
        flows = {}
        for line_id in lines_df.index:
            p1 = lines_df.loc[line_id, 'p1']
            flows[line_id] = p1 if not np.isnan(p1) else 0.0
        for trafo_id in trafos_df.index:
            p1 = trafos_df.loc[trafo_id, 'p1']
            flows[trafo_id] = p1 if not np.isnan(p1) else 0.0
        
        return flows
    
    def _get_line_currents(self) -> Dict[str, float]:
        """Get current magnitudes for all lines (A, max of both terminals)."""
        lines_df = self._net.get_lines()[['i1', 'i2']]
        trafos_df = self._net.get_2_windings_transformers()[['i1', 'i2']]
        
        currents = {}
        for line_id in lines_df.index:
            i1 = lines_df.loc[line_id, 'i1']
            i2 = lines_df.loc[line_id, 'i2']
            i1 = i1 if not np.isnan(i1) else 0.0
            i2 = i2 if not np.isnan(i2) else 0.0
            currents[line_id] = max(abs(i1), abs(i2))
        for trafo_id in trafos_df.index:
            i1 = trafos_df.loc[trafo_id, 'i1']
            i2 = trafos_df.loc[trafo_id, 'i2']
            i1 = i1 if not np.isnan(i1) else 0.0
            i2 = i2 if not np.isnan(i2) else 0.0
            currents[trafo_id] = max(abs(i1), abs(i2))
        
        return currents
    
    def _get_line_reactive_flows(self) -> Dict[str, float]:
        """Get reactive power flows for all lines (MVAr at terminal 1)."""
        lines_df = self._net.get_lines()[['q1']]
        trafos_df = self._net.get_2_windings_transformers()[['q1']]
        
        flows = {}
        for line_id in lines_df.index:
            q1 = lines_df.loc[line_id, 'q1']
            flows[line_id] = q1 if not np.isnan(q1) else 0.0
        for trafo_id in trafos_df.index:
            q1 = trafos_df.loc[trafo_id, 'q1']
            flows[trafo_id] = q1 if not np.isnan(q1) else 0.0
        
        return flows
    
    def compute_ptdf_for_line(self, line_id: str) -> Dict[str, float]:
        """
        Compute PTDF row for disconnecting a specific line.
        
        This shows how much each other line's flow changes when
        we disconnect the given line (per unit of the disconnected flow).
        
        Args:
            line_id: ID of line to disconnect
            
        Returns:
            Dictionary mapping other_line_id -> PTDF factor
        """
        original_flow = self._base_flows.get(line_id, 0.0)
        if abs(original_flow) < 1e-6:
            return {}
        
        # Create a variant for this calculation
        variant_id = f"ptdf_{line_id}"
        self._nm.create_variant(variant_id)
        self._nm.set_working_variant(variant_id)
        
        try:
            # Disconnect the line
            self._nm.disconnect_line(line_id)
            
            # Run load flow (AC or DC based on configuration)
            result = self._run_load_flow()
            
            if result is None or result.status != lf.ComponentStatus.CONVERGED:
                return {}
            
            # Get new flows
            new_flows = self._get_line_flows()
            
            # Compute PTDFs
            ptdf = {}
            for other_line, new_flow in new_flows.items():
                if other_line != line_id:
                    old_flow = self._base_flows.get(other_line, 0.0)
                    delta = new_flow - old_flow
                    ptdf[other_line] = delta / abs(original_flow) if abs(original_flow) > 1e-6 else 0.0
            
            return ptdf
            
        finally:
            self._nm.set_working_variant(self._nm.base_variant_id)
            self._nm.remove_variant(variant_id)
    
    def compute_flow_changes_and_rho(self,
                                      lines_to_disconnect: List[str],
                                      thermal_limits: Dict[str, float]
                                      ) -> Tuple[pd.DataFrame, np.ndarray]:
        """
        Compute flow changes AND rho values in a single load flow pass.

        This method combines what was previously done in two separate methods:
        - compute_flow_changes_after_disconnection (for DataFrame)
        - _create_obs_linecut (for rho values)

        By doing both in one pass, we avoid running load flow twice.

        Args:
            lines_to_disconnect: List of line IDs to disconnect (overloaded lines)
            thermal_limits: Dict mapping line_id to thermal limit for rho computation

        Returns:
            Tuple of (flow_changes_df, rho_array)
        """
        # Create variant
        variant_id = "flow_change_and_rho_analysis"
        self._nm.create_variant(variant_id)
        self._nm.set_working_variant(variant_id)

        try:
            # Re-apply any lines that are already disconnected in the observation
            line_names = self._nm.name_line
            line_status = self._obs.line_status
            disconnected_mask = ~line_status
            n_lines = len(line_names)

            # Collect all lines to disconnect
            disconnected_indices = np.where(disconnected_mask)[0]
            already_disconnected = [line_names[i] for i in disconnected_indices]

            # Solve the post-cut state ONCE for both DataFrame and rho computation
            converged, new_flows_arr = self._solve_after_cut(already_disconnected,
                                                             lines_to_disconnect)

            # ===== Extract data for BOTH outputs from this single solve =====

            if converged:
                # Get currents for rho computation
                i1_arr, i2_arr = self._nm.get_line_currents_array()
            else:
                i1_arr = np.zeros(n_lines)
                i2_arr = np.zeros(n_lines)

            # ===== BUILD DATAFRAME (same logic as before) =====
            idx_or = self._nm._cached_line_or_subid.copy()
            idx_ex = self._nm._cached_line_ex_subid.copy()

            init_flows = self._base_flows_arr.copy()
            init_flows = np.where(np.isnan(init_flows), 0.0, init_flows)

            # branch_direction_swaps - vectorized
            swap_mask = (init_flows < 0) & (init_flows != 0.0)
            idx_or_swapped = np.where(swap_mask, idx_ex, idx_or)
            idx_ex_swapped = np.where(swap_mask, idx_or, idx_ex)
            idx_or = idx_or_swapped
            idx_ex = idx_ex_swapped
            init_flows_abs = np.where(swap_mask, np.abs(init_flows), init_flows)

            # Apply swap correction to new_flows
            new_flows = np.where(swap_mask, -new_flows_arr, new_flows_arr)
            new_flows = np.where(np.isnan(new_flows), 0.0, new_flows)

            # Compute new_flows_swapped
            new_flows_swapped = (new_flows < 0) & (np.abs(new_flows) > np.abs(init_flows_abs))

            # Compute delta_flows
            abs_new = np.abs(new_flows)
            abs_init = np.abs(init_flows_abs)
            delta_flows = abs_new - abs_init

            # Case 1: Flow reversed and stronger
            case1_mask = new_flows_swapped
            delta_flows = np.where(case1_mask, abs_new + abs_init, delta_flows)
            idx_or = np.where(case1_mask, idx_ex, idx_or)
            idx_ex_temp = np.where(case1_mask, idx_or_swapped, idx_ex)
            idx_ex = idx_ex_temp

            # Case 2: Signs differ but new flow is weaker
            sign_new = np.sign(new_flows)
            sign_init = np.sign(init_flows_abs)
            case2_mask = (sign_new != sign_init) & (new_flows != 0) & (init_flows_abs != 0) & ~case1_mask
            delta_flows = np.where(case2_mask, -(abs_new + abs_init), delta_flows)

            # Compute gray_edges
            if lines_to_disconnect:
                first_ltc_idx = self._nm.get_line_idx(lines_to_disconnect[0])
                if first_ltc_idx >= 0:
                    ltc_report = np.abs(delta_flows[first_ltc_idx])
                else:
                    ltc_report = np.abs(delta_flows).max()
            else:
                ltc_report = np.abs(delta_flows).max()

            threshold = self._param_options.get("ThresholdReportOfLine", 0.05)
            max_overload = ltc_report * float(threshold)
            gray_edges = np.abs(delta_flows) < max_overload

            df = pd.DataFrame({
                'idx_or': idx_or,
                'idx_ex': idx_ex,
                'init_flows': init_flows_abs,
                'line_name': line_names,
                'swapped': swap_mask,
                'new_flows': new_flows,
                'new_flows_swapped': new_flows_swapped,
                'delta_flows': delta_flows,
                'gray_edges': gray_edges,
            })

            # ===== COMPUTE RHO (same logic as _create_obs_linecut) =====
            from expert_op4grid_recommender.config import MAX_RHO_BOTH_EXTREMITIES

            # In DC the load flow does not populate branch currents (i1/i2 = 0),
            # so derive them from the post-cut active-power flow (DC, or AC base
            # + DC transfer) — otherwise the overflow-graph rho collapses to 0
            # and no line reads as overloaded.
            if self._use_dc:
                i_dc = self._dc_currents_from_power(new_flows_arr)
                i1_arr = i_dc
                i2_arr = i_dc

            i1_arr = np.abs(i1_arr)
            i2_arr = np.abs(i2_arr)

            # Get thermal limits as arrays
            limit_or_list = []
            limit_ex_list = []
            for lid in line_names:
                limit = thermal_limits.get(lid, 9999.0)
                if isinstance(limit, (tuple, list)):
                    limit_or_list.append(limit[0])
                    limit_ex_list.append(limit[1])
                else:
                    limit_or_list.append(limit)
                    limit_ex_list.append(limit)
            thermal_or_arr = np.array(limit_or_list)
            thermal_ex_arr = np.array(limit_ex_list)

            if MAX_RHO_BOTH_EXTREMITIES:
                rho_or = np.where(thermal_or_arr > 0, i1_arr / thermal_or_arr, 0.0)
                rho_ex = np.where(thermal_ex_arr > 0, i2_arr / thermal_ex_arr, 0.0)
                rho = np.maximum(rho_or, rho_ex)
            else:
                # Create trafo mask for using i1 vs max(i1, i2)
                trafo_mask = np.array([lid in self._nm._trafos_set for lid in line_names])
    
                # Compute i_for_rho: use i1 for transformers, max(i1, i2) for lines
                i_for_rho = np.where(trafo_mask, i1_arr, np.maximum(i1_arr, i2_arr))
                # Compute rho (avoid division by zero)
                rho = np.where(thermal_or_arr > 0, i_for_rho / thermal_or_arr, 0.0)

            return df, rho

        finally:
            self._nm.set_working_variant(self._nm.base_variant_id)
            self._nm.remove_variant(variant_id)

    def compute_flow_changes_after_disconnection(self,
                                                   lines_to_disconnect: List[str]
                                                   ) -> pd.DataFrame:
        """
        Compute flow changes when specified lines are disconnected.

        This method replicates alphaDeesp's Simulation.create_df() logic exactly.
        OPTIMIZED: Uses vectorized numpy operations instead of row-by-row iteration.

        Args:
            lines_to_disconnect: List of line IDs to disconnect

        Returns:
            DataFrame with columns matching alphaDeesp's Grid2opSimulation format
        """
        # Create variant
        variant_id = "flow_change_analysis"
        self._nm.create_variant(variant_id)
        self._nm.set_working_variant(variant_id)

        try:
            # IMPORTANT: First, re-apply any lines that are already disconnected in the observation
            # The observation was created from a simulated state (with contingency applied),
            # but the network manager's base variant doesn't have those disconnections.
            line_names = self._nm.name_line
            line_status = self._obs.line_status
            disconnected_mask = ~line_status

            disconnected_indices = np.where(disconnected_mask)[0]
            already_disconnected = [line_names[i] for i in disconnected_indices]

            # Get new flows (signed values, like alphaDeesp's cut_lines_and_recomputes_flows)
            _converged, new_flows_arr = self._solve_after_cut(already_disconnected,
                                                              lines_to_disconnect)

            # ===== STEP 1: Build initial arrays (vectorized) =====
            # Use pre-cached indices from NetworkManager
            idx_or = self._nm._cached_line_or_subid.copy()
            idx_ex = self._nm._cached_line_ex_subid.copy()

            # OPTIMIZATION: Use pre-computed array directly
            init_flows = self._base_flows_arr.copy()
            init_flows = np.where(np.isnan(init_flows), 0.0, init_flows)

            # ===== STEP 2: branch_direction_swaps - vectorized =====
            # Swap when init_flows < 0
            swap_mask = (init_flows < 0) & (init_flows != 0.0)

            # Swap idx_or and idx_ex where needed
            idx_or_swapped = np.where(swap_mask, idx_ex, idx_or)
            idx_ex_swapped = np.where(swap_mask, idx_or, idx_ex)
            idx_or = idx_or_swapped
            idx_ex = idx_ex_swapped

            # Make init_flows absolute where swapped
            init_flows_abs = np.where(swap_mask, np.abs(init_flows), init_flows)

            # ===== STEP 3: Get new_flows and apply swapped correction - vectorized =====
            new_flows = np.where(swap_mask, -new_flows_arr, new_flows_arr)
            new_flows = np.where(np.isnan(new_flows), 0.0, new_flows)

            # ===== STEP 4: Compute new_flows_swapped - vectorized =====
            # True if new_flows < 0 AND |new_flows| > |init_flows|
            new_flows_swapped = (new_flows < 0) & (np.abs(new_flows) > np.abs(init_flows_abs))

            # ===== STEP 5: Compute delta_flows - vectorized =====
            abs_new = np.abs(new_flows)
            abs_init = np.abs(init_flows_abs)

            # Default: same direction - simple difference
            delta_flows = abs_new - abs_init

            # Case 1: Flow reversed and stronger (new_flows_swapped)
            case1_mask = new_flows_swapped
            delta_flows = np.where(case1_mask, abs_new + abs_init, delta_flows)

            # When new_flows_swapped, swap idx_or/idx_ex
            idx_or = np.where(case1_mask, idx_ex, idx_or)
            idx_ex_temp = np.where(case1_mask, idx_or_swapped, idx_ex)
            idx_ex = idx_ex_temp

            # Case 2: Signs differ but new flow is weaker - negative delta
            sign_new = np.sign(new_flows)
            sign_init = np.sign(init_flows_abs)
            case2_mask = (sign_new != sign_init) & (new_flows != 0) & (init_flows_abs != 0) & ~case1_mask
            delta_flows = np.where(case2_mask, -(abs_new + abs_init), delta_flows)

            # ===== STEP 6: Compute gray_edges - vectorized =====
            if lines_to_disconnect:
                first_ltc_idx = self._nm.get_line_idx(lines_to_disconnect[0])
                if first_ltc_idx >= 0:
                    ltc_report = np.abs(delta_flows[first_ltc_idx])
                else:
                    ltc_report = np.abs(delta_flows).max()
            else:
                ltc_report = np.abs(delta_flows).max()

            threshold = self._param_options.get("ThresholdReportOfLine", 0.05)
            max_overload = ltc_report * float(threshold)
            gray_edges = np.abs(delta_flows) < max_overload

            # Build DataFrame
            df = pd.DataFrame({
                'idx_or': idx_or,
                'idx_ex': idx_ex,
                'init_flows': init_flows_abs,
                'line_name': line_names,
                'swapped': swap_mask,
                'new_flows': new_flows,
                'new_flows_swapped': new_flows_swapped,
                'delta_flows': delta_flows,
                'gray_edges': gray_edges,
            })

            return df

        finally:
            self._nm.set_working_variant(self._nm.base_variant_id)
            self._nm.remove_variant(variant_id)
    
    def get_dataframe(self) -> pd.DataFrame:
        """
        Get base flow information as DataFrame.

        Provides interface compatible with alphaDeesp's Grid2opSimulation.
        OPTIMIZED: Uses pre-cached indices and arrays.

        Returns:
            DataFrame with flow information for all lines in alphaDeesp format
        """
        line_names = self._nm.name_line
        n_lines = len(line_names)

        # Use pre-cached indices from NetworkManager
        idx_or = self._nm._cached_line_or_subid.copy()
        idx_ex = self._nm._cached_line_ex_subid.copy()

        # OPTIMIZATION: Use pre-computed array directly
        flows = self._base_flows_arr.copy()

        # Determine swapped (negative flow = from ex to or)
        swapped = flows < 0

        return pd.DataFrame({
            'idx_or': idx_or,
            'idx_ex': idx_ex,
            'init_flows': np.abs(flows),
            'swapped': swapped,
            'new_flows': np.abs(flows),  # Same as init when no disconnection
            'new_flows_swapped': np.zeros(n_lines, dtype=bool),
            'delta_flows': np.zeros(n_lines),
            'gray_edges': np.zeros(n_lines, dtype=bool),
            'line_name': line_names,
        })


class OverflowGraphBuilder:
    """
    Builds overflow graphs compatible with alphaDeesp's OverFlowGraph format.
    
    The overflow graph is a directed graph where:
    - Nodes are substations
    - Edges are power lines with flow values
    - Edge weights represent flow changes after disconnecting overloaded lines
    """
    
    def __init__(self,
                 overflow_simulator: OverflowSimulator,
                 overloaded_line_ids: List[int],
                 param_options: Optional[Dict] = None):
        """
        Initialize the graph builder.
        
        Args:
            overflow_simulator: OverflowSimulator instance
            overloaded_line_ids: List of indices of overloaded lines
            param_options: Configuration parameters (thresholds, etc.)
        """
        self._sim = overflow_simulator
        self._overloaded_ids = overloaded_line_ids
        self._nm = overflow_simulator._nm
        
        self._params = param_options or {
            "ThresholdReportOfLine": 0.05,
            "ThersholdMinPowerOfLoop": 0.1,
            "ratioToKeepLoop": 0.25,
            "ratioToReconsiderFlowDirection": 0.75,
            "maxUnusedLines": 3,
        }
    
    def build_graph(self) -> Tuple[nx.MultiDiGraph, pd.DataFrame]:
        """
        Build the overflow graph.
        OPTIMIZED: Uses vectorized filtering instead of iterrows().

        Returns:
            Tuple of (graph, flow_changes_df)
        """
        # Get overloaded line names
        line_names = list(self._nm.name_line)
        overloaded_line_names = [line_names[i] for i in self._overloaded_ids]

        # Compute flow changes
        df = self._sim.compute_flow_changes_after_disconnection(overloaded_line_names)

        # Build graph
        G = nx.MultiDiGraph()

        # Add nodes (substations)
        for i, sub_name in enumerate(self._nm.name_sub):
            G.add_node(i, name=sub_name)

        if df.empty:
            return G, df

        # OPTIMIZATION: Vectorized filtering instead of iterrows()
        threshold = self._params.get("ThresholdReportOfLine", 0.05)
        max_delta = df['delta_flows'].abs().max()

        # Create mask for valid edges
        delta_arr = df['delta_flows'].values
        gray_arr = df['gray_edges'].values
        idx_or_arr = df['idx_or'].values
        idx_ex_arr = df['idx_ex'].values
        init_flows_arr = df['init_flows'].values
        new_flows_arr = df['new_flows'].values
        line_name_arr = df['line_name'].values

        valid_mask = (
            ~gray_arr &  # Not gray (not disconnected)
            ~np.isnan(delta_arr) &  # Not NaN
            (np.abs(delta_arr) >= threshold * max_delta) &  # Significant
            (idx_or_arr >= 0) &
            (idx_ex_arr >= 0)
        )

        # Get indices of valid rows
        valid_indices = np.where(valid_mask)[0]

        # Add edges for valid rows
        for i in valid_indices:
            delta = delta_arr[i]
            idx_or = int(idx_or_arr[i])
            idx_ex = int(idx_ex_arr[i])

            # Determine edge direction based on flow direction
            if delta >= 0:
                u, v = idx_or, idx_ex
            else:
                u, v = idx_ex, idx_or
                delta = -delta

            # Add edge with attributes
            G.add_edge(u, v,
                       name=line_name_arr[i],
                       capacity=delta,
                       label=f"{delta:.0f}",
                       flow_before=init_flows_arr[i],
                       flow_after=new_flows_arr[i])

        return G, df
    
    def get_topology(self) -> Dict[str, Any]:
        """
        Get topology information for alphaDeesp compatibility.
        
        Returns:
            Dictionary with topology data
        """
        return {
            'n_sub': self._nm.n_sub,
            'n_line': self._nm.n_line,
            'line_or_to_subid': self._nm.get_line_or_subid(),
            'line_ex_to_subid': self._nm.get_line_ex_subid(),
            'name_sub': self._nm.name_sub,
            'name_line': self._nm.name_line,
        }


def build_overflow_graph_pypowsybl(env: 'SimulationEnvironment',
                                    obs: 'PypowsyblObservation',
                                    overloaded_line_ids: List[int],
                                    non_connected_reconnectable_lines: List[str],
                                    lines_non_reconnectable: List[str],
                                    timestep: int,
                                    do_consolidate_graph: bool = True,
                                    use_dc: bool = False,
                                    param_options: Optional[Dict] = None,
                                    extra_lines_to_cut_ids: Optional[List[int]] = None):
    """
    Build overflow graph using pure pypowsybl.
    
    This is a drop-in replacement for the grid2op-based build_overflow_graph function.
    Uses alphaDeesp's OverFlowGraph and Structured_Overload_Distribution_Graph for 
    proper graph construction with color attributes.
    
    Args:
        env: SimulationEnvironment instance
        obs: Current observation (after contingency)
        overloaded_line_ids: List of indices of overloaded lines
        non_connected_reconnectable_lines: Lines that could be reconnected
        lines_non_reconnectable: Lines that cannot be reconnected
        timestep: Current timestep (for compatibility)
        do_consolidate_graph: Whether to consolidate the graph
        use_dc: If True, use DC load flow. If False, use AC load flow.
        param_options: Configuration parameters
        
    Returns:
        Tuple containing:
        - df_of_g: DataFrame with flow changes
        - overflow_sim: AlphaDeespAdapter instance (compatible with Grid2opSimulation)
        - g_overflow: OverFlowGraph instance
        - hubs: List of hub nodes
        - g_distribution_graph: Structured_Overload_Distribution_Graph
        - node_name_mapping: Dict mapping node indices to names
    """
    from expert_op4grid_recommender.config import PARAM_OPTIONS_EXPERT_OP
    from alphaDeesp.core.graphsAndPaths import OverFlowGraph, Structured_Overload_Distribution_Graph
    
    params = param_options or PARAM_OPTIONS_EXPERT_OP
    # Keep the fast_mode extraction flexible
    fast_mode = params.get("fast_mode", True) if isinstance(params, dict) else True

    # Operator-supplied extras: cut in the simulation alongside the
    # detected overloads but tagged ``is_extra_cut`` in the overflow
    # graph so the visualization keeps them out of the Overloads layer.
    extras = list(extra_lines_to_cut_ids or [])
    seen = set(overloaded_line_ids)
    extras = [idx for idx in extras if idx not in seen]
    full_ltc = list(overloaded_line_ids) + extras

    # Create alphaDeesp-compatible adapter (replaces Grid2opSimulation)
    overflow_sim = AlphaDeespAdapter(
        obs=obs,
        action_space=env.action_space,
        observation_space=None,  # Not needed for our use case
        param_options=params,
        debug=False,
        ltc=full_ltc,
        plot=False,
        simu_step=timestep,
        use_dc=use_dc,
        fast_mode=fast_mode
    )

    # Get flow changes DataFrame
    df_of_g = overflow_sim.get_dataframe()
    df_of_g["line_name"] = obs.name_line

    # Apply flow swap correction - this negates delta_flows for lines where
    # new_flows_swapped=True, making them negative (blue) instead of positive (red)
    df_of_g = _inhibit_swapped_flows(df_of_g)

    # Build topology info for alphaDeesp
    topo = overflow_sim.topo

    # Use alphaDeesp's OverFlowGraph to construct graph with proper color attributes
    g_overflow = OverFlowGraph(topo, full_ltc, df_of_g, float_precision="%.0f",
                               extra_lines_to_cut=extras)
    
    # Node name mapping
    node_name_mapping = {i: name for i, name in enumerate(obs.name_sub)}
    
    # Rename nodes from indices to substation names
    g_overflow.g = nx.relabel_nodes(g_overflow.g, node_name_mapping, copy=True)
    
    # Create distribution graph for path analysis
    g_distribution_graph = Structured_Overload_Distribution_Graph(g_overflow.g)
    c_path_init = g_distribution_graph.constrained_path.full_n_constrained_path()
    
    # Consolidate graph if requested
    if len(g_distribution_graph.g_only_red_components.nodes) != 0 and do_consolidate_graph:
        g_overflow.consolidate_graph(
            g_distribution_graph,
            non_connected_lines_to_ignore=non_connected_reconnectable_lines + lines_non_reconnectable,
            no_desambiguation=True
        )
        g_distribution_graph = Structured_Overload_Distribution_Graph(g_overflow.g)
    
    # Get hubs before adding reconnectable lines
    real_hubs = g_distribution_graph.get_hubs()
    
    # Add relevant null flow lines for reconnection analysis
    if len(non_connected_reconnectable_lines) >= 0:
        g_overflow.add_relevant_null_flow_lines_all_paths(
            g_distribution_graph,
            non_connected_lines=non_connected_reconnectable_lines,
            non_reconnectable_lines=lines_non_reconnectable
        )
        g_overflow.keep_overloads_components()  # remove unecessary components
        # Recreate distribution graph after adding lines
        # Note: possible_hubs parameter may not be supported in all alphaDeesp versions
        try:
            g_distribution_graph = Structured_Overload_Distribution_Graph(
                g_overflow.g, 
                possible_hubs=c_path_init
            )
        except TypeError:
            # Fallback for older alphaDeesp versions without possible_hubs support
            g_distribution_graph = Structured_Overload_Distribution_Graph(g_overflow.g)
    
    return df_of_g, overflow_sim, g_overflow, real_hubs, g_distribution_graph, node_name_mapping

def _inhibit_swapped_flows(df_of_g: pd.DataFrame) -> pd.DataFrame:
    """
    Correct flow direction swapping in the DataFrame.
    
    When flows are swapped, the delta_flows sign and idx_or/idx_ex need to be corrected.
    
    Args:
        df_of_g: DataFrame with flow changes
        
    Returns:
        Corrected DataFrame
    """
    if 'new_flows_swapped' not in df_of_g.columns:
        return df_of_g
    
    swapped_mask = df_of_g.new_flows_swapped
    if not swapped_mask.any():
        return df_of_g
    
    # Negate delta_flows for swapped lines
    df_of_g.loc[swapped_mask, "delta_flows"] = -df_of_g.loc[swapped_mask, "delta_flows"]
    
    # Swap idx_or and idx_ex
    idx_or = df_of_g.loc[swapped_mask, "idx_or"].copy()
    df_of_g.loc[swapped_mask, "idx_or"] = df_of_g.loc[swapped_mask, "idx_ex"]
    df_of_g.loc[swapped_mask, "idx_ex"] = idx_or
    
    return df_of_g



def _find_hubs_simple(graph: nx.MultiDiGraph) -> List[int]:
    """
    Simple hub detection based on node degree.
    
    Hubs are nodes with high connectivity (degree).
    
    Args:
        graph: NetworkX graph
        
    Returns:
        List of hub node indices
    """
    if len(graph.nodes()) == 0:
        return []
    
    degrees = dict(graph.degree())
    if not degrees:
        return []
    
    avg_degree = np.mean(list(degrees.values()))
    std_degree = np.std(list(degrees.values()))
    
    threshold = avg_degree + std_degree
    hubs = [node for node, degree in degrees.items() if degree > threshold]
    
    return hubs


class _ObsLineCut:
    """Simple wrapper class to hold rho values for obs_linecut."""
    def __init__(self, rho_values: np.ndarray):
        self.rho = rho_values


class AlphaDeespAdapter:
    """
    Adapter to make pypowsybl simulation compatible with alphaDeesp.
    
    This class wraps the pypowsybl-based simulation to provide the interface
    expected by alphaDeesp's Grid2opSimulation.
    """
    
    def __init__(self,
                 obs: 'PypowsyblObservation',
                 action_space: Any,
                 observation_space: Any,
                 param_options: Dict,
                 debug: bool = False,
                 ltc: List[int] = None,
                 plot: bool = False,
                 simu_step: int = 0,
                 use_dc: bool = False,
                 fast_mode: bool = True):
        """
        Initialize adapter with alphaDeesp-compatible interface.
        
        Args:
            obs: PypowsyblObservation instance
            action_space: ActionSpace instance  
            observation_space: Observation space (for compatibility)
            param_options: alphaDeesp parameters
            debug: Enable debug output
            ltc: List of overloaded line indices (Lines To Consider)
            plot: Enable plotting
            simu_step: Simulation timestep
            use_dc: Use DC load flow
        """
        self._obs = obs
        self._action_space = action_space
        self._params = param_options
        self._ltc = ltc or []
        self._use_dc = use_dc
        
        # Store ltc for visualization
        self.ltc = ltc or []
        
        # Store param_options for create_df compatibility
        self.param_options = param_options
        
        # Create overflow simulator
        self._overflow_sim = OverflowSimulator(
            network_manager=obs._network_manager,
            obs=obs,
            use_dc=use_dc,
            param_options=param_options,
            fast_mode=fast_mode
        )
        
        # Build topology info
        self.topo = self._build_topo()
        
        # Compute flow changes for overloaded lines
        if ltc:
            line_names = [obs.name_line[i] for i in ltc]
            # OPTIMIZATION: Compute both DataFrame and rho in a single load flow pass
            self._df, rho_values = self._overflow_sim.compute_flow_changes_and_rho(
                line_names,
                obs._thermal_limits
            )
            # Create obs_linecut wrapper with the computed rho values
            self.obs_linecut = _ObsLineCut(rho_values)
        else:
            self._df = self._overflow_sim.get_dataframe()
            self.obs_linecut = obs  # No lines cut, same as original
    
    def _build_topo(self) -> Dict:
        """
        Build topology structure for alphaDeesp.

        This must match the structure expected by OverFlowGraph:
        - 'edges': dict with 'idx_or' and 'idx_ex' arrays
        - 'nodes': dict with node info including 'are_prods', 'are_loads', 'names',
                   'prods_values', 'loads_values'

        OPTIMIZED: Uses pre-cached data from NetworkManager - no DataFrame calls.
        """
        nm = self._obs._network_manager

        # Get substation indices for each line (already cached in NetworkManager)
        idx_or = nm.get_line_or_subid()
        idx_ex = nm.get_line_ex_subid()

        # Initialize arrays for substations
        n_sub = nm.n_sub
        are_prods = np.zeros(n_sub, dtype=bool)
        are_loads = np.zeros(n_sub, dtype=bool)
        prods_values = np.zeros(n_sub)
        loads_values = np.zeros(n_sub)

        # OPTIMIZATION: Use pre-cached per-substation element lists
        # Mark substations that have generators
        for sub_idx in range(n_sub):
            gen_indices = nm._gens_per_sub[sub_idx]
            if gen_indices:
                are_prods[sub_idx] = True
                prods_values[sub_idx] = sum(nm._gen_p_values[i] for i in gen_indices)

        # Mark substations that have loads
        for sub_idx in range(n_sub):
            load_indices = nm._loads_per_sub[sub_idx]
            if load_indices:
                are_loads[sub_idx] = True
                loads_values[sub_idx] = sum(nm._load_p_values[i] for i in load_indices)

        return {
            'edges': {
                'idx_or': list(idx_or),
                'idx_ex': list(idx_ex),
            },
            'nodes': {
                'are_prods': are_prods.tolist(),
                'are_loads': are_loads.tolist(),
                'prods_values': prods_values.tolist(),
                'loads_values': loads_values.tolist(),
                'names': list(nm.name_sub),
            }
        }
    
    def get_dataframe(self) -> pd.DataFrame:
        """Get flow change DataFrame (alphaDeesp interface)."""
        return self._df.copy()

    def get_substation_elements(self) -> Dict:
        """
        Get elements per substation (alphaDeesp interface).
        OPTIMIZED: Uses fully pre-cached data from NetworkManager - no DataFrame calls.

        Returns dict mapping substation index to dict of element types.
        """
        nm = self._obs._network_manager

        result = {i: {'loads': [], 'generators': [], 'lines_or': [], 'lines_ex': []}
                  for i in range(nm.n_sub)}

        # Use pre-cached element lists from NetworkManager
        load_ids = nm._load_ids
        gen_ids = nm._gen_ids
        line_names = nm._line_ids

        for sub_idx in range(nm.n_sub):
            # Map load indices to load IDs
            result[sub_idx]['loads'] = [load_ids[i] for i in nm._loads_per_sub[sub_idx]]
            # Map generator indices to generator IDs
            result[sub_idx]['generators'] = [gen_ids[i] for i in nm._gens_per_sub[sub_idx]]
            # Map line indices to line names for origin connections
            result[sub_idx]['lines_or'] = [line_names[i] for i in nm._lines_or_per_sub[sub_idx]]
            # Map line indices to line names for extremity connections
            result[sub_idx]['lines_ex'] = [line_names[i] for i in nm._lines_ex_per_sub[sub_idx]]

        return result
    
    def get_substation_to_node_mapping(self) -> Dict[int, int]:
        """Get mapping from substation index to node index (alphaDeesp interface)."""
        # In our case, substations are nodes, so it's identity mapping
        nm = self._obs._network_manager
        return {i: i for i in range(nm.n_sub)}
    
    def get_internal_to_external_mapping(self) -> Dict[int, str]:
        """Get mapping from internal node index to external name (alphaDeesp interface)."""
        nm = self._obs._network_manager
        return {i: name for i, name in enumerate(nm.name_sub)}
