# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of expert_op4grid_recommender

"""
Tests for the Generalized Superposition Theorem (GST) — action pairs that
combine a topology action with an *injection* change (load shedding / renewable
curtailment / redispatch), or two injection changes.

Mirrors the structure of:
  https://github.com/marota/Topology_Superposition_Theorem/blob/master/superposition_theorem/tests/test_generalized_superposition.py

Each test verifies that ``compute_combined_pair_superposition`` (routed to the
GST path via ``act*_is_injection``) produces ``p_or`` / ``p_ex`` matching the
ground-truth combined-action simulation. The injection is a balanced grid2op
redispatch, which stands in for the load-shedding / curtailment / redispatch
``set_load_p`` / ``set_gen_p`` injection actions of the pypowsybl backend.
"""

import warnings
import numpy as np
import unittest
from unittest.mock import MagicMock

import grid2op
from grid2op.Parameters import Parameters
from lightsim2grid import LightSimBackend

from expert_op4grid_recommender.utils.superposition import (
    compute_combined_pair_superposition,
    compute_combined_pair_gst,
    compute_all_pairs_superposition,
    is_injection_action,
)
from expert_op4grid_recommender.action_evaluation.classifier import ActionClassifier


class TestGstActionTypeSuperposition(unittest.TestCase):

    def setUp(self) -> None:
        env_name = "l2rpn_case14_sandbox"
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore")
            params = Parameters()
            params.NO_OVERFLOW_DISCONNECTION = True
            params.LIMIT_INFEASIBLE_CURTAILMENT_STORAGE_ACTION = True
            params.ENV_DC = True
            params.MAX_LINE_STATUS_CHANGED = 99999
            params.MAX_SUB_CHANGED = 99999
            self.env = grid2op.make(env_name, backend=LightSimBackend(), param=params, test=True)
            self.env.set_max_iter(20)

        self.chronic_id = 0
        self.tol = 1e-3
        # Balanced redispatch — stands in for an injection action (LS / RC / redispatch)
        self.redisp = [(0, +5.0), (5, -5.0)]

    def tearDown(self) -> None:
        self.env.close()
        return super().tearDown()

    def _assert_accuracy(self, obs_target, result):
        self.assertNotIn("error", result, f"GST returned error: {result.get('error')}")
        p_computed = np.array(result["p_or_combined"])
        max_diff = np.max(np.abs(obs_target.p_or - p_computed))
        self.assertLessEqual(
            max_diff, self.tol,
            f"max |p_or_target - p_or_computed| = {max_diff:.2e} > tol {self.tol:.2e}")
        p_ex_computed = np.array(result["p_ex_combined"])
        max_diff_ex = np.max(np.abs(obs_target.p_ex - p_ex_computed))
        self.assertLessEqual(
            max_diff_ex, self.tol,
            f"max |p_ex_target - p_ex_computed| = {max_diff_ex:.2e} > tol {self.tol:.2e}")

    # =========================================================================
    # line disconnection + injection
    # =========================================================================
    def test_line_disconnection_plus_injection(self):
        self.env.set_id(self.chronic_id)
        self.env.reset()
        obs_start, *_ = self.env.step(self.env.action_space({}))
        id_l = 3
        obs_topo = obs_start.simulate(self.env.action_space({"set_line_status": [(id_l, -1)]}), 0)[0]
        obs_inj = obs_start.simulate(self.env.action_space({"redispatch": self.redisp}), 0)[0]
        obs_target = obs_start.simulate(
            self.env.action_space({"set_line_status": [(id_l, -1)], "redispatch": self.redisp}), 0)[0]

        result = compute_combined_pair_superposition(
            obs_start, obs_topo, obs_inj,
            act1_line_idxs=[id_l], act1_sub_idxs=[],
            act2_line_idxs=[], act2_sub_idxs=[],
            act1_is_injection=False, act2_is_injection=True,
        )
        self._assert_accuracy(obs_target, result)
        # injection is reported with beta = 1.0
        self.assertAlmostEqual(result["betas"][1], 1.0, places=6)
        self.assertTrue(result.get("is_gst"))

    # =========================================================================
    # injection + line disconnection  (reversed order)
    # =========================================================================
    def test_injection_plus_line_disconnection_reversed(self):
        self.env.set_id(self.chronic_id)
        self.env.reset()
        obs_start, *_ = self.env.step(self.env.action_space({}))
        id_l = 3
        obs_topo = obs_start.simulate(self.env.action_space({"set_line_status": [(id_l, -1)]}), 0)[0]
        obs_inj = obs_start.simulate(self.env.action_space({"redispatch": self.redisp}), 0)[0]
        obs_target = obs_start.simulate(
            self.env.action_space({"set_line_status": [(id_l, -1)], "redispatch": self.redisp}), 0)[0]

        # act1 = injection, act2 = topology
        result = compute_combined_pair_superposition(
            obs_start, obs_inj, obs_topo,
            act1_line_idxs=[], act1_sub_idxs=[],
            act2_line_idxs=[id_l], act2_sub_idxs=[],
            act1_is_injection=True, act2_is_injection=False,
        )
        self._assert_accuracy(obs_target, result)
        self.assertAlmostEqual(result["betas"][0], 1.0, places=6)

    # =========================================================================
    # line reconnection + injection
    # =========================================================================
    def test_line_reconnection_plus_injection(self):
        self.env.set_id(self.chronic_id)
        self.env.reset()
        id_l = 3
        obs_start, *_ = self.env.step(self.env.action_space({"set_line_status": [(id_l, -1)]}))
        obs_topo = obs_start.simulate(self.env.action_space({"set_line_status": [(id_l, +1)]}), 0)[0]
        obs_inj = obs_start.simulate(self.env.action_space({"redispatch": self.redisp}), 0)[0]
        obs_target = obs_start.simulate(
            self.env.action_space({"set_line_status": [(id_l, +1)], "redispatch": self.redisp}), 0)[0]

        result = compute_combined_pair_superposition(
            obs_start, obs_topo, obs_inj,
            act1_line_idxs=[id_l], act1_sub_idxs=[],
            act2_line_idxs=[], act2_sub_idxs=[],
            act1_is_injection=False, act2_is_injection=True,
        )
        self._assert_accuracy(obs_target, result)

    # =========================================================================
    # node split + injection
    # =========================================================================
    def test_node_split_plus_injection(self):
        self.env.set_id(self.chronic_id)
        self.env.reset()
        obs_start, *_ = self.env.step(self.env.action_space({}))
        id_sub = 5
        act_s = {"set_bus": {"substations_id": [(5, (1, 1, 2, 2, 1, 2, 2))]}}
        obs_topo = obs_start.simulate(self.env.action_space(act_s), 0)[0]
        obs_inj = obs_start.simulate(self.env.action_space({"redispatch": self.redisp}), 0)[0]
        obs_target = obs_start.simulate(
            self.env.action_space(act_s) + self.env.action_space({"redispatch": self.redisp}), 0)[0]

        result = compute_combined_pair_superposition(
            obs_start, obs_topo, obs_inj,
            act1_line_idxs=[], act1_sub_idxs=[id_sub],
            act2_line_idxs=[], act2_sub_idxs=[],
            act1_is_injection=False, act2_is_injection=True,
        )
        self._assert_accuracy(obs_target, result)

    # =========================================================================
    # node merge + injection
    # =========================================================================
    def test_node_merge_plus_injection(self):
        self.env.set_id(self.chronic_id)
        self.env.reset()
        id_sub = 5
        split = self.env.action_space({"set_bus": {"substations_id": [(5, (1, 1, 2, 2, 1, 2, 2))]}})
        obs_start, *_ = self.env.step(split)
        merge = {"set_bus": {"substations_id": [(5, (1, 1, 1, 1, 1, 1, 1))]}}
        obs_topo = obs_start.simulate(self.env.action_space(merge), 0)[0]
        obs_inj = obs_start.simulate(self.env.action_space({"redispatch": self.redisp}), 0)[0]
        obs_target = obs_start.simulate(
            self.env.action_space(merge) + self.env.action_space({"redispatch": self.redisp}), 0)[0]

        result = compute_combined_pair_superposition(
            obs_start, obs_topo, obs_inj,
            act1_line_idxs=[], act1_sub_idxs=[id_sub],
            act2_line_idxs=[], act2_sub_idxs=[],
            act1_is_injection=False, act2_is_injection=True,
        )
        self._assert_accuracy(obs_target, result)

    # =========================================================================
    # injection + injection
    # =========================================================================
    def test_injection_plus_injection(self):
        """Two injection changes combined.

        In DC this is exact (injection superposition is linear), which is why
        the assertion below holds to solver precision. The larger est-vs-sim
        gaps observed for injection+injection on an AC grid are therefore a
        structural AC-nonlinearity effect (two large injections compound the
        reactive / voltage coupling), NOT a formula error — see
        ``docs/recommender/superposition_module.md`` §10.
        """
        self.env.set_id(self.chronic_id)
        self.env.reset()
        obs_start, *_ = self.env.step(self.env.action_space({}))
        dP1 = [(0, +5.0), (5, -5.0)]
        dP2 = [(1, +5.0), (5, -5.0)]
        obs_i1 = obs_start.simulate(self.env.action_space({"redispatch": dP1}), 0)[0]
        obs_i2 = obs_start.simulate(self.env.action_space({"redispatch": dP2}), 0)[0]
        obs_target = obs_start.simulate(
            self.env.action_space({"redispatch": [(0, +5.0), (1, +5.0), (5, -10.0)]}), 0)[0]

        result = compute_combined_pair_superposition(
            obs_start, obs_i1, obs_i2,
            act1_line_idxs=[], act1_sub_idxs=[],
            act2_line_idxs=[], act2_sub_idxs=[],
            act1_is_injection=True, act2_is_injection=True,
        )
        self._assert_accuracy(obs_target, result)
        self.assertEqual(result["betas"], [1.0, 1.0])

    # =========================================================================
    # GST direct entry point matches the routed call
    # =========================================================================
    def test_gst_direct_entrypoint_matches(self):
        self.env.set_id(self.chronic_id)
        self.env.reset()
        obs_start, *_ = self.env.step(self.env.action_space({}))
        id_l = 3
        obs_topo = obs_start.simulate(self.env.action_space({"set_line_status": [(id_l, -1)]}), 0)[0]
        obs_inj = obs_start.simulate(self.env.action_space({"redispatch": self.redisp}), 0)[0]

        routed = compute_combined_pair_superposition(
            obs_start, obs_topo, obs_inj, [id_l], [], [], [],
            act1_is_injection=False, act2_is_injection=True)
        direct = compute_combined_pair_gst(
            obs_start, obs_topo, obs_inj, [id_l], [], [], [],
            act1_is_injection=False, act2_is_injection=True)
        np.testing.assert_allclose(routed["p_or_combined"], direct["p_or_combined"])
        self.assertEqual(routed["betas"], direct["betas"])


class TestIsInjectionAction(unittest.TestCase):
    """Detection of injection actions by id prefix and classifier type."""

    def setUp(self):
        self.classifier = ActionClassifier()

    def test_prefix_detection(self):
        self.assertTrue(is_injection_action("load_shedding_LOAD_5"))
        self.assertTrue(is_injection_action("curtail_GEN_2"))
        self.assertTrue(is_injection_action("redispatch_GEN_7"))
        self.assertFalse(is_injection_action("disco_LINE_3"))
        self.assertFalse(is_injection_action("reco_LINE_1"))
        self.assertFalse(is_injection_action("node_merging_SUB_4"))

    def test_classifier_type_detection(self):
        # load shedding via set_load_p
        self.assertTrue(is_injection_action(
            "x", {"set_load_p": {"L1": 0.0}}, self.classifier))
        # curtailment via set_gen_p
        self.assertTrue(is_injection_action(
            "x", {"set_gen_p": {"G1": 0.0}}, self.classifier))
        # redispatch via action_mode
        self.assertTrue(is_injection_action(
            "x", {"set_gen_p": {"G1": 50.0}, "action_mode": "redispatch"}, self.classifier))
        # a plain topology action is not an injection
        self.assertFalse(is_injection_action(
            "x", {"description_unitaire": "Ouverture ligne LINE_3"}, self.classifier))


class TestComputeAllPairsWithInjection(unittest.TestCase):
    """compute_all_pairs_superposition now includes injection-bearing pairs."""

    def test_injection_pairs_are_computed(self):
        env = MagicMock()
        env.name_line = ["LINE1", "LINE2", "LINE3"]

        def _obs(rho, p_or):
            o = MagicMock()
            o.rho = np.array(rho)
            o.p_or = np.array(p_or)
            o.p_ex = -np.array(p_or)
            return o

        obs_start = _obs([1.1, 0.5, 0.5], [100.0, 50.0, 50.0])
        obs_disco = _obs([1.0, 0.6, 0.6], [0.0, 60.0, 60.0])      # disconnect LINE1
        obs_shed = _obs([0.95, 0.45, 0.45], [85.0, 45.0, 45.0])   # load shedding

        detailed_actions = {
            "disco_LINE1": {"action": MagicMock(), "observation": obs_disco,
                            "description_unitaire": "Open LINE1", "non_convergence": None},
            "load_shedding_L7": {"action": MagicMock(), "observation": obs_shed,
                                 "description_unitaire": "Shed L7", "non_convergence": None},
        }

        classifier = MagicMock(spec=ActionClassifier)
        classifier.identify_action_type.return_value = "open_line"
        classifier._action_space = MagicMock()

        from expert_op4grid_recommender.utils import superposition
        orig_identify = superposition._identify_action_elements
        try:
            # disco_LINE1 -> line idx 0; load shedding -> no element
            superposition._identify_action_elements = MagicMock(
                side_effect=lambda action, aid, *a: ([0], []) if aid == "disco_LINE1" else ([], []))
            results = compute_all_pairs_superposition(
                obs_start=obs_start,
                detailed_actions=detailed_actions,
                classifier=classifier,
                env=env,
                lines_overloaded_ids=[0],
                lines_we_care_about=["LINE1", "LINE2", "LINE3"],
                pre_existing_rho={},
                dict_action={},
            )
        finally:
            superposition._identify_action_elements = orig_identify

        # The topology+injection pair must now be present (previously skipped).
        pair_id = "disco_LINE1+load_shedding_L7"
        self.assertIn(pair_id, results)
        res = results[pair_id]
        self.assertNotIn("error", res)
        # injection reported with beta = 1.0
        self.assertAlmostEqual(res["betas"][1], 1.0, places=6)


class _AcObs:
    """Minimal observation carrying AC-like flows (p_or != -p_ex, i.e. with
    line losses). Enough for the line-disconnection GST path, which reads only
    ``p_or`` / ``p_ex``."""

    def __init__(self, p_or, p_ex):
        self.p_or = np.array(p_or, dtype=float)
        self.p_ex = np.array(p_ex, dtype=float)


class TestGstIsAcAnchored(unittest.TestCase):
    """The GST is *AC-anchored*: both the injection superposition term and the
    injection-shifted beta right-hand side are read straight from the
    (AC) observation values — no DC quantity is recomputed. See
    ``docs/recommender/superposition_module.md`` §10.
    """

    def test_beta_rhs_and_reconstruction_use_observation_values(self):
        # AC-like states: p_or != -p_ex (losses present), arbitrary magnitudes.
        obs_start = _AcObs([100.0, 50.0, 30.0], [-99.0, -49.5, -29.7])
        obs_topo = _AcObs([0.0, 80.0, 45.0], [0.0, -79.0, -44.5])      # line 0 disconnected
        obs_inj = _AcObs([120.0, 40.0, 25.0], [-119.0, -39.6, -24.8])  # injection shifts flows
        sw = 0  # switched line: connected in obs_start (p_or=100 > 1 MW) -> p_or branch

        res = compute_combined_pair_gst(
            obs_start, obs_topo, obs_inj,
            act1_line_idxs=[sw], act1_sub_idxs=[],
            act2_line_idxs=[], act2_sub_idxs=[],
            act1_is_injection=False, act2_is_injection=True,
        )
        self.assertNotIn("error", res)

        # Q2: the beta RHS is the injection's AC p_or ratio on the switched line.
        beta_t_expected = obs_inj.p_or[sw] / obs_start.p_or[sw]  # 120/100 = 1.2
        self.assertAlmostEqual(res["betas"][0], beta_t_expected, places=9)
        self.assertEqual(res["betas"][1], 1.0)  # injection term added in full

        # Q1: the reconstruction superposes the AC p_or AND p_ex values verbatim.
        w = 1.0 - sum(res["betas"])
        exp_por = w * obs_start.p_or + res["betas"][0] * obs_topo.p_or + res["betas"][1] * obs_inj.p_or
        exp_pex = w * obs_start.p_ex + res["betas"][0] * obs_topo.p_ex + res["betas"][1] * obs_inj.p_ex
        np.testing.assert_allclose(res["p_or_combined"], exp_por, atol=1e-9)
        np.testing.assert_allclose(res["p_ex_combined"], exp_pex, atol=1e-9)

        # p_ex is carried independently of p_or — only meaningful in AC, where
        # p_or != -p_ex. This confirms AC (not DC-symmetric) values flow through.
        self.assertFalse(np.allclose(res["p_ex_combined"], -np.asarray(res["p_or_combined"])))

    def test_equivalent_to_explicit_gst_reconstruction(self):
        """The reported flows equal alpha*start + beta*topo + (inj - start)."""
        obs_start = _AcObs([80.0, 60.0], [-79.2, -59.4])
        obs_topo = _AcObs([0.0, 95.0], [0.0, -94.0])
        obs_inj = _AcObs([72.0, 64.0], [-71.3, -63.4])
        sw = 0
        res = compute_combined_pair_gst(
            obs_start, obs_topo, obs_inj, [sw], [], [], [],
            act1_is_injection=False, act2_is_injection=True)
        beta_t = obs_inj.p_or[sw] / obs_start.p_or[sw]
        # GST form: alpha*start + beta_t*topo + pure-superposition injection term
        explicit = ((1.0 - beta_t) * obs_start.p_or + beta_t * obs_topo.p_or
                    + (obs_inj.p_or - obs_start.p_or))
        np.testing.assert_allclose(res["p_or_combined"], explicit, atol=1e-9)


if __name__ == "__main__":
    unittest.main()
