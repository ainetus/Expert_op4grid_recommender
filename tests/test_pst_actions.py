import pytest
import numpy as np
from unittest.mock import MagicMock, patch
from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace
from expert_op4grid_recommender.action_evaluation.discovery import ActionDiscoverer
from expert_op4grid_recommender.action_evaluation.discovery._results import (
    DisconnectionBounds,
)
from expert_op4grid_recommender.action_evaluation.classifier import ActionClassifier
from expert_op4grid_recommender.pypowsybl_backend.action_space import PhaseShifterAction

@pytest.fixture
def mock_nm():
    nm = MagicMock(spec=NetworkManager)
    nm.network = MagicMock()
    nm._line_or_sub = {"PST1": "SUB1"}
    nm._line_ex_sub = {"PST1": "SUB2"}
    nm._pst_ids = ["PST1"]
    nm._pst_set = {"PST1"}
    nm.get_pst_ids.return_value = ["PST1"]
    nm.get_pst_tap_info.return_value = {
        'tap': 10,
        'low_tap': 0,
        'high_tap': 20,
        'step_count': 21
    }
    return nm

@pytest.fixture
def mock_obs(mock_nm):
    obs = MagicMock()
    obs._network_manager = mock_nm
    obs.name_sub = ["SUB1", "SUB2", "SUB3"]
    obs.name_line = ["LINE1", "PST1"]
    return obs

def test_phase_shifter_action_apply(mock_nm):
    action = PhaseShifterAction({"PST1": 12})
    action.apply(mock_nm)
    mock_nm.update_pst_tap_step.assert_called_once_with("PST1", 12)

def test_action_classifier_pst_tap():
    classifier = ActionClassifier()
    desc = {"description": "Variation de slot de 2 pour le PST PST1"}
    assert classifier.identify_action_type(desc) == "pst_tap"
    
    desc_tap = {"description": "PST tap Change"}
    assert classifier.identify_action_type(desc_tap) == "pst_tap"

def test_pst_discovery_blue_path(mock_obs, mock_nm):
    action_space = ActionSpace(mock_nm)
    mock_env = MagicMock()
    mock_env.action_space = action_space
    
    # Provide dummy values for required arguments
    discoverer = ActionDiscoverer(
        env=mock_env,
        obs=mock_obs,
        obs_defaut=MagicMock(),
        timestep=0,
        lines_defaut=[],
        lines_overloaded_ids=[],
        act_reco_maintenance=MagicMock(),
        classifier=MagicMock(),
        non_connected_reconnectable_lines=[],
        all_disconnected_lines=[],
        dict_action={},
        actions_unfiltered=set(),
        hubs=[],
        g_overflow=MagicMock(),
        g_distribution_graph=MagicMock(),
        simulator_data={},
        check_action_simulation=False
    )
    
    # Mock bounds and capacity map for scoring
    discoverer._cached_disconnection_bounds = DisconnectionBounds(
        max_overload_flow=100.0, min_redispatch=10.0, max_redispatch=50.0,
        capacity_map={"PST1": 40.0},  # dispatch_flow = 40.0
    )
    
    # Blue path: SUB1 is constrained
    nodes_blue = ["SUB1"]
    red_loops = []
    
    discoverer.find_relevant_pst_actions(nodes_blue, red_loops)
    
    assert "pst_tap_PST1_inc2" in discoverer.identified_pst_actions
    action = discoverer.identified_pst_actions["pst_tap_PST1_inc2"]
    assert action.pst_tap == {"PST1": 12} # tap (10) >= ref (10) -> inc2
    
    # Check details
    details = discoverer.params_pst_actions["pst_tap_PST1_inc2"]
    assert details["is_blue"] == True
    assert details["variation"] == 2
    assert details["max_reachable_tap"] == 20
    assert details["dispatch_flow_on_pst"] == 40.0
    assert details["affected_line"] == "PST1"
    
    # Check score: abs(40/100) = 0.4
    assert discoverer.scores_pst_actions["pst_tap_PST1_inc2"] == 0.4

def test_pst_discovery_red_loop(mock_obs, mock_nm):
    action_space = ActionSpace(mock_nm)
    mock_env = MagicMock()
    mock_env.action_space = action_space
    
    discoverer = ActionDiscoverer(
        env=mock_env,
        obs=mock_obs,
        obs_defaut=MagicMock(),
        timestep=0,
        lines_defaut=[],
        lines_overloaded_ids=[],
        act_reco_maintenance=MagicMock(),
        classifier=MagicMock(),
        non_connected_reconnectable_lines=[],
        all_disconnected_lines=[],
        dict_action={},
        actions_unfiltered=set(),
        hubs=[],
        g_overflow=MagicMock(),
        g_distribution_graph=MagicMock(),
        simulator_data={},
        check_action_simulation=False
    )
    
    # Mock bounds and capacity map for scoring
    discoverer._cached_disconnection_bounds = DisconnectionBounds(
        max_overload_flow=100.0, min_redispatch=10.0, max_redispatch=50.0,
        capacity_map={"PST1": 25.0},  # dispatch_flow = 25.0
    )
    
    # Red loop: SUB1 is on loop
    nodes_blue = []
    red_loops = [["SUB1", "SUB2"]]
    
    # Current tap is 10, ref is 10. Rule for red loop: move towards ref. 
    # If tap == ref, variation should be 0 (no change needed to decrease impedance)
    discoverer.find_relevant_pst_actions(nodes_blue, red_loops)
    assert len(discoverer.identified_pst_actions) == 0
    
    # Change current tap to be above ref
    mock_nm.get_pst_tap_info.return_value['tap'] = 15
    discoverer.find_relevant_pst_actions(nodes_blue, red_loops)
    
    assert "pst_tap_PST1_dec2" in discoverer.identified_pst_actions
    action = discoverer.identified_pst_actions["pst_tap_PST1_dec2"]
    assert action.pst_tap == {"PST1": 13} # tap (15) > ref (10) -> dec2 towards ref
    
    # Check score: abs(25/100) = 0.25
    assert discoverer.scores_pst_actions["pst_tap_PST1_dec2"] == 0.25

def test_pst_discovery_blue_path_inc_imp(mock_obs, mock_nm):
    action_space = ActionSpace(mock_nm)
    mock_env = MagicMock()
    mock_env.action_space = action_space
    
    discoverer = ActionDiscoverer(
        env=mock_env,
        obs=mock_obs,
        obs_defaut=MagicMock(),
        timestep=0,
        lines_defaut=[],
        lines_overloaded_ids=[],
        act_reco_maintenance=MagicMock(),
        classifier=MagicMock(),
        non_connected_reconnectable_lines=[],
        all_disconnected_lines=[],
        dict_action={},
        actions_unfiltered=set(),
        hubs=[],
        g_overflow=MagicMock(),
        g_distribution_graph=MagicMock(),
        simulator_data={},
        check_action_simulation=False
    )
    
    # Mock bounds and capacity map for scoring
    discoverer._cached_disconnection_bounds = DisconnectionBounds(
        max_overload_flow=100.0, min_redispatch=10.0, max_redispatch=50.0,
        capacity_map={"PST1": 60.0},  # dispatch_flow = 60.0
    )
    
    # Current tap < ref (10)
    mock_nm.get_pst_tap_info.return_value['tap'] = 5
    nodes_blue = ["SUB1"]
    
    discoverer.find_relevant_pst_actions(nodes_blue, [])
    
    assert "pst_tap_PST1_dec2" in discoverer.identified_pst_actions
    action = discoverer.identified_pst_actions["pst_tap_PST1_dec2"]
    assert action.pst_tap == {"PST1": 3} # tap (5) < ref (10) -> dec2 (more impedance)
    
    # Check score: abs(60/100) = 0.6
    assert discoverer.scores_pst_actions["pst_tap_PST1_dec2"] == 0.6

def test_pypowsybl_action_add_pst():
    from expert_op4grid_recommender.pypowsybl_backend.observation import PypowsyblAction
    
    a1 = PypowsyblAction()
    a1.pst_tap = {"PST1": 12}
    
    a2 = PypowsyblAction()
    a2.pst_tap = {"PST2": 15}
    
    combined = a1 + a2
    assert combined.pst_tap == {"PST1": 12, "PST2": 15}
    
    # Check overwrite
    a3 = PypowsyblAction()
    a3.pst_tap = {"PST1": 13}
    combined2 = combined + a3
    assert combined2.pst_tap["PST1"] == 13

def test_pst_discovery_at_limit(mock_obs, mock_nm):
    action_space = ActionSpace(mock_nm)
    mock_env = MagicMock()
    mock_env.action_space = action_space
    
    discoverer = ActionDiscoverer(
        env=mock_env,
        obs=mock_obs,
        obs_defaut=MagicMock(),
        timestep=0,
        lines_defaut=[],
        lines_overloaded_ids=[],
        act_reco_maintenance=MagicMock(),
        classifier=MagicMock(),
        non_connected_reconnectable_lines=[],
        all_disconnected_lines=[],
        dict_action={},
        actions_unfiltered=set(),
        hubs=[],
        g_overflow=MagicMock(),
        g_distribution_graph=MagicMock(),
        simulator_data={},
        check_action_simulation=False
    )
    
    # Blue path, tap already at HIGH limit
    mock_nm.get_pst_tap_info.return_value = {
        'tap': 20,
        'low_tap': 0,
        'high_tap': 20,
        'step_count': 21
    }
    nodes_blue = ["SUB1"]
    
    # Mock bounds to avoid division by zero if scoring happens
    discoverer._cached_disconnection_bounds = DisconnectionBounds(
        max_overload_flow=100.0, min_redispatch=10.0, max_redispatch=50.0,
        capacity_map={},
    )
    
    discoverer.find_relevant_pst_actions(nodes_blue, [])
    assert len(discoverer.identified_pst_actions) == 0

def test_action_classifier_combined_pst():
    classifier = ActionClassifier()
    
    # Mock action object with pst_tap and necessary grid2op attributes
    # We use numpy arrays to avoid TypeErrors in comparisons within etc.
    action = MagicMock()
    action.pst_tap = {"PST1": 12}
    
    # Mock topology attributes required by classifier methods
    action._topo_vect_to_sub = np.array([0, 0, 1, 1])
    action._set_topo_vect = np.zeros(4, dtype=int)
    action.line_change_status = np.zeros(2, dtype=int)
    action.line_or_change_bus = np.zeros(2, dtype=int)
    action.line_ex_change_bus = np.zeros(2, dtype=int)
    action.line_or_set_bus = np.zeros(2, dtype=int)
    action.line_ex_set_bus = np.zeros(2, dtype=int)
    action.line_set_status = np.zeros(2, dtype=int)
    action.load_change_bus = np.zeros(2, dtype=int)
    action.load_set_bus = np.zeros(2, dtype=int)
    
    assert classifier.identify_grid2op_action_type(grid2op_action=action) == "pst_tap"
    
    # Mock combined action
    action.pst_tap = {"PST1": 12}
    action.lines_or_bus = {"LINE1": 1}
    assert classifier.identify_grid2op_action_type(grid2op_action=action) == "pst_tap"

def test_pst_identified_sorted_by_score_not_network_order(mock_obs, mock_nm):
    """Regression: the MIN_PST floor must take the best-scored PST.

    add_prioritized_actions walks ``identified`` in insertion order, so a
    low-score PST listed first in the network (IIDM) order used to win the
    single MIN_PST=1 slot over a far better one (real case: MUR Y661 at
    0.04 ahead of NIORTY665 at 0.94).
    """
    from expert_op4grid_recommender.utils.helpers import add_prioritized_actions

    mock_nm._line_or_sub = {"MUR Y661": "SUB1", "NIORTY665": "SUB1"}
    mock_nm._line_ex_sub = {"MUR Y661": "SUB2", "NIORTY665": "SUB2"}
    mock_nm.get_pst_ids.return_value = ["MUR Y661", "NIORTY665"]
    mock_nm.get_pst_tap_info.return_value = {
        "tap": 17, "low_tap": 1, "high_tap": 33, "step_count": 33
    }

    action_space = ActionSpace(mock_nm)
    mock_env = MagicMock()
    mock_env.action_space = action_space
    discoverer = ActionDiscoverer(
        env=mock_env,
        obs=mock_obs,
        obs_defaut=MagicMock(),
        timestep=0,
        lines_defaut=[],
        lines_overloaded_ids=[],
        act_reco_maintenance=MagicMock(),
        classifier=MagicMock(),
        non_connected_reconnectable_lines=[],
        all_disconnected_lines=[],
        dict_action={},
        actions_unfiltered=set(),
        hubs=[],
        g_overflow=MagicMock(),
        g_distribution_graph=MagicMock(),
        simulator_data={},
        check_action_simulation=False
    )
    discoverer._cached_disconnection_bounds = DisconnectionBounds(
        max_overload_flow=391.5, min_redispatch=10.0, max_redispatch=50.0,
        capacity_map={"MUR Y661": 15.7, "NIORTY665": 368.0},
    )

    discoverer.find_relevant_pst_actions(["SUB1"], [])

    assert list(discoverer.identified_pst_actions) == [
        "pst_tap_NIORTY665_inc2",
        "pst_tap_MUR Y661_inc2",
    ]
    selected = add_prioritized_actions(
        {}, discoverer.identified_pst_actions, 15, n_action_max_per_type=1
    )
    assert list(selected) == ["pst_tap_NIORTY665_inc2"]

def test_pst_prioritization(mock_obs, mock_nm):
    action_space = ActionSpace(mock_nm)
    mock_env = MagicMock()
    mock_env.action_space = action_space
    
    discoverer = ActionDiscoverer(
        env=mock_env,
        obs=mock_obs,
        obs_defaut=MagicMock(),
        timestep=0,
        lines_defaut=[],
        lines_overloaded_ids=[],
        act_reco_maintenance=MagicMock(),
        classifier=MagicMock(),
        non_connected_reconnectable_lines=[],
        all_disconnected_lines=[],
        dict_action={},
        actions_unfiltered=set(),
        hubs=[],
        g_overflow=MagicMock(),
        g_distribution_graph=MagicMock(),
        simulator_data={},
        check_action_simulation=False
    )
    
    # Setup identified actions
    discoverer.identified_reconnections = {"reco1": MagicMock(), "reco2": MagicMock(), "reco3": MagicMock()}
    discoverer.identified_merges = {"merge1": MagicMock(), "merge2": MagicMock()}
    discoverer.identified_splits = {"split1": MagicMock()}
    discoverer.identified_disconnections = {"disco1": MagicMock()}
    
    # PST actions
    p1 = MagicMock()
    p2 = MagicMock()
    discoverer.identified_pst_actions = {"pst1": p1, "pst2": p2}
    
    # Mock scoring (descending)
    discoverer.scores_reconnections = {"reco1": 0.9, "reco2": 0.8, "reco3": 0.7}
    discoverer.scores_merges = {"merge1": 0.9, "merge2": 0.8}
    discoverer.scores_splits = {"split1": 0.9}
    discoverer.scores_disconnections = {"disco1": 0.9}
    discoverer.scores_pst_actions = {"pst1": 1.0, "pst2": 0.9} # PST has high scores
    
    # Run prioritization with small total limit and MIN_PST=2
    # Ensure we patch the correct config module used in discovery.py
    with patch('expert_op4grid_recommender.config.MIN_LINE_RECONNECTIONS', 1), \
         patch('expert_op4grid_recommender.config.MIN_CLOSE_COUPLING', 0), \
         patch('expert_op4grid_recommender.config.MIN_OPEN_COUPLING', 0), \
         patch('expert_op4grid_recommender.config.MIN_LINE_DISCONNECTIONS', 0), \
         patch('expert_op4grid_recommender.config.MIN_PST', 2):
        
        # Mock g_distribution_graph
        discoverer.g_distribution_graph = MagicMock()
        discoverer.g_distribution_graph.get_dispatch_edges_nodes.return_value = ([], [])
        discoverer.g_distribution_graph.get_red_loops_names.return_value = []
        discoverer.g_distribution_graph.get_constrained_edges_nodes.return_value = ([], [], [], [])
        
        # Mock find techniques to not clear our manually set identified_ actions
        with patch.object(discoverer, 'verify_relevant_reconnections'), \
             patch.object(discoverer, 'find_relevant_node_merging'), \
             patch.object(discoverer, 'find_relevant_node_splitting'), \
             patch.object(discoverer, 'find_relevant_disconnections'), \
             patch.object(discoverer, 'find_relevant_pst_actions'):
            
            prioritized, action_scores = discoverer.discover_and_prioritize(n_action_max=5)
            
            # Check if PST actions are included because of MIN_PST=2
            assert "pst1" in prioritized
            assert "pst2" in prioritized
            # Total should be capped at 5
            assert len(prioritized) == 5

def test_pst_recommender_enrichment():
    import sys
    
    # Robustly find ExpertAssist directory
    # If we are in /home/marotant/dev/Expert_op4grid_recommender/tests/test_pst_actions.py
    # we want to add /home/marotant/dev/AntiGravity/ExpertAssist
    expert_assist_path = "/home/marotant/dev/AntiGravity/ExpertAssist"
    if expert_assist_path not in sys.path:
        sys.path.insert(0, expert_assist_path)
        
    try:
        from expert_backend.services.recommender_service import RecommenderService
    except ImportError as e:
        pytest.skip(f"expert_backend not in path: {e}")
        
    service = RecommenderService()
    
    # Mock prioritised actions dict
    action_obj = MagicMock()
    action_obj.pst_tap = {"PST1": 12}
    # It must have other attributes too
    action_obj.lines_or_bus = {}
    action_obj.lines_ex_bus = {}
    action_obj.gens_bus = {}
    action_obj.loads_bus = {}
    action_obj.substations = {}
    action_obj.switches = {}
    
    prioritized = {
        "pst_action": {
            "action": action_obj,
            "description_unitaire": "PST variation",
            "rho_before": [0.5, 0.6],
            "rho_after": [0.4, 0.5],
            "max_rho": 0.6
        }
    }
    
    enriched = service._enrich_actions(prioritized)
    
    assert "pst_action" in enriched
    assert "action_topology" in enriched["pst_action"]
    assert enriched["pst_action"]["action_topology"]["pst_tap"] == {"PST1": 12}
