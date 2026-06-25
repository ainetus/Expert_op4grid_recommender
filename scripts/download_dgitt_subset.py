#!/usr/bin/env python3
"""
scripts/download_dgitt_subset.py
---------------------------------
Télécharge un sous-ensemble du dataset Hugging Face
``OpenSynth/D-GITT-RTE7000-*`` (instantanés XIIDM du réseau France, cf.
``docs/manoeuvre/dataset_rte7000/plan.md``) **sans dépendre du client ``hf``** :
listing par l'API ``/tree``, téléchargement HTTP simple via les URLs
``/resolve`` (redirigées vers le backend Xet ``cas-bridge.xethub.hf.co``),
vérification **md5** contre les fichiers jumeaux ``*.md5`` du dataset.

Reprise sur erreur : un fichier déjà présent et vérifié n'est pas retéléchargé.

Exemples ::

    # un jour complet (≈ 288 instantanés × 1,5 Mo)
    python scripts/download_dgitt_subset.py --prefix 2021/01/03 \
        --output data/dgitt_rte7000_2021

    # une plage horaire, en sous-échantillonnant 1 instantané sur 3 (15 min)
    python scripts/download_dgitt_subset.py --prefix 2021/01/03 --every 3 \
        --output data/dgitt_rte7000_2021

**Ne pas committer les données téléchargées** dans le dépôt.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

API = "https://huggingface.co/api/datasets/{repo}/tree/main/{prefix}"
RESOLVE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

#: codes HTTP transitoires (le CDN/rate-limiter HF rend des 403 épisodiques
#: sur les rafales anonymes — ils passent au retry)
_RETRIABLE = {403, 429, 500, 502, 503, 504}


def _http_get(url: str, timeout: int = 120, essais: int = 5) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dgitt-subset/1.0"})
    for essai in range(essais):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code not in _RETRIABLE or essai == essais - 1:
                raise
            time.sleep(2 ** essai + random.uniform(0, 1))
        except (urllib.error.URLError, TimeoutError):
            if essai == essais - 1:
                raise
            time.sleep(2 ** essai + random.uniform(0, 1))
    raise AssertionError("unreachable")


def lister_fichiers(repo: str, prefix: str) -> list[str]:
    """Chemins des ``*.xiidm.bz2`` sous ``prefix`` (API tree, paginée)."""
    fichiers: list[str] = []
    url = API.format(repo=repo, prefix=urllib.parse.quote(prefix))
    while url:
        req = urllib.request.Request(url, headers={"User-Agent": "dgitt-subset/1.0"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            items = json.load(resp)
            link = resp.headers.get("Link", "")
        fichiers += [it["path"] for it in items
                     if it.get("type") == "file"
                     and it["path"].endswith(".xiidm.bz2")]
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return sorted(fichiers)


def _md5_attendu(repo: str, path: str) -> str | None:
    """Digest md5 publié dans le fichier jumeau ``<path>.md5`` (None si absent)."""
    try:
        contenu = _http_get(RESOLVE.format(repo=repo, path=urllib.parse.quote(path + ".md5")),
                            timeout=30).decode("ascii", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    m = re.search(r"\b([0-9a-fA-F]{32})\b", contenu)
    return m.group(1).lower() if m else None


def _md5_fichier(p: pathlib.Path) -> str:
    h = hashlib.md5()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def telecharger_un(repo: str, path: str, racine: pathlib.Path) -> tuple[str, str]:
    """Télécharge ``path`` sous ``racine`` (arborescence préservée) et le
    vérifie ; retourne ``(path, statut)`` avec statut ∈ {ok, déjà, sans-md5}."""
    dest = racine / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    attendu = _md5_attendu(repo, path)
    if dest.exists() and attendu and _md5_fichier(dest) == attendu:
        return path, "déjà"
    octets = _http_get(RESOLVE.format(repo=repo, path=urllib.parse.quote(path)))
    if attendu:
        obtenu = hashlib.md5(octets).hexdigest()
        if obtenu != attendu:
            raise IOError(f"md5 invalide pour {path} : {obtenu} ≠ {attendu}")
    dest.write_bytes(octets)
    return path, ("ok" if attendu else "sans-md5")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default="OpenSynth/D-GITT-RTE7000-2021",
                    help="Dataset HF (défaut : %(default)s)")
    ap.add_argument("--prefix", action="append", required=True,
                    help="Préfixe d'arborescence à télécharger, ex. 2021/01/03 "
                         "(répétable)")
    ap.add_argument("--output", type=pathlib.Path, required=True,
                    help="Racine locale (l'arborescence du dataset y est préservée)")
    ap.add_argument("--every", type=int, default=None,
                    help="Ne garder qu'un instantané sur N (sous-échantillonnage)")
    ap.add_argument("--limit", type=int, default=None,
                    help="Plafond de fichiers (essais)")
    ap.add_argument("--jobs", type=int, default=4,
                    help="Téléchargements parallèles (défaut : %(default)s)")
    args = ap.parse_args()

    fichiers: list[str] = []
    for prefix in args.prefix:
        fichiers += lister_fichiers(args.repo, prefix.strip("/"))
    if args.every and args.every > 1:
        fichiers = fichiers[::args.every]
    if args.limit:
        fichiers = fichiers[:args.limit]
    print(f"{len(fichiers)} fichier(s) à télécharger vers {args.output}")

    statuts: dict[str, int] = {}
    echecs: list[str] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        futs = {pool.submit(telecharger_un, args.repo, p, args.output): p
                for p in fichiers}
        for i, fut in enumerate(as_completed(futs), 1):
            p = futs[fut]
            try:
                _, st = fut.result()
                statuts[st] = statuts.get(st, 0) + 1
            except Exception as exc:                       # noqa: BLE001
                echecs.append(p)
                print(f"  ✗ {p} : {exc}", file=sys.stderr)
            if i % 25 == 0 or i == len(fichiers):
                print(f"  … {i}/{len(fichiers)} ({statuts})")

    if echecs:
        print(f"\n{len(echecs)} échec(s) — relancer la commande pour reprendre.",
              file=sys.stderr)
        return 1
    print(f"\n→ terminé : {statuts}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
