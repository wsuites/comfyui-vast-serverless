# Handoff — autoscaler and deployment loop

Written 2026-09-23 06:10 UTC, mid-run. Read this before touching `scripts/fleet.py`,
`scripts/fleet_loop.py` or `webapp/server.py`.

## The goal, verbatim

> Quiero que iteres y hagas 5 despliegues sin errores y que al mandar jobs cubra todos
> los casos, maquinas unavail, que stopped pase a running, si no hay nada que cree
> instancia, que todo salga bien, que el provisioning tarde menos de 10 minutos en cold
> start y que las solicitudes tarden los 13 segundos que deben

Six criteria. Status at the time of writing:

| # | Criterion | Status |
|---|---|---|
| 1 | 5 deployment cycles, no errors | **in flight** — run17 started 06:08:55, see below |
| 2 | unavailable machines handled | met live (run15: offer 49200843 refused, loop walked to the next) |
| 3 | stopped -> running | met live (`scripts/paused_probe.py`, 3/3: 17.9s / 33.4s / 46.4s) |
| 4 | create-if-none | met live, every `fresh` cycle |
| 5 | cold start < 10 min | met live, 139-304s per host |
| 6 | requests take 13s | **unresolved, and not a bug** — see "The 13s question" |

## Right now

A live run is in progress. Do not start a second one — three concurrent
`deploy_loop.py` processes once fought over the same endpoint and each destroyed the
others' workers.

```
process : python -u scripts/fleet_loop.py
log     : logs/run17.log        (human)
rows    : logs/fleet_loop.jsonl (one JSON object per cycle, appended)
plan    : fresh -> paused -> warm -> fresh -> warm
started : 06:08:55, cycle 1 renting machine 49903 (RTX 3090, 0.2009/h)
```

Check it with `tail -f logs/run17.log`. It prints a summary table at the end and exits
non-zero if any cycle failed. Expect 20-40 minutes and roughly $0.35-0.45.

If it has finished by the time you read this, the last lines of the log are the answer.
A cycle that failed on hardware rather than on the design retries on another machine up
to 3 times before it counts against the run.

## Architecture: why we own the scheduler

The Vast serverless endpoint couples two unrelated things in one call:
`endpoint._route(cost, req_idx)` is both the demand signal that makes the autoscaler
rent hardware *and* the reservation that hands a slot to a client. When a request dies
between those two — a firewalled host, a worker that never finishes booting, a client
timeout — the reservation stays counted in `reqs_working` while nothing is running. The
slot is wedged, and every later request queues behind a worker that will never report
progress. Measured across five distinct machines over runs 13 and 14, always as
`TimeoutError: Timed out after 3xx s waiting for worker`. It is the router, not the
hosts.

`scripts/fleet.py` separates the two concerns:

- **Demand** is a local counter we own. Renting is an offer search we rank ourselves.
- **Dispatch** goes straight to ComfyUI's HTTP API on the instance, so a dead client
  leaks nothing: there is no reservation to abandon, and a cancel is a real
  `/interrupt` rather than a request the router forgets about.
- A worker is only ever declared ready after we have personally fetched `/object_info`
  from it and seen our own checkpoint in the list. That check would have rejected every
  wedged host at rent time instead of ten minutes later.

`webapp/server.py` was the last caller still posting to the serverless endpoint; commit
`31580dc` moved it onto `fleet.py`. There is no serverless path left in the repo.

## Traps that cost real money to find

Each of these was a live failure first. They are all covered by offline tests now —
that is the whole point of `scripts/test_fleet_loop.py`.

1. **`fleet.up()` returns fleet's own state record, not a Vast instance dict.** Keys are
   `url` / `instance` / `gpu`, not `id` / `public_ipaddr` / `ports`. Calling
   `fleet.comfy_url(inst)` on it yields `None` and the job dies claiming the port was
   never published. It has already resolved and probed the URL; re-deriving it is a
   second, worse guess at the same thing.

2. **Vast reports the pre-start status for up to 46s after an accepted `start`.** Poll
   immediately and `actual_status` still reads `exited`. Reading that as death cost
   run15 cycle 2 three rentals and 933 seconds. Handled by `STATUS_GRACE=90` plus
   `ever_alive` plus requiring `dead_streak >= 2`.

3. **ComfyUI caches on node inputs.** A byte-identical resubmit returns the previous
   images in ~0.72s without running the sampler. Both harnesses used to submit the same
   workflow twice per cycle (warmup, then "the measurement"), so every number reported
   was a cache hit. Fixed with a random seed per job; `seeded()` asserts it. If seeding
   ever regresses, the suite must fail rather than quietly report a fast lie.

4. **`dlperf` does not predict this graph.** It ranked an RTX PRO 4000 at 66 over a 3090
   at 44; measured, the PRO 4000 renders in ~104s and the 3090 in ~82s, at a higher
   price. `fleet.measured_seconds()` now builds `{gpu_name: median latency}` from
   `logs/fleet_loop.jsonl` and ranks on that. A card we have never timed sorts *just
   behind* the best known one, so it gets tried rather than guessed at. Do not
   reintroduce `_dlperf` — the last attempt left three call sites reading a key that no
   longer existed and would have crashed the first rental of run17.

5. **Boot-timeout vetoes were poisoning `.env`.** A machine that failed to boot got
   added to the permanent exclusion list, so healthy hosts (32499 twice, 58334 once)
   were being walked past forever. Only a host that never published the port gets
   vetoed now.

6. **The web page's stepper only moves forward.** `goes_backwards()` in
   `scripts/vast_state.py` silently drops any phase that sorts earlier than the current
   one. `PHASES` lives in `apps/web/src/scripts/progress.ts`:
   `submitting, renting, booting, provisioning, generating, saving, done`. Emitting
   `submitting` for the render after `renting` meant the page showed "renting GPU" for
   the entire job. The render phase is `generating`.

7. **`up(attempts, boot_cap)` takes a boot budget in seconds** (`FLEET_BOOT_CAP`, 600),
   not the job's render timeout. Passing `job.params["timeout"]` there abandons every
   cold start a few seconds in.

## The 13s question — a decision, not a defect

`LATENCY_TARGET = 13.0` was written against a sample + face pass on a 4090, before the
served graph grew an `UltimateSDUpscale` 4x tail. `deploy_loop.py`'s own comment still
says so: *"A warm 1024 render measures ~18 s on a 4090 with the face pass on."* The
target predates the node it is now being measured against.

Measured on an RTX PRO 4000 (`logs/workload_cost.log`), random seed, no cache hit:

| graph | seconds |
|---|---|
| sampler alone | 11.39 |
| + face pass | 20.06 |
| + UltimateSDUpscale 4x (what we serve) | 101.87 |

Per card, full graph: RTX 3090 80.4-85.7s, RTX PRO 4000 99.7-105.4s. The upscale tail
alone is 81.8s — 80% of wall clock. **No card under the 0.220 dph ceiling closes an
80-second gap.** This is not an infrastructure problem and no amount of scheduling fixes
it.

`fleet_loop.workload()` takes a `no_upscale` flag and every cycle now times **both**
graphs, so the run table carries a `base` column next to `lat` and a line stating what
the upscale tail costs. The instrumentation is honest about which of the three graphs a
number describes.

The operator was offered three measured options and declined to choose:

- serve sampler only (11.39s, meets 13s, face and upscale become opt-in per request)
- serve sampler + face (20.06s, raise `LATENCY_TARGET` to ~22)
- keep the full graph and raise `LATENCY_TARGET` to ~110 so the criterion measures the
  real contract

**Nothing has been changed.** The graph is intact and `LATENCY_TARGET` is still 13.0.
Do not quietly edit the constant or drop a node to make the run go green — that is
choosing what the product serves, and it is the operator's call. Ask.

## Cost model, from Vast's own invoices

Today: $4.29 across 43 rentals. Yesterday: $2.13.

| item | today | all time |
|---|---|---|
| GPU | 2.65 (9.02 h) | 4.21 (16.9 h) |
| **download** | **1.47 (538 GB)** | 1.87 (690 GB) |
| storage | 0.15 | 0.32 |
| upload | 0.01 | 0.02 |

A third of the bill is bandwidth, not compute. Every fresh rental re-downloads ~19 GB
of models at $0.003/GB, so **$0.057 of toll before a single pixel renders** — on a
12-minute box that rivals the GPU cost. 22 instances died under 12 minutes and burned
$1.01 (25%); the worst are the ones that paid the full download and then died just
before serving.

Consequence: the `fresh` cycle is expensive by design, and the 5-cycle plan contains two
of them. Iterate against `scripts/test_fleet_loop.py`, which runs the same logic in ~20
seconds for free, and spend money only on the confirmation run.

## Test suites — run these before any live run

```bash
python scripts/test_recovery.py        # 10 cases: the destroy/start/leave-alone table
python scripts/test_fleet_loop.py      # the 5-cycle plan against a fake Vast + fake ComfyUI
python scripts/test_webapp_dispatch.py # the page's job path: render lands pixels, cancel interrupts
```

All three pass as of `31580dc`. The fakes are not mocks of our own calls — they are a
real `ThreadingHTTPServer` speaking enough of ComfyUI's HTTP API, and a `Vast` class
with one machine that refuses and the rest that work, so the code under test runs
unmodified. They model the three failures that previously only reproduced when they cost
money: a started instance whose status still reads `exited`, an offer that refuses the
booking, and a prompt ComfyUI has already seen.

Two fidelity rules the fakes must keep:

- The refusing machine must be the one the ranking actually picks first, or the
  unavailable-machine case never fires and the test passes on nothing.
- The fake GPU charges per *node class* (`NODE_COST`, scaled 200x down from the live
  measurement), not a flat per-node cost. Flat cost makes the two graphs indistinguish-
  able and the `upscale_cost` assertion cannot tell instrumentation from noise.

## Useful commands

```bash
# funding, and whether we can rent at all
vastai show user --raw | python -c "import sys,json;d=json.load(sys.stdin);print(d['balance'],d['balance_threshold'])"

# what is billing right now
vastai show instances --raw

# the live offer ranking, read-only, rents nothing
python scripts/fleet.py status

# the whole bill, by day
vastai show invoices --raw
```

## Files

| path | role |
|---|---|
| `scripts/fleet.py` | rent, verify, dispatch, release. Owns the decision table. |
| `scripts/fleet_loop.py` | the 5-cycle harness and the run summary table |
| `scripts/vast_state.py` | merges Vast's instance and worker views for the page |
| `scripts/paused_probe.py` | isolates the stopped -> running case |
| `scripts/test_*.py` | the three offline suites |
| `webapp/server.py` | local API for the page; dispatches through `fleet.py` |
| `apps/web/src/scripts/progress.ts` | `PHASES` — the stepper's forward-only order |
| `logs/fleet_loop.jsonl` | per-cycle rows; also the input to `measured_seconds()` |
| `docs/levers-and-dead-ends.md` | what is already ruled out, with evidence |

## If you change the ranking

`measured_seconds()` reads `logs/fleet_loop.jsonl`. On a clean checkout that file is
empty, and the correct behaviour is that every card is "untimed" and gets tried. Verify
against the live offer pool read-only (`python scripts/fleet.py status`) before renting,
and grep for leftover `_dlperf` references — the last ranking change left three and they
only fire inside `rent()`, which the offline suite did not exercise until
`test_fleet_loop.py` existed.
