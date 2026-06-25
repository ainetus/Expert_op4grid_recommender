# Campagne D-GITT RTE 7000 — 7 journées, 3 ans (2021-2023)

Traitement du dataset Hugging Face `OpenSynth/D-GITT-RTE7000-{2021,2022,2023}`
par le pipeline du plan `docs/manoeuvre/dataset_rte7000/plan.md` : **7 journées
complètes** (≈ 2 000 instantanés XIIDM à 5 min), choisies pour contraster
jours de semaine / week-end, saisons et années.

> **Guide complet** (schémas des artefacts, observations détaillées, et
> **comment lancer le traitement localement sur d'autres journées**) :
> [`GUIDE.md`](GUIDE.md). Multi-journées en une commande :
> `bash scripts/process_dgitt_days.sh 2021/07/12 2021/07/13 …`

Reproduction d'une journée (~430 Mo téléchargés, jamais committés) :

```bash
python scripts/download_dgitt_subset.py --repo OpenSynth/D-GITT-RTE7000-2022 \
    --prefix 2022/06/15 --output data/dgitt_rte7000_2022
python scripts/build_rte7000_blocks.py \
    --input data/dgitt_rte7000_2022/2022/06/15 --output out_rte7000_20220615
```

## Table de campagne (`campagne/campagne.json`)

| Date | Jour | Saison | Blocs | Postes | Reconfig. structurelles | Max organes | Topologies stables |
|---|---|---|---|---|---|---|---|
| 2021-01-03 | dimanche | hiver | 6 233 | 1 861 | **36** | 16 | 4 066 |
| 2021-01-05 | mardi | hiver | 6 554 | 2 011 | 123 | 20 | 4 644 |
| 2021-04-14 | mercredi | printemps | 7 216 | 2 024 | 158 | 18 | 4 742 |
| 2021-07-15 | jeudi | été | 7 127 | 2 096 | 203 | 26 | 4 853 |
| 2021-10-12 | mardi | automne | 6 760 | 2 056 | **208** | 31 | 4 766 |
| 2022-06-15 | mercredi | été | 6 400 | 2 037 | 181 | 26 | 4 708 |
| 2023-02-08 | mercredi | hiver | 6 442 | 1 984 | 185 | **38** | 4 577 |

**46 732 blocs de transition** au total, tous avec séquence opérateur
observée. Lectures :

- le volume quotidien est remarquablement stable (6 200-7 200 blocs/jour,
  ~1 900-2 100 postes actifs) — dominé par les cycles journaliers
  consignation/remise en service ;
- les **reconfigurations structurelles** (scission/fusion/ré-aiguillage/
  sectionnement) varient fortement : **×5,8 entre dimanche (36) et mardi
  d'automne (208)** — l'effet jour-de-semaine (chantiers) domine l'effet
  saison ;
- les identifiants d'organes sont stables sur les 3 ans pour ≥ 1 068 postes
  (vérifié par les paires de combinaisons inter-années à organes identiques).

## Scénarios combinés (`combinaisons/`)

Les **catalogues de topologies stables** (`topologies.jsonl`, ~4 600/jour)
permettent de générer des paires « topologie de départ → topologie cible »
**jamais observées** mais dont les deux états sont réels
(`scripts/build_combination_scenarios.py`) — des cas plus durs et plus longs
que les blocs observés (diff médian 6-7 organes vs 1-2 observé) :

| Jeu | Combinaisons | Postes | Diff (min/méd/max) | smooth : partitions | aggressive : partitions |
|---|---|---|---|---|---|
| Intra-journée 2021-01-03 | 424 | 122 | 3/3/19 | 421/424 | **424/424** |
| Inter-saisons 2021 (5 jours) | 2 277 | 796 | 4/6/56 | 2 166/2 274 (95,3 %) | **2 264/2 274 (99,6 %)** |
| 3 ans (7 jours, dont 1 488 paires inter-années) | 3 079 | 1 068 | 4/7/57 | 2 884/3 061 (94,2 %) | **3 052/3 061 (99,7 %)** |

Sur le jeu 3 ans : 18 paires écartées par la garde de couverture (structure
du poste ayant évolué entre les années — décision B du plan en miniature) ;
détaillée exacte 2 955/3 061 (aggressive) ; ×1,06 vs borne basse ; 0 erreur
d'exécution.

Sur l'inter-saisons 2021 : réussite « détaillée exacte » (aggressive) de
98 % jusqu'à 29 organes de diff, 77 % sur 30-39 ; ×1,07 vs borne basse ;
0 erreur d'exécution. Le jeu combiné **discrimine** les algorithmes là où
les blocs observés (presque tous ≤ 2 organes) ne le faisaient pas — c'est le
jeu de difficulté du benchmark communautaire visé par le plan (phase 4).

Échantillon committé (`combinaisons/scenarios_echantillon/`) : le plus dur
(SSAVOP3, diff 56), un échec smooth (ARGOEP4, diff 26), l'« échec »
aggressive (BIACAP3, diff 5 — cas structurel, pas dimensionnel), verdicts de
benchmark embarqués dans `meta.benchmark`.

## Contenu committé vs régénérable

| Committé (ce dossier) | Régénérable (jamais committé) |
|---|---|
| `campagne/campagne.json` + `stats_<jour>.json` (top 50 postes) | données brutes XIIDM (~3 Go) |
| `combinaisons/*.resume.json` + échantillon | 7 × `out_rte7000_<jour>/` complets (~500 Mo) |
| `2021-01-03/` (journée pilote détaillée) | catalogues `topologies.jsonl` complets |
| `<jour>/scenarios_echantillon/` + `sequences_echantillon/` (7 journées) | les ~6 500 scénarios/séquences par journée |

Chaque journée de campagne (`2021-01-03/` … `2023-02-08/`) porte un
**échantillon de scénarios + séquences observées** : le plus gros bloc de la
journée, un représentant par tag structurel présent (scission/fusion/
ré-aiguillage/sectionnement) et un cycle consignation/remise — privilégiant
les transitions à fort diff (jusqu'à 38 organes, `VLEJUP6` 2023-02-08).
