#!/usr/bin/env python3
"""
scripts/build_rte7000_blocks.py
--------------------------------
Pipeline « dataset topologies historiques » (cf. docs/manoeuvre/dataset_rte7000/plan.md,
phases 1-4) : depuis un historique d'états d'organes par poste, ce script

1. reconstitue les chronologies de topologies détaillées par poste ;
2. détecte les **blocs de transition** (topologie détaillée de départ →
   topologie détaillée cible) avec l'évolution observée (états transitoires +
   manœuvres ordonnées) ;
3. **tague le type d'intervention** de chaque bloc (consignation, scission,
   fusion, ré-aiguillage, sectionnement…) — structurel si une fixture du
   poste est fournie, sinon repli par nommage des organes ;
4. écrit le dataset : ``blocs.jsonl``, scénarios (format
   ``tests/manoeuvre/scenarios``), séquences observées (format
   ``tests/manoeuvre/sequences``) et ``stats.json``.

Sur le dataset Hugging Face (à télécharger au préalable, cf.
``manoeuvre/dataset/dgitt.py``) ::

    hf download OpenSynth/D-GITT-RTE7000-2021 --repo-type dataset \
        --local-dir data/dgitt_rte7000_2021
    python scripts/build_rte7000_blocks.py \
        --input data/dgitt_rte7000_2021 \
        --fixtures tests/manoeuvre/fixtures \
        --output out_rte7000 [--vl CARRIP3 --vl MORBRP6] [--min-stabilite 2]

Mode **démo** (sans le dataset ni le réseau) : des chronologies sont
reconstruites depuis les séquences réelles du dépôt
(``tests/manoeuvre/sequences/*.json`` rejouées sur les fixtures) et le
pipeline complet tourne dessus ::

    python scripts/build_rte7000_blocks.py --demo --output out_demo
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from expert_op4grid_recommender.manoeuvre.dataset import (   # noqa: E402
    Snapshot,
    TimelinePoste,
    couverture_structure,
    ecrire_dataset,
    poste_from_fixture_json,
    postes_depuis_xiidm,
    stats_blocs,
    taguer_blocs,
)
from expert_op4grid_recommender.manoeuvre.dataset.extraction import (  # noqa: E402
    bloc_to_scenario,
)

REPO = pathlib.Path(__file__).resolve().parent.parent

#: en deçà de cette part d'organes du bloc connus de la structure, la
#: structure est jugée incompatible avec les données (autre convention d'ids,
#: autre version) et écartée — tagging par nommage à la place
SEUIL_COUVERTURE_STRUCTURE = 0.5


def _charger_postes(fixtures_dir: pathlib.Path, vls: set[str]) -> dict:
    """Structures de postes disponibles (fixture JSON par VL, nommage
    point/espace → underscore toléré)."""
    postes = {}
    if not fixtures_dir or not fixtures_dir.exists():
        return postes
    for vl in vls:
        for cand in (fixtures_dir / f"{vl}.json",
                     fixtures_dir / f"{vl.replace('.', '_').replace(' ', '_')}.json"):
            if cand.exists() and cand.stem != "index":
                try:
                    postes[vl] = poste_from_fixture_json(cand, vl)
                except Exception as exc:   # fixture incompatible : tag par nommage
                    print(f"  ! structure {vl} illisible ({exc}) — repli nommage")
                break
    return postes


def _premier_xiidm(input_dir: pathlib.Path) -> pathlib.Path | None:
    """Premier instantané XIIDM (chronologiquement) du dataset — la structure
    de référence des postes pour la période traitée."""
    from expert_op4grid_recommender.manoeuvre.dataset.dgitt import (
        _fichiers_xiidm, horodatage_depuis_nom,
    )
    dates = []
    for f in _fichiers_xiidm(input_dir):
        try:
            dates.append((horodatage_depuis_nom(f.name), f))
        except ValueError:
            continue
    return min(dates)[1] if dates else None


def _assembler_structures(
    vls: set[str],
    fixtures_dir: pathlib.Path,
    structures_xiidm: pathlib.Path | None,
    bloc_ref: dict,
) -> dict:
    """Structures des postes à blocs : d'abord depuis l'instantané XIIDM
    (ids garantis cohérents avec les données), puis fixtures JSON pour les VL
    restants. Une structure est écartée (repli nommage) si ses organes ne
    recouvrent pas ceux du bloc de référence (``couverture_structure`` <
    seuil) ou si la partition nodale n'est pas calculable dessus."""
    from expert_op4grid_recommender.manoeuvre.plugins import CibleDetaillee

    postes: dict = {}
    if structures_xiidm is not None:
        construits, echecs = postes_depuis_xiidm(structures_xiidm, vls)
        postes.update(construits)
        print(f"structures depuis {structures_xiidm.name} : "
              f"{len(construits)} construite(s), {len(echecs)} échec(s)")
    restants = vls - set(postes)
    if restants:
        postes.update(_charger_postes(fixtures_dir, restants))

    for vl in sorted(set(postes) & set(bloc_ref)):
        c = couverture_structure(postes[vl], bloc_ref[vl])
        if c < SEUIL_COUVERTURE_STRUCTURE:
            print(f"  ! structure {vl} incompatible avec les données "
                  f"(couverture {c:.0%}) — repli nommage")
            del postes[vl]
            continue
        try:    # sonde : partition nodale calculable (servira aux scénarios)
            CibleDetaillee(vl, bloc_ref[vl]).topologie_nodale(postes[vl])
        except Exception as exc:                            # noqa: BLE001
            print(f"  ! structure {vl} : partition nodale incalculable "
                  f"({exc}) — repli nommage")
            del postes[vl]
    return postes


def _timelines_demo():
    """Chronologies de démonstration : les séquences réelles du dépôt rejouées
    depuis leur état de départ (3 snapshots stables, puis un snapshot par
    manœuvre, puis 3 snapshots stables sur la cible)."""
    seq_dir = REPO / "tests" / "manoeuvre" / "sequences"
    for path in sorted(seq_dir.glob("*.json")):
        data = json.loads(path.read_text())
        vl = data["voltage_level_id"]
        depart = {k: bool(v) for k, v in data["depart"].items()}
        manos = sorted(data.get("manoeuvres", []),
                       key=lambda m: m.get("ordre", 0))
        if not manos:
            continue
        snaps, etat, t = [], dict(depart), 0

        def push(e):
            nonlocal t
            snaps.append(Snapshot(timestamp=f"2021-01-01T{t:04d}", etats=dict(e)))
            t += 1

        for _ in range(3):
            push(etat)
        for m in manos:
            etat[m["switch_id"]] = (m["action"] == "OPEN")
            push(etat)
        for _ in range(2):
            push(etat)
        yield path.stem, TimelinePoste(vl, snaps)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=pathlib.Path,
                    help="Dossier du dataset téléchargé : instantanés XIIDM "
                         "(*.xiidm.bz2, format réel RTE 7000) ou tabulaire "
                         "parquet/CSV (format auto-détecté)")
    ap.add_argument("--output", type=pathlib.Path, required=True)
    ap.add_argument("--fixtures", type=pathlib.Path,
                    default=REPO / "tests" / "manoeuvre" / "fixtures",
                    help="Dossier des structures de postes (fixtures JSON) "
                         "pour le tagging structurel")
    ap.add_argument("--structures-xiidm", type=pathlib.Path, default=None,
                    help="Instantané XIIDM d'où extraire les structures de "
                         "postes (ids cohérents avec les données). Défaut : "
                         "le premier instantané de --input quand il y en a")
    ap.add_argument("--vl", action="append", default=None,
                    help="Limiter à ce(s) poste(s) (répétable)")
    ap.add_argument("--min-stabilite", type=int, default=2,
                    help="Nb min de snapshots consécutifs d'un état stable")
    ap.add_argument("--sous-echantillon", type=int, default=None,
                    help="Ne garder qu'un instantané XIIDM sur N (allègement "
                         "mémoire/CPU ; perd la résolution fine des séquences)")
    ap.add_argument("--seuil-durable", type=int, default=None,
                    help="Plateau cible ≥ N snapshots → tag "
                         "reconfiguration_durable")
    ap.add_argument("--demo", action="store_true",
                    help="Chronologies reconstruites depuis les séquences du "
                         "dépôt (aucune donnée externe requise)")
    args = ap.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    structures_xiidm = args.structures_xiidm
    if args.demo:
        timelines = [(nom, tl) for nom, tl in _timelines_demo()]
    else:
        if not args.input:
            ap.error("--input requis (ou --demo)")
        from expert_op4grid_recommender.manoeuvre.dataset.dgitt import (
            charger_timelines,
        )
        vl_filter = set(args.vl) if args.vl else None
        timelines = [(tl.voltage_level_id, tl)
                     for tl in charger_timelines(
                         args.input, vl_filter,
                         sous_echantillon=args.sous_echantillon)]
        if structures_xiidm is None:
            structures_xiidm = _premier_xiidm(args.input)

    # Phase 1 — détection des blocs (les structures ne servent qu'au tagging :
    # on ne les construit que pour les postes ayant effectivement des blocs)
    # + catalogue des topologies distinctes (combinables en nouveaux
    # scénarios départ → cible, cf. scripts/build_combination_scenarios.py).
    tous_blocs, toutes_osc, noms_blocs, catalogue = [], [], [], []
    for nom, tl in timelines:
        blocs, osc = tl.detecter_blocs(min_stabilite=args.min_stabilite)
        tous_blocs.extend(blocs)
        toutes_osc.extend(osc)
        noms_blocs.extend(nom for _ in blocs)
        entrees = tl.catalogue(min_stabilite=args.min_stabilite)
        if sum(e.stable for e in entrees) >= 2:   # poste combinable
            catalogue.extend(entrees)
    nb_timelines = len(timelines)
    # Les chronologies complètes ne servent plus : les blocs portent leurs
    # bornes et transitoires. Libérer avant la phase structures (mémoire).
    del timelines

    # Phase 2 — structures des postes à blocs (XIIDM du dataset prioritaire,
    # fixtures en complément, garde de couverture d'identifiants).
    bloc_ref = {}
    for b in tous_blocs:
        bloc_ref.setdefault(b.voltage_level_id, b.etats_depart)
    # Construire des centaines de structures journalise un INFO par VL
    # (cellules/tronçons) : passer ces modules en WARNING le temps de la phase.
    for mod in ("cellules", "troncons", "topologie"):
        logging.getLogger(
            f"expert_op4grid_recommender.manoeuvre.{mod}").setLevel(
            logging.WARNING)
    postes = _assembler_structures(set(bloc_ref), args.fixtures,
                                   structures_xiidm, bloc_ref)
    print(f"{nb_timelines} chronologie(s), {len(bloc_ref)} poste(s) à blocs, "
          f"{len(postes)} structure(s) pour le tagging structurel")

    # Phase 3 — tagging.
    taguer_blocs(tous_blocs, postes, seuil_durable=args.seuil_durable)
    for nom, b in zip(noms_blocs, tous_blocs):
        print(f"  [{nom}] {b.resume()}")

    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    with (out / "blocs.jsonl").open("w") as f:
        for b in tous_blocs:
            f.write(json.dumps(bloc_to_scenario(
                b, postes.get(b.voltage_level_id)), ensure_ascii=False) + "\n")
    with (out / "topologies.jsonl").open("w") as f:
        for e in catalogue:
            f.write(json.dumps({
                "voltage_level_id": e.voltage_level_id,
                "topologie_id": e.topologie_id,
                "etats": e.etats,
                "premiere": e.premiere, "derniere": e.derniere,
                "nb_snapshots": e.nb_snapshots, "nb_episodes": e.nb_episodes,
                "stable": e.stable,
            }, ensure_ascii=False) + "\n")
    ecrire_dataset(tous_blocs, out, postes)
    stats = stats_blocs(tous_blocs)
    stats["oscillations_repliees"] = len(toutes_osc)
    vls_catalogue = {e.voltage_level_id for e in catalogue}
    stats["catalogue_topologies"] = {
        "postes_combinables": len(vls_catalogue),
        "topologies_distinctes": len(catalogue),
        "topologies_stables": sum(1 for e in catalogue if e.stable),
    }
    (out / "stats.json").write_text(json.dumps(stats, indent=2,
                                               ensure_ascii=False))
    print(f"\n→ {stats['nb_blocs']} bloc(s) ({stats['nb_postes']} poste(s)), "
          f"{stats['blocs_avec_sequence_observee']} avec séquence observée, "
          f"{len(toutes_osc)} oscillation(s) repliée(s)")
    print(f"→ catalogue : {len(catalogue)} topologie(s) distincte(s) sur "
          f"{len(vls_catalogue)} poste(s) combinable(s)")
    print(f"→ dataset écrit dans {out} (blocs.jsonl, topologies.jsonl, "
          f"scenarios/, sequences/, stats.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
