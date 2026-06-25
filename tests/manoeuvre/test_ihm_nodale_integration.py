"""
tests/manoeuvre/test_ihm_nodale_integration.py
----------------------------------------------
Tests d'**intégration** de la vue nodale de l'IHM (``scripts/manoeuvre_ihm.py``)
sur le réseau node-breaker standard ``create_four_substations_node_breaker_network``
(cible ``S1VL2``). Exercent ``Session.nodale_payload`` / ``nodale_state`` /
``nodale_to_detaillee`` et la cohérence détaillé ↔ nodal (édition, isolés,
rechargement de scénario) face au **vrai** SVG/graphe pypowsybl.

Nécessitent ``flask`` et ``pypowsybl`` (sinon test ignoré).
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytest.importorskip("flask")
pytest.importorskip("pypowsybl")

import pypowsybl as pp  # noqa: E402

_IHM_PATH = (pathlib.Path(__file__).resolve().parents[2]
             / "scripts" / "manoeuvre_ihm.py")
VL = "S1VL2"


def _load_ihm():
    spec = importlib.util.spec_from_file_location("manoeuvre_ihm_mod", _IHM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ihm = _load_ihm()


@pytest.fixture()
def session():
    net = pp.network.create_four_substations_node_breaker_network()
    s = ihm.Session(net)
    s.load(VL)
    return s


def _feeders(s):
    return {eq for g in s.groups_of(s.initial) for eq in g}


# --------------------------------------------------------------------------
# nodale_payload : structure et invariants
# --------------------------------------------------------------------------

def test_nodale_payload_keys(session):
    p = session.nodale_payload(session.initial)
    for k in ("groups", "labels", "types", "flows", "dirs", "order",
              "colors", "isolated"):
        assert k in p


def test_nodale_payload_branch_metadata(session):
    p = session.nodale_payload(session.initial)
    feeders = _feeders(session)
    assert feeders, "le poste de test doit avoir des départs"
    for eq in feeders:
        assert p["labels"].get(eq)                 # libellé présent
        assert p["dirs"].get(eq) in ("TOP", "BOTTOM")
        assert eq in p["colors"]                   # couleur résolue
        assert isinstance(p["order"].get(eq), float)


def test_nodale_payload_partition_covers_all_feeders(session):
    p = session.nodale_payload(session.initial)
    flat = {eq for g in p["groups"] for eq in g}
    assert flat == _feeders(session)


def test_nodale_payload_same_node_same_color(session):
    # S1VL2 pristine = un seul nœud électrique -> couleur unique.
    p = session.nodale_payload(session.initial)
    cols = {c for c in p["colors"].values() if c}
    assert len(p["groups"]) == 1
    assert len(cols) == 1


# --------------------------------------------------------------------------
# nodale_state : partition + couleurs + isolés (sans flux)
# --------------------------------------------------------------------------

def test_nodale_state_matches_payload_partition(session):
    payload = session.nodale_payload(session.initial)
    state = session.nodale_state(session.initial)
    assert {frozenset(g) for g in state["groups"]} == \
           {frozenset(g) for g in payload["groups"]}
    assert state["isolated"] == []
    assert set(state["colors"]) == _feeders(session)


# --------------------------------------------------------------------------
# Détection des ouvrages isolés (déconnectés) sur le vrai graphe
# --------------------------------------------------------------------------

def test_isolated_empty_on_pristine(session):
    assert session.nodale_state(session.initial)["isolated"] == []


def test_isolated_all_when_every_switch_open(session):
    feeders = _feeders(session)
    for sid in list(session.switches_df(VL).index):
        session.current[sid] = True            # tout ouvrir
    state = session.nodale_state(session.current)
    assert set(state["isolated"]) == feeders    # plus aucun raccordé à une barre


# --------------------------------------------------------------------------
# nodale_to_detaillee : pont nodal -> détaillé
# --------------------------------------------------------------------------

def test_to_detaillee_identity_is_verified(session):
    p = session.nodale_payload(session.initial)
    res = session.nodale_to_detaillee(p["groups"], p["isolated"])
    assert res["is_verified"] is True
    assert "nodale" in res
    for k in ("groups", "colors", "isolated"):
        assert k in res["nodale"]


def test_to_detaillee_returns_realised_nodale(session):
    p = session.nodale_payload(session.initial)
    res = session.nodale_to_detaillee(p["groups"], p["isolated"])
    realised = {eq for g in res["nodale"]["groups"] for eq in g}
    assert realised == _feeders(session)


def test_to_detaillee_isolated_kept_out_of_target(session):
    # Un départ déclaré isolé ne doit pas être placé sur un nœud cible.
    feeders = sorted(_feeders(session))
    iso = [feeders[0]]
    rest = feeders[1:]
    res = session.nodale_to_detaillee([rest], iso)
    assert res["nb_vise"] == 1            # une seule barre visée (sans l'isolé)


# --------------------------------------------------------------------------
# Restitution iso-aware : décomposition vrais nœuds vs ouvrages isolés
# (sont-ils ceux attendus ?) — le verdict ne doit plus conflater les deux.
# --------------------------------------------------------------------------

def test_to_detaillee_decomposition_fields_present(session):
    p = session.nodale_payload(session.initial)
    res = session.nodale_to_detaillee(p["groups"], p["isolated"])
    for k in ("nb_noeuds_reels", "nb_isoles", "nb_noeuds_vises",
              "nb_isoles_vises", "noeuds_ok", "isoles_ok", "isoles_obtenus",
              "isoles_attendus", "isoles_manquants", "isoles_inattendus",
              "algo_message"):
        assert k in res


def test_to_detaillee_identity_decomposes_as_one_node_no_isolated(session):
    # Identité : 1 nœud, 0 isolé, vérifié — message décomposé (sans « obtenu N »).
    p = session.nodale_payload(session.initial)
    res = session.nodale_to_detaillee(p["groups"], p["isolated"])
    assert res["is_verified"] is True
    assert res["nb_noeuds_reels"] == 1 and res["nb_isoles"] == 0
    assert "ouvrage(s) isolé(s)" not in res["message"]   # part isolés omise si nulle


def test_to_detaillee_isoles_obtenus_matches_realised_state(session):
    # Les isolés restitués == les isolés de l'état détaillé réalisé.
    p = session.nodale_payload(session.initial)
    res = session.nodale_to_detaillee(p["groups"], p["isolated"])
    assert res["isoles_obtenus"] == sorted(res["nodale"]["isolated"])
    # Décomposition cohérente : nb_noeuds_reels = nœuds réels (isolés exclus).
    iso = set(res["nodale"]["isolated"])
    reels = [[e for e in g if e not in iso] for g in res["nodale"]["groups"]]
    assert res["nb_noeuds_reels"] == len([g for g in reels if g])


def test_to_detaillee_unrealizable_isolation_flags_missing(session):
    # Demander un départ isolé que le réseau ne peut pas déconnecter :
    # restitution honnête (isoles_ok False + ouvrage signalé « manquant »),
    # et la décomposition compte le nœud réel sans l'isolé non obtenu.
    feeders = sorted(_feeders(session))
    iso = [feeders[0]]
    res = session.nodale_to_detaillee([feeders[1:]], iso)
    assert res["nb_isoles_vises"] == 1
    if not res["isoles_ok"]:                  # cas attendu sur ce réseau
        assert feeders[0] in res["isoles_manquants"]
        assert res["is_verified"] is False
        assert "ouvrage(s) isolé(s)" in res["message"]


def test_to_detaillee_all_isolated_not_counted_as_nodes(session):
    # État réalisé = tout déconnecté : la décomposition ne doit PAS restituer
    # « N nœuds » (un par départ), mais « 0 nœud + N ouvrages isolés ».
    feeders = _feeders(session)
    session.current = {sid: True for sid in session.initial}
    state = session.nodale_state(session.current)
    iso_obt = set(state["isolated"])
    noeuds_obt = [[e for e in g if e not in iso_obt] for g in state["groups"]]
    dec = ihm._decompose_realisation(noeuds_obt, iso_obt, [], list(feeders))
    assert dec["nb_noeuds_reels"] == 0
    assert dec["nb_isoles"] == len(feeders)
    assert dec["is_verified"] is True        # cible = tout isolé, atteinte


# --------------------------------------------------------------------------
# Cohérence au rechargement de scénario (fix : nodal suit la cible détaillée)
# --------------------------------------------------------------------------

def test_api_cible_returns_current_view_without_mutating(monkeypatch):
    # /api/cible : revenir éditer la cible (sans la modifier) alors qu'une
    # séquence est calculée.
    net = pp.network.create_four_substations_node_breaker_network()
    s = ihm.Session(net)
    s.load(VL)
    monkeypatch.setattr(ihm, "SESSION", s)
    client = ihm.app.test_client()
    r = client.post("/api/cible", json={})
    assert r.status_code == 200
    d = r.get_json()
    assert {"svg", "switches", "nb_noeuds", "nodale"} <= set(d)
    assert "groups" in d["nodale"]
    assert s.current == s.initial          # l'état cible n'est pas modifié


def test_scenario_reload_syncs_nodale(tmp_path, monkeypatch):
    monkeypatch.setattr(ihm, "SCEN_DIR", tmp_path)
    net = pp.network.create_four_substations_node_breaker_network()
    s = ihm.Session(net)
    s.load(VL)
    feeders = _feeders(s)

    # Cible = tout déconnecté (tous les ouvrages isolés).
    s.current = {sid: True for sid in s.initial}
    s.save_scenario("sc")

    # Retour à l'état de départ : plus aucun isolé.
    s.load(VL)
    assert s.nodale_state(s.current)["isolated"] == []

    # Rejouer (mode "both") : la cible détaillée chargée est reflétée côté nodal.
    s.load_scenario("sc", "both")
    assert set(s.nodale_state(s.current)["isolated"]) == feeders
