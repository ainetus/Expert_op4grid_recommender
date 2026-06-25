"""
manoeuvre/dataset/tagging.py — Tagging du **type d'intervention** d'un bloc de
transition (phase 3 du plan ``docs/manoeuvre/dataset_rte7000/plan.md``).

Taxonomie (multi-label) :

| Tag | Signature |
|---|---|
| ``consignation_ouvrage``   | ≥ 1 départ devient **isolé** (nœud 0-barre)    |
| ``remise_en_service``      | ≥ 1 départ isolé est **reconnecté**            |
| ``scission_noeud``         | le nb de nœuds électriques **barrés** augmente |
| ``fusion_noeuds``          | il diminue                                     |
| ``reaiguillage_departs``   | la barre câblée d'un départ change             |
| ``sectionnement_barre``    | un sectionnement inter-SJB bascule             |
| ``reconfiguration_durable``| plateau cible ≥ ``seuil_durable`` snapshots    |
| ``inclasse``               | aucun des cas ci-dessus                        |

Deux régimes :

- **structurel** (un ``PosteTopologique`` est fourni) : les signatures sont
  calculées sur les graphes de départ/cible (connexité, barres câblées,
  couplers inter-SJB) — fiable ;
- **par nommage** (pas de structure) : repli heuristique sur les conventions
  d'identifiants RTE (``COUPL``/``TRO.`` = couplage ; `` DJ`` / `` SA`` /
  ``SS``/``SL`` = genre d'organe), précision moindre — tags grossiers.

Les épisodes ``A → bruit → A`` sont repliés en **oscillations** au niveau de la
chronologie (``timeline``) et n'arrivent jamais ici.
"""
from __future__ import annotations

from typing import Iterable, Optional

import networkx as nx

from ..graph import busbar_nodes, equipment_nodes
from ..topologie import PosteTopologique
from ..algo.graph_ops import _inter_sjb_couplers, _wired_busbar
from ..plugins import CibleDetaillee
from .timeline import BlocTransition

#: taxonomie complète (référence pour les stats et la validation)
TAGS = (
    "consignation_ouvrage", "remise_en_service",
    "scission_noeud", "fusion_noeuds",
    "reaiguillage_departs", "sectionnement_barre",
    "reconfiguration_durable", "inclasse",
)


# ---------------------------------------------------------------------------
# Signatures structurelles
# ---------------------------------------------------------------------------

def _composantes_fermees(G: nx.Graph) -> list[set]:
    """Composantes connexes du sous-graphe des switches fermés."""
    H = nx.Graph()
    H.add_nodes_from(G.nodes())
    for u, v, d in G.edges(data=True):
        if not d.get("open", False):
            H.add_edge(u, v)
    return [set(c) for c in nx.connected_components(H)]


def _departs_isoles(G: nx.Graph) -> set[str]:
    """Départs dont la composante (switches fermés) ne contient aucune barre."""
    barres = set(busbar_nodes(G))
    eqs = set(equipment_nodes(G))
    iso: set[str] = set()
    for comp in _composantes_fermees(G):
        if comp & barres:
            continue
        for n in comp & eqs:
            eq = G.nodes[n].get("equipment_id")
            if eq:
                iso.add(eq)
    return iso


def _nb_noeuds_barres(G: nx.Graph) -> int:
    """Nœuds électriques **barrés** : composantes contenant au moins une barre
    et au moins un départ (les départs isolés ne comptent pas)."""
    barres = set(busbar_nodes(G))
    eqs = set(equipment_nodes(G))
    return sum(1 for comp in _composantes_fermees(G)
               if comp & barres and comp & eqs)


def taguer_bloc(
    bloc: BlocTransition,
    poste: Optional[PosteTopologique] = None,
    seuil_durable: Optional[int] = None,
) -> list[str]:
    """Tags d'intervention d'un bloc (mute ``bloc.tags`` et les retourne).

    ``poste`` : structure du poste (régime structurel) ; sans elle, repli par
    nommage des organes. ``seuil_durable`` : si fourni, un plateau cible d'au
    moins ce nombre de snapshots ajoute ``reconfiguration_durable``.
    """
    diff = bloc.diff()
    tags: list[str] = []

    if poste is not None:
        Gd = CibleDetaillee(bloc.voltage_level_id, bloc.etats_depart).to_graph(poste)
        Gc = CibleDetaillee(bloc.voltage_level_id, bloc.etats_cible).to_graph(poste)

        iso_d, iso_c = _departs_isoles(Gd), _departs_isoles(Gc)
        if iso_c - iso_d:
            tags.append("consignation_ouvrage")
        if iso_d - iso_c:
            tags.append("remise_en_service")

        nd, nc = _nb_noeuds_barres(Gd), _nb_noeuds_barres(Gc)
        if nc > nd:
            tags.append("scission_noeud")
        elif nc < nd:
            tags.append("fusion_noeuds")

        sect_ids = {sid for cp in _inter_sjb_couplers(poste)
                    if cp.is_sectionnement for sid in cp.switch_ids}
        if any(sid in sect_ids for sid in diff):
            tags.append("sectionnement_barre")

        for cell in poste.cellules.cellules_depart:
            bd, bc = _wired_busbar(cell, Gd), _wired_busbar(cell, Gc)
            if bd is not None and bc is not None and bd != bc:
                tags.append("reaiguillage_departs")
                break
    else:
        tags.extend(_taguer_par_nommage(diff))

    if seuil_durable is not None and bloc.duree_stable_apres >= seuil_durable:
        tags.append("reconfiguration_durable")
    if not tags:
        tags.append("inclasse")

    bloc.tags = tags
    return tags


def taguer_blocs(
    blocs: Iterable[BlocTransition],
    postes: Optional[dict[str, PosteTopologique]] = None,
    seuil_durable: Optional[int] = None,
) -> list[BlocTransition]:
    """Tague une collection de blocs ; ``postes`` mappe ``vl_id`` → structure
    (les postes absents passent en régime « par nommage »)."""
    postes = postes or {}
    for b in blocs:
        taguer_bloc(b, postes.get(b.voltage_level_id), seuil_durable)
    return list(blocs)


# ---------------------------------------------------------------------------
# Repli heuristique par nommage (sans structure)
# ---------------------------------------------------------------------------

def _taguer_par_nommage(diff: dict) -> list[str]:
    """Heuristique sur les conventions d'identifiants RTE (précision moindre,
    documentée comme telle dans le dataset) :

    - ``COUPL``/``TRO.`` dans l'id = organe de **couplage/tronçonnement** :
      son ouverture suggère une scission, sa fermeture une fusion ;
    - un départ dont **DJ et SA** s'ouvrent ensemble suggère une consignation
      (l'inverse une remise en service) — approché ici par la co-occurrence
      d'ouvertures (resp. fermetures) d'organes `` DJ`` et `` SA`` partageant
      le même préfixe d'ouvrage.
    """
    tags: list[str] = []
    coupl_open = coupl_close = False
    prefixes_ouv: dict[str, set[str]] = {}
    prefixes_ferm: dict[str, set[str]] = {}

    def _genre(sid: str) -> Optional[str]:
        if " DJ" in sid:
            return "DJ"
        if " SA" in sid or " SL" in sid or " SS" in sid:
            return "SA"
        return None

    def _prefixe(sid: str) -> str:
        return sid.split(" ")[0]

    for sid, (_a, b) in diff.items():
        est_coupl = "COUPL" in sid or "TRO." in sid
        if est_coupl:
            coupl_open |= bool(b)
            coupl_close |= not b
            continue
        g = _genre(sid)
        if g is None:
            continue
        cible = prefixes_ouv if b else prefixes_ferm
        cible.setdefault(_prefixe(sid), set()).add(g)

    if coupl_open:
        tags.append("scission_noeud")
    if coupl_close:
        tags.append("fusion_noeuds")
    if any(g >= {"DJ", "SA"} for g in prefixes_ouv.values()):
        tags.append("consignation_ouvrage")
    if any(g >= {"DJ", "SA"} for g in prefixes_ferm.values()):
        tags.append("remise_en_service")
    return tags
