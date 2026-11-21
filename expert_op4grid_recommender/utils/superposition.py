# expert_op4grid_recommender/utils/superposition.py
"""
Superposition theorem for inferring combined action pair effects.

This module adapts the superposition theorem from
https://github.com/marota/Topology_Superposition_Theorem
to work with the pypowsybl-based observations used in expert_op4grid_recommender.

Two regimes are supported:

* **Extended Superposition Theorem (EST)** — pairs of *topology* actions (line
  disconnection / reconnection, node split / merge, PST). The combined flow is

      p_or_combined = (1 - sum(betas)) * obs_start.p_or + sum(betas[i] * obs_unit[i].p_or)

  where ``betas`` solve a linear system built from p_or and delta_theta values
  at the lines / substations involved in each action.

* **Generalized Superposition Theorem (GST)** — pairs where at least one action
  is an *injection* change (load shedding, renewable curtailment, redispatch).
  The injection enters in pure superposition (its flow response is added in
  full) while the topology action keeps an injection-shifted EST beta. Reporting
  an injection action with ``beta = 1.0`` lets the SAME reconstruction formula
  above produce the exact GST flows — see :func:`compute_combined_pair_gst`.
"""

import numpy as np
from itertools import combinations
from typing import Dict, List, Tuple, Any, Optional

from expert_op4grid_recommender.utils.injections import consumption_mw, production_mw


# =============================================================================
# Low-level helper functions (adapted from superposition_theorem/core)
# =============================================================================

def _get_theta_node(obs, sub_id: int, bus: int) -> float:
    """Get the median voltage angle at a specific bus of a substation.

    Uses all connected elements at the bus (lines_or, lines_ex) to estimate
    the bus voltage angle, even for disconnected lines.

    Args:
        obs: Observation with theta_or, theta_ex, line_or_bus, line_ex_bus
        sub_id: Substation index
        bus: Bus number (1 or 2)

    Returns:
        Median theta at the bus (radians), or 0.0 if no connected elements
    """
    obj = obs.get_obj_connect_to(substation_id=sub_id)

    lines_or = [i for i in obj['lines_or_id'] if obs.line_or_bus[i] == bus]
    lines_ex = [i for i in obj['lines_ex_id'] if obs.line_ex_bus[i] == bus]

    thetas = np.concatenate([obs.theta_or[lines_or], obs.theta_ex[lines_ex]])
    thetas = thetas[thetas != 0]

    if len(thetas) == 0:
        return 0.0
    return float(np.median(thetas))


def get_delta_theta_line(obs, line_idx: int) -> float:
    """Compute delta theta (theta_or - theta_ex) for a line.

    Uses endpoint substation angles via connected elements, so this works
    correctly even when the line itself is disconnected (theta_or/theta_ex
    would be 0, but the substation bus still has a non-zero angle).

    Args:
        obs: Observation with theta_or, theta_ex, line_or_bus, line_ex_bus
        line_idx: Index of the line

    Returns:
        Delta theta value in radians
    """
    sub_or = obs.line_or_to_subid[line_idx]
    sub_ex = obs.line_ex_to_subid[line_idx]
    bus_or = int(obs.line_or_bus[line_idx])
    bus_ex = int(obs.line_ex_bus[line_idx])

    # For a disconnected line bus is -1, fall back to bus 1 (where it will reconnect)
    if bus_or == -1:
        bus_or = 1
    if bus_ex == -1:
        bus_ex = 1

    theta_or = _get_theta_node(obs, sub_or, bus_or)
    theta_ex = _get_theta_node(obs, sub_ex, bus_ex)
    return theta_or - theta_ex


def get_delta_theta_sub_2nodes(obs, sub_id: int) -> float:
    """Compute delta theta between bus 2 and bus 1 at a substation.

    This is the "virtual line" delta theta for substation topology actions.

    Args:
        obs: Observation
        sub_id: Substation index

    Returns:
        Delta theta (bus2 - bus1) in radians
    """
    theta_bus1 = _get_theta_node(obs, sub_id, bus=1)
    theta_bus2 = _get_theta_node(obs, sub_id, bus=2)
    return theta_bus2 - theta_bus1


def get_sub_node1_idsflow(obs, sub_id: int):
    """Identify elements belonging to node 1 (local bus 1) at a substation.

    Adapted from the reference superposition_theorem implementation.
    "Node 1" is the first bus (local bus number 1) within the substation.
    This must be called with an observation where the substation is already
    split (the post-split observation), so the bus assignments reflect
    the future topology.

    Works with both Grid2Op observations (via _get_bus_id) and
    PypowsyblObservation (via line_or_bus / get_obj_connect_to directly).

    Args:
        obs: Post-split observation where the substation has two buses
        sub_id: Substation index

    Returns:
        Tuple (ind_load_node1, ind_prod_node1, ind_lor_node1, ind_lex_node1)
    """
    if hasattr(obs, '_get_bus_id'):
        # Grid2Op path: _get_bus_id returns global bus IDs.
        # Global bus == sub_id means local bus 1.
        ind_prod, _ = obs._get_bus_id(obs.gen_pos_topo_vect, obs.gen_to_subid)
        ind_load, _ = obs._get_bus_id(obs.load_pos_topo_vect, obs.load_to_subid)
        ind_lor, _ = obs._get_bus_id(obs.line_or_pos_topo_vect, obs.line_or_to_subid)
        ind_lex, _ = obs._get_bus_id(obs.line_ex_pos_topo_vect, obs.line_ex_to_subid)

        ind_lor_node1 = [i for i in range(obs.n_line) if ind_lor[i] == sub_id]
        ind_lex_node1 = [i for i in range(obs.n_line) if ind_lex[i] == sub_id]
        ind_load_node1 = [i for i in range(obs.n_load) if ind_load[i] == sub_id]
        ind_prod_node1 = [i for i in range(obs.n_gen) if ind_prod[i] == sub_id]
    else:
        # PypowsyblObservation path: use get_obj_connect_to to get element lists
        # at this substation, then filter by local bus == 1.
        obj = obs.get_obj_connect_to(substation_id=sub_id)

        # line_or_bus / line_ex_bus give the local bus number (1 or 2) per line
        line_or_bus = obs.line_or_bus
        line_ex_bus = obs.line_ex_bus

        ind_lor_node1 = [i for i in obj['lines_or_id'] if line_or_bus[i] == 1]
        ind_lex_node1 = [i for i in obj['lines_ex_id'] if line_ex_bus[i] == 1]

        # For loads and gens, use sub_topology which returns bus assignments
        # in order: [loads..., gens..., lines_or..., lines_ex...]
        topo = obs.sub_topology(sub_id)
        n_loads_sub = len(obj['loads_id'])
        n_gens_sub = len(obj['generators_id'])

        ind_load_node1 = [
            obj['loads_id'][k] for k in range(n_loads_sub)
            if topo[k] == 1
        ]
        ind_prod_node1 = [
            obj['generators_id'][k] for k in range(n_gens_sub)
            if topo[n_loads_sub + k] == 1
        ]

    return (ind_load_node1, ind_prod_node1, ind_lor_node1, ind_lex_node1)


def get_virtual_line_flow(obs, ind_load, ind_prod, ind_lor, ind_lex) -> float:
    """Compute the virtual line flow at a substation using KCL at node 1.

    The virtual line flow represents the power flowing between the two buses
    of a split substation, computed from Kirchhoff's Current Law applied
    to node 1 elements.

    Args:
        obs: Observation with p_or, load_p, gen_p
        ind_load: Indices of loads on node 1
        ind_prod: Indices of generators on node 1
        ind_lor: Indices of lines with origin at node 1
        ind_lex: Indices of lines with extremity at node 1

    Returns:
        Virtual line active power flow (MW)
    """
    # Production counted positive on both backends: a pypowsybl observation
    # reports a producing generator with a negative gen_p (receptor convention)
    production = production_mw(obs)
    consumption = consumption_mw(obs)
    flow = 0.0
    flow += sum(-obs.p_or[i] for i in ind_lor)
    flow += sum(obs.p_or[i] for i in ind_lex)  # p_ex flows in opposite direction
    flow += sum(-consumption[i] for i in ind_load)
    flow += sum(production[i] for i in ind_prod)
    return flow


def _is_sub_reference_topology(obs, sub_id: int) -> bool:
    """Check if a substation is in reference (single bus) topology.

    Returns True if the substation has no element on bus 2.
    """
    topo = obs.sub_topology(sub_id)
    return 2 not in topo


# =============================================================================
# Beta coefficients computation
# =============================================================================

def get_betas_coeff(delta_theta_unit_acts, delta_theta_start,
                    p_or_unit_acts, p_or_start, idls):
    """Compute the superposition theorem beta coefficients.

    Adapted from get_betas_coeff_N_unit_acts_ultimate in the reference repo.

    The betas satisfy the linear system:
      A * betas = [1, 1, ..., 1]
    where A[j][i] = 1 - (feature_unit_act[i][j] / feature_start[j])
    with diagonal = 1.

    Args:
        delta_theta_unit_acts: array (n_actions, n_actions) of delta_theta
            for each unit action observation at each involved element
        delta_theta_start: array (n_actions,) delta_theta of start obs
            at each involved element
        p_or_unit_acts: array (n_actions, n_actions) of p_or
            for each unit action observation at each involved element
        p_or_start: array (n_actions,) p_or of start obs at involved elements
        idls: list of element indices (for reference, len = n_actions)

    Returns:
        betas: array of beta coefficients, one per action
    """
    n = len(idls)

    # Build the A matrix
    a = np.zeros((n, n))
    # p_or threshold: must be physically meaningful (not numerical noise).
    # Disconnected lines in pypowsybl have p_or ~ 1e-5 MW (noise). Using a
    # threshold of 1.0 MW ensures we only use p_or when the line is actually
    # carrying significant flow. Below this, delta_theta is more reliable.
    p_or_threshold = 1.0
    dt_threshold = 1e-6
    for j in range(n):
        for i in range(n):
            if i == j:
                a[j][i] = 1.0
            else:
                # Use p_or if the start flow is physically significant,
                # otherwise fall back to delta_theta.
                if abs(p_or_start[j]) > p_or_threshold:
                    a[j][i] = 1.0 - p_or_unit_acts[i][j] / p_or_start[j]
                elif abs(delta_theta_start[j]) > dt_threshold:
                    a[j][i] = 1.0 - delta_theta_unit_acts[i][j] / delta_theta_start[j]
                else:
                    a[j][i] = 1.0  # No coupling info available

    b = np.ones(n)
    try:
        betas = np.linalg.solve(a, b)

    except (np.linalg.LinAlgError, ValueError):
        # Singular matrix or other math error — can't solve
        print("[Superposition] Warning: could not solve linear system for betas. Falling back to [1.0, 1.0].")
        betas = np.ones(n)

    return betas


# =============================================================================
# Action element identification
# =============================================================================

def _identify_action_elements(action, action_id: str, dict_action: dict,
                              classifier, env) -> Tuple[List[int], List[int]]:
    """Identify the line indices and substation indices affected by an action.

    Args:
        action: The PypowsyblAction object
        action_id: Action ID string
        dict_action: Full action dictionary
        classifier: ActionClassifier instance
        env: Environment with name_line, action_space

    Returns:
        (line_indices, sub_indices) — lists of integer indices
    """
    line_indices = []
    sub_indices = []

    # Determine action type from the dict
    action_desc = dict_action.get(action_id, {})
    # Initialize classifier with env action space if possible for by_description=False
    if classifier._action_space is None:
        classifier._action_space = env.action_space

    # Try classification
    action_type = classifier.identify_action_type(action_desc, by_description=True)
    if action_type == "unknown":
        # Guess from ID prefixes
        if "reco_" in action_id or "close_line" in action_id:
            action_type = "close_line"
        elif "node_merging" in action_id or "close_coupling" in action_id:
            action_type = "close_coupling"
        elif "disco_" in action_id or "open_line" in action_id:
            action_type = "open_line"
        elif "open_coupling" in action_id:
            action_type = "open_coupling"
        elif "pst_" in action_id or "pst_tap_" in action_id:
            action_type = "pst"

    name_line = list(env.name_line)
    name_sub = list(env.name_sub)

    if "line" in action_type:
        # Line action: find which line(s) are affected
        affected_lines = set()

        # Check explicit attributes in action object
        for field in ('lines_or_bus', 'lines_ex_bus'):
            val = getattr(action, field, None)
            if val:
                affected_lines.update(val.keys())

        # Check for topo_vect if it's a grid2op-like action
        if hasattr(action, 'line_set_status'):
             for i, set_val in enumerate(action.line_set_status):
                 if set_val != 0:
                     affected_lines.add(name_line[i])

        # Also check for prefixes if still empty
        if not affected_lines:
            if "reco_" in action_id:
                line_name = action_id[len("reco_"):]
                affected_lines.add(line_name)
            elif "disco_" in action_id:
                line_name = action_id[len("disco_"):]
                affected_lines.add(line_name)
            elif "open_line_" in action_id:
                line_name = action_id[len("open_line_"):]
                affected_lines.add(line_name)
            elif "close_line_" in action_id:
                line_name = action_id[len("close_line_"):]
                affected_lines.add(line_name)

        for line_name in affected_lines:
            if line_name in name_line:
                line_indices.append(name_line.index(line_name))

    if "pst" in action_type:
        # PST action: find which branch(es) are affected
        affected_lines = set()

        # 1. Check action content first (preferred)
        # Handle both flat action_desc and nested content structure
        content = action_desc if "pst_tap" in action_desc else action_desc.get("content", {})
        pst_tap = content.get("pst_tap", {})
        if pst_tap:
            affected_lines.update(pst_tap.keys())

        # 2. Fallback to ElementId if it's there
        resid = action_desc.get("ElementId")
        if resid:
            affected_lines.add(resid)

        # 3. Fallback to ID-based extraction if still empty
        if not affected_lines:
            raw_name = None
            if "pst_tap_" in action_id:
                raw_name = action_id[len("pst_tap_"):]
            elif "pst_" in action_id:
                raw_name = action_id[len("pst_"):]
            
            if raw_name:
                # Strip the suffix added by discovery logic (e.g., _inc1, _dec2)
                import re
                clean_name = re.sub(r'_(inc|dec)\d+$', '', raw_name)
                affected_lines.add(clean_name)

        for line_name in affected_lines:
            # Robust matching: Try exact, then strip leading dot, then try containment
            if line_name in name_line:
                line_indices.append(name_line.index(line_name))
            elif line_name.startswith(".") and line_name[1:] in name_line:
                line_indices.append(name_line.index(line_name[1:]))
            else:
                # Last resort fuzzy match: find a line name that is a substring or vice versa
                # (but avoid matching just a dot if the name is empty after stripping)
                candidate_name = line_name.lstrip(".")
                if not candidate_name:
                    continue
                found = False
                for i, nl in enumerate(name_line):
                    if candidate_name in nl or nl in candidate_name:
                        line_indices.append(i)
                        found = True
                        break

    if "coupling" in action_type or "node_merging" in action_id or not line_indices:
        # Substation topology action: find which substation(s) are affected
        affected_subs = set()

        vl_id = action_desc.get("VoltageLevelId")
        if vl_id:
            affected_subs.add(vl_id)

        # Check action's substations field
        if hasattr(action, 'substations') and action.substations:
            affected_subs.update(action.substations.keys())

        # Check from ID suffix if node_merging
        if "node_merging_" in action_id:
            sub_name = action_id[len("node_merging_"):]
            affected_subs.add(sub_name)
        elif "open_coupling_" in action_id:
            sub_name = action_id[len("open_coupling_"):]
            affected_subs.add(sub_name)
        elif "close_coupling_" in action_id:
            sub_name = action_id[len("close_coupling_"):]
            affected_subs.add(sub_name)

        for sub_name in affected_subs:
            if sub_name in name_sub:
                sub_indices.append(name_sub.index(sub_name))

    return line_indices, sub_indices


# =============================================================================
# Rho estimation from superposed active power
# =============================================================================

def _estimate_rho_from_p(p_or_combined, p_ex_combined, obs_start,
                         obs_act1=None, obs_act2=None):
    """Estimate rho (loading) from superposed active power flows.

    Instead of superposing rho directly (which introduces bias from the
    max() convexity and reactive power non-superposition), this function:
    1. Computes rho/|P| conversion factors from obs_start for each extremity
    2. Applies them to the superposed P to get rho at each extremity
    3. Takes max(rho_or, rho_ex) per line

    The assumption is that cos(phi) and V don't change drastically between
    obs_start and the combined state, which is much weaker than assuming
    rho itself superposes linearly.

    For lines where |P_start| < threshold (e.g. disconnected lines), the
    function attempts a fallback using action observations (obs_act1, obs_act2)
    to derive the conversion factor.

    Args:
        p_or_combined: Superposed active power at origin (MW), array
        p_ex_combined: Superposed active power at extremity (MW), array
        obs_start: Base observation (for rho and p_or/p_ex reference values)
        obs_act1: Optional observation after action 1 (for fallback on disconnected lines)
        obs_act2: Optional observation after action 2 (for fallback on disconnected lines)

    Returns:
        rho_combined: Estimated loading array per line
    """
    from expert_op4grid_recommender.config import MAX_RHO_BOTH_EXTREMITIES

    p_or_start = obs_start.p_or
    p_ex_start = obs_start.p_ex
    rho_start = obs_start.rho  # already max(rho_or, rho_ex) if configured

    # We need per-extremity rho from obs_start. The observation stores
    # rho = max(rho_or, rho_ex) when MAX_RHO_BOTH_EXTREMITIES=True, but
    # we need the individual rho_or and rho_ex.
    # Use current arrays if available, otherwise fall back to rho-based estimate.
    has_current_data = hasattr(obs_start, 'a_or') and hasattr(obs_start, 'a_ex')
    has_limit_data = hasattr(obs_start, '_limit_or') and hasattr(obs_start, '_limit_ex')

    if has_current_data and has_limit_data:
        # Best path: use actual current and limits to get per-extremity rho
        i_or = np.abs(obs_start.a_or)
        i_ex = np.abs(obs_start.a_ex)
        limit_or = obs_start._limit_or.values
        limit_ex = obs_start._limit_ex.values
        limit_or = np.where(limit_or < 1e-6, 1e-6, limit_or)
        limit_ex = np.where(limit_ex < 1e-6, 1e-6, limit_ex)
        rho_or_start = i_or / limit_or
        rho_ex_start = i_ex / limit_ex
    else:
        # Fallback: use rho for both extremities (same as before for origin-only)
        rho_or_start = rho_start
        rho_ex_start = rho_start

    # Compute conversion factors: rho / |P| for each extremity.
    # This factor encapsulates cos(phi), V, and I_max in one ratio.
    # For lines with near-zero P (lightly loaded or disconnected), the factor
    # is unreliable — fall back to direct rho superposition for those lines.
    p_threshold = 0.1  # MW — below this, P is too small for a reliable ratio

    abs_p_or_start = np.abs(p_or_start)
    abs_p_ex_start = np.abs(p_ex_start)

    # Origin extremity: rho_or = |P_or_combined| * (rho_or_start / |P_or_start|)
    safe_or = abs_p_or_start > p_threshold
    with np.errstate(divide='ignore', invalid='ignore'):
        factor_or = np.where(safe_or, rho_or_start / abs_p_or_start, 0.0)

    # Fallback for origin: use action observations for lines where |P_or_start| < threshold
    needs_fallback_or = ~safe_or
    for obs_act in [obs_act1, obs_act2]:
        if obs_act is None or not np.any(needs_fallback_or):
            break
        abs_p_act = np.abs(obs_act.p_or)
        act_usable = needs_fallback_or & (abs_p_act > p_threshold)
        if np.any(act_usable):
            factor_or = np.where(act_usable, obs_act.rho / abs_p_act, factor_or)
            needs_fallback_or = needs_fallback_or & ~act_usable

    rho_or_est = np.abs(p_or_combined) * factor_or

    # Extremity side: rho_ex = |P_ex_combined| * (rho_ex_start / |P_ex_start|)
    safe_ex = abs_p_ex_start > p_threshold
    with np.errstate(divide='ignore', invalid='ignore'):
        factor_ex = np.where(safe_ex, rho_ex_start / abs_p_ex_start, 0.0)

    # Fallback for extremity: use action observations for lines where |P_ex_start| < threshold
    needs_fallback_ex = ~safe_ex
    for obs_act in [obs_act1, obs_act2]:
        if obs_act is None or not np.any(needs_fallback_ex):
            break
        abs_p_act_ex = np.abs(obs_act.p_ex)
        act_usable_ex = needs_fallback_ex & (abs_p_act_ex > p_threshold)
        if np.any(act_usable_ex):
            factor_ex = np.where(act_usable_ex, obs_act.rho / abs_p_act_ex, factor_ex)
            needs_fallback_ex = needs_fallback_ex & ~act_usable_ex

    rho_ex_est = np.abs(p_ex_combined) * factor_ex

    if MAX_RHO_BOTH_EXTREMITIES:
        rho_combined = np.maximum(rho_or_est, rho_ex_est)
    else:
        rho_combined = rho_or_est

    # For lines where both factors were unreliable (both |P| < threshold)
    # AND the fallback also didn't resolve them, set rho to 0.
    # This only affects lines with negligible flow, which won't be the
    # max_rho line anyway.
    both_unreliable = needs_fallback_or & needs_fallback_ex
    if np.any(both_unreliable):
        # These lines have near-zero flow — rho is essentially 0
        rho_combined[both_unreliable] = 0.0

    return rho_combined


def _compute_delta_theta_all_lines(obs):
    """Compute delta_theta for every line (works for both connected and disconnected).

    For connected lines, uses obs.theta_or - obs.theta_ex directly.
    For disconnected lines, falls back to get_delta_theta_line which reconstructs
    the angle difference from bus angles of other connected elements.

    Args:
        obs: Observation with theta_or, theta_ex, line_status attributes

    Returns:
        numpy array of delta_theta per line (radians)
    """
    dt = obs.theta_or - obs.theta_ex  # vectorized, correct for connected lines
    # For disconnected lines, theta_or/theta_ex are 0 → dt=0 (wrong)
    # Fix using get_delta_theta_line which looks up bus angles
    disconnected = ~obs.line_status.astype(bool)
    for i in np.where(disconnected)[0]:
        dt[i] = get_delta_theta_line(obs, int(i))
    return dt


def _estimate_rho_from_delta_theta(dt_combined, obs_start, obs_act1, obs_act2):
    """Estimate rho from superposed delta_theta, using action obs as fallback.

    For reconnection/merging actions, delta_theta is the natural base quantity
    (as used in beta computation). This function converts superposed delta_theta
    to rho using per-line conversion factors rho/|delta_theta|.

    Primary factor comes from obs_start (for lines connected there).
    For disconnected lines in obs_start, falls back to action observations.

    Args:
        dt_combined: Superposed delta_theta per line (radians), array
        obs_start: Base observation
        obs_act1: Observation after applying action 1
        obs_act2: Observation after applying action 2

    Returns:
        rho_combined: Estimated loading array per line
    """

    dt_threshold = 1e-4  # radians
    rho_start = obs_start.rho

    # Primary factor: from obs_start (for lines connected there)
    dt_start = _compute_delta_theta_all_lines(obs_start)
    abs_dt_start = np.abs(dt_start)
    safe = abs_dt_start > dt_threshold
    with np.errstate(divide='ignore', invalid='ignore'):
        factor = np.where(safe & (rho_start > 0), rho_start / abs_dt_start, 0.0)

    # Fallback: for disconnected lines in obs_start, use action observations
    needs_fallback = ~safe | (rho_start == 0)
    for obs_act in [obs_act1, obs_act2]:
        if not np.any(needs_fallback):
            break
        dt_act = _compute_delta_theta_all_lines(obs_act)
        abs_dt_act = np.abs(dt_act)
        rho_act = obs_act.rho
        act_usable = needs_fallback & (abs_dt_act > dt_threshold) & (rho_act > 0)
        if np.any(act_usable):
            factor = np.where(act_usable, rho_act / abs_dt_act, factor)
            needs_fallback = needs_fallback & ~act_usable

    return np.abs(dt_combined) * factor


# =============================================================================
# Generalized Superposition Theorem (GST): injection-aware action pairs
# =============================================================================
#
# The Extended Superposition Theorem (EST) above combines *topology* actions
# (line disconnection / reconnection, node split / merge, PST). The GST extends
# it to action pairs that also involve an *injection* change -- load shedding,
# renewable curtailment or redispatch -- which only modifies ``load_p`` /
# ``gen_p`` (no topology change).
#
# Adapted from ``compute_flows_GST_from_unit_act_obs`` in
# Topology_Superposition_Theorem. An injection action enters in *pure
# superposition* (its flow response ``obs_inj.p_or - obs_start.p_or`` is added
# in full), while a topology action keeps an EST beta that the injection shifts
# only through the right-hand side of the (unchanged) EST linear system:
#
#     beta_T = x(P_tgt, T_ref) / x(P_ref, T_ref)
#
# with ``x = p_or`` on disconnected / split assets (non-zero reference flow),
# ``x = delta_theta`` on reconnected / merged assets (zero reference flow), and
# ``x(P_tgt, T_ref) = x(P_ref, T_ref) + sum_k (x(obs_inj_k) - x(obs_start))``.
#
# Key identity used throughout: reporting an injection action with ``beta = 1.0``
# makes the *standard* reconstruction
#
#     p_combined = (1 - sum(betas)) * obs_start.p + betas[0]*p_act1 + betas[1]*p_act2
#
# reproduce the exact GST flows, because each injection term
# ``(obs_inj.p - obs_start.p)`` equals ``1.0*obs_inj.p - 1.0*obs_start.p`` and the
# ``-obs_start.p`` parts fold into the ``(1 - sum(betas))`` weight. As a result
# the downstream rho estimators (and Co-Study4Grid's ``compute_combined_rho``)
# need no change for injection pairs.

# Action types and id prefixes that denote a pure injection change.
INJECTION_ACTION_TYPES = frozenset({
    "load_power_reduction", "gen_power_reduction", "gen_redispatch",
    "open_load", "open_gen",
})
INJECTION_ID_PREFIXES = ("load_shedding_", "curtail_", "redispatch_")


def is_injection_action(action_id: str, action_desc: Optional[dict] = None,
                        classifier: Optional[Any] = None) -> bool:
    """Return True if an action is a pure injection change.

    Injection actions (load shedding ``set_load_p``, renewable curtailment /
    redispatch ``set_gen_p``) change only nodal injections, not the topology,
    and are handled by the Generalized Superposition Theorem. Detection is by
    action-id prefix (``load_shedding_`` / ``curtail_`` / ``redispatch_``) and,
    when available, by the classifier's action type.
    """
    if action_id and action_id.startswith(INJECTION_ID_PREFIXES):
        return True
    if classifier is not None and action_desc is not None:
        try:
            atype = classifier.identify_action_type(action_desc, by_description=True)
        except Exception:
            atype = None
        if atype in INJECTION_ACTION_TYPES:
            return True
    return False


def _resolve_topology_element(obs_start, obs_topo, line_idxs: List[int],
                              sub_idxs: List[int]):
    """Describe the switched element of a topology action.

    Returns ``(kind, idx, is_ref, ind_sub)`` where ``kind`` is ``"line"`` or
    ``"sub"``, ``is_ref`` flags a substation that starts in reference (single-bus)
    topology, and ``ind_sub`` are the node-1 element ids (for substations).
    Returns ``None`` when no element could be identified.
    """
    if line_idxs:
        return ("line", line_idxs[0], None, None)
    if sub_idxs:
        sid = sub_idxs[0]
        is_ref = _is_sub_reference_topology(obs_start, sid)
        # node-1 element ids are read from the split observation (its bus map
        # reflects the post-split topology); for a merge, obs_start is split.
        ind_sub = get_sub_node1_idsflow(obs_topo if is_ref else obs_start, sid)
        return ("sub", sid, is_ref, ind_sub)
    return None


def _topology_beta_gst(obs_start, obs_inj, kind: str, idx: int,
                       is_ref: Optional[bool], ind_sub) -> float:
    """GST topology coefficient ``beta_T = x_inj / x_start`` on the switched asset.

    ``x`` is the active flow for a disconnection / node split (non-zero reference
    flow) or the angle difference for a reconnection / node merge (zero reference
    flow), evaluated on the switched element at ``obs_start`` and at the injection
    observation ``obs_inj``. Returns 1.0 when the reference quantity is
    numerically negligible (the injection then leaves that sensitivity unchanged).
    """
    p_threshold = 1.0       # MW
    dth_threshold = 1e-9    # rad
    if kind == "line":
        p_start = float(obs_start.p_or[idx])
        if abs(p_start) > p_threshold:                      # disconnection
            return float(obs_inj.p_or[idx]) / p_start
        dth_start = get_delta_theta_line(obs_start, idx)    # reconnection
        if abs(dth_start) > dth_threshold:
            return get_delta_theta_line(obs_inj, idx) / dth_start
        return 1.0
    # substation virtual line
    il, ip, ilor, ilex = ind_sub
    if is_ref:                                              # node split
        v_start = get_virtual_line_flow(obs_start, il, ip, ilor, ilex)
        if abs(v_start) > p_threshold:
            return get_virtual_line_flow(obs_inj, il, ip, ilor, ilex) / v_start
        return 1.0
    dth_start = get_delta_theta_sub_2nodes(obs_start, idx)  # node merge
    if abs(dth_start) > dth_threshold:
        return get_delta_theta_sub_2nodes(obs_inj, idx) / dth_start
    return 1.0


def compute_combined_pair_gst(
        obs_start,
        obs_act1,
        obs_act2,
        act1_line_idxs: List[int],
        act1_sub_idxs: List[int],
        act2_line_idxs: List[int],
        act2_sub_idxs: List[int],
        act1_is_injection: bool,
        act2_is_injection: bool,
        obs_combined: Optional[Any] = None,
) -> Dict[str, Any]:
    """Generalized Superposition Theorem for a pair with >=1 injection action.

    Handles the three injection-bearing pair shapes:

    * **topology + injection** -- the topology action gets ``beta_T`` (computed
      from the injection observation through the EST right-hand side), the
      injection gets ``beta = 1.0``;
    * **injection + injection** -- both get ``beta = 1.0`` (pure injection
      superposition, exact in DC);

    and returns the same structure as :func:`compute_combined_pair_superposition`
    (``betas``, ``p_or_combined``, ``p_ex_combined``, islanding fields) so the
    standard reconstruction and the rho estimators work unchanged.
    """
    inj_flags = [bool(act1_is_injection), bool(act2_is_injection)]
    obs_list = [obs_act1, obs_act2]
    line_idxs = [act1_line_idxs, act2_line_idxs]
    sub_idxs = [act1_sub_idxs, act2_sub_idxs]

    if not (inj_flags[0] or inj_flags[1]):
        return {"error": "GST path requires at least one injection action."}

    # Beta per action: injection -> 1.0 (pure superposition term); topology ->
    # GST coefficient computed against the *other* action's (injection) state.
    betas = [1.0, 1.0]
    for i in range(2):
        if inj_flags[i]:
            continue
        j = 1 - i
        if not inj_flags[j]:
            # both topology -> not a GST pair (caller should use the EST path)
            return {"error": "GST path requires at least one injection action."}
        spec = _resolve_topology_element(obs_start, obs_list[i], line_idxs[i], sub_idxs[i])
        if spec is None:
            return {"error": f"Cannot identify switched element for topology action {i + 1}."}
        kind, idx, is_ref, ind_sub = spec
        betas[i] = _topology_beta_gst(obs_start, obs_list[j], kind, idx, is_ref, ind_sub)

    # No-op detection: an injection with no flow response is meaningless.
    noop_mw = 0.1
    p_or_start = np.asarray(obs_start.p_or, dtype=float)
    for i in range(2):
        if inj_flags[i]:
            e = float(np.max(np.abs(np.asarray(obs_list[i].p_or, dtype=float) - p_or_start)))
            if e < noop_mw:
                return {"error": f"No-op injection action detected (action {i + 1} has no flow effect)."}

    betas_arr = np.array(betas, dtype=float)
    if np.any(np.isnan(betas_arr)):
        return {"error": "Singular system — cannot compute GST betas", "betas": betas}

    beta_min, beta_max = -2.0, 3.0
    if np.any(betas_arr < beta_min) or np.any(betas_arr > beta_max):
        return {
            "error": (
                f"Unreliable GST superposition: betas {np.round(betas_arr, 3).tolist()} "
                f"are outside the physically meaningful range [{beta_min}, {beta_max}]. "
                f"The topology and injection actions are too strongly coupled."
            ),
            "betas": betas,
        }

    # Reconstruction — identical to the EST formula thanks to the beta=1.0
    # convention for injection terms (see the module-level note above).
    w_start = 1.0 - float(np.sum(betas_arr))
    p_or_combined = w_start * p_or_start
    p_ex_combined = w_start * np.asarray(obs_start.p_ex, dtype=float)
    for i in range(2):
        p_or_combined = p_or_combined + betas[i] * np.asarray(obs_list[i].p_or, dtype=float)
        p_ex_combined = p_ex_combined + betas[i] * np.asarray(obs_list[i].p_ex, dtype=float)

    # Islanding detection (if the actual combined observation is provided)
    is_islanded = False
    disconnected_mw = 0.0
    if obs_combined is not None and hasattr(obs_combined, "n_components"):
        if obs_combined.n_components > obs_start.n_components:
            is_islanded = True
            if hasattr(obs_combined, "main_component_load_mw") and hasattr(obs_start, "main_component_load_mw"):
                disconnected_mw = float(max(
                    0.0, obs_start.main_component_load_mw - obs_combined.main_component_load_mw))

    return {
        "betas": betas,
        "p_or_combined": p_or_combined.tolist(),
        "p_ex_combined": p_ex_combined.tolist(),
        "is_islanded": is_islanded,
        "disconnected_mw": disconnected_mw,
        "is_gst": True,
    }


# =============================================================================
# Pair superposition computation
# =============================================================================

def compute_combined_pair_superposition(
        obs_start,
        obs_act1,
        obs_act2,
        act1_line_idxs: List[int],
        act1_sub_idxs: List[int],
        act2_line_idxs: List[int],
        act2_sub_idxs: List[int],
        obs_combined: Optional[Any] = None,
        act1_is_pst: bool = False,
        act2_is_pst: bool = False,
        act1_is_injection: bool = False,
        act2_is_injection: bool = False,
) -> Dict[str, Any]:
    """Compute the superposition theorem for a pair of unitary actions.

    Topology-only pairs use the Extended Superposition Theorem (EST). When at
    least one action is an injection change (load shedding / curtailment /
    redispatch — flagged via ``act*_is_injection``), the call is routed to the
    Generalized Superposition Theorem (:func:`compute_combined_pair_gst`).

    Args:
        obs_start: Base observation (N-1 state)
        obs_act1: Observation after applying action 1 alone
        obs_act2: Observation after applying action 2 alone
        act1_line_idxs: Line indices involved in action 1
        act1_sub_idxs: Sub indices involved in action 1
        act2_line_idxs: Line indices involved in action 2
        act2_sub_idxs: Sub indices involved in action 2
        obs_combined: Optional actual combined observation (for islanding detection)
        act1_is_pst: True if action 1 is a phase shifter tap change
        act2_is_pst: True if action 2 is a phase shifter tap change
        act1_is_injection: True if action 1 is an injection change (GST path)
        act2_is_injection: True if action 2 is an injection change (GST path)

    Returns:
        Dict with betas, p_or_combined, p_ex_combined, rho_combined
    """
    # Generalized Superposition Theorem: an injection action (load shedding,
    # renewable curtailment, redispatch) on either side is handled by the GST
    # path, which combines a pure-superposition injection term with the
    # (injection-shifted) EST beta of the topology action.
    if act1_is_injection or act2_is_injection:
        return compute_combined_pair_gst(
            obs_start, obs_act1, obs_act2,
            act1_line_idxs, act1_sub_idxs,
            act2_line_idxs, act2_sub_idxs,
            act1_is_injection, act2_is_injection,
            obs_combined=obs_combined,
        )

    # Total number of elements must equal 2 (one per action)
    n_elements_act1 = len(act1_line_idxs) + len(act1_sub_idxs)
    n_elements_act2 = len(act2_line_idxs) + len(act2_sub_idxs)

    if n_elements_act1 == 0 or n_elements_act2 == 0:
        return {"error": "Cannot identify elements for one or both actions"}

    # For multi-element actions, use only the first element for simplicity
    # (the superposition theorem works element-by-element)
    if n_elements_act1 > 1:
        act1_line_idxs = act1_line_idxs[:1] if act1_line_idxs else []
        act1_sub_idxs = act1_sub_idxs[:1] if act1_sub_idxs and not act1_line_idxs else []

    if n_elements_act2 > 1:
        act2_line_idxs = act2_line_idxs[:1] if act2_line_idxs else []
        act2_sub_idxs = act2_sub_idxs[:1] if act2_sub_idxs and not act2_line_idxs else []

    n_actions = len(act1_line_idxs) + len(act1_sub_idxs) + len(act2_line_idxs) + len(act2_sub_idxs)
    if n_actions != 2:
        return {"error": f"Expected 2 elements for pair, got {n_actions}"}

    unit_act_observations = [obs_act1, obs_act2]

    # ---- Determine per-action element type and index ----
    act1_is_line = len(act1_line_idxs) > 0
    act2_is_line = len(act2_line_idxs) > 0

    # ---- Build p_or and delta_theta arrays, one value per action ----
    def _line_features(obs, line_idx):
        """Return (p_or, delta_theta) for a line element in a given obs."""
        return obs.p_or[line_idx], get_delta_theta_line(obs, line_idx)

    def _sub_features(obs, sub_idx, ind_sub, is_ref, action_applied):
        """Return (p_or_virtual, delta_theta_virtual) for a sub element."""
        if action_applied:
            if is_ref:
                return 0.0, get_delta_theta_sub_2nodes(obs, sub_idx)
            else:
                (il, ip, ilor, ilex) = ind_sub
                return get_virtual_line_flow(obs, il, ip, ilor, ilex), 0.0
        else:
            if is_ref:
                (il, ip, ilor, ilex) = ind_sub
                return get_virtual_line_flow(obs, il, ip, ilor, ilex), 0.0
            else:
                return 0.0, get_delta_theta_sub_2nodes(obs, sub_idx)

    # Pre-compute node-1 element lists for sub actions
    ind_sub_act1 = None
    is_start_ref_act1 = None
    if not act1_is_line:
        sid1 = act1_sub_idxs[0]
        is_start_ref_act1 = _is_sub_reference_topology(obs_start, sid1)
        if is_start_ref_act1:
            ind_sub_act1 = get_sub_node1_idsflow(obs_act1, sid1)
        else:
            ind_sub_act1 = get_sub_node1_idsflow(obs_start, sid1)

    ind_sub_act2 = None
    is_start_ref_act2 = None
    if not act2_is_line:
        sid2 = act2_sub_idxs[0]
        is_start_ref_act2 = _is_sub_reference_topology(obs_start, sid2)
        if is_start_ref_act2:
            ind_sub_act2 = get_sub_node1_idsflow(obs_act2, sid2)
        else:
            ind_sub_act2 = get_sub_node1_idsflow(obs_start, sid2)

    p_or_unit_act_list = []
    delta_theta_unit_act_list = []
    for j, obs in enumerate(unit_act_observations):
        row_p = []
        row_dt = []
        if act1_is_line:
            p, dt = _line_features(obs, act1_line_idxs[0])
        else:
            p, dt = _sub_features(obs, act1_sub_idxs[0], ind_sub_act1,
                                   is_start_ref_act1, action_applied=(j == 0))
        row_p.append(p)
        row_dt.append(dt)
        if act2_is_line:
            p, dt = _line_features(obs, act2_line_idxs[0])
        else:
            p, dt = _sub_features(obs, act2_sub_idxs[0], ind_sub_act2,
                                   is_start_ref_act2, action_applied=(j == 1))
        row_p.append(p)
        row_dt.append(dt)
        p_or_unit_act_list.append(row_p)
        delta_theta_unit_act_list.append(row_dt)

    p_or_unit_act = np.array(p_or_unit_act_list)
    delta_theta_unit_act = np.array(delta_theta_unit_act_list)

    # Start-observation feature values for each element
    p_or_start_list = []
    delta_theta_start_list = []
    for act_is_line, line_idxs, sub_idxs, ind_sub, is_ref in [
        (act1_is_line, act1_line_idxs, act1_sub_idxs, ind_sub_act1, is_start_ref_act1),
        (act2_is_line, act2_line_idxs, act2_sub_idxs, ind_sub_act2, is_start_ref_act2),
    ]:
        if act_is_line:
            p, dt = _line_features(obs_start, line_idxs[0])
        else:
            if is_ref:
                (il, ip, ilor, ilex) = ind_sub
                p = get_virtual_line_flow(obs_start, il, ip, ilor, ilex)
                dt = 0.0
            else:
                p = 0.0
                dt = get_delta_theta_sub_2nodes(obs_start, sub_idxs[0])
        p_or_start_list.append(p)
        delta_theta_start_list.append(dt)

    p_or_start = np.array(p_or_start_list)
    delta_theta_start = np.array(delta_theta_start_list)

    if act1_is_line:
        idls = [act1_line_idxs[0]]
    else:
        idls = [act1_sub_idxs[0]]
    if act2_is_line:
        idls.append(act2_line_idxs[0])
    else:
        idls.append(act2_sub_idxs[0])

    # ---- No-op detection ----
    noop_dt_threshold = 1e-6
    noop_rel_tol = 0.01
    noop_p_threshold = 0.1  # MW — for PST actions where status doesn't change

    act_is_pst_flags = [act1_is_pst, act2_is_pst]
    unit_act_obs = [obs_act1, obs_act2]
    for act_idx, (act_is_line, line_idxs, sub_idxs, ind_sub, is_ref) in enumerate([
        (act1_is_line, act1_line_idxs, act1_sub_idxs, ind_sub_act1, is_start_ref_act1),
        (act2_is_line, act2_line_idxs, act2_sub_idxs, ind_sub_act2, is_start_ref_act2),
    ]):
        obs_act_n = unit_act_obs[act_idx]

        if act_is_line:
            line_idx = line_idxs[0]
            if act_is_pst_flags[act_idx]:
                # PST tap change: line stays connected, check flow change instead
                p_start = obs_start.p_or[line_idx]
                p_after = obs_act_n.p_or[line_idx]
                changed = abs(p_after - p_start) > noop_p_threshold
            else:
                status_start = bool(obs_start.line_status[line_idx])
                status_after = bool(obs_act_n.line_status[line_idx])
                changed = (status_start != status_after)
        else:
            sub_idx = sub_idxs[0]
            dt_start = delta_theta_start_list[act_idx]
            dt_own = delta_theta_unit_act_list[act_idx][act_idx]
            if abs(dt_start) > noop_dt_threshold:
                changed = abs(dt_own - dt_start) / abs(dt_start) > noop_rel_tol
            elif abs(dt_own) > noop_dt_threshold:
                changed = True
            else:
                changed = False

        if not changed:
            act_name = f"action{act_idx + 1}"
            elem_desc = (f"line {line_idxs[0]}" if act_is_line else f"sub {sub_idxs[0]}")
            return {
                "error": (
                    f"No-op action detected: {act_name}'s characteristic element "
                    f"({elem_desc}) shows no topology change in obs_start. "
                    f"Action has no effect."
                )
            }

    # ---- Compute betas ----
    betas = get_betas_coeff(
        delta_theta_unit_act, delta_theta_start,
        p_or_unit_act, p_or_start,
        idls
    )

    if np.any(np.isnan(betas)):
        return {"error": "Singular system — cannot compute betas", "betas": betas.tolist()}

    # ---- Sanity check on betas ----
    BETA_MIN = -2.0
    BETA_MAX = 3.0
    if np.any(betas < BETA_MIN) or np.any(betas > BETA_MAX):
        return {
            "error": (
                f"Unreliable superposition: betas {np.round(betas, 3).tolist()} are outside "
                f"the physically meaningful range [{BETA_MIN}, {BETA_MAX}]. "
                f"The two actions are too strongly coupled for the linear approximation."
            ),
            "betas": betas.tolist(),
        }

    # ---- Compute combined p_or AND p_ex ----
    # Both p_or and p_ex obey the superposition theorem (both are linear in
    # voltage angles in the DC approximation). Computing them separately and
    # then deriving rho from each avoids the max() convexity bias and the
    # reactive power non-superposition bias.
    beta_sum = np.sum(betas)
    w_start = 1.0 - beta_sum

    p_or_combined = w_start * obs_start.p_or
    p_ex_combined = w_start * obs_start.p_ex
    for i in range(2):
        p_or_combined = p_or_combined + betas[i] * unit_act_observations[i].p_or
        p_ex_combined = p_ex_combined + betas[i] * unit_act_observations[i].p_ex

    # ---- Islanding detection (if obs_combined is provided) ----
    is_islanded = False
    disconnected_mw = 0.0
    if obs_combined is not None and hasattr(obs_combined, 'n_components'):
         if obs_combined.n_components > obs_start.n_components:
             is_islanded = True
             if hasattr(obs_combined, 'main_component_load_mw') and hasattr(obs_start, 'main_component_load_mw'):
                 disconnected_mw = float(max(0.0, obs_start.main_component_load_mw - obs_combined.main_component_load_mw))

    return {
        "betas": betas.tolist(),
        "p_or_combined": p_or_combined.tolist(),
        "p_ex_combined": p_ex_combined.tolist(),
        "is_islanded": is_islanded,
        "disconnected_mw": disconnected_mw,
    }


# =============================================================================
# Main entry point: compute all pairs
# =============================================================================

def compute_all_pairs_superposition(
        obs_start,
        detailed_actions: Dict[str, Dict],
        classifier,
        env,
        lines_overloaded_ids: List[int],
        lines_we_care_about,
        pre_existing_rho: Dict[int, float],
        dict_action: Optional[Dict] = None,
        use_p_based_rho: bool = False,
) -> Dict[str, Dict]:
    """Compute superposition theorem for all pairs of converged prioritized actions.

    Args:
        obs_start: Base observation (N-1 state, obs_simu_defaut)
        detailed_actions: Dict of action_id -> action details (from Reassessment)
        classifier: ActionClassifier instance
        env: Environment with name_line, action_space
        lines_overloaded_ids: Indices of overloaded lines
        lines_we_care_about: Set/list of line names we monitor
        pre_existing_rho: Dict of line_idx -> pre-existing rho values
        dict_action: Full action dictionary (for action type classification)
        use_p_based_rho: If True, derive rho from superposed p_or/p_ex
            using per-line power-factor ratios. If False (default), use
            direct superposition of rho arrays — empirically more accurate
            because the P-based conversion factor breaks down when lines
            change connection status or voltage/power-factor shift significantly.

    Returns:
        Dict of "action1_id+action2_id" -> result dict with betas, p_or_combined,
        max_rho, max_rho_line, etc.
    """
    if dict_action is None:
        dict_action = {}

    # Filter to converged actions only. Injection actions (load shedding,
    # curtailment, redispatch) are KEPT — the Generalized Superposition Theorem
    # combines them with topology actions (and with each other).
    converged_ids = [
        aid for aid, details in detailed_actions.items()
        if details.get("non_convergence") is None
    ]

    if len(converged_ids) < 2:
        if converged_ids:  # Don't print if it was already empty
            print(f"[Superposition] Only {len(converged_ids)} combinable converged action(s), "
                  f"need at least 2 for pair combination.")
        return {}

    print(f"[Superposition] Computing {len(converged_ids)} * "
          f"{len(converged_ids) - 1} / 2 = "
          f"{len(converged_ids) * (len(converged_ids) - 1) // 2} pairs")

    # Pre-compute element indices and PST / injection flags for each action
    action_elements = {}
    action_is_pst = {}
    action_is_injection = {}
    for aid in converged_ids:
        action_obj = detailed_actions[aid]["action"]
        line_idxs, sub_idxs = _identify_action_elements(
            action_obj, aid, dict_action, classifier, env
        )
        action_elements[aid] = (line_idxs, sub_idxs)
        # Detect PST actions by ID prefix or action description type
        action_desc = dict_action.get(aid, {})
        action_type = classifier.identify_action_type(action_desc, by_description=True)
        action_is_pst[aid] = (
            action_type == "pst"
            or "pst_tap" in aid
            or "pst_" in aid
        )
        # Detect injection actions (GST path): load shedding / curtailment /
        # redispatch. These carry no topology element.
        action_is_injection[aid] = is_injection_action(aid, action_desc, classifier)

    # Monitoring setup for max_rho computation
    from expert_op4grid_recommender import config
    name_line = list(env.name_line)
    num_lines = len(name_line)
    worsening_threshold = config.PRE_EXISTING_OVERLOAD_WORSENING_THRESHOLD

    monitoring_factor = config.MONITORING_FACTOR_THERMAL_LIMITS
    pre_existing_baseline = np.zeros(num_lines)
    is_pre_existing = np.zeros(num_lines, dtype=bool)
    for idx, rho_val in pre_existing_rho.items():
        if rho_val >= monitoring_factor:
            pre_existing_baseline[idx] = rho_val
            is_pre_existing[idx] = True

    if lines_we_care_about is not None and len(lines_we_care_about) > 0:
        care_mask = np.isin(name_line, list(lines_we_care_about))
    else:
        care_mask = np.ones(num_lines, dtype=bool)

    results = {}
    baseline_rho = obs_start.rho[lines_overloaded_ids]

    for aid1, aid2 in combinations(converged_ids, 2):
        line_idxs1, sub_idxs1 = action_elements[aid1]
        line_idxs2, sub_idxs2 = action_elements[aid2]
        inj1 = action_is_injection.get(aid1, False)
        inj2 = action_is_injection.get(aid2, False)

        # Topology actions need an identified switched element; injection
        # actions (handled by the GST in pure superposition) do not.
        if not inj1 and len(line_idxs1) + len(sub_idxs1) == 0:
            continue
        if not inj2 and len(line_idxs2) + len(sub_idxs2) == 0:
            continue

        obs_act1 = detailed_actions[aid1]["observation"]
        obs_act2 = detailed_actions[aid2]["observation"]

        try:
            result = compute_combined_pair_superposition(
                obs_start=obs_start,
                obs_act1=obs_act1,
                obs_act2=obs_act2,
                act1_line_idxs=line_idxs1,
                act1_sub_idxs=sub_idxs1,
                act2_line_idxs=line_idxs2,
                act2_sub_idxs=sub_idxs2,
                act1_is_pst=action_is_pst.get(aid1, False),
                act2_is_pst=action_is_pst.get(aid2, False),
                act1_is_injection=inj1,
                act2_is_injection=inj2,
            )
        except Exception as e:
            print(f"[Superposition] Error computing pair {aid1}+{aid2}: {e}")
            result = {"error": str(e)}

        if "error" in result:
            print(f"[Superposition] Skipping pair {aid1}+{aid2}: {result['error']}")
            results[f"{aid1}+{aid2}"] = result
            continue

        # ---- Compute combined rho ----
        if use_p_based_rho:
            # Detect if either action involves reconnection or node merging
            # (where delta_theta is the natural base quantity for estimation)
            use_delta_theta = False
            for aid, (line_idxs, sub_idxs) in [(aid1, action_elements[aid1]),
                                                 (aid2, action_elements[aid2])]:
                if line_idxs:
                    # Line action: reconnection if line was disconnected in obs_start
                    if not obs_start.line_status[line_idxs[0]]:
                        use_delta_theta = True
                elif sub_idxs:
                    # Sub action: node merging if sub was split in obs_start
                    if not _is_sub_reference_topology(obs_start, sub_idxs[0]):
                        use_delta_theta = True

            if use_delta_theta:
                # Delta-theta-based: for reconnection/merging pairs
                dt_start_all = _compute_delta_theta_all_lines(obs_start)
                dt_act1_all = _compute_delta_theta_all_lines(obs_act1)
                dt_act2_all = _compute_delta_theta_all_lines(obs_act2)
                betas = np.array(result["betas"])
                dt_combined = ((1.0 - np.sum(betas)) * dt_start_all
                               + betas[0] * dt_act1_all
                               + betas[1] * dt_act2_all)
                rho_combined = _estimate_rho_from_delta_theta(
                    dt_combined, obs_start, obs_act1, obs_act2
                )
            else:
                # P-based: for disconnection/splitting pairs (with action-obs fallback)
                p_or_combined = np.array(result["p_or_combined"])
                p_ex_combined = np.array(result["p_ex_combined"])
                rho_combined = _estimate_rho_from_p(
                    p_or_combined, p_ex_combined, obs_start, obs_act1, obs_act2
                )
        else:
            # Approximate method: superpose rho arrays directly.
            # Lighter but biased: ignores per-extremity asymmetry and
            # the non-linearity introduced by the max() in rho computation.
            rho_combined = np.abs(
                (1.0 - sum(result["betas"])) * obs_start.rho +
                result["betas"][0] * obs_act1.rho +
                result["betas"][1] * obs_act2.rho
            )

        # Rho on overloaded lines
        rho_after = rho_combined[lines_overloaded_ids]
        is_rho_reduction = bool(np.all(rho_after + 0.01 < baseline_rho))

        # Max rho among monitored lines
        worsened_mask = rho_combined > pre_existing_baseline * (1 + worsening_threshold)
        eligible_mask = care_mask & (~is_pre_existing | worsened_mask)

        max_rho = 0.0
        max_rho_line = "N/A"
        if np.any(eligible_mask):
            masked_rho = rho_combined[eligible_mask]
            max_idx = np.argmax(masked_rho)
            max_rho = float(masked_rho[max_idx])
            max_rho_line = name_line[np.where(eligible_mask)[0][max_idx]]

        # Islanding and disconnected MW
        is_islanded = result.get("is_islanded", False)
        disconnected_mw = result.get("disconnected_mw", 0.0)

        # Build descriptions
        desc1 = detailed_actions[aid1].get("description_unitaire", aid1)
        desc2 = detailed_actions[aid2].get("description_unitaire", aid2)

        # Scaling factor for monitoring limits
        # obs_start.rho is computed using monitored thermal limits (permanent * monitoring_factor),
        # so rho_combined inherits that scaling.  Multiply by monitoring_factor to convert back
        # to the permanent-limit reference frame (matching the simulation results).
        monitoring_factor = config.MONITORING_FACTOR_THERMAL_LIMITS

        result.update({
            "max_rho": max_rho * monitoring_factor,
            "max_rho_line": max_rho_line,
            "is_rho_reduction": is_rho_reduction,
            "p_or_combined": result["p_or_combined"],
            "description": f"{desc1} + {desc2}",
            "action1_id": aid1,
            "action2_id": aid2,
            "is_islanded": is_islanded,
            "disconnected_mw": disconnected_mw,
            "rho_after": (rho_combined[lines_overloaded_ids] * monitoring_factor).tolist(),
            "rho_before": (obs_start.rho[lines_overloaded_ids] * monitoring_factor).tolist(),
        })

        # Note: p_or_combined is kept in result for frontend diagram/test consistency

        pair_key = f"{aid1}+{aid2}"
        results[pair_key] = result

        print(f"  {pair_key}: betas={np.round(result['betas'], 4)}, "
              f"max_rho={max_rho:.3f} on {max_rho_line}, "
              f"rho_reduction={is_rho_reduction}")

    print(f"[Superposition] Computed {len(results)} pair combinations")
    return results
