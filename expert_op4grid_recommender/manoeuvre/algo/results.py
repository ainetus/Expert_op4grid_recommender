"""
manoeuvre/algo/results.py — Structures de sortie de l'algorithme (manœuvre unitaire et résultat).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Optional

from ..topologie import TopologieNodale

if TYPE_CHECKING:  # import différé (conformite dépend de results)
    from .conformite import ConformiteSequence


@dataclass
class Manoeuvre:
    """Une opération unitaire sur un organe de coupure."""
    switch_id: str
    action: Literal["OPEN", "CLOSE"]
    raison: str
    type_boucle: Optional[Literal["COURTE", "LONGUE"]] = None

    def __repr__(self) -> str:  # pragma: no cover - debug
        b = f" [{self.type_boucle}]" if self.type_boucle else ""
        return f"{self.action:5s} {self.switch_id}  ({self.raison}){b}"


@dataclass
class ResultatManoeuvres:
    """Résultat complet de l'algorithme."""
    voltage_level_id: str
    topo_initiale: TopologieNodale
    topo_cible: TopologieNodale
    manoeuvres: list[Manoeuvre] = field(default_factory=list)
    departs_reaiguilles: set[str] = field(default_factory=set)
    couplages_modifies: list[str] = field(default_factory=list)
    is_changed: bool = False
    is_verified: bool = False              # topologie NODALE atteinte
    is_verified_detaillee: bool = False    # topologie DÉTAILLÉE atteinte
    ecarts: list[str] = field(default_factory=list)  # écarts détaillés résiduels
    # Alertes de **bonne pratique** (non bloquantes) : p.ex. mode smooth ayant plus
    # d'un ouvrage temporairement hors tension à la fois (cf.
    # ``ouvrages_simultanement_hors_tension``). N'affecte pas ``is_verified*``.
    alertes: list[str] = field(default_factory=list)
    topo_obtenue: Optional[TopologieNodale] = None
    message: str = ""
    # Dégradation gracieuse : départs des nœuds cibles **non réalisables** sur ce
    # poste (cible partiellement atteinte, à compléter manuellement par l'opérateur).
    noeuds_non_realisables: list[list[str]] = field(default_factory=list)
    # Analyse de conformité « art de la manœuvre » (classification des
    # conséquences, matrice d'autorisation CCRT, états des départs,
    # temporisations ACT 104, contrôles attendus). Renseignée par
    # ``plugins.pipeline.verifier_sequence`` ; indépendante d'``ecarts`` /
    # ``alertes`` (compatibilité des goldens).
    conformite: Optional["ConformiteSequence"] = None

    @property
    def nb_manoeuvres(self) -> int:
        return len(self.manoeuvres)

    def resume(self) -> str:
        lines = [
            f"Manœuvres VL '{self.voltage_level_id}' : {self.nb_manoeuvres} OC, "
            f"changed={self.is_changed}, verified={self.is_verified}"
        ]
        for i, m in enumerate(self.manoeuvres, 1):
            lines.append(f"  {i:2d}. {m!r}")
        if self.message:
            lines.append(f"  -> {self.message}")
        return "\n".join(lines)
