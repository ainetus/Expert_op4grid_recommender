# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the MPL was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0

"""
ExpertOp4Grid Recommender - Expert system for power grid contingency analysis.

Analyzes N-1 contingencies in Grid2Op/pypowsybl environments, builds overflow
graphs, applies expert rules to filter potential actions, and identifies
corrective measures to alleviate line overloads.
"""

import logging

__version__ = "0.3.1.post1"

_logger = logging.getLogger(__name__)

# --- GLOBAL MONKEY PATCH for grid2op 1.12+ compatibility ---
# PyPowSyBlBackend 0.3.0 and other backends might not initialize self._sh_vnkv,
# which is required by grid2op 1.12+ for grids with shunts. The patch below
# lazily initializes ``_sh_vnkv`` on the grid2op ``Backend`` base class the
# first time ``get_shunt_setpoint`` is called.
#
# grid2op is optional: when it is not installed the patch is a no-op. Any other
# failure is logged (debug level) to avoid surfacing noise at package import
# time, but is not swallowed by a bare ``except``.
try:
    import numpy as np
    import grid2op.Backend.backend as g2op_bk
except ImportError as exc:
    _logger.debug("grid2op not available, skipping shunt-setpoint patch: %s", exc)
else:
    try:
        _orig_get_shunt_setpoint = g2op_bk.Backend.get_shunt_setpoint

        def _patched_get_shunt_setpoint(self):
            if (
                (not hasattr(self, "_sh_vnkv") or self._sh_vnkv is None)
                and getattr(self, "n_shunt", 0) > 0
            ):
                _logger.debug("Initializing _sh_vnkv for %s", type(self))
                self._sh_vnkv = np.ones(self.n_shunt, dtype=np.float32) * 225.0
            return _orig_get_shunt_setpoint(self)

        g2op_bk.Backend.get_shunt_setpoint = _patched_get_shunt_setpoint
        _logger.debug("grid2op.Backend.Backend.get_shunt_setpoint patched")
    except AttributeError as exc:
        # grid2op present but unexpected API surface — log and move on.
        _logger.debug("Failed to patch grid2op get_shunt_setpoint: %s", exc)
# -----------------------------------------------------------

# --- pypowsybl grid2op backend integer-value fix (0 sentinel -> -1), M5 ---
# Replaces the historical site-packages edit (scripts/patch_pypowsybl2grid_file.py)
# with an import-time, idempotent, version-guarded class patch. No-op when
# pypowsybl is not installed. Runs before any backend construction.
try:
    from expert_op4grid_recommender.patched_backend import (
        apply_pypowsybl_integer_value_patch,
    )
    apply_pypowsybl_integer_value_patch()
except Exception as exc:  # never let a patch failure break package import
    _logger.debug("Could not apply pypowsybl integer-value patch at import: %s", exc)
# -----------------------------------------------------------
# NOTE: the vendored alphaDeesp overflow-graph Dijkstra performance patch was
# removed once its two fixes landed upstream in expertop4grid 0.3.3 (the now
# floor): the precomputed edge-attribute weight in `_compute_sssp_paths`
# (ExpertOp4Grid_marota#1, "bless or fix") and the empty-red-loops guard in
# `get_dispatch_edges_nodes` (ExpertOp4Grid_marota#2). No local patch needed.
