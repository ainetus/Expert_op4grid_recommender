"""
manoeuvre/dataset — Construction du dataset « topologies historiques »
=======================================================================

Outillage du plan ``docs/manoeuvre/dataset_rte7000/plan.md`` (phases 1 à 4) : à partir
d'un **historique d'états d'organes par poste** (p. ex. le dataset
Hugging Face ``OpenSynth/D-GITT-RTE7000-2021``), ce package :

1. reconstitue la **chronologie de topologies détaillées** de chaque poste
   (``timeline.TimelinePoste``) ;
2. détecte les **blocs de transition** — les moments où la topologie change —
   entre deux états stables : topologie détaillée de **départ** → topologie
   détaillée **cible**, avec l'**évolution observée** (états intermédiaires et
   manœuvres ordonnées) pendant la transition (``timeline.detecter_blocs``) ;
3. **tague le type d'intervention** de chaque bloc (``tagging.taguer_bloc`` :
   consignation, remise en service, scission/fusion de nœud, ré-aiguillage,
   sectionnement de barre…) ;
4. **extrait** chaque bloc en scénario/séquence aux formats du dépôt
   (``extraction`` → ``tests/manoeuvre/scenarios`` / ``sequences``), prêt pour
   le benchmark du séquenceur via la façade pluggable.

Point d'entrée en ligne de commande : ``scripts/build_rte7000_blocks.py``
(mode ``--demo`` exécutable sans le dataset, sur les fixtures + séquences
réelles du dépôt).

Le cœur (timeline / tagging / extraction) est en Python pur (networkx pour le
tagging structurel) ; l'adaptateur de lecture du dataset (``dgitt``) charge
pandas paresseusement.
"""

from .timeline import (
    Snapshot,
    BlocTransition,
    Oscillation,
    TimelinePoste,
    TopologieRencontree,
    topologie_id,
)
from .tagging import taguer_bloc, taguer_blocs
from .extraction import (
    bloc_to_scenario,
    bloc_to_sequence_observee,
    ecrire_dataset,
    generer_combinaisons,
    stats_blocs,
)
from .structure import (
    couverture_structure,
    graph_from_fixture_json,
    poste_from_fixture_json,
    postes_depuis_xiidm,
)
from .source import (
    DATES_ECHANTILLON,
    REPO_DEFAUT,
    charger_situation,
    choisir_instantane,
    lister_instantanes,
    prefixe_jour,
    resoudre_et_telecharger,
    telecharger_instantane,
)

__all__ = [
    "Snapshot", "BlocTransition", "Oscillation", "TimelinePoste",
    "TopologieRencontree", "topologie_id",
    "taguer_bloc", "taguer_blocs",
    "bloc_to_scenario", "bloc_to_sequence_observee", "ecrire_dataset",
    "generer_combinaisons", "stats_blocs",
    "couverture_structure", "graph_from_fixture_json",
    "poste_from_fixture_json", "postes_depuis_xiidm",
    "DATES_ECHANTILLON", "REPO_DEFAUT", "charger_situation",
    "choisir_instantane", "lister_instantanes", "prefixe_jour",
    "resoudre_et_telecharger", "telecharger_instantane",
]
