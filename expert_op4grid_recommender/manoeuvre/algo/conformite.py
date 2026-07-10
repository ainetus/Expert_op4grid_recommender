"""
manoeuvre/algo/conformite.py — Vérificateur de conformité « art de la manœuvre ».

Enrichit la vérification des séquences au-delà de la seule règle du sectionneur
(``verification.py``) en portant les référentiels d'exploitation RTE (CCRT
C3-3, consigne ACT 104, méthode experte « fiche de manœuvres d'un CCO ») :

1. **Classification des conséquences** (R20) — chaque manœuvre est classée par
   rejeu sur le graphe du poste : manœuvre hors tension, ouverture/fermeture de
   boucle, préparer/désaiguiller, mise sous tension (à vide), mise hors
   tension, établissement/coupure de transit, changement du nombre de nœuds.
2. **Matrice d'autorisation par type d'organe** (R21) — un sectionneur (SA,
   SS, SL) ne peut ni établir ni couper un transit, ni changer le nombre de
   nœuds électriques du poste ; seuls le DJ et l'interrupteur le peuvent.
   Généralise la règle « sectionneur hors charge » (R18).
3. **Essai de barre** (R22, avertissement) — la remise sous tension d'une
   section de barre morte doit se faire par un **disjoncteur** (de préférence
   de ligne, sinon de couplage, en dernier recours de transformateur), pas par
   la simple fermeture d'un sectionneur.
4. **Suivi de l'état des départs** (R23) — machine à états (désaiguillé /
   préparé / préparé-DA / en service / en service-DA / états « bizarres »)
   avec table des transitions permises, bizarres ou **interdites**.
5. **Temporisations ACT 104** (R24) — 10 s après chaque manœuvre de
   sectionneur ; 60 s minimum entre deux fermetures d'un même disjoncteur
   (regonflage après cycle O-F-O).
6. **Contrôles attendus** (R25) — pour chaque manœuvre, les télésignalisations
   (TS) et télémesures (TM) SCADA à contrôler (TM I à 0, TS manque tension,
   conformité au calcul de répartition, nombre de nœuds…).

Le point d'entrée est :func:`analyser_conformite` ; son résultat
(:class:`ConformiteSequence`) est attaché par ``plugins.pipeline.
verifier_sequence`` au champ ``ResultatManoeuvres.conformite``. Les
**violations** et **avertissements** produits ici sont volontairement séparés
des ``ecarts`` / ``alertes`` historiques (compatibilité des goldens) : ils
constituent un étage de vérification additionnel, pas un remplacement.

Limites (documentées dans ``docs/manoeuvre/art_de_la_manoeuvre.md``) :

- **Présomption de tension** : l'analyse est mono-poste (un voltage level).
  Les équipements « feeders » (ligne, transformateur, groupe, batterie, HVDC)
  sont présumés pouvoir tenir leur composante sous tension ; les charges et
  moyens de compensation sont passifs. L'état de l'extrémité distante d'une
  ligne est inconnu : une coupure de transit peut en réalité être une simple
  mise hors tension d'un ouvrage déjà à vide (classification conservative).
- Pas de calcul électrique : ni transit (validation par calcul de répartition,
  R26), ni Icc — hors de portée du graphe seul.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import networkx as nx

from ..models import EquipmentType, SwitchKind
from ..topologie import PosteTopologique
from .results import Manoeuvre
from .graph_ops import (
    _inter_sjb_couplers,
    _is_open,
    _set_switch,
    _switch_edge_index,
    _wired_sjbs,
)


# ---------------------------------------------------------------------------
# Vocabulaire : familles d'organes, conséquences, états de départ
# ---------------------------------------------------------------------------

class FamilleOrgane(Enum):
    """Famille d'organe de coupure au sens du CCRT (matrice d'autorisation)."""
    DJ = "DJ"                              # disjoncteur
    INTERRUPTEUR = "INTERRUPTEUR"          # interrupteur / interrupteur-sectionneur
    SA_AIGUILLAGE = "SA_AIGUILLAGE"        # sectionneur d'aiguillage barre (SAB)
    SA_COUPLAGE = "SA_COUPLAGE"            # sectionneur d'une travée de couplage
    SS_SECTIONNEMENT = "SS_SECTIONNEMENT"  # sectionneur de sectionnement de barre
    SL_AUTRE = "SL_AUTRE"                  # sectionneur de ligne / autre sectionneur
    INCONNU = "INCONNU"                    # organe absent du poste


#: Familles soumises à la règle du sectionneur (manœuvre hors charge uniquement).
FAMILLES_SECTIONNEUR = frozenset({
    FamilleOrgane.SA_AIGUILLAGE,
    FamilleOrgane.SA_COUPLAGE,
    FamilleOrgane.SS_SECTIONNEMENT,
    FamilleOrgane.SL_AUTRE,
})


class Consequence(Enum):
    """Conséquence élémentaire d'une manœuvre (référentiel CCRT C3-3).

    Une manœuvre peut porter **plusieurs** conséquences simultanées (ex.
    l'ouverture du DJ d'un départ de charge coupe un transit **et** met la
    charge hors tension).
    """
    SANS_EFFET = "sans_effet"                    # l'organe est déjà dans l'état visé
    MANOEUVRE_HU = "manoeuvre_hors_tension"      # aucune des deux parties n'est sous tension
    OUVERTURE_BOUCLE = "ouverture_boucle"        # les deux côtés restent reliés par ailleurs
    FERMETURE_BOUCLE = "fermeture_boucle"        # les deux côtés étaient déjà reliés
    PREPARER = "preparer"                        # fermeture d'un SA, jonction de cellule morte
    DESAIGUILLER = "desaiguiller"                # ouverture d'un SA, jonction de cellule morte
    MISE_SOUS_TENSION = "mise_sous_tension"      # une partie morte est énergisée (à vide)
    MISE_HORS_TENSION = "mise_hors_tension"      # une partie devient morte
    ETABLIR_TRANSIT = "etablir_transit"          # deux parties actives sont reliées
    COUPER_TRANSIT = "couper_transit"            # deux parties actives sont séparées
    CHANGER_NB_NOEUDS = "changer_nb_noeuds"      # fusion/scission de nœuds électriques à barres


#: Conséquences qu'un **sectionneur** n'a pas le droit de porter (matrice CCRT :
#: « Établir un transit / Couper un transit / Changer le nombre de nœuds » = Non
#: pour SRB, SAB, SS ; oui pour DJ, INTER).
CONSEQUENCES_INTERDITES_SECTIONNEUR = frozenset({
    Consequence.ETABLIR_TRANSIT,
    Consequence.COUPER_TRANSIT,
    Consequence.CHANGER_NB_NOEUDS,
})


#: Équipements présumés capables de **tenir une composante sous tension**
#: (feeders vers le reste du réseau ou sources). Les charges et moyens de
#: compensation sont passifs : une composante qui n'en contient aucun est morte.
SOURCES_TENSION = frozenset({
    EquipmentType.LINE_SIDE1,
    EquipmentType.LINE_SIDE2,
    EquipmentType.TRANSFORMER_SIDE1,
    EquipmentType.TRANSFORMER_SIDE2,
    EquipmentType.DANGLING_LINE,
    EquipmentType.GENERATOR,
    EquipmentType.BATTERY,
    EquipmentType.HVDC_CONVERTER_STATION,
})


class EtatDepart(Enum):
    """État d'un départ au sens CCO : position des SA (aiguillage) × OC ligne.

    Nommage de la méthode experte « fiche de manœuvres d'un CCO » :
    ``PREPARE_DA`` y est aussi appelé « bizarre DA ouvert » dans la table des
    transitions (≥ 2 SA fermés, OC ligne ouvert).
    """
    DESAIGUILLE = "hors tension désaiguillé"     # 0 SA fermé, OC ligne ouvert
    PREPARE = "préparé"                          # 1 SA fermé, OC ligne ouvert
    PREPARE_DA = "préparé - DA"                  # ≥2 SA fermés, OC ligne ouvert
    EN_SERVICE = "en service"                    # 1 SA fermé, OC ligne fermé
    EN_SERVICE_DA = "en service - DA"            # ≥2 SA fermés, OC ligne fermé
    NON_PREPARE_FERME = "bizarre non préparé fermé"  # 0 SA fermé, OC ligne fermé


# Table des transitions d'état d'un départ (méthode experte CCO, « Suivi de
# l'état des départs et des ouvrages »). Valeur : (interdite ?, avertissement,
# contrôles attendus). Une transition observée absente de la table est signalée
# « étrange » (avertissement).
_TRANSITIONS_DEPART: dict[tuple[EtatDepart, EtatDepart],
                          tuple[bool, Optional[str], list[str]]] = {
    (EtatDepart.DESAIGUILLE, EtatDepart.PREPARE): (False, None, []),
    (EtatDepart.DESAIGUILLE, EtatDepart.NON_PREPARE_FERME): (
        False, "fermeture de l'OC ligne d'un départ non préparé "
               "(bizarre sauf manœuvre programmée)", []),
    (EtatDepart.PREPARE, EtatDepart.DESAIGUILLE): (False, None, []),
    (EtatDepart.PREPARE, EtatDepart.EN_SERVICE): (
        False, None,
        ["TM I/P/Q conformes au calcul de répartition (établissement du transit)",
         "vérifier les puissances de court-circuit (Scc) si dépassement possible"]),
    (EtatDepart.PREPARE, EtatDepart.PREPARE_DA): (
        False, "double aiguillage sous OC ligne ouvert (bizarre)",
        ["contrôle du nombre de nœuds du poste avant/après"]),
    (EtatDepart.PREPARE_DA, EtatDepart.PREPARE): (False, None, []),
    (EtatDepart.PREPARE_DA, EtatDepart.EN_SERVICE_DA): (
        False, None,
        ["TM I/P/Q conformes au calcul de répartition (établissement du transit)",
         "vérifier les puissances de court-circuit (Scc) si dépassement possible"]),
    (EtatDepart.EN_SERVICE, EtatDepart.PREPARE): (
        False, None,
        ["TM I passe à 0 ; liste des éléments qui passent hors tension le cas échéant"]),
    (EtatDepart.EN_SERVICE, EtatDepart.EN_SERVICE_DA): (
        False, None, ["contrôle du nombre de nœuds du poste avant/après"]),
    (EtatDepart.EN_SERVICE, EtatDepart.NON_PREPARE_FERME): (
        True, "ouverture du dernier SA d'un départ en service : manœuvre en "
              "charge interdite", []),
    (EtatDepart.EN_SERVICE_DA, EtatDepart.EN_SERVICE): (
        False, None, ["contrôle du nombre de nœuds du poste avant/après"]),
    (EtatDepart.EN_SERVICE_DA, EtatDepart.PREPARE_DA): (
        False, None,
        ["TM I passe à 0 ; liste des éléments qui passent hors tension le cas échéant"]),
    (EtatDepart.NON_PREPARE_FERME, EtatDepart.DESAIGUILLE): (
        False, "ouverture de l'OC ligne d'un départ non préparé "
               "(bizarre sauf manœuvre programmée)", []),
    (EtatDepart.NON_PREPARE_FERME, EtatDepart.EN_SERVICE): (
        True, "fermeture d'un SA sur un départ dont l'OC ligne est fermé : le "
              "sectionneur établirait le courant — interdit", []),
}


# ---------------------------------------------------------------------------
# Structures de résultat
# ---------------------------------------------------------------------------

@dataclass
class ManoeuvreClassee:
    """Classification d'une manœuvre : famille d'organe + conséquences + contrôles."""
    index: int                       # position dans la séquence (0-based)
    switch_id: str
    action: str                      # "OPEN" | "CLOSE"
    famille: FamilleOrgane
    consequences: frozenset[Consequence]
    #: Sections de barre (busbar_section_id) mises sous/hors tension par la manœuvre.
    sjb_impactees: tuple[str, ...] = ()
    controles: list[str] = field(default_factory=list)
    commentaire: str = ""

    def __repr__(self) -> str:  # pragma: no cover - debug
        cons = ",".join(sorted(c.value for c in self.consequences))
        return (f"{self.index:2d}. {self.action:5s} {self.switch_id} "
                f"[{self.famille.value}] -> {cons}")


@dataclass
class TransitionDepart:
    """Changement d'état d'un départ provoqué par une manœuvre."""
    index: int                       # manœuvre déclenchante
    equipment_id: str
    avant: EtatDepart
    apres: EtatDepart
    interdite: bool = False
    avertissement: Optional[str] = None
    controles: list[str] = field(default_factory=list)


@dataclass
class Temporisation:
    """Temps d'attente à insérer dans la séquence (consigne ACT 104)."""
    index: int                       # manœuvre concernée
    avant: bool                      # True : attendre avant ; False : après
    duree_s: int
    motif: str


@dataclass
class ConformiteSequence:
    """Résultat de l'analyse de conformité « art de la manœuvre » d'une séquence.

    ``violations`` : règles d'exploitation enfreintes (bloquantes au sens du
    CCRT — matrice d'autorisation des organes, transitions interdites).
    ``avertissements`` : bonnes pratiques non bloquantes (essai de barre par
    disjoncteur, transitions « bizarres »…).
    """
    voltage_level_id: str
    classees: list[ManoeuvreClassee] = field(default_factory=list)
    transitions: list[TransitionDepart] = field(default_factory=list)
    temporisations: list[Temporisation] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    avertissements: list[str] = field(default_factory=list)

    @property
    def is_conforme(self) -> bool:
        """True si aucune violation (les avertissements n'empêchent pas la conformité)."""
        return not self.violations

    @property
    def duree_temporisations_s(self) -> int:
        return sum(t.duree_s for t in self.temporisations)

    def resume(self) -> str:
        lines = [
            f"Conformité art de la manœuvre '{self.voltage_level_id}' : "
            f"{'CONFORME' if self.is_conforme else f'{len(self.violations)} violation(s)'}"
            f" · {len(self.avertissements)} avertissement(s)"
            f" · {self.duree_temporisations_s} s de temporisations"
        ]
        for c in self.classees:
            cons = ", ".join(sorted(x.value for x in c.consequences))
            lines.append(f"  {c.index:2d}. {c.action:5s} {c.switch_id} "
                         f"[{c.famille.value}] -> {cons}")
            for ctl in c.controles:
                lines.append(f"        contrôle : {ctl}")
        for v in self.violations:
            lines.append(f"  ✗ {v}")
        for a in self.avertissements:
            lines.append(f"  ⚠ {a}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Familles d'organes
# ---------------------------------------------------------------------------

def familles_organes(poste: PosteTopologique) -> dict[str, FamilleOrgane]:
    """Classe chaque organe du poste dans sa famille CCRT.

    Priorité : liaisons inter-SJB (sectionnements ``SS``, organes de travée de
    couplage ``DJ``/``SA_COUPLAGE``) puis cellules de départ (DJ, interrupteur,
    ``SA_AIGUILLAGE`` = sectionneur dont une borne est une SJB, ``SL_AUTRE``
    sinon), enfin repli sur le ``SwitchKind`` brut.
    """
    familles: dict[str, FamilleOrgane] = {}
    bb_nodes = set(poste.tronconnement.barre_par_busbar)

    # Repli générique par nature de l'organe.
    for _u, _v, d in poste.graph.edges(data=True):
        sid = d.get("switch_id")
        if sid is None:
            continue
        kind = d.get("kind")
        if kind == SwitchKind.BREAKER:
            familles[sid] = FamilleOrgane.DJ
        elif kind == SwitchKind.LOAD_BREAK_SWITCH:
            familles[sid] = FamilleOrgane.INTERRUPTEUR
        elif kind == SwitchKind.DISCONNECTOR:
            familles[sid] = FamilleOrgane.SL_AUTRE

    # Cellules de départ : SA d'aiguillage = sectionneur touchant une SJB.
    for cell in poste.cellules.cellules_depart:
        for sw in cell.switches:
            if sw.kind == SwitchKind.DISCONNECTOR and (
                    sw.node1 in bb_nodes or sw.node2 in bb_nodes):
                familles[sw.switch_id] = FamilleOrgane.SA_AIGUILLAGE

    # Liaisons inter-SJB : priment (un organe de travée de couplage ou un
    # sectionnement n'est pas un organe de départ).
    for cp in _inter_sjb_couplers(poste):
        if cp.is_sectionnement:
            for sid in cp.switch_ids:
                familles[sid] = FamilleOrgane.SS_SECTIONNEMENT
        else:
            for sid in cp.switch_ids:
                if sid not in cp.breaker_ids:
                    familles[sid] = FamilleOrgane.SA_COUPLAGE
    return familles


# ---------------------------------------------------------------------------
# Classification des conséquences (rejeu)
# ---------------------------------------------------------------------------

def _graphe_live(G: nx.Graph) -> nx.Graph:
    """Sous-graphe des liaisons fermées (connectivité électrique courante)."""
    H = nx.Graph()
    H.add_nodes_from(G.nodes())
    H.add_edges_from((u, v) for u, v, d in G.edges(data=True)
                     if not d.get("open", False))
    return H


def _profil_composante(G: nx.Graph, comp: set) -> tuple[bool, bool, list[str]]:
    """(contient une source de tension ?, contient un équipement ?, SJB de la
    composante — ``busbar_section_id`` triés)."""
    source = False
    equip = False
    sjbs: list[str] = []
    for n in comp:
        d = G.nodes[n]
        if d.get("equipment_id"):
            equip = True
            if d.get("equipment_type") in SOURCES_TENSION:
                source = True
        bb = d.get("busbar_section_id")
        if bb:
            sjbs.append(bb)
    return source, equip, sorted(sjbs)


def classifier_manoeuvres(
    poste: PosteTopologique, manoeuvres: list[Manoeuvre]
) -> list[ManoeuvreClassee]:
    """Classifie chaque manœuvre de la séquence par **rejeu** sur une copie du
    graphe du poste (jamais muté) : famille d'organe, conséquences élémentaires
    (CCRT C3-3), sections de barre impactées, contrôles SCADA attendus (R25).
    """
    familles = familles_organes(poste)
    G = poste.graph.copy()
    idx = _switch_edge_index(G)
    out: list[ManoeuvreClassee] = []

    for i, m in enumerate(manoeuvres):
        edge = idx.get(m.switch_id)
        if edge is None:
            out.append(ManoeuvreClassee(
                i, m.switch_id, m.action, FamilleOrgane.INCONNU,
                frozenset({Consequence.SANS_EFFET}),
                commentaire="organe inconnu du poste"))
            continue

        famille = familles.get(m.switch_id, FamilleOrgane.INCONNU)
        u, v = edge
        veut_ouvrir = (m.action == "OPEN")
        deja = (_is_open(G, m.switch_id) == veut_ouvrir)
        if deja:
            out.append(ManoeuvreClassee(
                i, m.switch_id, m.action, famille,
                frozenset({Consequence.SANS_EFFET})))
            continue

        cons: set[Consequence] = set()
        sjb_impactees: tuple[str, ...] = ()

        if veut_ouvrir:
            # Connectivité APRÈS ouverture : l'organe retiré du graphe live.
            _set_switch(G, m.switch_id, True)
            H = _graphe_live(G)
            if nx.has_path(H, u, v):
                comp = nx.node_connected_component(H, u)
                source, _equip, _ = _profil_composante(G, comp)
                cons.add(Consequence.OUVERTURE_BOUCLE if source
                         else Consequence.MANOEUVRE_HU)
            else:
                cu = nx.node_connected_component(H, u)
                cv = nx.node_connected_component(H, v)
                src_u, eq_u, sjb_u = _profil_composante(G, cu)
                src_v, eq_v, sjb_v = _profil_composante(G, cv)
                if not src_u and not src_v:
                    cons.add(Consequence.MANOEUVRE_HU)
                elif src_u and src_v:
                    cons.add(Consequence.COUPER_TRANSIT)
                    if sjb_u and sjb_v:
                        cons.add(Consequence.CHANGER_NB_NOEUDS)
                else:
                    # Un seul côté reste sous tension ; l'autre devient mort.
                    eq_mort, sjb_mort = (eq_u, sjb_u) if not src_u else (eq_v, sjb_v)
                    if eq_mort:
                        cons.update({Consequence.COUPER_TRANSIT,
                                     Consequence.MISE_HORS_TENSION})
                    elif sjb_mort:
                        cons.add(Consequence.MISE_HORS_TENSION)
                        sjb_impactees = tuple(sjb_mort)
                    elif famille == FamilleOrgane.SA_AIGUILLAGE:
                        cons.add(Consequence.DESAIGUILLER)
                    else:
                        cons.add(Consequence.MISE_HORS_TENSION)
        else:
            # Connectivité AVANT fermeture (l'organe est encore ouvert).
            H = _graphe_live(G)
            if nx.has_path(H, u, v):
                comp = nx.node_connected_component(H, u)
                source, _equip, _ = _profil_composante(G, comp)
                cons.add(Consequence.FERMETURE_BOUCLE if source
                         else Consequence.MANOEUVRE_HU)
            else:
                cu = nx.node_connected_component(H, u)
                cv = nx.node_connected_component(H, v)
                src_u, eq_u, sjb_u = _profil_composante(G, cu)
                src_v, eq_v, sjb_v = _profil_composante(G, cv)
                if not src_u and not src_v:
                    cons.add(Consequence.PREPARER
                             if famille == FamilleOrgane.SA_AIGUILLAGE
                             else Consequence.MANOEUVRE_HU)
                elif src_u and src_v:
                    cons.add(Consequence.ETABLIR_TRANSIT)
                    if sjb_u and sjb_v:
                        cons.add(Consequence.CHANGER_NB_NOEUDS)
                else:
                    eq_mort, sjb_mort = (eq_u, sjb_u) if not src_u else (eq_v, sjb_v)
                    if eq_mort:
                        cons.update({Consequence.MISE_SOUS_TENSION,
                                     Consequence.ETABLIR_TRANSIT})
                    elif sjb_mort:
                        cons.add(Consequence.MISE_SOUS_TENSION)
                        sjb_impactees = tuple(sjb_mort)
                    elif famille == FamilleOrgane.SA_AIGUILLAGE:
                        cons.add(Consequence.PREPARER)
                    else:
                        cons.add(Consequence.MISE_SOUS_TENSION)
            _set_switch(G, m.switch_id, False)

        mc = ManoeuvreClassee(i, m.switch_id, m.action, famille,
                              frozenset(cons), sjb_impactees)
        mc.controles = _controles_pour(mc)
        out.append(mc)
    return out


def _controles_pour(mc: ManoeuvreClassee) -> list[str]:
    """Contrôles SCADA attendus pour une manœuvre classée (R25 — tableau
    « Contrôle lors des manœuvres » : TS manque tension, TM I, calcul de
    répartition, nombre de nœuds)."""
    ctl: list[str] = []
    cons = mc.consequences
    if Consequence.COUPER_TRANSIT in cons:
        ctl.append("TM I passe à 0 sur le départ")
    if Consequence.MISE_HORS_TENSION in cons:
        cible = (" sur la/les section(s) " + ", ".join(mc.sjb_impactees)
                 if mc.sjb_impactees else "")
        ctl.append("TS absence de tension apparaît (si la TS existe)" + cible)
    if Consequence.MISE_SOUS_TENSION in cons:
        cible = (" sur la/les section(s) " + ", ".join(mc.sjb_impactees)
                 if mc.sjb_impactees else "")
        ctl.append("TS absence de tension disparaît (si la TS existe)" + cible)
    if Consequence.ETABLIR_TRANSIT in cons:
        ctl.append("TM I/P/Q prennent les valeurs du calcul de répartition "
                   "(peuvent être proches de 0)")
        ctl.append("si dépassement d'Icc : vérifier l'absence de TS PRESENCE "
                   "dans les postes concernés")
    if Consequence.CHANGER_NB_NOEUDS in cons:
        ctl.append("contrôle du nombre de nœuds électriques du poste avant/après")
    return ctl


# ---------------------------------------------------------------------------
# Matrice d'autorisation + essai de barre
# ---------------------------------------------------------------------------

def verifier_matrice_autorisation(
    classees: list[ManoeuvreClassee],
) -> tuple[list[str], list[str]]:
    """Confronte chaque manœuvre classée à la **matrice d'autorisation** CCRT
    (conséquence × famille d'organe) et à la bonne pratique de l'**essai de
    barre** (R22).

    Returns
    -------
    (violations, avertissements)
        - violation : un sectionneur porte une conséquence interdite (établir /
          couper un transit, changer le nombre de nœuds) — présomption de
          tension sur les feeders, cf. limites du module ;
        - avertissement : remise sous tension d'une section morte par un
          sectionneur (préférer un essai par disjoncteur) ou par le disjoncteur
          d'un départ **transformateur** (préférer ligne, sinon couplage).
    """
    violations: list[str] = []
    avertissements: list[str] = []
    for mc in classees:
        interdites = mc.consequences & CONSEQUENCES_INTERDITES_SECTIONNEUR
        if mc.famille in FAMILLES_SECTIONNEUR and interdites:
            noms = ", ".join(sorted(c.value for c in interdites))
            violations.append(
                f"manœuvre {mc.index + 1} ({mc.action} {mc.switch_id}) : un "
                f"sectionneur [{mc.famille.value}] ne peut pas « {noms} » "
                "(matrice CCRT : réservé au disjoncteur/interrupteur)")
        if (Consequence.MISE_SOUS_TENSION in mc.consequences
                and mc.sjb_impactees
                and mc.famille in FAMILLES_SECTIONNEUR):
            avertissements.append(
                f"manœuvre {mc.index + 1} ({mc.action} {mc.switch_id}) : remise "
                f"sous tension de section(s) {', '.join(mc.sjb_impactees)} par "
                "un sectionneur — réaliser d'abord un essai de barre par un "
                "disjoncteur (de préférence de ligne, sinon de couplage, en "
                "dernier recours de transformateur) [R22]")
    return violations, avertissements


# ---------------------------------------------------------------------------
# Machine à états des départs (R23)
# ---------------------------------------------------------------------------

def _etat_depart(nb_sa_fermes: int, oc_ligne_tous_fermes: bool) -> EtatDepart:
    """État CCO d'un départ à partir du câblage (nb de barres aiguillées ×
    position des OC ligne)."""
    if not oc_ligne_tous_fermes:
        if nb_sa_fermes == 0:
            return EtatDepart.DESAIGUILLE
        if nb_sa_fermes == 1:
            return EtatDepart.PREPARE
        return EtatDepart.PREPARE_DA
    if nb_sa_fermes == 0:
        return EtatDepart.NON_PREPARE_FERME
    if nb_sa_fermes == 1:
        return EtatDepart.EN_SERVICE
    return EtatDepart.EN_SERVICE_DA


def suivre_etats_departs(
    poste: PosteTopologique, manoeuvres: list[Manoeuvre]
) -> list[TransitionDepart]:
    """Rejoue la séquence et suit l'**état de chaque départ** (machine à états
    CCO). Chaque changement d'état est confronté à la table des transitions :
    transitions interdites (manœuvre en charge), bizarres (avertissement) ou
    étranges (hors table).

    Les cellules sans OC ligne propre (ré-aiguillage direct) sont hors du
    modèle d'état CCO et ignorées. L'état d'un **ouvrage** complet (en service /
    sous tension à vide / hors tension) exige les départs de *toutes* ses
    extrémités : hors de portée mono-poste, non traité ici.
    """
    cells = poste.cellules
    suivis = []  # (equipment_id, oc_ligne_ids, cellule)
    for c in cells.cellules_depart:
        oc_ligne = [s.switch_id for s in c.switches
                    if s.kind in (SwitchKind.BREAKER, SwitchKind.LOAD_BREAK_SWITCH)]
        if oc_ligne:
            suivis.append((c.equipment_id, oc_ligne))
    switch_vers_departs: dict[str, set[str]] = {}
    for c in cells.cellules_depart:
        for s in c.switches:
            switch_vers_departs.setdefault(s.switch_id, set()).add(c.equipment_id)

    G = poste.graph.copy()

    def etat(eq_id: str, oc_ligne: list[str]) -> EtatDepart:
        nb_sa = len(_wired_sjbs(G, cells, eq_id))
        tous_fermes = all(not _is_open(G, sid) for sid in oc_ligne)
        return _etat_depart(nb_sa, tous_fermes)

    courant = {eq: etat(eq, oc) for eq, oc in suivis}
    oc_par_depart = dict(suivis)

    transitions: list[TransitionDepart] = []
    for i, m in enumerate(manoeuvres):
        _set_switch(G, m.switch_id, m.action == "OPEN")
        for eq in switch_vers_departs.get(m.switch_id, ()):
            if eq not in courant:
                continue
            nouveau = etat(eq, oc_par_depart[eq])
            avant = courant[eq]
            if nouveau == avant:
                continue
            courant[eq] = nouveau
            regle = _TRANSITIONS_DEPART.get((avant, nouveau))
            if regle is None:
                transitions.append(TransitionDepart(
                    i, eq, avant, nouveau, interdite=False,
                    avertissement=f"transition étrange : « {avant.value} » → "
                                  f"« {nouveau.value} » (hors table CCO)"))
            else:
                interdite, avert, controles = regle
                transitions.append(TransitionDepart(
                    i, eq, avant, nouveau, interdite=interdite,
                    avertissement=avert, controles=list(controles)))
    return transitions


# ---------------------------------------------------------------------------
# Temporisations ACT 104 (R24)
# ---------------------------------------------------------------------------

#: Attente après toute manœuvre de sectionneur (mise à jour des schémas fantômes).
TEMPO_SECTIONNEUR_S = 10
#: Délai minimal entre deux fermetures d'un même DJ (regonflage après cycle O-F-O).
TEMPO_REGONFLAGE_DJ_S = 60


def calculer_temporisations(
    poste: PosteTopologique, manoeuvres: list[Manoeuvre]
) -> list[Temporisation]:
    """Temporisations à insérer dans la séquence (consigne ACT 104, sous-ensemble
    calculable depuis le seul graphe) :

    - **10 s après chaque manœuvre de sectionneur** ;
    - **60 s minimum entre deux fermetures d'un même disjoncteur** (regonflage
      après un cycle ouverture-fermeture) — le temps déjà écoulé en
      temporisations intermédiaires est décompté, comme dans la méthode CCO.

    Non calculables ici (contexte hors graphe, cf. R24 de la doc) : 2 min de
    passage de prises des régleurs (mise en service de transformateur neuf ou
    longuement consigné), 2 min entre manœuvres de sectionneurs d'un départ
    PSEM, 1 min entre mise hors tension et remise sous tension à vide d'une
    liaison 225/400 kV.
    """
    idx = _switch_edge_index(poste.graph)
    out: list[Temporisation] = []
    horloge = 0.0                       # temps simulé = somme des attentes
    derniere_fermeture: dict[str, float] = {}

    for i, m in enumerate(manoeuvres):
        edge = idx.get(m.switch_id)
        kind = poste.graph.edges[edge].get("kind") if edge else None

        if kind == SwitchKind.BREAKER and m.action == "CLOSE":
            prec = derniere_fermeture.get(m.switch_id)
            if prec is not None and horloge - prec < TEMPO_REGONFLAGE_DJ_S:
                attente = int(TEMPO_REGONFLAGE_DJ_S - (horloge - prec))
                out.append(Temporisation(
                    i, avant=True, duree_s=attente,
                    motif=f"regonflage du DJ {m.switch_id} : {TEMPO_REGONFLAGE_DJ_S} s "
                          "minimum entre deux fermetures (ACT 104)"))
                horloge += attente
            derniere_fermeture[m.switch_id] = horloge

        if kind == SwitchKind.DISCONNECTOR:
            out.append(Temporisation(
                i, avant=False, duree_s=TEMPO_SECTIONNEUR_S,
                motif="temporisation sectionneur : mise à jour des schémas "
                      "fantômes (ACT 104)"))
            horloge += TEMPO_SECTIONNEUR_S
    return out


# ---------------------------------------------------------------------------
# Point d'entrée
# ---------------------------------------------------------------------------

def analyser_conformite(
    poste: PosteTopologique, manoeuvres: list[Manoeuvre]
) -> ConformiteSequence:
    """Analyse de conformité « art de la manœuvre » d'une séquence complète :
    classification des conséquences (R20), matrice d'autorisation (R21), essai
    de barre (R22), suivi d'état des départs (R23), temporisations ACT 104
    (R24) et contrôles SCADA attendus (R25).

    Ne mute ni ``poste`` ni ``manoeuvres``. Complémentaire (et non redondant)
    des vérificateurs historiques : la règle du sectionneur hors charge (R18)
    reste portée par ``verification.py`` dans ``ecarts`` ; les verdicts produits
    ici alimentent le champ ``ResultatManoeuvres.conformite``.
    """
    classees = classifier_manoeuvres(poste, manoeuvres)
    violations, avertissements = verifier_matrice_autorisation(classees)
    transitions = suivre_etats_departs(poste, manoeuvres)
    for t in transitions:
        prefixe = (f"manœuvre {t.index + 1} — départ {t.equipment_id} : "
                   f"« {t.avant.value} » → « {t.apres.value} »")
        if t.interdite:
            violations.append(f"{prefixe} : {t.avertissement}")
        elif t.avertissement:
            avertissements.append(f"{prefixe} : {t.avertissement}")
    temporisations = calculer_temporisations(poste, manoeuvres)
    return ConformiteSequence(
        voltage_level_id=poste.voltage_level_id,
        classees=classees,
        transitions=transitions,
        temporisations=temporisations,
        violations=violations,
        avertissements=avertissements,
    )
