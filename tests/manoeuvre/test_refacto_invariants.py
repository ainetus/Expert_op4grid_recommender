"""
tests/manoeuvre/test_refacto_invariants.py
--------------------------------------------
Couverture des **invariants introduits par la campagne d'optimisation**
(cf. ``docs/manoeuvre/optimisations.md``). Complète les filets T1–T4 en pinnant
directement, au niveau unitaire, les propriétés sur lesquelles repose
l'iso-comportement des refactors :

- **P1+** : ``_assignations_connexes`` énumère **exactement** le même ensemble
  d'affectations que ``itertools.product`` filtré sur « groupes non vides et
  connexes » — c'est ce qui garantit le même coût minimal (donc, avec le
  tie-break lex-min, la même sortie).
- **Q1** : ``disconnectors_vers_barre`` est mémoïsé et **structurel** (invariant
  à l'état des organes) ; ``_edges_of_switches`` passe par l'index.
- **#3** : ``_verifier_regles`` est exactement la concaténation des
  vérificateurs individuels.
"""

from __future__ import annotations

import itertools

import networkx as nx
import pytest

from expert_op4grid_recommender.manoeuvre.topologie import (
    PosteTopologique,
    TopologieNodale,
)
from expert_op4grid_recommender.manoeuvre.algo import (
    _assignations_connexes,
    _edges_of_switches,
    _verifier_regles,
    _verifier_securite_sectionneurs,
    _verifier_un_seul_hors_tension,
    _verifier_sectionneurs_hors_charge,
    determiner_topo_complete_cible,
)

from .fixture_loader import build_graph_from_fixture, list_available_fixtures

VL = "CARRIP3"


# ---------------------------------------------------------------------------
# P1+ — équivalence du générateur de partitions connexes
# ---------------------------------------------------------------------------

def _product_filtre(sjb_nodes, k, CG):
    """Référence : affectations de ``product`` à k groupes **tous non vides et
    connexes** (l'ensemble que l'ancien code parcourait avant filtrage)."""
    ref = set()
    for assign in itertools.product(range(k), repeat=len(sjb_nodes)):
        groups: dict[int, set[int]] = {}
        for j, ni in enumerate(assign):
            groups.setdefault(ni, set()).add(sjb_nodes[j])
        if len(groups) != k:
            continue  # au moins un nœud vide
        if all(nx.is_connected(CG.subgraph(g)) for g in groups.values()):
            ref.add(assign)
    return ref


@pytest.mark.parametrize("edges", [
    [(0, 1), (1, 2), (2, 3)],            # barre à 4 sections (chaîne)
    [(0, 1), (2, 3), (0, 2)],            # 2 barres × 2 sections + couplage
    [(0, 1), (1, 2), (0, 2), (2, 3)],    # cycle + antenne
])
def test_assignations_connexes_equivaut_au_filtre_product(edges):
    """``_assignations_connexes`` génère **exactement** l'ensemble des
    affectations à groupes connexes (ni plus, ni moins, sans doublon)."""
    sjb = sorted({n for e in edges for n in e})
    CG = nx.Graph()
    CG.add_nodes_from(sjb)
    CG.add_edges_from(edges)

    def conn(fs):
        return bool(fs) and nx.is_connected(CG.subgraph(fs))

    for k in range(1, len(sjb) + 1):
        produit = list(_assignations_connexes(sjb, k, conn))
        assert len(produit) == len(set(produit)), f"k={k}: doublons générés"
        assert set(produit) == _product_filtre(sjb, k, CG), \
            f"k={k}: ensemble de candidats différent de product filtré"


# ---------------------------------------------------------------------------
# Q1 — mémoïsation des chemins SA (structurels, invariants à l'état)
# ---------------------------------------------------------------------------

pytestmark_carrip3 = pytest.mark.skipif(
    VL not in list_available_fixtures(), reason="Fixture CARRIP3 absente.")


@pytestmark_carrip3
def test_disconnectors_vers_barre_memoise():
    """Deux appels renvoient le **même objet** (mémoïsation effective)."""
    poste = PosteTopologique.from_graph(build_graph_from_fixture(VL), VL)
    cell = next(c for c in poste.cellules.cellules_depart if c.busbar_nodes)
    bb = next(iter(cell.busbar_nodes))
    assert cell.disconnectors_vers_barre(bb) is cell.disconnectors_vers_barre(bb)


@pytestmark_carrip3
def test_disconnectors_vers_barre_invariant_a_l_etat():
    """Le chemin SA (switch_ids) est **structurel** : inverser l'état de tous
    les organes ne le change pas."""
    poste = PosteTopologique.from_graph(build_graph_from_fixture(VL), VL)
    cell = next(c for c in poste.cellules.cellules_depart if c.busbar_nodes)
    bb = next(iter(cell.busbar_nodes))
    ref = [s.switch_id for s in cell.disconnectors_vers_barre(bb)]

    G2 = build_graph_from_fixture(VL)
    for _u, _v, d in G2.edges(data=True):
        if d.get("switch_id"):
            d["open"] = not d.get("open", False)
    poste2 = PosteTopologique.from_graph(G2, VL)
    cell2 = poste2.cellules.get_cellule_depart(cell.equipment_id)
    # même nœud SJB (les ids de nœuds sont stables entre deux constructions)
    got = [s.switch_id for s in cell2.disconnectors_vers_barre(bb)]
    assert got == ref


@pytestmark_carrip3
def test_edges_of_switches_via_index():
    """``_edges_of_switches`` rend les bonnes arêtes (par l'index) et **omet**
    les ids inconnus."""
    G = build_graph_from_fixture(VL)
    sids = [d["switch_id"] for _u, _v, d in G.edges(data=True)
            if d.get("switch_id")][:5]
    edges = _edges_of_switches(G, sids)
    assert {G.edges[u, v]["switch_id"] for u, v in edges} == set(sids)
    assert _edges_of_switches(G, ["ORGANE_INEXISTANT"]) == []


# ---------------------------------------------------------------------------
# #3 — _verifier_regles == concaténation des vérificateurs individuels
# ---------------------------------------------------------------------------

@pytestmark_carrip3
def test_verifier_regles_egale_somme_des_verificateurs():
    """L'agrégateur à passe unique reproduit exactement la concaténation
    historique : sécurité + [un seul HS] + hors charge."""
    G = build_graph_from_fixture(VL)
    # Couplage ouvert -> ≥ 2 nœuds ; cible = fusion en 1 nœud (séquence riche).
    for _u, _v, d in G.edges(data=True):
        if d.get("switch_id") == "CARRIP3_CARRI3COUPL.1 DJ_OC":
            d["open"] = True
    poste = PosteTopologique.from_graph(G, VL)
    connectes, isoles = [], []
    for noeud in poste.topologie_nodale.noeuds.values():
        ids = sorted(noeud.equipment_ids)
        (connectes if len(ids) > 1 else isoles).append(ids)
    cible = TopologieNodale.from_node_groups(VL, [sorted(sum(connectes, []))] + isoles)
    manos = determiner_topo_complete_cible(poste, cible).manoeuvres

    attendu_avec = (_verifier_securite_sectionneurs(poste, manos)
                    + _verifier_un_seul_hors_tension(poste, manos)
                    + _verifier_sectionneurs_hors_charge(poste, manos))
    assert _verifier_regles(poste, manos, un_seul=True) == attendu_avec

    attendu_sans = (_verifier_securite_sectionneurs(poste, manos)
                    + _verifier_sectionneurs_hors_charge(poste, manos))
    assert _verifier_regles(poste, manos, un_seul=False) == attendu_sans
