#!/usr/bin/env python3
"""
scripts/run_benchmark.py
-------------------------
Benchmark du séquenceur de manœuvres sur les **blocs de transition réels**
extraits du dataset RTE 7000 (phase 4 du plan ``docs/manoeuvre/dataset_rte7000/plan.md``).

Pour chaque bloc de ``<dataset>/blocs.jsonl`` (produit par
``scripts/build_rte7000_blocks.py``) dont la structure du poste est
disponible, le séquenceur est lancé via la **façade pluggable**
(``PlanificateurTopologie``, vérification indépendante) depuis l'état détaillé
de **départ** vers l'état détaillé **cible** du bloc, et comparé à :

- la **borne basse** : nombre d'organes différant entre départ et cible ;
- la **séquence réellement exécutée** par l'opérateur (``meta.
  nb_manoeuvres_observees``, résolution 5 min du dataset).

Sortie : un JSON de résultats par (bloc × algo × mode) + un résumé agrégé.

Exemple (structures extraites du premier instantané du jour traité) ::

    python scripts/run_benchmark.py \
        --dataset out_rte7000_20210103 \
        --structures-xiidm data/dgitt_rte7000_2021/2021/01/03/recollement-auto-20210103-0000-enrichi.xiidm.bz2 \
        --output out_rte7000_20210103/benchmark.json
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from expert_op4grid_recommender.manoeuvre.dataset import (   # noqa: E402
    couverture_structure,
    postes_depuis_xiidm,
)
from expert_op4grid_recommender.manoeuvre.plugins import (   # noqa: E402
    PlanificateurTopologie,
)
from expert_op4grid_recommender.manoeuvre.topologie import (  # noqa: E402
    PosteTopologique,
)

#: même seuil que build_rte7000_blocks : en deçà, la structure ne porte pas
#: les ids des données et le séquencement n'aurait pas de sens
SEUIL_COUVERTURE_STRUCTURE = 0.5


def _poste_dans_etat(base: PosteTopologique, etats: dict) -> PosteTopologique:
    """Le poste re-analysé dans l'état détaillé donné (le graphe de ``base``
    n'est jamais muté : copie avec les états appliqués)."""
    G = base.graph.copy()
    for _u, _v, d in G.edges(data=True):
        sid = d.get("switch_id")
        if sid in etats:
            d["open"] = bool(etats[sid])
    return PosteTopologique.from_graph(G, base.voltage_level_id)


def _executer(pipe: PlanificateurTopologie, poste: PosteTopologique,
              cible: dict, mode: str) -> dict:
    t0 = time.perf_counter()
    try:
        res = pipe.sequencer(poste, cible, mode=mode)
    except Exception as exc:                                # noqa: BLE001
        return {"statut": "erreur", "erreur": str(exc),
                "temps_s": round(time.perf_counter() - t0, 3)}
    return {
        "statut": "ok",
        "nb_manoeuvres": res.nb_manoeuvres,
        "is_verified": bool(res.is_verified),
        "is_verified_detaillee": bool(res.is_verified_detaillee),
        "nb_ecarts": len(res.ecarts),
        "nb_alertes": len(getattr(res, "alertes", []) or []),
        "noeuds_non_realisables": len(res.noeuds_non_realisables or []),
        "temps_s": round(time.perf_counter() - t0, 3),
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", type=pathlib.Path, required=True,
                    help="Dossier produit par build_rte7000_blocks.py "
                         "(contenant blocs.jsonl)")
    ap.add_argument("--blocs", type=pathlib.Path, default=None,
                    help="Fichier de blocs à benchmarker (défaut : "
                         "<dataset>/blocs.jsonl ; p. ex. un "
                         "blocs_combinaisons.jsonl de "
                         "build_combination_scenarios.py)")
    ap.add_argument("--structures-xiidm", type=pathlib.Path, required=True,
                    help="Instantané XIIDM d'où extraire les structures de "
                         "postes (ids cohérents avec les blocs)")
    ap.add_argument("--algo", default="libtopo",
                    help="Algorithme de phase B du registre (défaut : "
                         "%(default)s)")
    ap.add_argument("--modes", nargs="+", default=["smooth", "aggressive"])
    ap.add_argument("--vl", action="append", default=None,
                    help="Limiter à ce(s) poste(s) (répétable)")
    ap.add_argument("--min-organes", type=int, default=None,
                    help="Ne garder que les blocs changeant ≥ N organes")
    ap.add_argument("--tag", action="append", default=None,
                    help="Ne garder que les blocs portant ≥ 1 de ces tags "
                         "(répétable)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Plafond de blocs (essais)")
    ap.add_argument("--output", type=pathlib.Path, default=None,
                    help="Fichier JSON des résultats (défaut : "
                         "<dataset>/benchmark.json)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.WARNING)

    blocs_path = args.blocs or (args.dataset / "blocs.jsonl")
    if not blocs_path.exists():
        ap.error(f"{blocs_path} introuvable — lancer build_rte7000_blocks.py "
                 "d'abord")
    blocs = [json.loads(line)
             for line in blocs_path.read_text().splitlines() if line.strip()]
    if args.vl:
        blocs = [b for b in blocs if b["voltage_level_id"] in set(args.vl)]
    if args.min_organes:
        blocs = [b for b in blocs
                 if (b["meta"].get("nb_organes_changes") or 0) >= args.min_organes]
    if args.tag:
        voulus = set(args.tag)
        blocs = [b for b in blocs if voulus & set(b["meta"].get("tags", []))]
    if args.limit:
        blocs = blocs[: args.limit]

    vls = {b["voltage_level_id"] for b in blocs}
    postes, echecs = postes_depuis_xiidm(args.structures_xiidm, vls)
    print(f"{len(blocs)} bloc(s), {len(postes)} structure(s) construite(s), "
          f"{len(echecs)} échec(s) de structure")

    pipe = PlanificateurTopologie(planificateur=None, sequenceur=args.algo)
    resultats, ignores = [], 0
    for i, b in enumerate(blocs, 1):
        vl = b["voltage_level_id"]
        base = postes.get(vl)
        if base is None or couverture_structure(
                base, b["depart"]) < SEUIL_COUVERTURE_STRUCTURE:
            ignores += 1
            continue
        try:
            poste = _poste_dans_etat(base, b["depart"])
        except Exception as exc:                            # noqa: BLE001
            resultats.append({"bloc": b["name"], "voltage_level_id": vl,
                              "statut": "erreur_etat_depart", "erreur": str(exc)})
            continue
        ligne = {
            "bloc": b["name"],
            "voltage_level_id": vl,
            "tags": b["meta"].get("tags", []),
            "borne_basse": b["meta"].get("nb_organes_changes"),
            "nb_manoeuvres_observees": b["meta"].get("nb_manoeuvres_observees"),
            "algos": {},
        }
        for mode in args.modes:
            ligne["algos"][f"{args.algo}/{mode}"] = _executer(
                pipe, poste, b["cible"], mode)
        resultats.append(ligne)
        if i % 25 == 0:
            print(f"  … {i}/{len(blocs)} blocs")

    # ------------------------------------------------------------------
    # Résumé agrégé par algo/mode
    # ------------------------------------------------------------------
    resume: dict[str, dict] = {}
    for mode in args.modes:
        cle = f"{args.algo}/{mode}"
        lignes = [r for r in resultats if r.get("algos", {}).get(cle)]
        ok = [r for r in lignes if r["algos"][cle]["statut"] == "ok"]
        verifies = [r for r in ok if r["algos"][cle]["is_verified"]]
        exacts = [r for r in ok if r["algos"][cle]["is_verified_detaillee"]]
        ratios_bb = [r["algos"][cle]["nb_manoeuvres"] / r["borne_basse"]
                     for r in verifies if r.get("borne_basse")]
        ratios_obs = [r["algos"][cle]["nb_manoeuvres"]
                      / r["nb_manoeuvres_observees"]
                      for r in verifies if r.get("nb_manoeuvres_observees")]
        resume[cle] = {
            "blocs_executes": len(lignes),
            "erreurs": len(lignes) - len(ok),
            "partition_atteinte (is_verified)": len(verifies),
            "detaillee_exacte (is_verified_detaillee)": len(exacts),
            "manoeuvres/borne_basse (moy. sur vérifiés)":
                round(sum(ratios_bb) / len(ratios_bb), 2) if ratios_bb else None,
            "manoeuvres/observees (moy. sur vérifiés)":
                round(sum(ratios_obs) / len(ratios_obs), 2) if ratios_obs else None,
        }

    out = args.output or (args.dataset / "benchmark.json")
    out.write_text(json.dumps(
        {"algo": args.algo, "modes": args.modes,
         "nb_blocs_dataset": len(blocs), "blocs_sans_structure": ignores,
         "resume": resume, "resultats": resultats},
        indent=2, ensure_ascii=False))

    print(f"\n{ignores} bloc(s) sans structure compatible (ignorés)")
    for cle, agg in resume.items():
        print(f"[{cle}] " + ", ".join(f"{k}={v}" for k, v in agg.items()))
    print(f"→ résultats écrits dans {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
