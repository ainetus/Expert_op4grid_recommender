# expert_op4grid_recommender/utils/injections.py
#!/usr/bin/python3
# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of expert_op4grid_recommender, Expert system analyzer based on ExpertOp4Grid principles.
"""Backend-agnostic access to generator and load injections.

The two observation flavours disagree on two points that silently break a
naive per-substation sum of ``gen_p`` / ``load_p``:

* **Sign convention.** grid2op reports a producing generator with a positive
  ``gen_p``. pypowsybl follows the IIDM receptor convention for terminal
  powers, so a producing generator has a *negative* ``p`` (IEEE 14:
  ``target_p = 232.4`` → ``p = -232.4``). Loads are positive on both.
* **Element location.** grid2op observations expose ``gen_to_subid`` /
  ``load_to_subid``; :class:`PypowsyblObservation` does not, only
  ``get_obj_connect_to(substation_id=...)``.

Every helper returns production counted **positive** (a generator absorbing
power, e.g. a pump, comes out negative) and consumption counted positive,
indexed like ``obs.gen_p`` / ``obs.load_p``.
"""
from typing import Any, Dict, List, Tuple

import numpy as np


def uses_receptor_sign_convention(obs: Any) -> bool:
    """True when generator ``p`` follows the receptor convention (pypowsybl).

    pypowsybl observations carry their ``NetworkManager``; grid2op ones don't.
    """
    return getattr(obs, "_network_manager", None) is not None


def production_mw(obs: Any) -> np.ndarray:
    """Active production per generator (MW), producing generators positive."""
    gen_p = getattr(obs, "gen_p", getattr(obs, "prod_p", None))
    if gen_p is None:
        return np.zeros(0, dtype=float)
    prod = np.nan_to_num(np.asarray(gen_p, dtype=float))
    return -prod if uses_receptor_sign_convention(obs) else prod


def consumption_mw(obs: Any) -> np.ndarray:
    """Active consumption per load (MW), consuming loads positive."""
    load_p = getattr(obs, "load_p", None)
    if load_p is None:
        return np.zeros(0, dtype=float)
    return np.nan_to_num(np.asarray(load_p, dtype=float))


def _element_to_subid(obs: Any, attr: str, key: str, n_elements: int) -> np.ndarray:
    """Substation id of each element, ``-1`` when it cannot be located.

    Uses the vectorised ``obs.<attr>`` when present (grid2op), else inverts
    ``get_obj_connect_to`` substation by substation (pypowsybl).
    """
    direct = getattr(obs, attr, None)
    if direct is not None:
        return np.asarray(direct, dtype=int)
    sub_ids = np.full(n_elements, -1, dtype=int)
    get_obj_connect_to = getattr(obs, "get_obj_connect_to", None)
    if get_obj_connect_to is None:
        return sub_ids
    for sub_id in range(len(obs.name_sub)):
        for elt in get_obj_connect_to(substation_id=sub_id).get(key, []):
            if 0 <= int(elt) < n_elements:
                sub_ids[int(elt)] = sub_id
    return sub_ids


def gen_to_subid(obs: Any) -> np.ndarray:
    """Substation id of each generator (``-1`` when it cannot be located)."""
    n_gen = len(production_mw(obs))
    return _element_to_subid(obs, "gen_to_subid", "generators_id", n_gen)


def load_to_subid(obs: Any) -> np.ndarray:
    """Substation id of each load (``-1`` when it cannot be located)."""
    n_load = len(consumption_mw(obs))
    return _element_to_subid(obs, "load_to_subid", "loads_id", n_load)


def _sum_by_sub(values: np.ndarray, sub_ids: np.ndarray, n_sub: int) -> np.ndarray:
    """Sum ``values`` per substation, skipping elements that are not located."""
    n = min(len(values), len(sub_ids))
    values, sub_ids = values[:n], sub_ids[:n]
    located = (sub_ids >= 0) & (sub_ids < n_sub)
    out = np.zeros(n_sub, dtype=float)
    np.add.at(out, sub_ids[located], values[located])
    return out


def injections_by_sub(obs: Any) -> Tuple[np.ndarray, np.ndarray]:
    """Per-substation production and consumption (MW), indexed by substation id."""
    n_sub = len(obs.name_sub)
    prod = _sum_by_sub(production_mw(obs), gen_to_subid(obs), n_sub)
    load = _sum_by_sub(consumption_mw(obs), load_to_subid(obs), n_sub)
    return prod, load


def alphadeesp_node_injections(obs: Any) -> Dict[str, List[Any]]:
    """Per-substation injections in the shape alphaDeesp's ``build_nodes`` reads.

    ``are_prods`` / ``are_loads`` flag the substations holding a generator / a
    load. ``prods_values`` / ``loads_values`` hold one value per *flagged*
    substation, in substation order: ``build_nodes`` takes the next value only
    for a flagged substation, so a full per-substation array would shift the
    values onto other nodes. Each node then reads ``prod - load``: a production
    node (coral) or a consumption node (light blue).
    """
    n_sub = len(obs.name_sub)
    gen_sub, load_sub = gen_to_subid(obs), load_to_subid(obs)
    prod = _sum_by_sub(production_mw(obs), gen_sub, n_sub)
    load = _sum_by_sub(consumption_mw(obs), load_sub, n_sub)
    are_prods = _sum_by_sub(np.ones(len(gen_sub)), gen_sub, n_sub) > 0
    are_loads = _sum_by_sub(np.ones(len(load_sub)), load_sub, n_sub) > 0
    return {
        "are_prods": are_prods.tolist(),
        "are_loads": are_loads.tolist(),
        "prods_values": prod[are_prods].tolist(),
        "loads_values": load[are_loads].tolist(),
    }
