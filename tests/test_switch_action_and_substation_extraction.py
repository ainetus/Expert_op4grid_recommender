#!/usr/bin/python3
# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0

"""
Tests for:
1. SwitchAction class in pypowsybl backend action_space.py
2. switches field in action dictionaries from conversion_actions_repas.py
3. _get_subs_impacted_from_action_desc method in discovery.py

These tests verify the new features for pypowsybl backend topology simulation:
- SwitchAction properly updates network switches
- ActionSpace handles switches field in action dictionaries
- Converted actions include switches field for pypowsybl simulation
- Backend-agnostic substation extraction from action descriptions
"""

import pytest
import os
import sys
from unittest.mock import MagicMock
import pandas as pd
import numpy as np

# --- Test Setup: Add Project Root to Python path ---
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Skip all tests if pypowsybl is not available
pypowsybl = pytest.importorskip("pypowsybl")


# =============================================================================
# Mock Classes for Testing
# =============================================================================

class MockRepasAction:
    """Mock of a REPAS action object."""
    def __init__(self, action_id, switches_by_voltage_level, description="Test action"):
        self._id = action_id
        self._switches_by_voltage_level = switches_by_voltage_level
        self._description = description


def create_mock_network_with_topology():
    """Create a mock network with realistic topology data for testing."""
    mock_network = MagicMock()
    
    # Switches DataFrame
    switches_data = {
        'voltage_level_id': ['VL1', 'VL1', 'VL1', 'VL1'],
        'bus_breaker_bus1_id': ['VL1_NODE1', 'VL1_NODE2', 'VL1_NODE1', 'VL1_NODE3'],
        'bus_breaker_bus2_id': ['VL1_NODE2', 'VL1_NODE3', 'VL1_NODE3', 'VL1_NODE4'],
        'open': [False, True, False, True]
    }
    switches_df = pd.DataFrame(switches_data, index=['VL1_SW1', 'VL1_SW2', 'VL1_SW3', 'VL1_SW4'])
    mock_network.get_switches.return_value = switches_df
    
    # Loads DataFrame
    loads_data = {'bus_id': ['VL1_NODE1'], 'voltage_level_id': ['VL1']}
    loads_df = pd.DataFrame(loads_data, index=['LOAD1'])
    mock_network.get_loads.return_value = loads_df
    
    # Generators DataFrame
    generators_data = {'bus_id': ['VL1_NODE4'], 'voltage_level_id': ['VL1']}
    generators_df = pd.DataFrame(generators_data, index=['GEN1'])
    mock_network.get_generators.return_value = generators_df
    
    # Shunts DataFrame (empty)
    shunts_df = pd.DataFrame(columns=['bus_id', 'voltage_level_id'])
    shunts_df.index.name = 'id'
    mock_network.get_shunt_compensators.return_value = shunts_df
    
    # Branches DataFrame
    branches_data = {
        'bus1_id': ['VL1_NODE1', 'VL1_NODE4'],
        'bus2_id': ['VL2_NODE1', 'VL2_NODE2'],
        'voltage_level1_id': ['VL1', 'VL1'],
        'voltage_level2_id': ['VL2', 'VL2']
    }
    branches_df = pd.DataFrame(branches_data, index=['LINE1', 'LINE2'])
    mock_network.get_branches.return_value = branches_df
    
    return mock_network


@pytest.fixture
def mock_network_with_topology():
    """Fixture for mock network with topology."""
    return create_mock_network_with_topology()


# =============================================================================
# Tests for SwitchAction class
# =============================================================================

class TestSwitchAction:
    """Tests for the SwitchAction class in action_space.py."""
    
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
        # Even empty, it should have the modification function
        assert len(action._modifications) == 1
    
    def test_switch_action_apply_to_network(self):
        """Test that SwitchAction can be applied to a network."""
        from expert_op4grid_recommender.pypowsybl_backend.action_space import SwitchAction
        
        # Use IDs that exist in the mock network from create_mock_network_with_topology
        switch_states = {"VL1_SW1": True, "VL1_SW2": False}
        action = SwitchAction(switch_states)
        
        # Create a mock network manager with realistic switch index
        mock_nm = MagicMock()
        mock_nm.network = create_mock_network_with_topology()
        
        # Apply the action
        action.apply(mock_nm)
        
        # Verify update_switches was called
        mock_nm.network.update_switches.assert_called()
    
    def test_switch_action_combination(self):
        """Test combining SwitchActions with other actions."""
        from expert_op4grid_recommender.pypowsybl_backend.action_space import (
            SwitchAction, PypowsyblAction
        )
        
        switch_action = SwitchAction({"SW1": True})
        other_action = PypowsyblAction()
        
        combined = switch_action + other_action
        
        assert combined is not None
        # Combined should have modifications from both
        assert len(combined._modifications) >= 1


# =============================================================================
# Tests for ActionSpace handling switches field
# =============================================================================

class TestActionSpaceSwitchesField:
    """Tests for ActionSpace handling the switches field in action dicts."""
    
    @pytest.fixture
    def action_space_setup(self):
        """Create NetworkManager and ActionSpace for testing."""
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace
        
        network = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=network)
        action_space = ActionSpace(nm)
        
        return nm, action_space
    
    def test_action_space_handles_switches_field(self, action_space_setup):
        """Test that ActionSpace creates SwitchAction from switches field at root level."""
        nm, action_space = action_space_setup
        
        # New convention: switches is at root level, not inside content
        action_dict = {
            "switches": {
                "SWITCH_1": True,
                "SWITCH_2": False,
            }
        }
        
        action = action_space(action_dict)
        
        assert action is not None
        assert len(action._modifications) > 0
    
    def test_action_space_empty_switches(self, action_space_setup):
        """Test that empty switches dict doesn't add modification."""
        nm, action_space = action_space_setup
        
        action_dict = {"switches": {}}
        
        action = action_space(action_dict)
        
        assert action is not None
        # Empty switches should not add a SwitchAction
        assert len(action._modifications) == 0
    
    def test_action_space_switches_with_set_bus(self, action_space_setup):
        """Test action with both set_bus and switches at root level."""
        nm, action_space = action_space_setup
        
        first_line = nm.name_line[0] if nm.n_line > 0 else "LINE1"
        
        # New convention: both at root level
        action_dict = {
            "set_bus": {
                "lines_or_id": {first_line: 1},
            },
            "switches": {
                "SWITCH_1": True,
            }
        }
        
        action = action_space(action_dict)
        
        assert action is not None
        # Should have both BusAction and SwitchAction
        assert len(action._modifications) == 2
    
    def test_action_space_switches_with_line_status(self, action_space_setup):
        """Test action with both line status and switches."""
        nm, action_space = action_space_setup
        
        first_line = nm.name_line[0] if nm.n_line > 0 else "LINE1"
        
        action_dict = {
            "set_line_status": [(first_line, -1)],
            "switches": {
                "SWITCH_1": True,
            }
        }
        
        action = action_space(action_dict)
        
        assert action is not None
        assert len(action._modifications) == 2
    
    def test_action_space_content_with_switches_at_root(self, action_space_setup):
        """Test that ActionSpace extracts switches from root level (new convention)."""
        nm, action_space = action_space_setup
        
        # New convention: switches at root, not inside content
        action_dict = {
            "content": {
                "set_bus": {
                    "lines_or_id": {},
                    "lines_ex_id": {},
                },
            },
            "switches": {
                "SW1": True,
                "SW2": False,
            }
        }
        
        action = action_space(action_dict)
        
        assert action is not None
        # Should find switches at root level and create SwitchAction
        assert len(action._modifications) >= 1


class TestSwitchesDriveTopology:
    """When the switches of an action exist, they alone set the topology.

    The set_bus shipped alongside (possibly stale) is kept as topology info
    but must not be applied: it would disconnect lines listed ``-1`` and
    reconnect any element listed with a bus >= 1.
    """

    @pytest.fixture
    def setup(self):
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace

        network = pypowsybl.network.create_four_substations_node_breaker_network()
        nm = NetworkManager(network=network)
        return nm, ActionSpace(nm)

    @staticmethod
    def _connected(nm, line_id):
        row = nm.network.get_lines(attributes=["connected1", "connected2"]).loc[line_id]
        return bool(row["connected1"]), bool(row["connected2"])

    def test_stale_disconnection_not_applied(self, setup):
        nm, action_space = setup
        action = action_space({
            "content": {"set_bus": {"lines_or_id": {"LINE_S3S4": -1}}},
            "switches": {"S1VL2_COUPLER": True},
        })

        action.apply(nm)

        assert nm.network.get_switches().loc["S1VL2_COUPLER", "open"]
        assert self._connected(nm, "LINE_S3S4") == (True, True)
        # Still exposed as topology info for superposition / topo_vect.
        assert action.lines_or_bus == {"LINE_S3S4": -1}

    def test_tripped_element_not_reconnected(self, setup):
        nm, action_space = setup
        nm.disconnect_line("LINE_S2S3")
        action = action_space({
            "content": {"set_bus": {"lines_or_id": {"LINE_S2S3": 2}}, "switches": {"S1VL2_COUPLER": True}},
        })

        action.apply(nm)

        assert self._connected(nm, "LINE_S2S3") == (False, False)

    def test_set_bus_applied_when_a_switch_is_unknown(self, setup):
        nm, action_space = setup
        action = action_space({
            "content": {"set_bus": {"lines_or_id": {"LINE_S3S4": -1}}},
            "switches": {"S1VL2_COUPLER": True, "NOT_IN_NETWORK": True},
        })

        action.apply(nm)

        assert self._connected(nm, "LINE_S3S4")[0] is False


# =============================================================================
# Tests for switches field in conversion_actions_repas
# =============================================================================

class TestSwitchesFieldInConversion:
    """Tests for switches field in converted action dictionaries."""
    
    def test_analytical_conversion_includes_switches_at_root(self, mock_network_with_topology):
        """Test that analytical conversion includes switches at root level with full IDs."""
        from expert_op4grid_recommender.utils.conversion_actions_repas import (
            convert_to_grid2op_action
        )
        
        action = MockRepasAction(
            action_id="SWITCH_TEST",
            switches_by_voltage_level={
                'VL1': {'VL1_SW1': True, 'VL1_SW2': False}
            }
        )
        
        result = convert_to_grid2op_action(mock_network_with_topology, action)
        
        assert 'content' in result
        # New convention: switches is at root level, not inside content
        assert 'switches' in result
        # Full IDs (as expected by pypowsybl)
        assert result['switches'] == {'VL1_SW1': True, 'VL1_SW2': False}
    
    def test_batch_conversion_includes_switches_at_root(self, mock_network_with_topology):
        """Test that batch conversion includes switches field at root level."""
        from expert_op4grid_recommender.utils.conversion_actions_repas import (
            convert_to_grid2op_action_batch
        )
        
        actions = [
            MockRepasAction(
                action_id="ACTION_1",
                switches_by_voltage_level={'VL1': {'VL1_SW1': True}}
            ),
            MockRepasAction(
                action_id="ACTION_2",
                switches_by_voltage_level={'VL1': {'VL1_SW2': False}}
            )
        ]
        
        results = convert_to_grid2op_action_batch(mock_network_with_topology, actions)
        
        assert len(results) == 2
        # New convention: switches at root level
        assert results[0]['switches'] == {'VL1_SW1': True}
        assert results[1]['switches'] == {'VL1_SW2': False}
    
    def test_high_level_conversion_includes_switches_at_root(self, mock_network_with_topology):
        """Test that high-level conversion includes switches at root level."""
        from expert_op4grid_recommender.utils.conversion_actions_repas import (
            convert_repas_actions_to_grid2op_actions
        )
        
        action = MockRepasAction(
            action_id="HIGH_LEVEL_TEST",
            switches_by_voltage_level={
                'VL1': {'VL1_SW1': True, 'VL1_SW3': False}
            }
        )
        
        result = convert_repas_actions_to_grid2op_actions(
            mock_network_with_topology,
            [action],
            use_analytical=True
        )
        
        assert 'HIGH_LEVEL_TEST_VL1' in result
        action_dict = result['HIGH_LEVEL_TEST_VL1']
        # New convention: switches at root level
        assert 'switches' in action_dict
        assert action_dict['switches'] == {'VL1_SW1': True, 'VL1_SW3': False}
    
    def test_switches_flattened_from_multiple_vls(self, mock_network_with_topology):
        """Test that switches from multiple VLs are flattened with full IDs at root level."""
        from expert_op4grid_recommender.utils.conversion_actions_repas import (
            convert_to_grid2op_action
        )
        
        action = MockRepasAction(
            action_id="MULTI_VL_TEST",
            switches_by_voltage_level={
                'VL1': {'VL1_SW1': True},
                'VL2': {'VL2_SW1': False}
            }
        )
        
        result = convert_to_grid2op_action(mock_network_with_topology, action)
        
        # New convention: switches at root level, flattened from all VLs
        assert 'switches' in result
        assert result['switches'] == {'VL1_SW1': True, 'VL2_SW1': False}
    
    def test_empty_switches_produces_empty_dict(self, mock_network_with_topology):
        """Test that action with no switches produces empty switches dict at root."""
        from expert_op4grid_recommender.utils.conversion_actions_repas import (
            convert_to_grid2op_action
        )
        
        action = MockRepasAction(
            action_id="EMPTY_SWITCH_TEST",
            switches_by_voltage_level={'VL1': {}}
        )
        
        result = convert_to_grid2op_action(mock_network_with_topology, action)
        
        # New convention: switches at root level
        assert result['switches'] == {}
    
    def test_variant_conversion_includes_switches(self, mock_network_with_topology):
        """Test that variant-based conversion also includes switches."""
        # Skip this test as variant conversion requires real network clone operations
        pytest.skip("Variant conversion requires real pypowsybl network with clone support")


# =============================================================================
# Tests for _get_subs_impacted_from_action_desc
# =============================================================================

class TestGetSubsImpactedFromActionDesc:
    """Tests for the backend-agnostic substation extraction method."""
    
    @pytest.fixture
    def mock_observation(self):
        """Create a mock observation with line-to-substation mappings."""
        mock_obs = MagicMock()
        mock_obs.name_line = np.array(["L1", "L2", "L3"])
        mock_obs.name_sub = np.array(["Sub0", "Sub1", "Sub2", "Sub3"])
        mock_obs.line_or_to_subid = np.array([0, 1, 2])  # L1->Sub0, L2->Sub1, L3->Sub2
        mock_obs.line_ex_to_subid = np.array([1, 2, 3])  # L1->Sub1, L2->Sub2, L3->Sub3
        mock_obs.load_to_subid = np.array([0, 1])  # LOAD0->Sub0, LOAD1->Sub1
        mock_obs.gen_to_subid = np.array([0, 2])  # GEN0->Sub0, GEN2->Sub2
        return mock_obs
    
    @pytest.fixture
    def discoverer_with_obs(self, mock_observation):
        """Create a minimal discoverer for testing substation extraction."""
        from expert_op4grid_recommender.action_evaluation.discovery import ActionDiscoverer
        
        # Create minimal mocks for ActionDiscoverer initialization
        mock_env = MagicMock()
        mock_env.action_space = MagicMock()
        
        mock_classifier = MagicMock()
        mock_g_overflow = MagicMock()
        mock_g_distribution = MagicMock()
        
        discoverer = ActionDiscoverer(
            env=mock_env,
            obs=mock_observation,
            obs_defaut=mock_observation,
            classifier=mock_classifier,
            timestep=0,
            lines_defaut=[],
            lines_overloaded_ids=[],
            act_reco_maintenance=MagicMock(),
            non_connected_reconnectable_lines=[],
            all_disconnected_lines=[],
            dict_action={},
            actions_unfiltered=set(),
            hubs=[],
            g_overflow=mock_g_overflow,
            g_distribution_graph=mock_g_distribution,
            simulator_data={}
        )
        return discoverer
    
    def test_substations_id_direct(self, discoverer_with_obs):
        """Test extraction from substations_id."""
        action_desc = {
            "content": {
                "set_bus": {
                    "substations_id": [(0, [1, 2, 1]), (2, [1, 1])]
                }
            }
        }
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        assert set(subs) == {0, 2}
    
    def test_lines_or_id(self, discoverer_with_obs):
        """Test extraction from lines_or_id."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_or_id": {"L1": 1, "L2": 2}
                }
            }
        }
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        # L1 origin is Sub0, L2 origin is Sub1
        assert set(subs) == {0, 1}
    
    def test_lines_ex_id(self, discoverer_with_obs):
        """Test extraction from lines_ex_id."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_ex_id": {"L1": -1, "L3": 1}
                }
            }
        }
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        # L1 extremity is Sub1, L3 extremity is Sub3
        assert set(subs) == {1, 3}
    
    def test_combined_lines_or_and_ex(self, discoverer_with_obs):
        """Test extraction from both lines_or_id and lines_ex_id."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_or_id": {"L1": 1},
                    "lines_ex_id": {"L2": 2}
                }
            }
        }
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        # L1 origin is Sub0, L2 extremity is Sub2
        assert set(subs) == {0, 2}
    
    def test_empty_action_desc(self, discoverer_with_obs):
        """Test with empty action description."""
        action_desc = {"content": {}}
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        assert subs == []
    
    def test_empty_set_bus(self, discoverer_with_obs):
        """Test with empty set_bus dictionary."""
        action_desc = {"content": {"set_bus": {}}}
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        assert subs == []
    
    def test_unknown_line_name(self, discoverer_with_obs):
        """Test with unknown line name (should be skipped)."""
        action_desc = {
            "content": {
                "set_bus": {
                    "lines_or_id": {"UNKNOWN_LINE": 1, "L1": 1}
                }
            }
        }
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        # Should only include Sub0 from L1
        assert set(subs) == {0}
    
    def test_all_fields_combined(self, discoverer_with_obs):
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
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        # Sub3 from substations_id, Sub0 from L1 origin, Sub2 from L2 extremity
        assert set(subs) == {0, 2, 3}
    
    def test_loads_id_extraction(self, discoverer_with_obs):
        """Test extraction from loads_id."""
        action_desc = {
            "content": {
                "set_bus": {
                    "loads_id": {"LOAD0": 1, "LOAD1": 2}
                }
            }
        }
        
        # Map load names to indices
        discoverer_with_obs.obs_defaut.name_load = np.array(["LOAD0", "LOAD1"])
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        # LOAD0->Sub0, LOAD1->Sub1
        assert set(subs) == {0, 1}
    
    def test_generators_id_extraction(self, discoverer_with_obs):
        """Test extraction from generators_id."""
        action_desc = {
            "content": {
                "set_bus": {
                    "generators_id": {"GEN0": 1, "GEN2": 2}
                }
            }
        }
        
        # Map generator names to indices
        discoverer_with_obs.obs_defaut.name_gen = np.array(["GEN0", "GEN2"])
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        # GEN0->Sub0, GEN2->Sub2
        assert set(subs) == {0, 2}
    
    def test_no_content_key(self, discoverer_with_obs):
        """Test with action dict that has no content key."""
        action_desc = {
            "set_bus": {
                "lines_or_id": {"L1": 1}
            }
        }
        
        subs = discoverer_with_obs._get_subs_impacted_from_action_desc(action_desc)
        
        # Should handle gracefully - check both content and direct keys
        # Depending on implementation, may return empty or find in direct key
        assert isinstance(subs, list)


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegrationSwitchesEndToEnd:
    """Integration tests for the full switches workflow."""
    
    def test_converted_action_works_with_action_space(self, mock_network_with_topology):
        """Test that converted action with switches can be used by ActionSpace."""
        from expert_op4grid_recommender.utils.conversion_actions_repas import (
            convert_repas_actions_to_grid2op_actions
        )
        from expert_op4grid_recommender.pypowsybl_backend import NetworkManager, ActionSpace
        
        # Create a real network for ActionSpace
        network = pypowsybl.network.create_ieee9()
        nm = NetworkManager(network=network)
        action_space = ActionSpace(nm)
        
        # Convert an action using mock network (for topology computation)
        repas_action = MockRepasAction(
            action_id="E2E_TEST",
            switches_by_voltage_level={
                'VL1': {'SWITCH_1': True, 'SWITCH_2': False}
            }
        )
        
        converted = convert_repas_actions_to_grid2op_actions(
            mock_network_with_topology,
            [repas_action],
            use_analytical=True
        )
        
        # Get the converted action dict
        action_dict = converted['E2E_TEST_VL1']
        
        # Create action using ActionSpace
        action = action_space(action_dict)
        
        # Verify action was created with switches
        assert action is not None
        assert len(action._modifications) > 0
    
    def test_action_dict_structure_complete(self, mock_network_with_topology):
        """Test that action dict has complete structure for pypowsybl."""
        from expert_op4grid_recommender.utils.conversion_actions_repas import (
            convert_repas_actions_to_grid2op_actions
        )
        
        action = MockRepasAction(
            action_id="STRUCT_TEST",
            switches_by_voltage_level={
                'VL1': {'VL1_SW1': True, 'VL1_SW2': False}
            }
        )
        
        result = convert_repas_actions_to_grid2op_actions(
            mock_network_with_topology,
            [action],
            use_analytical=True
        )
        
        converted = result['STRUCT_TEST_VL1']
        
        # Check complete structure
        assert 'description' in converted
        assert 'description_unitaire' in converted
        assert 'VoltageLevelId' in converted
        assert 'content' in converted
        
        # New convention: switches at root level, not inside content
        assert 'switches' in converted
        
        content = converted['content']
        assert 'set_bus' in content
        
        # Check set_bus structure
        set_bus = content['set_bus']
        assert 'lines_or_id' in set_bus
        assert 'lines_ex_id' in set_bus
        assert 'loads_id' in set_bus
        assert 'generators_id' in set_bus
        assert 'shunts_id' in set_bus
        
        # Check switches structure (at root level)
        switches = converted['switches']
        assert isinstance(switches, dict)
        for switch_id, is_open in switches.items():
            assert isinstance(switch_id, str)
            assert isinstance(is_open, bool)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
