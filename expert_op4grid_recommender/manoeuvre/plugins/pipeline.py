"""
manoeuvre/plugins/pipeline.py — Orchestrateur des trois phases pluggables.

``PlanificateurTopologie`` est la **façade** que les consommateurs (IHM,
recommandeur, notebooks) appellent ; elle :

- résout chaque phase (A identification, B séquencement, C planification
  bout-en-bout) vers un algorithme du registre — par défaut ``"libtopo"`` ;
- **compose** les phases manquantes : sans planificateur (C), elle enchaîne
  identification (A) puis séquencement (B) ; sans identificateur (A), elle
  dérive la cible détaillée du planificateur (C) par rejeu ;
- applique une **vérification indépendante** uniforme après chaque calcul
  (``verifier_sequence``) : rejeu de la séquence, partition nodale atteinte,
  écarts détaillés, règles de sûreté du sectionneur, alertes « un seul ouvrage
  hors tension ». Les algorithmes tiers n'ont donc pas à être crus sur parole,
  et bénéficient des mêmes verdicts que l'implémentation native.
"""
from __future__ import annotations

from typing import Optional, Union

import networkx as nx

from ..topologie import PosteTopologique, TopologieNodale
from ..algo.results import ResultatManoeuvres
from ..algo.graph_ops import _set_switch, _switch_edge_index, _wired_busbar
from ..algo.conformite import analyser_conformite
from ..algo.verification import (
    _verifier_regles,
    ouvrages_simultanement_hors_tension,
)
from ..algo.targets import _ecarts_detailles
from .interfaces import (
    CibleDetaillee,
    IdentificateurTopologieDetaillee,
    PlanificateurNodal,
    ResultatIdentification,
    ResultatPlanification,
    SequenceurManoeuvres,
)
from . import registry


class PhaseNonConfiguree(RuntimeError):
    """Aucun algorithme (ni composition possible) pour la phase demandée."""


CibleLike = Union[CibleDetaillee, nx.Graph, dict]


# ---------------------------------------------------------------------------
# Vérification indépendante (commune à tous les algorithmes)
# ---------------------------------------------------------------------------

def _cible_busbar_depuis_graph(
    poste: PosteTopologique, cible_graph: nx.Graph,
) -> dict[str, int]:
    """Barre cible câblée de chaque départ dans le graphe cible."""
    cible_busbar: dict[str, int] = {}
    for c in poste.cellules.cellules_depart:
        for eq in {c.equipment_id} | set(c.shared_equipment_ids):
            bb = _wired_busbar(c, cible_graph)
            if bb is not None:
                cible_busbar[eq] = bb
    return cible_busbar


def verifier_sequence(
    poste: PosteTopologique,
    res: ResultatManoeuvres,
    topo_cible: Optional[TopologieNodale] = None,
    cible: Optional[CibleDetaillee] = None,
    mode: Optional[str] = None,
) -> ResultatManoeuvres:
    """Vérification **indépendante** d'une séquence de manœuvres (quel que soit
    l'algorithme qui l'a produite) : recalcule, par rejeu sur une copie du
    graphe du poste, les champs de verdict de ``res`` :

    - ``topo_obtenue`` / ``is_verified`` (partition nodale vs ``topo_cible``) ;
    - ``ecarts`` : organes inconnus du poste, écarts détaillés vs ``cible``
      (si fournie), règles de sûreté du sectionneur ;
    - ``is_verified_detaillee`` (si ``cible`` est fournie) ;
    - ``alertes`` « un seul ouvrage hors tension à la fois » (sauf
      ``mode="aggressive"``, qui dé-énergise en lot par construction) ;
    - ``conformite`` : analyse « art de la manœuvre » (classification des
      conséquences, matrice d'autorisation CCRT, états des départs,
      temporisations ACT 104, contrôles SCADA attendus — R20-R25). Champ
      **séparé** d'``ecarts``/``alertes`` (verdicts historiques inchangés).

    Mute ``res`` (et le retourne). Ne mute jamais ``poste``.
    """
    vl = poste.voltage_level_id
    G = poste.graph.copy()
    idx = _switch_edge_index(G)
    inconnus = sorted({m.switch_id for m in res.manoeuvres
                       if m.switch_id not in idx})
    for m in res.manoeuvres:
        _set_switch(G, m.switch_id, m.action == "OPEN")

    res.topo_obtenue = TopologieNodale.from_graph(G, vl)
    res.is_changed = bool(res.manoeuvres)
    if topo_cible is None and cible is not None:
        topo_cible = cible.topologie_nodale(poste)
    if topo_cible is not None:
        res.topo_cible = topo_cible
        res.is_verified = topo_cible.meme_topologie(res.topo_obtenue)

    ecarts = [f"organe inconnu du poste : '{sid}'" for sid in inconnus]
    if cible is not None:
        cible_graph = cible.to_graph(poste)
        ecarts = (_ecarts_detailles(poste, G, cible_graph,
                                    _cible_busbar_depuis_graph(poste, cible_graph))
                  + ecarts)
    ecarts += _verifier_regles(poste, res.manoeuvres, un_seul=False)
    res.ecarts = ecarts
    if cible is not None:
        res.is_verified_detaillee = res.is_verified and not ecarts

    if mode != "aggressive":
        res.alertes = ouvrages_simultanement_hors_tension(poste, res.manoeuvres)

    res.conformite = analyser_conformite(poste, res.manoeuvres)

    if not res.message:
        res.message = ("Topologie cible atteinte et vérifiée." if res.is_verified
                       else "Topologie cible non atteinte.")
    return res


# ---------------------------------------------------------------------------
# Façade d'orchestration
# ---------------------------------------------------------------------------

class PlanificateurTopologie:
    """Façade orchestrant les trois phases pluggables.

    Parameters
    ----------
    identificateur, sequenceur, planificateur :
        Pour chaque phase : un **nom** d'algorithme du registre (``str``), une
        **instance** respectant le contrat de la phase, ou ``None`` pour
        désactiver la phase (l'orchestrateur compose alors avec les autres).
        Défaut : ``"libtopo"`` partout.
    verification_independante :
        Si vrai (défaut), chaque résultat est repassé par
        ``verifier_sequence`` — les verdicts (``is_verified``, ``ecarts``,
        ``alertes``…) sont recalculés par l'orchestrateur, indépendamment de
        ce que déclare l'algorithme.

    Exemples
    --------
    Tout libTOPO (défaut)::

        pipe = PlanificateurTopologie()
        plan = pipe.planifier(poste, topo_cible)

    Plugger seulement son séquenceur (identification libTOPO conservée)::

        pipe = PlanificateurTopologie(sequenceur=MonSequenceur(),
                                      planificateur=None)

    Plugger un planificateur bout-en-bout enregistré::

        pipe = PlanificateurTopologie(planificateur="mon_algo")
    """

    def __init__(
        self,
        identificateur: Union[str, IdentificateurTopologieDetaillee, None] = "libtopo",
        sequenceur: Union[str, SequenceurManoeuvres, None] = "libtopo",
        planificateur: Union[str, PlanificateurNodal, None] = "libtopo",
        verification_independante: bool = True,
    ):
        self.identificateur = self._resoudre("identificateur", identificateur)
        self.sequenceur = self._resoudre("sequenceur", sequenceur)
        self.planificateur = self._resoudre("planificateur", planificateur)
        self.verification_independante = verification_independante

    @staticmethod
    def _resoudre(phase: str, algo):
        if algo is None or not isinstance(algo, str):
            return algo
        return registry.get(phase, algo)

    @staticmethod
    def _normaliser_cible(poste: PosteTopologique, cible: CibleLike) -> CibleDetaillee:
        if isinstance(cible, CibleDetaillee):
            return cible
        if isinstance(cible, nx.Graph):
            return CibleDetaillee.from_graph(cible, poste.voltage_level_id)
        if isinstance(cible, dict):
            return CibleDetaillee(voltage_level_id=poste.voltage_level_id,
                                  etats_organes={k: bool(v) for k, v in cible.items()})
        raise TypeError(
            f"Cible détaillée inattendue : {type(cible).__name__} "
            "(attendu CibleDetaillee, nx.Graph ou dict[switch_id, bool]).")

    # ------------------------------------------------------------------
    # Phase A — topologie nodale cible -> topologie détaillée cible
    # ------------------------------------------------------------------

    def identifier_topologie_detaillee(
        self,
        poste: PosteTopologique,
        topo_cible: TopologieNodale,
        **options,
    ) -> ResultatIdentification:
        """Identifie une topologie détaillée réalisant ``topo_cible``.

        Utilise l'identificateur configuré ; à défaut, dérive la cible du
        planificateur bout-en-bout (rejeu de sa séquence). ``is_realisable``
        est recalculé indépendamment de la déclaration de l'algorithme.
        """
        if self.identificateur is not None:
            res = self.identificateur.identifier(poste, topo_cible, **options)
        elif self.planificateur is not None:
            plan = self.planificateur.planifier(poste, topo_cible, **options)
            cible = plan.cible_detaillee or CibleDetaillee.from_manoeuvres(
                poste, plan.sequence.manoeuvres)
            res = ResultatIdentification(
                voltage_level_id=poste.voltage_level_id,
                cible=cible,
                is_realisable=plan.sequence.is_verified,
                message=plan.sequence.message,
                noeuds_non_realisables=plan.sequence.noeuds_non_realisables,
                sequence=plan.sequence,
            )
        else:
            raise PhaseNonConfiguree(
                "Ni identificateur ni planificateur configurés : impossible "
                "d'identifier une topologie détaillée.")

        if self.verification_independante and res.cible is not None:
            realise = res.cible.topologie_nodale(poste).meme_topologie(topo_cible)
            if realise != res.is_realisable:
                res.message = (
                    f"[vérification indépendante : cible détaillée "
                    f"{'réalise' if realise else 'NE réalise PAS'} la partition "
                    f"nodale visée, contrairement à la déclaration de "
                    f"l'algorithme] " + res.message)
            res.is_realisable = realise
        return res

    # ------------------------------------------------------------------
    # Phase B — topologie détaillée cible -> séquence de manœuvres
    # ------------------------------------------------------------------

    def sequencer(
        self,
        poste: PosteTopologique,
        cible: CibleLike,
        **options,
    ) -> ResultatManoeuvres:
        """Calcule la séquence de manœuvres vers une topologie détaillée cible
        (``CibleDetaillee``, graphe node/breaker, ou dict ``switch_id ->
        ouvert ?``)."""
        if self.sequenceur is None:
            raise PhaseNonConfiguree("Aucun séquenceur configuré.")
        cible = self._normaliser_cible(poste, cible)
        res = self.sequenceur.sequencer(poste, cible, **options)
        if self.verification_independante:
            verifier_sequence(poste, res, cible=cible,
                              mode=options.get("mode"))
        return res

    # ------------------------------------------------------------------
    # Phase C — topologie nodale cible -> séquence + détaillée
    # ------------------------------------------------------------------

    def planifier(
        self,
        poste: PosteTopologique,
        topo_cible: TopologieNodale,
        **options,
    ) -> ResultatPlanification:
        """Depuis une topologie **nodale** cible, calcule la séquence de
        manœuvres et la topologie détaillée atteinte.

        Voie directe si un planificateur (phase C) est configuré ; sinon
        **composition** : identification (A) puis séquencement (B) — en
        réutilisant la séquence sous-produit de l'identification si aucun
        séquenceur n'est configuré.
        """
        if self.planificateur is not None:
            plan = self.planificateur.planifier(poste, topo_cible, **options)
            if plan.cible_detaillee is None:
                plan.cible_detaillee = CibleDetaillee.from_manoeuvres(
                    poste, plan.sequence.manoeuvres)
        else:
            ident = self.identifier_topologie_detaillee(
                poste, topo_cible, **options)
            if ident.cible is None:
                seq = ResultatManoeuvres(
                    voltage_level_id=poste.voltage_level_id,
                    topo_initiale=poste.topologie_nodale,
                    topo_cible=topo_cible,
                    message=ident.message
                    or "Identification de topologie détaillée impossible.",
                )
                return ResultatPlanification(cible_detaillee=None, sequence=seq)
            if self.sequenceur is not None:
                seq = self.sequenceur.sequencer(poste, ident.cible, **options)
            elif ident.sequence is not None:
                seq = ident.sequence
            else:
                raise PhaseNonConfiguree(
                    "Aucun séquenceur configuré et l'identificateur ne "
                    "fournit pas de séquence.")
            plan = ResultatPlanification(cible_detaillee=ident.cible,
                                         sequence=seq)

        if self.verification_independante:
            verifier_sequence(poste, plan.sequence, topo_cible=topo_cible,
                              cible=plan.cible_detaillee,
                              mode=options.get("mode"))
        return plan
