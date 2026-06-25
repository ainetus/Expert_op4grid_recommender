"""
manoeuvre/plugins — Algorithmes pluggables pour les trois phases de calcul
==========================================================================

Permet de **plugger des algorithmes tiers** sur l'une, l'autre ou toutes les
phases du passage « topologie nodale cible → séquence de manœuvres » :

A. **Identification** : topologie nodale cible → topologie détaillée cible
   (contrat ``IdentificateurTopologieDetaillee``) ;
B. **Séquencement** : topologie détaillée cible → séquence de manœuvres
   (contrat ``SequenceurManoeuvres``) ;
C. **Planification bout-en-bout** : topologie nodale cible → séquence +
   topologie détaillée (contrat ``PlanificateurNodal``).

Les algorithmes natifs (portage libTOPO) sont enregistrés sous le nom
``"libtopo"`` pour chaque phase et restent le défaut.

Usage rapide
------------
>>> from expert_op4grid_recommender.manoeuvre.plugins import PlanificateurTopologie
>>> pipe = PlanificateurTopologie()                    # tout libTOPO
>>> plan = pipe.planifier(poste, topo_cible)           # nodale -> séquence + détaillée
>>> ident = pipe.identifier_topologie_detaillee(poste, topo_cible)   # phase A seule
>>> seq = pipe.sequencer(poste, ident.cible, mode="smooth")          # phase B seule

Plugger son algorithme (une phase suffit) ::

    from expert_op4grid_recommender.manoeuvre.plugins import register

    @register("sequenceur", "mon_algo")
    class MonSequenceur:
        nom = "mon_algo"
        def sequencer(self, poste, cible, **options):
            ...  # -> ResultatManoeuvres

    pipe = PlanificateurTopologie(sequenceur="mon_algo", planificateur=None)

Voir ``docs/architecture/plugins.md`` pour les contrats complets, la composition
des phases et la publication par entry points.
"""

from .interfaces import (
    CibleDetaillee,
    IdentificateurTopologieDetaillee,
    PlanificateurNodal,
    ResultatIdentification,
    ResultatPlanification,
    SequenceurManoeuvres,
)
from .registry import (
    ENTRY_POINT_GROUP,
    PHASES,
    disponibles,
    get,
    register,
)
from .pipeline import (
    PhaseNonConfiguree,
    PlanificateurTopologie,
    verifier_sequence,
)
# L'import enregistre les adaptateurs natifs "libtopo" dans le registre.
from .builtin import (
    LibTopoIdentificateur,
    LibTopoPlanificateur,
    LibTopoSequenceur,
)

__all__ = [
    # type pivot + résultats
    "CibleDetaillee", "ResultatIdentification", "ResultatPlanification",
    # contrats des phases
    "IdentificateurTopologieDetaillee", "SequenceurManoeuvres",
    "PlanificateurNodal",
    # registre
    "PHASES", "ENTRY_POINT_GROUP", "register", "get", "disponibles",
    # orchestration
    "PlanificateurTopologie", "verifier_sequence", "PhaseNonConfiguree",
    # implémentations natives
    "LibTopoIdentificateur", "LibTopoSequenceur", "LibTopoPlanificateur",
]
