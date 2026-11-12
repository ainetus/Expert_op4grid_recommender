# expert_op4grid_recommender/pypowsybl_backend/action_space.py
"""
Action Space for pypowsybl-based simulation.

Provides a grid2op-compatible interface for creating actions that modify
the network topology (line switching, bus reconfigurations).
"""

import pandas as pd
from typing import Dict, List, Any, Optional, TYPE_CHECKING
from .observation import PypowsyblAction

if TYPE_CHECKING:
    from .network_manager import NetworkManager


class PhaseShifterAction(PypowsyblAction):
    """Action to change PST tap position."""

    def __init__(self, pst_tap: Dict[str, int]):
        """
        Args:
            pst_tap: Dict mapping PST ID -> new tap step.
        """
        super().__init__()
        self.pst_tap = pst_tap

        def apply_pst_changes(nm: 'NetworkManager'):
            for pst_id, tap in self.pst_tap.items():
                nm.update_pst_tap_step(pst_id, tap)

        self._modifications.append(apply_pst_changes)


class LineStatusAction(PypowsyblAction):
    """Action to change line connection status."""
    
    def __init__(self, line_changes: List[tuple]):
        """
        Args:
            line_changes: List of (line_name, status) tuples
                         status: -1 to disconnect, 1 to reconnect
        """
        super().__init__()
        self.line_changes = line_changes
        
        def apply_line_changes(nm: 'NetworkManager'):
            for line_name, status in self.line_changes:
                if status == -1:
                    nm.disconnect_line(line_name)
                elif status == 1:
                    nm.reconnect_line(line_name)
        
        self._modifications.append(apply_line_changes)


def resolve_switch_id(sid: str, switch_ids) -> Optional[str]:
    """Network switch ID for ``sid``, tolerating a ``<VL>_`` prefix; else None.

    Example: PYMONP3_PYMON3COUPL -> PYMON3COUPL
    """
    if sid in switch_ids:
        return sid
    if '_' in sid:
        parts = sid.split('_', 1)
        if parts[1] in switch_ids:
            return parts[1]
    return None


class SwitchAction(PypowsyblAction):
    """Action to change switch states (open/close) for topology changes."""
    
    def __init__(self, switch_states: Dict[str, bool]):
        """
        Args:
            switch_states: Dict mapping switch_id -> open (True=open, False=closed)
        """
        super().__init__()
        self.switch_states = switch_states
        
        def apply_switch_changes(nm: 'NetworkManager'):
            net = nm.network

            # P3: fetch the switch index ONCE per apply (was 1-2 full ~85k-row
            # table fetches per switch id). The set of switches is variant-
            # invariant — only their `open` column changes — so a single read at
            # the top of apply is correct regardless of the active variant.
            switch_ids = set(net.get_switches().index)

            to_open = []
            to_close = []
            for sid, is_open in self.switch_states.items():
                actual_sid = resolve_switch_id(sid, switch_ids)
                if actual_sid:
                    if is_open: to_open.append(actual_sid)
                    else: to_close.append(actual_sid)
                else:
                    print(f"Warning: Switch ID {sid} not found in network (even with prefix stripping)")
            
            if to_open:
                try:
                    net.update_switches(id=to_open, open=[True] * len(to_open))
                except Exception as e:
                    print(f"Warning: Batch open switches failed: {e}")
            
            if to_close:
                try:
                    net.update_switches(id=to_close, open=[False] * len(to_close))
                except Exception as e:
                    print(f"Warning: Batch close switches failed: {e}")
        
        self._modifications.append(apply_switch_changes)


class BusAction(PypowsyblAction):
    """Action to change bus assignments (topology changes)."""
    
    def __init__(self, 
                 lines_or_bus: Optional[Dict[str, int]] = None,
                 lines_ex_bus: Optional[Dict[str, int]] = None,
                 loads_bus: Optional[Dict[str, int]] = None,
                 gens_bus: Optional[Dict[str, int]] = None,
                 substations: Optional[Dict[int, List[int]]] = None,
                 apply_to_network: bool = True):
        """
        Args:
            lines_or_bus: Dict mapping line_name -> bus_number at origin
            lines_ex_bus: Dict mapping line_name -> bus_number at extremity
            loads_bus: Dict mapping load_name -> bus_number
            gens_bus: Dict mapping gen_name -> bus_number
            substations: Dict mapping sub_id -> full topology vector
            apply_to_network: When False, the bus assignments are only carried
                as topology info (for ObservationWithTopologyOverride /
                superposition) and nothing is applied to the network — used
                when a SwitchAction already sets that topology.
        """
        super().__init__()
        self.lines_or_bus = lines_or_bus or {}
        self.lines_ex_bus = lines_ex_bus or {}
        self.loads_bus = loads_bus or {}
        self.gens_bus = gens_bus or {}
        self.substations = substations or {}
        
        def apply_bus_changes(nm: 'NetworkManager'):
            net = nm.network
            
            # Use cached sets from NetworkManager for O(1) membership tests
            lines_set = nm._lines_set
            trafos_set = nm._trafos_set
            
            # 1. Batch line/trafo origin changes (simple connected/disconnected)
            lines_or_disco = [lid for lid, bus in self.lines_or_bus.items() if bus == -1 and lid in lines_set]
            trafos_or_disco = [lid for lid, bus in self.lines_or_bus.items() if bus == -1 and lid in trafos_set]
            lines_or_reco = [lid for lid, bus in self.lines_or_bus.items() if bus >= 1 and lid in lines_set]
            trafos_or_reco = [lid for lid, bus in self.lines_or_bus.items() if bus >= 1 and lid in trafos_set]
            
            if lines_or_disco: net.update_lines(id=lines_or_disco, connected1=[False] * len(lines_or_disco))
            if trafos_or_disco: net.update_2_windings_transformers(id=trafos_or_disco, connected1=[False] * len(trafos_or_disco))
            if lines_or_reco: net.update_lines(id=lines_or_reco, connected1=[True] * len(lines_or_reco))
            if trafos_or_reco: net.update_2_windings_transformers(id=trafos_or_reco, connected1=[True] * len(trafos_or_reco))
            
            # 2. Batch line/trafo extremity changes
            lines_ex_disco = [lid for lid, bus in self.lines_ex_bus.items() if bus == -1 and lid in lines_set]
            trafos_ex_disco = [lid for lid, bus in self.lines_ex_bus.items() if bus == -1 and lid in trafos_set]
            lines_ex_reco = [lid for lid, bus in self.lines_ex_bus.items() if bus >= 1 and lid in lines_set]
            trafos_ex_reco = [lid for lid, bus in self.lines_ex_bus.items() if bus >= 1 and lid in trafos_set]
            
            if lines_ex_disco: net.update_lines(id=lines_ex_disco, connected2=[False] * len(lines_ex_disco))
            if trafos_ex_disco: net.update_2_windings_transformers(id=trafos_ex_disco, connected2=[False] * len(trafos_ex_disco))
            if lines_ex_reco: net.update_lines(id=lines_ex_reco, connected2=[True] * len(lines_ex_reco))
            if trafos_ex_reco: net.update_2_windings_transformers(id=trafos_ex_reco, connected2=[True] * len(trafos_ex_reco))
            
            # 3. Batch load/generator changes
            loads_disco = [lid for lid, bus in self.loads_bus.items() if bus == -1]
            loads_reco = [lid for lid, bus in self.loads_bus.items() if bus >= 1]
            if loads_disco: net.update_loads(id=loads_disco, connected=[False] * len(loads_disco))
            if loads_reco: net.update_loads(id=loads_reco, connected=[True] * len(loads_reco))
            
            gens_disco = [lid for lid, bus in self.gens_bus.items() if bus == -1]
            gens_reco = [lid for lid, bus in self.gens_bus.items() if bus >= 1]
            if gens_disco: net.update_generators(id=gens_disco, connected=[False] * len(gens_disco))
            if gens_reco: net.update_generators(id=gens_reco, connected=[True] * len(gens_reco))

            # 4. Handle full substation topology changes (Grid2Op substations_id / topo_vector)
            # This implements node merging/splitting via coupler manipulation
            # Note: Selector switch toggling is NOT needed for pypowsybl for these experto actions.
            if self.substations:
                all_switches = net.get_switches()
                for sub_id, topo_vector in self.substations.items():
                    try:
                        if sub_id < 0 or sub_id >= len(nm.name_sub):
                            print(f"Warning: Substation ID {sub_id} out of range (max {len(nm.name_sub)-1})")
                            continue
                            
                        sub_name = nm.name_sub[sub_id]
                        
                        # Determine if this is a merge (all connected elements on same bus)
                        connected_buses = set(b for b in topo_vector if b >= 1)
                        is_merge = len(connected_buses) <= 1
        
                        # Filter switches for this substation
                        sub_switches = all_switches[all_switches['voltage_level_id'] == sub_name]
                        
                        # Update Coupler switches (COUPL or TRO in name)
                        # For a merge, these should be CLOSED. For a split, OPENED.
                        coupler_ids = []
                        for sw_id, row in sub_switches.iterrows():
                            sw_name = row['name'] if 'name' in row and pd.notna(row['name']) and row['name'] != "" else sw_id
                            sw_name = str(sw_name).upper()
                            if 'COUPL' in sw_name or 'TRO' in sw_name:
                                coupler_ids.append(sw_id)
                        
                        if coupler_ids:
                            try:
                                # Grid2Op 'merge' means couplers are closed (open=False)
                                net.update_switches(id=coupler_ids, open=[not is_merge] * len(coupler_ids))
                            except Exception as e:
                                print(f"Warning: Coupler update failed for {sub_name}: {e}")
                    except Exception as e:
                        print(f"Warning: Failed to apply substation topology changes for ID {sub_id}: {e}")

        if apply_to_network:
            self._modifications.append(apply_bus_changes)


class PowerReductionAction(PypowsyblAction):
    """Action to reduce active power of loads and/or generators (without disconnecting them).

    Instead of toggling the ``connected`` flag, this action modifies the
    ``target_p`` (for loads) or ``target_p`` (for generators) so the element
    stays electrically connected but its active power injection is changed.
    """

    def __init__(self,
                 loads_p: Optional[Dict[str, float]] = None,
                 gens_p: Optional[Dict[str, float]] = None):
        """
        Args:
            loads_p: Dict mapping load_name -> new target active power (MW).
            gens_p: Dict mapping gen_name -> new target active power (MW).
        """
        super().__init__()
        self.loads_p = loads_p or {}
        self.gens_p = gens_p or {}

        def apply_power_reduction(nm: 'NetworkManager'):
            net = nm.network
            if self.loads_p:
                load_ids = list(self.loads_p.keys())
                target_ps = list(self.loads_p.values())
                net.update_loads(id=load_ids, p0=target_ps)
            if self.gens_p:
                gen_ids = list(self.gens_p.keys())
                target_ps = list(self.gens_p.values())
                net.update_generators(id=gen_ids, target_p=target_ps)

        self._modifications.append(apply_power_reduction)


class ActionSpace:
    """
    Grid2op-compatible action space for pypowsybl networks.
    
    Provides the familiar interface for creating actions:
    - action_space({"set_line_status": [...]})
    - action_space({"set_bus": {...}})
    
    Actions can be combined using the + operator.
    """
    
    def __init__(self, network_manager: 'NetworkManager'):
        """
        Initialize action space.
        
        Args:
            network_manager: The NetworkManager instance
        """
        self._network_manager = network_manager
        
        # Cache line and element information for validation
        self._line_names = set(network_manager.name_line)
        self._sub_names = set(network_manager.name_sub)
        self._gen_names = set(network_manager.name_gen)
        self._load_names = set(network_manager.name_load)
        # Switch index, read on first need (variant-invariant, see SwitchAction).
        self._switch_ids = None

    def _switches_all_resolve(self, switch_states: Dict[str, bool]) -> bool:
        """True when every switch of the action exists in the network."""
        if self._switch_ids is None:
            try:
                self._switch_ids = frozenset(self._network_manager.network.get_switches().index)
            except Exception:
                self._switch_ids = frozenset()
        return all(resolve_switch_id(sid, self._switch_ids) for sid in switch_states)

    def __call__(self, action_dict: Dict[str, Any]) -> PypowsyblAction:
        """
        Create an action from a dictionary specification.
        
        Supports grid2op-style action dictionaries:
        - {"set_line_status": [(line_name, status), ...]}
        - {"set_bus": {"lines_or_id": {name: bus}, "lines_ex_id": {name: bus}, ...}}
        - {"set_bus": {"substations_id": [(sub_id, [topo_vector]), ...]}}
        
        And pypowsybl-specific switch actions:
        - {"switches": {switch_id: open_state, ...}}  # Full switch IDs
        
        Also handles action dicts from conversion_actions_repas:
        - {"content": {"set_bus": {...}}, "switches": {...}}

        When ``switches`` are given and all exist in the network, they alone
        set the topology: ``set_bus`` is then kept as topology info only and
        not applied. Applying it too would replay a bus snapshot that may be
        stale (catalogue built on another topology) and would reconnect any
        element listed with a bus >= 1 — including a tripped contingency
        element. ``set_bus`` is applied as before when there are no switches
        or when some switch is unknown to the network.

        Args:
            action_dict: Dictionary specifying the action
            
        Returns:
            PypowsyblAction that can be applied or simulated
        """
        combined_action = PypowsyblAction()
        
        # Handle nested content structure from conversion_actions_repas
        # set_bus is inside content, switches is at top level
        working_dict = action_dict
        if "content" in action_dict and isinstance(action_dict["content"], dict):
            # Merge content with top-level for set_bus access
            working_dict = {**action_dict, **action_dict["content"]}
        
        # Handle pst_tap (PST ID -> tap step)
        pst_tap = working_dict.get("pst_tap")
        if pst_tap:
            pst_action = PhaseShifterAction(pst_tap)
            combined_action = combined_action + pst_action
        
        # Handle set_line_status
        if "set_line_status" in working_dict:
            line_changes = working_dict["set_line_status"]
            if line_changes:
                line_action = LineStatusAction(line_changes)
                combined_action = combined_action + line_action
        
        # Handle switches (full switch IDs)
        # Check top-level first (new format from conversion_actions_repas)
        # Then check inside working_dict (legacy format)
        switch_states = None
        if "switches" in action_dict:
            switch_states = action_dict["switches"]
        elif "switches" in working_dict:
            switch_states = working_dict["switches"]
        
        if switch_states:
            switch_action = SwitchAction(switch_states)
            combined_action = combined_action + switch_action
        
        # Handle power reduction (set_load_p / set_gen_p)
        loads_p = working_dict.get("set_load_p")
        gens_p = working_dict.get("set_gen_p")
        if loads_p or gens_p:
            power_action = PowerReductionAction(loads_p=loads_p, gens_p=gens_p)
            combined_action = combined_action + power_action

        # Handle set_bus
        if "set_bus" in working_dict:
            bus_config = working_dict["set_bus"]
            
            lines_or_bus = {}
            lines_ex_bus = {}
            loads_bus = {}
            gens_bus = {}
            substations = {}
            
            # Lines origin bus
            if "lines_or_id" in bus_config:
                for name, bus in bus_config["lines_or_id"].items():
                    lines_or_bus[name] = bus
            
            # Lines extremity bus
            if "lines_ex_id" in bus_config:
                for name, bus in bus_config["lines_ex_id"].items():
                    lines_ex_bus[name] = bus
            
            # Loads bus
            if "loads_id" in bus_config:
                for name, bus in bus_config["loads_id"].items():
                    loads_bus[name] = bus
            
            # Generators bus
            if "generators_id" in bus_config:
                for name, bus in bus_config["generators_id"].items():
                    gens_bus[name] = bus
            
            # Full substation topology
            if "substations_id" in bus_config:
                for sub_id, topo_vect in bus_config["substations_id"]:
                    substations[sub_id] = topo_vect
            
            if any([lines_or_bus, lines_ex_bus, loads_bus, gens_bus, substations]):
                bus_action = BusAction(
                    lines_or_bus=lines_or_bus,
                    lines_ex_bus=lines_ex_bus,
                    loads_bus=loads_bus,
                    gens_bus=gens_bus,
                    substations=substations,
                    apply_to_network=not (
                        switch_states and self._switches_all_resolve(switch_states)
                    ),
                )
                combined_action = combined_action + bus_action
        
        return combined_action
    
    def get_do_nothing_action(self) -> PypowsyblAction:
        """Return an action that does nothing."""
        return PypowsyblAction()
    
    # ========== Additional methods for compatibility ==========
    
    @property
    def n_line(self) -> int:
        """Number of lines."""
        return self._network_manager.n_line
    
    @property
    def n_sub(self) -> int:
        """Number of substations."""
        return self._network_manager.n_sub
