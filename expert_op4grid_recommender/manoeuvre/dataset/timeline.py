"""
manoeuvre/dataset/timeline.py — Chronologie de topologies détaillées d'un poste
et détection des blocs de transition.

Concepts (cf. ``docs/manoeuvre/dataset_rte7000/plan.md``, phases 1-2) :

- **Snapshot** : l'état détaillé d'un poste à un instant
  (``{switch_id: ouvert ?}`` — la forme de ``CibleDetaillee``) ;
- **état stable** : une topologie observée pendant au moins ``min_stabilite``
  snapshots consécutifs (filtre anti-bruit de télésignalisation) ;
- **bloc de transition** : intervalle entre deux états stables *différents* —
  topologie détaillée de **départ** → topologie détaillée **cible** — avec les
  états **transitoires** observés entre les deux (l'évolution réelle de la
  topologie, dont on dérive les **manœuvres observées** ordonnées) ;
- **oscillation** : retour à la même topologie stable après un épisode bref
  (A → bruit → A) — replié, consigné à part, jamais émis comme bloc.

Les horodatages sont des chaînes ISO 8601 (l'ordre lexicographique est
l'ordre temporel) ; tout type totalement ordonné convient.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterable, Optional


def topologie_id(etats: dict[str, bool]) -> str:
    """Identifiant **canonique** d'une topologie détaillée : hash stable des
    paires ``(switch_id, ouvert)`` triées. Deux états identiques ont le même
    id quel que soit l'ordre d'insertion."""
    h = hashlib.sha1()
    for sid in sorted(etats):
        h.update(sid.encode())
        h.update(b"1" if etats[sid] else b"0")
    return h.hexdigest()[:16]


@dataclass(frozen=True)
class Snapshot:
    """État détaillé d'un poste à un instant donné."""
    timestamp: str
    etats: dict[str, bool]   # switch_id -> True si OUVERT

    @property
    def topologie_id(self) -> str:
        return topologie_id(self.etats)


@dataclass
class Oscillation:
    """Épisode bref replié : la topologie stable est retrouvée à l'identique
    après ``nb_snapshots`` instants de bruit (déclenchement/réenclenchement,
    télésignalisation instable…)."""
    voltage_level_id: str
    t_debut: str
    t_fin: str
    nb_snapshots: int
    topologie_stable_id: str


@dataclass
class BlocTransition:
    """Transition d'un poste entre deux topologies détaillées **stables**.

    ``etats_depart`` / ``etats_cible`` sont les bornes stables ;
    ``transitoires`` les états intermédiaires observés (ordonnés) et
    ``manoeuvres_observees`` les bascules d'organes dérivées des diffs entre
    snapshots consécutifs (de la borne de départ à la borne cible incluses) —
    c'est la **séquence réelle approchée** à la résolution de l'historique.
    """
    voltage_level_id: str
    t_depart: str            # dernier instant de l'état stable de départ
    t_cible: str             # premier instant de l'état stable cible
    etats_depart: dict[str, bool]
    etats_cible: dict[str, bool]
    transitoires: list[Snapshot] = field(default_factory=list)
    manoeuvres_observees: list[dict] = field(default_factory=list)
    #: longueurs (en snapshots) des plateaux stables encadrant la transition
    duree_stable_avant: int = 0
    duree_stable_apres: int = 0
    #: la topologie de départ est-elle revue plus tard dans la chronologie ?
    retour_observe: bool = False
    #: types d'intervention (remplis par ``tagging.taguer_bloc``)
    tags: list[str] = field(default_factory=list)

    @property
    def topologie_depart_id(self) -> str:
        return topologie_id(self.etats_depart)

    @property
    def topologie_cible_id(self) -> str:
        return topologie_id(self.etats_cible)

    def diff(self) -> dict[str, tuple[Optional[bool], Optional[bool]]]:
        """Organes différant entre départ et cible : ``{sid: (avant, après)}``."""
        out: dict[str, tuple[Optional[bool], Optional[bool]]] = {}
        for sid in set(self.etats_depart) | set(self.etats_cible):
            a, b = self.etats_depart.get(sid), self.etats_cible.get(sid)
            if a != b:
                out[sid] = (a, b)
        return out

    @property
    def nb_organes_changes(self) -> int:
        return len(self.diff())

    def resume(self) -> str:
        return (f"Bloc {self.voltage_level_id} {self.t_depart} → {self.t_cible} : "
                f"{self.nb_organes_changes} organe(s), "
                f"{len(self.manoeuvres_observees)} manœuvre(s) observée(s), "
                f"tags={self.tags or ['-']}")


def _manoeuvres_entre(snaps: list[Snapshot]) -> list[dict]:
    """Bascules d'organes observées entre snapshots consécutifs (ordonnées)."""
    out: list[dict] = []
    for prev, cur in zip(snaps, snaps[1:]):
        for sid in sorted(set(prev.etats) | set(cur.etats)):
            a, b = prev.etats.get(sid), cur.etats.get(sid)
            if a != b and b is not None:
                out.append({"timestamp": cur.timestamp, "switch_id": sid,
                            "action": "OPEN" if b else "CLOSE"})
    return out


@dataclass
class _Run:
    tid: str
    debut: int   # index du premier snapshot du plateau
    fin: int     # index du dernier snapshot du plateau

    @property
    def longueur(self) -> int:
        return self.fin - self.debut + 1


class TimelinePoste:
    """Chronologie des états détaillés d'un poste (snapshots triés par
    horodatage), et détection des blocs de transition."""

    def __init__(self, voltage_level_id: str, snapshots: Iterable[Snapshot]):
        self.voltage_level_id = voltage_level_id
        self.snapshots: list[Snapshot] = sorted(snapshots,
                                                key=lambda s: s.timestamp)

    # ------------------------------------------------------------------

    def _runs(self) -> list[_Run]:
        """Plateaux de topologie identique consécutive (compression RLE)."""
        runs: list[_Run] = []
        for i, s in enumerate(self.snapshots):
            tid = s.topologie_id
            if runs and runs[-1].tid == tid:
                runs[-1].fin = i
            else:
                runs.append(_Run(tid, i, i))
        return runs

    def detecter_blocs(
        self,
        min_stabilite: int = 2,
    ) -> tuple[list[BlocTransition], list[Oscillation]]:
        """Détecte les blocs de transition entre états **stables**
        (plateaux ≥ ``min_stabilite`` snapshots).

        - les plateaux courts entre deux états stables différents sont les
          **transitoires** du bloc (évolution observée) ;
        - deux plateaux stables **identiques** séparés de bruit sont fusionnés
          et l'épisode est consigné en ``Oscillation`` (pas de bloc) ;
        - les plateaux instables en tête/queue de chronologie sont ignorés
          (pas de borne stable).

        Retourne ``(blocs, oscillations)``.
        """
        snaps = self.snapshots
        stables = [r for r in self._runs() if r.longueur >= min_stabilite]
        if len(stables) < 2:
            return [], []

        # Fusion des plateaux stables identiques consécutifs (oscillations).
        merged: list[_Run] = []
        oscillations: list[Oscillation] = []
        for r in stables:
            if merged and merged[-1].tid == r.tid:
                prev = merged[-1]
                oscillations.append(Oscillation(
                    voltage_level_id=self.voltage_level_id,
                    t_debut=snaps[prev.fin].timestamp,
                    t_fin=snaps[r.debut].timestamp,
                    nb_snapshots=r.debut - prev.fin - 1,
                    topologie_stable_id=r.tid,
                ))
                prev.fin = r.fin
            else:
                merged.append(_Run(r.tid, r.debut, r.fin))

        # tids des plateaux stables situés strictement APRÈS chaque plateau k
        # (pour la réversibilité : la topologie de départ est-elle revue ?).
        tids_apres: list[set] = [set() for _ in merged]
        vus: set = set()
        for k in range(len(merged) - 1, -1, -1):
            tids_apres[k] = set(vus)
            vus.add(merged[k].tid)

        blocs: list[BlocTransition] = []
        for k, (a, b) in enumerate(zip(merged, merged[1:])):
            fenetre = snaps[a.fin:b.debut + 1]     # bornes stables incluses
            blocs.append(BlocTransition(
                voltage_level_id=self.voltage_level_id,
                t_depart=snaps[a.fin].timestamp,
                t_cible=snaps[b.debut].timestamp,
                etats_depart=dict(snaps[a.fin].etats),
                etats_cible=dict(snaps[b.debut].etats),
                transitoires=list(fenetre[1:-1]),
                manoeuvres_observees=_manoeuvres_entre(fenetre),
                duree_stable_avant=a.longueur,
                duree_stable_apres=b.longueur,
                retour_observe=a.tid in tids_apres[k],
            ))
        return blocs, oscillations

    # ------------------------------------------------------------------

    def topologies_rencontrees(self) -> dict[str, int]:
        """Topologies distinctes rencontrées : ``{topologie_id: nb_snapshots}``."""
        out: dict[str, int] = {}
        for s in self.snapshots:
            out[s.topologie_id] = out.get(s.topologie_id, 0) + 1
        return out

    def catalogue(self, min_stabilite: int = 2) -> list["TopologieRencontree"]:
        """Catalogue **dédoublonné** des topologies détaillées rencontrées
        (phase 1 du plan) : pour chaque topologie distincte, son état d'organes,
        ses occurrences (snapshots, épisodes/plateaux, première/dernière vue)
        et si elle a été **stable** (≥ 1 plateau de ``min_stabilite`` snapshots).

        Les topologies stables d'un poste sont les états réels combinables en
        nouveaux scénarios « topologie de départ → topologie cible »
        (transitions jamais observées, potentiellement plus longues que les
        blocs réels). Trié par nb de snapshots décroissant."""
        snaps = self.snapshots
        entrees: dict[str, TopologieRencontree] = {}
        for r in self._runs():
            tid = r.tid
            e = entrees.get(tid)
            if e is None:
                e = entrees[tid] = TopologieRencontree(
                    voltage_level_id=self.voltage_level_id,
                    topologie_id=tid,
                    etats=snaps[r.debut].etats,
                    premiere=snaps[r.debut].timestamp,
                    derniere=snaps[r.fin].timestamp,
                )
            e.nb_snapshots += r.longueur
            e.nb_episodes += 1
            e.derniere = snaps[r.fin].timestamp
            if r.longueur >= min_stabilite:
                e.stable = True
        return sorted(entrees.values(), key=lambda e: -e.nb_snapshots)


@dataclass
class TopologieRencontree:
    """Une topologie détaillée distincte d'un poste, avec ses occurrences
    (entrée du catalogue de la phase 1)."""
    voltage_level_id: str
    topologie_id: str
    etats: dict[str, bool]
    premiere: str
    derniere: str
    nb_snapshots: int = 0
    nb_episodes: int = 0
    stable: bool = False
