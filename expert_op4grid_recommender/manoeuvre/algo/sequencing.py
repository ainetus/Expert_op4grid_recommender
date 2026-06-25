"""
manoeuvre/algo/sequencing.py — Séquenceur général (couplage + sectionnement, listeDordre) et ré-aiguillages.
"""
from __future__ import annotations

from collections import Counter
from typing import Literal, Optional
import networkx as nx

from ..cellules import CelluleDepart
from ..cellules import SwitchInfo
from ..topologie import TopologieNodale, PosteTopologique
from .results import Manoeuvre, ResultatManoeuvres
from .graph_ops import _InterSjbCoupler, _inter_sjb_couplers, _is_open, _live_graph_sans, _meme_noeud_hors_cellule, _ouvrages_energises_sur, _own_breakers_to_sjb, _sa_path_to_sjb, _set_switch, _wired_busbar, _wired_sjbs
from .verification import _optimiser_sequence, _verifier_regles


def _reaiguiller_vers_sjb(
    G: nx.Graph,
    cells,
    eq_id: str,
    target_sjb: int,
    manoeuvres: list[Manoeuvre],
    boucle: Optional[Literal["COURTE", "LONGUE"]] = None,
) -> bool:
    """
    Ré-aiguille un départ vers une SJB cible. Retourne True si des manœuvres
    ont été générées.

    Le **type de boucle est déterminé par l'invariant de sécurité des
    sectionneurs** (cf. docs/manoeuvre/regles.md) et non par une heuristique de
    phase :

    - **COURTE** si la barre cible et la (les) barre(s) actuelle(s) du départ
      sont **déjà le même nœud électrique** (reliées par ailleurs, p.ex. via le
      couplage fermé). On ferme alors le SA cible PUIS on ouvre l'ancien SA :
      les deux SA sont brièvement fermés mais entre barres équipotentielles
      (aucun court-circuit), et le départ reste sous tension.
    - **LONGUE** sinon (barres de potentiels différents) : ouvrir le DJ de
      cellule (départ hors tension, jonction morte) → **ouvrir l'ancien SA** →
      **fermer le SA cible** → refermer le DJ. On ne ferme jamais le SA cible
      tant que l'ancien SA est fermé (ponter deux potentiels = court-circuit).

    ``boucle`` peut être forcé, sinon il est déduit automatiquement.
    """
    cell = cells.get_cellule_depart(eq_id)
    if cell is None:
        return False

    sjb_id = (G.nodes[target_sjb].get("busbar_section_id") or str(target_sjb))
    sa_cible = _sa_path_to_sjb(cell, target_sjb)
    if not sa_cible:
        return False
    # Déjà câblé sur la cible et nulle part ailleurs ?
    deja = all(not _is_open(G, s) for s in sa_cible) and all(
        _is_open(G, s)
        for bb in cell.busbar_nodes if bb != target_sjb
        for s in _sa_path_to_sjb(cell, bb)
    )
    if deja:
        return False

    # Barres actuellement câblées (SA fermés) autres que la cible
    old_busbars = [
        bb for bb in cell.busbar_nodes
        if bb != target_sjb and _sa_path_to_sjb(cell, bb)
        and all(not _is_open(G, s) for s in _sa_path_to_sjb(cell, bb))
    ]
    # Invariant : boucle courte possible ssi toutes les anciennes barres sont
    # déjà au même potentiel que la cible (hors cellule).
    if boucle is None:
        boucle = ("COURTE"
                  if all(_meme_noeud_hors_cellule(G, cell, bb, target_sjb)
                         for bb in old_busbars)
                  else "LONGUE")

    n_before = len(manoeuvres)
    djs = _own_breakers_to_sjb(cell, target_sjb, eq_id)

    def _fermer_sa_cible():
        for sa in sa_cible:
            if _is_open(G, sa):
                _set_switch(G, sa, False)
                manoeuvres.append(Manoeuvre(
                    switch_id=sa, action="CLOSE",
                    raison=f"ré-aiguillage '{eq_id}' vers {sjb_id}",
                    type_boucle=boucle,
                ))

    # Organes **partagés** avec le chemin de la barre cible (ex. sectionneur de
    # ligne ``SL`` commun à toutes les barres) : à NE PAS ouvrir, sinon on
    # déconnecte le départ de sa barre cible elle-même. On ne déconnecte une
    # ancienne barre qu'en ouvrant les sectionneurs qui lui sont **propres**.
    _sa_cible_set = set(sa_cible)

    def _ouvrir_sa_anciens():
        for bb in cell.busbar_nodes:
            if bb == target_sjb:
                continue
            for sa in _sa_path_to_sjb(cell, bb):
                if sa in _sa_cible_set:
                    continue  # organe partagé avec la barre cible -> garder fermé
                if not _is_open(G, sa):
                    _set_switch(G, sa, True)
                    bb_id = G.nodes[bb].get("busbar_section_id") or str(bb)
                    manoeuvres.append(Manoeuvre(
                        switch_id=sa, action="OPEN",
                        raison=f"'{eq_id}' quitte {bb_id}",
                        type_boucle=boucle,
                    ))

    if boucle == "LONGUE":
        # 1) mise hors tension par le DJ de cellule
        for dj in djs:
            if not _is_open(G, dj):
                _set_switch(G, dj, True)
                manoeuvres.append(Manoeuvre(
                    switch_id=dj, action="OPEN",
                    raison=f"mise hors tension '{eq_id}' (boucle longue)",
                    type_boucle="LONGUE",
                ))
        # 2) ouvrir l'ancien SA AVANT 3) fermer le SA cible (jamais de pont)
        _ouvrir_sa_anciens()
        _fermer_sa_cible()
        # 4) remise sous tension
        for dj in djs:
            _set_switch(G, dj, False)
            manoeuvres.append(Manoeuvre(
                switch_id=dj, action="CLOSE",
                raison=f"remise sous tension '{eq_id}' (boucle longue)",
                type_boucle="LONGUE",
            ))
    else:  # COURTE : fermer la cible puis ouvrir l'ancien (même potentiel)
        _fermer_sa_cible()
        _ouvrir_sa_anciens()

    return len(manoeuvres) > n_before


def _isoler_depart_hors_barre(
    G: nx.Graph, cell: CelluleDepart, cible_graph: nx.Graph
) -> list[Manoeuvre]:
    """Isole un départ de ses barres (nœud à **0 barre** : ligne laissée sur son
    DJ) en respectant la **règle du sectionneur** — un sectionneur ne se manœuvre
    que hors charge :

    1. **dé-énergiser** : ouvrir le(s) DJ propre(s) encore fermé(s) ;
    2. **ouvrir les sectionneurs** de barre à ouvrir (désormais hors charge) ;
    3. **remettre le(s) DJ** à leur état cible (refermer si la cible les veut
       fermés — la ligne reste alors sur son DJ, isolée des barres).

    Ne fait rien (et n'ouvre aucun DJ) s'il n'y a aucun sectionneur à ouvrir.
    """
    sas = [sw for sw in cell.disconnectors
           if not _is_open(G, sw.switch_id) and _is_open(cible_graph, sw.switch_id)]
    if not sas:
        return []
    manos: list[Manoeuvre] = []
    djs_ouverts: list[str] = []
    for dj in cell.breakers:
        if not _is_open(G, dj.switch_id):
            _set_switch(G, dj.switch_id, True)
            manos.append(Manoeuvre(
                dj.switch_id, "OPEN",
                f"mise hors tension '{cell.equipment_id}' (avant ouverture sectionneur)"))
            djs_ouverts.append(dj.switch_id)
    for sw in sas:
        _set_switch(G, sw.switch_id, True)
        manos.append(Manoeuvre(
            sw.switch_id, "OPEN",
            f"isolement départ {cell.equipment_id} (nœud sans barre)"))
    for djid in djs_ouverts:
        if not _is_open(cible_graph, djid):
            _set_switch(G, djid, False)
            manos.append(Manoeuvre(
                djid, "CLOSE",
                f"remise sous tension '{cell.equipment_id}' (après ouverture sectionneur)"))
    return manos


def _appliquer_changements_dj(
    G: nx.Graph,
    reconnections: list[tuple[CelluleDepart, SwitchInfo]],
    disconnections: list[tuple[CelluleDepart, SwitchInfo]],
    skip: Optional[set[str]] = None,
) -> list[Manoeuvre]:
    """Applique sur ``G`` les changements d'état des **DJ de départ** (mise en
    service / hors service) et retourne les manœuvres correspondantes.

    ``skip`` : équipements à ignorer (ex. nœuds laissés à l'opérateur)."""
    skip = skip or set()
    out: list[Manoeuvre] = []
    for cell, dj in reconnections:
        if cell.equipment_id in skip:
            continue
        if _is_open(G, dj.switch_id):
            _set_switch(G, dj.switch_id, False)
            out.append(Manoeuvre(dj.switch_id, "CLOSE",
                                 f"mise en service départ {cell.equipment_id}"))
    for cell, dj in disconnections:
        if cell.equipment_id in skip:
            continue
        if not _is_open(G, dj.switch_id):
            _set_switch(G, dj.switch_id, True)
            out.append(Manoeuvre(dj.switch_id, "OPEN",
                                 f"mise hors service départ {cell.equipment_id}"))
    return out


def _consigner_non_realisables(
    res: ResultatManoeuvres, non_places: list[list[str]]
) -> None:
    """Renseigne la dégradation gracieuse : nœuds laissés à l'opérateur +
    écarts « nœud à compléter manuellement » (format unifié)."""
    res.noeuds_non_realisables = non_places
    for grp in non_places:
        res.ecarts.append(
            "nœud à compléter manuellement : {" + ", ".join(sorted(grp)) + "}")


def determiner_manoeuvres_avec_sections(
    poste: PosteTopologique,
    placement: list[tuple[set[str], set[str]]],
    cible_busbar: Optional[dict[str, int]] = None,
    organes_fixes: Optional[set[str]] = None,
) -> ResultatManoeuvres:
    """
    Calcule la séquence de manœuvres pour réaliser un **placement explicite**
    de nœuds sur des sections de jeux de barres, en respectant la règle du
    sectionnement de barre (dé-énergisation avant ouverture).

    ``organes_fixes`` (optionnel) : équipements à **laisser en place** (ni
    ré-aiguillage ni écart) — typiquement les organes internes à 2 bornes
    (self/réactance) déjà câblés sur leurs barres cibles.

    Parameters
    ----------
    poste :
        Vue complète du poste.
    placement :
        Liste de ``(departs, sjb_ids)`` : chaque entrée décrit un nœud cible,
        l'ensemble de ses départs et l'ensemble des SJB (``busbar_section_id``)
        qu'il occupe. Les départs non cités restent inchangés.
    cible_busbar :
        Optionnel — barre (SJB) **cible exacte** de chaque départ (``eq -> sjb``).
        Si fournie, ``target_sjb`` est amorcé avec cette barre quand elle est
        dans le groupe du nœud : chaque départ est alors placé **directement**
        sur sa barre finale (le raffinement R15 devient un no-op), ce qui évite
        le **double-déplacement** (placer puis ramener).

    Returns
    -------
    ResultatManoeuvres
    """
    vl = poste.voltage_level_id
    G = poste.graph.copy()
    cells = poste.cellules

    # Map busbar_section_id -> node
    sjb_node_par_id = {
        G.nodes[n].get("busbar_section_id"): n
        for n in poste.tronconnement.barre_par_busbar
    }

    # --- cible nodale (pour la vérification) -------------------------------
    # Les départs placés forment les nœuds explicites ; les départs non cités
    # conservent leur nœud courant (ex. générateurs isolés).
    groupes = [sorted(d) for d, _ in placement]
    places = {eq for d, _ in placement for eq in d}
    for noeud in poste.topologie_nodale.noeuds.values():
        reste = sorted(noeud.equipment_ids - places)
        if reste:
            groupes.append(reste)
    topo_cible = TopologieNodale.from_node_groups(vl, groupes)
    res = ResultatManoeuvres(
        voltage_level_id=vl,
        topo_initiale=poste.topologie_nodale,
        topo_cible=topo_cible,
    )

    # --- résolution départ -> SJB cible ------------------------------------
    target_sjb: dict[str, int] = {}
    node_de_sjb: dict[int, int] = {}  # sjb -> index de nœud
    for idx, (departs, sjb_ids) in enumerate(placement):
        sjb_set = {sjb_node_par_id[s] for s in sjb_ids if s in sjb_node_par_id}
        for s in sjb_set:
            node_de_sjb[s] = idx
        # ``departs`` est un ensemble : on l'itère **trié** pour que l'ordre
        # d'insertion dans ``target_sjb`` (donc l'ordre des manœuvres qui en
        # découlent) soit reproductible d'un process à l'autre (PYTHONHASHSEED).
        for eq in sorted(departs):
            if organes_fixes and eq in organes_fixes:
                continue  # organe interne à 2 bornes : laissé en place
            cell = cells.get_cellule_depart(eq)
            if cell is None:
                continue
            reachable = cell.busbar_nodes & sjb_set
            if not reachable:
                # Non bloquant : on consigne l'écart et on laisse le départ en
                # place (la vérification finale le signalera).
                res.ecarts.append(
                    f"'{eq}' ne peut atteindre aucune SJB de son nœud cible")
                continue
            # Barre cible exacte connue (flux détaillé) et dans le groupe : on la
            # vise directement → placement en une fois, sans retour ultérieur.
            if cible_busbar and cible_busbar.get(eq) in sjb_set:
                target_sjb[eq] = cible_busbar[eq]
                continue
            # On garde le départ sur sa barre actuelle si elle est dans le groupe
            # du nœud (évite un ré-aiguillage inutile) ; sinon on prend une SJB
            # du groupe.
            wired = [bb for bb in reachable
                     if _sa_path_to_sjb(cell, bb)
                     and all(not _is_open(G, s) for s in _sa_path_to_sjb(cell, bb))]
            target_sjb[eq] = wired[0] if wired else min(reachable)

    # --- couplers à ouvrir / fermer ----------------------------------------
    # - à ouvrir  : entre SJB de nœuds différents,
    # - à fermer  : entre SJB d'un même nœud actuellement séparées (fusion).
    couplers = _inter_sjb_couplers(poste)
    to_open: list[_InterSjbCoupler] = []
    to_close: list[_InterSjbCoupler] = []
    for cp in couplers:
        na, nb = node_de_sjb.get(cp.sjb_a), node_de_sjb.get(cp.sjb_b)
        if na is None or nb is None:
            continue
        currently_closed = all(not _is_open(G, sid) for sid in cp.switch_ids)
        if na != nb:
            # On n'ouvre un couplage que s'il est **réellement fermé** (conducteur).
            # Sinon les deux barres sont déjà séparées : ne pas toucher (évite
            # d'ouvrir un organe partagé avec un couplage à garder fermé — ex.
            # DJ commun à deux liaisons sur trois barres).
            if currently_closed:
                to_open.append(cp)
        elif not currently_closed:
            to_close.append(cp)

    # Organes (DJ) qui, une fois fermés, relient des SJB de **nœuds différents** :
    # ce sont des couplages multi-barres **partagés** (ex. cellule LIAIS d'un poste
    # triple-barre, dont le DJ est commun aux liaisons 1-2, 1-3, 2-3). Fermer un
    # coupler « même nœud » qui passe par un tel organe re-ponterait des barres
    # distinctes : à proscrire quand la fusion de ce nœud est déjà acquise par
    # ailleurs (coupler dédié), cf. Phase 0.
    bridge_breakers: set[str] = set()
    for cp in couplers:
        na, nb = node_de_sjb.get(cp.sjb_a), node_de_sjb.get(cp.sjb_b)
        if na is not None and nb is not None and na != nb:
            bridge_breakers.update(cp.breaker_ids)

    # --- groupes SJB finaux (couplers gardés fermés) -----------------------
    sjb_graph = nx.Graph()
    sjb_graph.add_nodes_from(poste.tronconnement.barre_par_busbar)
    for cp in couplers:
        if cp not in to_open:
            sjb_graph.add_edge(cp.sjb_a, cp.sjb_b)
    groupe_sjb = {}
    for gid, comp in enumerate(nx.connected_components(sjb_graph)):
        for s in comp:
            groupe_sjb[s] = gid

    # Référence = groupe portant le plus de départs cibles (le « tronc »)
    poids = Counter(groupe_sjb[s] for s in target_sjb.values() if s in groupe_sjb)
    ref_group = poids.most_common(1)[0][0] if poids else None
    ref_sjbs = {s for s, g in groupe_sjb.items() if g == ref_group}

    # SJB « derrière un sectionnement » : au moment où l'on ouvre les
    # sectionnements (phase C), les couplages (DJ) sont *encore fermés*. On
    # calcule donc la connectivité en gardant fermés tous les couplers SAUF les
    # sectionnements à ouvrir. La composante ne contenant pas la référence est
    # la section à mettre hors tension.
    to_open_sect_ids = {sid for cp in to_open if cp.is_sectionnement
                        for sid in cp.switch_ids}
    energ_graph = nx.Graph()
    energ_graph.add_nodes_from(poste.tronconnement.barre_par_busbar)
    for cp in couplers:
        if any(sid in to_open_sect_ids for sid in cp.switch_ids):
            continue  # sectionnement ouvert en phase C
        energ_graph.add_edge(cp.sjb_a, cp.sjb_b)
    ref_repr = next(iter(ref_sjbs), None)
    energises = (nx.node_connected_component(energ_graph, ref_repr)
                 if ref_repr is not None else set())
    sjb_isoles = set(poste.tronconnement.barre_par_busbar) - energises

    # R10bis — « isoler par les disjoncteurs d'abord » : une section n'a besoin
    # de **parking / dé-énergisation** que si son isolement passe par l'ouverture
    # d'un **sectionnement fermé** (organe hors charge). Une section isolable par
    # simple ouverture d'un **couplage (DJ, qui coupe la charge)** — ou par un
    # sectionnement déjà ouvert — garde ses départs en place. On ne conserve donc
    # dans ``sjb_isoles`` que les sections **incidentes à un sectionnement fermé
    # destiné à s'ouvrir**.
    sect_isol_sjbs: set[int] = set()
    for cp in to_open:
        if cp.is_sectionnement and any(not _is_open(G, s) for s in cp.switch_ids):
            sect_isol_sjbs.add(cp.sjb_a)
            sect_isol_sjbs.add(cp.sjb_b)
    sjb_isoles &= sect_isol_sjbs

    manoeuvres: list[Manoeuvre] = []
    reaiguilles: set[str] = set()

    # Index nœud -> SJB et départ -> nœud (pour la dé-énergisation des stubs)
    node_sjb_sets: dict[int, set[int]] = {}
    for s, idx in node_de_sjb.items():
        node_sjb_sets.setdefault(idx, set()).add(s)
    node_de_dep = {eq: idx for idx, (deps, _) in enumerate(placement)
                   for eq in deps}

    def _equipotentiel(a: int, b: int) -> bool:
        """True si deux SJB sont au même potentiel (chemin de switches fermés)."""
        Hc = nx.Graph()
        Hc.add_nodes_from(G.nodes())
        for u, v, dd in G.edges(data=True):
            if not dd.get("open", False):
                Hc.add_edge(u, v)
        return a in Hc and b in Hc and nx.has_path(Hc, a, b)

    def _departs_cables(s: int) -> list[str]:
        # Tri explicite : la séquence de dé-énergisation ne doit pas dépendre de
        # l'ordre d'itération de ``target_sjb`` (cf. construction triée ci-dessus).
        return sorted(eq for eq in target_sjb if s in _wired_sjbs(G, cells, eq))

    def parking_sjb(eq: str, target: int) -> Optional[int]:
        """SJB **tampon** où garer temporairement le départ pendant l'ouverture
        du sectionnement isolant sa cible (règle « un seul ouvrage hors tension à
        la fois », R10ter). Préférences :
        1. une SJB **hors section isolée** (et atteignable) ;
        2. à défaut, une SJB **équipotentielle** (parking en **boucle courte**,
           donc *sans* coupure — même si elle sera isolée ensuite) ;
        3. en dernier recours, toute SJB accessible distincte de la cible.
        Retourne ``None`` si aucune section de parking n'existe (→ exception :
        dé-énergisation en place en phase C)."""
        cell = cells.get_cellule_depart(eq)
        if cell is None:
            return None
        for bb in cell.busbar_nodes:                       # 1) hors section isolée
            if bb != target and bb not in sjb_isoles:
                return bb
        cur = _wired_busbar(cell, G)                       # 2) équipotentielle
        for bb in cell.busbar_nodes:
            if bb != target and (cur is None or _equipotentiel(bb, cur)):
                return bb
        for bb in cell.busbar_nodes:                       # 3) dernier recours
            if bb != target:
                return bb
        return None

    def _fermer_coupler(cp: _InterSjbCoupler, raison: str) -> None:
        for sid in cp.switch_ids:
            if _is_open(G, sid):
                _set_switch(G, sid, False)
                manoeuvres.append(Manoeuvre(sid, "CLOSE", raison))

    # --- Phase 0 : fermeture SÛRE des couplers (règle du sectionneur) -------
    # Un DJ de couplage peut relier deux potentiels différents (couplage) ; un
    # sectionneur ne se ferme que si ses deux côtés sont déjà équipotentiels ou
    # si l'un est hors tension. On ferme donc d'abord les DJ (qui équipotentient
    # leurs barres), puis les sectionneurs devenus sûrs.
    restants = [cp for cp in to_close
                if any(_is_open(G, s) for s in cp.switch_ids)]
    changed = True
    while changed and restants:
        changed = False
        for cp in list(restants):
            # Fusion déjà acquise (même potentiel) via un coupler dédié : fermer
            # ce coupler-ci serait redondant. Si en plus il passe par un organe
            # **partagé** reliant des nœuds différents (DJ d'une liaison multi-
            # barres), le fermer re-ponterait des barres distinctes -> on l'écarte.
            if (_equipotentiel(cp.sjb_a, cp.sjb_b)
                    and set(cp.breaker_ids) & bridge_breakers):
                restants.remove(cp)
                changed = True
                continue
            if cp.breaker_ids:                       # DJ -> couplage sûr
                _fermer_coupler(cp, "fermeture couplage de barres")
                restants.remove(cp)
                changed = True
            elif (_equipotentiel(cp.sjb_a, cp.sjb_b)
                  or not _departs_cables(cp.sjb_a)
                  or not _departs_cables(cp.sjb_b)):  # sectionneur sûr
                _fermer_coupler(cp, "fermeture sectionnement (barres équipotentielles)")
                restants.remove(cp)
                changed = True

    # Sectionneurs encore non sûrs : dé-énergiser le côté « stub » (moins de
    # départs) en ré-aiguillant ses départs vers une SJB du même nœud déjà
    # équipotentielle au côté conservé (manœuvre préalable), puis fermer.
    for cp in restants:
        a, b = cp.sjb_a, cp.sjb_b
        wa, wb = _departs_cables(a), _departs_cables(b)
        stub, keep = (b, a) if len(wb) <= len(wa) else (a, b)
        for eq in _departs_cables(stub):
            idx = node_de_dep.get(eq)
            cell = cells.get_cellule_depart(eq)
            alts = [bb for bb in cell.busbar_nodes
                    if idx is not None and bb in node_sjb_sets.get(idx, set())
                    and bb != stub and _equipotentiel(bb, keep)]
            if alts and _reaiguiller_vers_sjb(G, cells, eq, alts[0], manoeuvres):
                reaiguilles.add(eq)
                target_sjb[eq] = alts[0]   # éviter un retour en phase A/B
        if not _departs_cables(stub):
            _fermer_coupler(cp, "fermeture sectionnement (section mise hors tension)")

    # --- Phase A/B : ré-aiguillages boucle courte (couplage encore fermé) ---
    # Règle « un seul ouvrage hors tension à la fois » (R10ter, mode smooth) :
    # un départ dont la barre cible est une section à isoler est **garé** (un par
    # un) sur une SJB tampon — en **boucle courte** si équipotentielle (sans
    # coupure), sinon boucle longue (une seule coupure, séquentielle) —, puis
    # **ramené** en phase E. Faute de tampon (``parking_sjb`` None), il est
    # dé-énergisé **en place** en phase C (exception assumée).
    parkings: dict[str, int] = {}
    for eq, tgt in sorted(target_sjb.items()):
        if tgt in sjb_isoles:
            buf = parking_sjb(eq, tgt)
            if buf is None:
                continue  # aucun tampon : dé-énergisation en place (phase C)
            parkings[eq] = tgt
            if _reaiguiller_vers_sjb(G, cells, eq, buf, manoeuvres):
                reaiguilles.add(eq)
        else:
            if _reaiguiller_vers_sjb(G, cells, eq, tgt, manoeuvres):
                reaiguilles.add(eq)

    # --- Phase C : ouverture des sectionnements (règle du sectionneur) -----
    # Règle : un sectionneur de barre ne se manœuvre que hors charge. Avant
    # chaque ouverture, on vérifie par parcours du graphe « live » (switches
    # fermés) l'état des deux côtés une fois le sectionneur ouvert :
    #   - chemin parallèle conservé        -> manœuvre en boucle (équipotentiel) ;
    #   - au moins un côté hors tension     -> ouverture directe sûre ;
    #   - deux côtés sous tension           -> dé-énergisation préalable du côté
    #     le plus petit (ouverture de ses DJ d'ouvrage), ouverture du sectionneur,
    #     puis ré-énergisation (refermeture des DJ). Coupure momentanée assumée.
    all_sjb = set(poste.tronconnement.barre_par_busbar)
    for cp in to_open:
        if not cp.is_sectionnement:
            continue
        if all(_is_open(G, sid) for sid in cp.switch_ids):
            continue

        def _ouvrir(raison: str) -> None:
            for sid in cp.switch_ids:
                if not _is_open(G, sid):
                    _set_switch(G, sid, True)
                    manoeuvres.append(Manoeuvre(sid, "OPEN", raison))

        H = _live_graph_sans(G, cp.switch_ids)
        a, b = cp.sjb_a, cp.sjb_b
        if a in H and b in H and nx.has_path(H, a, b):
            # Les deux côtés restent reliés par un chemin parallèle : ouverture
            # en boucle, sans divergence de potentiel.
            _ouvrir("ouverture sectionnement de barre (boucle, chemin parallèle)")
            continue

        side_a = (nx.node_connected_component(H, a) if a in H else {a}) & all_sjb
        side_b = (nx.node_connected_component(H, b) if b in H else {b}) & all_sjb
        liv_a = _ouvrages_energises_sur(G, cells, side_a, H)
        liv_b = _ouvrages_energises_sur(G, cells, side_b, H)
        if not liv_a or not liv_b:
            _ouvrir("ouverture sectionnement de barre (section hors tension)")
            continue

        # Deux côtés sous tension. Côté à isoler = le plus petit (en ouvrages
        # énergisés). R10bis : on l'**isole d'abord par les disjoncteurs** en
        # ouvrant les **couplages** (DJ, qui coupent la charge) destinés à
        # s'ouvrir et reliant cette section à l'extérieur. Cela réduit le résidu
        # à dé-énergiser ; on ne manœuvre les DJ d'ouvrage qu'en dernier recours.
        side_isol = side_a if len(liv_a) <= len(liv_b) else side_b

        for cpl in to_open:
            if cpl.is_sectionnement:
                continue
            # couplage touchant la section à isoler (frontière ou interne) :
            # son ouverture (DJ, hors charge) réduit la section à dé-énergiser.
            if cpl.sjb_a in side_isol or cpl.sjb_b in side_isol:
                for sid in cpl.breaker_ids:
                    if not _is_open(G, sid):
                        _set_switch(G, sid, True)
                        manoeuvres.append(Manoeuvre(
                            sid, "OPEN",
                            "ouverture couplage de barres (isolement de la section)"))

        # Recalcul du côté à isoler après ouverture des couplages adjacents.
        H2 = _live_graph_sans(G, cp.switch_ids)
        a_isol = a if a in side_isol else b
        side_isol = ((nx.node_connected_component(H2, a_isol)
                      if a_isol in H2 else {a_isol}) & all_sjb)

        # Réduction de la **section morte** au strict nécessaire (R10ter). Si le
        # côté à isoler est un **nœud couplé** (plusieurs barres reliées par des
        # couplages DJ encore fermés, ex. 1.1+2.1 via COUPL.1), il suffit, pour
        # ouvrir le sectionnement, de rendre morte la **seule section adjacente**
        # (a_isol). On ouvre donc TEMPORAIREMENT les couplages DJ frontières (hors
        # charge) qui relient cette section au reste du nœud — puis on les REFERME
        # après —, afin de ne dé-énergiser QUE les départs de la section adjacente
        # (souvent un seul) au lieu de tout le nœud couplé.
        couplages_temp: list[str] = []
        _coupler_brk = [s for c2 in couplers if not c2.is_sectionnement
                        for s in c2.breaker_ids]
        H_sect = _live_graph_sans(G, list(cp.switch_ids) + _coupler_brk)
        minimal_dead = ((nx.node_connected_component(H_sect, a_isol)
                         if a_isol in H_sect else {a_isol}) & all_sjb)
        if minimal_dead < side_isol:
            to_open_brk = {s for c2 in to_open if not c2.is_sectionnement
                           for s in c2.breaker_ids}
            for c2 in couplers:
                if c2.is_sectionnement:
                    continue
                if (c2.sjb_a in minimal_dead) ^ (c2.sjb_b in minimal_dead):
                    for sid in c2.breaker_ids:
                        if not _is_open(G, sid):
                            _set_switch(G, sid, True)
                            manoeuvres.append(Manoeuvre(
                                sid, "OPEN",
                                "ouverture temporaire couplage "
                                "(réduction de la section à dé-énergiser)"))
                            if sid not in to_open_brk:
                                couplages_temp.append(sid)
            H2 = _live_graph_sans(G, cp.switch_ids)
            side_isol = ((nx.node_connected_component(H2, a_isol)
                          if a_isol in H2 else {a_isol}) & all_sjb)

        def _restaurer_couplages_temp() -> None:
            """Referme les couplages ouverts **temporairement** pour réduire la
            section morte (le sectionnement étant désormais ouvert, la refermeture
            est sûre ; la cible les veut fermés)."""
            for sid in couplages_temp:
                if _is_open(G, sid):
                    _set_switch(G, sid, False)
                    manoeuvres.append(Manoeuvre(
                        sid, "CLOSE",
                        "refermeture couplage (rétabli après isolement de section)"))

        liv_isol = _ouvrages_energises_sur(G, cells, side_isol, H2)
        if not liv_isol:
            _ouvrir("ouverture sectionnement de barre (section hors tension)")
            _restaurer_couplages_temp()
            continue
        if not all(brk for _, brk in liv_isol):
            # Ouvrage sans DJ propre : dé-énergisation impossible.
            _ouvrir("ouverture sectionnement de barre (section ATTENTION sous tension)")
            _restaurer_couplages_temp()
            continue

        # R10ter (mode smooth) : « un seul ouvrage hors tension à la fois ».
        # Plutôt que de dé-énergiser EN BLOC tous les ouvrages de la section à
        # isoler (plusieurs hors tension simultanément), on les **gare un par un**
        # sur le côté **survivant** : tant que le sectionnement est fermé les deux
        # côtés sont équipotentiels → ré-aiguillage en **boucle courte** (sans
        # coupure). La section vidée, on ouvre le sectionnement (un côté mort), puis
        # on **ramène** chaque ouvrage (un par un, boucle longue) sur sa cible.
        # Repli : un ouvrage n'atteignant PAS le côté survivant est dé-énergisé en
        # place (coupure inévitable, exception assumée).
        b_surv = b if a_isol == a else a
        side_surv = ((nx.node_connected_component(H2, b_surv)
                      if b_surv in H2 else {b_surv}) & all_sjb)
        # Section tampon (côté survivant) atteignable par chaque ouvrage à isoler.
        bufs: dict[str, int] = {}
        for eq, _brk in liv_isol:
            cell = cells.get_cellule_depart(eq)
            buf = next((bb for bb in (cell.busbar_nodes if cell else [])
                        if bb in side_surv), None)
            if buf is not None:
                bufs[eq] = buf
        # Parking « un par un » seulement si **≥ 2** ouvrages **et TOUS** peuvent se
        # garer sur le côté survivant : la section se vide alors **entièrement** →
        # le batch (plusieurs hors tension à la fois) est totalement évité. Sinon
        # (un seul ouvrage, ou bénéfice partiel), dé-énergisation **en place**
        # inchangée → pas de churn de séquence quand le gain n'est pas total.
        _park = len(liv_isol) >= 2 and len(bufs) == len(liv_isol)
        gares: list[tuple[str, int]] = []      # (eq, SJB de retour) à ramener
        djs_rouverts: list[str] = []
        for eq, brk in sorted(liv_isol, key=lambda x: x[0]):   # ordre déterministe
            if _park:
                cell = cells.get_cellule_depart(eq)
                # Retour = cible si sur le côté isolé, sinon section câblée actuelle.
                retour = target_sjb.get(eq)
                if retour is None or retour not in side_isol:
                    retour = _wired_busbar(cell, G)
                if _reaiguiller_vers_sjb(G, cells, eq, bufs[eq], manoeuvres):
                    reaiguilles.add(eq)
                if retour is not None and retour != bufs[eq]:
                    gares.append((eq, retour))
            else:
                for sid in brk:
                    if not _is_open(G, sid):
                        _set_switch(G, sid, True)
                        manoeuvres.append(Manoeuvre(
                            sid, "OPEN",
                            f"mise hors tension '{eq}' (avant ouverture sectionneur)"))
                        djs_rouverts.append(sid)
        _ouvrir("ouverture sectionnement de barre (section hors tension)")
        for sid in djs_rouverts:
            _set_switch(G, sid, False)
            manoeuvres.append(Manoeuvre(
                sid, "CLOSE", "remise sous tension (après ouverture sectionneur)"))
        for eq, retour in gares:               # ramener un par un (boucle longue)
            if _reaiguiller_vers_sjb(G, cells, eq, retour, manoeuvres):
                reaiguilles.add(eq)
        _restaurer_couplages_temp()

    # --- Phase D : ouverture des couplages (DJ) ----------------------------
    for cp in to_open:
        if cp.is_sectionnement:
            continue
        for sid in cp.breaker_ids:
            if not _is_open(G, sid):
                _set_switch(G, sid, True)
                manoeuvres.append(Manoeuvre(
                    switch_id=sid, action="OPEN",
                    raison="ouverture couplage de barres",
                ))

    # --- Phase E : ré-aiguillage boucle longue vers sections isolées -------
    for eq in sorted(parkings):
        if _reaiguiller_vers_sjb(G, cells, eq, parkings[eq], manoeuvres):
            reaiguilles.add(eq)

    # --- Phase F : enforcement de séparation (couplages multi-barres partagés)
    # Sur un poste à > 2 barres, une cellule de couplage **partagée** (un DJ
    # commun atteignant plusieurs barres par sélection de SA, ex. COUPL.A/LIAIS)
    # est mal décomposée en couplers par paires : le séquenceur peut laisser deux
    # SJB de **nœuds cibles différents** reliées. On corrige ici en raisonnant sur
    # la **connectivité réelle** (et non sur la liste de couplers) : tant que deux
    # SJB de nœuds différents restent reliées, ouvrir un **DJ de couplage** (organe
    # coupant la charge — manœuvre sûre) dont l'ouverture les sépare **sans**
    # déconnecter deux SJB d'un même nœud. No-op si la séparation est déjà acquise
    # (cas 2 barres) → aucun golden affecté.
    coupling_breakers = {sid for cp in couplers for sid in cp.breaker_ids}

    def _live() -> nx.Graph:
        Hc = nx.Graph()
        Hc.add_nodes_from(G.nodes())
        for u, v, dd in G.edges(data=True):
            if not dd.get("open", False):
                Hc.add_edge(u, v)
        return Hc

    def _viole_separation(H: nx.Graph):
        """Première paire de SJB de nœuds différents encore reliée, sinon None."""
        items = sorted(node_de_sjb.items())
        for i, (s1, n1) in enumerate(items):
            for s2, n2 in items[i + 1:]:
                if n1 != n2 and s1 in H and s2 in H and nx.has_path(H, s1, s2):
                    return s1, s2
        return None

    def _meme_noeud_casse(H: nx.Graph) -> bool:
        """Une paire de SJB d'un **même** nœud est-elle déconnectée dans H ?"""
        items = sorted(node_de_sjb.items())
        for i, (s1, n1) in enumerate(items):
            for s2, n2 in items[i + 1:]:
                if n1 == n2 and not (
                        s1 in H and s2 in H and nx.has_path(H, s1, s2)):
                    return True
        return False

    if _viole_separation(_live()) is not None:
        # **Transactionnel** : on tente la séparation, mais on ne CONSERVE les
        # ouvertures que si elles atteignent EXACTEMENT la topologie cible. Sinon
        # (couplages multi-barres partagés mal décomposés par `_inter_sjb_couplers`
        # → placement « exotique » irréalisable), on **annule** : Phase F ne doit
        # jamais sur-fragmenter ni dégrader le résultat du séquenceur de base.
        snapshot = {sid: _is_open(G, sid) for sid in coupling_breakers}
        n_avant = len(manoeuvres)

        # 1) Ouvrir TOUS les DJ de couplage dont l'ouverture ne casse aucune
        #    connexité **intra-nœud** (les barres à cheval sur des couplages
        #    parallèles — COUPL.A et COUPL.B — exigent d'ouvrir le *lot*, une
        #    ouverture isolée ne séparant rien). Les DJ qui unifient un nœud
        #    (LIAIS/TRO2.AB ici) cassent un même-nœud → conservés fermés.
        ouverts: list[str] = []
        for sid in sorted(coupling_breakers):
            if _is_open(G, sid):
                continue
            _set_switch(G, sid, True)
            if _meme_noeud_casse(_live()):
                _set_switch(G, sid, False)        # casserait un même-nœud
            else:
                ouverts.append(sid)
        # 2) Refermer les ouvertures **inutiles** (séparation conservée sans elles)
        #    → ne garder que le sous-ensemble minimal.
        for sid in ouverts:
            _set_switch(G, sid, False)
            if _viole_separation(_live()) is not None:
                _set_switch(G, sid, True)         # nécessaire : ré-ouvrir
        # 3) Émettre les manœuvres pour les DJ effectivement laissés ouverts.
        for sid in ouverts:
            if _is_open(G, sid):
                manoeuvres.append(Manoeuvre(
                    switch_id=sid, action="OPEN",
                    raison="ouverture couplage de barres (séparation de nœuds)"))

        # 4) Validation transactionnelle : conserver uniquement si la cible
        #    nodale est atteinte exactement ; sinon tout annuler.
        if not topo_cible.meme_topologie(TopologieNodale.from_graph(G, vl)):
            for sid in coupling_breakers:
                _set_switch(G, sid, snapshot[sid])
            del manoeuvres[n_avant:]

    # --- Optimisation : suppression des manœuvres sans effet (listeDordre) -
    manoeuvres = _optimiser_sequence(poste, manoeuvres)

    res.manoeuvres = manoeuvres
    res.departs_reaiguilles = reaiguilles
    res.couplages_modifies = [sid for cp in (to_open + to_close)
                              for sid in cp.switch_ids]

    # --- Vérification ------------------------------------------------------
    topo_obtenue = TopologieNodale.from_graph(G, vl)
    res.topo_obtenue = topo_obtenue
    res.is_verified = topo_cible.meme_topologie(topo_obtenue)
    res.is_changed = bool(manoeuvres)
    # Sûreté des sectionneurs : signaler toute ouverture restée sous tension
    # (sectionnements de barre) ou tout sectionneur manœuvré sous charge.
    res.ecarts += _verifier_regles(poste, manoeuvres, un_seul=False)
    res.message = (
        "Topologie cible atteinte et vérifiée."
        if res.is_verified
        else f"Cible non atteinte (obtenu {topo_obtenue.nb_noeuds} nœuds, "
             f"visé {topo_cible.nb_noeuds})."
    )
    return res


def determiner_manoeuvres_par_connectivite(poste, placement, topo_cible):
    """Réalisateur **connectivité-based** — postes > 2 barres à faisceaux de
    couplage **partagés** (un DJ atteignant plusieurs barres par sélection de SA,
    ex. COUPL.A/LIAIS), que ``_inter_sjb_couplers`` décompose mal en couplers par
    paires (le séquenceur général s'y trompe : ré-aiguillage qui isole, faisceau
    incomplètement ouvert).

    On raisonne sur la **connectivité réelle** du graphe (et non sur la liste de
    couplers) :

    1. **ré-aiguillage** des départs vers leur barre cible — *maintien* si le
       départ est déjà sur une SJB de son nœud (évite les isolements parasites) ;
    2. **sectionnements** (organes intra-barre, non partagés) : état direct selon
       la partition — fermé si même nœud, ouvert sinon ;
    3. **séparation** : ouverture du **lot minimal** de DJ de couplage séparant les
       nœuds différents sans casser une connexité intra-nœud (cf. Phase F) ;
    4. **fusion** : fermeture de DJ de couplage reliant les SJB d'un même nœud
       encore séparées, sans relier deux nœuds différents.

    Destiné à un usage **transactionnel** : l'appelant ne le retient que s'il
    ``is_verified`` (cf. ``determiner_topo_complete_cible``) — il ne peut donc
    jamais dégrader un résultat déjà correct.
    """
    vl = poste.voltage_level_id
    cells = poste.cellules
    G = poste.graph.copy()
    bp = poste.tronconnement.barre_par_busbar
    sjb_node = {G.nodes[n].get("busbar_section_id"): n for n in bp}
    couplers = _inter_sjb_couplers(poste)
    coupling_breakers = sorted({sid for cp in couplers for sid in cp.breaker_ids})

    node_de_sjb: dict[int, int] = {}
    for idx, (deps, sjbids) in enumerate(placement):
        for sid in sjbids:
            n = sjb_node.get(sid)
            if n is not None:
                node_de_sjb[n] = idx

    manoeuvres: list[Manoeuvre] = []

    def _live() -> nx.Graph:
        H = nx.Graph()
        H.add_nodes_from(G.nodes())
        for u, v, d in G.edges(data=True):
            if not d.get("open", False):
                H.add_edge(u, v)
        return H

    def _viol_sep(H) -> bool:
        it = sorted(node_de_sjb.items())
        for i, (s1, n1) in enumerate(it):
            for s2, n2 in it[i + 1:]:
                if n1 != n2 and s1 in H and s2 in H and nx.has_path(H, s1, s2):
                    return True
        return False

    def _nb_fusion_manquante(H) -> int:
        it = sorted(node_de_sjb.items())
        return sum(
            1 for i, (s1, n1) in enumerate(it) for s2, n2 in it[i + 1:]
            if n1 == n2 and not (s1 in H and s2 in H and nx.has_path(H, s1, s2))
        )

    # 1) ré-aiguillage des départs (maintien si déjà sur une SJB de son nœud).
    for idx, (deps, sjbids) in enumerate(placement):
        group = {sjb_node[s] for s in sjbids if s in sjb_node}
        for eq in sorted(deps):
            cell = cells.get_cellule_depart(eq)
            if cell is None:
                continue
            reachable = cell.busbar_nodes & group
            if not reachable:
                continue
            if _wired_busbar(cell, G) in group:
                continue  # déjà bien placé : ne pas bouger
            if _reaiguiller_vers_sjb(G, cells, eq, min(reachable), manoeuvres):
                pass

    # 2) sectionnements **intra-barre** : état direct selon la partition.
    #    On ne traite QUE les vrais sectionnements (deux SJB de la **même** barre).
    #    Attention : ``_inter_sjb_couplers`` étiquette aussi ``is_sectionnement``
    #    des liaisons SA-seules **inter-barres** (artefacts de la décomposition d'un
    #    faisceau partagé dont le chemin évite le DJ) — les ouvrir fragmenterait le
    #    poste. La séparation inter-barres est gérée en (3) par l'ouverture des DJ.
    for cp in couplers:
        if not cp.is_sectionnement:
            continue
        if bp.get(cp.sjb_a) != bp.get(cp.sjb_b):
            continue  # liaison inter-barres mal étiquetée : pas un sectionnement
        na, nb = node_de_sjb.get(cp.sjb_a), node_de_sjb.get(cp.sjb_b)
        if na is None or nb is None:
            continue
        want_open = (na != nb)
        for sid in cp.switch_ids:
            if _is_open(G, sid) != want_open:
                _set_switch(G, sid, want_open)
                manoeuvres.append(Manoeuvre(
                    sid, "OPEN" if want_open else "CLOSE",
                    "ouverture sectionnement de barre (séparation)" if want_open
                    else "fermeture sectionnement de barre (fusion)"))

    # 3) séparation : lot minimal de **DJ de couplage** ouverts, par
    #    **connectivité réelle** (robuste à la mauvaise attribution des faisceaux
    #    partagés). Les DJ coupent la charge (manœuvre sûre) ; on n'ouvre jamais un
    #    organe cassant une connexité intra-nœud, et on minimise l'ensemble retenu.
    sep_switches = coupling_breakers
    if _viol_sep(_live()):
        ouverts: list[str] = []
        for sid in sep_switches:
            if _is_open(G, sid):
                continue
            _set_switch(G, sid, True)
            if _nb_fusion_manquante(_live()) > 0:
                _set_switch(G, sid, False)  # casserait une connexité intra-nœud
            else:
                ouverts.append(sid)
        for sid in ouverts:
            _set_switch(G, sid, False)
            if _viol_sep(_live()):
                _set_switch(G, sid, True)  # nécessaire
        for sid in ouverts:
            if _is_open(G, sid):
                manoeuvres.append(Manoeuvre(
                    sid, "OPEN", "ouverture couplage de barres (séparation de nœuds)"))

    # 4) fusion : fermer des DJ de couplage reliant les SJB d'un même nœud, sans
    #    relier deux nœuds différents (réduction stricte des manques de fusion).
    while True:
        manque = _nb_fusion_manquante(_live())
        if manque == 0:
            break
        progres = False
        for sid in coupling_breakers:
            if not _is_open(G, sid):
                continue
            _set_switch(G, sid, False)
            if _viol_sep(_live()) or _nb_fusion_manquante(_live()) >= manque:
                _set_switch(G, sid, True)  # relie des nœuds différents / n'aide pas
            else:
                manoeuvres.append(Manoeuvre(
                    sid, "CLOSE", "fermeture couplage de barres (fusion de nœud)"))
                progres = True
                break
        if not progres:
            break

    manoeuvres = _optimiser_sequence(poste, manoeuvres)
    res = ResultatManoeuvres(
        voltage_level_id=vl,
        topo_initiale=poste.topologie_nodale,
        topo_cible=topo_cible,
    )
    res.manoeuvres = manoeuvres
    res.topo_obtenue = TopologieNodale.from_graph(G, vl)
    res.is_verified = topo_cible.meme_topologie(res.topo_obtenue)
    res.is_changed = bool(manoeuvres)
    res.ecarts = _verifier_regles(poste, manoeuvres, un_seul=False)
    res.message = (
        "Topologie cible atteinte (réalisateur connectivité)."
        if res.is_verified
        else f"Cible non atteinte (connectivité) : obtenu "
             f"{res.topo_obtenue.nb_noeuds} nœud(s), visé {topo_cible.nb_noeuds}."
    )
    return res
