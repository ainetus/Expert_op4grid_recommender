# Art de la manœuvre — analyse critique, consolidation et enrichissement des règles

> Confrontation du module manœuvre (règles **R1-R19**, cf. [`regles.md`](regles.md))
> aux référentiels d'exploitation RTE, consolidation en un corpus de règles
> enrichi (**R20-R28**) et proposition de restructuration du module, de
> l'algorithme et du vérificateur.

Sources analysées :

| Source | Contenu |
|--------|---------|
| **Méthode expertes pour reproduire la fiche de manœuvres d'un CCO** (note DRD-PILOT, 2024) | Ouvrages/départs à partir du graphe réseau, typologie ordonnée des manœuvres (~20 codes), essais de jeu de barre, changement de barre sans DA, temporisations ACT 104, suivi d'état des départs, validations (répartition/Icc), contrôles TITIEN (TS/TM), automates |
| **Manœuvres autorisées ou interdites et conséquences** (tableur) | Matrice « conséquence × organe → autorisée ? » (CCRT) et tableau « organe × manœuvre → conséquence → contrôle → conditions » |
| **Présentation automatisation de la manœuvre DRD** (mars 2024) | Découpage fonctionnel (représentation, heuristiques, validation/contrôle, documentation opérationnelle), bilan et limites du POC |

Document de référence métier : **CCRT chapitre C3-3** ; consigne **ACT 104**
(temporisations) ; consigne **AUT 102** (réenclencheurs/automates sur antennes).

---

## 1. Analyse critique : ce que le module couvre — et ne couvre pas

### 1.1 Cartographie de couverture

| Concept métier (sources) | État dans le module (avant enrichissement) | Règle |
|---|---|---|
| Sectionneur manœuvré **hors charge** uniquement | ✅ Couvert et vérifié (rejeu par manœuvre, messages de parade contextuels) | R6bis, R18 |
| Dé-énergisation d'une section avant ouverture de **sectionnement** | ✅ Couvert (parking one-by-one, cross-feed, isolement par DJ d'abord) | R10, R10bis, R10ter |
| Boucle **courte** / boucle **longue** (ré-aiguillage) | ✅ Couvert, choix automatique par équipotentialité | R7-R9 |
| Ordonnancement de séquence (`listeDordre` libTOPO) | ✅ Couvert (phases 0→E) mais **sans taxonomie de types** exposée | R11 |
| « Un seul ouvrage hors tension à la fois » | ✅ Couvert (alerte non bloquante, mode smooth) | R10ter |
| Vérification post-manœuvre (partition nodale, écarts détaillés) | ✅ Couvert, indépendante des algorithmes (façade plugins) | R14, R15 |
| Dégradation gracieuse / connaître ses limites | ✅ Couvert (écarts, `noeuds_non_realisables`) — même philosophie que le POC CCO et l'outil Elia | R16 |
| **Classification des conséquences** d'une manœuvre (transit, boucle, mise sous/hors tension, HU…) | ❌ Absente : le vérificateur ne connaissait qu'*une* règle (sectionneur hors charge) | → **R20** |
| **Matrice d'autorisation** conséquence × organe (CCRT) | ❌ Absente (la règle du sectionneur n'en couvre qu'une ligne) | → **R21** |
| **Essai de jeu de barre** (remise sous tension d'une section par DJ, préférence ligne > couplage > TR, « force » du départ) | ❌ Absent : le séquenceur referme un sectionnement sur section morte sans essai préalable par disjoncteur | → **R22** |
| **Vocabulaire et états des départs** (désaiguillé, préparé, ±DA, en service, SUAV/MES/MHU) + table des transitions permises/interdites | ❌ Absent (les manœuvres portent une `raison` libre, aucun suivi d'état) | → **R23** |
| **Temporisations ACT 104** (10 s sectionneur, 1 min regonflage DJ, 2 min régleurs, PSEM, 1 min MHU→SUAV 225/400 kV) | ❌ Absentes (la séquence est purement ordinale) | → **R24** |
| **Contrôles SCADA attendus** (TS manque tension, TM I, conformité au calcul, contrôle nœuds) | ❌ Absents | → **R25** |
| **Validations électriques** (calcul de répartition à chaque coupure/établissement de transit, seuil % IST, plages U, Icc) | ❌ Absentes du module (assumé : R12 « simplifié ») — alors que le dépôt possède un backend pypowsybl complet | → **R26** (spécifiée) |
| **Ordre inter-postes / inter-tensions** (MHU du plus bas au plus haut niveau de tension, MES inversée pour les TR ; départ manœuvré au poste en service avant le poste hors tension) | ❌ Hors périmètre : le module est **mono-poste** (un voltage level) | → **R27** (spécifiée) |
| **Ouvrages multi-postes, zone de manœuvre** (départ + ouvrage + postes fictifs/piquages, anneau de garde, antennes) | ❌ Hors périmètre mono-poste ; le recommandeur possède par ailleurs une détection d'antennes (`graph_analysis/antenna_graph.py`) non reliée | → **R27** (spécifiée) |
| **Automates** (RA, AMU/RMU, RTS, zonaux) : mise hors service avant manœuvre, reprogrammation antennes | ❌ Absents — également non codés dans le POC CCO (données non numérisées, politiques régionales) | → **R28** (spécifiée, non planifiée) |
| Changement de barre **sans couplage et sans DA** (pseudo-couplage par les SA d'un départ) | ⚠ Partiel : le module traite le changement de barre par couplage ou boucle longue, pas le schéma miroir par pseudo-couplage | § 4.3 |
| Cas pathologiques de description des postes (omnibus, mix_elements) | ⚠ Partiel : omnibus et organes internes gérés ; pas de taux de couverture mesuré sur le réseau complet (le POC CCO annonce 98,77 %) | § 4.3 |

### 1.2 Lecture critique

**Ce que le module fait bien.** Le cœur « sûreté sectionneur » est plus abouti
que le POC CCO : la règle du sectionneur est vérifiée *manœuvre par manœuvre*
par rejeu indépendant (y compris pour des séquences éditées à la main), le
choix boucle courte/longue est déduit de l'équipotentialité réelle et non d'une
heuristique de phase, et les postes complexes (N barres, faisceaux partagés,
omnibus) sont couverts avec des filets de régression (goldens). La philosophie
« savoir dire je ne sais pas » (R16) est exactement celle préconisée par la
note CCO et par le retour d'expérience Elia.

**L'angle mort principal : un vérificateur mono-règle.** Avant enrichissement,
tout le « verdict » du module se réduisait à : *la partition est-elle atteinte*
(R14/R15) et *aucun sectionneur n'est-il manœuvré sous charge* (R18). Or l'art
de la manœuvre du CCRT est défini par **conséquences** : chaque manœuvre
*établit ou coupe un transit*, *ouvre ou ferme une boucle*, *met sous ou hors
tension*, *change le nombre de nœuds* ou *n'a aucun effet électrique* — et
c'est le couple (conséquence, type d'organe) qui est autorisé ou interdit. La
règle du sectionneur n'est qu'une ligne de cette matrice. Sans classification
des conséquences, le module ne peut ni motiver ses verdicts dans le langage des
opérateurs, ni produire les contrôles et temporisations qui font une fiche de
manœuvres exploitable.

**Le vocabulaire opérateur est absent.** La note CCO montre que les CCO
raisonnent en états de départs (« préparé », « en service - DA »…) et
d'ouvrages (« SUAV », « MES/MHU »). Le module produit des `OPEN/CLOSE` avec une
raison libre : correct topologiquement, mais inexploitable pour la validation
métier ou le dialogue avec un opérateur. La machine à états CCO fournit en
outre un **second vérificateur indépendant** (les transitions interdites de la
table croisent la règle du sectionneur par un autre chemin — défense en
profondeur).

**La séquence n'est pas encore une fiche de manœuvres.** Une fiche CCO comporte
l'ordre *et* les temporisations *et* les contrôles attendus *et* les
validations amont. Le module s'arrêtait à l'ordre. Les temporisations ACT 104
et les contrôles TS/TM sont pourtant dérivables en grande partie du seul graphe
et de la séquence (voir R24/R25) ; les validations électriques (R26)
nécessitent un load flow — disponible ailleurs dans ce dépôt.

**L'essai de barre est la règle experte manquante la plus structurante pour le
séquenceur.** Remettre sous tension une section morte doit se faire par un
**disjoncteur** (capable de couper sur défaut de barre), de préférence de
ligne, jamais par la simple fermeture d'un sectionneur ; l'ouvrage d'essai doit
être sous tension jusqu'au DJ d'essai ; le choix se fait par « force » du
départ. Le séquenceur actuel referme un sectionnement sur section morte
(conforme à la règle du sectionneur, la section étant hors tension) mais sans
essai préalable : les séquences de *remise en service* de sections s'écartent
donc de la pratique CCO. C'est détecté désormais (avertissement R22) ; la
*génération* de l'essai reste à intégrer au séquenceur (§ 4.2).

**Le périmètre mono-poste est le plafond de verre.** Ouvrages multi-postes,
ordre inter-tensions des transformateurs, essais par la liaison remise en
service, zone de manœuvre avec postes fictifs : tout cela exige une couche
d'orchestration au-dessus des `PosteTopologique` (§ 4.3). C'est cohérent avec
le contrat pluggable existant (phases A/B/C) : il manque une **phase D**
(plan multi-postes).

---

## 2. Règles consolidées R20-R28

Les règles R20-R25 sont **implémentées** dans
`manoeuvre/algo/conformite.py` (point d'entrée `analyser_conformite`, résultat
`ConformiteSequence` attaché à `ResultatManoeuvres.conformite` par
`plugins.pipeline.verifier_sequence`). Les règles R26-R28 sont **spécifiées**
(cibles d'implémentation). Traçabilité code/tests : [`regles.md`](regles.md).

Trois niveaux de verdict, volontairement séparés des champs historiques
(`ecarts`/`alertes`, dont le contenu est figé par les goldens) :

- **violation** — règle d'exploitation enfreinte (matrice CCRT, transition
  interdite) ;
- **avertissement** — bonne pratique non bloquante (essai de barre,
  transitions « bizarres ») ;
- **annotation** — information exécutoire (contrôles attendus, temporisations).

### R20 — Classification des conséquences de chaque manœuvre

Toute manœuvre est classée par rejeu sur le graphe du poste en conséquences
élémentaires CCRT : `manoeuvre_hors_tension`, `ouverture_boucle` /
`fermeture_boucle`, `preparer` / `desaiguiller`, `mise_sous_tension`,
`mise_hors_tension`, `etablir_transit`, `couper_transit`,
`changer_nb_noeuds`, `sans_effet`. Une manœuvre peut porter plusieurs
conséquences (ouvrir le DJ d'un départ de charge = coupure de transit **et**
mise hors tension).

Modèle d'énergisation mono-poste (hypothèse conservative documentée) : une
composante est **présumée sous tension** si elle contient au moins un
équipement « feeder » (ligne, transformateur, groupe, batterie, HVDC, ligne
frontière) ; charges et compensation sont passives. L'état de l'extrémité
distante d'une ligne étant inconnu, une coupure de transit peut en réalité
n'être qu'une mise hors tension d'un ouvrage à vide.

- **Code** : `conformite.classifier_manoeuvres`, `Consequence`,
  `familles_organes` (familles CCRT : DJ, interrupteur, SA d'aiguillage, SA de
  couplage, sectionnement SS, sectionneur de ligne).

### R21 — Matrice d'autorisation conséquence × organe (CCRT)

Un **sectionneur** (SA d'aiguillage, SA de couplage, sectionnement, sectionneur
de ligne) ne peut ni **établir** ni **couper un transit**, ni **changer le
nombre de nœuds** du poste — conséquences réservées au **disjoncteur** et à
l'**interrupteur**. Les manœuvres de boucle, hors tension, préparer /
désaiguiller sont autorisées pour tous. La mise sous/hors tension par
sectionneur est tolérée « sous conditions » (limitée au jeu de barres et au
départ — la mise sous tension d'une *section* déclenche l'avertissement R22).

Cette règle **généralise** R18 (sectionneur hors charge) : elle produit les
mêmes verdicts sur les cas couverts par R18, et couvre en plus le pontage de
deux nœuds par SA (fermeture) et la scission de nœuds par sectionneur.

- **Code** : `conformite.verifier_matrice_autorisation`,
  `CONSEQUENCES_INTERDITES_SECTIONNEUR`, `FAMILLES_SECTIONNEUR`.

### R22 — Essai de jeu de barre (remise sous tension d'une section)

La remise sous tension d'une section de barre morte se fait par un
**disjoncteur** (l'essai « couvre » un défaut de barre), de préférence dans
l'ordre : **DJ de ligne > DJ de couplage > DJ de transformateur** ; l'ouvrage
d'essai doit être sous tension jusqu'au DJ d'essai ; à défaut d'opportunité
(un départ déjà à manœuvrer), choisir le départ le plus « fort » (approximation
CCO : nombre de départs hors antennes du nœud distant). Après essai, ouvrir le
DJ, fermer le sectionnement, refermer le DJ.

- **Implémenté (détection)** : la mise sous tension d'une section par un
  sectionneur lève un **avertissement** (`verifier_matrice_autorisation`).
- **À implémenter (génération)** : insertion de la sous-séquence d'essai par le
  séquenceur (§ 4.2), choix du DJ d'essai par opportunité puis par force.

### R23 — Suivi d'état des départs (machine à états CCO)

État d'un départ = (nb de barres aiguillées par SA fermés) × (OC ligne) :
`désaiguillé`, `préparé`, `préparé - DA`, `en service`, `en service - DA`,
`bizarre non préparé fermé`. Les transitions observées lors du rejeu sont
confrontées à la table CCO : transitions **interdites** (ouvrir le dernier SA
d'un départ en service ; fermer un SA sous OC ligne fermé non préparé),
transitions **bizarres** (fermeture d'OC ligne d'un départ non préparé, DA sous
OC ouvert) et contrôles associés (±I/Scc à l'établissement/coupure, contrôle du
nombre de nœuds sur les ±DA).

L'état des **ouvrages** complets (en service / SUAV / hors tension = somme des
départs de *toutes* les extrémités) exige la vision multi-postes → R27.

- **Code** : `conformite.suivre_etats_departs`, `EtatDepart`,
  `_TRANSITIONS_DEPART`, `TransitionDepart`.

### R24 — Temporisations (consigne ACT 104)

À insérer dans la séquence :

| Temporisation | Portée | Statut |
|---|---|---|
| **10 s** après toute manœuvre de sectionneur (schémas fantômes) | graphe seul | ✅ implémentée |
| **60 s** min entre deux fermetures d'un même DJ (regonflage, cycle O-F-O) — le temps déjà écoulé en temporisations est décompté | graphe seul | ✅ implémentée |
| 2 min avant fermeture du DJ secondaire d'un TR neuf ou longuement consigné (régleurs) | contexte (historique de consignation) | spécifiée |
| 2 min entre manœuvres de sectionneurs d'un départ **PSEM** (manœuvres programmées) | données poste (technologie PSEM) | spécifiée |
| 1 min entre MHU et remise SUAV d'une liaison **225/400 kV** (surtensions) | suivi d'état d'**ouvrage** (R27) | spécifiée |

- **Code** : `conformite.calculer_temporisations`, `Temporisation`,
  `TEMPO_SECTIONNEUR_S`, `TEMPO_REGONFLAGE_DJ_S`.

### R25 — Contrôles SCADA attendus par manœuvre

Chaque manœuvre classée porte ses contrôles (tableau « contrôle lors des
manœuvres ») :

| Conséquence | Contrôle attendu |
|---|---|
| coupure de transit | TM I passe à 0 sur le départ |
| mise hors tension | TS absence de tension **apparaît** (si la TS existe), sections nommées |
| mise sous tension | TS absence de tension **disparaît**, sections nommées |
| établissement de transit | TM I/P/Q conformes au calcul de répartition ; si dépassement d'Icc, absence de TS PRESENCE dans les postes |
| changement du nombre de nœuds / ±DA | contrôle du nombre de nœuds du poste avant/après |
| boucle, manœuvre HU, préparer/désaiguiller | aucun |

La traduction en requêtes temps réel (TITIEN : libellés TS/TM, fenêtres
20 s avant / 30 s après, pas 10 s) est hors périmètre du module — l'annotation
fournit le *besoin* de contrôle, pas la requête.

- **Code** : `conformite._controles_pour` (porté par
  `ManoeuvreClassee.controles`), plus les contrôles de transition R23.

### R26 — Validations par calcul de répartition et de court-circuit *(spécifiée)*

Toute étape portant `etablir_transit` / `couper_transit` (et les fermetures de
couplage) doit être validée en amont par calcul de répartition (transits
< X % de l'IST — 80 % dans le POC CCO —, tensions dans les plages) ; les
établissements de transit exigent de plus une validation Icc. Le cadre de
référence en manœuvre diffère du cadre général (pas d'étude N-1, dépassements
d'Icc possibles sous condition d'absence de personnel).

**Chemin d'implémentation dans ce dépôt** : la classification R20 fournit
exactement les étapes à valider ; le backend pypowsybl du recommandeur
(`pypowsybl_backend/simulation_env.py`, variantes réseau, load flow) fournit le
simulateur. Un `ValidateurElectrique` (phase de vérification optionnelle,
activée quand un réseau pypowsybl est disponible — IHM et dataset RTE-7000 en
ont un) appliquerait les lots de manœuvres entre étapes à valider, comme décrit
dans la note CCO. C'est le pont naturel entre le module manœuvre (aujourd'hui
autonome) et le reste du recommandeur.

### R27 — Ouvrages multi-postes et ordre inter-tensions *(spécifiée)*

Couche d'orchestration au-dessus des postes (« phase D », § 4.3) :

1. **Ouvrages** : un ouvrage = ses départs dans chaque poste + la
   liaison/le TR ; états en service / SUAV / hors tension par somme des états
   de départs (R23) ; postes fictifs (piquages, 3 enroulements) absorbés dans
   l'ouvrage.
2. **Ordre inter-postes** : manœuvrer le départ du poste **en service** avant
   celui du poste hors tension (l'essai de barre est fait par la liaison que
   l'on remet en service) ; tri MHU par tension croissante, **MES par tension
   décroissante pour les transformateurs** (MHU du TR par le plus bas niveau,
   MES par le plus haut).
3. **Zone de manœuvre** : postes modifiés + anneau de garde de profondeur 1
   (en sautant les postes fictifs), antennes identifiées — se raccorder à la
   détection d'antennes du recommandeur.
4. Temporisation « 1 min MHU→SUAV 225/400 kV » (R24) : détectable à ce niveau.

### R28 — Automates *(spécifiée, non planifiée)*

Les manœuvres réelles modifient presque toujours des automates (RA, AMU/RMU,
RTS, automates zonaux) : mise hors service avant manœuvre, remise en service
après, reprogrammation des renvois lors des mises en/hors antenne (AUT 102).
Les données (Tomate II, TITIEN) ne sont pas dans le fichier réseau : hors de
portée du module. Le modèle de résultat doit en revanche pouvoir **porter
l'alerte** : toute séquence qui met un ouvrage en antenne ou l'en sort devrait
signaler « vérifier la position des réenclencheurs (AUT 102) ». La détection
d'antenne au niveau zone (R27) en est le prérequis.

---

## 3. Correspondance avec la typologie CCO (`listeDordre` à ~20 codes)

La méthode CCO ordonne les manœuvres par **types** globaux (un ordre unique qui
respecte toujours l'art de la manœuvre). Correspondance avec les phases du
séquenceur et la classification R20 :

| Code CCO | Sens | Équivalent module |
|---|---|---|
| `mhu_fermeture` / `mhu_ouverture` | manœuvre d'OC en zone hors tension | R20 `manoeuvre_hors_tension` |
| `fermeture_boucle` / `ouverture_boucle` | OC de JdB sans changement de nœud | R20 `fermeture/ouverture_boucle` ; phases 0/D |
| `prep_essai_-u`, `prep_essai_des`, `essai_section_pp`, `essai_section_+u`, `essai_section_-u`, `+u_section_ss`, `essai_section_des` | sous-séquence d'essai de jeu de barre | **manquant** (R22 génération, § 4.2) |
| `preparer` / `desaiguiller` | fermeture du 1er SA / ouverture du dernier SA | R20 `preparer`/`desaiguiller` ; boucle longue R9 |
| `+u_dep` / `-u_dep` | fermeture / ouverture d'un DJ de départ | R20 `etablir_transit`/`couper_transit`+`mise_*_tension` |
| `+da` / `-da` | fermeture / ouverture d'un second SA | R20 boucles + transition R23 `en service ↔ en service - DA` |
| `-u_section_ss_def` | changement de barre sans couplage, dernier désaiguillage | partiel (§ 4.3, pseudo-couplage) |

L'ordre global CCO (essais avant mises en service, `+u_dep` avant `-u_dep`,
désaiguillages en dernier) est **compatible** avec l'ordre R11 du séquenceur ;
la classification R20 permet désormais d'**annoter** chaque manœuvre du code
CCO correspondant (utile pour l'IHM et la comparaison à des fiches réelles).

---

## 4. Restructuration proposée (module, algo, vérificateur)

### 4.1 Vérificateur : d'une règle unique à un pipeline de contrôleurs — **fait**

Le vérificateur devient un empilement de contrôleurs indépendants, tous fondés
sur le **rejeu** (l'invariant « `poste.graph` jamais muté » le permet) :

```
verifier_sequence (façade plugins — verdict unique pour tout algorithme)
├── rejeu topologique        → is_verified / is_verified_detaillee / ecarts   (R14, R15)
├── sûreté sectionneur       → ecarts (bloquant)                              (R18)
├── bonne pratique smooth    → alertes (« un seul ouvrage HS »)               (R10ter)
└── conformité art de la manœuvre → conformite (nouveau champ)                (R20-R25)
    ├── classification des conséquences + familles d'organes                  (R20)
    ├── matrice d'autorisation CCRT   → violations                           (R21)
    ├── essai de barre                → avertissements                        (R22)
    ├── machine à états des départs   → violations / avertissements          (R23)
    ├── temporisations ACT 104        → annotations                          (R24)
    └── contrôles SCADA attendus      → annotations                          (R25)
```

Chaque étage est appelable isolément (API publique), la façade les agrège. Les
champs historiques restent inchangés (compatibilité des goldens et de l'IHM) ;
`ResultatManoeuvres.conformite` porte le nouveau verdict. À terme, si le nombre
de contrôleurs croît (R26+), promouvoir `verification.py` + `conformite.py`
en package `algo/verification/` avec un protocole `Controleur` commun.

### 4.2 Algorithme : générer ce que le vérificateur exige

Par ordre de valeur :

1. **Essai de barre (R22, génération)** — dans le séquenceur, remplacer la
   fermeture directe d'un sectionnement sur section morte par la sous-séquence
   CCO : choisir le DJ d'essai (opportunité → force, ligne > couplage > TR),
   essayer (+DJ), rouvrir (-DJ), fermer le SS, refermer le DJ. Points
   d'insertion : `determiner_manoeuvres_avec_sections` (fermetures de
   sectionnement) et `_aligner_couplers_sur_cible`. Le vérificateur R22 sert de
   test d'acceptation (0 avertissement attendu sur les séquences générées).
2. **Annotation en codes CCO** — porter sur `Manoeuvre` un champ
   `type_manoeuvre` déduit de R20/R23 (table § 3). Aucune modification de
   l'ordre : uniquement de l'explicabilité (IHM, comparaison à des fiches
   réelles du dataset RTE-7000).
3. **Plan temporisé** — un `PlanManoeuvres` = séquence + temporisations R24
   intercalées + contrôles R25, prêt pour une exécution/animation IHM (l'IHM
   anime déjà les séquences ; les temporisations donnent l'échelle de temps).
4. **Changement de barre sans couplage et sans DA** (schéma miroir par
   pseudo-couplage des SA d'un départ, essai de la barre d'arrivée compris) —
   complète les limites « cible arbitraire » et « barre de réserve fusionnée ».

### 4.3 Module : ouvrir le périmètre mono-poste (phase D)

Le contrat pluggable actuel (A identification, B séquencement, C planification)
est **par poste**. Ajouter une **phase D — plan de manœuvre multi-postes** :

```
PlanificateurZone (phase D, nouveau)
  entrées : situation initiale + cible (réseau), liste de postes impactés
  1. zone de manœuvre (postes + anneau de garde, postes fictifs absorbés)   (R27.3)
  2. ouvrages et leurs départs ; états ES/SUAV/HU                            (R27.1)
  3. par poste : phases A/B/C existantes (inchangées)
  4. entrelacement inter-postes : poste en service d'abord, tri par tension,
     règle TR (MES tension décroissante)                                     (R27.2)
  5. temporisations d'ouvrage (1 min MHU→SUAV 225/400 kV)                    (R24)
  6. validations électriques sur les étapes à transit (backend pypowsybl)    (R26)
  7. alerte automates sur mise/fin d'antenne                                 (R28)
```

Le module reste sans dépendance au reste du recommandeur pour les étapes 1-5 ;
l'étape 6 introduit une dépendance **optionnelle** (protocole `Simulateur`
injecté, implémenté par `pypowsybl_backend` côté recommandeur — le module ne
l'importe pas). La brique dataset (RTE-7000, chronologies de topologies) fournit
les cas de test réels : les journées à re-groupements de nœuds
(`dataset/exploration.py`) donnent des couples avant/après pour confronter les
plans générés aux manœuvres réellement observées — la méthode de validation
que la note CCO appelle de ses vœux (« % de réussite, % de non-fait, absence
d'échec »).

### 4.4 Ce qu'il ne faut PAS restructurer

- **Le cœur séquenceur (R1-R19)** : éprouvé par goldens et postes réels ; les
  enrichissements s'ajoutent en couches (vérification, annotation, génération
  d'essais) sans en modifier l'architecture.
- **La séparation `ecarts`/`alertes`/`conformite`** : fusionner les verdicts
  casserait les goldens et mélangerait sûreté (bloquante) et conformité
  métier (contextuelle).
- **L'autonomie du module** vis-à-vis de grid2op/du recommandeur : les ponts
  (R26, antennes) passent par des protocoles injectés, pas par des imports.

---

## 5. Hypothèses et limites du vérificateur de conformité

1. **Présomption de tension** (mono-poste) : les feeders (ligne, TR, groupe)
   sont présumés sous tension. Conséquences : une manœuvre sur un ouvrage
   réellement consigné peut être classée « transit » (conservatif — faux
   positif possible sur les cellules de ré-aiguillage sans DJ) ; une coupure de
   transit peut en réalité être une MHU d'ouvrage à vide. La levée de cette
   hypothèse exige la vision zone (R27) ou l'état électrique réel (R26).
2. **Pas de calcul électrique** : les conséquences sont topologiques ; les
   valeurs (transits, tensions, Icc) relèvent de R26.
3. **Machine à états** : les cellules sans OC ligne propre (ré-aiguillage
   direct) sont hors modèle CCO ; les ouvrages multi-OC-lignes sont suivis par
   l'état agrégé de leurs OC (comme le POC CCO, qui documente la même limite).
4. **Temporisations** : sous-ensemble calculable du graphe (10 s sectionneur,
   60 s regonflage DJ) ; les temporisations contextuelles (régleurs TR, PSEM,
   MHU→SUAV 225/400 kV) sont listées mais non calculées (R24).

---

## 6. Références croisées

- Spécification des règles implémentées (traçabilité code/tests) :
  [`regles.md`](regles.md) — R20-R25 y sont référencées.
- Code : `expert_op4grid_recommender/manoeuvre/algo/conformite.py` ;
  intégration `manoeuvre/plugins/pipeline.py::verifier_sequence`.
- Tests : `tests/manoeuvre/test_conformite_art_manoeuvre.py`.
- Architecture plugins (phases A/B/C) : `docs/architecture/plugins.md`.
- Dataset de validation : `docs/manoeuvre/dataset_rte7000/`.
