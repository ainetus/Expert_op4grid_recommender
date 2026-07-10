"""
tests/manoeuvre/test_conformite_art_manoeuvre.py
--------------------------------------------------
Vérificateur de conformité « art de la manœuvre » (``algo/conformite.py``,
règles R20-R25 de ``docs/manoeuvre/art_de_la_manoeuvre.md``) :

- classification des conséquences par rejeu (R20) : boucles, transit,
  mise sous/hors tension, préparer/désaiguiller, manœuvre hors tension ;
- matrice d'autorisation CCRT par famille d'organe (R21) ;
- avertissement « essai de barre par disjoncteur » (R22) ;
- machine à états des départs + transitions interdites (R23) ;
- temporisations ACT 104 (R24) ;
- contrôles SCADA attendus (R25) ;
- intégration : ``verifier_sequence`` renseigne ``ResultatManoeuvres.conformite``
  sans toucher ``ecarts``/``alertes`` (compatibilité des goldens).
"""

from __future__ import annotations

import pytest

from expert_op4grid_recommender.manoeuvre.topologie import (
    PosteTopologique,
    TopologieNodale,
)
from expert_op4grid_recommender.manoeuvre.algo import (
    Consequence,
    EtatDepart,
    FamilleOrgane,
    Manoeuvre,
    analyser_conformite,
    calculer_temporisations,
    classifier_manoeuvres,
    determiner_topo_complete_cible,
    familles_organes,
)
from expert_op4grid_recommender.manoeuvre.algo.graph_ops import _sa_path_to_sjb
from expert_op4grid_recommender.manoeuvre.plugins.pipeline import verifier_sequence

from .fixture_loader import build_graph_from_fixture, list_available_fixtures


def _fixtures_available() -> bool:
    return len(list_available_fixtures()) > 0


pytestmark = pytest.mark.skipif(
    not _fixtures_available(), reason="Fixtures de postes non générées."
)

SS_112 = "CARRIP3_CARRI3SEC..12 SS.1.12_OC"
DJ_COUPL = "CARRIP3_CARRI3COUPL.1 DJ_OC"
DJ_BARR6 = "CARRIP3_CARRI3BARR6.1 DJ_OC"
SA1_BARR6 = "CARRIP3_CARRI3BARR6.1 SA.1_OC"
SA2_BARR6 = "CARRIP3_CARRI3BARR6.1 SA.2_OC"


@pytest.fixture
def poste_carrip3() -> PosteTopologique:
    G = build_graph_from_fixture("CARRIP3")
    return PosteTopologique.from_graph(G, "CARRIP3")


def _set_switch_in_graph(G, switch_id, open_):
    for _u, _v, d in G.edges(data=True):
        if d.get("switch_id") == switch_id:
            d["open"] = open_


# ---------------------------------------------------------------------------
# Familles d'organes
# ---------------------------------------------------------------------------

def test_familles_organes_carrip3(poste_carrip3):
    fam = familles_organes(poste_carrip3)
    # Sectionnements de barre : les deux SS de CARRIP3.
    assert fam[SS_112] == FamilleOrgane.SS_SECTIONNEMENT
    assert fam["CARRIP3_CARRI3SEC..12 SS.2.12_OC"] == FamilleOrgane.SS_SECTIONNEMENT
    # Travée de couplage : DJ + SA de couplage.
    assert fam[DJ_COUPL] == FamilleOrgane.DJ
    assert fam["CARRIP3_CARRI3COUPL.1 SA.1_OC"] == FamilleOrgane.SA_COUPLAGE
    # Cellule de départ : DJ + SA d'aiguillage.
    assert fam[DJ_BARR6] == FamilleOrgane.DJ
    assert fam[SA1_BARR6] == FamilleOrgane.SA_AIGUILLAGE


# ---------------------------------------------------------------------------
# Classification des conséquences (R20)
# ---------------------------------------------------------------------------

def test_boucle_courte_classee_boucle(poste_carrip3):
    """Ré-aiguillage en boucle courte (couplage fermé) : fermeture puis
    ouverture de SA = fermeture/ouverture de boucle — autorisé pour un SA."""
    seq = [Manoeuvre(SA2_BARR6, "CLOSE", "boucle courte"),
           Manoeuvre(SA1_BARR6, "OPEN", "boucle courte")]
    classees = classifier_manoeuvres(poste_carrip3, seq)
    assert classees[0].consequences == frozenset({Consequence.FERMETURE_BOUCLE})
    assert classees[1].consequences == frozenset({Consequence.OUVERTURE_BOUCLE})
    conf = analyser_conformite(poste_carrip3, seq)
    assert conf.is_conforme


def test_boucle_longue_conforme(poste_carrip3):
    """Boucle longue dans les règles : -DJ (coupure), ±SA hors charge
    (désaiguiller/préparer), +DJ (établissement du transit)."""
    seq = [Manoeuvre(DJ_BARR6, "OPEN", "mhu départ"),
           Manoeuvre(SA1_BARR6, "OPEN", "désaiguiller"),
           Manoeuvre(SA2_BARR6, "CLOSE", "préparer"),
           Manoeuvre(DJ_BARR6, "CLOSE", "mes")]
    classees = classifier_manoeuvres(poste_carrip3, seq)
    assert Consequence.COUPER_TRANSIT in classees[0].consequences
    assert classees[1].consequences == frozenset({Consequence.DESAIGUILLER})
    assert classees[2].consequences == frozenset({Consequence.PREPARER})
    assert Consequence.ETABLIR_TRANSIT in classees[3].consequences
    conf = analyser_conformite(poste_carrip3, seq)
    assert conf.is_conforme
    assert not conf.avertissements


def test_ouverture_couplage_change_nb_noeuds(poste_carrip3):
    """Ouvrir le DJ de couplage scinde le poste en 2 nœuds : couper un transit
    + changer le nombre de nœuds — autorisé pour un DJ."""
    classees = classifier_manoeuvres(
        poste_carrip3, [Manoeuvre(DJ_COUPL, "OPEN", "split")])
    assert classees[0].famille == FamilleOrgane.DJ
    assert Consequence.COUPER_TRANSIT in classees[0].consequences
    assert Consequence.CHANGER_NB_NOEUDS in classees[0].consequences
    conf = analyser_conformite(poste_carrip3, [Manoeuvre(DJ_COUPL, "OPEN", "s")])
    assert conf.is_conforme


def test_sans_effet(poste_carrip3):
    """Une manœuvre plaçant l'organe dans son état courant est « sans effet »."""
    classees = classifier_manoeuvres(
        poste_carrip3, [Manoeuvre(SA1_BARR6, "CLOSE", "déjà fermé")])
    assert classees[0].consequences == frozenset({Consequence.SANS_EFFET})


def test_organe_inconnu(poste_carrip3):
    classees = classifier_manoeuvres(
        poste_carrip3, [Manoeuvre("ORGANE_INEXISTANT", "OPEN", "?")])
    assert classees[0].famille == FamilleOrgane.INCONNU
    assert "inconnu" in classees[0].commentaire


def test_manoeuvre_hors_tension(poste_carrip3):
    """SA manœuvré derrière un DJ ouvert **et** vers une section morte :
    manœuvre hors tension / préparer, sans violation."""
    G = build_graph_from_fixture("CARRIP3")
    p0 = PosteTopologique.from_graph(G, "CARRIP3")
    # Section 1.2 (node 1) rendue morte : SS ouvert + départs désaiguillés.
    _set_switch_in_graph(G, SS_112, True)
    for c in p0.cellules.cellules_depart:
        if 1 in c.busbar_nodes:
            for sid in _sa_path_to_sjb(c, 1):
                _set_switch_in_graph(G, sid, True)
    poste = PosteTopologique.from_graph(G, "CARRIP3")
    # Ouvrir puis refermer le SS entre 1.1 (vive) et 1.2 (morte) n'est qu'une
    # mise hors/sous tension de la section - pas un transit.
    classees = classifier_manoeuvres(
        poste, [Manoeuvre(SS_112, "CLOSE", "resection")])
    assert classees[0].consequences == frozenset({Consequence.MISE_SOUS_TENSION})
    assert classees[0].sjb_impactees == ("CARRIP3_1.2",)


# ---------------------------------------------------------------------------
# Matrice d'autorisation (R21) + essai de barre (R22)
# ---------------------------------------------------------------------------

def test_sa_en_charge_est_une_violation(poste_carrip3):
    """Ouvrir le SA d'un départ en service (sans chemin parallèle) coupe le
    transit par un sectionneur : violation de la matrice CCRT **et**
    transition d'état interdite (R23)."""
    conf = analyser_conformite(
        poste_carrip3, [Manoeuvre(SA1_BARR6, "OPEN", "faute")])
    assert not conf.is_conforme
    assert any("couper_transit" in v for v in conf.violations)
    assert any("manœuvre en charge interdite" in v for v in conf.violations)


def test_fermeture_sa_pontant_deux_noeuds_est_une_violation(poste_carrip3):
    """Couplage ouvert (2 nœuds) : fermer le 2e SA d'un départ en service
    ponterait les deux barres par le sectionneur — interdit."""
    G = build_graph_from_fixture("CARRIP3")
    _set_switch_in_graph(G, DJ_COUPL, True)          # scinde le poste en 2 nœuds
    poste = PosteTopologique.from_graph(G, "CARRIP3")
    conf = analyser_conformite(
        poste, [Manoeuvre(SA2_BARR6, "CLOSE", "DA interdit")])
    assert not conf.is_conforme
    assert any("etablir_transit" in v or "changer_nb_noeuds" in v
               for v in conf.violations)


def test_essai_de_barre_avertissement(poste_carrip3):
    """Remettre sous tension une section morte par la seule fermeture du SS :
    avertissement R22 (préférer un essai par disjoncteur)."""
    G = build_graph_from_fixture("CARRIP3")
    p0 = PosteTopologique.from_graph(G, "CARRIP3")
    _set_switch_in_graph(G, SS_112, True)
    for c in p0.cellules.cellules_depart:
        if 1 in c.busbar_nodes:
            for sid in _sa_path_to_sjb(c, 1):
                _set_switch_in_graph(G, sid, True)
    poste = PosteTopologique.from_graph(G, "CARRIP3")
    conf = analyser_conformite(
        poste, [Manoeuvre(SS_112, "CLOSE", "remise en service section")])
    assert conf.is_conforme                       # autorisé (limité au JdB)…
    assert any("essai de barre" in a for a in conf.avertissements)  # …mais déconseillé


# ---------------------------------------------------------------------------
# Machine à états des départs (R23)
# ---------------------------------------------------------------------------

def test_trajectoire_etats_boucle_longue(poste_carrip3):
    seq = [Manoeuvre(DJ_BARR6, "OPEN", "mhu"),
           Manoeuvre(SA1_BARR6, "OPEN", "désaiguiller"),
           Manoeuvre(SA2_BARR6, "CLOSE", "préparer"),
           Manoeuvre(DJ_BARR6, "CLOSE", "mes")]
    conf = analyser_conformite(poste_carrip3, seq)
    tr = [(t.avant, t.apres) for t in conf.transitions
          if t.equipment_id == "BARR6L31CARRI"]
    assert tr == [
        (EtatDepart.EN_SERVICE, EtatDepart.PREPARE),
        (EtatDepart.PREPARE, EtatDepart.DESAIGUILLE),
        (EtatDepart.DESAIGUILLE, EtatDepart.PREPARE),
        (EtatDepart.PREPARE, EtatDepart.EN_SERVICE),
    ]
    assert not any(t.interdite for t in conf.transitions)
    # L'établissement du transit porte les contrôles +I / Scc.
    dern = conf.transitions[-1]
    assert any("calcul de répartition" in c for c in dern.controles)


def test_double_aiguillage_controle_noeuds(poste_carrip3):
    """Passage en service → en service - DA : transition permise avec contrôle
    du nombre de nœuds (couplage fermé : boucle courte)."""
    conf = analyser_conformite(
        poste_carrip3, [Manoeuvre(SA2_BARR6, "CLOSE", "+DA")])
    t = next(t for t in conf.transitions if t.equipment_id == "BARR6L31CARRI")
    assert (t.avant, t.apres) == (EtatDepart.EN_SERVICE, EtatDepart.EN_SERVICE_DA)
    assert not t.interdite
    assert any("nombre de nœuds" in c for c in t.controles)
    assert conf.is_conforme


# ---------------------------------------------------------------------------
# Temporisations ACT 104 (R24)
# ---------------------------------------------------------------------------

def test_tempo_sectionneur_10s(poste_carrip3):
    seq = [Manoeuvre(SA2_BARR6, "CLOSE", "boucle courte"),
           Manoeuvre(SA1_BARR6, "OPEN", "boucle courte")]
    tempos = calculer_temporisations(poste_carrip3, seq)
    assert [(t.index, t.avant, t.duree_s) for t in tempos] == [
        (0, False, 10), (1, False, 10)]


def test_tempo_regonflage_dj_60s(poste_carrip3):
    """Cycle fermeture → ouverture → re-fermeture d'un même DJ : la seconde
    fermeture attend le complément à 60 s (temporisations déjà écoulées
    déduites, comme dans la méthode CCO)."""
    seq = [Manoeuvre(DJ_BARR6, "OPEN", "essai -u"),
           Manoeuvre(DJ_BARR6, "CLOSE", "essai +u"),        # 1re fermeture (t=0)
           Manoeuvre(DJ_BARR6, "OPEN", "-u"),
           Manoeuvre(SA2_BARR6, "CLOSE", "préparer"),        # +10 s (sectionneur)
           Manoeuvre(DJ_BARR6, "CLOSE", "mes")]              # 2e fermeture
    tempos = calculer_temporisations(poste_carrip3, seq)
    regonflage = [t for t in tempos if "regonflage" in t.motif]
    assert len(regonflage) == 1
    t = regonflage[0]
    assert (t.index, t.avant) == (4, True)
    assert t.duree_s == 50          # 60 s - 10 s de tempo sectionneur déjà écoulée
    # Pas de tempo à la première fermeture du DJ.
    assert not any(t.index == 1 for t in regonflage)


def test_pas_de_tempo_dj_ferme_une_seule_fois(poste_carrip3):
    seq = [Manoeuvre(DJ_BARR6, "OPEN", "-u"),
           Manoeuvre(DJ_BARR6, "CLOSE", "mes")]
    tempos = calculer_temporisations(poste_carrip3, seq)
    assert not [t for t in tempos if "regonflage" in t.motif]


# ---------------------------------------------------------------------------
# Contrôles attendus (R25)
# ---------------------------------------------------------------------------

def test_controles_attendus(poste_carrip3):
    seq = [Manoeuvre(DJ_BARR6, "OPEN", "coupure"),
           Manoeuvre(DJ_BARR6, "CLOSE", "mes")]
    classees = classifier_manoeuvres(poste_carrip3, seq)
    assert any("TM I passe à 0" in c for c in classees[0].controles)
    assert any("calcul de répartition" in c for c in classees[1].controles)
    # Boucles et manœuvres hors charge : aucun contrôle.
    boucle = classifier_manoeuvres(
        poste_carrip3, [Manoeuvre(SA2_BARR6, "CLOSE", "boucle")])
    assert boucle[0].controles == []


# ---------------------------------------------------------------------------
# Intégration : séquences de l'algorithme + verifier_sequence
# ---------------------------------------------------------------------------

def test_sequence_algo_est_conforme(poste_carrip3):
    """Les séquences produites par le séquenceur natif respectent la matrice
    d'autorisation (aucune violation)."""
    deps = sorted({c.equipment_id for c in poste_carrip3.cellules.cellules_depart})
    cible = TopologieNodale.from_node_groups(
        poste_carrip3.voltage_level_id, [deps[:8], deps[8:]])
    res = determiner_topo_complete_cible(poste_carrip3, cible)
    conf = analyser_conformite(poste_carrip3, res.manoeuvres)
    assert conf.is_conforme, conf.violations


def test_verifier_sequence_renseigne_conformite(poste_carrip3):
    """``verifier_sequence`` attache l'analyse au champ ``conformite`` sans
    modifier les verdicts historiques (``ecarts``/``alertes``)."""
    from expert_op4grid_recommender.manoeuvre.algo import ResultatManoeuvres

    res = ResultatManoeuvres(
        voltage_level_id=poste_carrip3.voltage_level_id,
        topo_initiale=poste_carrip3.topologie_nodale,
        topo_cible=poste_carrip3.topologie_nodale,
        manoeuvres=[Manoeuvre(SA1_BARR6, "OPEN", "faute")],
    )
    ecarts_regle_historique = None
    verifier_sequence(poste_carrip3, res)
    assert res.conformite is not None
    assert not res.conformite.is_conforme
    # La règle historique du sectionneur sous charge reste portée par ecarts.
    ecarts_regle_historique = [e for e in res.ecarts if "sous charge" in e]
    assert ecarts_regle_historique
    # …et les violations de conformité n'y sont PAS dupliquées.
    assert not any("matrice CCRT" in e for e in res.ecarts)
    assert not any("matrice CCRT" in a for a in res.alertes)


def test_analyser_conformite_ne_mute_pas_le_poste(poste_carrip3):
    etats_avant = {d.get("switch_id"): d.get("open")
                   for _u, _v, d in poste_carrip3.graph.edges(data=True)
                   if d.get("switch_id")}
    analyser_conformite(poste_carrip3, [
        Manoeuvre(DJ_BARR6, "OPEN", "x"),
        Manoeuvre(SA1_BARR6, "OPEN", "y")])
    etats_apres = {d.get("switch_id"): d.get("open")
                   for _u, _v, d in poste_carrip3.graph.edges(data=True)
                   if d.get("switch_id")}
    assert etats_avant == etats_apres
