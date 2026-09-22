# Agent surface plan

How an LLM agent drives `cv` on the operator's behalf, and what is missing before it can.

The target is a conversational operator: *"ten shots of this scene", "same ones with the other
character", "send each one as it lands", "A/B this new model against anima", "get a machine with
5 GB more disk"*. Each of those should be one or two `cv` calls with a machine-readable reply,
not a workflow the agent has to assemble.

## Where we are today

The CLI is already agent-shaped in two important ways: every command takes `--json`, and every
configuration value comes from a single `.env` at the repository root. What it lacks is
discoverability, per-image delivery, and any way to name a model that is not already compiled into
the code.

Measured against the requests above:

| Request | Today | Gap |
|---|---|---|
| N images of a scene | `cv gen "..." -b 10` | one request, all-or-nothing, ~5 min of silence |
| the same, different subject | — | no way to derive from a previous job |
| stream each image as it lands | `cv gen --discord` | only to Discord |
| deliver to an arbitrary webhook | `--discord-webhook <url>` | URL is swappable, the payload shape is not |
| override cfg, steps, size, seed | full flag set on `gen` and `job submit` | none |
| A/B two models | `cv gen compare --arms` | arms are a closed enum of five |
| A/B a model we just downloaded | — | needs `ARMS` plus a loader subgraph in Python |
| two subjects, one seed | two calls, same `--seed` | no fan-out primitive, no grouping |
| more disk on the worker | `VAST_DISK_SPACE` in `.env` | template-level, needs reprovisioning |
| video | — | out of scope, see below |

## The plan

### 1. `cv agent manifest --json`

One call that returns the whole surface: commands, parameters, live enums (registered models,
background-removal models, arms), rough cost and latency per operation, and an explicit list of
what the toolchain cannot do. An agent reads this once instead of shelling out to thirty
`--help` screens, and it cannot hallucinate an arm or a flag that does not exist because the
enums are generated from the same tables the commands validate against.

Pairs with `docs/agent-guide.md`: conventions, the cost model, and the failure modes worth
retrying.

### 2. Streaming events

`--json --stream` emits NDJSON on stdout: one event per phase change and one per image as it
lands, terminated by a final summary object. This is the primitive behind *"send them as they
come"* — everything else in this document that streams is built on it.

Event shape follows the job snapshot already defined in `webapp/server.py`, so the SSE stream,
`cv job watch` and this share one vocabulary.

### 3. Generic webhooks

`--webhook <url>` with `--webhook-format json|multipart|discord`, retries with backoff, and the
seed and parameters in the payload. Discord becomes one format among several rather than the only
delivery path; `DISCORD_WEBHOOK` keeps working as the default for `--discord`.

### 4. Derive from a previous job

`cv gen --from <job-id> --set prompt="..." --set cfg=6`

Reads the stored parameters, applies the overrides, submits. *"Now the same ones but X"* stops
requiring the agent to carry state between turns, and the provenance in `run_info.json` records
what it was derived from.

### 5. Fan-out with a shared seed

`cv gen --variant a="..." --variant b="..." --shared-seed`

Submits a group of jobs that differ in exactly one field and share a seed, so the comparison
measures the change rather than noise. Returns a group id; `cv job ls --group <id>` and the
gallery understand it. This is the same idea `gen compare` already applies to model arms,
generalised to any single-field difference.

### 6. Models as data

Move `ARMS` out of `scripts/ab_modelo.py` and into a registry of `models/*.json`:

```json
{
  "name": "anima",
  "arch": "sdxl-unet",
  "unet": "...",
  "clip": "...",
  "vae": "...",
  "defaults": { "steps": 25, "cfg": 5.0, "scheduler": "karras" }
}
```

With `cv model add`, `cv model ls` and `cv model pull <hf-repo>` (fetch onto the worker), the
`--arms` enum becomes whatever is registered, and *"A/B this new model against anima"* works for
a model that did not exist when the code was written.

Crossing architectures still needs a loader subgraph per `arch` — the graph shape genuinely
differs — but adding one becomes a workflow template plus a JSON entry rather than an edit to the
804-line builder. The registry is what `cv agent manifest` reports as the live arm enum.

### 7. Endpoint and worker hardware

`cv endpoint template set --disk 31` and `--disk` on `cv instance rent`, so changing worker
storage is a call rather than an `.env` edit followed by a reprovision. `cv endpoint scale`
already covers the autoscaler knobs.

### 8. Cost guardrails

An agent in a loop spends real money. `--dry-run` returns the estimate without renting anything;
`--max-cost` refuses above a ceiling; submissions take an idempotency key so a retried call after a
timeout does not render twice.

## Phasing

1. **Manifest, streaming, webhooks.** Unlocks most of the conversational requests and touches no
   Python.
2. **`--from` and `--variant`.** Removes agent-side state; needs job parameters readable by id.
3. **Model registry.** The largest change: moves a table out of Python and gives the arms enum a
   runtime source.
4. **Hardware and guardrails.**

## Out of scope

Video. It is a different pipeline (WAN, AnimateDiff or similar), not a flag on the existing graph,
and minutes of footage on a single rented GPU is chunked rendering measured in hours. If it
happens it is its own project with its own workflows and its own cost model.

## Open questions

- Does `--stream` replace `--json` or compose with it? Composing keeps one flag meaning one thing.
- Group ids: new concept, or reuse the `pair_id` the compare path already writes?
- Where does the model registry live so both the Python engine and the TypeScript CLI read it
  without one owning the other — alongside `workflows/`, read by both, is the obvious answer.
