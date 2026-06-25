"""
manoeuvre/dataset/dgitt.py — Adaptateur de lecture du dataset
``OpenSynth/D-GITT-RTE7000-2021`` (Hugging Face) vers les chronologies du
pipeline (``TimelinePoste``).

Schéma réel du dataset (inspecté le 2026-06-10, cf.
``docs/manoeuvre/dataset_rte7000/plan.md`` § « État d'avancement ») :

- **un fichier = un instantané** complet du réseau français en topologie
  *node-breaker*, au format **XIIDM** compressé **bzip2**, lisible par
  pypowsybl ;
- arborescence ``2021/MM/JJ/recollement-auto-YYYYMMDD-HHMM-enrichi.xiidm.bz2``
  (+ un ``.md5`` jumeau, ignoré), **pas de temps 5 min** ;
- les **états d'organes** (DJ *et* SA — ``get_switches`` retourne BREAKER et
  DISCONNECTOR) sont présents ; les identifiants d'organes sont stables dans le
  temps (cf. README du dataset).

L'adaptateur lit donc chaque instantané XIIDM, en extrait l'état détaillé
``{switch_id: ouvert ?}`` **par voltage level** (``get_switches``), horodate
depuis le nom de fichier, et reconstitue les ``TimelinePoste``. C'est le
**chemin par défaut** (auto-détecté dès qu'un ``*.xiidm`` est présent).

Un **chemin tabulaire** générique est conservé en repli (format **long** —
une ligne = un état/changement d'organe : ``(horodatage, poste, organe,
ouvert ?)`` — avec auto-détection des colonnes ``_ALIASES``), au cas où une
ré-exportation parquet/CSV du dataset serait fournie.

Téléchargement du dataset (hors de ce module — nécessite l'accès réseau à
``huggingface.co`` **et** au backend de stockage **Xet**
``*.xethub.hf.co``) ::

    pip install -U huggingface_hub
    hf download OpenSynth/D-GITT-RTE7000-2021 --repo-type dataset \
        --include "2021/01/03/*.xiidm.bz2" \
        --local-dir data/dgitt_rte7000_2021

puis ::

    python scripts/build_rte7000_blocks.py --input data/dgitt_rte7000_2021 ...

**Ne pas committer les données brutes** dans le dépôt.
"""
from __future__ import annotations

import bz2
import gzip
import logging
import os
import re
import sys
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

from .timeline import Snapshot, TimelinePoste

logger = logging.getLogger(__name__)


# ===========================================================================
# Chemin XIIDM (format réel du dataset RTE 7000) — chemin par défaut
# ===========================================================================

#: horodatage RTE dans les noms de fichiers : ``…-YYYYMMDD-HHMM-…``
_MOTIF_HORODATAGE = re.compile(r"(\d{8})[-_]?(\d{4})(?:\D|$)")


def horodatage_depuis_nom(
    nom: str, motif: re.Pattern = _MOTIF_HORODATAGE,
) -> str:
    """Horodatage ISO 8601 (``YYYY-MM-DDTHH:MM``) extrait du nom de fichier.

    L'ordre lexicographique de la chaîne retournée est l'ordre temporel.
    Lève ``ValueError`` si aucun motif date+heure n'est reconnu (ou si la
    date/heure est invalide)."""
    m = motif.search(nom)
    if not m:
        raise ValueError(
            f"Nom de fichier non horodaté : {nom!r} "
            f"(motif attendu : …YYYYMMDD-HHMM…). Passez un autre `motif` si "
            "le nommage diffère.")
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M")
    except ValueError as exc:
        raise ValueError(
            f"Date/heure invalide dans {nom!r} : {exc}") from exc
    return dt.strftime("%Y-%m-%dT%H:%M")


#: extensions reconnues d'un instantané (compressé ou non) — un suffixe
#: surnuméraire (``.md5`` jumeau du dataset, ``.lock``/``.metadata``/
#: ``.incomplete`` du cache ``hf download``…) disqualifie le fichier
_EXTENSIONS_XIIDM = tuple(
    base + comp
    for base in (".xiidm", ".iidm")
    for comp in ("", ".bz2", ".gz"))


def _est_xiidm(p: Path) -> bool:
    """Le fichier est-il un instantané XIIDM/IIDM (compressé ou non) ?"""
    return p.name.lower().endswith(_EXTENSIONS_XIIDM)


def _a_des_xiidm(input_dir: Path) -> bool:
    """Y a-t-il au moins un instantané XIIDM sous ``input_dir`` ? (court-circuit
    au premier trouvé — peu coûteux même sur de très gros dossiers)."""
    for p in Path(input_dir).rglob("*"):
        if p.is_file() and _est_xiidm(p):
            return True
    return False


def _fichiers_xiidm(input_dir: Path) -> list[Path]:
    """Tous les instantanés XIIDM sous ``input_dir`` (récursif, ``.md5`` exclus)."""
    return [p for p in Path(input_dir).rglob("*")
            if p.is_file() and _est_xiidm(p)]


def _octets_decompresses(path: Path) -> bytes:
    """Contenu décompressé d'un instantané (bzip2/gzip/non compressé)."""
    n = path.name.lower()
    if n.endswith(".bz2"):
        with bz2.open(path, "rb") as fh:
            return fh.read()
    if n.endswith(".gz"):
        with gzip.open(path, "rb") as fh:
            return fh.read()
    return path.read_bytes()


def _charger_reseau(path: Path):
    """Charge un instantané XIIDM avec pypowsybl (décompression transparente).

    pypowsybl déduit le format de l'extension : pour un fichier compressé, on
    décompresse vers un ``.xiidm`` temporaire (bzip2 n'est pas géré nativement),
    chargé puis supprimé."""
    import pypowsybl as pp   # paresseux : seul le chemin XIIDM dépend de pypowsybl

    n = path.name.lower()
    if not (n.endswith(".bz2") or n.endswith(".gz")):
        return pp.network.load(str(path))

    tmp = tempfile.NamedTemporaryFile(suffix=".xiidm", delete=False)
    try:
        tmp.write(_octets_decompresses(path))
        tmp.close()
        return pp.network.load(tmp.name)
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


def _etats_switches_par_vl(
    net, vl_filter: Optional[set[str]] = None,
) -> dict[str, dict[str, bool]]:
    """État détaillé ``{switch_id: ouvert ?}`` par voltage level d'un réseau.

    Utilise ``get_switches(all_attributes=True)`` (colonnes ``voltage_level_id``
    et ``open``, index = id d'organe). ``vl_filter`` restreint aux postes voulus.

    Les identifiants (VL et organes) sont **internés** (``sys.intern``) : ils
    se répètent à l'identique d'un instantané à l'autre — sur une journée du
    réseau France (288 instantanés × ~86 000 organes), l'interning évite des
    gigaoctets de chaînes dupliquées."""
    sw = net.get_switches(all_attributes=True)
    for col in ("voltage_level_id", "open"):
        if col not in sw.columns:
            raise ValueError(
                f"Colonne '{col}' absente de get_switches() — version de "
                f"pypowsybl ? Colonnes : {sorted(map(str, sw.columns))}")
    if vl_filter:
        sw = sw[sw["voltage_level_id"].isin(vl_filter)]
    out: dict[str, dict[str, bool]] = {}
    for vl, grp in sw.groupby("voltage_level_id", sort=False):
        out[sys.intern(str(vl))] = {sys.intern(str(sid)): bool(o)
                                    for sid, o in zip(grp.index, grp["open"])}
    return out


def charger_timelines_xiidm(
    input_dir: Path,
    vl_filter: Optional[set[str]] = None,
    sous_echantillon: Optional[int] = None,
    motif_horodatage: re.Pattern = _MOTIF_HORODATAGE,
    sur_erreur: str = "lever",
) -> Iterator[TimelinePoste]:
    """Itère les ``TimelinePoste`` reconstruites depuis des instantanés XIIDM.

    Chaque instantané (un fichier ``*.xiidm[.bz2/.gz]``) est chargé via
    pypowsybl ; ses organes sont extraits **par poste** et horodatés depuis le
    nom de fichier. Les snapshots d'un même poste sont assemblés en chronologie.

    Paramètres
    ----------
    vl_filter:
        restreint aux voltage levels donnés (recommandé : charger 7000 postes
        sur de longues périodes est coûteux en mémoire — cf. plan, stockage
        parquet incrémental hors de ce module).
    sous_echantillon:
        ne garder qu'un instantané sur ``N`` (allègement ; perd la résolution
        fine des séquences observées).
    sur_erreur:
        ``"lever"`` (défaut) propage toute erreur d'horodatage/lecture ;
        ``"ignorer"`` saute l'instantané fautif (en le journalisant).
    """
    fichiers = _fichiers_xiidm(input_dir)
    if not fichiers:
        raise FileNotFoundError(
            f"Aucun instantané XIIDM (*.xiidm[.bz2/.gz]) sous {input_dir} — "
            "le dataset est-il téléchargé ? (cf. docstring du module).")

    horodates: list[tuple[str, Path]] = []
    for f in fichiers:
        try:
            horodates.append((horodatage_depuis_nom(f.name, motif_horodatage), f))
        except ValueError:
            if sur_erreur != "ignorer":
                raise
            logger.warning("Nom non horodaté, ignoré : %s", f)
    horodates.sort(key=lambda t: t[0])
    if sous_echantillon and sous_echantillon > 1:
        horodates = horodates[::sous_echantillon]

    par_vl: dict[str, list[Snapshot]] = defaultdict(list)
    derniers: dict[str, dict[str, bool]] = {}
    for i, (ts, f) in enumerate(horodates):
        try:
            net = _charger_reseau(f)
            etats = _etats_switches_par_vl(net, vl_filter)
        except Exception as exc:                      # noqa: BLE001
            if sur_erreur != "ignorer":
                raise
            logger.warning("Instantané illisible, ignoré (%s) : %s", exc, f)
            continue
        for vl, e in etats.items():
            # Partage structurel : un état identique au précédent réutilise le
            # MÊME dict (les Snapshot ne sont jamais mutés) — la mémoire d'une
            # chronologie est en O(changements), pas O(instantanés). Sur une
            # journée France entière, c'est ce qui rend la passe complète
            # tenable (~15 Go → ~1 Go).
            prec = derniers.get(vl)
            if prec is not None and prec == e:
                e = prec
            else:
                derniers[vl] = e
            par_vl[vl].append(Snapshot(timestamp=ts, etats=e))
        if (i + 1) % 100 == 0:
            logger.info("… %d/%d instantanés lus", i + 1, len(horodates))

    for vl in sorted(par_vl):
        yield TimelinePoste(vl, par_vl[vl])


# ===========================================================================
# Chemin tabulaire générique (repli — parquet/CSV format long)
# ===========================================================================

#: alias de colonnes reconnus (insensibles à la casse), par champ normalisé
_ALIASES: dict[str, tuple[str, ...]] = {
    "timestamp": ("timestamp", "time", "datetime", "date", "t", "instant",
                  "horodatage"),
    "voltage_level": ("voltage_level_id", "voltage_level", "vl", "vl_id",
                      "substation", "substation_id", "poste", "sub_id"),
    "switch": ("switch_id", "switch", "organe", "organe_id", "breaker_id",
               "id_organe", "element_id"),
    "open": ("open", "is_open", "etat", "state", "status", "value", "position"),
}

_TRUE = {"true", "1", "open", "ouvert", "opened", "o"}
_FALSE = {"false", "0", "closed", "ferme", "fermé", "close", "f", "c"}


def _to_open(v) -> bool:
    """Normalise une valeur d'état en « ouvert ? » (booléen)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"État d'organe non interprétable : {v!r}")


def _detecter_colonnes(colonnes, mapping: Optional[dict] = None) -> dict:
    """Mappe les champs normalisés vers les colonnes réelles (les ``mapping``
    explicites priment ; sinon premiers alias trouvés, insensibles à la casse)."""
    lower = {str(c).lower(): c for c in colonnes}
    out: dict[str, str] = {}
    mapping = mapping or {}
    for champ, alias in _ALIASES.items():
        if champ in mapping:
            out[champ] = mapping[champ]
            continue
        for a in alias:
            if a in lower:
                out[champ] = lower[a]
                break
    manquants = set(_ALIASES) - set(out)
    if manquants:
        raise ValueError(
            f"Colonnes non identifiées : {sorted(manquants)}. "
            f"Colonnes du fichier : {sorted(map(str, colonnes))}. "
            "Complétez _ALIASES (manoeuvre/dataset/dgitt.py) ou passez un "
            "mapping explicite {champ: colonne}.")
    return out


def _fichiers_tabulaires(input_dir: Path) -> list[Path]:
    exts = {".parquet", ".csv", ".csv.gz"}
    return sorted(p for p in Path(input_dir).rglob("*")
                  if p.is_file() and (p.suffix in exts
                                      or "".join(p.suffixes[-2:]) in exts))


def charger_timelines_tabulaire(
    input_dir: Path,
    vl_filter: Optional[set[str]] = None,
    mapping: Optional[dict] = None,
) -> Iterator[TimelinePoste]:
    """Itère les ``TimelinePoste`` depuis des fichiers parquet/CSV format long.

    Lit tous les fichiers parquet/CSV de ``input_dir`` (récursif), concatène,
    trie par horodatage et reconstruit, par poste, les snapshots successifs en
    appliquant les lignes sur un état courant (fonctionne pour un format
    « événements » — seules les bascules — comme pour un format « snapshots »
    — l'état complet à chaque pas).
    """
    import pandas as pd   # paresseux : le cœur du package n'en dépend pas

    fichiers = _fichiers_tabulaires(input_dir)
    if not fichiers:
        raise FileNotFoundError(
            f"Aucun fichier parquet/CSV sous {input_dir} — le dataset "
            "est-il téléchargé ? (cf. docstring de manoeuvre/dataset/dgitt.py)")

    frames = []
    cols: Optional[dict] = None
    for f in fichiers:
        df = (pd.read_parquet(f) if f.suffix == ".parquet"
              else pd.read_csv(f))
        if cols is None:
            cols = _detecter_colonnes(df.columns, mapping)
            logger.info("Colonnes détectées : %s", cols)
        frames.append(df[[cols["timestamp"], cols["voltage_level"],
                          cols["switch"], cols["open"]]])
    data = pd.concat(frames, ignore_index=True)
    assert cols is not None

    data = data.sort_values([cols["voltage_level"], cols["timestamp"]],
                            kind="stable")
    for vl, grp in data.groupby(cols["voltage_level"], sort=True):
        vl = str(vl)
        if vl_filter and vl not in vl_filter:
            continue
        snapshots: list[Snapshot] = []
        etat: dict[str, bool] = {}
        for ts, sous in grp.groupby(cols["timestamp"], sort=True):
            etat = dict(etat)
            for _, row in sous.iterrows():
                etat[str(row[cols["switch"]])] = _to_open(row[cols["open"]])
            snapshots.append(Snapshot(timestamp=str(ts), etats=etat))
        if snapshots:
            yield TimelinePoste(vl, snapshots)


# ===========================================================================
# Point d'entrée — auto-détection du format
# ===========================================================================

def charger_timelines(
    input_dir: Path,
    vl_filter: Optional[set[str]] = None,
    mapping: Optional[dict] = None,
    sous_echantillon: Optional[int] = None,
) -> Iterator[TimelinePoste]:
    """Itère les ``TimelinePoste`` du dataset, **format auto-détecté**.

    - si des instantanés XIIDM sont présents (format réel du dataset RTE 7000)
      → ``charger_timelines_xiidm`` ;
    - sinon, repli sur le format tabulaire parquet/CSV
      → ``charger_timelines_tabulaire``.
    """
    input_dir = Path(input_dir)
    if _a_des_xiidm(input_dir):
        yield from charger_timelines_xiidm(
            input_dir, vl_filter, sous_echantillon=sous_echantillon)
    elif _fichiers_tabulaires(input_dir):
        yield from charger_timelines_tabulaire(input_dir, vl_filter, mapping)
    else:
        raise FileNotFoundError(
            f"Aucun instantané reconnu sous {input_dir} : ni XIIDM "
            "(*.xiidm[.bz2/.gz]) ni tabulaire (*.parquet/*.csv). Le dataset "
            "est-il téléchargé ? (cf. docstring de manoeuvre/dataset/dgitt.py)")
