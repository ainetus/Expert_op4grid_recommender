# Pluggable Recommendation Models

The analysis pipeline does not hardcode the expert system anymore: it
consumes any class implementing `RecommenderModel`. This document is
the **library-side contract reference** — it spells out the strategy
pattern, the input/output DTOs, and the reusable pipeline phases every
model can rely on.

For the **app-side integration** (registry, built-in random examples,
backend service patches, frontend wiring, filter chain) see
`marota/co-study4grid` — `docs/architecture/recommender_models.md`.

---

## Why pluggable

Five design principles drive the interface:

1. **Strategy pattern** — same input / output contract for every model.
   The pipeline doesn't need to know whether it's running the expert
   system, a random baseline, an ML policy, or anything else.
2. **Capability flags** — `requires_overflow_graph` lets the caller
   skip the expensive step-1 graph build when the model does not need
   it. The pipeline mirrors this: graph is computed iff the model
   needs it OR the operator opted in.
3. **Parameter introspection** — `params_spec()` enumerates parameters
   the model actually consumes; clients (UI, REST API) hide everything
   else, so the operator never tunes a knob the model will ignore.
4. **Homogeneous output** — `{action_id -> action_object}`. Reassessment
   into rich action cards (rho-before / rho-after / simulated
   observation / non-convergence / combined-pair scores) happens
   downstream and works for any model that emits raw actions.
5. **Pass what was already computed** — step-1 outputs (overload
   detection, pre-existing exclusion, island guard, baseline rho) are
   propagated to the model through the input DTO instead of being
   recomputed downstream.

---

## The contract: `RecommenderModel`

```python
from expert_op4grid_recommender.models.base import (
    RecommenderModel,
    RecommenderInputs,
    RecommenderOutput,
    ParamSpec,
)

class MyModel(RecommenderModel):
    name: ClassVar[str] = "my_model"            # registry key
    label: ClassVar[str] = "My Model"           # human label for the UI
    requires_overflow_graph: ClassVar[bool] = False  # capability flag

    @classmethod
    def params_spec(cls) -> list[ParamSpec]:
        return [
            ParamSpec("n_prioritized_actions", "N Actions", "int",
                     default=5, min=1, max=50),
            # add any other operator-tunable knob the model uses
        ]

    def recommend(self, inputs: RecommenderInputs, params: dict) -> RecommenderOutput:
        # ...sample / score / build the candidate actions...
        return RecommenderOutput(
            prioritized_actions={action_id: action_obj, ...},
            action_scores={},  # free-form; may be empty
        )
```

### Required class attributes

| Attribute                 | Type             | Purpose                                        |
|---------------------------|------------------|------------------------------------------------|
| `name`                    | `str` (non-empty)| Identifier used by the registry and UI.        |
| `label`                   | `str`            | Human-readable label shown in the dropdown.    |
| `requires_overflow_graph` | `bool`           | Whether the model needs the step-2 graph step. |

### Required methods

- `params_spec() -> list[ParamSpec]` — classmethod. The list of
  parameters the model consumes. Clients render only these in the UI.
- `recommend(inputs, params) -> RecommenderOutput` — instance method.
  Produces a flat `{action_id: action_object}` mapping.

Instantiating a subclass that omits either method raises `TypeError`
(ABC enforcement). The `name`/`label` attributes must be set on the
subclass; an empty `name` is rejected at registration time downstream.

---

## `RecommenderInputs` — what the pipeline gathers

Dataclass with three groups of fields:

### 1. Always populated

| Field                       | Description                                              |
|-----------------------------|----------------------------------------------------------|
| `obs`                       | Initial network observation (N state).                   |
| `obs_defaut`                | Post-fault observation (N-K state).                      |
| `lines_defaut: list[str]`   | Names of the lines forming the contingency.              |
| `lines_overloaded_names`    | Names of constrained lines under the N-K state.          |
| `lines_overloaded_ids`      | Indices into `obs_defaut.name_line`, paired with `_names`. |
| `dict_action: dict`         | Action dictionary (id → entry; entries carry switches /  |
|                             | content / VoltageLevelId / description).                 |
| `env`                       | Simulation environment (grid2op or pypowsybl backend).   |
| `classifier`                | `ActionClassifier` instance.                             |
| `timestep: int`             | Current timestep.                                        |

### 2. Network handles (paired with observations)

| Field             | Paired with    | Notes                                                |
|-------------------|----------------|------------------------------------------------------|
| `network`         | `obs` (N)      | pypowsybl `Network`. On pypowsybl backend = `env.network_manager.network`; on grid2op = `env.backend._grid.network`. May be `None` on legacy paths. |
| `network_defaut`  | `obs_defaut`   | Same instance with the contingency variant active. Sourced from `obs_defaut._network_manager.network` (pypowsybl) or `env`-level introspection. May be `None`. |

Use the network handle for **topology / device-level queries** (lines,
generators, voltage levels, switches, transformers); use the
observation for **state-dependent values** (flows, voltages, rho).

### 3. Pre-computed step-1 outcome

These are exposed so models don't recompute. Available on every call.

| Field                          | Description                                                                |
|--------------------------------|----------------------------------------------------------------------------|
| `lines_overloaded_rho`         | `list[float]` aligned with `lines_overloaded_names` / `_ids`. Equivalent to `obs_defaut.rho[lines_overloaded_ids]` pre-extracted to a plain Python list. |
| `lines_overloaded_ids_kept`    | Subset of `lines_overloaded_ids` retained after the island-prevention guard. |
| `pre_existing_rho`             | `{line_idx: rho_N}` for lines already overloaded in the N state. Empty dict = no pre-existing overload (semantically meaningful; not `None`). |

### 4. Optional — only when the overflow graph step ran

These are populated whenever the chosen model declares
`requires_overflow_graph=True` AND the operator did not disable the
step-2 graph (or when the operator opted in for a non-requiring model).

| Field                                | Description                                                                 |
|--------------------------------------|-----------------------------------------------------------------------------|
| `overflow_graph`                     | alphaDeesp overflow graph.                                                  |
| `distribution_graph`                 | Structured overload distribution graph (path / hub / loop info).            |
| `overflow_sim`                       | Associated alphaDeesp simulator.                                            |
| `hubs: list[str]`                    | Hub substation names (node-splitting candidates).                           |
| `node_name_mapping`                  | Internal index → substation-name mapping.                                   |
| `non_connected_reconnectable_lines`  | Currently-disconnected lines eligible for reconnection.                     |
| `lines_non_reconnectable`            | Currently-disconnected lines NOT eligible for reconnection.                 |
| `lines_we_care_about`                | Operator-supplied monitoring list (defaults to all lines).                  |
| `maintenance_to_reco_at_t`           | Lines scheduled for reconnection from maintenance at this timestep.         |
| `act_reco_maintenance`               | Action object that performs the scheduled maintenance reconnections.        |
| `use_dc: bool`                       | True when step-2 fell back to DC load flow.                                 |
| `filtered_candidate_actions`         | **Action IDs retained by the expert rule filter.** `None` when the filter didn't run; `[]` when it ran with no result. Forwarded so non-expert models that declare `requires_overflow_graph=True` (e.g. `RandomOverflowRecommender`) can sample inside the same reduced action space. |

### Backend metadata

| Field             | Notes                                          |
|-------------------|------------------------------------------------|
| `is_pypowsybl`    | True when the env runs the pypowsybl backend.  |
| `fast_mode`       | True for pypowsybl no-voltage-control mode.    |

### Private escape hatch — `_context`

The expert recommender reaches back into the analysis context for the
few internal helpers (simulation hooks, rho-reduction check, baseline
simulation) that would otherwise pollute the DTO. **External models
must not rely on this field.** Treat it as private; the contract is
the public fields above.

---

## `RecommenderOutput`

```python
@dataclass
class RecommenderOutput:
    prioritized_actions: dict      # {action_id: action_object}
    action_scores: dict = field(default_factory=dict)
```

- `prioritized_actions` — the raw actions selected by the model. NOT
  enriched with simulated rho / observation. The reassessment step
  in `utils/reassessment.py` runs each one through the simulator,
  computes rho-before / rho-after / `max_rho` / `non_convergence` /
  the post-action `observation`, and wraps the result in the
  legacy `SimulatedAction` shape the action cards consume.
- `action_scores` — free-form, may be empty. When populated, follows
  the legacy expert schema:
  ```python
  {
      "line_reconnection":   {"scores": {aid: score, ...}, "params": {...}, "non_convergence": {aid: reason, ...}},
      "line_disconnection":  {...},
      "open_coupling":       {...},
      "close_coupling":      {...},
      "pst_tap":             {...},
      "load_shedding":       {...},
      "renewable_curtailment": {...},
  }
  ```

---

## `ParamSpec` — declarative UI / API contract

```python
@dataclass
class ParamSpec:
    name: str                         # snake_case key the model reads from params dict
    label: str                        # human label shown in the UI
    kind: Literal["int", "float", "bool"]
    default: Any
    min: float | None = None
    max: float | None = None
    description: str | None = None
    group: str | None = None
```

The UI uses this to render only the fields the model actually consumes
and to grey out the rest. The REST surface mirrors this via the
`GET /api/models` endpoint (see app-side docs).

---

## Built-in: `ExpertRecommender`

Wraps the existing rule-based discovery pipeline behind the contract.

- `name = "expert"`, `label = "Expert system"`,
  `requires_overflow_graph = True`
- `params_spec()` exposes every legacy knob (`n_prioritized_actions`,
  `min_line_reconnections`, `min_close_coupling`, `min_open_coupling`,
  `min_line_disconnections`, `min_pst`, `min_load_shedding`,
  `min_renewable_curtailment_actions`, `ignore_reconnections`).
- `recommend()` delegates to `_run_expert_discovery(context, n_action_max)`
  which (a) calls the rule-validation filter, (b) pre-processes the
  alphaDeesp graph, (c) instantiates `ActionDiscoverer` and runs
  `discover_and_prioritize`. The escape hatch via `inputs._context` is
  used for the simulation helpers.

External models must NEVER use `_context`. They consume only the public
fields documented above.

---

## Reusable pipeline phases

### `_run_expert_action_filter(context)`

Located in `expert_op4grid_recommender/main.py`. Standalone helper
that runs **path analysis + `ActionRuleValidator.categorize_actions`**
and writes `context["filtered_candidate_actions"]`.

- **Idempotent.** Returns immediately when the field is already
  populated.
- **Requires** `g_distribution_graph` and `hubs` in context (= run
  `run_analysis_step2_graph` first).
- Called automatically by `run_analysis_step2_discovery` when the
  chosen model declares `requires_overflow_graph=True`. The Expert
  model also invokes it internally (idempotent, free no-op).

Note: this filter removes broadly invalid actions (wrong shape /
already-open lines / etc.). It does NOT narrow to overflow-relevant
actions — that targeting is done by `ActionDiscoverer`'s per-type
mixins for the expert path. Sampling models that need the path-
relevant subset apply an additional filter on top (see the app-side
docs for the three-layer filter chain).

### Reassessment (`utils/reassessment.py`)

Run automatically after `recommender.recommend()`. Three pure
functions any model gets for free:

- `reassess_prioritized_actions(prioritized_actions, context)` —
  simulates each returned action, computes rho-before / rho-after /
  `max_rho` / `is_rho_reduction` / non-convergence and packages the
  result in the legacy action-card schema.
- `propagate_non_convergence_to_scores(detailed_actions, action_scores)`
  — copies per-action `non_convergence` reasons into the per-category
  score table so the UI can flag them in context.
- `compute_combined_pairs(detailed_actions, context)` — runs the
  superposition theorem on every detailed-action pair. Returns `{}`
  on any failure (decorative metadata; must never break the main
  flow).

### `build_recommender_inputs(context)`

Also in `utils/reassessment.py`. Projects the analysis context into a
`RecommenderInputs` DTO. Sources the two network handles, the
pre-computed step-1 outcome, the optional overflow-graph artefacts,
and `filtered_candidate_actions`. Defensive copies for list/dict
fields so the model can mutate the DTO without leaking back into the
shared context.

---

## Integration point: `run_analysis_step2_discovery`

The single entry point used by every caller (CLI, Co-Study4Grid backend,
tests):

```python
def run_analysis_step2_discovery(
    context,
    recommender=None,            # default: ExpertRecommender()
    params=None,                 # default: {"n_prioritized_actions": config.N_PRIORITIZED_ACTIONS}
) -> dict:
```

Flow:

1. Resolve `recommender` (default ExpertRecommender) and `params`.
2. If `recommender.requires_overflow_graph and context["g_distribution_graph"]`
   is present, call `_run_expert_action_filter(context)` (idempotent).
3. `inputs = build_recommender_inputs(context)`.
4. `output = recommender.recommend(inputs, params)`.
5. `detailed_actions, pre_existing_info = reassess_prioritized_actions(...)`.
6. `action_scores = propagate_non_convergence_to_scores(...)`.
7. `combined_actions = compute_combined_pairs(...)`.
8. Return the legacy result dict shape: `lines_overloaded_names`,
   `prioritized_actions` (= detailed), `action_scores`,
   `pre_existing_overloads`, `combined_actions`.

This means: any caller that previously consumed
`run_analysis_step2_discovery(context)` continues to work unchanged.
New callers can swap recommenders by passing the second argument.

---

## Minimal example: writing a new model

```python
import random
from expert_op4grid_recommender.models.base import (
    RecommenderModel, RecommenderInputs, RecommenderOutput, ParamSpec,
)

class FirstNRecommender(RecommenderModel):
    """Trivial baseline: pick the first N entries from the dict."""
    name = "first_n"
    label = "First N (debug)"
    requires_overflow_graph = False

    @classmethod
    def params_spec(cls):
        return [ParamSpec("n_prioritized_actions", "N", "int", default=5, min=1, max=50)]

    def recommend(self, inputs: RecommenderInputs, params: dict) -> RecommenderOutput:
        n = int(params.get("n_prioritized_actions", 5))
        env = inputs.env
        out = {}
        for action_id, entry in list((inputs.dict_action or {}).items())[:n]:
            content = (entry or {}).get("content")
            if content is None:
                continue
            try:
                out[action_id] = env.action_space(content)
            except Exception:
                continue
        return RecommenderOutput(prioritized_actions=out)
```

Registration and UI exposure happen on the app side (the registry
lives in `marota/co-study4grid`). The library only defines the
contract.

---

## Testing

Library-level tests live in `tests/`:

- `test_models_base.py` — ABC contract, DTO defaults, ParamSpec.
- `test_models_expert.py` — ExpertRecommender metadata + `_context`
  requirement.
- `test_reassessment.py` — reassessment helpers, network extraction
  (env + obs), overloaded-rho pre-extraction, combined pairs.
- `test_filtered_candidate_actions_propagation.py` — regression cover
  for the silent None vs [] bug.

Run with `pytest tests/` from the repo root. All tests are mock-based
and do NOT require a live pypowsybl / grid2op environment.
