# Pilote RTE 7000 — journée 2021-01-03 (passe France entière)

Premier traitement réel du dataset Hugging Face
[`OpenSynth/D-GITT-RTE7000-2021`](https://huggingface.co/datasets/OpenSynth/D-GITT-RTE7000-2021)
(licence CDLA-permissive-2.0) par le pipeline du plan
`docs/manoeuvre/dataset_rte7000/plan.md` — phases 1 à 4 exécutées sur **une journée
complète** (287 instantanés XIIDM à 5 min, ~5 865 postes, ~86 000 organes).

## Reproduction

```bash
# 1. Télécharger la journée (≈ 430 Mo — ne PAS committer les données)
python scripts/download_dgitt_subset.py --prefix 2021/01/03 \
    --output data/dgitt_rte7000_2021

# 2. Blocs de transition + tagging (structures extraites du 1er instantané)
python scripts/build_rte7000_blocks.py --input data/dgitt_rte7000_2021 \
    --output out_rte7000_20210103

# 3. Benchmark du séquenceur sur les reconfigurations réelles
python scripts/run_benchmark.py --dataset out_rte7000_20210103 \
    --structures-xiidm data/dgitt_rte7000_2021/2021/01/03/recollement-auto-20210103-0000-enrichi.xiidm.bz2 \
    --tag scission_noeud --tag fusion_noeuds --tag reaiguillage_departs \
    --tag sectionnement_barre \
    --output out_rte7000_20210103/benchmark_reconfigurations.json
```

## Contenu

| Fichier | Quoi |
|---|---|
| `stats.json` | Statistiques complètes de la journée (par tag, par poste, tailles) |
| `benchmark_reconfigurations.json` | Benchmark séquenceur vs opérateur sur les 31 reconfigurations |
| `scenarios_echantillon/` | 7 scénarios représentatifs (format `tests/manoeuvre/scenarios` + partitions nodales + meta) |
| `sequences_echantillon/` | Les 7 séquences **réellement exécutées** correspondantes (résolution 5 min) |

La sortie complète (6 233 scénarios + 6 233 séquences, ~70 Mo) n'est pas
versionnée : elle se reconstruit à l'identique avec les commandes ci-dessus.

## Chiffres clés de la journée

- **6 233 blocs de transition** sur **1 861 postes** (sur 5 865), tous avec
  séquence observée ; 2 249 oscillations repliées (A → bruit → A).
- 1 861/1 861 structures de postes extraites du premier instantané
  (0 échec, 0 incompatibilité d'identifiants) → tagging **structurel** et
  partitions nodales calculées pour 100 % des blocs.
- Tailles de transition : 1 organe × 3 391, 2 organes × 2 768, ≥ 3 organes
  × 74 (max : 16 organes, CONCAP3).
- Tags : `inclasse` 2 138, `consignation_ouvrage` 2 053, `remise_en_service`
  2 034, `fusion_noeuds` 15, `scission_noeud` 12, `reaiguillage_departs` 5,
  `sectionnement_barre` 4 (multi-label).
- Rythmes d'exploitation visibles : nombreux couples « remise en service
  matinale (~08-09 h) / consignation vespérale (~17-18 h) » sur les mêmes
  postes (cycles journaliers).

## Benchmark séquenceur (31 reconfigurations structurelles)

| Algo | Partition atteinte | Détaillée exacte | Manœuvres / borne basse | Manœuvres / **opérateur** |
|---|---|---|---|---|
| libtopo smooth | 31/31 | 30/31 | ×1,17 | **×1,10** |
| libtopo aggressive | 31/31 | 31/31 | ×1,11 | **×1,06** |

## Benchmark élargi (2 842 blocs ≥ 2 organes)

`benchmark_min2organes.resume.json` (résumé + les 66 blocs « non triviaux » ;
le JSON complet, 2,3 Mo, se régénère avec `--min-organes 2`) :

| Algo | Partition atteinte | Détaillée exacte | Manœuvres / borne basse | Manœuvres / **opérateur** |
|---|---|---|---|---|
| libtopo smooth | **2 842/2 842** | 2 841/2 842 | ×1,00 | ×0,99 |
| libtopo aggressive | **2 842/2 842** | **2 842/2 842** | ×1,00 | ×0,99 |

0 erreur d'exécution ; **2 777/2 842 blocs résolus avec exactement le nombre
de manœuvres de l'opérateur** (médiane ×1,00). Le seul bloc où l'algorithme
fait plus que l'opérateur est `KERHEP3 09:10` (9 vs 3, cf. ci-dessous) ; sur
une soixantaine de blocs, l'algorithme trouve une séquence **plus courte**
que celle observée (étapes opérateur supplémentaires réelles, ou artefacts
de la fenêtre de 5 min).

Cas remarquables (dans l'échantillon) :

- `CONCAP3 … reaiguillage_departs` : ré-aiguillage de **16 organes** —
  séquenceur = opérateur, **16 manœuvres** dans les deux modes ;
- `KERHEP3 … remise_en_service` (+ scission + sectionnement) : l'opérateur
  fait 3 manœuvres là où smooth en produit 11 (non exacte) et aggressive 9 —
  cas d'analyse d'écart type pour le papier ;
- `JUINEP4 … scission_noeud` : l'opérateur exécute **7** manœuvres pour un
  diff de 3 organes (étapes intermédiaires réelles) ; aggressive retrouve
  **exactement 7**, smooth 11.

## Lecture des limites

- La « séquence observée » est échantillonnée à **5 min** : plusieurs
  manœuvres dans une même fenêtre sont ordonnées alphabétiquement à
  l'intérieur du pas (l'ordre inter-pas est, lui, réel).
- `min_stabilite=2` (10 min) filtre le bruit ; les épisodes plus brefs sont
  comptés comme oscillations, pas comme blocs.
- Journée unique : les tags `reconfiguration_durable` (plateau long) ne sont
  pas calculables ; les volumes par tag ne sont pas encore représentatifs de
  l'année.
