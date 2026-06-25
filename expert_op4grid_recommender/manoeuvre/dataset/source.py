"""
manoeuvre/dataset/source.py — Source « instantané par date » du dataset
=======================================================================

Liste et télécharge **à la demande** un instantané XIIDM du dataset Hugging Face
``OpenSynth/D-GITT-RTE7000-*`` (réseau France, 1 instantané toutes les 5 min ;
arborescence ``YYYY/MM/JJ/recollement-auto-YYYYMMDD-HHMM-enrichi.xiidm.bz2``)
pour une **date** et une **heure** données, **sans dépendre du client ``hf``** :
listing par l'API ``/tree``, téléchargement HTTP via les URLs ``/resolve``,
vérification md5 contre les fichiers jumeaux ``*.md5``. Stdlib pur.

C'est la brique qui branche l'IHM de manœuvre (``scripts/manoeuvre_ihm.py``) sur
le dataset : choisir une date → relever les voltage levels de l'instantané →
charger la topologie d'un poste à cette date/heure. Conçu pour un Space
HuggingFace (accès internet sortant vers ``huggingface.co``), avec cache local
``cache_dir`` (reprise : un instantané déjà vérifié n'est pas retéléchargé).

Les helpers HTTP dupliquent volontairement ceux de
``scripts/download_dgitt_subset.py`` (qui reste **autonome**, sans dépendre du
package) ; ils convergent sur le même schéma d'URL et la même vérification md5.

Un ``HF_TOKEN`` (jeton de lecture) est optionnel : utile pour desserrer le
rate-limit anonyme du CDN HF.
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from .dgitt import _charger_reseau, horodatage_depuis_nom

#: dataset par défaut (année 2021 ; voir docs/manoeuvre/dataset_rte7000/)
REPO_DEFAUT = "OpenSynth/D-GITT-RTE7000-2021"

#: dates **identifiées intéressantes** (journées traitées dans
#: docs/manoeuvre/dataset_rte7000/ — « Table de campagne », 3 ans 2021-2023) — proposées
#: comme accès rapide dans l'IHM. Toute autre date reste saisissable. L'année
#: d'une date détermine le repo HuggingFace (cf. ``repo_pour_date``).
DATES_ECHANTILLON: tuple[str, ...] = (
    "2021-01-03", "2021-01-05", "2021-04-14", "2021-07-15", "2021-10-12",
    "2022-06-15", "2023-02-08",
)


def repo_pour_date(repo_base: str, date_iso: str) -> str:
    """Repo HuggingFace à interroger pour la date ``date_iso``.

    Le dataset D-GITT-RTE7000 existe par année (``…-2021`` / ``…-2022`` /
    ``…-2023``). Si ``repo_base`` se termine par une année (``…-YYYY``), on la
    remplace par l'année de ``date_iso`` ; sinon ``repo_base`` est renvoyé tel
    quel (configuration mono-repo / repo personnalisé). Fonction pure."""
    m = re.match(r"^(.*-)(\d{4})$", repo_base or "")
    y = re.match(r"^\s*(\d{4})-", date_iso or "")
    if m and y:
        return f"{m.group(1)}{y.group(1)}"
    return repo_base

API = "https://huggingface.co/api/datasets/{repo}/tree/main/{prefix}"
RESOLVE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"

#: codes HTTP transitoires (le CDN/rate-limiter HF rend des 403 épisodiques sur
#: les rafales anonymes — ils passent au retry)
_RETRIABLE = {403, 429, 500, 502, 503, 504}


# ===========================================================================
# Couche HTTP (isolée pour le mock en test)
# ===========================================================================

def _entetes(token: Optional[str] = None) -> dict:
    """En-têtes HTTP (User-Agent + Authorization si un jeton est fourni)."""
    en = {"User-Agent": "dgitt-subset/1.0"}
    if token:
        en["Authorization"] = f"Bearer {token}"
    return en


def _http_get(url: str, timeout: int = 120, essais: int = 5,
              token: Optional[str] = None) -> bytes:
    """GET avec retries exponentiels sur les codes transitoires."""
    req = urllib.request.Request(url, headers=_entetes(token))
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


def lister_fichiers(repo: str, prefix: str,
                    token: Optional[str] = None) -> list[str]:
    """Chemins des ``*.xiidm.bz2`` sous ``prefix`` (API tree, paginée), triés."""
    fichiers: list[str] = []
    url = API.format(repo=repo, prefix=urllib.parse.quote(prefix))
    while url:
        req = urllib.request.Request(url, headers=_entetes(token))
        with urllib.request.urlopen(req, timeout=60) as resp:
            items = json.load(resp)
            link = resp.headers.get("Link", "")
        fichiers += [it["path"] for it in items
                     if it.get("type") == "file"
                     and it["path"].endswith(".xiidm.bz2")]
        m = re.search(r'<([^>]+)>;\s*rel="next"', link)
        url = m.group(1) if m else None
    return sorted(fichiers)


def _md5_attendu(repo: str, path: str,
                 token: Optional[str] = None) -> Optional[str]:
    """Digest md5 publié dans le fichier jumeau ``<path>.md5`` (None si absent)."""
    try:
        contenu = _http_get(
            RESOLVE.format(repo=repo, path=urllib.parse.quote(path + ".md5")),
            timeout=30, token=token).decode("ascii", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise
    m = re.search(r"\b([0-9a-fA-F]{32})\b", contenu)
    return m.group(1).lower() if m else None


def _md5_fichier(p: Path) -> str:
    """Digest md5 d'un fichier local (lecture par blocs)."""
    h = hashlib.md5()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def telecharger_un(repo: str, path: str, racine: Path,
                   token: Optional[str] = None) -> Path:
    """Télécharge ``path`` sous ``racine`` (arborescence préservée) et le vérifie
    (md5). Reprise : si le fichier local existe et concorde, pas de re-téléchargement.
    Retourne le chemin local."""
    dest = Path(racine) / path
    dest.parent.mkdir(parents=True, exist_ok=True)
    attendu = _md5_attendu(repo, path, token=token)
    if dest.exists() and attendu and _md5_fichier(dest) == attendu:
        return dest
    octets = _http_get(RESOLVE.format(repo=repo, path=urllib.parse.quote(path)),
                       token=token)
    if attendu:
        obtenu = hashlib.md5(octets).hexdigest()
        if obtenu != attendu:
            raise IOError(f"md5 invalide pour {path} : {obtenu} ≠ {attendu}")
    dest.write_bytes(octets)
    return dest


# ===========================================================================
# Résolution par date / heure
# ===========================================================================

def prefixe_jour(date_iso: str) -> str:
    """``'2021-01-03'`` → ``'2021/01/03'`` (préfixe d'arborescence du dataset).

    Lève ``ValueError`` si la date n'est pas au format ``YYYY-MM-DD``."""
    m = re.match(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$", date_iso or "")
    if not m:
        raise ValueError(f"Date attendue au format YYYY-MM-DD : {date_iso!r}")
    return "/".join(m.groups())


def _minutes(hhmm: str) -> int:
    """``'HH:MM'`` → minutes depuis minuit (tolère ``'HHhMM'`` / ``'HHMM'``)."""
    m = re.match(r"^\s*(\d{1,2})[h:]?(\d{2})\s*$", hhmm or "")
    if not m:
        return 12 * 60
    return int(m.group(1)) * 60 + int(m.group(2))


def lister_instantanes(repo: str, date_iso: str,
                       token: Optional[str] = None) -> list[dict]:
    """Instantanés disponibles pour ``date_iso`` :
    ``[{'ts': 'HH:MM', 'iso': 'YYYY-MM-DDTHH:MM', 'path': <hf_path>}, …]`` trié
    par horodatage croissant. Liste vide si la journée est absente du dataset."""
    out: list[dict] = []
    for path in lister_fichiers(repo, prefixe_jour(date_iso), token=token):
        try:
            iso = horodatage_depuis_nom(Path(path).name)
        except ValueError:
            continue
        out.append({"ts": iso[11:16], "iso": iso, "path": path})
    out.sort(key=lambda d: d["iso"])
    return out


def choisir_instantane(instantanes: list[dict],
                       heure: str = "12:00") -> Optional[dict]:
    """Instantané le plus proche de ``heure`` (HH:MM). ``None`` si liste vide.

    Par défaut **midi** : sur un poste calme, l'instantané de mi-journée est
    représentatif ; l'utilisateur peut viser n'importe quel horodatage."""
    if not instantanes:
        return None
    cible = _minutes(heure)
    return min(instantanes, key=lambda d: abs(_minutes(d["ts"]) - cible))


def telecharger_instantane(repo: str, hf_path: str, cache_dir,
                           token: Optional[str] = None) -> Path:
    """Télécharge l'instantané ``hf_path`` dans ``cache_dir`` (repris si déjà
    présent et vérifié). Retourne le chemin local."""
    return telecharger_un(repo, hf_path, Path(cache_dir), token=token)


def resoudre_et_telecharger(repo: str, date_iso: str, cache_dir,
                            heure: str = "12:00",
                            token: Optional[str] = None) -> tuple[Path, dict]:
    """Liste la journée → choisit l'instantané le plus proche de ``heure`` →
    le télécharge dans ``cache_dir``. Retourne ``(chemin_local, meta)`` où
    ``meta = {'date', 'ts', 'iso', 'path'}``.

    Lève ``FileNotFoundError`` si aucun instantané n'existe pour cette date."""
    insts = lister_instantanes(repo, date_iso, token=token)
    choisi = choisir_instantane(insts, heure)
    if choisi is None:
        raise FileNotFoundError(
            f"Aucun instantané pour {date_iso} dans {repo}.")
    local = telecharger_instantane(repo, choisi["path"], cache_dir, token=token)
    return local, {"date": date_iso, **choisi}


def charger_situation(repo: str, date_iso: str, cache_dir,
                      heure: str = "12:00",
                      token: Optional[str] = None):
    """Bout-en-bout : résout + télécharge l'instantané (date/heure) puis le
    **charge en réseau pypowsybl**. Retourne ``(net, meta)`` avec
    ``meta = {'date', 'ts', 'iso', 'path', 'local'}``.

    pypowsybl n'est importé que par ``_charger_reseau`` (paresseux)."""
    local, meta = resoudre_et_telecharger(repo, date_iso, cache_dir,
                                          heure=heure, token=token)
    net = _charger_reseau(local)
    meta["local"] = str(local)
    return net, meta
