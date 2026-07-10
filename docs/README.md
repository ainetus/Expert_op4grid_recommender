# Documentation — ExpertOp4Grid Recommender

Index of all project documentation. For the project overview, installation and
usage see the [root README](../README.md); for the full release history see
[CHANGELOG.md](../CHANGELOG.md).

```
docs/
├── architecture/      system overview, pipeline & extension points
├── recommender/       corrective-action design specs
├── manoeuvre/         detailed-topology maneuver module (+ IHM, assets, dataset)
│   └── dataset_rte7000/   historical RTE-7000 topology/maneuver benchmark
├── reviews/           point-in-time code reviews (audits of a given version)
├── archive/           finished/transient notes kept for the record
└── release-notes/     per-version notes (canonical history: CHANGELOG.md)
```

## Architecture & algorithms (`architecture/`)
- [`architecture/overview.md`](architecture/overview.md) — **start here**:
  architecture & overall-understanding overview — what the tool does, the
  end-to-end two-step pipeline, the subsystems, the extension points, and a
  development chronology (mermaid diagrams + tables).
- [`architecture/simulation-pipeline.md`](architecture/simulation-pipeline.md) —
  the pypowsybl simulation pipeline: AC/DC & fast/slow load-flow modes, voltage
  initialisation, variant lifecycle (kept-variant registry + LRU backstop), the
  unified backend simulation seam + `BaselineContext` (R4), thermal-limit
  hypotheses, retry branches.
- [`architecture/configuration.md`](architecture/configuration.md) — the pydantic
  `Settings` single source of truth (R3): computed derived paths, the
  `get_settings()` / `override_settings()` / `reset_settings()` accessors, the
  module-attribute compatibility layer, and the fork-free test override.
- [`architecture/recommender_models.md`](architecture/recommender_models.md) —
  the pluggable `RecommenderModel` contract: DTO fields, capability flags,
  reusable pipeline phases, integration point, and a minimal new-model example.
- [`architecture/plugins.md`](architecture/plugins.md) — the maneuver module's
  pluggable calculation phases (identification / sequencing / end-to-end
  planning): contracts, registry, entry points, independent verification.

## Recommender action designs (`recommender/`)
Design specs for the corrective action types the recommender proposes.
- [`recommender/antenna_overflow_graph.md`](recommender/antenna_overflow_graph.md) —
  overflow-graph handling for islanded-pocket ("antenna") contingencies.
- [`recommender/load_shedding.md`](recommender/load_shedding.md) — load shedding
  (downstream consumption reduction) as a corrective action type.
- [`recommender/renewable_curtailment.md`](recommender/renewable_curtailment.md) —
  renewable curtailment, the upstream counterpart to load shedding.
- [`recommender/superposition_module.md`](recommender/superposition_module.md) —
  the superposition theorem and Generalized Superposition Theorem (§10) used to
  estimate combined action impact via virtual flows / delta-theta.

## Maneuver module (`manoeuvre/`)
Detailed-topology (node-breaker) maneuver planning — its rules, web IHM, and the
historical dataset it is validated against. (Its pluggable calculation phases are
documented under [`architecture/plugins.md`](architecture/plugins.md).)
- [`manoeuvre/module.md`](manoeuvre/module.md) — module reference: objectives,
  pipeline, node-breaker topology analysis.
- [`manoeuvre/regles.md`](manoeuvre/regles.md) — business rules (R1–R25) with
  rule-to-code traceability.
- [`manoeuvre/art_de_la_manoeuvre.md`](manoeuvre/art_de_la_manoeuvre.md) —
  critical coverage analysis against the RTE operating references (CCRT C3-3,
  ACT 104, CCO expert method), the consolidated/enriched rule set R20–R28
  (consequence classification, authorization matrix, busbar test, bay state
  machine, timing, expected SCADA checks, electrical validation, multi-substation
  orchestration, automata) and the module/algo/verifier restructuring plan.
- [`manoeuvre/ihm.md`](manoeuvre/ihm.md) — the interactive web IHM (topology
  editor + sequence animation, day-exploration map, ⚙ config modal, isolated-device
  declaration, scenario author/date metadata) and the hosted HuggingFace Space.
  Launch recipes are in the [README Getting Started](../README.md#getting-started).
- [`manoeuvre/postes_n_jeux_de_barres.md`](manoeuvre/postes_n_jeux_de_barres.md) —
  extension from 2-busbar to N-busbar substations.
- [`manoeuvre/optimisations.md`](manoeuvre/optimisations.md) — performance/quality
  review and measured results.
- [`manoeuvre/implementation_plan.md`](manoeuvre/implementation_plan.md) — port
  roadmap from C++ libTOPO to Python.

Developer conventions for the module:
[`../expert_op4grid_recommender/manoeuvre/CLAUDE.md`](../expert_op4grid_recommender/manoeuvre/CLAUDE.md).

### RTE-7000 dataset (`manoeuvre/dataset_rte7000/`)
Historical topology/maneuver benchmark built from the public D-GITT RTE-7000
dataset, used to validate the maneuver module.
- [`manoeuvre/dataset_rte7000/README.md`](manoeuvre/dataset_rte7000/README.md) —
  campaign table (7 days over 2021–2023) with statistics.
- [`manoeuvre/dataset_rte7000/GUIDE.md`](manoeuvre/dataset_rte7000/GUIDE.md) —
  dataset contents, schema, and local reproduction.
- [`manoeuvre/dataset_rte7000/plan.md`](manoeuvre/dataset_rte7000/plan.md) — the
  6-phase build & publication plan.
- [`manoeuvre/dataset_rte7000/handoff.md`](manoeuvre/dataset_rte7000/handoff.md) —
  session handoff: status, results, and next tasks.

## Release notes (`release-notes/`)
Per-version notes (v0.2.2 → v0.2.6, v0.2.9). The canonical, continuously updated
history is [CHANGELOG.md](../CHANGELOG.md). Latest:
[`release-notes/v0.2.9.md`](release-notes/v0.2.9.md) — discovery restructured
around data (R5/A5), `ActionType` + C7 rule-bypass fix, container-aware
reassessment. Built on `0.2.8` (deep revisions **R3** config single-source +
**R4** unified simulation seam / `BaselineContext`; see the CHANGELOG).

## Reviews (`reviews/`)
Point-in-time audits of the codebase at a given version — findings age with the
code they describe.
- [`reviews/2026-07_full_code_review.md`](reviews/2026-07_full_code_review.md) —
  comprehensive review at v0.2.5: architecture, interfaces, performance,
  documentation, maintainability; prioritized findings, deep revisions and
  quick wins.

## Archive (`archive/`)
Finished / transient documents kept for the record — not living reference.
- [`archive/migration_plan`](archive/MIGRATION_PLAN.md) — grid2op → pure-pypowsybl
  migration strategy.
- [`archive/code-quality-analysis.md`](archive/code-quality-analysis.md) —
  static-analysis snapshot and cleanup log.
- [`archive/SETUP_SUMMARY.md`](archive/SETUP_SUMMARY.md) — config-override session
  summary (canonical test-config docs now in
  [`../tests/README_CONFIG.md`](../tests/README_CONFIG.md)).

## Deployment & backend docs (co-located with code)
These live next to the code they document:
- [`../deploy/huggingface/README.md`](../deploy/huggingface/README.md) ·
  [`../deploy/huggingface/SETUP.md`](../deploy/huggingface/SETUP.md) —
  HuggingFace Space deployment of the maneuver IHM.
- [`../expert_op4grid_recommender/pypowsybl_backend/README.md`](../expert_op4grid_recommender/pypowsybl_backend/README.md) —
  pure-pypowsybl backend quick-start & API.
- [`../expert_op4grid_recommender/pypowsybl_backend/methodologie_developpement_backend_pypowsybl.md`](../expert_op4grid_recommender/pypowsybl_backend/methodologie_developpement_backend_pypowsybl.md) —
  backend development & validation methodology.
- [`../tests/README_CONFIG.md`](../tests/README_CONFIG.md) — test configuration
  and the `conftest.py` override mechanism (incl. skipping visualization &
  troubleshooting).
