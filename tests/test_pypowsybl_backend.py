# tests/test_pypowsybl_backend.py
"""
Tests for the pypowsybl backend.

These tests verify that the pypowsybl backend provides equivalent functionality
to the grid2op-based implementation.
"""

import pytest
import numpy as np
from pathlib import Path
import tempfile
import expert_op4grid_recommender.config as configuration_module
import expert_op4grid_recommender
expert_op4grid_recommender.config = configuration_module

# Skip all tests if pypowsybl is not available
pypowsybl = pytest.importorskip("pypowsybl")


class TestNetworkManager:
    """Tests for NetworkManager class."""
    
    @pytest.fixture
    def sample_network(self):
        """Create a simple test network."""
        # Create a simple 3-bus network
        network = pypowsybl.network.create_ieee9()
        return network
    
    def test_network_manager_creation(self, sample_network):
        """Test that NetworkManager can be created from a network."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager
        
        nm = NetworkManager(network=sample_network)
        
        assert nm.n_line > 0
        assert nm.n_sub > 0
        assert len(nm.name_line) == nm.n_line
        assert len(nm.name_sub) == nm.n_sub
    
    def test_variant_management(self, sample_network):
        """Test variant creation and switching."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager
        
        nm = NetworkManager(network=sample_network)
        
        # Create a variant
        variant_id = nm.create_variant("test_variant")
        assert variant_id == "test_variant"
        
        # Switch to it
        nm.set_working_variant("test_variant")
        
        # Make a change
        if nm.n_line > 0:
            first_line = nm.name_line[0]
            nm.disconnect_line(first_line)
        
        # Switch back to base
        nm.reset_to_base()
        
        # Clean up
        nm.remove_variant("test_variant")
    
    def test_load_flow(self, sample_network):
        """Test load flow execution."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager
        
        nm = NetworkManager(network=sample_network)
        
        # Run AC load flow
        result = nm.run_load_flow(dc=False)
        assert result is not None
        
        # Run DC load flow
        result_dc = nm.run_load_flow(dc=True)
        assert result_dc is not None


class TestActionSpace:
    """Tests for ActionSpace class."""
    
    @pytest.fixture
    def env_with_action_space(self):
        """Create environment with action space."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace
        
        network = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=network)
        action_space = ActionSpace(nm)
        
        return nm, action_space
    
    def test_line_disconnection_action(self, env_with_action_space):
        """Test creating a line disconnection action."""
        nm, action_space = env_with_action_space
        
        if nm.n_line > 0:
            first_line = nm.name_line[0]
            action = action_space({"set_line_status": [(first_line, -1)]})
            
            assert action is not None
            assert len(action._modifications) > 0
    
    def test_line_reconnection_action(self, env_with_action_space):
        """Test creating a line reconnection action."""
        nm, action_space = env_with_action_space
        
        if nm.n_line > 0:
            first_line = nm.name_line[0]
            action = action_space({"set_line_status": [(first_line, 1)]})
            
            assert action is not None
    
    def test_bus_action(self, env_with_action_space):
        """Test creating a bus topology action."""
        nm, action_space = env_with_action_space
        
        if nm.n_line > 0:
            first_line = nm.name_line[0]
            action = action_space({
                "set_bus": {
                    "lines_or_id": {first_line: 1},
                    "lines_ex_id": {first_line: 1}
                }
            })
            
            assert action is not None
    
    def test_action_combination(self, env_with_action_space):
        """Test combining multiple actions."""
        nm, action_space = env_with_action_space
        
        if nm.n_line >= 2:
            line1 = nm.name_line[0]
            line2 = nm.name_line[1]
            
            action1 = action_space({"set_line_status": [(line1, -1)]})
            action2 = action_space({"set_line_status": [(line2, -1)]})
            
            combined = action1 + action2
            
            assert combined is not None
            assert len(combined._modifications) == 2
    
    def test_switch_action_creation(self, env_with_action_space):
        """Test creating a switch action for topology changes."""
        nm, action_space = env_with_action_space
        
        # Create action with switches field (short names)
        switch_states = {
            "SWITCH_1": True,   # Open
            "SWITCH_2": False,  # Closed
        }
        action = action_space({"switches": switch_states})
        
        assert action is not None
        assert len(action._modifications) > 0
    
    def test_switch_action_empty_dict(self, env_with_action_space):
        """Test creating a switch action with empty switches dict."""
        nm, action_space = env_with_action_space
        
        # Empty switches should not add modifications
        action = action_space({"switches": {}})
        
        assert action is not None
        # Empty switches dict should not add a SwitchAction
        assert len(action._modifications) == 0
    
    def test_combined_bus_and_switch_action(self, env_with_action_space):
        """Test creating an action with both set_bus and switches."""
        nm, action_space = env_with_action_space
        
        if nm.n_line > 0:
            first_line = nm.name_line[0]
            action = action_space({
                "set_bus": {
                    "lines_or_id": {first_line: 1},
                    "lines_ex_id": {first_line: 1}
                },
                "switches": {
                    "SWITCH_1": True,
                }
            })
            
            assert action is not None
            # Should have both BusAction and SwitchAction modifications
            assert len(action._modifications) == 2
    
    def test_switch_action_with_line_status(self, env_with_action_space):
        """Test creating an action combining line status and switches."""
        nm, action_space = env_with_action_space
        
        if nm.n_line > 0:
            first_line = nm.name_line[0]
            action = action_space({
                "set_line_status": [(first_line, -1)],
                "switches": {
                    "SWITCH_1": True,
                }
            })
            
            assert action is not None
            assert len(action._modifications) == 2
    
    def test_switches_by_voltage_level_action(self, env_with_action_space):
        """Test creating an action using switches at top level (full IDs)."""
        nm, action_space = env_with_action_space
        
        # Create action with switches at top level (full switch IDs)
        # This is the new format from conversion_actions_repas
        action_dict = {
            "content": {"set_bus": {}},
            "switches": {
                "VL1_SWITCH_1": True,
                "VL1_SWITCH_2": False,
                "VL2_COUPL_DJ": True,
            }
        }
        action = action_space(action_dict)
        
        assert action is not None
        assert len(action._modifications) > 0
    
    def test_switches_at_top_level(self, env_with_action_space):
        """Test that switches at top level are correctly handled."""
        nm, action_space = env_with_action_space
        
        # New format: switches outside content
        action = action_space({
            "content": {"set_bus": {}},
            "switches": {"VL1_SW1": True}
        })
        
        assert action is not None
        # Should have one switch action
        assert len(action._modifications) == 1


class TestObservation:
    """Tests for PypowsyblObservation class."""
    
    @pytest.fixture
    def observation(self):
        """Create a test observation."""
        from expert_op4grid_recommender.pypowsybl_backend import (
            NetworkManager, ActionSpace, PypowsyblObservation
        )
        
        network = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=network)
        action_space = ActionSpace(nm)
        
        # Run load flow first
        nm.run_load_flow()
        
        obs = PypowsyblObservation(nm, action_space)
        return obs, nm, action_space
    
    def test_observation_properties(self, observation):
        """Test that observation has required properties."""
        obs, nm, _ = observation
        
        # Check array properties
        assert isinstance(obs.rho, np.ndarray)
        assert isinstance(obs.line_status, np.ndarray)
        assert isinstance(obs.theta_or, np.ndarray)
        assert isinstance(obs.theta_ex, np.ndarray)
        assert isinstance(obs.load_p, np.ndarray)
        assert isinstance(obs.gen_p, np.ndarray)
        
        # Check name arrays
        assert isinstance(obs.name_line, np.ndarray)
        assert isinstance(obs.name_sub, np.ndarray)
        
        # Check lengths match
        assert len(obs.rho) == nm.n_line
        assert len(obs.line_status) == nm.n_line
    
    def test_generator_pmax_and_setpoints(self, observation):
        """``gen_pmax`` / ``gen_target_p`` follow the network's ``max_p`` /
        ``target_p``, aligned with ``name_gen`` (``set_gen_p`` sets the latter)."""
        obs, nm, _ = observation
        gens = nm.network.get_generators().reindex(obs.name_gen)
        np.testing.assert_allclose(obs.gen_pmax, gens["max_p"])
        np.testing.assert_allclose(obs.gen_target_p, gens["target_p"])

    def test_simulate_method(self, observation):
        """Test the simulate method."""
        obs, nm, action_space = observation
        
        if nm.n_line > 0:
            first_line = nm.name_line[0]
            action = action_space({"set_line_status": [(first_line, -1)]})
            
            obs_simu, reward, done, info = obs.simulate(action)
            
            # Should return an observation
            assert obs_simu is not None
            assert isinstance(obs_simu.rho, np.ndarray)
            
            # Info should have exception key
            assert "exception" in info

    def test_max_rho_both_extremities(self, observation):
        """Test max rho from both extremities calculation."""
        from unittest.mock import patch
        obs, nm, action_space = observation
        
        with patch('expert_op4grid_recommender.config.MAX_RHO_BOTH_EXTREMITIES', True):
            obs._refresh_state()
            rho_both = obs.rho.copy()
            
        with patch('expert_op4grid_recommender.config.MAX_RHO_BOTH_EXTREMITIES', False):
            obs._refresh_state()
            rho_single = obs.rho.copy()
        
        assert len(rho_both) == nm.n_line
        assert len(rho_single) == nm.n_line
        assert np.all(rho_both >= rho_single)


class TestSimulationEnvironment:
    """Tests for SimulationEnvironment class."""
    
    @pytest.fixture
    def temp_network_file(self):
        """Create a temporary network file."""
        network = pypowsybl.network.create_ieee9()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            network_path = Path(tmpdir) / "test_network.xiidm"
            network.save(str(network_path))
            yield network_path
    
    def test_environment_creation(self, temp_network_file):
        """Test creating environment from file."""
        from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment
        
        env = SimulationEnvironment(network_path=temp_network_file)
        
        assert env.n_line > 0
        assert env.n_sub > 0
        assert env.action_space is not None
    
    def test_get_obs(self, temp_network_file):
        """Test getting observation."""
        from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment
        
        env = SimulationEnvironment(network_path=temp_network_file)
        obs = env.get_obs()
        
        assert obs is not None
        assert isinstance(obs.rho, np.ndarray)
    
    def test_thermal_limits(self, temp_network_file):
        """Test thermal limit handling."""
        from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment
        
        env = SimulationEnvironment(network_path=temp_network_file)
        
        limits = env.get_thermal_limit()
        assert len(limits) == env.n_line
        
        # Set new limits
        new_limits = np.ones(env.n_line) * 1000.0
        env.set_thermal_limit(new_limits)
        
        updated_limits = env.get_thermal_limit()
        np.testing.assert_array_equal(updated_limits, new_limits)


class TestOverflowAnalysis:
    """Tests for overflow analysis functionality."""
    
    @pytest.fixture
    def overflow_setup(self):
        """Setup for overflow analysis tests."""
        from expert_op4grid_recommender.pypowsybl_backend import (
            NetworkManager, ActionSpace, PypowsyblObservation
        )
        
        network = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=network)
        action_space = ActionSpace(nm)
        nm.run_load_flow()
        obs = PypowsyblObservation(nm, action_space)
        
        return nm, action_space, obs
    
    def test_overflow_simulator_creation(self, overflow_setup):
        """Test creating overflow simulator."""
        from expert_op4grid_recommender.pypowsybl_backend import OverflowSimulator
        
        nm, action_space, obs = overflow_setup
        
        # Test AC mode
        sim_ac = OverflowSimulator(nm, obs, use_dc=False)
        assert sim_ac is not None
        
        # Test DC mode
        sim_dc = OverflowSimulator(nm, obs, use_dc=True)
        assert sim_dc is not None
    
    def test_overflow_graph_nodes_carry_their_injections(self, overflow_setup):
        """Each node of the overflow graph reads its own N-1 injection: the
        generator buses are production nodes (coral), the load buses
        consumption nodes. alphaDeesp's build_nodes takes one value per flagged
        substation; a full per-substation array shifted the values onto other
        nodes, and the receptor sign made production negative."""
        from alphaDeesp.core.graphsAndPaths import OverFlowGraph
        from expert_op4grid_recommender.pypowsybl_backend.overflow_analysis import (
            AlphaDeespAdapter,
        )
        from expert_op4grid_recommender.utils.injections import injections_by_sub

        nm, action_space, obs = overflow_setup
        cut = list(nm.name_line).index("L7-5-0")
        adapter = AlphaDeespAdapter(obs, action_space, None, {}, ltc=[cut])
        g = OverFlowGraph(adapter.topo, [cut], adapter.get_dataframe(),
                          float_precision="%.0f").g

        prod, load = injections_by_sub(obs)
        for sub, name in enumerate(nm.name_sub):
            assert float(g.nodes[sub]["value"]) == pytest.approx(prod[sub] - load[sub]), name
        producing = {nm.name_sub[s] for s in g.nodes if g.nodes[s]["prod_or_load"] == "prod"}
        assert producing == {"VL1", "VL2", "VL3"}  # the three generator buses
        assert all(g.nodes[s]["fillcolor"] == "coral" for s in g.nodes
                   if nm.name_sub[s] in producing)

    def test_flow_changes_computation(self, overflow_setup):
        """Test computing flow changes after disconnection."""
        from expert_op4grid_recommender.pypowsybl_backend import OverflowSimulator

        nm, action_space, obs = overflow_setup
        sim = OverflowSimulator(nm, obs, use_dc=True)

        if nm.n_line > 0:
            first_line = nm.name_line[0]
            df = sim.compute_flow_changes_after_disconnection([first_line])
            
            assert df is not None
            assert 'line_name' in df.columns
            assert 'delta_flows' in df.columns
            assert len(df) == nm.n_line


class TestSwitchAction:
    """Tests for the SwitchAction class."""
    
    def test_switch_action_import(self):
        """Test that SwitchAction can be imported."""
        from expert_op4grid_recommender.pypowsybl_backend.action_space import SwitchAction
        assert SwitchAction is not None
    
    def test_switch_action_creation(self):
        """Test creating a SwitchAction with switch states."""
        from expert_op4grid_recommender.pypowsybl_backend.action_space import SwitchAction
        
        switch_states = {
            "SWITCH_1": True,   # Open
            "SWITCH_2": False,  # Closed
        }
        action = SwitchAction(switch_states)
        
        assert action is not None
        assert len(action._modifications) == 1
    
    def test_switch_action_empty_dict(self):
        """Test creating a SwitchAction with empty dict."""
        from expert_op4grid_recommender.pypowsybl_backend.action_space import SwitchAction
        
        action = SwitchAction({})
        
        assert action is not None
        assert len(action._modifications) == 1
    
    def test_switch_action_combination_with_pypowsybl_action(self):
        """Test combining SwitchAction with PypowsyblAction."""
        from expert_op4grid_recommender.pypowsybl_backend.action_space import (
            SwitchAction, PypowsyblAction
        )
        
        switch_action = SwitchAction({"SW1": True})
        other_action = PypowsyblAction()
        
        combined = switch_action + other_action
        
        assert combined is not None
        assert len(combined._modifications) >= 1
    
    def test_switch_action_with_real_network(self):
        """Test SwitchAction with a real pypowsybl network."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager
        from expert_op4grid_recommender.pypowsybl_backend.action_space import SwitchAction
        
        network = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=network)
        
        # Get actual switch IDs from the network
        switches_df = network.get_switches()
        if len(switches_df) > 0:
            switch_id = switches_df.index[0]
            switch_states = {switch_id: True}  # Open the switch
            
            action = SwitchAction(switch_states)
            
            # Create a variant to test
            nm.create_variant("test_switch")
            nm.set_working_variant("test_switch")
            
            # Apply the action
            action.apply(nm)
            
            # Verify the switch state changed
            new_switches = network.get_switches()
            assert new_switches.loc[switch_id, 'open'] == True
            
            # Cleanup
            nm.reset_to_base()
            nm.remove_variant("test_switch")


class TestIntegration:
    """Integration tests comparing pypowsybl backend behavior."""
    
    def test_contingency_analysis_workflow(self):
        """Test a complete contingency analysis workflow."""
        from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment
        
        # Create test network
        network = pypowsybl.network.create_ieee9()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            network_path = Path(tmpdir) / "test_network.xiidm"
            network.save(str(network_path))
            
            # Create environment
            env = SimulationEnvironment(network_path=network_path)
            obs = env.get_obs()
            
            # Get line names
            line_names = list(env.name_line)
            if len(line_names) > 0:
                # Create contingency action
                contingency_line = line_names[0]
                action = env.action_space({
                    "set_line_status": [(contingency_line, -1)]
                })
                
                # Simulate
                obs_simu, _, done, info = obs.simulate(action)
                
                # Check results
                assert obs_simu is not None
                
                # Find overloaded lines
                overloaded = np.where(obs_simu.rho >= 1.0)[0]
                print(f"Overloaded lines after contingency: {len(overloaded)}")
    
    def test_action_with_switches_workflow(self):
        """Test workflow using action with switches field."""
        from expert_op4grid_recommender.pypowsybl_backend import SimulationEnvironment
        
        network = pypowsybl.network.create_ieee9()
        
        with tempfile.TemporaryDirectory() as tmpdir:
            network_path = Path(tmpdir) / "test_network.xiidm"
            network.save(str(network_path))
            
            env = SimulationEnvironment(network_path=network_path)
            
            # Get actual switches from the network
            switches_df = env.network_manager.network.get_switches()
            
            if len(switches_df) > 0:
                switch_id = switches_df.index[0]
                original_state = switches_df.loc[switch_id, 'open']
                
                # Create action with switches field
                action = env.action_space({
                    "switches": {switch_id: not original_state}
                })
                
                assert action is not None
                assert len(action._modifications) > 0


class TestNonReconnectableLineDetection:
    """Tests for detecting non-reconnectable lines from switch topology."""

    @pytest.fixture
    def real_network_manager(self):
        """Create a NetworkManager from the real test grid xiidm file."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager
        from expert_op4grid_recommender import config

        grid_path = config.ENV_FOLDER / "bare_env_small_grid_test" / "grid.xiidm"
        nm = NetworkManager(network_path=grid_path)
        return nm

    def test_detect_non_reconnectable_lines_returns_list(self, real_network_manager):
        """Test that detect_non_reconnectable_lines returns a list."""
        result = real_network_manager.detect_non_reconnectable_lines()
        assert isinstance(result, list)

    def test_detect_non_reconnectable_lines_finds_expected_lines(self, real_network_manager):
        """Test that known non-reconnectable lines are detected.

        In the test grid, CRENEL71VIELM, GEN.PL73VIELM, and PYMONL61VOUGL
        have breakers open and all disconnectors open at both extremities.
        """
        result = real_network_manager.detect_non_reconnectable_lines()

        # These lines have all breakers and disconnectors open at both sides
        expected_non_reco_lines = ["CRENEL71VIELM", "GEN.PL73VIELM", "PYMONL61VOUGL"]
        for line in expected_non_reco_lines:
            assert line in result, f"{line} should be detected as non-reconnectable"

    def test_detect_non_reconnectable_lines_finds_expected_transformers(self, real_network_manager):
        """Test that non-reconnectable transformers are also detected.

        In the test grid, CPVANY632 and PYMONY632 have breakers open and all
        disconnectors open at both extremities.
        """
        result = real_network_manager.detect_non_reconnectable_lines()

        expected_non_reco_trafos = ["CPVANY632", "PYMONY632"]
        for trafo in expected_non_reco_trafos:
            assert trafo in result, f"{trafo} should be detected as non-reconnectable"

    def test_reconnectable_lines_not_included(self, real_network_manager):
        """Test that lines with at least one closed disconnector are NOT flagged.

        BOISSL61GEN.P has a closed disconnector at side 1 (SA.1), so it
        should NOT be detected as non-reconnectable.
        """
        result = real_network_manager.detect_non_reconnectable_lines()

        # BOISSL61GEN.P: breaker open at side 1 but SA.1 closed -> reconnectable
        assert "BOISSL61GEN.P" not in result

    def test_connected_lines_not_included(self, real_network_manager):
        """Test that fully connected lines are never flagged."""
        result = real_network_manager.detect_non_reconnectable_lines()

        # AISERL31MAGNY is a connected line - should never appear
        assert "AISERL31MAGNY" not in result

    def test_one_side_isolated_is_non_reconnectable_synthetic(self):
        """Regression test for OR-logic fix: a line with only ONE fully-isolated
        extremity (breaker open + all disconnectors open on that side) must be
        detected as non-reconnectable, even if the other side still has a closed
        disconnector.  The previous AND logic incorrectly excluded such lines.
        """
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _is_non_reconnectable
        import pandas as pd
        import types

        line_id = "TEST_LINE_OR"
        vl1, vl2 = "VL1", "VL2"

        # Side 1: line at node 10, breaker 10->20 (OPEN), disconnector 20->30 (OPEN)
        nodes_vl1 = pd.DataFrame({"connectable_id": [line_id, None, None]}, index=[10, 20, 30])
        switches_vl1 = pd.DataFrame({
            "node1": [10, 20], "node2": [20, 30],
            "kind": ["BREAKER", "DISCONNECTOR"], "open": [True, True],
        })

        # Side 2: line at node 100, breaker 100->200 (OPEN), disconnector 200->300 (CLOSED)
        nodes_vl2 = pd.DataFrame({"connectable_id": [line_id, None, None]}, index=[100, 200, 300])
        switches_vl2 = pd.DataFrame({
            "node1": [100, 200], "node2": [200, 300],
            "kind": ["BREAKER", "DISCONNECTOR"], "open": [True, False],
        })

        class FakeTopo:
            def __init__(self, nodes, switches):
                self.nodes = nodes
                self.switches = switches

        def get_node_breaker_topology(vl_id):
            return FakeTopo(nodes_vl1, switches_vl1) if vl_id == vl1 else FakeTopo(nodes_vl2, switches_vl2)

        network = types.SimpleNamespace(get_node_breaker_topology=get_node_breaker_topology)

        result = _is_non_reconnectable(network, line_id, vl1, vl2)
        assert result is True, (
            "Line with one fully-isolated side (breaker + all disconnectors open) "
            "must be considered non-reconnectable"
        )

    def test_neither_side_isolated_is_reconnectable_synthetic(self):
        """When neither extremity is fully isolated the line must NOT be flagged."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _is_non_reconnectable
        import pandas as pd
        import types

        line_id = "TEST_LINE_RECO"
        vl1, vl2 = "VL_A", "VL_B"

        def make_side_nodes_switches(node_line, node_inter, node_bus, disc_open):
            nodes = pd.DataFrame(
                {"connectable_id": [line_id, None, None]},
                index=[node_line, node_inter, node_bus],
            )
            switches = pd.DataFrame({
                "node1": [node_line, node_inter],
                "node2": [node_inter, node_bus],
                "kind": ["BREAKER", "DISCONNECTOR"],
                "open": [True, disc_open],   # breaker open, disconnector as requested
            })
            return nodes, switches

        nodes_vl1, sw_vl1 = make_side_nodes_switches(10, 20, 30, disc_open=False)
        nodes_vl2, sw_vl2 = make_side_nodes_switches(100, 200, 300, disc_open=False)

        class FakeTopo:
            def __init__(self, nodes, switches):
                self.nodes = nodes
                self.switches = switches

        def get_node_breaker_topology(vl_id):
            return FakeTopo(nodes_vl1, sw_vl1) if vl_id == vl1 else FakeTopo(nodes_vl2, sw_vl2)

        network = types.SimpleNamespace(get_node_breaker_topology=get_node_breaker_topology)

        result = _is_non_reconnectable(network, line_id, vl1, vl2)
        assert result is False, (
            "Line where both sides still have a closed disconnector should NOT "
            "be considered non-reconnectable"
        )

    def test_check_line_side_switches_returns_none_when_no_breaker(self, real_network_manager):
        """Test that _check_line_side_switches returns None if no breaker exists.

        CURTIL61ZCUR5 has no breaker at CURTIP6 or ZCUR5P6 in this grid.
        """
        nm = real_network_manager

        # CURTIL61ZCUR5 is disconnected but has no breaker on either side
        result = nm._check_line_side_switches("CURTIL61ZCUR5", "CURTIP6")
        assert result is None

    def test_check_line_side_switches_returns_tuple(self, real_network_manager):
        """Test that _check_line_side_switches returns (breaker_open, all_disc_open)."""
        nm = real_network_manager

        # PYMONL61VOUGL at PYMONP6: breaker open, all disconnectors open
        result = nm._check_line_side_switches("PYMONL61VOUGL", "PYMONP6")
        assert result is not None
        breaker_open, all_disc_open = result
        assert breaker_open is True
        assert all_disc_open is True

    def test_check_line_side_with_closed_disconnector(self, real_network_manager):
        """Test side with open breaker but at least one closed disconnector."""
        nm = real_network_manager

        # BOISSL61GEN.P at BOISSP6: breaker open, but SA.1 is closed
        result = nm._check_line_side_switches("BOISSL61GEN.P", "BOISSP6")
        assert result is not None
        breaker_open, all_disc_open = result
        assert breaker_open is True
        assert all_disc_open is False

    def test_ieee9_network_has_no_non_reconnectable(self):
        """Test that a simple IEEE9 network has no non-reconnectable lines."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager

        network = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=network)
        result = nm.detect_non_reconnectable_lines()

        # IEEE9 is a fully connected network with no disconnected lines
        assert result == []

    def test_standalone_function_matches_network_manager(self, real_network_manager):
        """Test that the standalone function gives the same result as NetworkManager."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import (
            detect_non_reconnectable_lines as detect_standalone
        )

        nm_result = real_network_manager.detect_non_reconnectable_lines()
        standalone_result = detect_standalone(real_network_manager.network)

        assert nm_result == standalone_result

    def test_standalone_function_with_raw_network(self):
        """Test the standalone function directly with a raw pypowsybl network.

        This validates it works without a NetworkManager, as needed for
        the grid2op backend which accesses the network via env.backend._grid.network.
        """
        from expert_op4grid_recommender.utils.helpers_pypowsybl import (
            detect_non_reconnectable_lines as detect_standalone
        )
        from expert_op4grid_recommender import config

        grid_path = config.ENV_FOLDER / "bare_env_small_grid_test" / "grid.xiidm"
        network = pypowsybl.network.load(str(grid_path))

        result = detect_standalone(network)

        assert isinstance(result, list)
        assert "CRENEL71VIELM" in result
        assert "PYMONL61VOUGL" in result
        assert "BOISSL61GEN.P" not in result


class TestBuildConnectableToNodeMap:
    """Tests for _build_connectable_to_node_map helper."""

    def test_with_real_topology(self):
        """Test building connectable map from a real voltage level topology."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _build_connectable_to_node_map
        from expert_op4grid_recommender import config

        grid_path = config.ENV_FOLDER / "bare_env_small_grid_test" / "grid.xiidm"
        network = pypowsybl.network.load(str(grid_path))

        # Pick a voltage level that has a known line
        lines_df = network.get_lines()
        if lines_df.empty:
            pytest.skip("No lines in test grid")
        first_line = lines_df.index[0]
        vl = lines_df.loc[first_line, 'voltage_level1_id']

        topo = network.get_node_breaker_topology(vl)
        conn_map = _build_connectable_to_node_map(topo.nodes, vl)

        assert isinstance(conn_map, dict)
        assert len(conn_map) > 0
        # The known line should appear in its VL's connectable map
        assert (first_line, vl) in conn_map

    def test_with_empty_dataframe(self):
        """Test that an empty nodes DataFrame returns an empty dict."""
        import pandas as pd
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _build_connectable_to_node_map

        empty_df = pd.DataFrame(columns=['connectable_id'])
        result = _build_connectable_to_node_map(empty_df, "VL1")
        assert result == {}

    def test_with_all_nan_connectable_ids(self):
        """Test that a DataFrame with only NaN connectable_ids returns empty dict."""
        import pandas as pd
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _build_connectable_to_node_map

        df = pd.DataFrame({'connectable_id': [float('nan'), float('nan')]}, index=[0, 1])
        result = _build_connectable_to_node_map(df, "VL1")
        assert result == {}
    
    def test_first_occurrence_wins(self):
        """Test that when a connectable_id appears at multiple nodes, the first is kept."""
        import pandas as pd
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _build_connectable_to_node_map

        df = pd.DataFrame(
            {'connectable_id': ['LINE_A', 'LINE_B', 'LINE_A']},
            index=[10, 20, 30]
        )
        result = _build_connectable_to_node_map(df, "VL1")
        assert result[('LINE_A', 'VL1')] == (10, 'VL1')
        assert result[('LINE_B', 'VL1')] == (20, 'VL1')
        assert len(result) == 2


class TestBuildSwitchAdjacency:
    """Tests for _build_switch_adjacency helper."""

    def test_with_real_topology(self):
        """Test building switch adjacency from a real voltage level topology."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _build_switch_adjacency
        from expert_op4grid_recommender import config

        grid_path = config.ENV_FOLDER / "bare_env_small_grid_test" / "grid.xiidm"
        network = pypowsybl.network.load(str(grid_path))

        lines_df = network.get_lines()
        if lines_df.empty:
            pytest.skip("No lines in test grid")
        vl = lines_df.iloc[0]['voltage_level1_id']

        topo = network.get_node_breaker_topology(vl)
        sw_adj = _build_switch_adjacency(topo.switches, vl)

        assert isinstance(sw_adj, dict)
        assert len(sw_adj) > 0
        # Each entry should be a list of (other_node, kind, is_open) tuples
        for node, neighbors in sw_adj.items():
            assert isinstance(node, tuple)
            assert isinstance(neighbors, list)
            for other, kind, is_open in neighbors:
                assert isinstance(other, tuple)
                assert kind in ('BREAKER', 'DISCONNECTOR', 'LOAD_BREAK_SWITCH')
                assert isinstance(is_open, (bool, np.bool_))

    def test_bidirectional_entries(self):
        """Test that every switch creates entries in both directions."""
        import pandas as pd
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _build_switch_adjacency

        switches = pd.DataFrame({
            'node1': [1, 3],
            'node2': [2, 4],
            'kind': ['BREAKER', 'DISCONNECTOR'],
            'open': [True, False],
        })
        adj = _build_switch_adjacency(switches, "VL1")

        # node 1 -> node 2
        assert any(other == (2, "VL1") for other, _, _ in adj[(1, "VL1")])
        # node 2 -> node 1
        assert any(other == (1, "VL1") for other, _, _ in adj[(2, "VL1")])
        # node 3 -> node 4
        assert any(other == (4, "VL1") for other, _, _ in adj[(3, "VL1")])
        # node 4 -> node 3
        assert any(other == (3, "VL1") for other, _, _ in adj[(4, "VL1")])

    def test_with_empty_dataframe(self):
        """Test that an empty switches DataFrame returns an empty dict."""
        import pandas as pd
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _build_switch_adjacency

        empty_df = pd.DataFrame(columns=['node1', 'node2', 'kind', 'open'])
        result = _build_switch_adjacency(empty_df, "VL1")
        assert len(result) == 0

    def test_preserves_kind_and_open_state(self):
        """Test that kind and open state are correctly stored."""
        import pandas as pd
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _build_switch_adjacency

        switches = pd.DataFrame({
            'node1': [10],
            'node2': [20],
            'kind': ['BREAKER'],
            'open': [True],
        })
        adj = _build_switch_adjacency(switches, "VL1")

        neighbors_of_10 = adj[(10, "VL1")]
        assert len(neighbors_of_10) == 1
        other, kind, is_open = neighbors_of_10[0]
        assert other == (20, "VL1")
        assert kind == 'BREAKER'
        assert is_open == True


class TestCheckSwitchesFromLookups:
    """Tests for _check_switches_from_lookups helper."""

    @pytest.fixture
    def real_network(self):
        """Load the real test grid network."""
        from expert_op4grid_recommender import config

        grid_path = config.ENV_FOLDER / "bare_env_small_grid_test" / "grid.xiidm"
        return pypowsybl.network.load(str(grid_path))

    def test_matches_original_for_all_disconnected_lines(self, real_network):
        """Verify _check_switches_from_lookups matches _check_line_side_switches
        for every disconnected line in the real grid."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import (
            _check_line_side_switches,
            _check_switches_from_lookups,
            _build_connectable_to_node_map,
            _build_switch_adjacency,
        )

        network = real_network
        lines_df = network.get_lines()
        disconnected = lines_df[~lines_df['connected1'] | ~lines_df['connected2']]

        trafos_df = network.get_2_windings_transformers()
        disconnected_trafos = trafos_df[~trafos_df['connected1'] | ~trafos_df['connected2']]

        # Build caches for all relevant VLs
        vl_ids = set()
        for df in (disconnected, disconnected_trafos):
            if not df.empty:
                vl_ids.update(df['voltage_level1_id'].values)
                vl_ids.update(df['voltage_level2_id'].values)

        topo_cache = {}
        for vl_id in vl_ids:
            topo = network.get_node_breaker_topology(vl_id)
            topo_cache[vl_id] = (
                _build_connectable_to_node_map(topo.nodes, vl_id),
                _build_switch_adjacency(topo.switches, vl_id),
            )

        # Compare for every disconnected element and every side
        mismatches = []
        for df in (disconnected, disconnected_trafos):
            for eid, row in df.iterrows():
                for vl_col in ('voltage_level1_id', 'voltage_level2_id'):
                    vl = row[vl_col]
                    original = _check_line_side_switches(network, eid, vl)
                    conn_map, sw_adj = topo_cache[vl]
                    optimized = _check_switches_from_lookups(conn_map, sw_adj, eid, vl)
                    if original != optimized:
                        mismatches.append((eid, vl, original, optimized))

        assert mismatches == [], f"Mismatches between original and optimized: {mismatches}"

    def test_returns_none_when_line_not_in_map(self):
        """Test that a line not in the connectable map returns None."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _check_switches_from_lookups

        result = _check_switches_from_lookups({}, {}, "NONEXISTENT_LINE", "VL1")
        assert result is None

    def test_returns_none_when_no_breakers(self):
        """Test that a line node with only disconnectors returns None."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _check_switches_from_lookups

        conn_map = {("LINE_A", "VL1"): 1}
        # Node 1 only has DISCONNECTOR neighbors, no BREAKER
        sw_adj = {1: [(2, 'DISCONNECTOR', True)]}

        result = _check_switches_from_lookups(conn_map, sw_adj, "LINE_A", "VL1")
        assert result is None

    def test_open_breaker_and_open_disconnectors(self):
        """Test a line with open breaker and all disconnectors open."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _check_switches_from_lookups

        # line node=1, breaker to intermediate node=2, disconnectors from node=2
        conn_map = {("LINE_A", "VL1"): 1}
        sw_adj = {
            1: [(2, 'BREAKER', True)],  # open breaker
            2: [(1, 'BREAKER', True), (3, 'DISCONNECTOR', True), (4, 'DISCONNECTOR', True)],
        }

        result = _check_switches_from_lookups(conn_map, sw_adj, "LINE_A", "VL1")
        assert result is not None
        breaker_open, all_disc_open = result
        assert breaker_open is True
        assert all_disc_open is True

    def test_open_breaker_but_closed_disconnector(self):
        """Test a line with open breaker but a closed disconnector."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _check_switches_from_lookups

        conn_map = {("LINE_A", "VL1"): 1}
        sw_adj = {
            1: [(2, 'BREAKER', True)],  # open breaker
            2: [(1, 'BREAKER', True), (3, 'DISCONNECTOR', False)],  # closed disconnector
        }

        result = _check_switches_from_lookups(conn_map, sw_adj, "LINE_A", "VL1")
        assert result is not None
        breaker_open, all_disc_open = result
        assert breaker_open is True
        assert all_disc_open is False

    def test_closed_breaker(self):
        """Test a line with closed breaker."""
        from expert_op4grid_recommender.utils.helpers_pypowsybl import _check_switches_from_lookups

        conn_map = {("LINE_A", "VL1"): 1}
        sw_adj = {
            1: [(2, 'BREAKER', False)],  # closed breaker
            2: [(1, 'BREAKER', False), (3, 'DISCONNECTOR', False)],
        }

        result = _check_switches_from_lookups(conn_map, sw_adj, "LINE_A", "VL1")
        assert result is not None
        breaker_open, all_disc_open = result
        assert breaker_open is False
        assert all_disc_open is False



class TestPypowsyblRobustnessAndFixes:
    """Tests for recent fixes and robustness improvements."""

    def test_switch_prefix_matching(self):
        """Test that switches can be matched using prefixes (e.g. SUB_ID_SW_ID)."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace
        
        net = pypowsybl.network.create_four_substations_node_breaker_network()
        all_sw = net.get_switches()
        sub_id_net = all_sw['voltage_level_id'].iloc[0]
        sw_id = all_sw[all_sw['voltage_level_id'] == sub_id_net].index[0]
        
        # Ensure it's OPEN first
        net.update_switches(id=[sw_id], open=[True])
        
        nm = NetworkManager(network=net)
        aspace = ActionSpace(nm)
        
        # Try to close the switch using a prefixed ID
        prefixed_id = f"{sub_id_net}_{sw_id}"
        action = aspace({"switches": {prefixed_id: False}}) # False = closed
        
        # Apply action via NetworkManager directly to avoid PypowsyblObservation reset to base
        action.apply(nm)
        
        # Check if closed
        assert net.get_switches().loc[sw_id, "open"] == False

    def test_tro_coupler_management(self):
        """Test that TRO switches are correctly handled as couplers during merge."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace
        
        net = pypowsybl.network.create_four_substations_node_breaker_network()
        sub_name = net.get_voltage_levels().index[0]
        topo = net.get_node_breaker_topology(sub_name)
        nodes = topo.nodes.index.tolist()
        
        # Create a "TRO" switch manually
        tro_id = "SUB_TRO_1"
        net.create_switches(id=[tro_id], voltage_level_id=sub_name, node1=nodes[0], node2=nodes[1], kind='BREAKER', open=True)
        
        nm = NetworkManager(network=net)
        action_space = ActionSpace(nm)
        
        sub_id = nm.get_sub_idx(sub_name)
        n_elements = nm._cached_sub_info[sub_id]
        topo_vector = [1] * n_elements # All on bus 1 = MERGE
        
        action = action_space({"set_bus": {"substations_id": [(sub_id, topo_vector)]}})
        action.apply(nm)
        
        # TRO switch should be closed
        assert net.get_switches().loc[tro_id, "open"] == False

    def test_simulate_variant_id_preservation_on_error(self):
        """Test that simulate returns an observation with variant_id even on error."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace, PypowsyblObservation
        
        net = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=net)
        aspace = ActionSpace(nm)
        obs = PypowsyblObservation(nm, aspace)
        
        # Create an action that will cause divergence or failure if possible, 
        # or just mock a failure during apply if we want to be sure.
        # Here we just use a normal action but keep_variant=True
        first_line = nm.name_line[0]
        action = aspace({"set_line_status": [(first_line, -1)]})
        
        # Test baseline: it should have a variant_id
        obs_simu, _, _, _ = obs.simulate(action, keep_variant=True)
        assert obs_simu._variant_id is not None
        assert "simulate_kept" in obs_simu._variant_id


class TestPowerReductionAction:
    """Tests for PowerReductionAction and set_load_p/set_gen_p in ActionSpace."""

    @pytest.fixture
    def env_with_action_space(self):
        """Create environment with action space."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace
        network = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=network)
        action_space = ActionSpace(nm)
        return nm, action_space

    def test_power_reduction_action_class_creation(self):
        """Test PowerReductionAction stores loads_p and gens_p."""
        from expert_op4grid_recommender.pypowsybl_backend.action_space import PowerReductionAction

        action = PowerReductionAction(loads_p={"LOAD_A": 0.0}, gens_p={"GEN_1": 0.0})
        assert action.loads_p == {"LOAD_A": 0.0}
        assert action.gens_p == {"GEN_1": 0.0}
        assert len(action._modifications) == 1

    def test_power_reduction_action_defaults_empty(self):
        """PowerReductionAction with no args has empty dicts."""
        from expert_op4grid_recommender.pypowsybl_backend.action_space import PowerReductionAction

        action = PowerReductionAction()
        assert action.loads_p == {}
        assert action.gens_p == {}
        assert len(action._modifications) == 1

    def test_action_space_set_load_p(self, env_with_action_space):
        """ActionSpace creates PowerReductionAction from set_load_p dict."""
        nm, action_space = env_with_action_space
        loads = nm.network.get_loads()
        if len(loads) == 0:
            pytest.skip("No loads in test network")

        first_load = loads.index[0]
        action = action_space({"set_load_p": {first_load: 0.0}})

        assert action is not None
        assert action.loads_p == {first_load: 0.0}

    def test_action_space_set_gen_p(self, env_with_action_space):
        """ActionSpace creates PowerReductionAction from set_gen_p dict."""
        nm, action_space = env_with_action_space
        gens = nm.network.get_generators()
        if len(gens) == 0:
            pytest.skip("No generators in test network")

        first_gen = gens.index[0]
        action = action_space({"set_gen_p": {first_gen: 0.0}})

        assert action is not None
        assert action.gens_p == {first_gen: 0.0}

    def test_action_space_combined_load_and_gen_reduction(self, env_with_action_space):
        """ActionSpace handles both set_load_p and set_gen_p in one call."""
        nm, action_space = env_with_action_space
        loads = nm.network.get_loads()
        gens = nm.network.get_generators()
        if len(loads) == 0 or len(gens) == 0:
            pytest.skip("Need both loads and generators")

        first_load = loads.index[0]
        first_gen = gens.index[0]
        action = action_space({
            "set_load_p": {first_load: 0.0},
            "set_gen_p": {first_gen: 0.0},
        })

        assert action.loads_p == {first_load: 0.0}
        assert action.gens_p == {first_gen: 0.0}

    def test_power_reduction_combined_with_topology(self, env_with_action_space):
        """Power reduction can be combined with topology actions via +."""
        nm, action_space = env_with_action_space
        loads = nm.network.get_loads()
        if len(loads) == 0 or nm.n_line == 0:
            pytest.skip("Need loads and lines")

        first_load = loads.index[0]
        first_line = nm.name_line[0]

        pr_action = action_space({"set_load_p": {first_load: 0.0}})
        topo_action = action_space({"set_line_status": [(first_line, -1)]})

        combined = pr_action + topo_action
        assert combined.loads_p == {first_load: 0.0}
        assert len(combined._modifications) == 2

    def test_power_reduction_apply_changes_load_target_p(self, env_with_action_space):
        """Applying a power reduction action changes the load's active power in the network."""
        nm, action_space = env_with_action_space
        loads = nm.network.get_loads()
        if len(loads) == 0:
            pytest.skip("No loads in test network")

        first_load = loads.index[0]
        # pypowsybl uses 'p0' column for load active power setpoint
        p_col = "p0" if "p0" in loads.columns else "target_p"

        # Create variant to avoid modifying base
        nm.create_variant("test_pr")
        nm.set_working_variant("test_pr")
        try:
            action = action_space({"set_load_p": {first_load: 0.0}})
            action.apply(nm)

            loads_after = nm.network.get_loads()
            new_p = float(loads_after.loc[first_load, p_col])
            assert new_p == 0.0, f"Expected active power=0.0 after reduction, got {new_p}"
        finally:
            nm.reset_to_base()
            nm.remove_variant("test_pr")

    def test_power_reduction_apply_changes_gen_target_p(self, env_with_action_space):
        """Applying a power reduction action changes the generator's target_p in the network."""
        nm, action_space = env_with_action_space
        gens = nm.network.get_generators()
        if len(gens) == 0:
            pytest.skip("No generators in test network")

        first_gen = gens.index[0]

        nm.create_variant("test_pr_gen")
        nm.set_working_variant("test_pr_gen")
        try:
            action = action_space({"set_gen_p": {first_gen: 0.0}})
            action.apply(nm)

            gens_after = nm.network.get_generators()
            new_p = float(gens_after.loc[first_gen, "target_p"])
            assert new_p == 0.0, f"Expected target_p=0.0 after reduction, got {new_p}"
        finally:
            nm.reset_to_base()
            nm.remove_variant("test_pr_gen")

    def test_power_reduction_load_stays_connected(self, env_with_action_space):
        """After power reduction, the load remains electrically connected."""
        nm, action_space = env_with_action_space
        loads = nm.network.get_loads()
        if len(loads) == 0:
            pytest.skip("No loads in test network")

        # Check whether 'connected' is a column in this pypowsybl version
        if "connected" not in loads.columns:
            pytest.skip("pypowsybl version does not expose 'connected' column for loads")

        first_load = loads.index[0]

        nm.create_variant("test_pr_connected")
        nm.set_working_variant("test_pr_connected")
        try:
            action = action_space({"set_load_p": {first_load: 0.0}})
            action.apply(nm)

            loads_after = nm.network.get_loads()
            is_connected = bool(loads_after.loc[first_load, "connected"])
            assert is_connected, "Load should remain connected after power reduction"
        finally:
            nm.reset_to_base()
            nm.remove_variant("test_pr_connected")

    def test_power_reduction_gen_stays_connected(self, env_with_action_space):
        """After power reduction, the generator remains electrically connected."""
        nm, action_space = env_with_action_space
        gens = nm.network.get_generators()
        if len(gens) == 0:
            pytest.skip("No generators in test network")

        first_gen = gens.index[0]

        nm.create_variant("test_pr_gen_connected")
        nm.set_working_variant("test_pr_gen_connected")
        try:
            action = action_space({"set_gen_p": {first_gen: 0.0}})
            action.apply(nm)

            gens_after = nm.network.get_generators()
            is_connected = bool(gens_after.loc[first_gen, "connected"])
            assert is_connected, "Generator should remain connected after power reduction"
        finally:
            nm.reset_to_base()
            nm.remove_variant("test_pr_gen_connected")

    def test_power_reduction_partial_value(self, env_with_action_space):
        """Power reduction to a non-zero target value."""
        nm, action_space = env_with_action_space
        loads = nm.network.get_loads()
        if len(loads) == 0:
            pytest.skip("No loads in test network")

        first_load = loads.index[0]
        p_col = "p0" if "p0" in loads.columns else "target_p"

        nm.create_variant("test_pr_partial")
        nm.set_working_variant("test_pr_partial")
        try:
            action = action_space({"set_load_p": {first_load: 50.0}})
            action.apply(nm)

            loads_after = nm.network.get_loads()
            new_p = float(loads_after.loc[first_load, p_col])
            assert new_p == 50.0
        finally:
            nm.reset_to_base()
            nm.remove_variant("test_pr_partial")

    def test_pypowsybl_action_has_power_reduction_attrs(self):
        """PypowsyblAction base class has loads_p and gens_p attributes."""
        from expert_op4grid_recommender.pypowsybl_backend.observation import PypowsyblAction

        action = PypowsyblAction()
        assert hasattr(action, "loads_p")
        assert hasattr(action, "gens_p")
        assert action.loads_p == {}
        assert action.gens_p == {}

    def test_pypowsybl_action_add_merges_power_reduction(self):
        """PypowsyblAction.__add__ merges loads_p and gens_p."""
        from expert_op4grid_recommender.pypowsybl_backend.observation import PypowsyblAction

        a = PypowsyblAction()
        a.loads_p = {"LOAD_A": 0.0}
        b = PypowsyblAction()
        b.gens_p = {"GEN_1": 0.0}
        b.loads_p = {"LOAD_B": 10.0}

        combined = a + b
        assert combined.loads_p == {"LOAD_A": 0.0, "LOAD_B": 10.0}
        assert combined.gens_p == {"GEN_1": 0.0}

    def test_do_nothing_action_has_empty_power_reduction(self, env_with_action_space):
        """A do-nothing action has empty power reduction dicts."""
        _, action_space = env_with_action_space
        action = action_space.get_do_nothing_action()
        assert action.loads_p == {}
        assert action.gens_p == {}


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])
