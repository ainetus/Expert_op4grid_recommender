"""
manoeuvre/algo/graph_ops.py — Helpers bas niveau : index O(1), lecture/écriture d'organes, chemins structurels, couplers inter-SJB.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Optional
import networkx as nx

from ..models import SwitchKind
from ..cellules import CelluleDepart
from ..topologie import PosteTopologique


def _switch_edge_index(G: nx.Graph) -> dict[str, tuple[int, int]]:
    """Index ``switch_id -> (u, v)`` mémoïsé sur le graphe (``G.graph``).

    Construit en une passe au premier accès, puis réutilisé en **O(1)** — il
    remplace les anciens scans linéaires de ``_is_open`` / ``_set_switch``.

    Validité : la coordonnée ``(u, v)`` d'un switch est **topologique**, donc
    stable tant que la structure du graphe ne change pas. Le séquenceur ne fait
    que basculer l'attribut ``open`` (jamais ajouter/retirer d'arête), et
    ``G.copy()`` préserve nœuds et arêtes : l'index reste valide sur les graphes
    dérivés (copies de travail, ``cible_graph`` du même réseau).

    Garde-fou **O(1)** : le nombre de *nœuds* (``len(G)``, immédiat) sert de
    canari. ``number_of_edges()`` serait O(arêtes) (somme des degrés) et, appelé
    à chaque lookup, annulerait le bénéfice de l'index. Dans ce module la
    structure (nœuds **et** arêtes) est figée après construction ; une variation
    du nombre de nœuds (sous-graphe, vue) force la reconstruction.
    """
    cache = G.graph.get("_switch_edge_index")
    if cache is None or cache[0] != G.number_of_nodes():
        mapping = {d["switch_id"]: (u, v)
                   for u, v, d in G.edges(data=True)
                   if d.get("switch_id") is not None}
        cache = (G.number_of_nodes(), mapping)
        G.graph["_switch_edge_index"] = cache
    return cache[1]


def _equipment_node_index(G: nx.Graph) -> dict[str, int]:
    """Index ``equipment_id -> node`` mémoïsé sur le graphe (cf.
    ``_switch_edge_index`` ; garde-fou O(1) sur le nombre de nœuds)."""
    cache = G.graph.get("_equipment_node_index")
    if cache is None or cache[0] != G.number_of_nodes():
        mapping = {d["equipment_id"]: n
                   for n, d in G.nodes(data=True)
                   if d.get("equipment_id") is not None}
        cache = (G.number_of_nodes(), mapping)
        G.graph["_equipment_node_index"] = cache
    return cache[1]


def _set_switch(G: nx.Graph, switch_id: str, open_: bool) -> None:
    """Modifie l'état d'un switch (par son id) dans le graphe simulé.

    No-op silencieux si l'id est inconnu (contrat historique, cf.
    ``tests/manoeuvre/test_lookup_helpers.py``)."""
    edge = _switch_edge_index(G).get(switch_id)
    if edge is not None:
        G.edges[edge]["open"] = open_


def _is_open(G: nx.Graph, switch_id: str) -> bool:
    """État d'un switch (True = ouvert). Un id inconnu est considéré **ouvert**
    (contrat historique)."""
    edge = _switch_edge_index(G).get(switch_id)
    if edge is None:
        return True
    return bool(G.edges[edge].get("open", False))


def _eq_node(G: nx.Graph, eq_id: str) -> Optional[int]:
    """Nœud de connectivité d'un équipement (ou ``None`` si inconnu)."""
    return _equipment_node_index(G).get(eq_id)


def _edges_of_switches(graph: nx.Graph, switch_ids):
    """Arêtes (u, v) des switches donnés, via l'index O(1) (un id inconnu est
    simplement omis — même sémantique que l'ancien scan linéaire)."""
    idx = _switch_edge_index(graph)
    return [idx[sid] for sid in switch_ids if sid in idx]


def _sa_path_to_sjb(cell: CelluleDepart, sjb_node: int) -> list[str]:
    """IDs des sectionneurs (SA) sur le chemin départ -> SJB."""
    return [s.switch_id for s in cell.disconnectors_vers_barre(sjb_node)]


def _wired_busbar(cell: CelluleDepart, graph: nx.Graph) -> Optional[int]:
    """SJB sur laquelle un départ est câblé (chemin de SA fermés) dans ``graph``."""
    for bb in cell.busbar_nodes:
        sa = _sa_path_to_sjb(cell, bb)
        if sa and all(not graph.edges[u, v].get("open", False)
                      for u, v in _edges_of_switches(graph, sa)):
            return bb
    return None


def _own_breakers_to_sjb(
    cell: CelluleDepart, sjb_node: int, eq_id: str | None = None
) -> list[str]:
    """
    **Disjoncteur d'ensemble de la cellule** à manœuvrer pour mettre le départ
    hors tension lors d'un ré-aiguillage en boucle longue.

    Règle (cf. docs/manoeuvre/regles.md, R7) : on n'ouvre que le DJ situé
    **côté sélecteurs de barre** (entre les sectionneurs d'aiguillage et le
    reste de la cellule). Ouvrir ce seul disjoncteur dé-énergise la cellule et
    suffit pour basculer ensuite les sectionneurs ; on n'ouvre **pas** les
    disjoncteurs propres aux équipements situés en aval (cas omnibus : un même
    DJ de cellule alimente plusieurs équipements).

    Méthode : on parcourt le chemin équipement → SJB depuis le **côté barre**,
    on saute les sectionneurs (sélecteurs de barre), puis on collecte le(s)
    disjoncteur(s) en série jusqu'à un nœud de branchement (degré > 2, signe
    d'un point omnibus partagé) ou l'équipement.
    """
    if cell.subgraph is None:
        return [b.switch_id for b in cell.breakers]
    sg = cell.subgraph
    eq_id = eq_id or cell.equipment_id
    eq_node = next((n for n, d in sg.nodes(data=True)
                    if d.get("equipment_id") == eq_id), None)
    if eq_node is None:
        return [b.switch_id for b in cell.breakers]
    try:
        path = nx.shortest_path(sg, eq_node, sjb_node)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return []

    res: list[str] = []
    collecting = False
    # Parcours depuis le côté barre (path[::-1] = [sjb, …, équipement])
    rev = path[::-1]
    for a, b in zip(rev, rev[1:]):
        d = sg.edges[a, b]
        kind = d.get("kind")
        sid = d.get("switch_id")
        if kind == SwitchKind.BREAKER and sid:
            res.append(sid)
            collecting = True
            # Nœud de branchement omnibus atteint -> DJ de cellule identifié.
            if b != eq_node and sg.degree(b) > 2:
                break
        elif kind == SwitchKind.DISCONNECTOR:
            if collecting:
                break          # sectionneur en aval du DJ -> fin
            continue            # sélecteurs de barre en amont -> on saute
        elif collecting:
            break
    return list(dict.fromkeys(res))


def _wired_sjbs(G: nx.Graph, cells, eq_id: str) -> set[int]:
    """SJB vers lesquelles le départ est *câblé* (SA fermés), indépendamment
    de la connectivité électrique globale."""
    cell = cells.get_cellule_depart(eq_id)
    if cell is None:
        return set()
    res = set()
    for bb in cell.busbar_nodes:
        sa = _sa_path_to_sjb(cell, bb)
        if sa and all(not _is_open(G, s) for s in sa):
            res.add(bb)
    return res


def _organes_internes_2bornes(poste: PosteTopologique) -> set[str]:
    """Équipements présents dans **plusieurs cellules de départ**.

    Détection **structurelle** (pas par identifiant) : un organe interne à 2
    bornes (typiquement une self/réactance dont les deux côtés sont câblés chacun
    sur une barre) apparaît dans deux cellules de départ. Ces organes sont laissés
    en place (ni ré-aiguillés ni signalés en écart)."""
    occ: Counter = Counter()
    for c in poste.cellules.cellules_depart:
        for eq in {c.equipment_id} | set(c.shared_equipment_ids):
            occ[eq] += 1
    return {eq for eq, n in occ.items() if n > 1}


def _live_graph_sans(G: nx.Graph, switch_ids) -> nx.Graph:
    """Sous-graphe des switches **fermés**, en forçant l'ouverture (le retrait)
    des switches ``switch_ids`` — utilisé pour évaluer la connectivité une fois
    un sectionneur ouvert."""
    forces = set(switch_ids)
    H = nx.Graph()
    H.add_nodes_from(G.nodes())
    for u, v, d in G.edges(data=True):
        if d.get("switch_id") in forces:
            continue
        if not d.get("open", False):
            H.add_edge(u, v)
    return H


def _ouvrages_energises_sur(
    G: nx.Graph, cells, side_sjbs: set[int], H: nx.Graph
) -> list[tuple[str, list[str]]]:
    """Ouvrages **énergisant** le côté ``side_sjbs`` : ceux dont le nœud propre
    est relié, par un chemin de switches **fermés** dans ``H``, à une SJB du
    côté. ``H`` est le graphe « live » privé du sectionneur considéré, de sorte
    que la distinction des deux côtés est correcte.

    La connectivité électrique est utilisée (et non le câblage SA) pour capter
    aussi les ouvrages raccordés directement par disjoncteur sans sectionneur
    d'aiguillage (ex. côté HT d'un transformateur).

    Retourne ``[(equipment_id, [breaker_ids])]``. ``breaker_ids`` vide signale
    un ouvrage **sans DJ propre** : il ne peut être mis hors tension par
    ouverture de DJ."""
    out: list[tuple[str, list[str]]] = []
    seen: set[str] = set()
    for c in cells.cellules_depart:
        for eq in {c.equipment_id} | set(c.shared_equipment_ids):
            if eq in seen:
                continue
            cell = cells.get_cellule_depart(eq)
            if cell is None:
                continue
            en = _eq_node(G, eq)
            if en is None or en not in H:
                continue
            if not any(s in H and nx.has_path(H, en, s) for s in side_sjbs):
                continue
            seen.add(eq)
            out.append((eq, [b.switch_id for b in cell.breakers]))
    return out


def _meme_noeud_hors_cellule(
    G: nx.Graph, cell: CelluleDepart, bb1: int, bb2: int
) -> bool:
    """
    True si les deux SJB sont au **même potentiel** (même nœud électrique) par un
    chemin **n'empruntant pas** les nœuds internes de la cellule ``cell``.

    Sert à appliquer l'invariant de sécurité des sectionneurs : fermer (ou
    ouvrir) un SA d'aiguillage qui relie ``bb1`` et ``bb2`` via la cellule n'est
    sûr que si ``bb1`` et ``bb2`` sont déjà reliés *par ailleurs* (donc déjà au
    même potentiel) — sinon le départ ponterait deux nœuds distincts.
    """
    if bb1 == bb2:
        return True
    internes = {n for n in cell.all_nodes if n not in cell.busbar_nodes}
    H = nx.Graph()
    H.add_nodes_from(n for n in G.nodes() if n not in internes)
    for u, v, d in G.edges(data=True):
        if d.get("open", False) or u in internes or v in internes:
            continue
        H.add_edge(u, v)
    return bb1 in H and bb2 in H and nx.has_path(H, bb1, bb2)


@dataclass
class _InterSjbCoupler:
    """Liaison entre deux SJB : sectionnement (SA seuls) ou couplage (avec DJ)."""
    sjb_a: int
    sjb_b: int
    switch_ids: list[str]
    breaker_ids: list[str]

    @property
    def is_sectionnement(self) -> bool:
        return not self.breaker_ids


def _inter_sjb_couplers(poste: PosteTopologique) -> list[_InterSjbCoupler]:
    """
    Recense les liaisons inter-SJB (sectionnements et couplages) d'un poste,
    en contractant les nœuds intermédiaires du sous-graphe de couplage.

    **Mémoïsé sur le poste** (auparavant recalculé ~10×/analyse). Le résultat ne
    dépend que de la **topologie** (graphe + tronçonnement), pas de l'état
    ouvert/fermé des organes — propriété vérifiée par
    ``tests/manoeuvre/test_couplers_memoisation.py``. Le poste n'étant jamais
    muté structurellement, le cache reste valide toute sa durée de vie.
    """
    cached = getattr(poste, "_inter_sjb_couplers_cache", None)
    if cached is not None:
        return cached

    G = poste.graph
    bb_nodes = set(poste.tronconnement.barre_par_busbar)

    # Sous-graphe de couplage : SJB + nœuds hors cellules de départ
    departure_internal: set[int] = set()
    for c in poste.cellules.cellules_depart:
        departure_internal.update(n for n in c.all_nodes if n not in bb_nodes)
    coupler_nodes = bb_nodes | (set(G.nodes()) - departure_internal)
    coupler_G = G.subgraph(coupler_nodes)

    couplers: list[_InterSjbCoupler] = []
    bb_list = sorted(bb_nodes)
    for i, a in enumerate(bb_list):
        for b in bb_list[i + 1:]:
            # Chemins entre a et b ne traversant aucune autre SJB. On détecte
            # **tous les couplages parallèles** (ex. un couplage DJ direct ET une
            # liaison via un nœud de couplage commun à plusieurs barres) en
            # retirant itérativement les arêtes du chemin trouvé jusqu'à
            # épuisement. Sans cela, un couplage fermé masqué par un couplage
            # parallèle ouvert resterait fermé → fusion de nœuds erronée.
            others = bb_nodes - {a, b}
            H = coupler_G.subgraph(coupler_nodes - others).copy()
            while True:
                try:
                    path = nx.shortest_path(H, a, b)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    break
                sw_ids, brk_ids = [], []
                edges = list(zip(path, path[1:]))
                for u, v in edges:
                    d = H.edges[u, v]
                    sid = d.get("switch_id")
                    if sid is None:
                        continue
                    sw_ids.append(sid)
                    if d.get("kind") == SwitchKind.BREAKER:
                        brk_ids.append(sid)
                if sw_ids:
                    couplers.append(_InterSjbCoupler(a, b, sw_ids, brk_ids))
                # Retirer les arêtes du chemin pour révéler un couplage parallèle.
                H.remove_edges_from(edges)

    poste._inter_sjb_couplers_cache = couplers
    return couplers
