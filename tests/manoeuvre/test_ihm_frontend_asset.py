"""
tests/manoeuvre/test_ihm_frontend_asset.py
------------------------------------------
Vérifie l'**externalisation du front-end** de l'IHM (#8) : le HTML/CSS/JS vit
désormais dans ``scripts/manoeuvre_ihm_assets/index.html`` et est servi tel quel
par la route ``/``. Garde-fou contre un asset manquant / un chargement cassé.

Nécessite ``flask`` et ``pypowsybl`` (sinon test ignoré).
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

pytest.importorskip("flask")
pytest.importorskip("pypowsybl")

import pypowsybl as pp  # noqa: E402

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_IHM_PATH = _ROOT / "scripts" / "manoeuvre_ihm.py"
_ASSET = _ROOT / "scripts" / "manoeuvre_ihm_assets" / "index.html"
VL = "S1VL2"


def _load_ihm():
    spec = importlib.util.spec_from_file_location("manoeuvre_ihm_front_mod", _IHM_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ihm = _load_ihm()


def test_asset_file_exists_and_is_html():
    assert _ASSET.is_file(), "asset front-end absent"
    txt = _ASSET.read_text(encoding="utf-8")
    assert txt.startswith("<!DOCTYPE html>")
    assert "</html>" in txt


def test_page_constant_loaded_from_asset():
    # PAGE est chargé depuis le fichier (pas de HTML embarqué dans le .py).
    assert ihm.PAGE == _ASSET.read_text(encoding="utf-8")


def test_index_route_serves_asset():
    ihm.SESSION = ihm.Session(pp.network.create_four_substations_node_breaker_network())
    ihm.SESSION.load(VL)
    client = ihm.app.test_client()
    r = client.get("/")
    assert r.status_code == 200
    assert r.mimetype == "text/html"
    assert r.get_data(as_text=True) == _ASSET.read_text(encoding="utf-8")


def test_python_source_no_longer_embeds_full_page():
    # Le .py ne doit plus contenir le gros bloc HTML (uniquement le loader).
    src = _IHM_PATH.read_text(encoding="utf-8")
    assert 'PAGE = r"""' not in src
    assert "manoeuvre_ihm_assets" in src


# ---------------------------------------------------------------------------
# Garde-fou de **structure** : verrouille la présence des éléments / handlers
# de la refonte IHM (onglets, poste unifié, Scénario Topologique, promote,
# Recharger, Valider/Sauvegarder, sections nodales repliables…) et l'**absence**
# des éléments retirés/renommés. Un retrait accidentel casse la CI tout de suite.
# ---------------------------------------------------------------------------

#: marqueurs qui DOIVENT être présents dans l'asset servi
REQUIRED_MARKERS = [
    # Situation réseau : onglets + bouton unique + sélecteur de fichier
    'id="tabLocal"', 'id="tabRte"', 'id="panelLocal"', 'id="panelRte"',
    'id="btnCharger"', 'onclick="chargerSituation()"', 'onclick="pickGridFile()"',
    'function selectTab', '/api/pick_grid_file',
    # Champ poste unifié + titre + liste browsable + pas d'auto-chargement
    'id="posteSearch"', 'id="posteList"', 'id="posteListTitle"',
    "Pré-sélection de postes typiques", 'function renderPosteList',
    'function awaitPosteChoice',
    # Scénario Topologique en 3 étapes
    "🗺 Scénario Topologique", "1 · Poste", "2 · Topologie cible",
    "3 · Séquence de manœuvres",
    # Recharger (modale)
    'onclick="openReload()"', 'id="reloadModal"', 'id="scenSel"',
    'function validateReload',
    # Valider / Sauvegarder séparés + téléchargement local
    'onclick="validateCible()"', "💾 Sauvegarder", 'function maybeDownload',
    # Boutons en en-tête de schéma : reset + promote cible→départ
    "↺ État d'origine", "⇧ Nouvelle Topologie Départ", 'onclick="promoteCible()"',
    '/api/promote_cible',
    # Volet nodal : sections repliables + Réinitialiser + isolés (conteneurs)
    'onclick="toggleNsec(this)"', 'function toggleNsec', "↺ Réinitialiser",
    "＋ Nœud", 'id="ndDepartIso"', 'id="ndCibleIso"',
    # Restitution iso-aware : décomposition vrais nœuds vs ouvrages isolés
    'function nodComputeMsg', 'function decFmt', 'd.nb_noeuds_reels',
    "ouvrage(s) isolé(s)",
    # Dates d'accès rapide 2021-2023
    "'2022-06-15'", "'2023-02-08'",
]

#: marqueurs qui ne DOIVENT PLUS apparaître (éléments retirés / renommés)
FORBIDDEN_MARKERS = [
    'id="dsBox"',                 # ancien encart dataset → onglets
    'id="poste"',                 # ancien menu épinglé → recherche unifiée
    'id="posteCat"',              # ancien sélecteur par typologie → posteList
    "<datalist",                  # ancienne datalist → posteList
    ">= départ<",                 # reset nodal renommé « ↺ Réinitialiser »
    "Scénarios sauvegardés",      # section → modale Recharger
    "Nœuds électriques",          # section redondante retirée
    "loadScen('as_depart')",      # bouton recâblé sur promoteCible()
    "Valider &amp; sauvegarder",  # scindé en Valider + Sauvegarder
    "load(r.postes[0])",          # auto-chargement de poste supprimé
]


def test_frontend_structure_required_markers():
    txt = _ASSET.read_text(encoding="utf-8")
    missing = [m for m in REQUIRED_MARKERS if m not in txt]
    assert not missing, f"marqueurs IHM attendus absents : {missing}"


def test_frontend_structure_removed_markers_absent():
    txt = _ASSET.read_text(encoding="utf-8")
    present = [m for m in FORBIDDEN_MARKERS if m in txt]
    assert not present, f"marqueurs retirés encore présents : {present}"


def test_frontend_script_block_balanced():
    # Un seul bloc <script> non vide (garde-fou contre une troncature de l'asset).
    txt = _ASSET.read_text(encoding="utf-8")
    assert txt.count("<script>") == 1 and txt.count("</script>") == 1
    body = txt.split("<script>", 1)[1].split("</script>", 1)[0]
    assert len(body) > 10_000          # le JS de l'IHM est substantiel
    assert body.count("{") == body.count("}")   # accolades équilibrées
