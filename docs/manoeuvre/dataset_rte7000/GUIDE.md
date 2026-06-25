# Guide — Dataset « blocs de transition RTE 7000 » : contenu, observations, exécution locale

> Ce guide documente le dataset **construit** par le pipeline du plan
> `docs/manoeuvre/dataset_rte7000/plan.md` à partir des instantanés publics
> [`OpenSynth/D-GITT-RTE7000-{2021,2022,2023}`](https://huggingface.co/datasets/OpenSynth/D-GITT-RTE7000-2021)
> (licence CDLA-permissive-2.0), les **observations** tirées de la campagne
> 7 journées / 3 ans, et la **procédure pas-à-pas** pour reproduire et
> étendre le traitement sur votre machine.

---

## 1. Ce que le pipeline construit

Le dataset source est une série d'instantanés **XIIDM complets du réseau de
transport français** (~5 865 postes, ~86 000 organes DJ+SA, topologie
node-breaker), un fichier toutes les **5 minutes**. Le pipeline en dérive,
par poste électrique :

1. la **chronologie** des topologies détaillées (`{organe: ouvert ?}`) ;
2. les **états stables** (plateaux ≥ `min_stabilite` snapshots, 10 min par
   défaut) et les **oscillations** repliées (A → bruit → A) ;
3. les **blocs de transition** : topologie stable de départ → topologie
   stable cible, avec les états transitoires et les **manœuvres réellement
   exécutées par l'opérateur** (ordonnées, horodatées à 5 min près) ;
4. le **tag d'intervention** de chaque bloc (consignation, remise en
   service, scission/fusion de nœud, ré-aiguillage, sectionnement…), calculé
   **structurellement** sur le graphe node-breaker du poste — la structure
   étant extraite du dataset lui-même (premier instantané de la période) ;
5. le **catalogue des topologies distinctes** rencontrées (stables ou non),
   base des **scénarios combinés** : toute paire de topologies stables d'un
   même poste est un scénario départ → cible réaliste, potentiellement
   jamais observé et plus dur que les blocs réels ;
6. les **benchmarks** du séquenceur de manœuvres (`manoeuvre/`) contre la
   borne basse (nb d'organes changés) et contre la **séquence opérateur**.

### Artefacts produits par journée (`out_rte7000/<YYYYMMDD>/`)

| Fichier | Contenu | Volume typique |
|---|---|---|
| `blocs.jsonl` | 1 ligne = 1 bloc au format scénario (cf. schéma ci-dessous) | ~25 Mo |
| `topologies.jsonl` | 1 ligne = 1 topologie distincte du catalogue | ~15 Mo |
| `scenarios/*.json` | les mêmes blocs, un fichier par bloc (format IHM/tests) | ~6 000 fichiers |
| `sequences/*.json` | séquences opérateur observées (format `tests/manoeuvre/sequences`) | ~6 000 fichiers |
| `stats.json` | agrégats : par tag, par poste, tailles, catalogue | ~80 Ko |

### Schémas JSON

Ligne de `blocs.jsonl` (= contenu d'un `scenarios/*.json`) :

```json
{
  "voltage_level_id": "CONCAP3",
  "name": "CONCAP3_2021-01-03T11_55_reaiguillage_departs",
  "depart":  {"<organe>": false, "...": true},   // true = OUVERT
  "cible":   {"<organe>": true,  "...": true},
  "depart_nodale": [["DEP1","DEP2"], ["DEP3"]],  // partitions nodales (si structure)
  "cible_nodale":  [["DEP1"], ["DEP2","DEP3"]],
  "meta": {
    "source": "historique",
    "tags": ["reaiguillage_departs"],
    "t_depart": "2021-01-03T11:35", "t_cible": "2021-01-03T11:55",
    "topologie_depart_id": "…", "topologie_cible_id": "…",   // hash canonique
    "nb_organes_changes": 16,
    "nb_manoeuvres_observees": 16,
    "nb_etats_transitoires": 3,
    "duree_stable_avant": 12, "duree_stable_apres": 31,      // en snapshots
    "retour_observe": false
  }
}
```

Ligne de `topologies.jsonl` (catalogue, postes à ≥ 2 topologies stables) :

```json
{
  "voltage_level_id": "VAUJAP1",
  "topologie_id": "153a7946…",            // sha1 canonique des (organe, état)
  "etats": {"<organe>": false, "...": true},
  "premiere": "2021-01-03T00:00", "derniere": "2021-01-03T23:55",
  "nb_snapshots": 212, "nb_episodes": 3, "stable": true
}
```

Séquence observée (`sequences/*_observee.json`) : champs des séquences du
dépôt (`depart`, `cible`, `manoeuvres[{ordre, switch_id, action, raison}]`,
`mode: "observee"`). **Attention** : à l'intérieur d'une même fenêtre de
5 min, l'ordre des manœuvres est alphabétique (l'ordre inter-fenêtres est
réel).

Résultat de benchmark (`benchmark*.json`) : `resume` agrégé par algo/mode +
`resultats[]` par bloc (`nb_manoeuvres`, `is_verified` = partition atteinte,
`is_verified_detaillee` = état d'organes exact, `nb_ecarts`, `nb_alertes`,
`temps_s`, et les références `borne_basse` / `nb_manoeuvres_observees`).

### Ce qui est versionné dans ce dépôt vs régénérable

Versionné (ce dossier `docs/manoeuvre/dataset_rte7000/`) : la table de campagne
(`campagne/campagne.json`), les stats par journée (top 50 postes), les
résumés de benchmarks (agrégats + tous les blocs non triviaux), des
échantillons de scénarios/séquences, la journée pilote détaillée
(`2021-01-03/`). **Jamais versionné** : les XIIDM bruts (~430 Mo/jour) et
les sorties complètes (~70 Mo/jour) — tout se reconstruit avec les commandes
du § 3.

---

## 2. Observations (campagne 7 journées, 2021-2023)

Voir la table complète dans `README.md` (et `campagne/campagne.json`).
Synthèse des constats :

1. **Volume et stabilité** : 46 732 blocs sur 7 journées ; le volume
   quotidien est remarquablement stable (6 200-7 200 blocs, ~2 000 postes
   actifs sur ~5 865) quel que soit le jour ou la saison.
2. **Cycles journaliers dominants** : ~2/3 des blocs sont des couples
   `remise_en_service` matinale (~07-09 h) / `consignation_ouvrage`
   vespérale (~17-21 h) sur les mêmes postes — exploitation quotidienne
   (lignes/transformateurs cyclés), à séparer des reconfigurations.
3. **Effet jour-de-semaine ≫ effet saison** pour les vraies
   **reconfigurations structurelles** (scission/fusion/ré-aiguillage/
   sectionnement) : 36 le dimanche d'hiver contre 123-208 les jours ouvrés
   (max : mardi d'automne 2021-10-12, 208) — signature des chantiers.
4. **Tailles de transition** : 95 % des blocs changent ≤ 2 organes ; la
   queue va jusqu'à 38 organes (2023-02-08). Diff moyen ≈ 1,5 organe.
5. **Identifiants stables 3 ans** : > 1 000 postes ont des paires de
   topologies à organes strictement identiques entre 2021 et 2023 (vérifié
   par la garde de couverture des combinaisons) ; les exceptions (18 paires
   écartées sur le jeu 3 ans) tracent les **évolutions de structure**.
6. **Benchmark vs opérateur (blocs observés ≥ 2 organes, 2 842 cas)** :
   partition atteinte 2 842/2 842 dans les deux modes ; **2 777 blocs
   résolus avec exactement le nombre de manœuvres de l'opérateur** ;
   moyenne ×0,99 (l'algorithme est parfois plus court que l'observé).
   Divergences instructives : `KERHEP3 09:10` (opérateur 3, algo 9-11) et
   `JUINEP4 15:20` (opérateur 7 = aggressive, smooth 11).
7. **Les scénarios combinés graduent la difficulté** là où les blocs
   observés saturent : sur le jeu 3 ans (3 061 exécutés, diff médian 7,
   max 57), partitions **94,2 % (smooth)** vs **99,7 % (aggressive)** ;
   réussite « détaillée exacte » (aggressive) ~98 % jusqu'à 29 organes,
   77 % sur 30-39. L'« échec » aggressive `BIACAP3` (diff 5) montre que la
   difficulté n'est pas que dimensionnelle.
8. **Limites d'interprétation** : résolution 5 min (manœuvres co-fenêtrées
   ordonnées alphabétiquement ; une « manœuvre opérateur » peut en cacher
   plusieurs) ; `min_stabilite=2` (10 min) définit la frontière
   bloc/oscillation ; tags `reconfiguration_durable` non calculables sur une
   journée isolée ; ~1/3 des blocs restent `inclasse` (majoritairement des
   organes hors cellules de départ — taxonomie à affiner).

---

## 3. Exécuter localement sur d'autres journées

### Prérequis

```bash
git clone https://github.com/marota/expert_op4grid_recommender
cd expert_op4grid_recommender
pip install pypowsybl networkx pandas      # cœur du pipeline
pip install pytest                          # optionnel : suite de tests
```

Aucun token Hugging Face n'est requis (dataset public). Le téléchargeur
maison `scripts/download_dgitt_subset.py` n'utilise que HTTP standard
(API `/tree` + `/resolve`), vérifie chaque fichier contre son jumeau
`*.md5`, reprend où il s'était arrêté et réessaie les `403` épisodiques du
rate-limiter anonyme. (Le client officiel `hf download` fonctionne aussi en
local si votre réseau atteint `*.xethub.hf.co` — mais il laisse un
`.cache/` dans le dossier de sortie ; le pipeline l'ignore.)

Dimensionnement constaté (machine 4 cœurs / 15 Go) :

| Étape | Durée / journée | RAM | Disque |
|---|---|---|---|
| Téléchargement (288 instantanés) | 3-10 min | — | ~430 Mo |
| Pipeline blocs+catalogue | 25-35 min (≈ 12 min lecture + 15-20 min structures) | < 4 Go | ~70 Mo |
| Benchmark (par millier de blocs) | ~10-15 min | < 2 Go | ~1 Mo |

### Une journée, pas à pas

```bash
# 1. Télécharger (l'arborescence YYYY/MM/DD du dataset est préservée)
python scripts/download_dgitt_subset.py \
    --repo OpenSynth/D-GITT-RTE7000-2022 --prefix 2022/03/09 \
    --output data/dgitt_rte7000_2022

# 2. Blocs + tags + catalogue (structures auto-extraites du 1er instantané)
python scripts/build_rte7000_blocks.py \
    --input data/dgitt_rte7000_2022/2022/03/09 \
    --output out_rte7000/20220309

# 3. (option) Benchmark séquenceur vs opérateur sur les blocs ≥ 2 organes
python scripts/run_benchmark.py \
    --dataset out_rte7000/20220309 --min-organes 2 \
    --structures-xiidm data/dgitt_rte7000_2022/2022/03/09/recollement-auto-20220309-0000-enrichi.xiidm.bz2
```

Options utiles de `build_rte7000_blocks.py` : `--vl POSTE` (répétable, pour
se limiter à quelques postes — beaucoup plus rapide), `--sous-echantillon N`
(1 instantané sur N), `--min-stabilite N` (définition d'« état stable »),
`--seuil-durable N` (tag `reconfiguration_durable` sur plateaux longs),
`--structures-xiidm CHEMIN` (instantané de référence des structures, défaut :
le premier de `--input`).

### Plusieurs journées d'un coup

```bash
# Une semaine de juillet 2021 + un contraste hiver 2023 :
bash scripts/process_dgitt_days.sh \
    2021/07/12 2021/07/13 2021/07/14 2021/07/15 2021/07/16 \
    2023/02/08
```

Le script déduit le dépôt HF de l'année, télécharge séquentiellement (doux
avec le rate-limiter), enchaîne les pipelines avec **au plus 2 concurrents**
(`-j` pour changer ; comptez ~4 Go de RAM par pipeline), journalise dans
`out_rte7000/<YYYYMMDD>.log` et termine par un résumé par journée. Relance
idempotente : ce qui est déjà téléchargé/produit n'est pas refait pour le
téléchargement (vérification md5) ; un pipeline se relance, lui, entièrement.

### Scénarios combinés et benchmark de difficulté

```bash
# Fusionner les catalogues de plusieurs journées (saisons/années) :
python scripts/build_combination_scenarios.py \
    --catalogue out_rte7000/20210715/topologies.jsonl \
    --catalogue out_rte7000/20230208/topologies.jsonl \
    --max-par-poste 3 --min-organes 4 \
    --output out_combinaisons/blocs_combinaisons.jsonl

# Les benchmarker (structures = un instantané d'une des journées sources ;
# la garde de couverture écarte les postes dont la structure a changé) :
python scripts/run_benchmark.py \
    --dataset out_rte7000/20230208 \
    --blocs out_combinaisons/blocs_combinaisons.jsonl \
    --structures-xiidm data/dgitt_rte7000_2023/2023/02/08/recollement-auto-20230208-0000-enrichi.xiidm.bz2 \
    --output out_combinaisons/benchmark.json
```

### Pièges connus

- **Ne committez jamais** les XIIDM bruts ni les sorties complètes (`/data`
  est ignoré par git ; les artefacts à versionner sont des **résumés**).
- **Mémoire** : la tenue en mémoire d'une journée France entière repose sur
  le **partage structurel des états** dans `charger_timelines_xiidm`
  (mémoire en O(changements)) — verrouillé par
  `test_charger_timelines_xiidm_partage_structurel_des_etats`. Sur des
  périodes plus longues qu'une journée, traitez **journée par journée**
  (un appel pipeline par jour) plutôt qu'en un seul appel.
- **Fixtures du dépôt ≠ dataset** : les fixtures `tests/manoeuvre/fixtures`
  utilisent d'autres identifiants d'organes (export normalisé `_OC`) ; le
  tagging structurel sur données D-GITT doit utiliser les structures
  extraites du dataset (comportement par défaut). La garde
  `couverture_structure` écarte automatiquement toute structure inadaptée.
- **Artefacts de cache** : seuls les vrais suffixes `*.xiidm[.bz2|.gz]` sont
  lus ; les `*.lock`/`*.metadata`/`*.incomplete` d'un `hf download`
  interrompu sont ignorés.
- **Choix des journées** : pour des statistiques d'interventions, privilégiez
  des jours **ouvrés** (les dimanches sous-échantillonnent les
  reconfigurations ×5) et étalez les saisons ; vérifiez la complétude via
  l'API (`…/tree/main/YYYY/MM/DD`, 288 fichiers attendus, parfois moins).
