"""
tests/manoeuvre/test_ihm_nodale_edit.py
---------------------------------------
Tests de la **logique pure** de l'éditeur de topologie nodale de l'IHM
(``scripts/manoeuvre_ihm.py``) : normalisation d'une partition nodale éditée par
l'expert en une partition **complète et disjointe** de l'univers des départs.

Seule la fonction pure ``_normalize_groups`` est exercée (pas de Flask, de SVG
ni de pypowsybl) ; le pont nodal → détaillé (``Session.nodale_to_detaillee``)
nécessite un réseau et relève des essais manuels / d'intégration.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytest.importorskip("flask")
pytest.importorskip("pypowsybl")

_IHM_PATH = (pathlib.Path(__file__).resolve().parents[2]
             / "scripts" / "manoeuvre_ihm.py")


def _load_ihm():
    spec = importlib.util.spec_from_file_location("manoeuvre_ihm_mod", _IHM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ihm = _load_ihm()


def _as_sets(groups):
    return {frozenset(g) for g in groups}


def _assert_partition(universe, groups):
    """Vérifie que ``groups`` est une partition complète et disjointe de l'univers."""
    seen = []
    for g in groups:
        assert g, "aucun groupe vide ne doit subsister"
        seen.extend(g)
    assert sorted(seen) == sorted(universe)          # complète
    assert len(seen) == len(set(seen))               # disjointe


def test_identity_partition_preserved():
    universe = ["A", "B", "C", "D"]
    groups = [["A", "B"], ["C", "D"]]
    out = ihm._normalize_groups(universe, groups)
    _assert_partition(universe, out)
    assert _as_sets(out) == _as_sets(groups)


def test_empty_groups_removed():
    universe = ["A", "B"]
    out = ihm._normalize_groups(universe, [["A"], [], ["B"], []])
    _assert_partition(universe, out)
    assert _as_sets(out) == {frozenset(["A"]), frozenset(["B"])}


def test_duplicate_branch_last_assignment_wins():
    universe = ["A", "B"]
    # 'A' apparaît dans deux groupes : la dernière affectation l'emporte.
    out = ihm._normalize_groups(universe, [["A", "B"], ["A"]])
    _assert_partition(universe, out)
    # A doit être seul avec lui-même dans le dernier groupe, B dans le premier.
    grp_of = {eq: i for i, g in enumerate(out) for eq in g}
    assert grp_of["A"] != grp_of["B"]


def test_missing_branches_reinjected():
    universe = ["A", "B", "C"]
    # 'C' est absent de la cible éditée -> réinjecté dans un nœud dédié.
    out = ihm._normalize_groups(universe, [["A", "B"]])
    _assert_partition(universe, out)
    assert ["C"] in out


def test_unknown_branches_ignored():
    universe = ["A", "B"]
    out = ihm._normalize_groups(universe, [["A", "ZZZ"], ["B"]])
    _assert_partition(universe, out)
    assert all("ZZZ" not in g for g in out)


def test_merge_all_into_one_node():
    universe = ["A", "B", "C"]
    out = ihm._normalize_groups(universe, [["A", "B", "C"]])
    _assert_partition(universe, out)
    assert len(out) == 1


def test_full_split_one_node_per_branch():
    universe = ["A", "B", "C"]
    out = ihm._normalize_groups(universe, [["A"], ["B"], ["C"]])
    _assert_partition(universe, out)
    assert len(out) == 3


# --------------------------------------------------------------------------
# Décodage d'identifiant SVG pypowsybl
# --------------------------------------------------------------------------

def test_decode_svg_id_basic():
    # _46_ -> '.', _95_ -> '_'
    assert ihm._decode_svg_id("idC_46_REGL61VIELM_95_TWO") == "idC.REGL61VIELM_TWO"


def test_decode_svg_id_dash_and_space():
    # _45_ -> '-', _32_ -> ' '
    assert ihm._decode_svg_id("A_45_B_32_C") == "A-B C"


def test_decode_svg_id_noop():
    assert ihm._decode_svg_id("idDARCEL61VIELM") == "idDARCEL61VIELM"


# --------------------------------------------------------------------------
# Extraction des libellés / direction / abscisse des départs (parsing SVG pur)
# --------------------------------------------------------------------------

_SVG_FEEDERS = """
<g class="sld-top-feeder" id="idC_46_REGL61VIELM_95_TWO" transform="translate(665.0,80.0)">
  <text class="sld-label" id="idC_46_REGL61VIELM_95_TWO_95_N_95_LABEL" x="-5" y="-11">C.REG1  </text>
</g>
<g class="sld-bottom-feeder" id="idCOUCHL61VIELM_95_TWO" transform="translate(715.0,450.0)">
  <text class="sld-label" id="idCOUCHL61VIELM_95_TWO_95_S_95_LABEL" x="-5" y="-11">COUCH1</text>
</g>
<g class="sld-load sld-top-feeder sld-vl180to300 sld-bus-0" id="idDARCEL61VIELM" transform="translate(57.0,75.5)">
  <rect/>
  <text class="sld-label" id="idDARCEL61VIELM_95_N_95_LABEL" x="-5" y="-6">DARCEL61VIELM</text>
</g>
<text class="sld-label" id="idVIELMP6_95_1_46_1_95_NW_95_LABEL" x="0" y="0">VIELMP6_1.1</text>
"""


def test_parse_feeder_meta_line_top():
    meta = ihm._parse_feeder_meta(_SVG_FEEDERS)
    m = meta["C.REGL61VIELM_TWO"]
    assert m == {"label": "C.REG1", "dir": "TOP", "x": 665.0}


def test_parse_feeder_meta_line_bottom_uses_S_label():
    meta = ihm._parse_feeder_meta(_SVG_FEEDERS)
    m = meta["COUCHL61VIELM_TWO"]
    assert m == {"label": "COUCH1", "dir": "BOTTOM", "x": 715.0}


def test_parse_feeder_meta_load_combined_class():
    # La classe sld-top-feeder est combinée avec sld-load : doit être captée.
    meta = ihm._parse_feeder_meta(_SVG_FEEDERS)
    m = meta["DARCEL61VIELM"]
    assert m["label"] == "DARCEL61VIELM" and m["dir"] == "TOP" and m["x"] == 57.0


def test_parse_feeder_meta_excludes_busbar_label():
    # Le libellé de barre (_NW_LABEL) n'est pas un départ -> absent.
    meta = ihm._parse_feeder_meta(_SVG_FEEDERS)
    assert not any("VIELMP6_1.1" == v["label"] for v in meta.values())


# --------------------------------------------------------------------------
# Extraction des couleurs de nœud (topological_coloring) — parsing SVG pur
# --------------------------------------------------------------------------

_SVG_COLORS = """
<style>
.sld-vl180to300.sld-bus-0 {--sld-vl-color: #218B21}
.sld-vl180to300.sld-bus-1 {--sld-vl-color: #2563EB}
.sld-vl70to120.sld-bus-0 {--sld-vl-color: #CC5500}
</style>
<g class="sld-load sld-top-feeder sld-vl180to300 sld-bus-0" id="idDARCEL61VIELM" transform="translate(1,1)"><rect/></g>
<g class="sld-node sld-fictitious sld-vl180to300 sld-bus-1" id="idINTERNAL_95_VIELMP6_95_C_46_REGL61VIELM_95_TWO"><circle/></g>
<g class="sld-breaker" id="idSOMESWITCH"><rect/></g>
"""


def test_parse_node_colors_resolves_per_element():
    colors = ihm._parse_node_colors(_SVG_COLORS)
    assert colors["DARCEL61VIELM"] == "#218B21"
    assert colors["INTERNAL_VIELMP6_C.REGL61VIELM_TWO"] == "#2563EB"


def test_parse_node_colors_ignores_elements_without_bus_class():
    colors = ihm._parse_node_colors(_SVG_COLORS)
    # L'élément sans sld-vl/sld-bus (le breaker) n'a pas de couleur.
    assert "SOMESWITCH" not in colors


def test_parse_node_colors_palette_scoped_by_voltage_class():
    # bus-0 existe dans deux classes de tension : la bonne palette est choisie.
    colors = ihm._parse_node_colors(_SVG_COLORS)
    assert colors["DARCEL61VIELM"] == "#218B21"          # vl180to300, pas #CC5500


# --------------------------------------------------------------------------
# Détection des ouvrages isolés (déconnectés) — graphe NX pur
# --------------------------------------------------------------------------

def _graph_iso_fixture():
    import networkx as nx
    from expert_op4grid_recommender.manoeuvre.models import NodeType
    G = nx.Graph()
    G.add_node(0, node_type=NodeType.BUSBAR_SECTION)
    G.add_node(1, node_type=NodeType.EQUIPMENT, equipment_id="L1")  # connecté
    G.add_node(2, node_type=NodeType.EQUIPMENT, equipment_id="L2")  # switch ouvert
    G.add_node(3, node_type=NodeType.EQUIPMENT, equipment_id="L3")  # flottant
    G.add_edge(0, 1, open=False, switch_id="S1")
    G.add_edge(0, 2, open=True, switch_id="S2")
    return G


def test_isolated_assets_detects_open_and_floating():
    G = _graph_iso_fixture()
    assert set(ihm._isolated_assets(G)) == {"L2", "L3"}


def test_isolated_assets_none_when_all_connected():
    import networkx as nx
    from expert_op4grid_recommender.manoeuvre.models import NodeType
    G = nx.Graph()
    G.add_node(0, node_type=NodeType.BUSBAR_SECTION)
    G.add_node(1, node_type=NodeType.EQUIPMENT, equipment_id="L1")
    G.add_edge(0, 1, open=False, switch_id="S1")
    assert ihm._isolated_assets(G) == []


def test_isolated_assets_all_when_busbar_isolated():
    # Tout switch ouvert -> aucun équipement n'atteint la barre.
    G = _graph_iso_fixture()
    G.edges[0, 1]["open"] = True
    assert set(ihm._isolated_assets(G)) == {"L1", "L2", "L3"}


# --------------------------------------------------------------------------
# Restitution iso-aware : décomposition vrais nœuds vs ouvrages isolés
# (_decompose_realisation, _fmt_noeuds_isoles) — logique pure
# --------------------------------------------------------------------------

def test_decompose_identity_verified():
    # Mêmes nœuds, aucun isolé attendu ni obtenu -> vérifié.
    dec = ihm._decompose_realisation([["A", "B", "C"]], [], [["A", "B", "C"]], [])
    assert dec["is_verified"] is True
    assert dec["noeuds_ok"] is True and dec["isoles_ok"] is True
    assert dec["nb_noeuds_reels"] == 1 and dec["nb_isoles"] == 0


def test_decompose_two_nodes_two_isolated_verified():
    # Le cœur du correctif : 2 nœuds + 2 ouvrages isolés réalisés == cible.
    # Ne doit PAS se restituer « 4 nœuds » (ordre des groupes/isolés indifférent).
    dec = ihm._decompose_realisation(
        noeuds_obtenus=[["A", "B"], ["C", "D"]], isoles_obtenus=["Y", "X"],
        noeuds_attendus=[["C", "D"], ["A", "B"]], isoles_attendus=["X", "Y"])
    assert dec["is_verified"] is True
    assert dec["nb_noeuds_reels"] == 2 and dec["nb_isoles"] == 2
    assert dec["nb_noeuds_vises"] == 2 and dec["nb_isoles_vises"] == 2
    assert dec["isoles_obtenus"] == ["X", "Y"]      # trié
    assert dec["isoles_manquants"] == [] and dec["isoles_inattendus"] == []


def test_decompose_real_node_count_excludes_isolated_singletons():
    # Les ouvrages isolés présentés comme singletons ne sont pas comptés en nœuds.
    dec = ihm._decompose_realisation(
        noeuds_obtenus=[["A", "B"], ["X"], ["Y"]], isoles_obtenus=["X", "Y"],
        noeuds_attendus=[["A", "B"]], isoles_attendus=["X", "Y"])
    assert dec["nb_noeuds_reels"] == 1       # {A,B} ; X et Y sont des isolés
    assert dec["nb_isoles"] == 2
    assert dec["is_verified"] is True


def test_decompose_isolated_mismatch_flags_missing_and_unexpected():
    # X attendu isolé mais raccordé ; Z déconnecté sans l'avoir prévu.
    dec = ihm._decompose_realisation(
        noeuds_obtenus=[["A", "B", "X"]], isoles_obtenus=["Z"],
        noeuds_attendus=[["A", "B"]], isoles_attendus=["X"])
    assert dec["isoles_ok"] is False and dec["is_verified"] is False
    assert dec["isoles_manquants"] == ["X"]
    assert dec["isoles_inattendus"] == ["Z"]


def test_decompose_node_partition_mismatch():
    # Mêmes ouvrages, mais répartis différemment entre nœuds.
    dec = ihm._decompose_realisation(
        noeuds_obtenus=[["A"], ["B", "C"]], isoles_obtenus=[],
        noeuds_attendus=[["A", "B"], ["C"]], isoles_attendus=[])
    assert dec["noeuds_ok"] is False and dec["is_verified"] is False


def test_fmt_noeuds_isoles():
    assert ihm._fmt_noeuds_isoles(2, 0) == "2 nœud(s)"
    assert ihm._fmt_noeuds_isoles(2, 2) == "2 nœud(s) + 2 ouvrage(s) isolé(s)"
    assert ihm._fmt_noeuds_isoles(0, 3) == "0 nœud(s) + 3 ouvrage(s) isolé(s)"
