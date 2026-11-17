# expert_op4grid_recommender/graph_analysis/structural_antennas.py
#!/usr/bin/python3
# Copyright (c) 2025-2026, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of expert_op4grid_recommender, Expert system analyzer based on ExpertOp4Grid principles.
"""Antennas hanging off the constrained path of the overflow graph.

An *antenna* is a part of the grid tied to the rest of the network through a
single node of the constrained path, or through several nodes of one side of it
when it is insensitive to the overload. An antenna that exports power is an
**evacuation antenna**: all its net production leaves through that attachment
node. Upstream (amont), it then runs down the constrained path into the
overload, so lowering it relieves the constraint. Downstream (aval), it feeds
the area the constraint supplies, so raising it relieves the constraint (and
curtailing it would aggravate it). Either way it is a lever, even though its
own branches are gray in the overflow graph.

Detection is topological, on the overflow graph restricted to the branches in
service in the N-1 state: remove the constrained-path nodes of one side; every
remaining connected component that touches neither the other side nor an
overloaded line, and is adjacent to at least one removed node (its attachment
nodes), is an antenna of that side.

With a single attachment node, the near-zero report is a consequence, not the
criterion. In DC, cutting the overloaded line only moves injections at its two
ends, both outside the antenna; every antenna node follows the attachment
node's angle, so the report on every antenna branch is exactly 0, even for an
internally meshed antenna. AC leaves a residual (losses, distributed slack),
reported as ``report_ratio_max`` / ``is_insensitive``. A component tied to
several nodes of the path carries the angle difference between them, so there
insensitivity becomes the criterion: it must stay below the gray threshold.

``tag_structural_antennas`` then types the detected antennas on the overflow
graph itself, as node / edge attributes.

See ``docs/recommender/evacuation_antennas.md``.
"""
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence

import networkx as nx
import numpy as np

from expert_op4grid_recommender import config
from expert_op4grid_recommender.utils.injections import (
    consumption_mw,
    gen_to_subid,
    load_to_subid,
    production_mw,
)

logger = logging.getLogger(__name__)

AMONT = "amont"
AVAL = "aval"

# Antenna type by (side, exports to its attachment node).
_ANTENNA_TYPES = {
    (AMONT, True): "evacuation",
    (AMONT, False): "consumption_upstream",
    (AVAL, False): "consumption",
    (AVAL, True): "evacuation_downstream",
}
_ID_PREFIXES = {
    "evacuation": "EVAC",
    "consumption_upstream": "CONS_UP",
    "consumption": "CONS",
    "evacuation_downstream": "EVAC_DOWN",
}
# The antenna types that evacuate production, upstream or downstream.
EVACUATION_TYPES = frozenset({"evacuation", "evacuation_downstream"})


@dataclass
class StructuralAntenna:
    """A sub-network tied to the constrained path through a single node, or
    through several nodes of one side when insensitive.

    ``export_mw`` is the active power leaving the antenna towards its
    attachment nodes (positive: the antenna feeds the constrained path).
    ``attachment_nodes`` lists them by decreasing power exchanged with the
    antenna; ``attachment_node`` (and its substation) is the first one. Only
    attachment nodes that map to a substation appear in
    ``attachment_sub_ids``. ``nodes`` are overflow-graph labels; ``sub_ids`` /
    ``sub_names`` the substations they map to.
    """

    antenna_id: str
    antenna_type: str
    side: str
    attachment_node: Any
    attachment_sub_id: Optional[int]
    attachment_sub_name: str
    nodes: List[Any]
    sub_ids: List[int]
    sub_names: List[str]
    connection_lines: List[str]
    internal_lines: List[str]
    export_mw: float
    prod_mw: float
    load_mw: float
    renewable_gens: List[str]
    dispatchable_gens: List[str]
    loads: List[str]
    report_ratio_max: float
    is_insensitive: bool
    attachment_nodes: List[Any] = field(default_factory=list)
    attachment_sub_ids: List[int] = field(default_factory=list)
    attachment_sub_names: List[str] = field(default_factory=list)

    def __post_init__(self):
        # A single attachment given through the scalar fields only.
        if not self.attachment_nodes:
            self.attachment_nodes = [self.attachment_node]
        if not self.attachment_sub_ids and self.attachment_sub_id is not None:
            self.attachment_sub_ids = [self.attachment_sub_id]
        if not self.attachment_sub_names:
            self.attachment_sub_names = [self.attachment_sub_name]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _base_color(color: Any) -> str:
    """Base colour of a (possibly compound, quoted) Graphviz colour string."""
    return str(color or "").split(":", 1)[0].strip().strip('"').lower()


def _plain(label: Any) -> Any:
    """Graph label as a plain Python value (numpy str / int labels compare and
    hash equal to them, so graph lookups keep working)."""
    if isinstance(label, np.str_):
        return str(label)
    if isinstance(label, np.integer):
        return int(label)
    return label


def _edge_report(data: Dict[str, Any]) -> float:
    """Report (flow change, MW) carried by an overflow-graph edge."""
    for key in ("capacity", "label"):
        try:
            return float(data[key])
        except (KeyError, TypeError, ValueError):
            continue
    return 0.0


def detect_structural_antennas(
    g_overflow: Any,
    g_distribution_graph: Any,
    obs: Any,
    sides: Sequence[str] = (AMONT,),
    min_mw: float = 1.0,
    max_fraction: float = 0.25,
    report_threshold: Optional[float] = None,
    renewable_sources: Optional[Iterable[str]] = None,
) -> List[StructuralAntenna]:
    """Detect the antennas hanging off the constrained path.

    A component tied to several nodes of the path is kept only when it is
    insensitive (``report_ratio_max`` below ``report_threshold``). Pure
    function: neither graph is modified.

    Args:
        g_overflow: ``OverFlowGraph`` (or its networkx graph). Node labels may be
            substation names (step 2) or substation ids (after
            ``pre_process_graph_alphadeesp``). They may differ from
            ``g_distribution_graph``'s (discovery's pre-processing re-labels
            ``g`` to ids while the distribution graph keeps names): the
            constrained path is matched into the graph's label domain
            through ``obs.name_sub``.
        g_distribution_graph: ``Structured_Overload_Distribution_Graph`` built on
            the same graph; provides the constrained path.
        obs: N-1 observation (``line_status``, ``p_or`` / ``p_ex``, injections).
        sides: constrained-path sides to scan, ``"amont"`` and/or ``"aval"``.
        min_mw: minimum |export| (MW) at the attachment node to type an antenna.
        max_fraction: a component holding more than this share of the graph
            nodes is not an antenna (guard against a whole region hanging off a
            single node).
        report_threshold: insensitivity threshold on
            ``|report| / |report of the overload|`` (a criterion for a
            component tied to several nodes); defaults to the gray-edge
            threshold ``ThresholdReportOfLine``.
        renewable_sources: energy sources counted as renewable; defaults to
            ``config.RENEWABLE_ENERGY_SOURCES``.

    Returns:
        list[StructuralAntenna]: sorted by side, then decreasing |export|.
        Ids are ``<TYPE PREFIX>_<first attachment substation>_<k>``.
    """
    g = getattr(g_overflow, "g", g_overflow)
    if report_threshold is None:
        report_threshold = float(
            config.PARAM_OPTIONS_EXPERT_OP.get("ThresholdReportOfLine", 0.05))
    if renewable_sources is None:
        renewable_sources = config.RENEWABLE_ENERGY_SOURCES
    renewable_set = {str(s).upper() for s in renewable_sources}

    cp = g_distribution_graph.get_constrained_path()
    if cp is None:
        return []

    # Graph labels <-> substation ids (labels are names or ids).
    name_sub = [str(s) for s in obs.name_sub]
    sub_of_label: Dict[Any, int] = {}
    for sub_id, name in enumerate(name_sub):
        if name in g:
            sub_of_label[name] = sub_id
        elif sub_id in g:
            sub_of_label[sub_id] = sub_id
    label_of_sub = {sub_id: label for label, sub_id in sub_of_label.items()}
    name_to_sub = {name: i for i, name in enumerate(name_sub)}

    # The distribution graph keeps the substation NAMES while discovery's
    # pre-processing re-labels ``g`` back to substation IDs
    # (``pre_process_graph_alphadeesp``) — so the two may be in different
    # label domains. Everything below is matched through substation ids
    # into the overflow graph's domain; untranslated nodes pass through.
    def to_graph_label(x: Any) -> Any:
        if x in g:
            return x
        sid = name_to_sub.get(str(x))
        if sid is None and isinstance(x, int):
            sid = x
        return label_of_sub.get(sid, x)

    side_nodes = {
        AMONT: {to_graph_label(n) for n in cp.n_amont()},
        AVAL: {to_graph_label(n) for n in cp.n_aval()},
    }

    # Branches in service in the N-1 state. The overflow graph keeps one edge
    # per line, open ones included (gray, e.g. the contingency itself), so the
    # connectivity must come from ``line_status``. Unknown names stay connected.
    line_idx = {str(name): i for i, name in enumerate(obs.name_line)}
    line_status = np.asarray(obs.line_status, dtype=bool)
    cut_nodes = set()
    cut_reports = []
    edges = []  # (u, v, line name, |report|) of the in-service branches
    for u, v, data in g.edges(data=True):
        report = abs(_edge_report(data))
        if data.get("constrained") or _base_color(data.get("color")) == "black":
            cut_nodes.update((u, v))
            cut_reports.append(report)
        name = data.get("name")
        i = line_idx.get(str(name)) if name is not None else None
        if u != v and (i is None or line_status[i]):
            edges.append((u, v, name, report))
    ref_report = max(cut_reports, default=0.0)

    connectivity = nx.Graph()
    connectivity.add_nodes_from(g.nodes)
    incident: Dict[Any, List[int]] = {}
    for k, (u, v, _, _) in enumerate(edges):
        connectivity.add_edge(u, v)
        incident.setdefault(u, []).append(k)
        incident.setdefault(v, []).append(k)

    # Element data, read once.
    prod = production_mw(obs)
    cons = consumption_mw(obs)
    gens_by_sub: Dict[int, List[int]] = {}
    for gen_id, sub_id in enumerate(gen_to_subid(obs)):
        gens_by_sub.setdefault(int(sub_id), []).append(gen_id)
    loads_by_sub: Dict[int, List[int]] = {}
    for load_id, sub_id in enumerate(load_to_subid(obs)):
        loads_by_sub.setdefault(int(sub_id), []).append(load_id)
    name_gen = [str(n) for n in getattr(obs, "name_gen", [])]
    name_gen += [f"gen_{i}" for i in range(len(name_gen), len(prod))]
    name_load = [str(n) for n in getattr(obs, "name_load", [])]
    name_load += [f"load_{i}" for i in range(len(name_load), len(cons))]
    sources = getattr(obs, "gen_energy_source", getattr(obs, "gen_type", None))
    is_renewable = np.zeros(len(prod), dtype=bool)
    if sources is not None:
        for gen_id, source in enumerate(list(sources)[:len(prod)]):
            is_renewable[gen_id] = str(source).upper() in renewable_set
    or_sub = np.asarray(obs.line_or_to_subid, dtype=int)
    ex_sub = np.asarray(obs.line_ex_to_subid, dtype=int)
    p_or = np.nan_to_num(np.asarray(obs.p_or, dtype=float))
    p_ex_raw = getattr(obs, "p_ex", None)
    p_ex = np.nan_to_num(np.asarray(p_ex_raw, dtype=float)) if p_ex_raw is not None else -p_or

    def describe(component, attachments, side):
        conn_lines, internal_lines = set(), set()
        max_report = 0.0
        for k in {k for node in component for k in incident.get(node, ())}:
            u, v, name, report = edges[k]
            max_report = max(max_report, report)
            if name is None:
                continue
            if u in component and v in component:
                internal_lines.add(name)
            else:  # the other end is an attachment node (the only neighbours)
                conn_lines.add(name)

        # Power leaving the antenna, read at the antenna-side terminal, and
        # the power exchanged with each attachment node.
        export = 0.0
        exchanged = dict.fromkeys(attachments, 0.0)
        for name in conn_lines:
            i = line_idx.get(str(name))
            if i is None:
                continue
            or_label, ex_label = label_of_sub.get(or_sub[i]), label_of_sub.get(ex_sub[i])
            if or_label in component or ex_label in attachments:
                flow, attachment = p_or[i], ex_label
            elif ex_label in component or or_label in attachments:
                flow, attachment = p_ex[i], or_label
            else:
                continue
            export += flow
            if attachment in exchanged:
                exchanged[attachment] += abs(flow)
        if abs(export) <= min_mw:
            return None
        attachments = sorted(attachments, key=lambda a: (-exchanged[a], str(a)))
        attachment = attachments[0]

        sub_ids = sorted(sub_of_label[n] for n in component if n in sub_of_label)
        gen_ids = [gid for s in sub_ids for gid in gens_by_sub.get(s, ())]
        load_ids = [lid for s in sub_ids for lid in loads_by_sub.get(s, ())]
        if ref_report > 0:
            ratio = max_report / ref_report
        else:
            ratio = 0.0 if max_report == 0 else float("inf")
        attachment_sub = sub_of_label.get(attachment)
        antenna_type = _ANTENNA_TYPES[(side, export > 0)]

        def sub_name_of(label):
            return name_sub[sub_of_label[label]] if label in sub_of_label else str(label)

        return StructuralAntenna(
            antenna_id="",  # assigned once sorted
            antenna_type=antenna_type,
            side=side,
            attachment_node=_plain(attachment),
            attachment_sub_id=attachment_sub,
            attachment_sub_name=sub_name_of(attachment),
            nodes=sorted((_plain(n) for n in component), key=str),
            sub_ids=sub_ids,
            sub_names=[name_sub[s] for s in sub_ids],
            connection_lines=sorted(map(str, conn_lines)),
            internal_lines=sorted(map(str, internal_lines)),
            export_mw=round(float(export), 2),
            prod_mw=round(float(sum(prod[gid] for gid in gen_ids)), 2),
            load_mw=round(float(sum(cons[lid] for lid in load_ids)), 2),
            renewable_gens=[name_gen[gid] for gid in gen_ids
                            if prod[gid] > 0 and is_renewable[gid]],
            dispatchable_gens=[name_gen[gid] for gid in gen_ids
                               if prod[gid] > 0 and not is_renewable[gid]],
            loads=[name_load[lid] for lid in load_ids if cons[lid] > 0],
            report_ratio_max=round(float(ratio), 4),
            is_insensitive=bool(ratio < report_threshold),
            attachment_nodes=[_plain(a) for a in attachments],
            attachment_sub_ids=[sub_of_label[a] for a in attachments if a in sub_of_label],
            attachment_sub_names=[sub_name_of(a) for a in attachments],
        )

    n_nodes = connectivity.number_of_nodes()
    antennas = []
    for side in sides:
        removed = side_nodes[side]
        excluded = side_nodes[AVAL if side == AMONT else AMONT] | cut_nodes
        remaining = connectivity.subgraph(n for n in connectivity if n not in removed)
        for component in nx.connected_components(remaining):
            if not component.isdisjoint(excluded):
                continue  # the rest of the grid
            attachments = {nb for node in component for nb in connectivity[node]
                           if nb in removed}
            if not attachments:
                continue  # already islanded
            if len(component) > max_fraction * n_nodes:
                logger.debug("Component of %d nodes hanging off %s exceeds the "
                             "antenna size cap", len(component), attachments)
                continue
            antenna = describe(component, attachments, side)
            if antenna is None:
                continue
            if len(attachments) > 1 and not antenna.is_insensitive:
                logger.debug("Component tied to %s carries report (%.1f%% of the "
                             "overload's): not an antenna", sorted(map(str, attachments)),
                             100 * antenna.report_ratio_max)
                continue
            antennas.append(antenna)

    side_rank = {side: rank for rank, side in enumerate(sides)}
    antennas.sort(key=lambda a: (side_rank[a.side], -abs(a.export_mw),
                                 a.attachment_sub_name, str(a.nodes[0])))
    counters: Dict[str, int] = {}
    for antenna in antennas:
        stem = f"{_ID_PREFIXES[antenna.antenna_type]}_{antenna.attachment_sub_name}"
        counters[stem] = counters.get(stem, 0) + 1
        antenna.antenna_id = f"{stem}_{counters[stem]}"
    return antennas


def tag_structural_antennas(g_overflow: Any, antennas: Sequence[StructuralAntenna]) -> None:
    """Type the antennas on the overflow graph, in place.

    Antenna nodes and branches (connection and internal lines) get
    ``antenna_id``, ``antenna_type`` and ``in_evacuation_antenna`` (upstream or
    downstream evacuation antenna); attachment nodes (all of them) get
    ``is_antenna_attachment``. Elements outside any antenna are left
    untouched, so an absent attribute means "no". The attributes survive the
    re-labelling of ``pre_process_graph_alphadeesp``; colours, reports and
    labels are not modified.

    Args:
        g_overflow: ``OverFlowGraph`` (or its networkx graph) the antennas were
            detected on.
        antennas: output of :func:`detect_structural_antennas`.
    """
    g = getattr(g_overflow, "g", g_overflow)
    tags_of_line: Dict[str, Dict[str, Any]] = {}
    for antenna in antennas:
        tags = {
            "antenna_id": antenna.antenna_id,
            "antenna_type": antenna.antenna_type,
            "in_evacuation_antenna": antenna.antenna_type in EVACUATION_TYPES,
        }
        for node in antenna.nodes:
            if node in g:
                g.nodes[node].update(tags)
        for attachment in antenna.attachment_nodes:
            if attachment in g:
                g.nodes[attachment]["is_antenna_attachment"] = True
        for line in antenna.connection_lines + antenna.internal_lines:
            tags_of_line[line] = tags
    if not tags_of_line:
        return
    for _, _, data in g.edges(data=True):
        tags = tags_of_line.get(str(data.get("name")))
        if tags is not None:
            data.update(tags)
