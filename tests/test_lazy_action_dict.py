# tests/test_lazy_action_dict.py
"""
Tests for the LazyActionDict class and enrich_actions_lazy function.

Verifies:
- Lazy computation of 'content' from 'switches' on first access
- Backward compatibility when 'content' is already present
- Correct behavior for actions without switches
- Single computation (idempotency)
- Integration with NetworkTopologyCache
"""

from unittest.mock import MagicMock, patch


class TestLazyActionDict:
    """Tests for the LazyActionDict dict subclass."""

    def test_content_in_contains_returns_true(self):
        """'content' in lazy_dict should return True even when not yet computed."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        lazy = LazyActionDict({"switches": {"sw1": False}}, topology_cache=MagicMock())
        assert "content" in lazy

    def test_content_not_computed_until_accessed(self):
        """Content should not be computed until explicitly accessed."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        lazy = LazyActionDict({"switches": {"sw1": False}}, topology_cache=cache)

        assert not lazy._content_computed
        cache.compute_bus_assignments.assert_not_called()

    def test_getitem_triggers_computation(self):
        """Accessing ['content'] should trigger lazy computation."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {"sw1": "VL1"}
        # First call: initial state (empty switches); second call: after switch changes.
        cache.compute_bus_assignments.side_effect = [
            {"VL1": {"node_a": 1}},  # initial
            {"VL1": {"node_a": 2}},  # final
        ]
        _empty = {"lines_or_id": {}, "lines_ex_id": {}, "loads_id": {}, "generators_id": {}, "shunts_id": {}}
        cache.get_element_bus_assignments.side_effect = [
            {**_empty, "lines_or_id": {"L1": 1}},  # initial assignments
            {**_empty, "lines_or_id": {"L1": 2}},  # final assignments (L1 moved to bus 2)
        ]

        lazy = LazyActionDict(
            {"switches": {"sw1": False}, "VoltageLevelId": "VL1"},
            topology_cache=cache
        )

        content = lazy["content"]

        assert lazy._content_computed
        assert cache.compute_bus_assignments.call_count == 2
        assert cache.get_element_bus_assignments.call_count == 2
        assert "set_bus" in content
        # Only L1 changed (1 → 2), so only L1 is in the result.
        assert content["set_bus"] == {"lines_or_id": {"L1": 2}, "lines_ex_id": {}, "loads_id": {}, "generators_id": {}, "shunts_id": {}}
        assert content["switches"] == {"sw1": False}

    def test_get_triggers_computation(self):
        """Accessing .get('content') should trigger lazy computation."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {"sw1": "VL1"}
        cache.compute_bus_assignments.side_effect = [{}, {}]
        cache.get_element_bus_assignments.side_effect = [{}, {}]

        lazy = LazyActionDict({"switches": {"sw1": False}}, topology_cache=cache)

        content = lazy.get("content", {})

        assert lazy._content_computed
        assert isinstance(content, dict)

    def test_computation_happens_only_once(self):
        """Content computation should happen exactly once, even with multiple accesses.

        Internally the computation makes 2 calls to compute_bus_assignments (one for
        the initial baseline and one for the final state after switch changes), but
        those 2 calls happen only on the first access.  Subsequent accesses return
        the cached result without any additional cache calls.
        """
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {"sw1": "VL1"}
        cache.compute_bus_assignments.side_effect = [{}, {}]
        cache.get_element_bus_assignments.side_effect = [{}, {}]

        lazy = LazyActionDict({"switches": {"sw1": False}}, topology_cache=cache)

        lazy["content"]
        lazy["content"]
        lazy.get("content")

        # 2 calls: one for initial state, one for final state (only on first access).
        assert cache.compute_bus_assignments.call_count == 2

    def test_preexisting_content_without_switches_not_recomputed(self):
        """Content of an action without switches is used as is."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        existing_content = {"set_bus": {"lines_or_id": {"L1": -1}}}
        cache = MagicMock()

        lazy = LazyActionDict({"content": existing_content}, topology_cache=cache)

        assert lazy._content_computed  # Already marked as computed
        assert lazy["content"] == existing_content
        cache.compute_bus_assignments.assert_not_called()

    def test_preexisting_content_kept_without_cache(self):
        """Without a topology cache the file content cannot be re-derived: keep it."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        existing_content = {"set_bus": {"lines_or_id": {"L1": 2}}}
        lazy = LazyActionDict(
            {"content": existing_content, "switches": {"sw1": False}},
            topology_cache=None,
        )

        assert lazy._content_computed
        assert lazy["content"] == existing_content

    @staticmethod
    def _stale_switch_action():
        """A REPAS-like coupling action whose shipped set_bus is stale."""
        return {
            "switches": {"sw_coupl": True},
            "content": {
                "set_bus": {
                    "lines_or_id": {"L_MOVED": 2, "L_STALE_OFF": -1},
                    "lines_ex_id": {}, "loads_id": {}, "generators_id": {},
                },
                "pst_tap": {"PST1": 3},
            },
        }

    def test_preexisting_switch_content_is_rederived(self):
        """set_bus shipped with switches is re-derived on the loaded network.

        The stale ``-1`` (a line out of service when the catalogue was built)
        must not survive: only elements the switches actually move are kept.
        """
        from expert_op4grid_recommender.data_loader import LazyActionDict

        _empty = {"lines_or_id": {}, "lines_ex_id": {}, "loads_id": {}, "generators_id": {}, "shunts_id": {}}
        cache = MagicMock()
        cache._switch_to_vl = {"sw_coupl": "VL1"}
        cache.compute_bus_assignments.side_effect = [{"VL1": {}}, {"VL1": {}}]
        cache.get_element_bus_assignments.side_effect = [
            {**_empty, "lines_or_id": {"L_MOVED": 1, "L_STALE_OFF": 1}},
            {**_empty, "lines_or_id": {"L_MOVED": 2, "L_STALE_OFF": 1}},
        ]

        lazy = LazyActionDict(self._stale_switch_action(), topology_cache=cache)
        assert not lazy._content_computed

        content = lazy["content"]

        assert content["set_bus"]["lines_or_id"] == {"L_MOVED": 2}
        assert content["switches"] == {"sw_coupl": True}
        assert content["pst_tap"] == {"PST1": 3}  # other content keys survive

    def test_preexisting_switch_content_kept_when_switches_unknown(self):
        """Switches absent from the network: fall back to the file content."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {}
        data = self._stale_switch_action()

        content = LazyActionDict(data, topology_cache=cache)["content"]

        assert content == data["content"]
        cache.compute_bus_assignments.assert_not_called()

    def test_preexisting_switch_content_kept_when_derivation_fails(self):
        """A derivation error falls back to the file content, not to an empty set_bus."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {"sw_coupl": "VL1"}
        cache.compute_bus_assignments.side_effect = RuntimeError("boom")
        data = self._stale_switch_action()

        content = LazyActionDict(data, topology_cache=cache)["content"]

        assert content == data["content"]

    def test_disco_action_from_action_id(self):
        """disco_* actions should derive content from action ID."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        lazy = LazyActionDict(
            {"description_unitaire": "Ouverture de la ligne 'AISERL31MAGNY'"},
            topology_cache=MagicMock(),
            action_id="disco_AISERL31MAGNY"
        )

        content = lazy["content"]

        assert content["set_bus"]["lines_or_id"] == {"AISERL31MAGNY": -1}
        assert content["set_bus"]["lines_ex_id"] == {"AISERL31MAGNY": -1}
        assert content["set_bus"]["loads_id"] == {}
        assert content["set_bus"]["generators_id"] == {}

    def test_reco_action_from_action_id(self):
        """reco_* actions should derive content (status 1) from action ID."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        lazy = LazyActionDict(
            {"description_unitaire": "Fermeture de la ligne 'BOISSL61GEN.P'"},
            topology_cache=MagicMock(),
            action_id="reco_BOISSL61GEN.P"
        )

        content = lazy["content"]

        assert content["set_bus"]["lines_or_id"] == {"BOISSL61GEN.P": 1}
        assert content["set_bus"]["lines_ex_id"] == {"BOISSL61GEN.P": 1}
        assert content["set_bus"]["loads_id"] == {}
        assert content["set_bus"]["generators_id"] == {}

    def test_disco_action_from_description(self):
        """disco actions should fall back to description if action_id has no disco_ prefix."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        lazy = LazyActionDict(
            {"description_unitaire": "Ouverture de la ligne 'CPVANL61ZMAGN'"},
            topology_cache=MagicMock(),
            action_id="some_other_id"
        )

        content = lazy["content"]

        assert content["set_bus"]["lines_or_id"] == {"CPVANL61ZMAGN": -1}
        assert content["set_bus"]["lines_ex_id"] == {"CPVANL61ZMAGN": -1}

    def test_no_switches_no_disco_returns_empty_set_bus(self):
        """Actions without switches and not disco_* should get empty set_bus."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        lazy = LazyActionDict(
            {"description": "some non-disco action"},
            topology_cache=cache,
            action_id="unknown_action"
        )

        content = lazy["content"]

        assert content == {"set_bus": {}}
        cache.compute_bus_assignments.assert_not_called()

    def test_no_cache_returns_empty_set_bus(self):
        """Switch actions without a topology cache should get empty set_bus."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        lazy = LazyActionDict({"switches": {"sw1": False}}, topology_cache=None)

        content = lazy["content"]

        assert content == {"set_bus": {}}

    def test_other_keys_work_normally(self):
        """Non-content keys should work as normal dict operations."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        lazy = LazyActionDict(
            {"description": "test", "switches": {"sw1": False}},
            topology_cache=MagicMock()
        )

        assert lazy["description"] == "test"
        assert lazy.get("description") == "test"
        assert lazy.get("nonexistent", "default") == "default"
        assert "description" in lazy
        assert "nonexistent" not in lazy

    def test_vl_ids_derived_from_switch_to_vl(self):
        """Impacted VL IDs should be derived from _switch_to_vl mapping."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {"sw1": "VL_A", "sw2": "VL_B"}
        cache.compute_bus_assignments.side_effect = [{}, {}]
        cache.get_element_bus_assignments.side_effect = [{}, {}]

        lazy = LazyActionDict(
            {"switches": {"sw1": False, "sw2": True}},
            topology_cache=cache
        )

        lazy["content"]

        # Two calls are made: first with empty switches (initial baseline), second
        # with the actual switch changes.  Verify the second call uses the correct args.
        all_calls = cache.compute_bus_assignments.call_args_list
        assert len(all_calls) == 2
        # First call: initial state, no switch changes
        assert all_calls[0][0][0] == {}
        assert all_calls[0][0][1] == {"VL_A", "VL_B"}
        # Second call: actual switch changes
        assert all_calls[1][0][0] == {"sw1": False, "sw2": True}
        assert all_calls[1][0][1] == {"VL_A", "VL_B"}

    def test_unknown_switch_ids_produce_warning(self):
        """Switches not in _switch_to_vl should produce empty content with warning."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {}  # No switches known

        lazy = LazyActionDict(
            {"switches": {"unknown_sw": False}},
            topology_cache=cache
        )

        content = lazy["content"]

        assert content == {"set_bus": {}}
        cache.compute_bus_assignments.assert_not_called()

    def test_cache_exception_returns_empty_set_bus(self):
        """If cache computation fails, content should have empty set_bus."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {"sw1": "VL1"}
        # The first compute_bus_assignments call (initial state) raises.
        cache.compute_bus_assignments.side_effect = RuntimeError("computation failed")

        lazy = LazyActionDict(
            {"switches": {"sw1": False}},
            topology_cache=cache
        )

        content = lazy["content"]

        assert content == {"set_bus": {}, "switches": {"sw1": False}}

    def test_only_changed_elements_returned(self):
        """set_bus should contain only elements whose bus assignment changed."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        cache = MagicMock()
        cache._switch_to_vl = {"sw1": "VL1"}

        _base = {"lines_or_id": {}, "lines_ex_id": {}, "loads_id": {}, "generators_id": {}, "shunts_id": {}}

        # Initial state: CPVANY632 is on bus 1, other assets also on bus 1
        initial_set_bus = {
            **_base,
            "lines_or_id": {"CPVANL31MESNA": 1, "CPVANL31RIBAU": 1},
            "lines_ex_id": {"CPVANY632": 1, "CPVANY633": 1},
            "loads_id": {"CPVAN3TR311": 1},
        }
        # After opening the breaker: only CPVANY632 becomes disconnected (bus -1)
        final_set_bus = {
            **_base,
            "lines_or_id": {"CPVANL31MESNA": 1, "CPVANL31RIBAU": 1},
            "lines_ex_id": {"CPVANY632": -1, "CPVANY633": 1},
            "loads_id": {"CPVAN3TR311": 1},
        }

        cache.compute_bus_assignments.side_effect = [{"VL1": {}}, {"VL1": {}}]
        cache.get_element_bus_assignments.side_effect = [initial_set_bus, final_set_bus]

        lazy = LazyActionDict(
            {"switches": {"sw1": True}},  # opening breaker (True = open)
            topology_cache=cache
        )

        content = lazy["content"]
        set_bus = content["set_bus"]

        # Only CPVANY632 changed (1 → -1); all other assets are unchanged.
        assert set_bus["lines_ex_id"] == {"CPVANY632": -1}
        assert set_bus["lines_or_id"] == {}
        assert set_bus["loads_id"] == {}


class TestEnrichActionsLazy:
    """Tests for the enrich_actions_lazy function."""

    def test_wraps_all_actions(self):
        """All actions should be wrapped as LazyActionDict instances."""
        from expert_op4grid_recommender.data_loader import LazyActionDict

        with patch('expert_op4grid_recommender.data_loader.enrich_actions_lazy') as mock_fn:
            # Test the real function instead
            pass

        from expert_op4grid_recommender.data_loader import enrich_actions_lazy

        mock_network = MagicMock()
        # Mock NetworkTopologyCache constructor
        with patch('expert_op4grid_recommender.utils.conversion_actions_repas.NetworkTopologyCache') as MockCache:
            MockCache.return_value = MagicMock()

            dict_actions = {
                "a1": {"switches": {"sw1": False}},
                "a2": {"switches": {"sw2": True}},
            }

            result = enrich_actions_lazy(dict_actions, mock_network)

            assert len(result) == 2
            for action_id in result:
                assert isinstance(result[action_id], LazyActionDict)

    def test_shared_cache_across_actions(self):
        """All actions should share the same NetworkTopologyCache instance."""
        from expert_op4grid_recommender.data_loader import enrich_actions_lazy

        mock_network = MagicMock()
        with patch('expert_op4grid_recommender.utils.conversion_actions_repas.NetworkTopologyCache') as MockCache:
            cache_instance = MagicMock()
            MockCache.return_value = cache_instance

            dict_actions = {
                "a1": {"switches": {"sw1": False}},
                "a2": {"switches": {"sw2": True}},
            }

            result = enrich_actions_lazy(dict_actions, mock_network)

            # Both actions should reference the same cache
            assert result["a1"]._topology_cache is result["a2"]._topology_cache
            assert result["a1"]._topology_cache is cache_instance

    def test_preserves_existing_content_without_switches(self):
        """Actions without switches that already have content should retain it."""
        from expert_op4grid_recommender.data_loader import enrich_actions_lazy

        mock_network = MagicMock()
        with patch('expert_op4grid_recommender.utils.conversion_actions_repas.NetworkTopologyCache') as MockCache:
            MockCache.return_value = MagicMock()

            existing_content = {"set_bus": {"lines_or_id": {"L1": -1}}}
            dict_actions = {"a1": {"content": existing_content}}

            result = enrich_actions_lazy(dict_actions, mock_network)

            assert result["a1"]._content_computed
            assert result["a1"]["content"] == existing_content

    def test_switch_actions_with_content_are_rederived(self):
        """Switch actions shipping a content are re-derived on the loaded network."""
        from expert_op4grid_recommender.data_loader import enrich_actions_lazy

        mock_network = MagicMock()
        with patch('expert_op4grid_recommender.utils.conversion_actions_repas.NetworkTopologyCache') as MockCache:
            MockCache.return_value = MagicMock()

            dict_actions = {
                "a1": {"content": {"set_bus": {"lines_or_id": {"L1": -1}}}, "switches": {"sw1": False}},
            }

            result = enrich_actions_lazy(dict_actions, mock_network)

            assert not result["a1"]._content_computed

    def test_action_id_passed_to_lazy_dict(self):
        """Each LazyActionDict should receive its action_id."""
        from expert_op4grid_recommender.data_loader import enrich_actions_lazy

        mock_network = MagicMock()
        with patch('expert_op4grid_recommender.utils.conversion_actions_repas.NetworkTopologyCache') as MockCache:
            MockCache.return_value = MagicMock()

            dict_actions = {
                "disco_LINEA": {"description_unitaire": "Ouverture de la ligne 'LINEA'"},
                "switch_action": {"switches": {"sw1": False}},
            }

            result = enrich_actions_lazy(dict_actions, mock_network)

            assert result["disco_LINEA"]._action_id == "disco_LINEA"
            assert result["switch_action"]._action_id == "switch_action"

    def test_disco_action_content_via_enrich(self):
        """disco_* actions should produce correct content after enrich_actions_lazy."""
        from expert_op4grid_recommender.data_loader import enrich_actions_lazy

        mock_network = MagicMock()
        with patch('expert_op4grid_recommender.utils.conversion_actions_repas.NetworkTopologyCache') as MockCache:
            MockCache.return_value = MagicMock()

            dict_actions = {
                "disco_MYLINE": {"description_unitaire": "Ouverture de la ligne 'MYLINE'"},
            }

            result = enrich_actions_lazy(dict_actions, mock_network)
            content = result["disco_MYLINE"]["content"]

            assert content["set_bus"]["lines_or_id"] == {"MYLINE": -1}
            assert content["set_bus"]["lines_ex_id"] == {"MYLINE": -1}
