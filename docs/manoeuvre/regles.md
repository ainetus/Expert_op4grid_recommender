# Module manœuvre — spécification des règles implémentées

> Traçabilité des **règles métier** du séquencement de manœuvres
> (topologie nodale cible → séquence détaillée d'organes de coupure).
> Chaque règle référence sa fonction et son test.

Code : `expert_op4grid_recommender/manoeuvre/` — Tests : `tests/manoeuvre/`.

Point d'entrée unique : `algo.determiner_topo_complete_cible(poste, topo_cible)`.

---

## Vocabulaire

| Terme | Définition |
|-------|------------|
| **SJB** | Section de jeu de barres (busbar section). |
| **Barre** (jeu de barres) | Rail électrique courant sur la longueur du poste ; peut être sectionné en plusieurs sections longitudinales. |
| **Sectionnement (de barre)** | Sectionneur (SA / DISCONNECTOR) reliant *directement* deux SJB d'une **même** barre. Manœuvrable **hors charge** uniquement. |
| **Couplage** | Travée à disjoncteur (DJ / BREAKER) reliant deux **barres** différentes. Le DJ coupe le courant nominal. |
| **DJ de cellule** | Disjoncteur d'ensemble d'une cellule de départ, côté sélecteurs de barre ; met le départ hors tension. |
| **SA d'aiguillage** | Sectionneur sélecteur reliant une cellule de départ à une barre donnée. |
| **Boucle courte** | Ré-aiguillage sans coupure : le départ reste sous tension (un chemin parallèle existe via le couplage fermé). |
| **Boucle longue** | Ré-aiguillage avec mise hors tension du départ (ouverture du DJ de cellule). |
| **Tronçon** | Composante structurelle de SJB ; `nb_jeux_barres` = nombre de barres distinctes du tronçon. |
| **Nœud électrique** | Bus : ensemble d'équipements au même potentiel. |

---

## Règles

### R1 — Faisabilité / nettoyage des ouvrages
Tout départ de la topologie cible doit exister physiquement dans le poste ;
sinon la cible est déclarée infaisable. (Portage de `connectAndDeconnectOuvrageHS`.)
- **Code** : `determiner_topo_complete_cible` (contrôle des départs manquants).
- **Test** : `test_algo.py` (cibles standard), `test_carrip3_manoeuvre.py`.

### R2 — Court-circuit d'identité
Si la topologie nodale courante satisfait déjà la cible (même partition), on
ne génère **aucune** manœuvre.
- **Code** : `determiner_topo_complete_cible` (test `meme_topologie` initial).
- **Test** : `test_algo.py::test_noop_aucune_manoeuvre`.

### R3 — Distinction sectionnement vs couplage
Une liaison inter-SJB est un **sectionnement** si elle ne comporte que des
sectionneurs (pas de DJ) — les deux SJB sont alors sur la même barre ; sinon
c'est un **couplage** (présence d'un DJ).
Détection des barres : nommage RTE (entier de tête du `busbar_section_id`) en
primaire, repli structurel par connectivité de sectionnement (chemins sans DJ).
- **Code** : `troncons._detecter_barres`, `algo._inter_sjb_couplers`
  (`_InterSjbCoupler.is_sectionnement`).
- **Test** : `test_troncons.py` (nb de barres par poste), `test_carrip3_3noeuds.py`.

### R4 — Tronçonnement structurel
Le tronçonnement regroupe les SJB indépendamment de l'état courant des organes
(structure du poste). `nb_jeux_barres` du tronçon = nombre de barres distinctes.
- **Code** : `troncons.construire_tronconnement`.
- **Test** : `test_troncons.py` (CARRIP3 : 1 tronçon, 2 barres, DJ de couplage).

### R5 — Placement nœud → sections de barres (modèle par segments)
À chaque nœud cible on attribue un **groupe connexe de SJB** (dans le graphe des
couplers). Un nœud peut donc **occuper plusieurs barres** : les couplers internes
au groupe sont fermés (un seul potentiel), ceux entre groupes ouverts.
- un **départ reste sur sa barre courante** si elle appartient au groupe de son
  nœud (ré-aiguillage évité) ;
- contrainte de connexité (un groupe = une partie connexe du graphe des
  couplers) et de faisabilité (chaque départ atteint une SJB de son groupe) ;
- on retient l'affectation de **coût minimal** :
  `5 × ré-aiguillages + manœuvres de couplers + 4 × ouvertures de sectionnement`.
  Les ré-aiguillages étant fortement pénalisés, **fermer un couplage** est
  privilégié quand cela suffit (cf. R6).
- une cible demandant plus de nœuds que physiquement réalisable (p.ex. nœuds
  mixtes > barres) n'a aucune affectation valide → **infaisable**.
- **Postes > 2 barres — pénalité multi-barres + double candidat « origin-preserving »** :
  à coût égal, une pénalité **dominante** (`POIDS_NOEUD_MULTIBARRE`) écarte les
  nœuds « exotiques » (demi-rames croisées, ex. `{1A,2B}`) que les faisceaux de
  couplage **partagés** rendent faussement réalisables. Mais cette pénalité peut
  forcer un placement **mono-barre** qui *multiplie les ré-aiguillages* là où un
  nœud multi-barres **légitime** (barres entièrement couplées) serait moins
  coûteux. Parade : `determiner_topo_complete_cible` réalise **deux** placements —
  pénalisé **et** de coût brut minimal (`penaliser_multibarre=False`) — et retient
  **transactionnellement** la réalisation **vérifiée** la **moins coûteuse en
  manœuvres** (le placement exotique non réalisable n'étant jamais vérifié, il est
  écarté → **aucune régression**). Garantie : à partition nodale égale, la cible
  détaillée **auto-déterminée** coûte **≤** la cible détaillée faite main.
- **Code** : `algo._placement_automatique(..., penaliser_multibarre)`,
  `algo._recherche_exhaustive`, `algo.determiner_topo_complete_cible` (double candidat).
- **Test** : `test_carrip3_3noeuds.py::test_point_entree_unique_cree_3eme_noeud_automatiquement`,
  `::test_infaisable_trois_noeuds_mixtes`, `test_algo.py::test_trois_noeuds_sur_deux_barres_infaisable`,
  `test_postes_3barres_400kv.py::test_placement_mono_barre_pour_separation`,
  `test_cible_detaillee_optimalite.py` (garantie `T_ALGO ≤ T_VISÉE` ; convergence TAVELP7).

### R6 — Évaluation de l'état de couplage (utiliser les barres disponibles)
Lorsqu'il y a **moins de nœuds que de barres**, on **utilise les barres
disponibles** : un nœud s'étend sur plusieurs barres en gardant le **couplage
fermé**, plutôt que de ramener tous les départs sur une seule barre. Concrètement
(découle de R5) : un couplage est **fermé** entre SJB d'un même nœud, **ouvert**
entre SJB de nœuds différents. Les départs restent sur leurs barres, sans
ré-aiguillage, dans la mesure du possible.
- **Code** : `algo._placement_automatique` (groupes multi-barres),
  `determiner_manoeuvres_avec_sections` (`to_open` / `to_close`).
- **Test** : `test_algo.py::test_cible_un_noeud_referme_couplage_sans_reaiguiller`,
  `::test_split_ouvre_le_couplage`.

### R6bis — Fermeture **sûre** des couplers (règle du sectionneur)
La fermeture d'un coupler respecte l'invariant des sectionneurs :
- un **DJ de couplage** (BREAKER) peut relier deux barres de **potentiels
  différents** : c'est l'organe de couplage, fermé en premier — il
  équipotentialise alors ses barres ;
- un **sectionneur** (DISCONNECTOR entre deux SJB) n'est fermé que si ses deux
  côtés sont **déjà équipotentiels** (reliés par ailleurs, p.ex. par un DJ
  fermé juste avant) **ou** si l'un des côtés est **hors tension** ;
- si un sectionneur à fermer reste non sûr (deux potentiels vifs, sans pont DJ
  possible), on **dé-énergise d'abord le côté « stub »** (le moins chargé) en
  ré-aiguillant ses départs (boucle longue) vers une SJB du même nœud déjà
  équipotentielle au côté conservé, **puis** on ferme le sectionneur (section
  morte). C'est une **manœuvre préalable** au sens demandé.

Ordre de fermeture : DJ d'abord, puis sectionneurs devenus sûrs, enfin
dé-énergisation + fermeture des sectionneurs « stub ».
- **Code** : `determiner_manoeuvres_avec_sections` (Phase 0 : `_equipotentiel`,
  `_departs_cables`, dé-énergisation des stubs).
- **Test** : `test_algo.py::test_fusion_un_noeud_respecte_regle_sectionneur`
  (rejoue la séquence et vérifie chaque manœuvre de sectionneur).

### R7 — Ré-aiguillage : DJ d'ensemble de cellule uniquement
Pour ré-aiguiller un départ en **boucle longue**, on ouvre **uniquement le DJ
d'ensemble de la cellule** (côté sélecteurs de barre), ce qui met la cellule
hors tension et suffit pour basculer ensuite les sectionneurs. On n'ouvre
**pas** les disjoncteurs propres aux équipements situés en aval (cas omnibus :
un DJ de cellule commun alimente plusieurs équipements — inutile et à éviter
d'ouvrir un DJ « le long de la charge »).
- **Code** : `algo._own_breakers_to_sjb` (parcours depuis le côté barre, arrêt
  au premier nœud de branchement omnibus).
- **Test** : `test_carrip3_3noeuds.py::test_boucle_longue_ouvre_seulement_le_dj_de_cellule`.

### Invariant de sécurité des sectionneurs
Un sectionneur (SA d'aiguillage ou sectionnement de barre) ne se manœuvre **hors
charge** : il ne doit jamais relier ni séparer deux points de **potentiels
différents**. Concrètement :

- **Fermeture** d'un SA reliant deux barres : autorisée seulement si les deux
  barres sont **déjà au même potentiel** (déjà le même nœud électrique), ou si
  l'un des côtés est **hors tension**. Sinon → court-circuit.
- **Ouverture** d'un SA (alors que les deux barres sont reliées par lui) :
  autorisée seulement si les deux barres **restent au même potentiel** après
  (encore reliées par ailleurs → aucun courant coupé), ou si l'un des côtés est
  **hors tension**.

Conséquence pratique sur le ré-aiguillage : le choix **boucle courte vs longue
est déduit automatiquement** de ce critère (et non d'une heuristique de phase) —
courte si la barre actuelle et la barre cible sont déjà le même nœud électrique
(hors cellule), longue sinon.
- **Code** : `algo._meme_noeud_hors_cellule` (test d'équipotentialité hors
  cellule), `algo._reaiguiller_vers_sjb` (décision automatique).

Cet invariant gouverne l'ordre des manœuvres en R8, R9 et R10.

### R8 — Boucle courte (couplage fermé)
Quand un chemin parallèle existe (couplage encore fermé → les deux barres sont
au **même potentiel**), le ré-aiguillage se fait **sous tension** : fermer le SA
vers la barre cible **puis** ouvrir le SA vers l'ancienne barre. Le pont
temporaire entre les deux SA est sûr (même potentiel) ; aucun DJ n'est ouvert.
- **Code** : `algo._reaiguiller_vers_sjb` (branche `COURTE`).
- **Test** : `test_algo.py::test_split_boucle_courte`,
  `test_carrip3_3noeuds.py::test_boucle_courte_avant_sectionnement_longue_apres`.

### R9 — Boucle longue (pas de chemin parallèle)
Les deux barres étant à des potentiels **différents** (couplage ouvert), l'ordre
est impératif pour respecter l'invariant ci-dessus :
1. **ouvrir le DJ de cellule** (R7) → départ hors tension, jonction des SA morte ;
2. **ouvrir le SA vers l'ancienne barre** (la jonction reste morte) ;
3. **fermer le SA vers la barre cible** (jamais simultané avec l'ancien SA) ;
4. **refermer le DJ de cellule** → départ ré-alimenté sur la barre cible.
On ne ferme **jamais** le SA cible tant que l'ancien SA est fermé (ce serait un
pont entre deux barres de tensions différentes = court-circuit).
- **Code** : `algo._reaiguiller_vers_sjb` (branche `LONGUE`).
- **Test** : `test_carrip3_3noeuds.py::test_departs_du_3eme_noeud_en_boucle_longue`,
  `::test_boucle_longue_ouvre_ancien_sa_avant_fermer_nouveau`,
  `::test_boucle_longue_jamais_deux_sa_fermes_simultanement`.

### R9bis — Sectionneur **partagé** avec la barre cible (sectionneur de ligne)
Certaines cellules ont un **sectionneur de ligne** (`SL`) **commun** au chemin
vers *toutes* les barres (la sélection de barre se fait par les SA `SA.1/SA.2/…`
en aval). En quittant une ancienne barre, on **n'ouvre pas** un sectionneur qui
appartient aussi au chemin de la **barre cible** : sinon on déconnecte le départ
de sa barre cible elle-même (manœuvre parasite + cible non atteinte). On n'ouvre
que les sectionneurs **propres** à l'ancienne barre.
- **Code** : `algo._reaiguiller_vers_sjb._ouvrir_sa_anciens` (filtre `sa_cible`).
- **Test** : `test_postes_3barres_400kv.py` (TAVELP7, cible 3 nœuds).

### R10 — Règle du sectionneur de barre (dé-énergisation)
Un sectionneur de barre ne se manœuvre que **hors charge**. Pour scinder une
barre en deux nœuds (créer un nœud au-delà du nombre de barres) :
1. ré-aiguiller (boucle courte) tous les départs de la section à isoler sur
   l'autre barre, afin de la laisser **hors tension** ;
2. **ouvrir le sectionnement** (sûr, section morte) ;
3. ré-aiguiller (boucle longue) les départs du nouveau nœud sur la section
   désormais isolée.
La section « hors tension » est jugée sur le **câblage des SA** (et non sur la
connectivité électrique globale, qui voit tout relié tant que le couplage est
fermé).
- **Code** : `algo.determiner_manoeuvres_avec_sections` (phases ré-aiguillage /
  parking / ouverture sectionnement), `_wired_sjbs`.
- **Test** : `test_carrip3_3noeuds.py::test_sectionnement_ouvert_hors_tension`,
  `::test_3noeuds_atteint_et_verifie`.

### R10bis — Isoler **par les disjoncteurs d'abord** (minimiser les ré-aiguillages)
Avant de dé-énergiser une section par ré-aiguillage (parking, coûteux), on
**isole d'abord** cette section autant que possible par **ouverture des
disjoncteurs de couplage et de tronçonnement environnants** — organes qui
**coupent la charge** et n'imposent donc aucune contrainte « hors tension ».
Le ré-aiguillage n'intervient qu'**en dernier recours**, sur le **résidu**.

Conséquences sur l'algorithme :
1. Une section isolable de la référence par **simple ouverture d'un couplage
   (DJ)** — ou par un sectionnement **déjà ouvert** — **ne nécessite aucun
   parking** : ses départs restent en place. Seules les sections **incidentes à
   un sectionnement fermé destiné à s'ouvrir** sont conservées dans
   `sjb_isoles` (filtre `sect_isol_sjbs`).
2. Avant d'ouvrir un **sectionnement** (phase C), on **ouvre d'abord les
   couplages destinés à s'ouvrir qui touchent la section à isoler** (frontière
   ou interne). Cela réduit la section à mettre hors tension à son **résidu**
   minimal, puis :
   - **0 ouvrage** restant → ouverture directe (section morte) ;
   - **n ≥ 1 ouvrages** restants → ouverture momentanée de leur **DJ d'ouvrage**
     (mise hors tension), ouverture du sectionneur, puis refermeture des DJ.
3. Exemple **PALUNP3 → 4 nœuds** : section 1.2 reliée à 2.2 par `COUPL.2` (DJ).
   En ouvrant `COUPL.2` **avant** `SS.1.12`, la section 1.2 se réduit à son seul
   résidu au lieu d'entraîner la dé-énergisation des 5 départs de 2.2 — la
   séquence passe de **40 à 12 manœuvres** (cf. séquence experte ≈ 9).
- **Code** : `algo.determiner_manoeuvres_avec_sections` — filtre
  `sect_isol_sjbs` de `sjb_isoles` ; phase C, ouverture des couplages incidents
  à `side_isol` avant le sectionneur (`_live_graph_sans`,
  `_ouvrages_energises_sur`).
- **Test** : `test_palunp3_isolation_disjoncteurs.py`.

### R10ter — Modes de dé-énergisation : **smooth** vs **agressif**
Le séquenceur détaillé (`determiner_manoeuvres_cible_detaillee(..., mode=)`)
propose deux stratégies pour ouvrir un sectionnement de barre :

- **smooth** (défaut) — **un seul ouvrage hors tension à la fois**. Pour mettre
  une section hors tension, chaque ouvrage est **garé** (ré-aiguillé), **un par
  un**, sur une **section de parking** : une SJB atteignable distincte, **hors
  section isolée** de préférence, et **équipotentielle** si possible — auquel cas
  le ré-aiguillage est en **boucle courte** et l'ouvrage n'est **pas** déconnecté
  du tout (`parking_sjb`). Les ouvrages garés sont **ramenés** ensuite (boucle
  longue) sur la section isolée. `target_sjb` est amorcé avec la barre cible
  exacte (`cible_busbar`) pour éviter les déplacements inutiles.
  - **Vidage one-by-one du côté à isoler** : quand l'ouverture d'un sectionnement
    a **les deux côtés sous tension** avec **plusieurs** ouvrages à isoler, on les
    **gare un par un** sur le **côté survivant** (boucle courte, équipotentiel tant
    que le sectionnement est fermé → *aucune* coupure), on ouvre le sectionnement
    (section vidée), puis on **ramène** chaque ouvrage un par un (boucle longue).
    Repli : si **tous** les ouvrages ne peuvent pas atteindre le côté survivant,
    dé-énergisation **en place** (inchangée).
  - **Réduction de la section morte (cross-feed)** : si le côté à isoler est un
    **nœud couplé** (plusieurs barres reliées par un couplage DJ encore **fermé**,
    ex. 1.1+2.1 via `COUPL.1`), il suffit de rendre morte la **seule section
    adjacente** au sectionnement. On ouvre donc **temporairement** les couplages DJ
    frontières (hors charge) — puis on les **referme** —, ne dé-énergisant que les
    départs de cette section (souvent **un seul**). Combiné au vidage one-by-one,
    **ROMAIP6** passe de **6** coupures simultanées à **0** (séquence ≈ experte
    « 1 ouvrage à la fois », 42 manœuvres).
  - **Limite** : un poste sans côté survivant atteignable **ni** couplage de
    cross-feed à ouvrir (départs confinés à des sections mono-barre) garde une
    dé-énergisation en place groupée — **signalée par l'alerte** ci-dessous.
- **agressif** (`_sequence_detaillee_aggressive`) — minimiser les **bascules de
  DJ**. D'abord (1) **fermer** les couplages/sectionnements destinés à fusionner
  (équipotentialité), puis (2) **switcher en boucle courte** — **sans ouvrir le
  DJ** — tout départ dont les barres source/cible sont **au même potentiel**
  (cœur du mode : un départ équipotentiel se déplace sous tension) ; (3) ne
  **dé-énergiser en lot** que les ouvrages **non** réalisables en boucle courte
  **et** ceux à isoler pour ouvrir un sectionnement ; (4) ouvrir les
  splits, ré-alimenter une fois. **Transactionnel** : si la variante boucle courte
  n'atteint pas la cible **exactement** (typiquement une section devant être morte
  pour ouvrir un sectionnement, ce que la boucle courte empêcherait), on **retombe**
  sur la dé-énergisation groupée intégrale — jamais de régression de sûreté ni
  d'exactitude. Ex. **CPNIEP6** (barres couplées) : **5** manœuvres en boucle courte
  (≈ séquence experte) au lieu de 17 ; **ROMAIP6** (re-sectionnement) : repli
  dé-énergisé exact.

Les deux modes atteignent la **même** topologie détaillée cible, sectionneurs
ouverts hors tension (`_verifier_securite_sectionneurs`).
- **Code** : `algo.determiner_manoeuvres_cible_detaillee` (param `mode`),
  `determiner_manoeuvres_avec_sections` (parking `parking_sjb`, param
  `cible_busbar`), `_sequence_detaillee_aggressive` (wrapper transactionnel
  boucle courte / dé-énergisé), `_aggressive_impl(boucle_courte=…)`.
- **Vérification + alerte (smooth)** : `ouvrages_simultanement_hors_tension`
  (public) rejoue la séquence et signale tout moment où **plus d'un** ouvrage est
  **temporairement hors tension** — c.-à-d. dont le **DJ propre**, initialement
  **fermé**, est ouvert **puis refermé** (coupure temporaire) — qu'il s'agisse d'un
  **ré-aiguillage** *ou* d'une **dé-énergisation de section**. Sont **exclus** : les
  ouvrages **déjà déconnectés** au départ et ceux **mis hors service** (DJ final
  ouvert). C'est une **alerte de bonne pratique non bloquante** (n'affecte pas
  `is_verified*`), portée par `ResultatManoeuvres.alertes` (mode agressif
  **exempté**, il batch volontairement). L'IHM la surface en badge ⚠. La séquence
  experte « 1 ouvrage à la fois » lève **0** alerte ; un batch en lève autant que
  de chevauchements.
- **Test** : `test_mode_agressif_et_un_seul_ouvrage.py` (boucle courte CPNIEP6,
  repli ROMAIP6, alerte un-seul détectée/propre), `test_ssavop3_modes.py`,
  `test_cztryp6_3noeuds.py`.
- **IHM** : sélecteur « Mode : Smooth / Agressif » avant le calcul.

### R11 — Ordonnancement de la séquence (`listeDordre`)
Ordre imposé pour minimiser les risques :
1. **fermer** les couplages nécessaires (préparation) ;
2. ré-aiguillages **boucle courte** ;
3. **ouvrir les sectionnements** (sections hors tension) ;
4. **ouvrir les couplages** (DJ) ;
5. ré-aiguillages **boucle longue** vers les sections isolées.

**Exception (R10bis)** : un couplage (DJ) touchant la **section à isoler** est
ouvert **dès la phase C, avant** le sectionnement de cette section, afin de la
réduire à son résidu (isolement par les disjoncteurs d'abord). Cela ne
contrevient pas à la sûreté (le DJ coupe la charge) ; l'ordre 3-avant-4 reste la
règle pour les couplages **non incidents** à une section en cours d'isolement
(p.ex. CARRIP3, dont la section isolée est une feuille sans couplage adjacent).
- **Code** : `algo.determiner_manoeuvres_avec_sections` (phases 0 → E ; phase C
  ouvre les couplages incidents avant le sectionneur).
- **Test** : `test_carrip3_3noeuds.py::test_ordre_sectionnement_avant_couplage`,
  `test_palunp3_isolation_disjoncteurs.py`.

### R12 — Contrôle de court-circuit avant fermeture de couplage
On ne ferme un couplage que si les deux SJB visent le **même nœud cible**
(même potentiel attendu) ; sinon la fermeture est refusée (risque de
court-circuit) et signalée.
- **Code** : `algo.determiner_manoeuvres_avec_sections` (phase 0, garde).
- **Statut** : simplifié (égalité de nœud cible, pas de calcul de potentiel fin).

### R13 — Suppression des manœuvres sans effet
La séquence est rejouée depuis l'état initial ; toute manœuvre plaçant un OC
dans l'état où il se trouve déjà est retirée. Les bascules réelles (ex.
ouverture/fermeture d'un DJ en boucle longue) sont conservées.
- **Code** : `algo._optimiser_sequence`.
- **Test** : couvert indirectement (cohérence des séquences vérifiées).

### R14 — Vérification post-manœuvre (nodale)
Après application de la séquence, la topologie nodale est recalculée
(`TopologieNodale.from_graph`) et comparée à la cible par isomorphisme de
partition (`meme_topologie`). Le résultat porte `is_verified`.
- **Code** : `algo.determiner_topo_complete_cible` / `determiner_manoeuvres_avec_sections`.
- **Test** : tous les tests `test_carrip3_*` et `test_algo.py` (assertion `is_verified`).

### R15 — Topologie détaillée imposée (barre exacte + vérification détaillée)
Quand une **topologie détaillée cible** est imposée (état précis de chaque
organe, donc la barre exacte de chaque départ — plus spécifique que la seule
partition nodale), on vise cet état exact :
1. atteindre la **topologie nodale** cible de façon sûre (R1-R14) ;
2. **raffiner** : ramener chaque départ sur sa **barre imposée** (ré-aiguillage
   boucle courte, sûr car les barres d'un même nœud sont équipotentielles) — par
   défaut les départs reviennent sur leur barre d'origine, au prix de
   **manœuvres supplémentaires** (« requinçonçage ») ;
3. **vérifier la topologie détaillée** (barre de chaque départ + état de chaque
   coupler) ; les **écarts** résiduels sont consignés (`ecarts`,
   `is_verified_detaillee`).

**Candidat « diff minimal » (anti ferme-puis-rouvre)** : la voie placement
ci-dessus reconstruit la partition en **fermant des couplers** pour fusionner les
SJB d'un nœud, que l'alignement (R19) **rouvre** ensuite si la cible les veut
ouverts — beaucoup de manœuvres inutiles quand la cible n'est qu'un **petit
delta** de l'état courant. `determiner_manoeuvres_cible_detaillee` calcule donc
**aussi** un candidat qui ne manœuvre **que les organes dont l'état diffère** de
la cible : ré-aiguillage des départs concernés (`_reaiguiller_vers_sjb`, règle du
sectionneur), isolation des départs déconnectés, puis manœuvre **directe** des
couplers / sectionnements restants. Il est retenu **transactionnellement** s'il
atteint **exactement** la cible détaillée (un ordre direct dangereux — sectionneur
sous charge — est consigné en écart, donc rejeté) et qu'il est **plus court**. Ne
touchant que les organes qui *doivent* changer, il est alors **toujours ≤** la
voie principale (ex. **MUHLBP7** : **9** manœuvres au lieu de 37 ; CARRIP3 1 nœud :
2 au lieu de 20 ; TAVELP7 : 7 au lieu de 29).

**Cohérence omnibus** : la barre cible d'un départ est comparée sur **sa propre
cellule** (primaire). Un organe **partagé** entre cellules (omnibus) n'hérite plus
de la barre d'une cellule co-locataire → plus d'**écart fantôme** « sur X au lieu
de Y » alors que le départ n'a pas bougé (`_ecarts_detailles`).

**Réalisation mode-dépendante** (postes > 2 JdB, R19) : le mode `smooth`
garantit la **partition nodale** exacte (`is_verified`) — d'éventuels écarts de
faisceau partagé restent consignés ; le mode `aggressive` (alignement d'organes,
R19) atteint la **topologie détaillée exacte** (`is_verified_detaillee`, 0 écart)
sur les cibles 3 JdB testées. Les tests de scénario sont **mode-conscients** :
la partition exacte est exigée des deux modes, l'exactitude détaillée d'au moins
un des deux (`test_scenarios_sauvegardes.py::test_scenario_atteint_topologie_
detaillee`).
- **Code** : `algo.determiner_manoeuvres_cible_detaillee` (combine voie principale
  `_determiner_manoeuvres_cible_detaillee_principal` + candidat
  `_sequence_detaillee_minimal_delta`), `_ecarts_detailles`.
- **Test** : `test_algo.py::test_cible_detaillee_atteinte_avec_barres_exactes`,
  `::test_cible_detaillee_signale_les_ecarts`,
  `test_scenarios_sauvegardes.py::test_carrip3_1noeud_minimal_vs_requinconcage`,
  `test_muhlbp7_sequence_minimale.py` (9 ≤ experte, aucun coupler manœuvré
  inutilement).
- **IHM** : la cible éditée étant détaillée, l'IHM appelle ce mode et affiche
  « DÉTAILLÉE VÉRIFIÉE » ou « NODALE OK · N écart(s) ».

### R16 — Dégradation gracieuse
Si une étape n'est pas réalisable en sécurité (pas de SJB tampon pour isoler une
section, départ inatteignable…), l'algorithme **ne s'interrompt pas** : il
consigne l'**écart** (`res.ecarts`) et poursuit ; `topo_obtenue` est toujours
renseignée. L'IHM affiche alors « NODALE OK · N écart(s) » au lieu d'un échec
opaque.
- **Code** : `determiner_manoeuvres_avec_sections` (collecte des écarts au lieu
  de `return` anticipé), `determiner_manoeuvres_cible_detaillee`.
- **Test** : `test_scenarios_sauvegardes.py` (postes réels rejoués).

### R17 — Postes à > 2 jeux de barres (placement par composantes)
Le placement nodal combinatoire (R5) est conçu pour le **double jeu de barres**.
Sur un poste à **plus de 2 jeux de barres** (niveaux supplémentaires — ex. `3B`,
`4B` —, organes internes à 2 bornes, nœuds à 0 barre), `determiner_manoeuvres_
cible_detaillee` route vers `_sequence_detaillee_multibarres`, qui **dérive le
placement directement des composantes connexes du graphe cible** : chaque nœud =
ses équipements + les sections de barre qu'il occupe. Cela donne les groupes
exacts, y compris les nœuds multi-barres et ceux à 0 barre.

Trois mécanismes complètent ce chemin :
- **Organes internes à 2 bornes** (self/réactance `LINE_SIDE1`+`LINE_SIDE2`,
  `_organes_internes_2bornes`) : passés en `organes_fixes` au séquenceur, ils ne
  sont **ni ré-aiguillés ni signalés en écart** (chaque côté reste sur sa barre).
- **Nœud à 0 barre** (départ isolé sur son DJ, SA ouverts) : **isolé** en
  ouvrant ses sectionneurs de barre pour atteindre l'état cible.
- **Couplages parallèles** (`_inter_sjb_couplers`) : *tous* les couplages entre
  deux barres sont détectés (retrait itératif des arêtes du chemin), pas
  seulement le plus court. Indispensable quand un couplage **fermé** (ex.
  `COUPL.B`) est masqué par un couplage **parallèle ouvert** (ex. liaison
  `SELF.1` via un nœud de couplage commun à trois barres) : sinon les deux barres
  resteraient reliées → fusion de nœuds erronée.

Correctif associé du séquenceur : un couplage n'est mis en `to_open` que s'il est
**réellement fermé** (conducteur), pour ne pas ouvrir un organe partagé avec un
couplage à garder fermé (DJ commun sur trois barres).

Sur les postes à **faisceau de couplage partagé** (DJ atteignant > 2 barres), ce
chemin est complété **transactionnellement** par le **réalisateur par
connectivité** et l'**alignement détaillé des faisceaux** (cf. **R19**), qui
réalisent les séparations/fusions/tronçonnages de demi-rames non couverts par le
ré-aiguillage SJB-par-SJB.

Si la cible n'est pas atteinte en totalité, dégradation gracieuse (R16) : nodale
partielle + écarts pour complétion manuelle.
- **Code** : `_sequence_detaillee_multibarres`, `_organes_internes_2bornes`,
  `_inter_sjb_couplers` (couplages parallèles), `determiner_manoeuvres_avec_
  sections` (`organes_fixes`, garde `to_open` conducteur).
- **Tests** : `test_scenarios_sauvegardes.py::test_morbrp6_multibarres` (MORBRP6,
  poste 4 JdB, cible atteinte en 8 manœuvres ; réf. séquence experte
  `sequences/MORBRP6_cible_4noeuds_expert.json`) et
  `::test_morbrp6_degradation_gracieuse` (cible exigeant des manœuvres sur la
  self des JdB 3/4 → non atteignable → dégradation gracieuse : nœuds à compléter
  manuellement signalés).

> Fallback nodal-only : `_placement_automatique` conserve un **scoping aux 2 JdB
> principaux** + diagnostic explicite + placement partiel best-effort
> (`_main_busbar_sjb`, `_scoping_raison`, `_diagnostic_infaisabilite`,
> `_placement_best_effort`) pour l'API purement nodale `determiner_topo_complete_
> cible`, qui ne dispose pas de la cible détaillée.

### R18 — Sectionneur manœuvré hors charge (règle générale + vérification + alerte IHM)
Un **sectionneur** (DISCONNECTOR) ne se manœuvre que **hors charge** : on ne peut
pas ouvrir un sectionneur qui **coupe un courant** (qui déconnecte, sans chemin
parallèle, deux parties **toutes deux porteuses d'un ouvrage** énergisé). La
parade : **dé-énergiser d'abord la branche par son disjoncteur**, manœuvrer le
sectionneur, puis ré-alimenter.

- **Application** (isolation d'un nœud à 0 barre, R17) :
  `_isoler_depart_hors_barre` ouvre le DJ → ouvre le(s) SA → referme le DJ à son
  état cible (la ligne reste sur son DJ, isolée des barres). Ex. MORBRP6 : la
  séquence finit par `OPEN ARRIG.1 DJ → OPEN ARRIG.1 SA.1 → CLOSE ARRIG.1 DJ`.
- **Vérification générique** : `_sectionneurs_sous_charge_par_manoeuvre` analyse
  la séquence **manœuvre par manœuvre** (rejeu + connexité « live ») ;
  `_verifier_sectionneurs_hors_charge` agrège les écarts. Intégrée à la
  vérification de toutes les séquences (`determiner_manoeuvres_avec_sections`,
  chemins détaillé / multi-barres / agressif). Le message de parade est
  **contextuel** : sélecteur de barre de départ → « dé-énergiser la branche par
  son disjoncteur » ; **sectionnement de barre** (sectionneur entre deux
  sections) → « mettre la **section de barre** hors tension (ré-aiguiller ses
  départs) ».
- **Alerte IHM** : en séquence **manuelle/éditée**, l'IHM (`manoeuvre_ihm.py`)
  affiche un ⚠ sur chaque manœuvre fautive **et le texte explicite de la règle
  enfreinte sous la ligne** (bandeau rouge), via `Session._violations_regles` →
  payload `violations`.
- **Tests** : `test_verifier_sectionneur_sous_charge_detecte` (sélecteur de
  départ, séquence fautive `MORBRP6_cible_4noeuds_wrong_last_step.json`),
  `test_verifier_mauvaise_manoeuvre_finale` (séquence manuelle dont **seule la
  dernière** manœuvre `OPEN AT761 SA.1` est fautive,
  `MORBRP6_cible_4noeuds_mauvaise_manoeuvre.json`),
  `test_message_sectionnement_de_barre_specifique` (message « section de barre »
  sur CZBEVP3) et `test_morbrp6_multibarres` (ordre DJ→SA→DJ vérifié).

### R19 — Réalisateur par connectivité + alignement détaillé des faisceaux (postes > 2 JdB, faisceaux partagés)
Sur les postes à **faisceau de couplage partagé** (un même DJ atteignant > 2
barres — ex. `COUPL.A`/`COUPL.B`/`LIAIS` sur les postes 400 kV à 3 barres), le
ré-aiguillage SJB-par-SJB (R8/R9) ne suffit pas à séparer/fusionner proprement
les nœuds : `_inter_sjb_couplers` peut **mal attribuer** un faisceau partagé. Deux
mécanismes, branchés **transactionnellement** (retenus seulement s'ils vérifient
**exactement** la cible — sinon revert, jamais de dégradation), complètent R17 :

1. **Réalisateur par connectivité** (`determiner_manoeuvres_par_connectivite`) —
   repli du chemin nodal *et* du chemin détaillé multi-barres. Au lieu de
   raisonner faisceau par faisceau, il agit sur la **connectivité réelle** :
   (a) ré-aiguillage des départs (maintien en place si déjà dans le bon nœud),
   (b) **sectionnements intra-barre** par état direct, (c) **séparation** par
   ouverture du **lot minimal** de DJ de couplage qui sépare deux nœuds, (d)
   **fusion** par fermeture des faisceaux requis. Réalise les manœuvres
   opérationnelles (séparer des barres couplées ; tronçonner un JdB en
   demi-rames) sur les 7 postes 400 kV à 3 barres testés.
2. **Alignement détaillé des faisceaux** (`_aligner_couplers_sur_cible`) — une
   fois la **partition nodale** atteinte, ramène chaque **faisceau** (bay de
   couplage) à l'**état d'organes exact** de la cible détaillée, faisceau par
   faisceau : dé-énergiser (ouvrir le DJ) → positionner les SA **hors charge** →
   refermer le DJ à l'état cible (faisceaux actifs d'abord). Garde
   **transactionnelle de préservation de partition** : tout réalignement qui
   changerait la partition nodale est **annulé en bloc**. Supprime les écarts de
   faisceau « cosmétiques » (faisceau électriquement équivalent mais d'organes
   différents).
- **Mode `aggressive`** : atteint la topologie détaillée **exacte** (0 écart) sur
  les cibles 3 JdB testées. **Mode `smooth`** : atteint la **partition nodale**
  exacte ; un réalignement de faisceau partagé peut émettre une **alerte
  sectionneur** (R18) consignée en écart plutôt que manœuvré sous charge.
- **Code** : `algo.determiner_manoeuvres_par_connectivite`,
  `algo._aligner_couplers_sur_cible`, `algo._sequence_detaillee_multibarres`
  (repli only-on-failure), `algo.determiner_topo_complete_cible` (branche nodale).
- **Tests** : `test_postes_3barres_400kv.py` (7 postes, cibles 3-7 nœuds),
  `test_golden_sequences.py` (goldens `CHESNP7_cible_3noeuds__{smooth,aggressive}`,
  `TRI.PP7_cible_3_noeuds__{smooth,aggressive}`, `TAVELP7_cible_3noeuds__*`),
  `test_sequences_sauvegardees_3barres.py`.

---

## Conformité « art de la manœuvre » (R20-R25)

> Analyse critique, spécification complète et règles non encore implémentées
> (R26 validations électriques, R27 multi-postes, R28 automates) :
> **`docs/manoeuvre/art_de_la_manoeuvre.md`**. Les verdicts de ces règles sont
> portés par le champ **séparé** `ResultatManoeuvres.conformite`
> (`ConformiteSequence` : `violations` / `avertissements` / annotations),
> renseigné par `plugins.pipeline.verifier_sequence` — les champs historiques
> `ecarts`/`alertes` sont inchangés (goldens iso).

### R20 — Classification des conséquences de chaque manœuvre
Chaque manœuvre est classée par rejeu en conséquences élémentaires CCRT
(manœuvre hors tension, ouverture/fermeture de boucle, préparer/désaiguiller,
mise sous/hors tension, établissement/coupure de transit, changement du nombre
de nœuds, sans effet), avec la famille CCRT de l'organe (DJ, interrupteur, SA
d'aiguillage, SA de couplage, sectionnement SS, sectionneur de ligne).
Présomption de tension sur les feeders (mono-poste, conservatif).
- **Code** : `algo/conformite.py` — `classifier_manoeuvres`, `Consequence`,
  `FamilleOrgane`, `familles_organes`.
- **Test** : `test_conformite_art_manoeuvre.py::test_boucle_courte_classee_boucle`,
  `::test_boucle_longue_conforme`, `::test_ouverture_couplage_change_nb_noeuds`,
  `::test_manoeuvre_hors_tension`, `::test_familles_organes_carrip3`.

### R21 — Matrice d'autorisation conséquence × organe (CCRT)
Un sectionneur ne peut ni établir/couper un transit ni changer le nombre de
nœuds (réservé DJ/interrupteur) → **violation**. Généralise R18 : couvre aussi
le pontage de deux nœuds par fermeture de SA.
- **Code** : `conformite.verifier_matrice_autorisation`.
- **Test** : `::test_sa_en_charge_est_une_violation`,
  `::test_fermeture_sa_pontant_deux_noeuds_est_une_violation`,
  `::test_sequence_algo_est_conforme` (les séquences natives sont conformes).

### R22 — Essai de jeu de barre (détection)
La remise sous tension d'une section morte par un **sectionneur** lève un
**avertissement** (préférer un essai par disjoncteur : ligne > couplage >
transformateur). La *génération* de la sous-séquence d'essai par le séquenceur
reste à faire (cf. art_de_la_manoeuvre.md § 4.2).
- **Code** : `conformite.verifier_matrice_autorisation` (branche essai de barre).
- **Test** : `::test_essai_de_barre_avertissement`.

### R23 — Suivi d'état des départs (machine à états CCO)
États (nb SA fermés × OC ligne) : désaiguillé, préparé, préparé-DA, en service,
en service-DA, bizarre non préparé fermé. Transitions **interdites** (ouvrir le
dernier SA en service ; fermer un SA sous OC fermé non préparé) → violation ;
transitions « bizarres » → avertissement ; contrôles associés (±I/Scc,
contrôle nœuds sur ±DA).
- **Code** : `conformite.suivre_etats_departs`, `EtatDepart`, `_TRANSITIONS_DEPART`.
- **Test** : `::test_trajectoire_etats_boucle_longue`,
  `::test_double_aiguillage_controle_noeuds`, `::test_sa_en_charge_est_une_violation`.

### R24 — Temporisations (ACT 104, sous-ensemble graphe)
10 s après toute manœuvre de sectionneur ; 60 s minimum entre deux fermetures
d'un même DJ (regonflage, temps déjà écoulé décompté). Temporisations
contextuelles (régleurs TR, PSEM, MHU→SUAV 225/400 kV) spécifiées non calculées.
- **Code** : `conformite.calculer_temporisations`, `Temporisation`.
- **Test** : `::test_tempo_sectionneur_10s`, `::test_tempo_regonflage_dj_60s`,
  `::test_pas_de_tempo_dj_ferme_une_seule_fois`.

### R25 — Contrôles SCADA attendus par manœuvre
Par conséquence : TM I passe à 0 (coupure), TS absence tension apparaît /
disparaît (mise hors/sous tension, sections nommées), TM I/P/Q conformes au
calcul + Icc/TS PRESENCE (établissement), contrôle du nombre de nœuds
(changement de nœuds, ±DA). Boucles et manœuvres HU : aucun contrôle.
- **Code** : `conformite._controles_pour` (→ `ManoeuvreClassee.controles`),
  contrôles de transition dans `_TRANSITIONS_DEPART`.
- **Test** : `::test_controles_attendus`, `::test_trajectoire_etats_boucle_longue`.

---

## Postes multi-sections

Les postes à plusieurs **sections par barre** (ex. **CARRIP6** : 2 barres × 3
sections = 6 SJB ; chaîne de couplers
`1.1–1.2–1.3–(DJ)–2.3–2.2–2.1`) sont gérés : pour ouvrir un sectionnement, les
départs de la section à isoler sont garés temporairement sur une SJB
**équipotentielle** accessible (même destinée à être isolée ensuite), puis
ré-aiguillés en boucle longue/courte. Validé par
`test_scenarios_sauvegardes.py` (CARRIP6 → 5 nœuds, topologie détaillée
vérifiée).

## Limites connues (documentées)

| Cas | Statut |
|-----|--------|
| Ré-aiguillage d'omnibus complexes (scission d'un groupe sur deux barres) | partiel |
| Contrôle de court-circuit fin (potentiel / déphasage) | simplifié (R12) |
| Topologies de couplers non chaînées (≥ 3 barres en anneau) | partiel |
| Nœuds mêlant départs connectés et déconnectés | partiel |
| Postes > 2 jeux de barres (niveaux 3B/4B, organes internes à 2 bornes, nœuds à 0 barre) | géré via placement par composantes (R17) ; fallback nodal-only : scoping 2 JdB |
| Postes 3 JdB à **faisceau de couplage partagé** (séparation/fusion/tronçonnage de demi-rames) | géré via réalisateur connectivité + alignement détaillé (R19) — exact en `aggressive` ; `smooth` exact en nodal, écart faisceau possible |
| Cible **arbitraire** (round-robin) exigeant la *reconnexion* de départs déjà déconnectés | partiel |

---

## Correspondance avec le C++ `libTOPO`

| Règle | Fonction C++ (`TOPOPoste.cc`) |
|-------|-------------------------------|
| R1 | `connectAndDeconnectOuvrageHS` |
| R4 | `buildTronconnement`, `CelluleBarresTopo::tronconneGraph` |
| R5 | `identifySuperTronconnement`, `getTronconnementBesoinReaiguillage2barres` |
| R6 | `evalueEtatCouplage` |
| R7–R9 | `reaiguillage2barres`, `CelluleDepartTopo::reaiguillageBarres` |
| R10 | `tronconnerSJB`, `isolateDepartFromBarre` |
| R11 | `listeDordre` |
| R14 | fin de `determineTopoCompleteCible` (`memeTopologie`) |
