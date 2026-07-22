# expert_op4grid_recommender/environment_pypowsybl.py
"""
Environment setup using pure pypowsybl backend.

This is the migrated version of environment.py that works without grid2op.
Provides the same interface for easy migration.
"""

import os
import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Union

import numpy as np
import pypowsybl as pp

from expert_op4grid_recommender.pypowsybl_backend import (
    SimulationEnvironment,
    PypowsyblObservation
)
from expert_op4grid_recommender import config
from expert_op4grid_recommender.exceptions import LoadFlowDivergedError
from expert_op4grid_recommender.data_loader import load_interesting_lines, DELETED_LINE_NAME, load_actions


def get_env_first_obs_pypowsybl(env_folder: Union[str, Path],
                                  env_name: str,
                                  thermal_limits_file: Optional[str] = None,
                                  is_DC: bool = False,
                                  threshold_thermal_limit: float = 0.95,
                                  network: Optional[pp.network.Network] = None,
                                  skip_initial_obs: bool = False
                                  ) -> Tuple[SimulationEnvironment, Optional[PypowsyblObservation], str]:
    """
    Creates a pypowsybl-based simulation environment and retrieves the initial observation.

    This function initializes a SimulationEnvironment using pypowsybl directly,
    without the grid2op dependency.

    Args:
        env_folder (str or Path): Path to the folder containing the environment files.
        env_name (str): The specific name of the environment to load.
        thermal_limits_file (str, optional): Name of thermal limits JSON file.
        is_DC (bool, optional): If True, use DC load flow instead of AC. Defaults to False.
        threshold_thermal_limit (float, optional): Multiplier for thermal limits. Defaults to 0.95.
        network (pp.network.Network, optional): A pre-loaded pypowsybl network
            object. When provided, the XIIDM/CGMES file on disk is NOT re-parsed;
            the SimulationEnvironment reuses this exact instance. Useful when an
            outer caller (e.g. an HTTP backend) has already called
            `pp.network.load(...)` and wants to avoid the ~1-5 s duplicate parse.
            `env_folder` / `env_name` are still used to locate companion files
            (thermal limits, non-reconnectable list), but NOT to locate the
            network itself. Defaults to None (load from disk).
        skip_initial_obs (bool, optional): When True, do NOT call `env.get_obs()`
            — returns `obs=None` in the result tuple. Building the first
            `PypowsyblObservation` on a large grid is ~3-5 s of pypowsybl
            reads (get_lines/buses/generators/loads). Callers that don't
            consume the returned `obs` (e.g. HTTP backends that only use
            `env`) can set this flag to save that time. Subsequent
            `env.get_obs()` calls remain functional. Defaults to False.

    Returns:
        tuple: A tuple containing:
            - env (SimulationEnvironment): The initialized environment object.
            - obs (PypowsyblObservation | None): The initial observation — `None`
              when `skip_initial_obs=True`.
            - path_env (str): The path to the environment.
    """
    env_path = Path(env_folder) / env_name

    # When a pre-loaded Network is injected we skip the .xiidm discovery
    # entirely — we only need env_path resolved for downstream helpers
    # (thermal limits lookup, non-reconnectable CSV, etc.).
    network_file = None
    if network is None:
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

        # Also check in grid/ subfolder
        if network_file is None:
            grid_folder = env_path / "grid"
            if grid_folder.exists():
                for ext in ['.xiidm', '.iidm', '.xml']:
                    candidates = list(grid_folder.glob(f"*{ext}"))
                    if candidates:
                        network_file = candidates[0]
                        break

        if network_file is None:
            raise FileNotFoundError(f"No network file found in {env_path}")
    else:
        # When env_path itself points to a file (direct .xiidm path), treat
        # its parent as the companion-file folder — same rule as the disk
        # path above, applied in the pre-loaded branch too.
        if env_path.is_file() and env_path.suffix.lower() in ['.xiidm', '.iidm', '.xml']:
            env_path = env_path.parent

    # Look for thermal limits
    thermal_limits_path = None
    if thermal_limits_file:
        thermal_limits_path = env_path / thermal_limits_file
        if not thermal_limits_path.exists():
            thermal_limits_path = None

    if thermal_limits_path is None:
        for name in ['thermal_limits.json', 'limits.json']:
            candidate = env_path / name
            if candidate.exists():
                thermal_limits_path = candidate
                break

    # Create environment — inject the pre-loaded Network when provided so
    # SimulationEnvironment/NetworkManager skip the duplicate pp.network.load().
    env = SimulationEnvironment(
        network_path=network_file,
        network=network,
        thermal_limits_path=thermal_limits_path,
        threshold_thermal_limit=threshold_thermal_limit
    )

    # Configure DC mode if requested
    if is_DC:
        # Store DC preference for simulations
        env._use_dc = True
        env.network_manager._default_dc = True

    # Get initial observation — skipped when the caller doesn't consume it.
    # Building the first PypowsyblObservation on a large grid is several
    # seconds of pypowsybl reads; skipping saves that time.
    obs = None if skip_initial_obs else env.get_obs()

    return env, obs, str(env_path)


def setup_environment_configs_pypowsybl(analysis_date: Optional[datetime.datetime] = None,
                                         env_folder: Optional[Union[str, Path]] = None,
                                         env_name: Optional[str] = None,
                                         network: Optional[pp.network.Network] = None,
                                         skip_initial_obs: bool = False
                                         ) -> Tuple[SimulationEnvironment, Optional[PypowsyblObservation], str, str,
                                                    Optional[List], Dict, List[str], List[str]]:
    """
    Sets up the pypowsybl environment and loads related configuration files.

    This function performs several setup tasks:
    1. Loads the predefined action space from a JSON file specified in the config.
    2. Initializes the pypowsybl environment using parameters from the config.
    3. Retrieves the initial observation.
    4. Loads lists of non-reconnectable lines and lines to monitor from CSV files.

    Args:
        analysis_date: The date for analysis (optional, for compatibility).
        env_folder: Override for environment folder path.
        env_name: Override for environment name.
        network: Optional pre-loaded `pp.network.Network` to reuse instead of
            re-parsing the .xiidm file. Forwarded to `get_env_first_obs_pypowsybl`
            which forwards it to `SimulationEnvironment`. Useful when an outer
            caller (HTTP backend, notebook, …) already holds a Network instance
            — avoids a duplicate ~1-5 s pypowsybl parse on large grids.

    Returns:
        tuple: A tuple containing:
            - env (SimulationEnvironment): The initialized environment object.
            - obs (PypowsyblObservation): The initial observation.
            - path_chronic (str): The environment path (for compatibility).
            - chronic_name (str): Name identifier for the analysis.
            - custom_layout (list or None): Grid layout coordinates (if available).
            - dict_action (dict): The loaded action space dictionary.
            - lines_non_reconnectable (list): Lines that cannot be reconnected.
            - lines_we_care_about (list): Lines designated for monitoring.
    """
    # Use config values if not overridden
    if env_folder is None:
        env_folder = config.ENV_FOLDER
    if env_name is None:
        env_name = config.ENV_NAME

    # Load action dictionary
    dict_action = load_actions(config.ACTION_FILE_PATH)

    # Create environment
    env, obs, env_path = get_env_first_obs_pypowsybl(
        env_folder,
        env_name,
        is_DC=config.USE_DC_LOAD_FLOW,
        threshold_thermal_limit=config.MONITORING_FACTOR_THERMAL_LIMITS,
        network=network,
        skip_initial_obs=skip_initial_obs,
    )
    
    # For static analysis, use env_name as chronic_name
    chronic_name = env_name
    if analysis_date:
        chronic_name = f"{env_name}_{analysis_date.strftime('%Y%m%d')}"
    
    # Custom layout (not typically available in bare pypowsybl)
    custom_layout = None
    
    # Load non-reconnectable lines from CSV
    lines_non_reconnectable = []
    try:
        lines_non_reconnectable = list(load_interesting_lines(
            path=env_path,
            file_name="non_reconnectable_lines.csv"
        ))
    except FileNotFoundError:
        pass

    # Detect non-reconnectable lines from switch topology in the grid context.
    # CSV file per chronic remains the authoritative source, but we supplement with
    # topology detection for robustness (e.g. for bare environments with specific dates).
    try:
        detected_non_reco = env.network_manager.detect_non_reconnectable_lines()
        if detected_non_reco:
            print(f"Detected {len(detected_non_reco)} non-reconnectable lines from grid topology: {detected_non_reco}")
        for line in detected_non_reco:
            if line not in lines_non_reconnectable:
                lines_non_reconnectable.append(line)
    except Exception as e:
        print(f"Warning: Could not detect non-reconnectable lines from grid topology: {e}")

    # Add deleted lines
    lines_non_reconnectable += list(DELETED_LINE_NAME)
    
    # Load lines to monitor
    lines_we_care_about = []
    if config.IGNORE_LINES_MONITORING:
        limits_df = env.network_manager.network.get_operational_limits().reset_index()
        branches_with_perm_limits = set(
            limits_df[limits_df['name'] == 'permanent_limit']['element_id'].unique()
        )
        lines_we_care_about = np.array([l for l in env.name_line if l in branches_with_perm_limits])
        n_excluded = len(env.name_line) - len(lines_we_care_about)
        if n_excluded > 0:
            print(f"Excluded {n_excluded} branch(es) without permanent operational limits from monitoring.")
    else:
        try:
            monitoring_file = config.LINES_MONITORING_FILE
            if monitoring_file is None:
                monitoring_file = os.path.join(str(env_folder), "lignes_a_monitorer.csv")
            lines_we_care_about = load_interesting_lines(file_name=monitoring_file)
        except FileNotFoundError:
            # No monitoring file: consider all branches that have permanent operational limits
            limits_df = env.network_manager.network.get_operational_limits().reset_index()
            branches_with_perm_limits = set(
                limits_df[limits_df['name'] == 'permanent_limit']['element_id'].unique()
            )
            lines_we_care_about = np.array([l for l in env.name_line if l in branches_with_perm_limits])
            n_excluded = len(env.name_line) - len(lines_we_care_about)
            if n_excluded > 0:
                print(f"Excluded {n_excluded} branch(es) without permanent operational limits from monitoring.")
    
    return (env, obs, env_path, chronic_name, custom_layout, 
            dict_action, lines_non_reconnectable, lines_we_care_about)


def set_thermal_limits_from_network(env: SimulationEnvironment, 
                                     threshold: float = 0.95) -> SimulationEnvironment:
    """
    Set thermal limits from the network's operational limits.

    Args:
        env: SimulationEnvironment instance
        threshold: Multiplier for limits (e.g., 0.95 for 95%)

    Returns:
        The environment with updated thermal limits
    """
    thermal_limits = env.network_manager.get_thermal_limits()
    
    # Apply threshold
    limits_array = np.array([
        thermal_limits.get(ln, 9999.0) * threshold 
        for ln in env.name_line
    ])
    
    env.set_thermal_limit(limits_array)
    return env


def switch_to_dc_load_flow_pypowsybl(env: SimulationEnvironment,
                                       lines_defaut: List[str],
                                       lines_overloaded_ids_kept: List[int],
                                       maintenance_to_reco_at_t: List[str]
                                       ) -> Tuple[SimulationEnvironment, PypowsyblObservation, PypowsyblObservation]:
    """
    Switch to DC load flow mode and re-run simulations.

    This is called when AC load flow fails to converge.

    Args:
        env: Current environment
        lines_defaut: List of contingency line names
        lines_overloaded_ids_kept: Indices of overloaded lines being considered
        maintenance_to_reco_at_t: Lines to reconnect from maintenance

    Returns:
        Tuple of (new_env, new_obs, obs_simu)
    """
    from expert_op4grid_recommender.utils.simulation import simulate_contingency

    print("Switching to DC load flow due to convergence issues.")
    
    # Get the network path from current environment
    network_path = None
    for attr in ['_network_path', 'network_path']:
        if hasattr(env, attr):
            network_path = getattr(env, attr)
            break
    
    if network_path is None:
        # Try to get from network manager
        network_path = env.network_manager.network
    
    # Create new environment with DC mode
    # Note: In the pypowsybl backend, we can set DC mode on the network manager
    env.network_manager._default_dc = True

    # Mark this (possibly reused) env as transiently escalated to DC so a later
    # analysis restores it (issue #6). This flag is checked at the top of
    # run_analysis_step1's reused-env branch, which calls
    # env.reset_loadflow_mode_to_baseline() to undo the escalation. Without it,
    # DC mode + the DC-solved (de-energised) base variant leak across analyses
    # and silently hide every subsequent overload.
    env._dc_escalation_pending = True

    # Reset and get new observation
    obs = env.reset()
    
    # Create maintenance reconnection action
    act_reco_maintenance = env.action_space(
        {"set_line_status": [(line_reco, 1) for line_reco in maintenance_to_reco_at_t]}
    )
    
    # Simulate contingency in DC mode (retain the variant + fast mode, as the
    # dedicated pypowsybl helper did before the R4 module unification).
    obs_simu, has_converged = simulate_contingency(
        env, obs, lines_defaut, act_reco_maintenance, timestep=0,
        simulate_kwargs={"keep_variant": True, "fast_mode": True},
    )
    
    if not has_converged:
        raise LoadFlowDivergedError(
            "Simulation failed even in DC mode. Cannot build overflow graph."
        )

    return env, obs, obs_simu


# ============================================================================
# Compatibility layer - allows gradual migration
# ============================================================================

def get_env_first_obs(env_folder, env_name, use_evaluation_config, date=None, is_DC=False):
    """
    Compatibility wrapper that uses pypowsybl backend.
    
    This function provides the same interface as the original grid2op version
    but uses the pypowsybl backend internally.
    
    Note: The date parameter is kept for API compatibility but is not used
    in static analysis mode.
    """
    return get_env_first_obs_pypowsybl(
        env_folder=env_folder,
        env_name=env_name,
        is_DC=is_DC
    )


def setup_environment_configs(analysis_date: datetime.datetime,
                               network: Optional[pp.network.Network] = None,
                               skip_initial_obs: bool = False):
    """
    Compatibility wrapper that uses pypowsybl backend.

    This function provides the same interface as the original grid2op version.
    `network` is an optional pre-loaded Network passed through to
    `setup_environment_configs_pypowsybl` — see that docstring for the
    load-deduplication use case. `skip_initial_obs` skips the first
    `env.get_obs()` call, returning `obs=None` in the tuple — useful when
    the caller only consumes the env and not the obs (saves ~3-5 s on
    large grids).
    """
    return setup_environment_configs_pypowsybl(analysis_date, network=network, skip_initial_obs=skip_initial_obs)
