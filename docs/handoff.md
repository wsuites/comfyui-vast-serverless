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
| 1 | 5 deployment cycles, no errors | **met live** — run17, 5/5 |
| 2 | unavailable machines handled | met live (run15: offer 49200843 refused, loop walked to the next) |
| 3 | stopped -> running | met live (run17 cycle 2, 79.3s; `paused_probe.py` 3/3) |
| 4 | create-if-none | met live, run17 cycles 1 and 4 |
| 5 | cold start < 10 min | met live, 5/5 under 600s, worst 209.4s |
| 6 | requests take 13s | **unresolved, and not a bug** — see "The 13s question" |

## run17 — the clean live run

2026-09-23 06:08-06:33. Cost $0.134. Nothing left billing.

```
#  cycle   result     boot  warmup      lat     base  gpu
1  fresh   pass     209.4s   99.4s   78.26s   18.78s  RTX 3090
2  paused  pass      79.3s  102.0s   80.01s   20.09s  RTX 3090
3  warm    pass       3.6s   74.6s   79.82s    20.4s  RTX 3090
4  fresh   pass     176.6s  101.0s    80.1s    19.0s  RTX 3090
5  warm    pass       3.6s   77.4s   84.49s   19.57s  RTX 3090
5/5 cycles passed, 5/5 booted under 600s, 0/5 under the 13s latency target.
```

`lat` is the full served graph, `base` the same render without the upscale tail. On a
3090 the tail costs 61s of an 80s render.

To repeat it: `python -u scripts/fleet_loop.py`. Do not start a second one concurrently
— three `deploy_loop.py` processes once fought over the same endpoint and each destroyed
the others' workers. A cycle that fails on hardware rather than on the design retries on
another machine up to 3 times before it counts against the run.

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

8. **A running `webapp/server.py` serves the code it was started with.** The process had
   been up since 22/09 12:34 — before `31580dc` — so it was still posting to mizuki's
   serverless endpoint, whose autoscaler never rents. The page sat on "No machine yet"
   forever while `fleet.py status` from the same checkout ranked offers in seconds. An
   hour went into reading code that was already correct on disk. Check the process start
   time against the commit date *first*:
   `Get-CimInstance Win32_Process -Filter "Name like '%python%'" | select ProcessId,CreationDate,CommandLine`.

9. **Nothing releases a worker when you stop generating.** The idle reaper
   (`FLEET_IDLE_AFTER=600`) lives in `fleet.tick()`, and `tick()` only runs inside
   `fleet.py daemon` — which is not running. A worker rented from the page bills until
   something stops it by hand. Separately, `_run_job` never called `fleet.touch()`,
   because web renders do not go through `fleet.render()`: the day a daemon *is* started
   it would have read a stale `last_job` and reaped a machine mid-render. Both touches
   are in now, one either side of the submit.

10. **`fleet.http()` threw away the body of an HTTP 400.** ComfyUI puts the entire reason
    a prompt was rejected in that body, under `node_errors` — node id, field, and the
    value it did not like. Without it the page showed `HTTPError: HTTP Error 400: Bad
    Request` and nothing else, which is indistinguishable from a wedged worker. The body
    is folded into `exc.msg` now, and it stays an `HTTPError` so `comfy_ready`'s
    `URLError` catch still behaves.

    The 400 that prompted this was a *missing model*: a combo input is declared as
    `[[options...], {...}]`, and a checkpoint that never downloaded is simply absent from
    the list, so the graph is rejected as invalid rather than reported as incomplete.
    `fleet.missing_inputs()` now walks the workflow against `/object_info` before
    submitting, and `webapp/server.py` waits up to `MODEL_WAIT = 300` for a file that is
    still landing — the deferred set is ~5.6 GB and arrives in about a minute on a
    healthy origin. Past that the caller gets the name of the file instead of another
    minute of billing. An unreadable `/object_info` reports nothing missing: this check
    may delay a job, never fail one.

11. **A closed stdout killed the deferred downloads, silently.** The deferred half of
    provisioning runs in a subshell backgrounded past the ready signal. It inherited the
    foreground script's stdout, which closes when that script exits — so the first
    `print()` after the exit raised, `ThreadPoolExecutor.map` propagated the exception,
    and the whole remaining batch died. The instance reported ready, ComfyUI came up in
    32s, and `models/vae/` was one file short with no `.part` and no error anywhere.

    Worth stating plainly because the symptom invites the wrong diagnosis: 3 of the 4
    deferred files (4.18 GB + 1.19 GB + 2.51 GB) *had* landed, in ~54s. The origin was
    never the problem, and `additional_disk_usage ≈ 0` is not evidence that a download
    is dead. The subshell owns its output now (`exec >>"$MODEL_LOG" 2>&1`), reporting
    goes through a `say()` that cannot raise, and the consumer loop is `as_completed`,
    so one dead future can no longer cancel its siblings.

12. **"No machine yet — the autoscaler is looking for an offer" is what success looks
    like too.** `_watch_infra` overwrites the page's phase and detail from
    `vast_state.describe()` every `POLL_SECONDS`, and `vast_state.py:281` emits that line
    whenever there is no instance yet — which is also exactly the state of a healthy
    `fleet.up()` mid offer-search. The text names a component that no longer exists in
    the repo. It is not a diagnosis; read `logs/webapp.log` instead.

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

# shell on a specific instance (xcl is the key; id_rsa is not)
vastai show instances --raw | python -c "import sys,json;[print(i['id'],i['ssh_host'],i['ssh_port']) for i in json.load(sys.stdin)]"
ssh -i ~/.ssh/xcl -p <port> root@<ssh_host>

# shell through the named tunnel instead (any instance, whichever Cloudflare picks)
cloudflared access tcp --hostname mizuki-ssh.whitesu.dev --url localhost:2223
ssh -i ~/.ssh/xcl -p 2223 root@localhost

# are the tunnels actually up
cloudflared tunnel info mizuki-ssh
cloudflared tunnel info mizuki-worker

# push a provisioning script edit + the onstart to the template
python scripts/renew_provisioning.py
```

`PYTHONIOENCODING=utf-8` on every `vastai` call from Windows. The account env vars
(`CF_*_TOKEN`, `S3_*`) are set once with `vastai create env-var NAME VALUE` and are
injected into every rental. They are masked on read, so they cannot be recovered from
Vast: the S3 ones live in `.env` (gitignored) and the Cloudflare ones are re-derivable
with `cloudflared tunnel token mizuki-ssh` / `mizuki-worker`.

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
| `scripts/serverless_provision.sh` | what a rented box runs; uploaded to R2, not read from here |
| `scripts/renew_provisioning.py` | uploads that script and rewrites template 549464 + the onstart |
| `docs/levers-and-dead-ends.md` | what is already ruled out, with evidence |

## If you change the ranking

`measured_seconds()` reads `logs/fleet_loop.jsonl`. On a clean checkout that file is
empty, and the correct behaviour is that every card is "untimed" and gets tried. Verify
against the live offer pool read-only (`python scripts/fleet.py status`) before renting,
and grep for leftover `_dlperf` references — the last ranking change left three and they
only fire inside `rent()`, which the offline suite did not exercise until
`test_fleet_loop.py` existed.

## Reaching a running instance

**SSH works, and it has all along.** The `Permission denied (publickey)` that sent a whole
debugging session down the `vastai execute` route was the wrong key, not a locked box:

```bash
ssh -i ~/.ssh/xcl -p <port> root@ssh<N>.vast.ai
```

`~/.ssh/id_rsa` is denied and `vast_opencode_test` times out. `xcl` is the one. The port
and host come from `vastai show instances --raw` (`ssh_port`, `ssh_host`). This goes
through Vast's own reverse proxy, so it works on hosts that firewall every inbound port,
and it addresses **one specific instance** — which the tunnels below do not.

`vastai execute` is the fallback, and a poor one: it only runs on **stopped** instances
("Execute command only avail on stopped instances"), its whitelist is roughly `ls`, `du`,
`cat`, `rm` — `curl`, `wget` and `bash -c` all come back `{"error":"invalid_args"}` — and
multiple paths in one `ls` is a 400. The CLI's own `execute` is broken outright
(`AttributeError: 'str' object has no attribute 'get'`); the raw API works:
`PUT /api/v0/instances/command/{id}/`, then poll the `result_url` it returns.

### The two tunnels

Both are cloudflared, both dial **out** to Cloudflare, so neither cares what the host
firewalls. Both are named tunnels on `whitesu.dev`, and in both cases the split is the
same: the **hostname** is public and lives in the template env, the **token** is a Vast
*account* env var, masked in `vastai show env-vars` and never written into the template —
the template is world-readable via `vastai show template`, and the provisioning `.sh` sits
on a public R2 URL.

| | worker | admin ssh |
|---|---|---|
| hostname | `mizuki-py.whitesu.dev` | `mizuki-ssh.whitesu.dev` |
| token env var | `CF_WORKER_TOKEN` | `CF_SSH_TOKEN` |
| local origin | `https://localhost:3000` | `ssh://localhost:22` |
| purpose | the pyworker's *reported* url | shell on a firewalled host |

The worker one exists because the pyworker does not serve on its own bind — it **reports**
a url built from env vars (`metrics.py: get_url() -> f"https://{PUBLIC_IPADDR}:{VAST_TCP_PORT_3000}"`)
and the autoscaler hands that to the client verbatim. On a host that firewalls its
forwarded ports the reported url times out from outside and every request dies in the
queue. The onstart brings the tunnel up and then exports `PUBLIC_IPADDR` and
`VAST_TCP_PORT_3000=443` so the pyworker reports the tunnel instead. It is a deliberate
lie to the autoscaler, which is why it is opt-in, and it must be ordered **before**
`start_server.sh`: `get_url` is `@cache`d and read once at import.

To use the admin one:

```bash
cloudflared access tcp --hostname mizuki-ssh.whitesu.dev --url localhost:2223
ssh -i ~/.ssh/xcl -p 2223 root@localhost
```

Every instance runs the same token, so the hostname resolves to whichever connection
Cloudflare picks. With one box up that is the box; with several it is a coin toss, and the
Vast ssh proxy is the way to address a named one.

### Quick tunnels rate-limit, and the retry loop is what keeps them down

The admin tunnel used to be a quick tunnel (`--url` with no token, random
`*.trycloudflare.com` per boot). Those are handed out per source IP and quota'd. Retrying
every 15s asks for ~240 an hour from one address, and Cloudflare answers *"quick tunnel
provisioning failed with status 429: error code: 1015"* — so the retry loop was the cause
of the outage it was retrying. Measured on 52224586: six hours, zero tunnels, and one
Discord DOWN line per attempt burying every other message in the channel.

The loop now backs off 15s → 15min and reports DOWN once per outage rather than per
attempt, and the failure reason is grepped out of the tunnel's own log — without it a 1015
reads exactly like a network blip and gets retried forever instead of waited out. With
`CF_SSH_TOKEN` set none of this applies (a named tunnel has no such quota), but the brake
stays for a dead origin, and the quick tunnel stays as the fallback path so a machine
rented without the env var still has a door.

### `pkill -f` over SSH kills the shell that ran it

```bash
ssh host "pkill -f 'tunnel --no-autoupdate run'; setsid nohup ... &"   # never reaches line 2
```

`-f` matches full command lines, and the remote `bash -c` *contains* the pattern as an
argument, so it matches itself. It dies before the next line. Break the self-match with a
character class, which changes the literal but not the regex:

```bash
pkill -f -- '--url ssh[:]//localhost:22'
```

## Windows-side footguns

- **`vastai` output crashes on `charmap`.** Any command that might print non-ASCII —
  `vastai logs`, an `ls` of a directory with Cyrillic names — needs
  `PYTHONIOENCODING=utf-8` in the environment, or it dies in the codec rather than in the
  API call.
- **git-bash `/tmp` is not Windows-python `/tmp`.** A file written by one is invisible to
  the other. Use the session scratchpad, or a path inside the repo, for anything that
  crosses between them. `$TMPDIR` inside a git-bash heredoc can also resolve empty, which
  turns `> "$TMPDIR/x.sh"` into `> /x.sh` and a `Permission denied` that looks like it
  came from the remote host.

## Dead ends

- **Installing a model through ComfyUI-Manager's API.** V3.41 answers
  `Invalid model install request is detected` with HTTP 400 for anything outside its own
  whitelist, so it is not a general write path onto a running worker. Use SSH.
- **Trusting `additional_disk_usage` to tell you a download is alive.** See trap 11.

## The provisioning script does not re-run when you edit it

Vast's image keeps one hash per provisioning phase in `/.provisioner_state/`:
`apt.hash`, `pip.hash`, `git.hash`, `downloads.hash`, `provisioning_script.hash`, and the
rest. On boot, a phase whose hash still matches is skipped, and `/.provisioning_complete`
is created anyway.

There are two gates in series, and clearing either one alone buys nothing. With the marker
deleted and the hashes intact, the boot re-enters provisioning and leaves it again in zero
seconds, every phase skipped. With the hashes deleted and the marker intact, the boot never
enters provisioning at all — measured twice on 52224586, 15:45 and 15:55 UTC, both silent.
Delete both, in the same stopped window, or expect another clean-looking boot with an empty
`models/vae/`.

The hash for the script is over its **URL**, not its body. That is the whole trap: the
upload goes to a fixed R2 key, so it used to be a fixed URL, and an instance that had
provisioned once kept the version it first downloaded forever, through any number of
restarts. Deleting `/.provisioning_complete` and `/provisioning.sh` changed nothing —
measured on 52224586 on 2026-09-23, where a restart reported "provisioned" in zero seconds
and `models/vae/` stayed empty.

`renew_provisioning.py` now appends `?v=<sha256(body)[:12]>` to the URL it writes into the
template, so editing the script changes the URL and the script phase stops matching on its
own. The current one is `...serverless_provision.sh?v=109e35e1f97c`. Two caveats: an
instance only sees the new URL if it is re-rented or its template env is re-read, and the
other phases (`downloads.hash` in particular) key on their own inputs, not on this one. For
a machine that is already up and must take a new script *now*, the manual route below is
still the only thing that is certain.

To force an existing instance onto a new script:

```bash
# instance must be stopped - `execute` is refused on running ones
vastai execute <iid> "rm -f /.provisioning_complete"
vastai execute <iid> "rm -f /.provisioner_state/provisioning_script.hash"
vastai execute <iid> "rm -f /.provisioner_state/downloads.hash"   # also re-checks the model list
vastai start instance <iid>
```

New rentals are unaffected: they have no state directory and always run the current
script. This only bites when you are debugging a fix against a machine that is already up.
