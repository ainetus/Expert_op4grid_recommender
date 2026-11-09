# tests/test_expert_op4grid_analyzer.py

import pytest
import os
import sys
import numpy as np
import pandas as pd # Import pandas for MockDistributionGraph
import networkx as nx

# --- Test Setup: Add Project Root to Python path ---
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# --- Mock Objects ---
# (Mock classes: MockAction, MockActionObject, MockActionSpace, MockObservation, MockEnv, MockAlphaDeesp, MockGraph, MockOverflowGraph remain the same as the previous version)
class MockAction:
    def __init__(self, **kwargs):
        defaults = {'line_or_set_bus': [0], 'line_ex_set_bus': [0], 'line_set_status': [0], 'load_set_bus': [0], 'line_or_change_bus': [0], 'line_ex_change_bus': [0], 'line_change_status': [0], 'load_change_bus': [0], '_set_topo_vect': [0], '_topo_vect_to_sub': [0]}
        for key, value in defaults.items(): setattr(self, key, np.array(kwargs.get(key, value)))


class MockActionObject:
    """Mocks the higher-level action object returned by an action_space."""

    def __init__(self, substations_id=None, lines_ex_id=None, lines_or_id=None, set_line_status=None):
        self.substations_id = substations_id or []
        self.lines_ex_id = lines_ex_id or {}
        self.lines_or_id = lines_or_id or {}
        self.set_line_status = set_line_status or []

        # Ensure content reflects all input arguments so .get() returns expected structure
        self.content = {
            'set_line_status': self.set_line_status,
            'set_bus': {
                'substations_id': self.substations_id,
                'lines_ex_id': self.lines_ex_id,
                'lines_or_id': self.lines_or_id
            }
        }

    # Add this method to satisfy the .get() call in the code under test
    def get(self, key, default=None):
        return self.content.get(key, default)

    def __add__(self, other):
        return self  # For tests, a simple return is sufficient.

    def impact_on_objects(self):
        """Mocks the impact_on_objects method, focusing on topology impact."""
        assigned_bus_list = []
        if self.substations_id:
            # Assumes the action affects only one substation for simplicity in mock
            sub_id = self.substations_id[0][0]
            # Create a dummy assignment structure expected by the code
            assigned_bus_list.append({'substation': sub_id})

        return {
            "topology": {
                "assigned_bus": assigned_bus_list
            }
        }

    def as_dict(self):
        return self.content

class MockActionSpace:
    """Mocks the Grid2Op action_space callable."""
    def __call__(self, action_dict):
        set_bus = action_dict.get("set_bus", {})
        # Return MockActionObject containing necessary attributes
        return MockActionObject(
            substations_id=set_bus.get("substations_id"),
            lines_ex_id=set_bus.get("lines_ex_id"),
            lines_or_id=set_bus.get("lines_or_id"),
            set_line_status=action_dict.get("set_line_status") # Pass this through
        )# --- Updated MockObservation ---
class MockObservation:
    """A comprehensive mock for a Grid2Op observation object."""
    def __init__(self, **kwargs):
        self.name_sub = np.array(kwargs.get('name_sub', []))
        self.sub_topologies = kwargs.get('sub_topologies', {})
        self.topo_vect = np.array(kwargs.get('topo_vect', []))
        self.sub_info = np.array(kwargs.get('sub_info', np.ones(len(self.name_sub))))#[k for k, v in sorted(data.items()) for _ in v]
        self.name_line = np.array(kwargs.get('name_line', []))
        num_lines = len(self.name_line)
        self.rho = kwargs.get('rho', np.zeros(num_lines))
        # Add attributes needed for get_delta_theta_line
        self.line_or_to_subid = np.array(kwargs.get('line_or_to_subid', np.zeros(num_lines, dtype=int)))
        self.line_ex_to_subid = np.array(kwargs.get('line_ex_to_subid', np.zeros(num_lines, dtype=int)))
        self.line_or_bus = np.array(kwargs.get('line_or_bus', np.ones(num_lines, dtype=int)))
        self.line_ex_bus = np.array(kwargs.get('line_ex_bus', np.ones(num_lines, dtype=int)))
        self.theta_or = np.array(kwargs.get('theta_or', np.zeros(num_lines)))
        self.theta_ex = np.array(kwargs.get('theta_ex', np.zeros(num_lines)))
        # --- End added attributes ---
        self.line_status = np.ones(num_lines, dtype=bool)
        # Load and generator attributes
        self.name_load = np.array(kwargs.get('name_load', []))
        self.name_gen = np.array(kwargs.get('name_gen', []))
        self.load_to_subid = np.array(kwargs.get('load_to_subid', []), dtype=int) if kwargs.get('load_to_subid') is not None else np.array([], dtype=int)
        self.gen_to_subid = np.array(kwargs.get('gen_to_subid', []), dtype=int) if kwargs.get('gen_to_subid') is not None else np.array([], dtype=int)
        # Optional: pre-computed per-substation element lists for get_obj_connect_to
        self._obj_connect_to = kwargs.get('obj_connect_to', None)

        class MockLoadP:
            def __init__(self, values): self._values = np.array(values if values is not None else [100.0])
            def sum(self): return self._values.sum()
            def __getitem__(self, idx): return self._values[idx]
            def __len__(self): return len(self._values)
        self.load_p = MockLoadP(kwargs.get('load_values'))

        class MockGenP:
            def __init__(self, values): self._values = np.array(values if values is not None else [100.0])
            def sum(self): return self._values.sum()
            def __getitem__(self, idx): return self._values[idx]
            def __len__(self): return len(self._values)
        self.gen_p = MockGenP(kwargs.get('gen_values'))

        # Generator energy source (for renewable curtailment)
        gen_names = kwargs.get('name_gen', [])
        n_gen = len(gen_names)
        gen_es = kwargs.get('gen_energy_sources', ['OTHER'] * n_gen)
        self.gen_energy_source = np.array(gen_es, dtype=object)

        self._simulate_return = kwargs.get('simulate_return')

    def sub_topology(self, sub_id): return np.array(self.sub_topologies.get(sub_id, []))
    def simulate(self, action, time_step): return self._simulate_return or (self, 0.0, False, {"exception": []})
    def __add__(self, action):
        if hasattr(action, 'substations_id') and action.substations_id:
             new_topo = self.topo_vect.copy(); return MockObservation(**{**self.__dict__, 'topo_vect': new_topo}) # Pass existing attrs
        return self
    def get_energy_graph(self): return None
    def get_obj_connect_to(self, substation_id):
         """Return element indices per substation. Uses explicit mapping if provided."""
         if self._obj_connect_to and substation_id in self._obj_connect_to:
             entry = self._obj_connect_to[substation_id]
             return {
                 'loads_id': entry.get('loads_id', []),
                 'generators_id': entry.get('generators_id', []),
                 'lines_or_id': entry.get('lines_or_id', []),
                 'lines_ex_id': entry.get('lines_ex_id', []),
             }
         # Fallback: auto-derive from line_or/ex_to_subid
         lines_or = [i for i, s in enumerate(self.line_or_to_subid) if s == substation_id]
         lines_ex = [i for i, s in enumerate(self.line_ex_to_subid) if s == substation_id]
         loads = [i for i, s in enumerate(self.load_to_subid) if s == substation_id]
         gens = [i for i, s in enumerate(self.gen_to_subid) if s == substation_id]
         return {'loads_id': loads, 'generators_id': gens,
                 'lines_or_id': lines_or, 'lines_ex_id': lines_ex}

class MockEnv:
    def __init__(self, name_line=None, maintenance_array=None, name_sub=None):
        self.name_line = name_line if name_line is not None else ["L1", "L2", "L3"]; self.name_sub = name_sub if name_sub is not None else ["S1", "S2"]; maint_arr = maintenance_array if maintenance_array is not None else np.zeros((2, len(self.name_line)), dtype=bool)
        self.chronics_handler = type('Chronics', (), {'real_data': type('RealData', (), {'data': type('Data', (), {'maintenance_handler': type('Maintenance', (), {'array': maint_arr})()})()})()})(); self.action_space = MockActionSpace()
    def get_obs(self): return MockObservation(name_line=self.name_line, name_sub=self.name_sub)

class MockAlphaDeesp:
    def rank_current_topo_at_node_x(self, g, sub_id, **kwargs): return 1.0 if sub_id == 0 else 0.5

class MockGraph(nx.MultiDiGraph):
    def __init__(self, edge_data=None, **attr):
        super().__init__(**attr)
        if edge_data:
            for (u, v), data in edge_data.items():
                # Check if data is a dictionary of attributes or a dictionary of edge_ids
                # If 'data' has integer keys (like 0, 1), it's likely {edge_key: {attributes}}

                if isinstance(data, dict):
                    for key, attributes in data.items():
                        if isinstance(key, int) and isinstance(attributes, dict):
                            # Case: {0: {'name': 'L1'}} -> key=0 is the edge_key (multigraph index)
                            self.add_edge(u, v, key=key, **attributes)
                        else:
                            # Case: {'weight': 10} -> standard attributes
                            # We can't unpack 'data' directly if it has int keys, so we check carefully
                            self.add_edge(u, v, **data)
                            break  # Stop loop if we treated 'data' as flat attributes
                else:
                    self.add_edge(u, v)

class MockOverflowGraph:
    def __init__(self, name_line=None,edge_data=None):
        self.g = MockGraph(edge_data=edge_data)

class MockDistributionGraph:
    def __init__(self): self.red_loops = pd.DataFrame(columns=["Path"])
    def get_dispatch_edges_nodes(self, only_loop_paths=False): return [], []
    def get_constrained_edges_nodes(self): return [], [], [], []

from expert_op4grid_recommender.action_evaluation.discovery import ActionDiscoverer
from expert_op4grid_recommender.action_evaluation.classifier import ActionClassifier
## Section 3: ActionDiscoverer Internal Method Tests ##
# These tests now use an instance of ActionDiscoverer

# Fixture to create a discoverer instance with basic mocks
@pytest.fixture
def basic_discoverer():
    mock_obs = MockObservation(name_sub=["A", "B", "C", "D"], name_line=["L1", "L2", "L3"], line_or_to_subid=[0, 1, 2], line_ex_to_subid=[1, 2, 3])
    mock_env = MockEnv(name_line=mock_obs.name_line, name_sub=mock_obs.name_sub)
    mock_g_overflow = MockOverflowGraph(edge_data={ (0, 1): {0:{"name":"L1"}}, (1, 2): {0:{"name":"L2"}} }) # Edges A-B, B-C
    mock_g_dist = MockDistributionGraph()
    return ActionDiscoverer( env=mock_env, obs=mock_obs,classifier=ActionClassifier(MockActionSpace()), obs_defaut=mock_obs, timestep=0, lines_defaut=[], lines_overloaded_ids=[], act_reco_maintenance=MockActionObject(), non_connected_reconnectable_lines=[], all_disconnected_lines=[], dict_action={}, actions_unfiltered=set(), hubs=[], g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist, simulator_data={})

def test_internal_is_sublist(basic_discoverer):
    assert basic_discoverer._is_sublist(["B", "C"], ["A", "B", "C", "D"]) is True
    assert basic_discoverer._is_sublist(["C", "A"], ["A", "B", "C", "D"]) is False

def test_internal_get_line_substations(basic_discoverer):
    assert basic_discoverer._get_line_substations("L2") == ("B", "C")

def test_internal_find_paths_for_line(basic_discoverer):
    line_subs = ("A", "B")
    paths = [["A", "B", "C"], ["B", "C", "D"], ["C", "D", "E"]]
    assert basic_discoverer._find_paths_for_line(line_subs, paths) == [["A", "B", "C"]]

def test_internal_get_active_edges_between(basic_discoverer):
    # Update discoverer's graph mock for this test
    basic_discoverer.g_overflow = MockOverflowGraph(edge_data={ (0, 1): {0: {"name": "L1"}, 1: {"name": "L1_dashed", "style": "dashed"}}, (1, 0): {0: {"name": "L1_rev"}} }) # A-B edges
    active = basic_discoverer._get_active_edges_between("A", "B")
    assert "L1" in active
    assert "L1_rev" in active
    assert "L1_dashed" not in active

def test_internal_has_blocking_disconnected_line(basic_discoverer):
    basic_discoverer.all_disconnected_lines = ["L2"]
    # Graph has A-B edge, but no B-C edge implicitly
    basic_discoverer.g_overflow = MockOverflowGraph(edge_data={ (0, 1): {0:{"name":"L1"}} })
    blocked, blocker = basic_discoverer._has_blocking_disconnected_line(["A", "B", "C"], "L1")
    assert blocked is True and blocker == "L2"
    # Now add a parallel active edge for L2 (B-C)
    basic_discoverer.g_overflow = MockOverflowGraph(edge_data={ (0, 1): {0:{"name":"L1"}}, (1,2): {0:{"name":"L3"}} })
    blocked, blocker = basic_discoverer._has_blocking_disconnected_line(["A", "B", "C"], "L1")
    assert blocked is False and blocker is None

def test_internal_check_other_reconnectable_line(basic_discoverer):
    basic_discoverer.all_disconnected_lines = ["L2"]
    # Path A-B-C exists, L1 connects A-B, L2 connects B-C
    # Case 1: No parallel edge for L2 -> blocked
    basic_discoverer.g_overflow = MockOverflowGraph(edge_data={ (0, 1): {0:{"name":"L1"}}})
    has_path,path, blocker = basic_discoverer._check_other_reconnectable_line_on_path("L1", [["A", "B", "C"]])
    assert has_path is False and blocker == "L2"
    # Case 2: Add parallel edge L3 for L2 -> not blocked
    basic_discoverer.g_overflow = MockOverflowGraph(edge_data={ (0, 1): {0:{"name":"L1"}}, (1,2): {0:{"name":"L3"}} })
    has_path,path, blocker = basic_discoverer._check_other_reconnectable_line_on_path("L1", [["A", "B", "C"]])
    assert has_path is True and blocker is None

## Section 4: Discovery Class Method Unit Tests (using fixture) ##

# Mock for rho reduction checks specific to discovery tests
def mock_rho_reduction_discovery(obs, timestep, act_defaut, action, *args, **kwargs):
    """Predictable mock: effective if action involves Sub0, disconnects L1, or reconnects L1."""
    is_effective = False
    # Check for node merge/split of Sub0
    if hasattr(action, 'substations_id') and action.substations_id and action.substations_id[0][0] == 0:
        is_effective = True
    # Check for disconnection of L1
    elif hasattr(action, 'lines_ex_id') and action.lines_ex_id.get("L1") == -1:
         is_effective = True
    elif hasattr(action, 'lines_or_id') and action.lines_or_id.get("L1") == -1:
         is_effective = True
    # Check for reconnection of L1
    #elif hasattr(action, 'set_line_status') and action.set_line_status == [("L1", 1)]:
    elif hasattr(action, 'lines_ex_id') and action.lines_ex_id.get("L1") == 1:
         if hasattr(action, 'lines_or_id') and action.lines_or_id.get("L1") == 1:
            is_effective = True

    return is_effective, obs

def mock_compute_node_splitting_action_score_value(overflow_graph, g_distribution_graph, node: int,
                                                  dict_edge_names_buses=None): return 1.0 if node == 0 else 0.5

def mock_edge_names_buses_dict(obs, action_topo_vect, sub_impacted_id): return {line_name:1 for line_name in obs.name_line}

@pytest.fixture
def discoverer_instance(monkeypatch):
    """Provides a configured ActionDiscoverer instance with mocks for testing discovery methods."""
    # NOTE: discovery was split into a package; `_default_check_rho_reduction` now lives in
    # the `_base` submodule and `AlphaDeesp_warmStart` in the `_node_splitting` submodule.
    # Monkeypatching at the package level does not rebind the mixin-module globals that the
    # method bodies actually resolve against, so we target the submodules directly.
    monkeypatch.setattr("expert_op4grid_recommender.action_evaluation.discovery._base._default_check_rho_reduction", mock_rho_reduction_discovery)
    monkeypatch.setattr("expert_op4grid_recommender.utils.simulation.create_default_action", lambda *args: MockActionObject())
    monkeypatch.setattr("expert_op4grid_recommender.action_evaluation.discovery._node_splitting.AlphaDeesp_warmStart", lambda *args: MockAlphaDeesp())
    #def mock_id(desc, **kwargs): return desc.get("type", "unknown")
    #monkeypatch.setattr("expert_op4grid_recommender.action_evaluation.classifier.identify_action_type", mock_id)
    def mock_sort(map_score):
        items = sorted(map_score.items(), key=lambda item: item[1]['score'], reverse=True)
        return {k: v['action'] for k,v in items}, [v.get('sub_impacted') or v.get('line_impacted') for k,v in items], [v['score'] for k,v in items]
    monkeypatch.setattr("expert_op4grid_recommender.utils.helpers.sort_actions_by_score", mock_sort)

    name_line=["L1", "L2"]
    mock_obs = MockObservation(name_sub=["Sub0", "Sub1", "Sub3"], name_line=name_line, sub_topologies={0: [1, 2, 2], 1: [1, 1], 2: [2, 1, 2]}, sub_info=[3,2,3], topo_vect=np.array([1,2,1, 1,1, 2,1,1]))
    mock_env = MockEnv(name_line=mock_obs.name_line, name_sub=mock_obs.name_sub)
    mock_g_overflow = MockOverflowGraph(edge_data={ (0, 1): {0:{"name":"L1","capacity":1}}, (1, 2): {0:{"name":"L2","capacity":2}} })
    mock_g_dist = MockDistributionGraph()

    discoverer = ActionDiscoverer( env=mock_env, obs=mock_obs, obs_defaut=mock_obs,classifier=ActionClassifier(MockActionSpace()),
                                   timestep=0, lines_defaut=[], lines_overloaded_ids=[0], act_reco_maintenance=MockActionObject(), non_connected_reconnectable_lines=["L1"], all_disconnected_lines=[],
                                   dict_action={
                                       "reco_L1": {
                                           "type": "close_line",
                                           "description_unitaire": "Fermeture L1",  # Added
                                           "content": {"set_bus": {"lines_ex_id": {"L1": 1}},
                                                       "lines_or_id": {"L1": 1}}},#{"set_line_status": [("L1", 1)], "set_bus": {}}},
                                       "disco_L1": {
                                           "type": "open_line",
                                           "description_unitaire": "Ouverture L1",  # Added
                                           "content": {"set_bus": {"lines_ex_id": {"L1": -1}}}},
                                       "merge_S0": {
                                           "type": "close_coupling",
                                           "description_unitaire": "Fermeture COUPL S0",  # Added
                                           "content": {"set_bus": {"substations_id": [(0, [1, 1, 1])]}}},
                                       "split_S0": {
                                           "type": "open_coupling",
                                           "description_unitaire": "Ouverture COUPL S0",  # Added
                                           "content": {"set_bus": {"substations_id": [(0, [1, 2, 2])]}}},
                                       "split_S1": {
                                           "type": "open_coupling",
                                           "description_unitaire": "Ouverture COUPL S1",  # Added
                                           "content": {"set_bus": {"substations_id": [(1, [1, 2])]}}},
                                       "disco_L2": {
                                           "type": "open_line",
                                           "description_unitaire": "Ouverture L2",  # Added
                                           "content": {"set_bus": {"lines_ex_id": {"L2": -1}}}},
                                   },
                      actions_unfiltered={"reco_L1", "disco_L1", "merge_S0", "split_S0", "split_S1", "disco_L2"}, hubs=["Sub0"], g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist, simulator_data={}, check_action_simulation=True )

    monkeypatch.setattr(discoverer, '_edge_names_buses_dict', mock_edge_names_buses_dict)
    monkeypatch.setattr(discoverer, 'compute_node_splitting_action_score_value', mock_compute_node_splitting_action_score_value)
    return discoverer

def test_discoverer_find_relevant_node_merging(discoverer_instance):
    discoverer_instance.find_relevant_node_merging(["Sub0", "Sub1", "Sub3"])
    assert len(discoverer_instance.identified_merges) == 2
    assert len(discoverer_instance.effective_merges) == 1
    assert discoverer_instance.effective_merges[0].substations_id[0][0] == 0
    # Verify scores dict is populated for identified merges
    assert len(discoverer_instance.scores_merges) == 2
    for action_id, score in discoverer_instance.scores_merges.items():
        assert isinstance(score, float)

def test_node_merging_identified_sorted_by_score(discoverer_instance, monkeypatch):
    """Regression: identified merges come out score-descending, not in
    dispatch-path order, since the MIN_CLOSE_COUPLING floor takes the first N."""
    # Score grows with the substation index, so the first substation met on the
    # dispatch path gets the lowest score: insertion order != score order.
    monkeypatch.setattr(
        discoverer_instance, "compute_node_merging_score",
        lambda sub_id, buses: (float(sub_id), {}),
    )
    discoverer_instance.find_relevant_node_merging(["Sub0", "Sub1", "Sub3"])

    action_ids = list(discoverer_instance.identified_merges)
    scores = [discoverer_instance.scores_merges[aid] for aid in action_ids]
    assert len(action_ids) == 2
    assert scores == sorted(scores, reverse=True)
    assert scores[0] > scores[1]

def test_discoverer_verify_relevant_reconnections(discoverer_instance, monkeypatch):
    def mock_path_check(*args): return True, ["Sub0", "Sub1", "Sub3"], None # Assume path is always clear
    monkeypatch.setattr(discoverer_instance, "_check_other_reconnectable_line_on_path", mock_path_check)
    discoverer_instance.verify_relevant_reconnections(lines_to_reconnect={"L1"}, red_loop_paths=[])
    assert "reco_L1" in discoverer_instance.identified_reconnections
    assert "L1" in discoverer_instance.effective_reconnections
    # Verify scores dict is populated
    assert "reco_L1" in discoverer_instance.scores_reconnections
    assert isinstance(discoverer_instance.scores_reconnections["reco_L1"], float)

def test_discoverer_find_relevant_disconnections(discoverer_instance):
    discoverer_instance.find_relevant_disconnections(lines_constrained_path_names=["L1"])
    assert "disco_L1" in discoverer_instance.identified_disconnections
    assert "disco_L1" in discoverer_instance.effective_disconnections
    assert "disco_L2" not in discoverer_instance.identified_disconnections # Not in constrained path
    # Verify scores dict is populated for identified disconnections
    assert "disco_L1" in discoverer_instance.scores_disconnections
    assert isinstance(discoverer_instance.scores_disconnections["disco_L1"], float)
    # disco_L2 should not be scored (not on constrained path)
    assert "disco_L2" not in discoverer_instance.scores_disconnections


def test_disconnections_sorted_by_score_descending(discoverer_instance, monkeypatch):
    """identified_disconnections must be ordered highest-score-first after find_relevant_disconnections."""
    # Add two extra open_line actions both touching constrained line L1
    discoverer_instance.dict_action["disco_L1_A"] = {
        "type": "open_line",
        "description_unitaire": "Ouverture L1 variant A",
        "content": {"set_bus": {"lines_ex_id": {"L1": -1}}},
    }
    discoverer_instance.dict_action["disco_L1_B"] = {
        "type": "open_line",
        "description_unitaire": "Ouverture L1 variant B",
        "content": {"set_bus": {"lines_or_id": {"L1": -1}}},
    }
    discoverer_instance.actions_unfiltered |= {"disco_L1_A", "disco_L1_B"}

    # Actions are processed in sorted(actions_unfiltered) alphabetical order.
    # Among the three on-path open_line actions the alphabetical order is:
    #   disco_L1 (1st), disco_L1_A (2nd), disco_L1_B (3rd).
    # Assign scores [0.1, 0.9, 0.4] in that processing order so the expected
    # descending sort is: disco_L1_A (0.9) > disco_L1_B (0.4) > disco_L1 (0.1),
    # which differs from alphabetical order and thus truly exercises the sort.
    score_iter = iter([0.1, 0.9, 0.4])
    monkeypatch.setattr(discoverer_instance, "compute_disconnection_score", lambda lines: next(score_iter))

    discoverer_instance.find_relevant_disconnections(lines_constrained_path_names=["L1"])

    keys = list(discoverer_instance.identified_disconnections.keys())
    assert len(keys) == 3, f"Expected 3 identified disconnections, got {keys}"
    assert keys[0] == "disco_L1_A", f"Highest-score action should be first, got {keys}"
    assert keys[1] == "disco_L1_B", f"Second action should be disco_L1_B, got {keys}"
    assert keys[2] == "disco_L1", f"Lowest-score action should be last, got {keys}"


def test_disconnections_identified_order_matches_scores_order(discoverer_instance, monkeypatch):
    """The insertion order of identified_disconnections must match descending scores_disconnections."""
    # Add one more disco action on L1 so we have two to compare.
    # Alphabetical processing order: disco_L1 (1st), disco_L1_high (2nd).
    # Assign 0.2 then 0.8 so disco_L1_high ends up first after sorting.
    discoverer_instance.dict_action["disco_L1_high"] = {
        "type": "open_line",
        "description_unitaire": "Ouverture L1 high score",
        "content": {"set_bus": {"lines_ex_id": {"L1": -1}}},
    }
    discoverer_instance.actions_unfiltered |= {"disco_L1_high"}

    score_iter = iter([0.2, 0.8])
    monkeypatch.setattr(discoverer_instance, "compute_disconnection_score", lambda lines: next(score_iter))

    discoverer_instance.find_relevant_disconnections(lines_constrained_path_names=["L1"])

    identified_keys = list(discoverer_instance.identified_disconnections.keys())
    scores = [discoverer_instance.scores_disconnections[k] for k in identified_keys]
    assert scores == sorted(scores, reverse=True), (
        f"identified_disconnections keys are not in descending score order: "
        f"{list(zip(identified_keys, scores))}"
    )


def test_discoverer_find_relevant_node_splitting(discoverer_instance):
    discoverer_instance.find_relevant_node_splitting(hubs_names=["Sub0"], nodes_blue_path_names=["Sub1"])
    assert "split_S0" in discoverer_instance.identified_splits
    assert "split_S1" in discoverer_instance.identified_splits
    assert len(discoverer_instance.effective_splits) == 1
    assert discoverer_instance.effective_splits[0].substations_id[0][0] == 0
    assert discoverer_instance.scores_splits[0] >= discoverer_instance.scores_splits[1] # Check sort order
    # Verify scores dict is populated
    assert "split_S0" in discoverer_instance.scores_splits_dict
    assert "split_S1" in discoverer_instance.scores_splits_dict
    assert discoverer_instance.scores_splits_dict["split_S0"] == 1.0
    assert discoverer_instance.scores_splits_dict["split_S1"] == 0.5


# --- Add these new tests to your test_expert_op4grid_analyzer.py file ---

def test_internal_compute_node_splitting_action_score(discoverer_instance):
    """
    Dedicated test for the internal _compute_node_splitting_action_score method.
    Verifies that the score calculation logic works correctly based on mocks.
    """
    # Use the discoverer_instance fixture which already has mocks set up
    discoverer = discoverer_instance
    alpha_ranker = MockAlphaDeesp() # We know its behavior

    # Mock an action targeting Sub0 (index 0)
    action_sub0 = MockActionObject(substations_id=[(0, [1, 2])]) # Splitting action
    # Mock an action targeting Sub1 (index 1)
    action_sub1 = MockActionObject(substations_id=[(1, [1, 1])]) # Non-splitting/irrelevant action content

    # --- Test Case 1: Substation 0 (should get score 1.0 from MockAlphaDeesp) ---
    # Manually set up obs_defaut to ensure sub_info and topo_vect are suitable
    discoverer.obs_defaut = MockObservation(name_sub=["Sub0", "Sub1"], sub_info=[2, 2], topo_vect=[1, 1, 1, 1])
    score0, details0 = discoverer.compute_node_splitting_action_score(action_sub0, 0, alpha_ranker)
    assert score0 == 1.0, "Score for Sub0 should be 1.0 based on mock ranker"
    assert isinstance(details0, dict)

    # --- Test Case 2: Substation 1 (should get score 0.5 from MockAlphaDeesp) ---
    discoverer.obs_defaut = MockObservation(name_sub=["Sub0", "Sub1"], sub_info=[2, 2], topo_vect=[1, 1, 1, 1])
    score1, details1 = discoverer.compute_node_splitting_action_score(action_sub1, 1, alpha_ranker)
    assert score1 == 0.5, "Score for Sub1 should be 0.5 based on mock ranker"
    assert isinstance(details1, dict)

def test_internal_identify_and_score_node_splitting_actions(discoverer_instance, monkeypatch):
    """
    Dedicated test for the internal _identify_and_score_node_splitting_actions method.
    Verifies action identification, relevance check (hubs/blue path), and scoring.
    """
    discoverer = discoverer_instance
    alpha_ranker = MockAlphaDeesp()

    # Override dict_action and unfiltered actions for this specific test
    discoverer.dict_action["split_S2"]={"type": "open_coupling",
                                           "description_unitaire": "Ouverture COUPL S2",  # Added
                                           "content": {"set_bus": {"substations_id": [(2, [1, 2])]}}}
    #discoverer.dict_action = {
    #    "split_S0": {"type": "open_coupling", "content": {"set_bus": {"substations_id": [(0, [1, 2])]}}}, # Relevant (Hub)
    #    "split_S1": {"type": "open_coupling", "content": {"set_bus": {"substations_id": [(1, [1, 2])]}}}, # Relevant (Blue Path)
    #    "split_S2": {"type": "open_coupling", "content": {"set_bus": {"substations_id": [(2, [1, 2])]}}}, # Irrelevant
    #    "merge_S0": {"type": "close_coupling", "content": {"set_bus": {"substations_id": [(0, [1, 1])]}}}, # Wrong type
    #}
    discoverer.actions_unfiltered = set(discoverer.dict_action.keys()) # Assume all passed initial filter

    # Define hubs and blue path for relevance check
    hubs_names = ["Sub0"]
    nodes_blue_path_names = ["Sub1"]

    # Mock obs_defaut needed for name lookup
    discoverer.obs_defaut = MockObservation(name_sub=["Sub0", "Sub1", "Sub2"])

    # --- Execute the method under test ---
    map_score, ignored = discoverer.identify_and_score_node_splitting_actions(
        hubs_names, nodes_blue_path_names, alpha_ranker
    )

    # --- Assertions ---
    # Check identified and scored actions
    assert len(map_score) == 2, "Should identify 2 relevant splitting actions"
    assert "split_S0" in map_score
    assert "split_S1" in map_score
    assert map_score["split_S0"]["score"] == 1.0 # From mock ranker for sub_id 0
    assert map_score["split_S0"]["sub_impacted"] == "Sub0"
    assert map_score["split_S1"]["score"] == 0.5 # From mock ranker for sub_id 1
    assert map_score["split_S1"]["sub_impacted"] == "Sub1"

    # Check ignored actions
    assert len(ignored) == 5, "Should ignore 5 actions"
    ignored_ids = {k for k, v in discoverer.dict_action.items() if v in ignored}
    assert "reco_L1" in ignored_ids
    assert "disco_L1" in ignored_ids
    assert "disco_L2" in ignored_ids
    assert "merge_S0" in ignored_ids # Ignored because it's not "open_coupling" type
    assert "split_S2" in ignored_ids #Ignored because not on constrained path


# =============================================================================
# Tests for _get_subs_impacted_from_action_desc (new backend-agnostic method)
# =============================================================================

class TestGetSubsImpactedFromActionDesc:
    """Tests for the _get_subs_impacted_from_action_desc method in ActionDiscoverer."""
    
    @pytest.fixture
    def discoverer_for_subs_impacted(self):
        """Create a discoverer with proper observation setup for testing."""
        mock_obs = MockObservation(
            name_sub=["Sub0", "Sub1", "Sub2", "Sub3"],
            name_line=["L1", "L2", "L3"],
            line_or_to_subid=[0, 1, 2],  # L1->Sub0, L2->Sub1, L3->Sub2
            line_ex_to_subid=[1, 2, 3]   # L1->Sub1, L2->Sub2, L3->Sub3
        )
        mock_env = MockEnv(name_line=mock_obs.name_line, name_sub=mock_obs.name_sub)
        mock_g_overflow = MockOverflowGraph()
        mock_g_dist = MockDistributionGraph()
        
        discoverer = ActionDiscoverer(
            env=mock_env,
            obs=mock_obs,
            obs_defaut=mock_obs,
            classifier=ActionClassifier(MockActionSpace()),
            timestep=0,
            lines_defaut=[],
            lines_overloaded_ids=[],
            act_reco_maintenance=MockActionObject(),
            non_connected_reconnectable_lines=[],
            all_disconnected_lines=[],
            dict_action={},
            actions_unfiltered=set(),
            hubs=[],
            g_overflow=mock_g_overflow,
            g_distribution_graph=mock_g_dist,
            simulator_data={}
        )
        return discoverer
    
    def test_substations_id_direct(self, discoverer_for_subs_impacted):
        """Test extraction from substations_id (direct substation topology changes)."""
        action_desc = {
            "content": {
                "set_bus": {
                    "substations_id": [(0, [1, 2, 1]), (2, [1, 1])]
                }
            }
        }
        
        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)
        
        assert set(subs) == {0, 2}
    
    def test_lines_or_id(self, discoverer_for_subs_impacted):
        """Test extraction from lines_or_id (line origin bus changes)."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_or_id": {"L1": 1, "L2": 2}
                }
            }
        }
        
        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)
        
        # L1 origin is Sub0, L2 origin is Sub1
        assert set(subs) == {0, 1}
    
    def test_lines_ex_id(self, discoverer_for_subs_impacted):
        """Test extraction from lines_ex_id (line extremity bus changes)."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_ex_id": {"L1": -1, "L3": 1}
                }
            }
        }
        
        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)
        
        # L1 extremity is Sub1, L3 extremity is Sub3
        assert set(subs) == {1, 3}
    
    def test_combined_lines_or_and_ex(self, discoverer_for_subs_impacted):
        """Test extraction from both lines_or_id and lines_ex_id."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_or_id": {"L1": 1},
                    "lines_ex_id": {"L2": 2}
                }
            }
        }
        
        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)
        
        # L1 origin is Sub0, L2 extremity is Sub2
        assert set(subs) == {0, 2}
    
    def test_empty_action_desc(self, discoverer_for_subs_impacted):
        """Test with empty action description."""
        action_desc = {"content": {}}
        
        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)
        
        assert subs == []
    
    def test_empty_set_bus(self, discoverer_for_subs_impacted):
        """Test with empty set_bus dictionary."""
        action_desc = {"content": {"set_bus": {}}}
        
        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)
        
        assert subs == []
    
    def test_unknown_line_name(self, discoverer_for_subs_impacted):
        """Test with unknown line name (should be skipped)."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_or_id": {"UNKNOWN_LINE": 1, "L1": 1}
                }
            }
        }
        
        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)
        
        # Should only include Sub0 from L1, ignore unknown line
        assert set(subs) == {0}
    
    def test_all_fields_combined(self, discoverer_for_subs_impacted):
        """Test with all possible fields combined."""
        action_desc = {
            "content": {
                "set_bus": {
                    "substations_id": [(3, [1, 1])],
                    "lines_or_id": {"L1": 1},
                    "lines_ex_id": {"L2": 2}
                }
            }
        }
        
        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)
        
        # Sub3 from substations_id, Sub0 from L1 origin, Sub2 from L2 extremity
        assert set(subs) == {0, 2, 3}
    
    def test_duplicate_substations_deduplicated(self, discoverer_for_subs_impacted):
        """Test that duplicate substations are deduplicated."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_or_id": {"L1": 1},  # Sub0
                    "lines_ex_id": {"L1": 1}   # Sub1
                }
            }
        }

        subs = discoverer_for_subs_impacted._get_subs_impacted_from_action_desc(action_desc)

        # Should be deduplicated (using set internally)
        assert len(subs) == len(set(subs))


# =============================================================================
# Tests for disconnection scoring (_asymmetric_bell_score, compute_disconnection_score)
# =============================================================================

class TestAsymmetricBellScore:
    """Tests for the static _asymmetric_bell_score method."""

    def test_zero_at_min_boundary(self):
        """Score should be zero at the min_flow boundary."""
        score = ActionDiscoverer._asymmetric_bell_score(10.0, 10.0, 50.0)
        assert score == 0.0

    def test_zero_at_max_boundary(self):
        """Score should be zero at the max_flow boundary."""
        score = ActionDiscoverer._asymmetric_bell_score(50.0, 10.0, 50.0)
        assert score == 0.0

    def test_positive_inside_range(self):
        """Score should be positive between min and max."""
        score = ActionDiscoverer._asymmetric_bell_score(30.0, 10.0, 50.0)
        assert score > 0.0

    def test_peak_closer_to_max(self):
        """Score should be higher closer to max_flow than to min_flow."""
        # With default alpha=3.0, beta=1.5, peak is at x=0.8
        score_low = ActionDiscoverer._asymmetric_bell_score(20.0, 10.0, 50.0)   # x=0.25
        score_high = ActionDiscoverer._asymmetric_bell_score(40.0, 10.0, 50.0)  # x=0.75
        assert score_high > score_low

    def test_negative_below_min(self):
        """Score should be negative below min_flow."""
        score = ActionDiscoverer._asymmetric_bell_score(5.0, 10.0, 50.0)
        assert score < 0.0

    def test_negative_above_max(self):
        """Score should be negative above max_flow."""
        score = ActionDiscoverer._asymmetric_bell_score(60.0, 10.0, 50.0)
        assert score < 0.0

    def test_increasingly_negative_further_from_range(self):
        """Score should become more negative the further from the range."""
        score_close = ActionDiscoverer._asymmetric_bell_score(8.0, 10.0, 50.0)
        score_far = ActionDiscoverer._asymmetric_bell_score(0.0, 10.0, 50.0)
        assert score_far < score_close < 0.0

    def test_degenerate_range_returns_zero(self):
        """Score should be zero when max <= min (no valid range)."""
        assert ActionDiscoverer._asymmetric_bell_score(5.0, 10.0, 10.0) == 0.0
        assert ActionDiscoverer._asymmetric_bell_score(5.0, 10.0, 5.0) == 0.0

    def test_peak_value_normalized_to_one(self):
        """Peak score should be approximately 1.0."""
        # Peak is at x=0.8 for alpha=3.0, beta=1.5 → observed_flow = 10 + 0.8*40 = 42
        score_peak = ActionDiscoverer._asymmetric_bell_score(42.0, 10.0, 50.0)
        assert abs(score_peak - 1.0) < 0.01


class TestUnconstrainedLinearScore:
    """Tests for _unconstrained_linear_score (disconnection scoring when no new overloads arise)."""

    def test_one_at_max_flow(self):
        """Score should be exactly 1.0 at max_flow."""
        assert ActionDiscoverer._unconstrained_linear_score(100.0, 20.0, 100.0) == 1.0

    def test_zero_at_min_flow(self):
        """Score should be exactly 0.0 at min_flow."""
        assert ActionDiscoverer._unconstrained_linear_score(20.0, 20.0, 100.0) == 0.0

    def test_linear_in_between(self):
        """Score should be linear between min and max (midpoint = 0.5)."""
        score = ActionDiscoverer._unconstrained_linear_score(60.0, 20.0, 100.0)
        assert abs(score - 0.5) < 1e-9

    def test_negative_below_min(self):
        """Score should be negative below min_flow."""
        score = ActionDiscoverer._unconstrained_linear_score(10.0, 20.0, 100.0)
        assert score < 0.0

    def test_capped_at_one_above_max(self):
        """Score should be capped at 1.0 even beyond max_flow."""
        score = ActionDiscoverer._unconstrained_linear_score(120.0, 20.0, 100.0)
        assert score == 1.0

    def test_degenerate_range_returns_zero(self):
        """Should return 0 when max_flow <= min_flow."""
        assert ActionDiscoverer._unconstrained_linear_score(50.0, 50.0, 50.0) == 0.0
        assert ActionDiscoverer._unconstrained_linear_score(50.0, 60.0, 50.0) == 0.0

    def test_increasingly_negative_further_below_min(self):
        """Score should become more negative the further below min_flow."""
        s1 = ActionDiscoverer._unconstrained_linear_score(15.0, 20.0, 100.0)
        s2 = ActionDiscoverer._unconstrained_linear_score(0.0, 20.0, 100.0)
        assert s2 < s1 < 0.0


class TestComputeDisconnectionFlowBounds:
    """Tests for _compute_disconnection_flow_bounds with obs_linecut fix (issue #30)."""

    def _make_discoverer(self, rho_defaut, rho_linecut=None, lines_overloaded_ids=None):
        """Helper to build a discoverer with controlled rho values."""
        n_lines = len(rho_defaut)
        name_line = [f"L{i}" for i in range(n_lines)]
        name_sub = [f"S{i}" for i in range(n_lines + 1)]

        mock_obs_defaut = MockObservation(
            name_sub=name_sub,
            name_line=name_line,
            rho=np.array(rho_defaut, dtype=float),
            line_or_to_subid=list(range(n_lines)),
            line_ex_to_subid=list(range(1, n_lines + 1)),
        )
        mock_obs_linecut = None
        if rho_linecut is not None:
            mock_obs_linecut = MockObservation(
                name_sub=name_sub,
                name_line=name_line,
                rho=np.array(rho_linecut, dtype=float),
                line_or_to_subid=list(range(n_lines)),
                line_ex_to_subid=list(range(1, n_lines + 1)),
            )

        mock_env = MockEnv(name_line=name_line, name_sub=name_sub)
        # Overflow graph: edge 0->1 named L0 with capacity 100, edge 1->2 named L1 with capacity 50
        edge_data = {}
        for i in range(min(n_lines, 2)):
            edge_data[(i, i + 1)] = {0: {"name": f"L{i}", "capacity": 100.0 / (i + 1)}}
        mock_g_overflow = MockOverflowGraph(edge_data=edge_data)
        mock_g_dist = MockDistributionGraph()

        return ActionDiscoverer(
            env=mock_env,
            obs=mock_obs_defaut,
            obs_defaut=mock_obs_defaut,
            obs_linecut=mock_obs_linecut,
            classifier=ActionClassifier(MockActionSpace()),
            timestep=0,
            lines_defaut=[],
            lines_overloaded_ids=lines_overloaded_ids or [0],
            act_reco_maintenance=MockActionObject(),
            non_connected_reconnectable_lines=[],
            all_disconnected_lines=[],
            dict_action={},
            actions_unfiltered=set(),
            hubs=[],
            g_overflow=mock_g_overflow,
            g_distribution_graph=mock_g_dist,
            simulator_data={},
            check_action_simulation=False,
        )

    def test_unconstrained_when_obs_linecut_is_none(self):
        """When obs_linecut is None, max_redispatch must be inf (unconstrained regime)."""
        d = self._make_discoverer(rho_defaut=[1.2, 0.8], rho_linecut=None)
        _, _, max_redispatch = d._compute_disconnection_flow_bounds()
        assert max_redispatch == float('inf')

    def test_unconstrained_when_obs_linecut_equals_obs_defaut(self):
        """When obs_linecut == obs_defaut and no lines are overloaded, max_redispatch is inf."""
        # In practice, overloaded lines are always cut in obs_linecut (rho → 0).
        # This test covers the case where no lines cross 100% in either observation.
        rho = [0.8, 0.7]
        d = self._make_discoverer(rho_defaut=rho, rho_linecut=rho)
        _, _, max_redispatch = d._compute_disconnection_flow_bounds()
        assert max_redispatch == float('inf')

    def test_unconstrained_when_obs_linecut_has_higher_but_not_overloaded(self):
        """Loading increase that stays below 1.0 must NOT constrain max_redispatch."""
        # L0 goes 0.7 → 0.9 (increased but < 1.0) → should NOT trigger constraint
        # L1 goes 0.5 → 0.6 (increased but < 1.0) → should NOT trigger constraint
        d = self._make_discoverer(rho_defaut=[0.7, 0.5], rho_linecut=[0.9, 0.6])
        _, _, max_redispatch = d._compute_disconnection_flow_bounds()
        assert max_redispatch == float('inf')

        # L0: capacity=100, rho_defaut=0.7, rho_linecut=1.2  (newly overloaded)
        #   max_overload_flow=100 (overloaded line L0 capacity)
        #   ratio = max_overload_flow / (rho_after - rho_before) * (1 - rho_before)
        #         = 100 / (1.2 - 0.7) * (1 - 0.7) = 100 / 0.5 * 0.3 = 60
        # L1: capacity=50, rho_defaut=0.5, rho_linecut=0.8   (NOT overloaded)
        #   does NOT constrain
        # binding = 60
        d = self._make_discoverer(rho_defaut=[0.7, 0.5], rho_linecut=[1.2, 0.8])
        _, _, max_redispatch = d._compute_disconnection_flow_bounds()
        assert max_redispatch == pytest.approx(60.0, rel=1e-6)

    def test_constrained_when_existing_overload_not_relieved(self):
        """An existing overload that worsens beyond threshold must constrain by its capacity."""
        # L0: rho_defaut=1.1 (overloaded), rho_linecut=1.3 (still overloaded) 
        #   n_state_rho=1.1, rho_after=1.3 > 1.1*1.02 -> ratio = capacity_l = 100.0
        d = self._make_discoverer(rho_defaut=[1.1, 0.5], rho_linecut=[1.3, 0.6])
        _, _, max_redispatch = d._compute_disconnection_flow_bounds()
        assert max_redispatch == 100.0

    def test_unconstrained_when_existing_overload_is_relieved(self):
        """An existing overload that IS relieved in obs_linecut (rho_after < 1) must NOT constrain."""
        # L0: rho_defaut=1.2 (overloaded), rho_linecut=0.5 (relieved) → rho_after < 1.0 → no constraint
        d = self._make_discoverer(rho_defaut=[1.2, 0.5], rho_linecut=[0.5, 0.6])
        _, _, max_redispatch = d._compute_disconnection_flow_bounds()
        assert max_redispatch == float('inf')

    def test_max_redispatch_uses_obs_defaut_as_rho_before(self):
        """rho_before in the ratio formula must come from obs_defaut, not obs."""
        # Deliberately set obs (self.obs) differently from obs_defaut to confirm the
        # method reads obs_defaut for rho_before, not obs.
        # L0: rho_defaut=0.6, rho_linecut=1.2 → newly overloaded
        rho_defaut = [0.6, 0.5]
        rho_linecut = [1.2, 0.5]  # L0 becomes newly overloaded
        d = self._make_discoverer(rho_defaut=rho_defaut, rho_linecut=rho_linecut)
        # Patch self.obs to have a very different rho to ensure it is NOT used
        d.obs = MockObservation(
            name_sub=d.obs.name_sub,
            name_line=d.obs.name_line,
            rho=np.array([0.1, 0.1]),  # very different from obs_defaut
            line_or_to_subid=list(range(2)),
            line_ex_to_subid=list(range(1, 3)),
        )
        _, _, max_redispatch = d._compute_disconnection_flow_bounds()
        # Expected: max_overload_flow=100, rho_before=0.6 (obs_defaut), rho_after=1.2 (obs_linecut)
        # ratio = max_overload_flow / (rho_after - rho_before) * (1 - rho_before)
        #       = 100 / (1.2 - 0.6) * (1 - 0.6) = 100 / 0.6 * 0.4 = 66.667
        assert max_redispatch == pytest.approx(66.667, rel=1e-3)

    def test_min_redispatch_uses_obs_defaut(self):
        """min_redispatch must reflect the worst overload in obs_defaut."""
        # L0 rho=1.3 → min_redispatch = (1.3-1)*100 = 30
        d = self._make_discoverer(rho_defaut=[1.3, 0.5], rho_linecut=None, lines_overloaded_ids=[0])
        _, min_redispatch, _ = d._compute_disconnection_flow_bounds()
        assert min_redispatch == pytest.approx(30.0, rel=1e-6)

    def test_max_overload_flow_is_overloaded_line_capacity(self):
        """max_overload_flow must equal the overloaded line's capacity, not the global max."""
        # L0 is overloaded (rho=1.2, lines_overloaded_ids=[0], capacity=100).
        # L1 has a smaller capacity (50). max_overload_flow = capacity of L0 = 100.
        d = self._make_discoverer(rho_defaut=[1.2, 0.5], rho_linecut=None)
        max_overload_flow, _, _ = d._compute_disconnection_flow_bounds()
        assert max_overload_flow == pytest.approx(100.0, rel=1e-6)

    def test_max_overload_flow_uses_overloaded_line_not_global_max(self):
        """When the overloaded line is NOT the highest-capacity edge, use its capacity."""
        # L0 has capacity 100 (higher), L1 (overloaded, lines_overloaded_ids=[1]) has capacity 50.
        # max_overload_flow must be 50 (L1's capacity), not 100 (L0's capacity).
        d = self._make_discoverer(rho_defaut=[0.5, 1.2], rho_linecut=None, lines_overloaded_ids=[1])
        max_overload_flow, _, _ = d._compute_disconnection_flow_bounds()
        assert max_overload_flow == pytest.approx(50.0, rel=1e-6)


class TestOverloadDisconnectionPriority:
    """Tests for prioritization of direct overload line disconnections.

    When an action disconnects one of the overloaded lines (lines_overloaded_ids)
    and the regime is unconstrained (no new overloads), the score should be boosted
    above 1.0 to rank above all other disconnection actions.
    """

    def _make_discoverer(self, rho_defaut, rho_linecut=None, lines_overloaded_ids=None,
                         dict_action=None, actions_unfiltered=None):
        """Build a discoverer with controlled rho values and optional action dicts."""
        n_lines = len(rho_defaut)
        name_line = [f"L{i}" for i in range(n_lines)]
        name_sub = [f"S{i}" for i in range(n_lines + 1)]

        mock_obs_defaut = MockObservation(
            name_sub=name_sub,
            name_line=name_line,
            rho=np.array(rho_defaut, dtype=float),
            line_or_to_subid=list(range(n_lines)),
            line_ex_to_subid=list(range(1, n_lines + 1)),
        )
        mock_obs_linecut = None
        if rho_linecut is not None:
            mock_obs_linecut = MockObservation(
                name_sub=name_sub,
                name_line=name_line,
                rho=np.array(rho_linecut, dtype=float),
                line_or_to_subid=list(range(n_lines)),
                line_ex_to_subid=list(range(1, n_lines + 1)),
            )

        mock_env = MockEnv(name_line=name_line, name_sub=name_sub)
        edge_data = {}
        for i in range(min(n_lines, 2)):
            edge_data[(i, i + 1)] = {0: {"name": f"L{i}", "capacity": 100.0 / (i + 1)}}
        mock_g_overflow = MockOverflowGraph(edge_data=edge_data)
        mock_g_dist = MockDistributionGraph()

        return ActionDiscoverer(
            env=mock_env,
            obs=mock_obs_defaut,
            obs_defaut=mock_obs_defaut,
            obs_linecut=mock_obs_linecut,
            classifier=ActionClassifier(MockActionSpace()),
            timestep=0,
            lines_defaut=[],
            lines_overloaded_ids=lines_overloaded_ids or [0],
            act_reco_maintenance=MockActionObject(),
            non_connected_reconnectable_lines=[],
            all_disconnected_lines=[],
            dict_action=dict_action or {},
            actions_unfiltered=actions_unfiltered or set(),
            hubs=[],
            g_overflow=mock_g_overflow,
            g_distribution_graph=mock_g_dist,
            simulator_data={},
            check_action_simulation=False,
        )

    def test_overload_disconnection_score_boosted_in_unconstrained_regime(self):
        """Disconnecting an overloaded line in unconstrained regime should score > 1.0."""
        # L0 is overloaded (index 0), no obs_linecut → unconstrained regime
        d = self._make_discoverer(rho_defaut=[1.2, 0.8], rho_linecut=None, lines_overloaded_ids=[0])
        # Force bounds to be computed
        d._disco_bounds = d._compute_disconnection_flow_bounds()
        d._disco_capacity_map = d._build_line_capacity_map()
        # Disconnecting L0 (the overloaded line) should get score > 1.0
        score = d.compute_disconnection_score({"L0"})
        assert score > 1.0, f"Expected score > 1.0 for overload disconnection, got {score}"

    def test_non_overload_disconnection_score_not_boosted(self):
        """Disconnecting a non-overloaded line should stay in [0, 1] in unconstrained regime."""
        # L0 is overloaded, L1 is not. Unconstrained regime (no obs_linecut).
        d = self._make_discoverer(rho_defaut=[1.2, 0.8], rho_linecut=None, lines_overloaded_ids=[0])
        d._disco_bounds = d._compute_disconnection_flow_bounds()
        d._disco_capacity_map = d._build_line_capacity_map()
        # Disconnecting L1 (NOT overloaded) should score <= 1.0
        score = d.compute_disconnection_score({"L1"})
        assert score <= 1.0, f"Expected score <= 1.0 for non-overload disconnection, got {score}"

    def test_overload_disconnection_ranks_above_non_overload(self):
        """Overload line disconnection must rank above all non-overload disconnections."""
        # L0 is overloaded. Unconstrained regime.
        d = self._make_discoverer(rho_defaut=[1.2, 0.8], rho_linecut=None, lines_overloaded_ids=[0])
        d._disco_bounds = d._compute_disconnection_flow_bounds()
        d._disco_capacity_map = d._build_line_capacity_map()
        score_overload = d.compute_disconnection_score({"L0"})
        score_other = d.compute_disconnection_score({"L1"})
        assert score_overload > score_other, (
            f"Overload disconnection (L0, score={score_overload}) must rank above "
            f"non-overload disconnection (L1, score={score_other})"
        )

    def test_overload_disconnection_no_bonus_in_constrained_regime(self):
        """Overload line disconnection must NOT get the bonus in constrained regime."""
        # L0 is overloaded; L1 is not overloaded but would become overloaded if L0 is cut.
        # rho_defaut: L0=1.2 (overloaded), L1=0.7
        # rho_linecut: L0=0.0 (cut), L1=1.5 (newly overloaded → constrained regime)
        d = self._make_discoverer(
            rho_defaut=[1.2, 0.7],
            rho_linecut=[0.0, 1.5],
            lines_overloaded_ids=[0],
        )
        d._disco_bounds = d._compute_disconnection_flow_bounds()
        d._disco_capacity_map = d._build_line_capacity_map()
        _, _, max_redispatch = d._disco_bounds
        assert max_redispatch < float('inf'), "Regime must be constrained for this test"
        # In constrained regime there is no bonus, so score must be <= 1.0
        score = d.compute_disconnection_score({"L0"})
        assert score <= 1.0, (
            f"No priority bonus should apply in constrained regime, got score={score}"
        )

    def test_overload_disconnection_included_even_if_not_on_constrained_path(self):
        """An action disconnecting an overloaded line must be considered even if not on the constrained path."""
        # L0 is overloaded. Constrained path contains only L1 (not L0).
        d = self._make_discoverer(
            rho_defaut=[1.2, 0.8],
            rho_linecut=None,
            lines_overloaded_ids=[0],
            dict_action={
                "disco_L0": {
                    "type": "open_line",
                    "description_unitaire": "Ouverture L0",
                    "content": {"set_bus": {"lines_ex_id": {"L0": -1}}},
                },
                "disco_L1": {
                    "type": "open_line",
                    "description_unitaire": "Ouverture L1",
                    "content": {"set_bus": {"lines_ex_id": {"L1": -1}}},
                },
            },
            actions_unfiltered={"disco_L0", "disco_L1"},
        )
        # Only L1 is on the constrained path; L0 is the overloaded line itself
        d.find_relevant_disconnections(lines_constrained_path_names=["L1"])
        assert "disco_L0" in d.identified_disconnections, (
            "Overload line disconnection (L0) must be included even if not on constrained path"
        )

    def test_overload_disconnection_ranks_first_among_all_disconnections(self):
        """After find_relevant_disconnections, the overloaded line action ranks first."""
        # L0 is overloaded. Constrained path includes both L0 and L1.
        d = self._make_discoverer(
            rho_defaut=[1.2, 0.8],
            rho_linecut=None,
            lines_overloaded_ids=[0],
            dict_action={
                "disco_L0": {
                    "type": "open_line",
                    "description_unitaire": "Ouverture L0",
                    "content": {"set_bus": {"lines_ex_id": {"L0": -1}}},
                },
                "disco_L1": {
                    "type": "open_line",
                    "description_unitaire": "Ouverture L1",
                    "content": {"set_bus": {"lines_ex_id": {"L1": -1}}},
                },
            },
            actions_unfiltered={"disco_L0", "disco_L1"},
        )
        d.find_relevant_disconnections(lines_constrained_path_names=["L0", "L1"])
        keys = list(d.identified_disconnections.keys())
        assert keys[0] == "disco_L0", (
            f"Overload disconnection (L0) should rank first, but got order: {keys}"
        )
        assert d.scores_disconnections["disco_L0"] > d.scores_disconnections["disco_L1"]


class TestNodeMergingScore:
    """Tests for the compute_node_merging_score method."""

    @pytest.fixture
    def merging_discoverer(self):
        """Create a discoverer with meaningful theta values for node merging tests."""
        # Sub0 has 2 buses. L1 originates at Sub0 (bus 1), L2 also at Sub0 (bus 2).
        # Load0 on bus 1, Gen0 on bus 2 at Sub0.
        # L3 connects Sub1->Sub2.
        # Overflow graph: edge (0,1) with L1 has POSITIVE capacity (red loop flow on bus 1),
        # edge (0,1) with L2 has NEGATIVE capacity (on bus 2).
        #
        # sub_topology order for Sub0: [Load0_bus, Gen0_bus, L1_or_bus, L2_or_bus]
        #                              = [1, 2, 1, 2]
        mock_obs = MockObservation(
            name_sub=["Sub0", "Sub1", "Sub2"],
            name_line=["L1", "L2", "L3"],
            name_load=["Load0"],
            name_gen=["Gen0"],
            load_to_subid=[0],
            gen_to_subid=[0],
            sub_topologies={0: [1, 2, 1, 2], 1: [1, 1], 2: [1, 1]},
            sub_info=[4, 3, 3],
            topo_vect=np.array([1, 2, 1, 2, 1, 1, 1, 1, 1, 1]),
            line_or_to_subid=[0, 0, 1],
            line_ex_to_subid=[1, 1, 2],
            line_or_bus=[1, 2, 1],
            line_ex_bus=[1, 1, 1],
            # L1: theta_or=-5.0 (at Sub0 bus 1), theta_ex=-3.0 (at Sub1 bus 1)
            # L2: theta_or=-1.0 (at Sub0 bus 2), theta_ex=-3.0 (at Sub1 bus 1)
            # L3: theta_or=-3.0 (at Sub1 bus 1), theta_ex=-1.0 (at Sub2 bus 1)
            theta_or=np.array([-5.0, -1.0, -3.0]),
            theta_ex=np.array([-3.0, -3.0, -1.0]),
        )
        mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
        # Edge (0,1) with L1 has POSITIVE capacity (red loop on bus 1),
        # Edge (0,1) with L2 has NEGATIVE capacity (on bus 2)
        mock_g_overflow = MockOverflowGraph(
            edge_data={(0, 1): {0: {"name": "L1", "capacity": 10},
                                1: {"name": "L2", "capacity": -5}},
                       (1, 2): {0: {"name": "L3", "capacity": 3}}}
        )
        mock_g_dist = MockDistributionGraph()

        return ActionDiscoverer(
            env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
            classifier=ActionClassifier(MockActionSpace()),
            timestep=0, lines_defaut=[], lines_overloaded_ids=[0],
            act_reco_maintenance=MockActionObject(),
            non_connected_reconnectable_lines=[], all_disconnected_lines=[],
            dict_action={}, actions_unfiltered=set(), hubs=[],
            g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
            simulator_data={}, check_action_simulation=False
        )

    def test_returns_tuple(self, merging_discoverer):
        """compute_node_merging_score must return a (float, dict) tuple."""
        result = merging_discoverer.compute_node_merging_score(0, [1, 2])
        assert isinstance(result, tuple) and len(result) == 2
        score, details = result
        assert isinstance(score, float)
        assert isinstance(details, dict)

    def test_red_loop_bus_identified_by_positive_capacity(self, merging_discoverer):
        """Bus carrying positive capacity edges should be identified as red loop bus."""
        # Sub0 bus 1 has L1 (line_or_bus[0]=1), and L1 has positive capacity=10
        # Sub0 bus 2 has L2 (line_or_bus[1]=2), and L2 has negative capacity=-5
        # So bus 1 is the red loop bus (theta1), bus 2 is the other (theta2)
        # theta1 = get_theta_node(obs, 0, 1) = median of theta_or[0] = -5.0
        # theta2 = get_theta_node(obs, 0, 2) = median of theta_or[1] = -1.0
        # score = theta2 - theta1 = -1.0 - (-5.0) = 4.0
        score, details = merging_discoverer.compute_node_merging_score(0, [1, 2])
        assert score > 0.0  # theta2 > theta1 means flow towards red loop
        # Red loop bus assets should contain L1 (on bus 1)
        assert "L1" in details["targeted_node_assets"]["lines"]

    def test_single_bus_returns_zero(self, merging_discoverer):
        """Should return (0.0, {}) when fewer than 2 buses."""
        score, details = merging_discoverer.compute_node_merging_score(0, [1])
        assert score == 0.0
        assert details == {}

    def test_details_contains_targeted_node_assets(self, merging_discoverer):
        """Details dict must contain targeted_node_assets with lines on the red loop bus."""
        _, details = merging_discoverer.compute_node_merging_score(0, [1, 2])
        assert "targeted_node_assets" in details
        assets = details["targeted_node_assets"]
        assert "lines" in assets
        assert "loads" in assets
        assert "generators" in assets
        # L1 is on bus 1 (red loop bus) at Sub0 origin
        assert "L1" in assets["lines"]
        # L2 is on bus 2 (not red loop bus), should not appear
        assert "L2" not in assets["lines"]


# =============================================================================
# Tests for params storage alongside scores in each discovery method
# =============================================================================

class TestDiscoveryParamsStorage:
    """Tests that each discovery method stores params alongside scores."""

    def test_reconnection_params_populated(self, discoverer_instance, monkeypatch):
        """verify_relevant_reconnections must populate params_reconnections with threshold and max flow."""
        def mock_path_check(*args): return True, ["Sub0", "Sub1", "Sub3"], None
        monkeypatch.setattr(discoverer_instance, "_check_other_reconnectable_line_on_path", mock_path_check)
        discoverer_instance.verify_relevant_reconnections(lines_to_reconnect={"L1"}, red_loop_paths=[])
        params = discoverer_instance.params_reconnections
        assert "percentage_threshold_min_dispatch_flow" in params
        assert "max_dispatch_flow" in params
        assert isinstance(params["percentage_threshold_min_dispatch_flow"], (int, float))
        assert isinstance(params["max_dispatch_flow"], (int, float))
        assert params["max_dispatch_flow"] > 0

    def test_disconnection_params_populated(self, discoverer_instance):
        """find_relevant_disconnections must populate params_disconnections with redispatch bounds."""
        discoverer_instance.find_relevant_disconnections(lines_constrained_path_names=["L1"])
        params = discoverer_instance.params_disconnections
        assert isinstance(params, dict)
        if params:
            assert "regime" in params
            assert "min_redispatch" in params
            if params["regime"] == "constrained":
                assert "max_redispatch" in params
                assert "peak_redispatch" in params
                expected_peak = params["min_redispatch"] + 0.8 * (params["max_redispatch"] - params["min_redispatch"])
                assert abs(params["peak_redispatch"] - expected_peak) < 1e-9
            else:
                assert params["regime"] == "unconstrained"
                assert "max_overload_flow" in params

    def test_splitting_params_populated_per_action(self, discoverer_instance):
        """find_relevant_node_splitting must store per-action details in params_splits_dict."""
        discoverer_instance.find_relevant_node_splitting(hubs_names=["Sub0"], nodes_blue_path_names=["Sub1"])
        params = discoverer_instance.params_splits_dict
        assert isinstance(params, dict)
        # Each scored action should have a details entry
        for action_id in discoverer_instance.scores_splits_dict:
            assert action_id in params
            assert isinstance(params[action_id], dict)

    def test_merging_params_populated_per_action(self, discoverer_instance):
        """find_relevant_node_merging must store per-action params with assets."""
        discoverer_instance.find_relevant_node_merging(["Sub0", "Sub1", "Sub3"])
        params = discoverer_instance.params_merges
        assert isinstance(params, dict)
        # Each scored action should have a per-action details entry
        for action_id in discoverer_instance.scores_merges:
            assert action_id in params
            assert isinstance(params[action_id], dict)
            assert "targeted_node_assets" in params[action_id]
            assets = params[action_id]["targeted_node_assets"]
            assert "lines" in assets
            assert "loads" in assets
            assert "generators" in assets


# =============================================================================
# Tests for action_scores dictionary structure and rounding
# =============================================================================

class TestActionScoresStructureAndRounding:
    """Tests for the nested action_scores dict assembled by discover_and_prioritize."""

    @pytest.fixture
    def scores_discoverer(self):
        """Create a discoverer with pre-populated scores/params to test assembly and rounding."""
        mock_obs = MockObservation(
            name_sub=["Sub0", "Sub1"],
            name_line=["L1", "L2"],
            sub_topologies={0: [1, 2], 1: [1, 1]},
            sub_info=[2, 2],
            topo_vect=np.array([1, 2, 1, 1]),
            line_or_to_subid=[0, 1],
            line_ex_to_subid=[1, 0],
            line_or_bus=[1, 1],
            line_ex_bus=[1, 1],
            theta_or=np.array([0.0, 0.0]),
            theta_ex=np.array([0.0, 0.0]),
        )
        mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
        mock_g_overflow = MockOverflowGraph(
            edge_data={(0, 1): {0: {"name": "L1", "capacity": 10}}}
        )
        mock_g_dist = MockDistributionGraph()

        discoverer = ActionDiscoverer(
            env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
            classifier=ActionClassifier(MockActionSpace()),
            timestep=0, lines_defaut=[], lines_overloaded_ids=[0],
            act_reco_maintenance=MockActionObject(),
            non_connected_reconnectable_lines=[], all_disconnected_lines=[],
            dict_action={}, actions_unfiltered=set(), hubs=[],
            g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
            simulator_data={}, check_action_simulation=False
        )

        # Pre-populate with values that have many decimals to test rounding
        discoverer.scores_reconnections = {"reco_1": 3.14159, "reco_2": -1.23456}
        discoverer.params_reconnections = {
            "percentage_threshold_min_dispatch_flow": 0.10000001,
            "max_dispatch_flow": 123.456789,
        }
        discoverer.scores_disconnections = {"disco_1": 0.87654321}
        discoverer.params_disconnections = {
            "regime": "constrained",
            "min_redispatch": 10.111, "max_redispatch": 50.999, "peak_redispatch": 42.8888,
        }
        discoverer.scores_splits_dict = {"split_1": 0.99999, "split_2": -0.12345}
        discoverer.params_splits_dict = {
            "split_1": {"node_type": "amont",
                        "targeted_node_assets": {"lines": ["L1"], "loads": [], "generators": []},
                        "in_negative_flows": 12.3456, "out_negative_flows": 78.9012,
                        "in_positive_flows": 0.0, "out_positive_flows": 5.55555},
            "split_2": {"node_type": "aval",
                        "targeted_node_assets": {"lines": ["L2"], "loads": ["Load_X"], "generators": ["Gen_Y"]},
                        "in_negative_flows": 99.9999, "out_negative_flows": 1.11111,
                        "in_positive_flows": 3.33333, "out_positive_flows": 0.0},
        }
        discoverer.scores_merges = {"merge_1": 2.71828}
        discoverer.params_merges = {
            "merge_1": {
                "targeted_node_assets": {"lines": ["L1"], "loads": ["Load_A"], "generators": []},
            },
        }
        return discoverer

    def test_action_scores_has_all_four_types(self, scores_discoverer):
        """action_scores must have exactly the four expected action type keys."""
        # Manually call the assembly logic (extract from discover_and_prioritize)
        scores_discoverer.prioritized_actions = {}
        action_scores = self._build_action_scores(scores_discoverer)
        expected_types = {"line_reconnection", "line_disconnection", "open_coupling", "close_coupling"}
        assert set(action_scores.keys()) == expected_types

    def test_each_type_has_scores_and_params(self, scores_discoverer):
        """Each action type entry must have 'scores' and 'params' keys."""
        action_scores = self._build_action_scores(scores_discoverer)
        for action_type, entry in action_scores.items():
            assert "scores" in entry, f"Missing 'scores' in {action_type}"
            assert "params" in entry, f"Missing 'params' in {action_type}"

    def test_scores_sorted_descending(self, scores_discoverer):
        """Scores within each type must be sorted in descending order."""
        action_scores = self._build_action_scores(scores_discoverer)
        for action_type, entry in action_scores.items():
            scores = list(entry["scores"].values())
            assert scores == sorted(scores, reverse=True), \
                f"Scores not sorted descending in {action_type}: {scores}"

    def test_scores_rounded_to_two_decimals(self, scores_discoverer):
        """All score values must be rounded to 2 decimal places."""
        action_scores = self._build_action_scores(scores_discoverer)
        assert action_scores["line_reconnection"]["scores"]["reco_1"] == 3.14
        assert action_scores["line_reconnection"]["scores"]["reco_2"] == -1.23
        assert action_scores["line_disconnection"]["scores"]["disco_1"] == 0.88
        assert action_scores["open_coupling"]["scores"]["split_1"] == 1.0
        assert action_scores["open_coupling"]["scores"]["split_2"] == -0.12
        assert action_scores["close_coupling"]["scores"]["merge_1"] == 2.72

    def test_flat_params_rounded_to_two_decimals(self, scores_discoverer):
        """Flat params (reconnections, disconnections) must have floats rounded."""
        action_scores = self._build_action_scores(scores_discoverer)
        reco_params = action_scores["line_reconnection"]["params"]
        assert reco_params["percentage_threshold_min_dispatch_flow"] == 0.1
        assert reco_params["max_dispatch_flow"] == 123.46

        disco_params = action_scores["line_disconnection"]["params"]
        assert disco_params["regime"] == "constrained"  # String preserved
        assert disco_params["min_redispatch"] == 10.11
        assert disco_params["max_redispatch"] == 51.0
        assert disco_params["peak_redispatch"] == 42.89

    def test_nested_params_rounded_to_two_decimals(self, scores_discoverer):
        """Per-action nested params (open/close_coupling) must have floats rounded, non-floats preserved."""
        action_scores = self._build_action_scores(scores_discoverer)
        split_params = action_scores["open_coupling"]["params"]

        s1 = split_params["split_1"]
        assert s1["node_type"] == "amont"  # String preserved
        assert isinstance(s1["targeted_node_assets"], dict)  # Assets dict preserved
        assert s1["in_negative_flows"] == 12.35
        assert s1["out_negative_flows"] == 78.9
        assert s1["out_positive_flows"] == 5.56

        s2 = split_params["split_2"]
        assert s2["in_negative_flows"] == 100.0
        assert s2["out_negative_flows"] == 1.11

        # close_coupling (merges) is now per-action too
        merge_params = action_scores["close_coupling"]["params"]
        m1 = merge_params["merge_1"]
        assert isinstance(m1["targeted_node_assets"], dict)  # Assets dict preserved

    def test_empty_scores_produce_empty_entries(self, scores_discoverer):
        """When a category has no scored actions, its scores and params should be empty."""
        scores_discoverer.scores_reconnections = {}
        scores_discoverer.params_reconnections = {}
        action_scores = self._build_action_scores(scores_discoverer)
        assert action_scores["line_reconnection"]["scores"] == {}
        assert action_scores["line_reconnection"]["params"] == {}

    @staticmethod
    def _build_action_scores(discoverer):
        """Replicate the action_scores assembly logic from discover_and_prioritize."""
        def _round_scores(d):
            return {k: round(v, 2) for k, v in d.items()}

        def _round_params(d):
            out = {}
            for k, v in d.items():
                if isinstance(v, dict):
                    out[k] = {kk: round(vv, 2) if isinstance(vv, float) else vv for kk, vv in v.items()}
                elif isinstance(v, float):
                    out[k] = round(v, 2)
                else:
                    out[k] = v
            return out

        return {
            "line_reconnection": {
                "scores": _round_scores(dict(sorted(discoverer.scores_reconnections.items(), key=lambda x: x[1], reverse=True))),
                "params": _round_params(discoverer.params_reconnections),
            },
            "line_disconnection": {
                "scores": _round_scores(dict(sorted(discoverer.scores_disconnections.items(), key=lambda x: x[1], reverse=True))),
                "params": _round_params(discoverer.params_disconnections),
            },
            "open_coupling": {
                "scores": _round_scores(dict(sorted(discoverer.scores_splits_dict.items(), key=lambda x: x[1], reverse=True))),
                "params": _round_params(discoverer.params_splits_dict),
            },
            "close_coupling": {
                "scores": _round_scores(dict(sorted(discoverer.scores_merges.items(), key=lambda x: x[1], reverse=True))),
                "params": _round_params(discoverer.params_merges),
            },
        }


## Section: Load Shedding Tests ##

class MockConstrainedPath:
    """Mocks the constrained path object for load shedding tests."""
    def __init__(self, aval_nodes, amont_nodes=None):
        self._aval = aval_nodes
        self._amont = amont_nodes or []
    def n_aval(self):
        return self._aval
    def n_amont(self):
        return self._amont

class MockDistributionGraphWithPath(MockDistributionGraph):
    """Extended mock with constrained path support."""
    def __init__(self, constrained_edges=None, aval_nodes=None, amont_nodes=None):
        super().__init__()
        self._constrained_edges = constrained_edges or []
        self._aval_nodes = aval_nodes or []
        self._amont_nodes = amont_nodes or []

    def get_constrained_path(self):
        return MockConstrainedPath(self._aval_nodes, self._amont_nodes)

    def get_constrained_edges_nodes(self):
        return self._constrained_edges, self._amont_nodes + self._aval_nodes, [], []


@pytest.fixture
def load_shedding_discoverer():
    """Provides a discoverer configured for load shedding tests."""
    # Network: Sub0 --L1-- Sub1 --L2-- Sub2
    # Sub0: amont, Sub1: constraint, Sub2: aval with loads
    mock_obs = MockObservation(
        name_sub=np.array(["Sub0", "Sub1", "Sub2"]),
        name_line=np.array(["L1", "L2"]),
        name_load=np.array(["Load_A", "Load_B"]),
        name_gen=np.array(["Gen_A"]),
        line_or_to_subid=np.array([0, 1]),
        line_ex_to_subid=np.array([1, 2]),
        line_or_bus=np.array([1, 1]),
        line_ex_bus=np.array([1, 1]),
        rho=np.array([1.2, 0.5]),  # L1 overloaded at 120%
        load_to_subid=np.array([2, 2]),  # Both loads at Sub2
        gen_to_subid=np.array([0]),
        load_values=[50.0, 30.0],  # 50 MW and 30 MW loads
        sub_topologies={0: [1, 1], 1: [1, 1], 2: [1, 1, 1, 1]},
        sub_info=np.array([2, 2, 4]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1, 1]),
        obj_connect_to={
            0: {'loads_id': [], 'generators_id': [0], 'lines_or_id': [0], 'lines_ex_id': []},
            1: {'loads_id': [], 'generators_id': [], 'lines_or_id': [1], 'lines_ex_id': [0]},
            2: {'loads_id': [0, 1], 'generators_id': [], 'lines_or_id': [], 'lines_ex_id': [1]},
        },
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))

    # Overflow graph: edges with capacity (blue path)
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "100"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })

    # Distribution graph: Sub2 is aval, L1 and L2 are constrained edges
    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2"],
        aval_nodes=[2],   # Sub2 is downstream
        amont_nodes=[0],  # Sub0 is upstream
    )

    discoverer = ActionDiscoverer(
        env=mock_env,
        obs=mock_obs,
        obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0,
        lines_defaut=["L1"],
        lines_overloaded_ids=[0],  # L1 is overloaded
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[],
        all_disconnected_lines=[],
        dict_action={},
        actions_unfiltered=set(),
        hubs=[],
        g_overflow=mock_g_overflow,
        g_distribution_graph=mock_g_dist,
        simulator_data={},
        check_action_simulation=False,
    )
    return discoverer


def test_load_shedding_finds_candidates(load_shedding_discoverer):
    """Test that load shedding creates one action per load on the downstream node."""
    discoverer = load_shedding_discoverer
    discoverer.find_relevant_load_shedding([2])  # Sub2 is aval with 2 loads

    # One action per load: Load_A (50MW) and Load_B (30MW)
    assert len(discoverer.identified_load_shedding) == 2
    assert "load_shedding_Load_A" in discoverer.identified_load_shedding
    assert "load_shedding_Load_B" in discoverer.identified_load_shedding


def test_load_shedding_score_computation(load_shedding_discoverer):
    """Test that scores are computed correctly based on influence and coverage."""
    discoverer = load_shedding_discoverer
    discoverer.find_relevant_load_shedding([2])

    assert "load_shedding_Load_A" in discoverer.scores_load_shedding
    score = discoverer.scores_load_shedding["load_shedding_Load_A"]
    assert 0 < score <= 1.0


def test_load_shedding_params_structure(load_shedding_discoverer):
    """Test that params contain expected fields including load_name."""
    discoverer = load_shedding_discoverer
    discoverer.find_relevant_load_shedding([2])

    params = discoverer.params_load_shedding["load_shedding_Load_A"]
    assert params["substation"] == "Sub2"
    assert params["node_type"] == "aval"
    assert params["load_name"] == "Load_A"
    assert "influence_factor" in params
    assert "P_shedding_MW" in params
    assert "P_overload_excess_MW" in params
    assert "available_load_MW" in params
    assert params["available_load_MW"] == 50.0  # Load_A's power
    assert "in_negative_flows" in params
    assert "out_negative_flows" in params
    assert "coverage_ratio" in params
    assert params["loads_shed"] == ["Load_A"]


def test_load_shedding_skips_node_without_loads(load_shedding_discoverer):
    """Test that nodes without loads are skipped."""
    discoverer = load_shedding_discoverer
    discoverer.find_relevant_load_shedding([0])  # Sub0 has no loads

    assert len(discoverer.identified_load_shedding) == 0


def test_load_shedding_skips_node_without_blue_edge(load_shedding_discoverer):
    """Test that nodes not connected to blue edges are skipped."""
    discoverer = load_shedding_discoverer
    # Override constrained edges to exclude L2 (the edge connecting to Sub2)
    discoverer.g_distribution_graph = MockDistributionGraphWithPath(
        constrained_edges=["L1"],  # Only L1, not L2
        aval_nodes=[2],
        amont_nodes=[0],
    )
    discoverer.find_relevant_load_shedding([2])

    # Sub2 has loads but its adjacent edge L2 is not in the blue path
    assert len(discoverer.identified_load_shedding) == 0


def test_load_shedding_influence_factor(load_shedding_discoverer):
    """Test that influence_factor is the ratio of max negative flow to max overload flow."""
    discoverer = load_shedding_discoverer
    discoverer.find_relevant_load_shedding([2])

    params = discoverer.params_load_shedding["load_shedding_Load_A"]
    # Blue edge L2 has label=-60 → neg_in=60, max_overload_flow=100
    assert params["influence_factor"] == round(60.0 / 100.0, 2)  # 0.6


def test_load_shedding_overload_excess(load_shedding_discoverer):
    """Test that P_overload_excess is computed from rho and max overload flow."""
    discoverer = load_shedding_discoverer
    discoverer.find_relevant_load_shedding([2])

    params = discoverer.params_load_shedding["load_shedding_Load_A"]
    # rho_max = 1.2, max_overload_flow = 100 -> excess = (1.2-1.0)*100 = 20 MW
    assert params["P_overload_excess_MW"] == 20.0


def test_load_shedding_neg_flows_in_params(load_shedding_discoverer):
    """Test that in/out_negative_flows are stored in params (consistent with node splitting)."""
    discoverer = load_shedding_discoverer
    discoverer.find_relevant_load_shedding([2])

    params = discoverer.params_load_shedding["load_shedding_Load_A"]
    # Edge L2 (1->2) has label="-60" → negative in-edge for Sub2 → in_neg=60, out_neg=0
    assert params["in_negative_flows"] == 60.0
    assert params["out_negative_flows"] == 0.0
    # influence_factor = max(60, 0) / 100 = 0.6
    assert params["influence_factor"] == 0.6


def test_load_shedding_action_uses_power_reduction(load_shedding_discoverer):
    """Test that load shedding uses set_load_p (power reduction to 0 MW) not disconnection.

    Load shedding actions should reduce active power to 0 MW via set_load_p,
    keeping the load electrically connected rather than disconnecting it.
    """
    discoverer = load_shedding_discoverer

    # Track what the action_space receives
    calls = []
    original_action_space = discoverer.action_space
    def capturing_action_space(action_dict):
        calls.append(action_dict)
        return original_action_space(action_dict)
    discoverer.action_space = capturing_action_space

    discoverer.find_relevant_load_shedding([2])

    # One call per load (2 loads at Sub2)
    assert len(calls) == 2
    for call in calls:
        # Must use set_load_p, NOT set_bus
        assert "set_load_p" in call, f"Expected set_load_p key, got: {list(call.keys())}"
        assert "set_bus" not in call, "Should not use set_bus for power reduction"
        loads_p_dict = call["set_load_p"]
        # Each action targets exactly one load
        assert len(loads_p_dict) == 1
        # Key must be a string (load name)
        key = list(loads_p_dict.keys())[0]
        assert isinstance(key, str), f"Expected string key, got {type(key).__name__}: {key}"
        # Target power must be 0.0
        assert loads_p_dict[key] == 0.0, f"Expected target_p=0.0, got {loads_p_dict[key]}"
    # Verify both load names are used across the calls
    all_keys = {list(c["set_load_p"].keys())[0] for c in calls}
    assert all_keys == {"Load_A", "Load_B"}


def test_load_shedding_params_contain_power_reduction_fields(load_shedding_discoverer):
    """Test that load shedding params include action_mode, target_p_MW, and reduction_MW."""
    discoverer = load_shedding_discoverer
    discoverer.find_relevant_load_shedding([2])

    params = discoverer.params_load_shedding["load_shedding_Load_A"]
    assert params["action_mode"] == "power_reduction"
    assert params["target_p_MW"] == 0.0
    assert params["reduction_MW"] == 50.0  # Load_A's available power


def test_load_shedding_multiple_subs_multiple_loads():
    """Test load shedding with multiple aval substations, each having multiple loads.

    Network: Sub0 --L1-- Sub1 --L2-- Sub2 --L3-- Sub3
    Sub0: amont (generator)
    Sub1: constraint node
    Sub2: aval, 3 loads (70MW, 40MW, 10MW)
    Sub3: aval, 2 loads (25MW, 15MW)
    """
    mock_obs = MockObservation(
        name_sub=np.array(["Sub0", "Sub1", "Sub2", "Sub3"]),
        name_line=np.array(["L1", "L2", "L3"]),
        name_load=np.array(["Load_X", "Load_Y", "Load_Z", "Load_P", "Load_Q"]),
        name_gen=np.array(["Gen_1"]),
        line_or_to_subid=np.array([0, 1, 2]),
        line_ex_to_subid=np.array([1, 2, 3]),
        line_or_bus=np.array([1, 1, 1]),
        line_ex_bus=np.array([1, 1, 1]),
        rho=np.array([1.3, 0.5, 0.4]),  # L1 overloaded at 130%
        load_to_subid=np.array([2, 2, 2, 3, 3]),  # 3 loads at Sub2, 2 at Sub3
        gen_to_subid=np.array([0]),
        load_values=[70.0, 40.0, 10.0, 25.0, 15.0],
        sub_topologies={0: [1, 1], 1: [1, 1], 2: [1, 1, 1, 1, 1], 3: [1, 1, 1]},
        sub_info=np.array([2, 2, 5, 3]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1]),
        obj_connect_to={
            0: {'loads_id': [], 'generators_id': [0], 'lines_or_id': [0], 'lines_ex_id': []},
            1: {'loads_id': [], 'generators_id': [], 'lines_or_id': [1], 'lines_ex_id': [0]},
            2: {'loads_id': [0, 1, 2], 'generators_id': [], 'lines_or_id': [2], 'lines_ex_id': [1]},
            3: {'loads_id': [3, 4], 'generators_id': [], 'lines_or_id': [], 'lines_ex_id': [2]},
        },
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))

    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 200.0, "label": "200"}},
        (1, 2): {0: {"name": "L2", "capacity": 120.0, "label": "-120"}},
        (2, 3): {0: {"name": "L3", "capacity": 50.0, "label": "-50"}},
    })

    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2", "L3"],
        aval_nodes=[2, 3],
        amont_nodes=[0],
    )

    discoverer = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[0],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
        simulator_data={}, check_action_simulation=False,
    )

    discoverer.find_relevant_load_shedding([2, 3])

    # Should create 5 actions total: 3 for Sub2 + 2 for Sub3
    assert len(discoverer.identified_load_shedding) == 5

    expected_actions = {
        "load_shedding_Load_X", "load_shedding_Load_Y", "load_shedding_Load_Z",
        "load_shedding_Load_P", "load_shedding_Load_Q",
    }
    assert set(discoverer.identified_load_shedding.keys()) == expected_actions

    # Each action targets exactly one load
    for action_id, params in discoverer.params_load_shedding.items():
        assert len(params["loads_shed"]) == 1
        assert params["loads_shed"][0] == params["load_name"]
        assert params["available_load_MW"] == params["P_shedding_MW"] or params["P_shedding_MW"] <= params["available_load_MW"]

    # Sub2 loads have higher influence (L2 capacity 120 vs L3 capacity 50 for Sub3)
    # Sub2 influence = max(neg_in, neg_out) / max_overload = 120/200 = 0.6
    # Sub3 influence: in-edge L3 label=-50 → neg_in=50, but also out-edge from Sub3 doesn't exist
    #   So Sub3 influence = 50/200 = 0.25
    assert discoverer.params_load_shedding["load_shedding_Load_X"]["influence_factor"] == 0.6
    assert discoverer.params_load_shedding["load_shedding_Load_P"]["influence_factor"] == 0.25

    # Higher-power loads at more influential substations should score higher
    score_X = discoverer.scores_load_shedding["load_shedding_Load_X"]  # 70MW @ Sub2
    score_Z = discoverer.scores_load_shedding["load_shedding_Load_Z"]  # 10MW @ Sub2
    score_P = discoverer.scores_load_shedding["load_shedding_Load_P"]  # 25MW @ Sub3
    assert score_X > score_Z, "Larger load at same sub should score higher"
    assert score_X > score_P, "Load at more influential sub should score higher"

    # Results should be sorted by score descending
    scores_list = list(discoverer.scores_load_shedding.values())
    action_ids_sorted = list(discoverer.identified_load_shedding.keys())
    scores_sorted = [discoverer.scores_load_shedding[aid] for aid in action_ids_sorted]
    assert scores_sorted == sorted(scores_sorted, reverse=True), "Actions should be sorted by score descending"

# ===========================================================================
# Section: Renewable Curtailment (find_relevant_renewable_curtailment) Tests
# ===========================================================================
# Network topology used in these tests:
#   Sub0 (amont, wind+solar) --L1-- Sub1 (constraint) --L2-- Sub2 (aval, loads)
#   L1 is overloaded at 120% (rho=1.2), capacity=100 MW.
#   Edge L2 carries -60 MW (blue, negative).
#   Sub0 has Wind_A (80 MW) and Solar_B (40 MW); Sub1 has no renewables.

@pytest.fixture
def renewable_discoverer():
    """Provides a discoverer configured for renewable curtailment tests."""
    mock_obs = MockObservation(
        name_sub=np.array(["Sub0", "Sub1", "Sub2"]),
        name_line=np.array(["L1", "L2"]),
        name_load=np.array(["Load_A"]),
        name_gen=np.array(["Wind_A", "Solar_B", "Thermal_C"]),
        line_or_to_subid=np.array([0, 1]),
        line_ex_to_subid=np.array([1, 2]),
        line_or_bus=np.array([1, 1]),
        line_ex_bus=np.array([1, 1]),
        rho=np.array([1.2, 0.5]),       # L1 overloaded at 120%
        load_to_subid=np.array([2]),    # Load at Sub2
        gen_to_subid=np.array([0, 0, 1]),  # Wind_A, Solar_B at Sub0; Thermal_C at Sub1
        load_values=[50.0],
        gen_values=[-80.0, -40.0, -200.0],  # Negative = producing (generator convention)
        gen_energy_sources=["WIND", "SOLAR", "THERMAL"],
        sub_topologies={0: [1, 1, 1, 1], 1: [1, 1, 1], 2: [1, 1]},
        sub_info=np.array([4, 3, 2]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1, 1, 1]),
        obj_connect_to={
            0: {'loads_id': [], 'generators_id': [0, 1, 2], 'lines_or_id': [0], 'lines_ex_id': []},
            1: {'loads_id': [], 'generators_id': [2], 'lines_or_id': [1], 'lines_ex_id': [0]},
            2: {'loads_id': [0], 'generators_id': [], 'lines_or_id': [], 'lines_ex_id': [1]},
        },
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))

    # L1: negative label (-80 MW) — blue edge from amont Sub0 toward constraint Sub1.
    #     A negative outgoing flow at Sub0 means reducing generation here alleviates L1.
    # L2: negative label (-60 MW) — blue edge going downstream to aval Sub2.
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "-80"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })

    # Sub0 is amont, Sub2 is aval; L1 and L2 are on the constrained path
    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2"],
        aval_nodes=[2],
        amont_nodes=[0],
    )

    discoverer = ActionDiscoverer(
        env=mock_env,
        obs=mock_obs,
        obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0,
        lines_defaut=["L1"],
        lines_overloaded_ids=[0],  # L1 is overloaded
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[],
        all_disconnected_lines=[],
        dict_action={},
        actions_unfiltered=set(),
        hubs=[],
        g_overflow=mock_g_overflow,
        g_distribution_graph=mock_g_dist,
        simulator_data={},
        check_action_simulation=False,
    )
    return discoverer


def test_renewable_curtailment_finds_candidates(renewable_discoverer):
    """Renewable curtailment creates one action per renewable generator on the upstream node."""
    discoverer = renewable_discoverer
    discoverer.find_relevant_renewable_curtailment([0])  # Sub0 is amont

    # Wind_A and Solar_B are renewable; Thermal_C is not
    assert len(discoverer.identified_renewable_curtailment) == 2
    assert "curtail_Wind_A" in discoverer.identified_renewable_curtailment
    assert "curtail_Solar_B" in discoverer.identified_renewable_curtailment
    assert "curtail_Thermal_C" not in discoverer.identified_renewable_curtailment


def test_renewable_curtailment_skips_non_renewable(renewable_discoverer):
    """Nodes with only non-renewable generators produce no curtailment actions."""
    discoverer = renewable_discoverer
    discoverer.find_relevant_renewable_curtailment([1])  # Sub1 has only Thermal_C

    assert len(discoverer.identified_renewable_curtailment) == 0


def test_renewable_curtailment_score_in_range(renewable_discoverer):
    """Scores are in [0, 1]."""
    discoverer = renewable_discoverer
    discoverer.find_relevant_renewable_curtailment([0])

    for action_id, score in discoverer.scores_renewable_curtailment.items():
        assert 0 < score <= 1.0, f"Score for {action_id} out of range: {score}"


def test_renewable_curtailment_params_structure(renewable_discoverer):
    """Params contain all expected fields."""
    discoverer = renewable_discoverer
    discoverer.find_relevant_renewable_curtailment([0])

    params = discoverer.params_renewable_curtailment["curtail_Wind_A"]
    assert params["substation"] == "Sub0"
    assert params["gen_name"] == "Wind_A"
    assert params["action_mode"] == "power_reduction"
    assert params["target_p_MW"] == 0.0
    assert "influence_factor" in params
    assert "mw_required" in params
    assert params["gen_p"] == 80.0  # absolute value of -80
    assert params["reduction_MW"] == 80.0
    assert "coverage_ratio" in params


def test_renewable_curtailment_overload_excess(renewable_discoverer):
    """mw_required reflects the overload excess scaled by influence factor."""
    discoverer = renewable_discoverer
    discoverer.find_relevant_renewable_curtailment([0])

    params = discoverer.params_renewable_curtailment["curtail_Wind_A"]
    # mw_required = P_overload_excess * (1+margin) / influence_factor
    # P_overload_excess = (1.2-1.0)*100 = 20 MW, margin=0.05, influence=0.8
    # mw_required = 20*1.05/0.8 = 26.25
    assert params["mw_required"] == round(20.0 * 1.05 / 0.8, 2)


def test_renewable_curtailment_influence_factor(renewable_discoverer):
    """influence_factor = max(neg_in, neg_out) / max_overload_flow.

    In the base fixture, L1 is an out-edge from Sub0 with label=-80.
    neg_out = 80, neg_in = 0 → influence_flow = 80, max_overload_flow = 100.
    influence_factor = 80 / 100 = 0.8.
    """
    discoverer = renewable_discoverer
    discoverer.find_relevant_renewable_curtailment([0])

    # Wind_A (index 0) is at Sub0; only Wind_A and Solar_B are renewable
    params_wind = discoverer.params_renewable_curtailment["curtail_Wind_A"]
    # L1 out-edge from Sub0 has label=-80 → neg_out=80, max_overload_flow=100
    assert params_wind["influence_factor"] == round(80.0 / 100.0, 2)  # 0.8


def test_renewable_curtailment_zero_influence_when_no_negative_blue_edge(renewable_discoverer):
    """Nodes with only positive blue edge flows get influence_factor=0 and score=0."""
    discoverer = renewable_discoverer
    # Override with positive L1 label → no negative outgoing flow from Sub0 → influence=0
    discoverer.g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "100"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })
    discoverer.find_relevant_renewable_curtailment([0])

    # Actions are still created but with zero influence and zero score
    for action_id in discoverer.identified_renewable_curtailment:
        assert discoverer.scores_renewable_curtailment[action_id] == 0.0
        assert discoverer.params_renewable_curtailment[action_id]["influence_factor"] == 0.0


def test_renewable_curtailment_action_uses_power_reduction(renewable_discoverer):
    """Curtailment actions use set_gen_p (power reduction to 0 MW) not disconnection."""
    # Use modified fixture where Sub0 has negative outgoing flow so candidates exist
    mock_obs = MockObservation(
        name_sub=np.array(["Sub0", "Sub1", "Sub2"]),
        name_line=np.array(["L1", "L2"]),
        name_load=np.array(["Load_A"]),
        name_gen=np.array(["Wind_A", "Solar_B"]),
        line_or_to_subid=np.array([0, 1]),
        line_ex_to_subid=np.array([1, 2]),
        line_or_bus=np.array([1, 1]),
        line_ex_bus=np.array([1, 1]),
        rho=np.array([1.2, 0.5]),
        load_to_subid=np.array([2]),
        gen_to_subid=np.array([0, 0]),
        load_values=[50.0],
        gen_values=[-80.0, -40.0],  # Negative = producing (generator convention)
        gen_energy_sources=["WIND", "SOLAR"],
        sub_topologies={0: [1, 1, 1], 1: [1, 1], 2: [1, 1]},
        sub_info=np.array([3, 2, 2]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1]),
        obj_connect_to={
            0: {'loads_id': [], 'generators_id': [0, 1], 'lines_or_id': [0], 'lines_ex_id': []},
            1: {'loads_id': [], 'generators_id': [], 'lines_or_id': [1], 'lines_ex_id': [0]},
            2: {'loads_id': [0], 'generators_id': [], 'lines_or_id': [], 'lines_ex_id': [1]},
        },
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "-80"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })
    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2"],
        aval_nodes=[2],
        amont_nodes=[0],
    )
    discoverer = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[0],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
        simulator_data={}, check_action_simulation=False,
    )

    calls = []
    original = discoverer.action_space
    def capturing(action_dict):
        calls.append(action_dict)
        return original(action_dict)
    discoverer.action_space = capturing

    discoverer.find_relevant_renewable_curtailment([0])

    assert len(calls) == 2
    for call in calls:
        # Must use set_gen_p, NOT set_bus
        assert "set_gen_p" in call, f"Expected set_gen_p key, got: {list(call.keys())}"
        assert "set_bus" not in call, "Should not use set_bus for power reduction"
        gen_p_dict = call["set_gen_p"]
        assert len(gen_p_dict) == 1
        key = list(gen_p_dict.keys())[0]
        assert isinstance(key, str), f"Expected string key, got {type(key).__name__}: {key}"
        assert gen_p_dict[key] == 0.0, f"Expected target_p=0.0, got {gen_p_dict[key]}"
    all_keys = {list(c["set_gen_p"].keys())[0] for c in calls}
    assert all_keys == {"Wind_A", "Solar_B"}


def test_renewable_curtailment_params_contain_power_reduction_fields(renewable_discoverer):
    """Curtailment params include action_mode, target_p_MW, and reduction_MW."""
    discoverer = renewable_discoverer
    discoverer.find_relevant_renewable_curtailment([0])

    params = discoverer.params_renewable_curtailment["curtail_Wind_A"]
    assert params["action_mode"] == "power_reduction"
    assert params["target_p_MW"] == 0.0
    assert params["reduction_MW"] == 80.0  # Wind_A's gen_p (absolute value)


@pytest.mark.parametrize(
    "gens",
    [
        [("Wind_Big", -90.0), ("Wind_Small", -20.0)],
        # Low-score generator first in network order: identified must still
        # come out score-descending, since prioritization takes the first N.
        [("Wind_Small", -20.0), ("Wind_Big", -90.0)],
    ],
    ids=["big_first", "small_first"],
)
def test_renewable_curtailment_higher_power_scores_higher(gens):
    """Within the same amont substation, higher-power generator scores higher."""
    mock_obs = MockObservation(
        name_sub=np.array(["Sub0", "Sub1", "Sub2"]),
        name_line=np.array(["L1", "L2"]),
        name_load=np.array(["Load_A"]),
        name_gen=np.array([name for name, _ in gens]),
        line_or_to_subid=np.array([0, 1]),
        line_ex_to_subid=np.array([1, 2]),
        line_or_bus=np.array([1, 1]),
        line_ex_bus=np.array([1, 1]),
        rho=np.array([1.2, 0.5]),
        load_to_subid=np.array([2]),
        gen_to_subid=np.array([0, 0]),
        load_values=[50.0],
        gen_values=[p for _, p in gens],  # Negative = producing (generator convention)
        gen_energy_sources=["WIND", "WIND"],
        sub_topologies={0: [1, 1, 1], 1: [1, 1], 2: [1, 1]},
        sub_info=np.array([3, 2, 2]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1]),
        obj_connect_to={
            0: {'loads_id': [], 'generators_id': [0, 1], 'lines_or_id': [0], 'lines_ex_id': []},
            1: {'loads_id': [], 'generators_id': [], 'lines_or_id': [1], 'lines_ex_id': [0]},
            2: {'loads_id': [0], 'generators_id': [], 'lines_or_id': [], 'lines_ex_id': [1]},
        },
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "-80"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })
    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2"],
        aval_nodes=[2],
        amont_nodes=[0],
    )
    discoverer = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[0],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
        simulator_data={}, check_action_simulation=False,
    )
    discoverer.find_relevant_renewable_curtailment([0])

    assert len(discoverer.identified_renewable_curtailment) == 2
    score_big = discoverer.scores_renewable_curtailment["curtail_Wind_Big"]
    score_small = discoverer.scores_renewable_curtailment["curtail_Wind_Small"]
    assert score_big > score_small, "Higher-power generator should score higher"

    # Results sorted by score descending
    action_ids = list(discoverer.identified_renewable_curtailment.keys())
    scores = [discoverer.scores_renewable_curtailment[aid] for aid in action_ids]
    assert scores == sorted(scores, reverse=True)
    assert action_ids[0] == "curtail_Wind_Big"


def test_renewable_curtailment_in_action_scores():
    """renewable_curtailment key is present in discover_and_prioritize action_scores."""
    # Use the MockDistributionGraphWithPath fixture-style setup but inline
    mock_obs = MockObservation(
        name_sub=np.array(["Sub0", "Sub1"]),
        name_line=np.array(["L1"]),
        name_load=np.array([]),
        name_gen=np.array(["Gen_A"]),
        line_or_to_subid=np.array([0]),
        line_ex_to_subid=np.array([1]),
        rho=np.array([0.5]),
        load_to_subid=np.array([], dtype=int),
        gen_to_subid=np.array([0]),
        load_values=[],
        gen_values=[50.0],
        gen_energy_sources=["WIND"],
        sub_topologies={0: [1, 1], 1: [1, 1]},
        sub_info=np.array([2, 2]),
        topo_vect=np.array([1, 1, 1, 1]),
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "50"}},
    })

    class MinimalDistGraph:
        def __init__(self):
            self.red_loops = __import__('pandas').DataFrame(columns=["Path"])
        def get_dispatch_edges_nodes(self, only_loop_paths=False): return [], []
        def get_constrained_edges_nodes(self): return [], [], [], []
        def get_constrained_path(self): return None

    discoverer = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=MinimalDistGraph(),
        simulator_data={}, check_action_simulation=False,
    )

    _, action_scores = discoverer.discover_and_prioritize(n_action_max=5)
    assert "renewable_curtailment" in action_scores
    assert "scores" in action_scores["renewable_curtailment"]
    assert "params" in action_scores["renewable_curtailment"]
    assert "non_convergence" in action_scores["renewable_curtailment"]


# ===========================================================================
# Section: Redispatching (find_relevant_redispatch) Tests
# ===========================================================================
# Network topology used in these tests:
#   Sub0 (amont, thermal gen) --L1-- Sub1 (constraint) --L2-- Sub2 (aval, thermal gen)
#   L1 is overloaded at 120% (rho=1.2), capacity=100 MW.
#   Raise dispatchable production downstream (Sub2/aval); lower it upstream
#   (Sub0/amont). Renewable generators are NOT eligible.

@pytest.fixture
def redispatch_discoverer():
    """Provides a discoverer configured for redispatching tests."""
    mock_obs = MockObservation(
        name_sub=np.array(["Sub0", "Sub1", "Sub2"]),
        name_line=np.array(["L1", "L2"]),
        name_load=np.array(["Load_A"]),
        name_gen=np.array(["Therm0", "Therm2", "Wind2"]),
        line_or_to_subid=np.array([0, 1]),
        line_ex_to_subid=np.array([1, 2]),
        line_or_bus=np.array([1, 1]),
        line_ex_bus=np.array([1, 1]),
        rho=np.array([1.2, 0.5]),       # L1 overloaded at 120%
        load_to_subid=np.array([1]),
        gen_to_subid=np.array([0, 2, 2]),  # Therm0 at Sub0; Therm2 + Wind2 at Sub2
        load_values=[50.0],
        gen_values=[-50.0, -30.0, -20.0],  # Negative = producing
        gen_energy_sources=["THERMAL", "NUCLEAR", "WIND"],
        sub_topologies={0: [1, 1], 1: [1, 1], 2: [1, 1, 1, 1]},
        sub_info=np.array([2, 2, 4]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1, 1]),
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))

    # L1 (0->1) label -80: blue, negative -> Sub0 neg_out=80 (amont influence).
    # L2 (1->2) label -60: blue, negative -> Sub2 neg_in=60 (aval influence).
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "-80"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })
    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2"],
        aval_nodes=[2],
        amont_nodes=[0],
    )

    discoverer = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[0],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
        simulator_data={}, check_action_simulation=False,
    )
    return discoverer


def test_redispatch_finds_up_and_down_candidates(redispatch_discoverer):
    """One redispatch action per dispatchable gen: raise downstream, lower upstream."""
    d = redispatch_discoverer
    d.find_relevant_redispatch(nodes_up_indices=[2], nodes_down_indices=[0])
    assert "redispatch_Therm0" in d.identified_redispatch  # amont -> lower
    assert "redispatch_Therm2" in d.identified_redispatch  # aval -> raise


def test_redispatch_skips_renewable(redispatch_discoverer):
    """Renewable generators are not dispatchable and produce no redispatch action."""
    d = redispatch_discoverer
    d.find_relevant_redispatch(nodes_up_indices=[2], nodes_down_indices=[0])
    assert "redispatch_Wind2" not in d.identified_redispatch


def test_redispatch_directions_and_targets(redispatch_discoverer):
    """Up adds the default delta to current production; down subtracts it."""
    d = redispatch_discoverer
    d.find_relevant_redispatch(nodes_up_indices=[2], nodes_down_indices=[0])
    up = d.params_redispatch["redispatch_Therm2"]
    down = d.params_redispatch["redispatch_Therm0"]
    assert up["direction"] == "up"
    assert up["node_type"] == "aval"
    assert up["delta_MW"] == 10.0
    assert up["target_p_MW"] == 40.0  # 30 + 10
    assert down["direction"] == "down"
    assert down["node_type"] == "amont"
    assert down["delta_MW"] == -10.0
    assert down["target_p_MW"] == 40.0  # 50 - 10


def test_redispatch_action_encodes_real_setpoint(redispatch_discoverer):
    """Redispatch encodes the real target_p in set_gen_p (not 0 like curtailment)."""
    d = redispatch_discoverer
    calls = []
    original = d.action_space
    def capturing(action_dict):
        calls.append(action_dict)
        return original(action_dict)
    d.action_space = capturing
    d.find_relevant_redispatch(nodes_up_indices=[2], nodes_down_indices=[0])
    targets = {list(c["set_gen_p"].keys())[0]: list(c["set_gen_p"].values())[0]
               for c in calls if "set_gen_p" in c}
    assert targets["Therm2"] == 40.0
    assert targets["Therm0"] == 40.0
    assert all("set_bus" not in c for c in calls)


def test_redispatch_params_structure(redispatch_discoverer):
    """Params expose the redispatch action_mode and MW fields."""
    d = redispatch_discoverer
    d.find_relevant_redispatch(nodes_up_indices=[2], nodes_down_indices=[0])
    p = d.params_redispatch["redispatch_Therm2"]
    for key in ("substation", "gen_name", "action_mode", "direction",
                "target_p_MW", "delta_MW", "influence_factor", "coverage_ratio"):
        assert key in p
    assert p["action_mode"] == "redispatch"


def test_redispatch_scores_in_range(redispatch_discoverer):
    """Scores are in (0, 1]."""
    d = redispatch_discoverer
    d.find_relevant_redispatch(nodes_up_indices=[2], nodes_down_indices=[0])
    for action_id, score in d.scores_redispatch.items():
        assert 0 < score <= 1.0, f"Score for {action_id} out of range: {score}"


def test_redispatch_in_action_scores():
    """redispatch key is present in discover_and_prioritize action_scores."""
    mock_obs = MockObservation(
        name_sub=np.array(["Sub0", "Sub1"]),
        name_line=np.array(["L1"]),
        name_load=np.array([]),
        name_gen=np.array(["Gen_A"]),
        line_or_to_subid=np.array([0]),
        line_ex_to_subid=np.array([1]),
        rho=np.array([0.5]),
        load_to_subid=np.array([], dtype=int),
        gen_to_subid=np.array([0]),
        load_values=[],
        gen_values=[-50.0],
        gen_energy_sources=["THERMAL"],
        sub_topologies={0: [1, 1], 1: [1, 1]},
        sub_info=np.array([2, 2]),
        topo_vect=np.array([1, 1, 1, 1]),
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "50"}},
    })

    class MinimalDistGraph:
        def __init__(self):
            self.red_loops = __import__('pandas').DataFrame(columns=["Path"])
        def get_dispatch_edges_nodes(self, only_loop_paths=False): return [], []
        def get_constrained_edges_nodes(self): return [], [], [], []
        def get_constrained_path(self): return None

    discoverer = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=MinimalDistGraph(),
        simulator_data={}, check_action_simulation=False,
    )

    _, action_scores = discoverer.discover_and_prioritize(n_action_max=5)
    assert "redispatch" in action_scores
    assert "scores" in action_scores["redispatch"]
    assert "params" in action_scores["redispatch"]
    assert "non_convergence" in action_scores["redispatch"]


# ===========================================================================
# Section: Redispatching on antenna sites (higher-voltage reference)
# ===========================================================================
# Generators usually sit on a radial ("antenne") voltage level absent from the
# influence graph. When the SAME physical site has a higher-voltage (400/225kV)
# busbar that IS in the graph, the generator becomes of interest and the higher
# node is used as the influence/score reference.

def test_redispatch_antenna_uses_higher_voltage_reference():
    # node0 AMONTP6 (225, amont)  --L1(overload)--  node1 CONSTP6 (225, constraint)
    #   --L2-- node2 ZSITEP7 (400, aval, in graph)   ;   node3 ZSITEP3 (63, antenna
    #   gen, NOT a graph node, SAME site 'ZSITE' as node2).
    mock_obs = MockObservation(
        name_sub=np.array(["AMONTP6", "CONSTP6", "ZSITEP7", "ZSITEP3"]),
        name_line=np.array(["L1", "L2"]),
        name_load=np.array([]),
        name_gen=np.array(["Therm_ant"]),
        line_or_to_subid=np.array([0, 1]),
        line_ex_to_subid=np.array([1, 2]),
        line_or_bus=np.array([1, 1]),
        line_ex_bus=np.array([1, 1]),
        rho=np.array([1.2, 0.5]),
        load_to_subid=np.array([], dtype=int),
        gen_to_subid=np.array([3]),          # generator on the antenna VL (node3)
        load_values=[],
        gen_values=[-40.0],                  # producing 40 MW
        gen_energy_sources=["THERMAL"],
        sub_topologies={0: [1, 1], 1: [1, 1], 2: [1, 1], 3: [1, 1]},
        sub_info=np.array([2, 2, 2, 2]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1, 1]),
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
    # L1 (0->1) label -80 (overloaded blue), L2 (1->2) label -60 -> node2 neg_in=60.
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "-80"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })
    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2"],
        aval_nodes=[2],
        amont_nodes=[0],
    )
    d = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[0],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
        simulator_data={}, check_action_simulation=False,
    )

    # Sanity: the same-site higher-voltage map links the 63kV antenna (node3)
    # to the 400kV busbar (node2).
    higher = d._get_site_higher_voltage_map()
    assert higher.get(3) == [2]

    d.find_relevant_redispatch(nodes_up_indices=[2], nodes_down_indices=[0])

    # The antenna generator is discovered (raise), scored via the 400kV node.
    assert "redispatch_Therm_ant" in d.identified_redispatch
    p = d.params_redispatch["redispatch_Therm_ant"]
    assert p["direction"] == "up"
    assert p["substation"] == "ZSITEP3"            # the generator's own VL
    assert p["influence_ref_substation"] == "ZSITEP7"  # higher-voltage reference
    assert p["via_higher_voltage"] is True
    assert p["target_p_MW"] == 50.0               # 40 + 10 (raise)
    assert 0 < d.scores_redispatch["redispatch_Therm_ant"] <= 1.0


def test_redispatch_antenna_skipped_when_no_higher_node_in_graph():
    # Same antenna gen, but the site's higher-voltage node is NOT on the
    # aval/amont side (not in the influence node sets) -> no action.
    mock_obs = MockObservation(
        name_sub=np.array(["AMONTP6", "CONSTP6", "ZSITEP7", "ZSITEP3"]),
        name_line=np.array(["L1", "L2"]),
        name_load=np.array([]),
        name_gen=np.array(["Therm_ant"]),
        line_or_to_subid=np.array([0, 1]),
        line_ex_to_subid=np.array([1, 2]),
        line_or_bus=np.array([1, 1]),
        line_ex_bus=np.array([1, 1]),
        rho=np.array([1.2, 0.5]),
        load_to_subid=np.array([], dtype=int),
        gen_to_subid=np.array([3]),
        load_values=[],
        gen_values=[-40.0],
        gen_energy_sources=["THERMAL"],
        sub_topologies={0: [1, 1], 1: [1, 1], 2: [1, 1], 3: [1, 1]},
        sub_info=np.array([2, 2, 2, 2]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1, 1]),
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "-80"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })
    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2"], aval_nodes=[2], amont_nodes=[0],
    )
    d = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[0],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
        simulator_data={}, check_action_simulation=False,
    )
    # Pass empty up/down node sets: the 400kV reference is in neither -> skip.
    d.find_relevant_redispatch(nodes_up_indices=[], nodes_down_indices=[])
    assert "redispatch_Therm_ant" not in d.identified_redispatch


def test_site_higher_voltage_map_from_pypowsybl_network():
    """The site->higher-voltage map prefers the pypowsybl network
    (substation_id + nominal_v) over the RTE-name heuristic."""
    class _FakeNetwork:
        def __init__(self, df):
            self._df = df
        def get_voltage_levels(self):
            return self._df
    class _FakeNM:
        def __init__(self, net):
            self.network = net

    vl_df = pd.DataFrame(
        {"substation_id": ["BEON", "BEON", "MID"], "nominal_v": [400.0, 63.0, 225.0]},
        index=["WIND_BUS_A", "WIND_BUS_B", "OTHER_BUS"],  # names that defeat the name heuristic
    )
    mock_obs = MockObservation(
        name_sub=np.array(["WIND_BUS_A", "WIND_BUS_B", "OTHER_BUS"]),
        name_line=np.array(["L1"]),
        line_or_to_subid=np.array([0]),
        line_ex_to_subid=np.array([1]),
        rho=np.array([0.5]),
        sub_topologies={0: [1, 1], 1: [1, 1], 2: [1, 1]},
        sub_info=np.array([2, 2, 2]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1]),
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
    mock_env.network_manager = _FakeNM(_FakeNetwork(vl_df))
    mock_g_overflow = MockOverflowGraph(edge_data={(0, 1): {0: {"name": "L1", "capacity": 100.0}}})

    d = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=[], lines_overloaded_ids=[],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=MockDistributionGraph(),
        simulator_data={}, check_action_simulation=False,
    )

    meta = d._get_voltage_level_metadata()
    assert meta[0] == ("BEON", 400.0)
    assert meta[1] == ("BEON", 63.0)
    assert meta[2] == ("MID", 225.0)
    # Antenna 63kV bus (idx 1) references the same-site 400kV bus (idx 0).
    higher = d._get_site_higher_voltage_map()
    assert higher.get(1) == [0]
    assert 0 not in higher          # 400kV has no higher same-site node
    assert 2 not in higher          # MID site has a single VL


def test_renewable_curtailment_antenna_uses_higher_voltage_reference():
    # WIND generator on a 63kV antenna VL (node3, NOT a graph node) whose
    # same physical site has a 400kV busbar (node2) present in the graph.
    mock_obs = MockObservation(
        name_sub=np.array(["AMONTP6", "CONSTP6", "ZSITEP7", "ZSITEP3"]),
        name_line=np.array(["L1", "L2"]),
        name_load=np.array([]),
        name_gen=np.array(["WIND_ant"]),
        line_or_to_subid=np.array([0, 1]),
        line_ex_to_subid=np.array([1, 2]),
        line_or_bus=np.array([1, 1]),
        line_ex_bus=np.array([1, 1]),
        rho=np.array([1.2, 0.5]),
        load_to_subid=np.array([], dtype=int),
        gen_to_subid=np.array([3]),
        load_values=[],
        gen_values=[-40.0],
        gen_energy_sources=["WIND"],
        sub_topologies={0: [1, 1], 1: [1, 1], 2: [1, 1], 3: [1, 1]},
        sub_info=np.array([2, 2, 2, 2]),
        topo_vect=np.array([1, 1, 1, 1, 1, 1, 1, 1]),
    )
    mock_env = MockEnv(name_line=list(mock_obs.name_line), name_sub=list(mock_obs.name_sub))
    mock_g_overflow = MockOverflowGraph(edge_data={
        (0, 1): {0: {"name": "L1", "capacity": 100.0, "label": "-80"}},
        (1, 2): {0: {"name": "L2", "capacity": 60.0, "label": "-60"}},
    })
    mock_g_dist = MockDistributionGraphWithPath(
        constrained_edges=["L1", "L2"], aval_nodes=[2], amont_nodes=[0],
    )
    d = ActionDiscoverer(
        env=mock_env, obs=mock_obs, obs_defaut=mock_obs,
        classifier=ActionClassifier(MockActionSpace()),
        timestep=0, lines_defaut=["L1"], lines_overloaded_ids=[0],
        act_reco_maintenance=MockActionObject(),
        non_connected_reconnectable_lines=[], all_disconnected_lines=[],
        dict_action={}, actions_unfiltered=set(), hubs=[],
        g_overflow=mock_g_overflow, g_distribution_graph=mock_g_dist,
        simulator_data={}, check_action_simulation=False,
    )
    # Curtailment is called with all nodes by the orchestrator.
    d.find_relevant_renewable_curtailment([0, 1, 2, 3])
    assert "curtail_WIND_ant" in d.identified_renewable_curtailment
    p = d.params_renewable_curtailment["curtail_WIND_ant"]
    assert p["substation"] == "ZSITEP3"
    assert p["influence_ref_substation"] == "ZSITEP7"
    assert p["via_higher_voltage"] is True
    assert p["target_p_MW"] == 0.0


def _spy_all_family_methods(d, monkeypatch):
    """Replace every find/verify family method with a call-recording spy."""
    calls = {}
    names = [
        "verify_relevant_reconnections", "find_relevant_node_merging",
        "find_relevant_node_splitting", "find_relevant_disconnections",
        "find_relevant_pst_actions", "find_relevant_load_shedding",
        "find_relevant_renewable_curtailment", "find_relevant_redispatch",
    ]
    for name in names:
        calls[name] = {"called": False}
        def _make(n):
            def _spy(*args, **kwargs):
                calls[n]["called"] = True
            return _spy
        monkeypatch.setattr(d, name, _make(name))
    return calls


def test_allowed_action_types_restricts_discovery_to_redispatch(redispatch_discoverer, monkeypatch):
    from expert_op4grid_recommender import config
    monkeypatch.setattr(config, "ALLOWED_ACTION_TYPES", ["redispatch"], raising=False)
    d = redispatch_discoverer
    calls = _spy_all_family_methods(d, monkeypatch)
    d.discover_and_prioritize(n_action_max=5)
    assert calls["find_relevant_redispatch"]["called"]
    # Families that WOULD have candidates on this fixture are skipped.
    assert not calls["find_relevant_disconnections"]["called"]
    assert not calls["find_relevant_load_shedding"]["called"]
    assert not calls["find_relevant_node_splitting"]["called"]


def test_empty_allowed_action_types_keeps_all_families(redispatch_discoverer, monkeypatch):
    from expert_op4grid_recommender import config
    monkeypatch.setattr(config, "ALLOWED_ACTION_TYPES", [], raising=False)
    d = redispatch_discoverer
    calls = _spy_all_family_methods(d, monkeypatch)
    d.discover_and_prioritize(n_action_max=5)
    # No restriction → families whose conditions hold are all invoked.
    assert calls["find_relevant_redispatch"]["called"]
    assert calls["find_relevant_disconnections"]["called"]
    assert calls["find_relevant_load_shedding"]["called"]
