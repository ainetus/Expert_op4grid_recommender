"""
manoeuvre/algo/  —  Phase 2 : algorithme nodale → détaillée
---------------------------------------------------------------
Calcule la séquence d'organes de coupure (OC) à manœuvrer pour passer de
l'état détaillé courant d'un poste à une topologie nodale cible.

Ce package éclate l'ancien module monolithique ``algo.py`` en sous-modules en
couches (dépendances strictement descendantes, sans cycle) :

    results       — structures de sortie (Manoeuvre, ResultatManoeuvres)
    graph_ops     — helpers bas niveau (index O(1), lecture/écriture d'organes,
                    chemins structurels, couplers inter-SJB)
    placement     — placement nœud → sections de barres (phases 2.2-2.4)
    verification  — vérificateurs de la règle du sectionneur (rejeu unique)
    conformite    — conformité « art de la manœuvre » (R20-R25) : conséquences,
                    matrice d'autorisation CCRT, états des départs,
                    temporisations ACT 104, contrôles SCADA attendus
    sequencing    — séquenceur général (couplage + sectionnement) + ré-aiguillage
    targets       — points d'entrée (cible nodale / cible détaillée)

Le package **réexporte** toute la surface de l'ancien module (points d'entrée
publics et symboles privés), de sorte que ``from ...manoeuvre.algo import X`` et
``manoeuvre.algo.X`` restent inchangés pour tous les appelants.

Correspondance C++ : ``Topologie::determineTopoCompleteCible`` (TOPOPoste.cc:3944)
et ses sous-routines (``connectAndDeconnectOuvrageHS``, ``evalueEtatCouplage``,
``identifySuperTronconnement``, ``getTronconnementBesoinReaiguillage2barres``,
``reaiguillage2barres``, ``listeDordre``).

Point d'entrée unique
~~~~~~~~~~~~~~~~~~~~~~~
``determiner_topo_complete_cible(poste, topo_cible)`` intègre toute la chaîne :
faisabilité → **placement automatique** nœud→sections de barres
(``_placement_automatique``) → séquenceur général
(``determiner_manoeuvres_avec_sections``) → vérification. Il gère
indifféremment 1 barre, 2 barres, et la **création de nœuds supplémentaires**
par ouverture de sectionnement, sans placement explicite.

Couverture
~~~~~~~~~~
- Postes 1 barre et 2 barres standard (couplage + ré-aiguillage boucle
  courte/longue).                                                        [OK]
- Création d'un nœud au-delà du nombre de barres par **ouverture de
  sectionnement de barre** (dé-énergisation préalable de la section).    [OK]
- Ordonnancement type ``listeDordre`` : fermeture des couplages d'abord,
  ré-aiguillages boucle courte, ouverture des sectionnements (hors tension),
  ouverture des couplages, ré-aiguillages boucle longue, puis suppression
  des manœuvres sans effet.                                              [OK]
- Contrôle de court-circuit avant fermeture d'un couplage.               [OK]
- Vérification post-manœuvre (recalcul de la topologie nodale).          [OK]

Règle du sectionneur de barre
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Un sectionneur de barre ne se manœuvre que hors charge. Pour scinder une barre
en deux nœuds, la section à isoler est d'abord mise hors tension (ses départs
ré-aiguillés sur l'autre barre en boucle courte), puis le sectionnement est
ouvert, puis les départs du nouveau nœud y sont ré-aiguillés en boucle longue.

Postes multi-sections : les postes à plusieurs sections par barre (ex. CARRIP6,
2 barres × 3 sections = 6 SJB) sont gérés (ouverture de sectionnements avec
garage temporaire des départs sur une SJB équipotentielle).

Dégradation gracieuse : si une étape n'est pas réalisable en sécurité (pas de
SJB tampon, départ inatteignable…), l'algorithme **ne s'interrompt pas** : il
consigne les **écarts** et poursuit ; ``topo_obtenue`` est toujours renseignée.

Postes à > 2 jeux de barres : le placement nodal combinatoire est conçu pour le
double jeu de barres. Sur un poste à plus de 2 jeux de barres (niveaux
supplémentaires 3B/4B, organes internes à 2 bornes type self/réactance, nœuds à
0 barre), ``determiner_manoeuvres_cible_detaillee`` route vers
``_sequence_detaillee_multibarres`` qui **dérive le placement des composantes
connexes du graphe cible** (groupes exacts), laisse en place les organes à 2
bornes (``organes_fixes``), **isole** les nœuds à 0 barre, et détecte **tous les
couplages parallèles** (``_inter_sjb_couplers``). L'API nodale-only
``determiner_topo_complete_cible`` conserve un fallback : scoping aux 2 JdB
principaux + diagnostic + placement partiel best-effort.

Limites connues (documentées, cf. doc C++) :
- Ré-aiguillage d'omnibus complexes (départs multiples scindés)          [partiel]
- Contrôle de court-circuit : vérifie l'équipotentialité courante avant
  manœuvre de sectionneur (pas de calcul de potentiel/déphasage fin)     [simplifié]
- Topologies de couplers non chaînées (≥ 3 barres en anneau)             [partiel]
- Nœuds mêlant départs connectés et déconnectés                          [partiel]
- Postes > 2 jeux de barres : placement par composantes du graphe cible
  (chemin détaillé) ; fallback nodal-only = scoping aux 2 JdB            [OK/partiel]
"""
from __future__ import annotations

# Surface héritée d'``algo.py`` (avant son éclatement en sous-modules) : le
# package réexporte exactement ce qui était importable depuis ``manoeuvre.algo``
# — points d'entrée publics **et** symboles privés utilisés par les tests / l'IHM.
# Les ré-exports privés utilisent la forme ``X as X`` (re-export explicite, PEP 484)
# pour rester importables sans être signalés « inutilisés ».
from .results import (
    Manoeuvre as Manoeuvre,
    ResultatManoeuvres as ResultatManoeuvres,
)
from .graph_ops import (
    _switch_edge_index as _switch_edge_index,
    _equipment_node_index as _equipment_node_index,
    _set_switch as _set_switch,
    _is_open as _is_open,
    _eq_node as _eq_node,
    _edges_of_switches as _edges_of_switches,
    _sa_path_to_sjb as _sa_path_to_sjb,
    _wired_busbar as _wired_busbar,
    _own_breakers_to_sjb as _own_breakers_to_sjb,
    _wired_sjbs as _wired_sjbs,
    _organes_internes_2bornes as _organes_internes_2bornes,
    _live_graph_sans as _live_graph_sans,
    _ouvrages_energises_sur as _ouvrages_energises_sur,
    _meme_noeud_hors_cellule as _meme_noeud_hors_cellule,
    _InterSjbCoupler as _InterSjbCoupler,
    _inter_sjb_couplers as _inter_sjb_couplers,
)
from .placement import (
    _assignations_connexes as _assignations_connexes,
    _main_busbar_sjb as _main_busbar_sjb,
    _scoping_raison as _scoping_raison,
    _message_non_realisable as _message_non_realisable,
    _diagnostic_infaisabilite as _diagnostic_infaisabilite,
    _placement_greedy as _placement_greedy,
    _placement_best_effort as _placement_best_effort,
    _placement_automatique as _placement_automatique,
    _placement_avec_reconnexions as _placement_avec_reconnexions,
    _departure_dj_changes as _departure_dj_changes,
)
from .verification import (
    _rejeu_securite as _rejeu_securite,
    _verifier_securite_sectionneurs as _verifier_securite_sectionneurs,
    _verifier_un_seul_hors_tension as _verifier_un_seul_hors_tension,
    ouvrages_simultanement_hors_tension as ouvrages_simultanement_hors_tension,
    sectionneurs_sous_charge_par_manoeuvre as sectionneurs_sous_charge_par_manoeuvre,
    _verifier_regles as _verifier_regles,
    _verifier_sectionneurs_hors_charge as _verifier_sectionneurs_hors_charge,
    _optimiser_sequence as _optimiser_sequence,
    _sectionneurs_sous_charge_par_manoeuvre as _sectionneurs_sous_charge_par_manoeuvre,
)
from .conformite import (
    FamilleOrgane as FamilleOrgane,
    Consequence as Consequence,
    EtatDepart as EtatDepart,
    ManoeuvreClassee as ManoeuvreClassee,
    TransitionDepart as TransitionDepart,
    Temporisation as Temporisation,
    ConformiteSequence as ConformiteSequence,
    familles_organes as familles_organes,
    classifier_manoeuvres as classifier_manoeuvres,
    verifier_matrice_autorisation as verifier_matrice_autorisation,
    suivre_etats_departs as suivre_etats_departs,
    calculer_temporisations as calculer_temporisations,
    analyser_conformite as analyser_conformite,
)
from .sequencing import (
    _reaiguiller_vers_sjb as _reaiguiller_vers_sjb,
    _isoler_depart_hors_barre as _isoler_depart_hors_barre,
    _appliquer_changements_dj as _appliquer_changements_dj,
    _consigner_non_realisables as _consigner_non_realisables,
    determiner_manoeuvres_avec_sections as determiner_manoeuvres_avec_sections,
)
from .targets import (
    _ecarts_detailles as _ecarts_detailles,
    _sequence_detaillee_aggressive as _sequence_detaillee_aggressive,
    _sequence_detaillee_multibarres as _sequence_detaillee_multibarres,
    determiner_topo_complete_cible as determiner_topo_complete_cible,
    determiner_manoeuvres_cible_detaillee as determiner_manoeuvres_cible_detaillee,
)

__all__ = [
    "Manoeuvre",
    "ResultatManoeuvres",
    "determiner_topo_complete_cible",
    "determiner_manoeuvres_avec_sections",
    "determiner_manoeuvres_cible_detaillee",
    "sectionneurs_sous_charge_par_manoeuvre",
    "ouvrages_simultanement_hors_tension",
    # Conformité « art de la manœuvre » (R20-R25)
    "FamilleOrgane",
    "Consequence",
    "EtatDepart",
    "ManoeuvreClassee",
    "TransitionDepart",
    "Temporisation",
    "ConformiteSequence",
    "familles_organes",
    "classifier_manoeuvres",
    "verifier_matrice_autorisation",
    "suivre_etats_departs",
    "calculer_temporisations",
    "analyser_conformite",
]
