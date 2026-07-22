# expert_op4grid_recommender/pypowsybl_backend/simulation_env.py
"""
Simulation Environment that wraps pypowsybl network with grid2op-like interface.

This is the main entry point for using the pypowsybl backend. It provides
a unified interface similar to grid2op's Environment class.
"""

import numpy as np
import json
from pathlib import Path
from typing import Optional, Dict, List, Union
import pypowsybl as pp
import pypowsybl.loadflow as lf

from .network_manager import NetworkManager
from .observation import PypowsyblObservation
from .action_space import ActionSpace


class SimulationEnvironment:
    """
    Grid2op-compatible simulation environment using pypowsybl.
    
    This class provides the main interface for:
    - Loading networks from XIIDM/CGMES files
    - Getting observations of the current state
    - Creating and simulating actions
    - Managing thermal limits
    
    Example usage:
        env = SimulationEnvironment("/path/to/network.xiidm")
        obs = env.get_obs()
        action = env.action_space({"set_line_status": [("Line1", -1)]})
        obs_simu, reward, done, info = obs.simulate(action)
    
    Attributes:
        network_manager: The underlying NetworkManager
        action_space: ActionSpace for creating actions
        name_line: Array of line names
        name_sub: Array of substation names
    """
    
    def __init__(self,
                 network_path: Optional[Union[str, Path]] = None,
                 network: Optional[pp.network.Network] = None,
                 thermal_limits_path: Optional[Union[str, Path]] = None,
                 thermal_limits: Optional[Dict[str, float]] = None,
                 lf_parameters: Optional[lf.Parameters] = None,
                 threshold_thermal_limit: float = 1.0):
        """
        Initialize the simulation environment.
        
        Args:
            network_path: Path to network file (XIIDM, CGMES, etc.)
            network: Pre-loaded pypowsybl network (alternative to path)
            thermal_limits_path: Path to JSON file with thermal limits
            thermal_limits: Dict mapping line_id -> limit (alternative to path)
            lf_parameters: Load flow parameters (uses defaults if None)
            threshold_thermal_limit: Multiplier for thermal limits (e.g., 0.95 for 95%)
        """
        # Initialize network manager
        self.network_manager = NetworkManager(
            network_path=network_path,
            network=network,
            lf_parameters=lf_parameters
        )
        
        # Initialize action space
        self.action_space = ActionSpace(self.network_manager)
        
        # Set up thermal limits
        self._threshold = threshold_thermal_limit
        self._thermal_limits = self._load_thermal_limits(
            thermal_limits_path, thermal_limits
        )
        
        # DC load-flow mode (mirrors network_manager._default_dc for external,
        # DC-aware callers). Always defined so the attribute exists before any
        # DC escalation sets it. get_env_first_obs_pypowsybl flips it to True
        # when the env is created in DC mode.
        self._use_dc = False

        # Transient-DC-escalation guard (issue #6). When an
        # overload-disconnection load flow diverges, the analysis pipeline calls
        # switch_to_dc_load_flow_pypowsybl, which escalates THIS env to DC for a
        # single analysis. This flag records that escalation so a subsequent
        # analysis that reuses the env can detect and undo it (see
        # reset_loadflow_mode_to_baseline) instead of silently inheriting DC mode
        # + a de-energised base variant.
        self._dc_escalation_pending = False

        # Run initial load flow to ensure valid state
        self._ensure_valid_state()

        # Create initial observation
        self._current_obs = None
    
    def _load_thermal_limits(self,
                             path: Optional[Union[str, Path]],
                             limits_dict: Optional[Dict[str, float]]) -> Dict[str, float]:
        """Load thermal limits from file or dict."""
        if limits_dict is not None:
            base_limits = limits_dict
        elif path is not None:
            with open(path, 'r') as f:
                base_limits = json.load(f)
        else:
            # Get from network
            base_limits = self.network_manager.get_thermal_limits()
        
        # Apply threshold
        return {k: v * self._threshold for k, v in base_limits.items()}
    
    def _ensure_valid_state(self):
        """Run load flow to ensure network is in a valid state.

        Uses `voltage_init_mode=DC_VALUES` explicitly because this LF is
        called both at construction time (no previous voltage state has
        ever been computed on this Network) and on `reset()` (the base
        variant has been restored, so "previous values" may be stale or
        invalid). Without this, pypowsybl defaults to `PREVIOUS_VALUES`
        which throws a "Voltage magnitude is undefined" exception and
        retries internally with DC_VALUES — wasted ~70 ms + spurious
        warning in the logs.

        Runs in ``fast=True`` mode (AFTER_GENERATOR tap-changer control):
        this LF only establishes a *converged* baseline state — the
        accuracy-critical per-action re-simulations run their own LFs
        afterwards — so the ~6-7x cheaper fast solve suffices (and
        after-generator keeps the same currents anyway). This matters for
        the parallel reassessment, where every worker builds its own
        SimulationEnvironment and pays this baseline LF once: the slow
        (incremental) default costs ~4.6 s each on the France grid vs ~0.7 s.
        """
        result = self.network_manager.run_load_flow(
            fast=True,
            voltage_init_mode=lf.VoltageInitMode.DC_VALUES,
        )
        if result is None or result.status != lf.ComponentStatus.CONVERGED:
            print(f"Warning: Initial load flow did not converge: {result}")
    
    def get_obs(self) -> PypowsyblObservation:
        """
        Get the current observation.
        
        Returns:
            PypowsyblObservation representing current network state
        """
        self._current_obs = PypowsyblObservation(
            self.network_manager,
            self.action_space,
            self._thermal_limits
        )
        return self._current_obs
    
    def reset(self) -> PypowsyblObservation:
        """
        Reset to base state and return observation.
        
        Returns:
            PypowsyblObservation of the base state
        """
        self.network_manager.reset_to_base()
        self._ensure_valid_state()
        return self.get_obs()

    def reset_loadflow_mode_to_baseline(self, dc: bool = False) -> None:
        """Restore this env's load-flow mode to ``dc`` and re-energise the base
        variant — the inverse of a transient ``switch_to_dc`` escalation.

        ``switch_to_dc_load_flow_pypowsybl`` escalates a (possibly reused) env to
        DC for a single analysis: it sets ``NetworkManager._default_dc = True``
        and DC-solves the base variant, which leaves every bus ``v_mag = NaN``
        because a DC load flow computes no voltage magnitudes. Since one env is
        shared across analyses, that escalation would otherwise persist and force
        every later analysis into DC — where branch currents are not populated,
        so ``rho ≈ 0`` and real overloads silently vanish (issue #6).

        Call this before reusing the env for a new analysis to put it back into
        the configured baseline mode with an AC-solved (energised) base variant,
        exactly as a freshly built env would be. Idempotent; its cost is a single
        baseline load flow (far cheaper than the ~4 s full env rebuild the bug
        otherwise forces). It clears :attr:`_dc_escalation_pending`.

        Args:
            dc: The baseline DC-mode to restore (normally ``config.USE_DC_LOAD_FLOW``).
        """
        self._use_dc = dc
        self.network_manager._default_dc = dc
        self.network_manager.reset_to_base()
        # Re-solve the base variant in the restored mode so downstream
        # observations read an energised grid (AC) instead of the de-energised
        # DC snapshot the escalation left behind.
        self._ensure_valid_state()
        self._dc_escalation_pending = False

    def set_thermal_limit(self, thermal_limits: Union[List[float], np.ndarray]):
        """
        Set thermal limits for all lines.
        
        Args:
            thermal_limits: Array of thermal limits in same order as name_line
        """
        # Hoist name_line to a local once (P4): even though it is now a cached
        # property, keeping a single reference documents the O(n) contract and
        # is robust to any future property-side regression.
        names = self.name_line
        for i, limit in enumerate(thermal_limits):
            self._thermal_limits[names[i]] = limit

    def get_thermal_limit(self) -> np.ndarray:
        """
        Get thermal limits as array.

        Returns:
            Array of thermal limits in same order as name_line
        """
        names = self.name_line
        return np.array([
            self._thermal_limits.get(ln, 9999.0)
            for ln in names
        ])
    
    # ========== Properties matching grid2op interface ==========
    
    @property
    def name_line(self) -> np.ndarray:
        """Array of line names."""
        return self.network_manager.name_line
    
    @property
    def name_sub(self) -> np.ndarray:
        """Array of substation names."""
        return self.network_manager.name_sub
    
    @property
    def name_gen(self) -> np.ndarray:
        """Array of generator names."""
        return self.network_manager.name_gen
    
    @property
    def name_load(self) -> np.ndarray:
        """Array of load names."""
        return self.network_manager.name_load
    
    @property
    def n_line(self) -> int:
        """Number of lines."""
        return self.network_manager.n_line
    
    @property
    def n_sub(self) -> int:
        """Number of substations."""
        return self.network_manager.n_sub
    
    @property
    def backend(self) -> 'BackendWrapper':
        """
        Wrapper providing access to the underlying network.
        
        This maintains compatibility with code that accesses env.backend._grid.network
        """
        return BackendWrapper(self.network_manager)
    
    # ========== Chronics handling (placeholder) ==========
    
    @property
    def chronics_handler(self) -> 'ChronicsHandlerPlaceholder':
        """
        Placeholder for chronics handling.
        
        For static analysis without time series, this returns a minimal interface.
        """
        return ChronicsHandlerPlaceholder()
    
    def get_line_info(self, line_name: str = None, line_id: int = None):
        """
        Get information about a line.
        
        Args:
            line_name: Line name to look up
            line_id: Line index to look up
            
        Returns:
            Tuple of (line_id, line_name, ...)
        """
        if line_name is not None:
            idx = np.where(self.name_line == line_name)[0]
            if len(idx) > 0:
                return (idx[0], line_name)
        elif line_id is not None:
            if 0 <= line_id < len(self.name_line):
                return (line_id, self.name_line[line_id])
        return (None, None)


class BackendWrapper:
    """Wrapper to provide env.backend._grid.network access pattern."""
    
    def __init__(self, network_manager: NetworkManager):
        self._network_manager = network_manager
        self._grid = GridWrapper(network_manager)
    
    @property
    def _grid(self):
        return self.__grid
    
    @_grid.setter
    def _grid(self, value):
        self.__grid = value


class GridWrapper:
    """Wrapper to provide backend._grid.network access."""
    
    def __init__(self, network_manager: NetworkManager):
        self._network_manager = network_manager
    
    @property
    def network(self):
        return self._network_manager.network


class ChronicsHandlerPlaceholder:
    """Placeholder for chronics handling in static analysis."""
    
    def __init__(self):
        self.real_data = RealDataPlaceholder()
    
    def get_name(self) -> str:
        return "static_analysis"


class RealDataPlaceholder:
    """Placeholder for real data in chronics."""
    
    def __init__(self):
        self.subpaths = []


def make_simulation_env(env_folder: Union[str, Path],
                        env_name: str,
                        thermal_limits_file: Optional[str] = None,
                        threshold_thermal_limit: float = 0.95) -> SimulationEnvironment:
    """
    Create a SimulationEnvironment from a folder structure.
    
    This function looks for network files in the standard locations
    and creates an environment ready for analysis.
    
    Args:
        env_folder: Base folder containing environments
        env_name: Name of the specific environment
        thermal_limits_file: Optional specific thermal limits file
        threshold_thermal_limit: Thermal limit multiplier
        
    Returns:
        Configured SimulationEnvironment
    """
    env_path = Path(env_folder) / env_name
    
    # Look for network file
    network_file = None
    
    # Check if env_path is already a valid network file
    if env_path.is_file() and env_path.suffix.lower() in ['.xiidm', '.iidm', '.xml']:
        network_file = env_path
        # If it's a file, we treat its parent as the environment folder for other files (e.g. thermal limits)
        env_path = env_path.parent
    
    if network_file is None:
        for ext in ['.xiidm', '.iidm', '.xml']:
            candidates = list(env_path.glob(f"*{ext}"))
            if candidates:
                network_file = candidates[0]
                break
    
    if network_file is None:
        # Try looking in grid/ subfolder
        grid_folder = env_path / "grid"
        if grid_folder.exists():
            for ext in ['.xiidm', '.iidm', '.xml']:
                candidates = list(grid_folder.glob(f"*{ext}"))
                if candidates:
                    network_file = candidates[0]
                    break
    
    if network_file is None:
        raise FileNotFoundError(f"No network file found in {env_path}")
    
    # Look for thermal limits
    thermal_limits_path = None
    if thermal_limits_file:
        thermal_limits_path = env_path / thermal_limits_file
    else:
        # Try default names
        for name in ['thermal_limits.json', 'limits.json']:
            candidate = env_path / name
            if candidate.exists():
                thermal_limits_path = candidate
                break
    
    return SimulationEnvironment(
        network_path=network_file,
        thermal_limits_path=thermal_limits_path,
        threshold_thermal_limit=threshold_thermal_limit
    )
