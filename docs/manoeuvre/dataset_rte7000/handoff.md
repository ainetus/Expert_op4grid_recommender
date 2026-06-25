# Passation de session — façade pluggable, IHM, dataset RTE 7000

> Document de reprise pour une nouvelle session sur la branche
> `claude/clever-curie-ly6jcx`. Résume ce qui est **fait** (4 commits), ce qui
> est **planifié**, et la **prochaine tâche** (traiter le dataset Hugging Face
> `OpenSynth/D-GITT-RTE7000-2021`, bloqué jusqu'ici par l'allowlist réseau).

---

## 1. Ce qui est fait sur cette branche

### Commit `a26531a` — Couche pluggable des 3 phases de calcul (`manoeuvre/plugins/`)

Trois contrats substituables (PEP 544, doc `docs/architecture/plugins.md`) :

- **Phase A** `IdentificateurTopologieDetaillee.identifier(poste, topo_cible)` :
  topologie **nodale** cible → topologie **détaillée** cible ;
- **Phase B** `SequenceurManoeuvres.sequencer(poste, cible)` : topologie
  détaillée cible → **séquence de manœuvres** ;
- **Phase C** `PlanificateurNodal.planifier(poste, topo_cible)` : nodale →
  séquence + détaillée, en une passe.

Pièces : type pivot sérialisable `CibleDetaillee` (`{switch_id: ouvert?}`,
`from_graph`/`from_manoeuvres`/`to_graph`/`diff`) ; registre par phase
(`register`/`get`/`disponibles` + entry points
`expert_op4grid_recommender.manoeuvre`, nom `<phase>.<nom>`) ; façade
`PlanificateurTopologie` (compose les phases manquantes : A+B⇒C, C⇒A par
rejeu) ; **vérification indépendante** `verifier_sequence` (partition, écarts
détaillés, règle du sectionneur, alertes R10ter — un plugin mensonger est
démasqué) ; adaptateurs natifs « libtopo » (défaut des 3 phases). Tests :
`tests/manoeuvre/test_plugins_interface.py` (13 cas) ; verrou de surface
publique mis à jour.

### Commit `57ade3e` — IHM migrée sur la façade + sélection d'algos

`scripts/manoeuvre_ihm.py` : `/api/nodale_to_detaillee` → phase A,
`/api/sequence` → phase B, avec l'algorithme **sélectionné par phase**
(`Session.algos`, `GET/POST /api/algos`). Front : sélecteurs « Algo » (volet
nodal = A, panneau Séquence = B), badge `algo <nom>`. Tests :
`tests/manoeuvre/test_ihm_algo_selection.py` (7 cas, dont plugin menteur
démasqué dans la payload).

### Commit `109950d` — Plan de travail publication (`docs/manoeuvre/dataset_rte7000/plan.md`)

Plan en 6 phases (audit → extraction → blocs → tagging → dataset de test /
benchmark → publication), avec logique, risques/parades, jalons. À lire en
premier.

### Commit `7e5a8c7` — Pipeline dataset historique (`manoeuvre/dataset/`)

- `timeline.py` : chronologies d'états d'organes par poste, `topologie_id`
  canonique, états stables (`min_stabilite`), **blocs de transition**
  (départ → cible détaillées + états transitoires + **manœuvres observées**
  ordonnées = séquence réelle approchée), oscillations A→bruit→A repliées,
  réversibilité ;
- `tagging.py` : tags multi-label (`consignation_ouvrage`,
  `remise_en_service`, `scission_noeud`, `fusion_noeuds`,
  `reaiguillage_departs`, `sectionnement_barre`, `reconfiguration_durable`,
  `inclasse`) — régime structurel (fixture du poste) ou repli par nommage RTE ;
- `extraction.py` : blocs → scénarios (format `tests/manoeuvre/scenarios`) +
  séquences observées (format `sequences`) + `stats_blocs` ;
- `structure.py` : `PosteTopologique` depuis fixture JSON, sans pypowsybl ;
- `dgitt.py` : **adaptateur du dataset HF** — lecture parquet/CSV format long
  `(timestamp, voltage_level, switch, état)`, auto-détection de colonnes
  (`_ALIASES`), erreurs explicites listant les colonnes trouvées. **Écrit
  défensivement : le schéma réel n'a pas pu être inspecté** (réseau bloqué) ;
- `scripts/build_rte7000_blocks.py` : CLI bout-en-bout (`--input`,
  `--fixtures`, `--vl`, `--min-stabilite`, `--seuil-durable`) + mode
  `--demo` (chronologies reconstruites depuis les 18 séquences réelles du
  dépôt → **19 blocs / 11 postes**, tags validés : CARRIP3 1 nœud →
  `fusion_noeuds`, PALUNP3 expert → 2 blocs `consignation` puis
  `remise_en_service`).

Tests : `tests/manoeuvre/test_dataset_timeline.py` (13 cas, dont reproduction
exacte d'une séquence réelle de 20 manœuvres). Suite `tests/manoeuvre/`
complète : **918 passed**.

---

## 2. Accès réseau (résolu) et première passe réelle (faite, 2026-06-10)

L'allowlist couvre `huggingface.co` **et** le backend Xet
(`cas-bridge.xethub.hf.co`) : téléchargement opérationnel. Les anciennes
étapes 1-6 de ce document sont **exécutées** sur la journée 2021-01-03
complète — résultats, échantillons et reproduction dans
`docs/manoeuvre/dataset_rte7000/2021-01-03/README.md`, synthèse dans le plan
(« Première passe réelle exécutée »).

À savoir pour reproduire/étendre :

- **Téléchargement** : utiliser `scripts/download_dgitt_subset.py` (md5,
  reprise, retries) — le client officiel `hf download` se bloque dans cet
  environnement ;
- **Structures** : extraire du **dataset lui-même** (`postes_depuis_xiidm`,
  auto par défaut dans `build_rte7000_blocks.py`) — les fixtures du dépôt ont
  d'autres ids d'organes (export normalisé `_OC`) et sont écartées par la
  garde `couverture_structure` ;
- **Mémoire** : la passe journée entière tient en < 4 Go grâce au partage
  structurel des états (`charger_timelines_xiidm`) — ne pas le casser
  (test `test_charger_timelines_xiidm_partage_structurel_des_etats`) ;
- **Benchmark** : `scripts/run_benchmark.py --dataset … --structures-xiidm …`
  (filtres `--tag`, `--min-organes`, `--vl`, `--limit`).

Chiffres de référence (journée 2021-01-03) : 6 233 blocs / 1 861 postes,
36 reconfigurations structurelles ; benchmark sur ces 31 blocs (≥ 1 tag
structurel) : partition 31/31, détaillée exacte 30-31/31, manœuvres vs
opérateur ×1,10 (smooth) / ×1,06 (aggressive).

## 3. Prochaines tâches

1. **Étendre l'échantillon temporel** : traiter quelques journées/semaines
   réparties (saisonnalité, jours ouvrés vs week-end — 2021-01-03 est un
   dimanche) avec les mêmes commandes ; comparer les distributions de tags.
2. **Benchmark élargi** : la passe `--min-organes 2` (2 842 blocs) donne la
   distribution de référence ; analyser les écarts (KERHEP3, JUINEP4 :
   l'opérateur fait mieux/différemment — comprendre les règles en jeu),
   creuser les `inclasse` (2 138 : probablement organes hors cellules de
   départ — affiner la taxonomie).
3. **Versionnage structurel** (décision B du plan) : sur plusieurs jours,
   détecter les changements de structure entre instantanés (hash du graphe
   hors états) et segmenter par version au lieu de supposer la structure du
   1er instantané.
4. **Jeu d'or** : faire valider un échantillon stratifié de tags via l'IHM
   (chargement scénario départ/cible), figer `calibration`/`test`.
5. **CI** : geler quelques blocs réels (échantillon committé) en goldens du
   séquenceur ; marqueur pytest `dataset` pour la suite longue.

## 4. Repères

| Quoi | Où |
|---|---|
| Plan publication 6 phases | `docs/manoeuvre/dataset_rte7000/plan.md` |
| **Guide dataset construit + exécution locale** | `docs/manoeuvre/dataset_rte7000/GUIDE.md` |
| Campagne 7 journées / 3 ans (table, benchmarks) | `docs/manoeuvre/dataset_rte7000/README.md` |
| Orchestrateur multi-journées | `scripts/process_dgitt_days.sh` |
| Résultats première passe réelle (2021-01-03) | `docs/manoeuvre/dataset_rte7000/2021-01-03/` |
| Doc couche pluggable | `docs/architecture/plugins.md` |
| Doc module manoeuvre (conventions, invariants) | `expert_op4grid_recommender/manoeuvre/CLAUDE.md` |
| Pipeline dataset | `expert_op4grid_recommender/manoeuvre/dataset/` |
| Téléchargeur dataset HF | `scripts/download_dgitt_subset.py` |
| CLI pipeline | `scripts/build_rte7000_blocks.py` (`--demo` marche sans données) |
| Benchmark séquenceur vs opérateur | `scripts/run_benchmark.py` |
| Tests | `pytest tests/manoeuvre/` (deps : `pip install pytest networkx pandas pypowsybl flask`) |
| Formats scénario/séquence | `tests/manoeuvre/scenarios/`, `tests/manoeuvre/sequences/` |
| Fixtures structurelles de postes | `tests/manoeuvre/fixtures/` |

Conventions : la branche de travail historique `claude/clever-curie-ly6jcx`
est fusionnée dans `claude/trusting-darwin-9c3ud7` (développement courant) ;
commits descriptifs en français (style des commits existants), ne jamais
muter `poste.graph`, suite `tests/manoeuvre/` verte avant chaque push.
