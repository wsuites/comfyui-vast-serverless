# Running the autoscaler on a VPS

The reaper is the reason for this document. `scripts/fleet.py daemon` is what
releases a rented GPU once it has been idle for `FLEET_IDLE_AFTER`, and on the
workstation it only ever ran while a terminal was open. A closed laptop, a
Windows update, a crashed shell — each of those is a rental that keeps billing
until somebody notices. Moving the four processes to a machine that does not
get turned off, under a supervisor that starts them at boot, is the whole
point; the web UI being reachable from anywhere is a side effect.

Read `docs/monorepo-contract.md` first for the process topology. This document
is the deployment of it.

```
             Cloudflare                  the VPS
 browser ──▶ Access ──▶ tunnel ──▶ cv-web    127.0.0.1:4321  Basic Auth
                                     │
                                     ▼
                                  cv-api    127.0.0.1:8800  holds the API key
                                     │
   cv-fleet ── every 30s ────────────┴──────────────────▶  Vast
```

Nothing listens on a public interface. cloudflared connects outbound, so no
port needs opening, and `webapp/server.py` refuses to bind anything but a
loopback address because it holds `VAST_API_KEY` and has no authentication of
its own.

## Already in place

Done in phase 1, on the VPS, as the user that owns the checkout:

| | |
|---|---|
| checkout | `~/comfy-vast`, cloned from the public GitHub remote |
| python | `~/comfy-vast/.venv/bin/python` — 3.12.3, requirements installed |
| node | 22.23.2 at `/usr/bin/node` |
| pnpm | 10.13.1 at `/usr/bin/pnpm`, matching the `packageManager` pin |
| cloudflared | 2026.7.3 at `/usr/local/bin/cloudflared`, `~/.cloudflared/cert.pem` present |
| `.env` | copied out of band, mode 600, never through git |
| suites | all five offline suites pass there |

Three values in the VPS `.env` deliberately differ from the workstation's:
`PYTHON_BIN` points at the venv, `WEBAPP_AUTH` is `on` rather than `off`, and
`WEBAPP_PASS` is a password generated for this host. `WEB_RETENTION_GB=5` is
appended for the same reason — 11 GB free, and nothing else reclaims
`output/web`.

## Phase 2 — the four units

### 1. Get the code and build the site

```sh
cd ~/comfy-vast
git pull
pnpm install --frozen-lockfile
pnpm web:build              # emits apps/web/dist/server/entry.mjs
```

`.env` must exist before the build, not just before the run:
`apps/web/astro.config.mjs` reads `WEB_HOST`/`WEB_PORT` at build time and bakes
them in as the adapter's defaults. The launcher exports them again at runtime,
so this is belt and braces, but it is one less way to end up listening
somewhere unintended.

Budget: `node_modules` is a few hundred MB and `dist` is small, against 11 GB
free. The gallery cap is the thing to watch, not the build.

### 2. Prove each process by hand first

systemd turns a failure into a restart loop and a log line. It is worth seeing
each process succeed once in a terminal, where the error is on the screen:

```sh
cd ~/comfy-vast
.venv/bin/python webapp/server.py            # autoscaler-vast api -> http://127.0.0.1:8800
deploy/systemd/cv-web.sh                     # astro listening on 127.0.0.1:4321
.venv/bin/python scripts/fleet.py status     # reads the Vast account, rents nothing
```

`fleet.py status` is the one that proves `.env` is right: it is the first thing
here that authenticates against Vast.

### 3. Install the units

```sh
sudo deploy/systemd/install.sh
```

The four unit files live in `deploy/systemd/` as templates — `__ROOT__`,
`__USER__` and `__HOME__` — because this repository is public and those paths
are not. The installer fills them from the checkout's own location and owner,
runs `systemd-analyze verify`, reloads, and *enables* the units without
starting them. `cv-tunnel` is skipped unless `~/.cloudflared/config.yml`
already exists, so that a box with no tunnel yet does not collect a failed
unit on every boot.

These are system units, under `/etc/systemd/system`, not `systemctl --user`.
A user manager only exists once that user has a session, or once somebody
remembers `loginctl enable-linger`; `Linger=no` is the default and this host
has it. The reaper has to come up on a cold boot with nobody logged in.

| unit | what it runs | why it exists |
|---|---|---|
| `cv-api` | `.venv/bin/python webapp/server.py` | dispatch, progress, gallery |
| `cv-web` | `deploy/systemd/cv-web.sh` | the Astro site and the only password prompt |
| `cv-fleet` | `fleet.py daemon --interval 30` | the reaper; the reason for all of this |
| `cv-tunnel` | `cloudflared ... tunnel run` | phase 3; needs a tunnel to exist first |

### 4. Start and smoke-test

```sh
sudo systemctl start cv-api cv-web
systemctl --no-pager status cv-api cv-web

curl -s -o /dev/null -w '%{http_code}\n' localhost:4321/            # 401
curl -s -u "admin:$PASS" localhost:4321/api/status | head -c 200    # JSON through the proxy
curl -s localhost:8800/api/status | head -c 80                      # the same, direct
```

A `401` on the first one is the pass, not the failure: it means
`WEBAPP_AUTH=on` survived the trip through the launcher.

Then the reaper, last and on its own, because it is the unit that can spend
and unspend money:

```sh
.venv/bin/python scripts/fleet.py status     # confirm what it is about to manage
sudo systemctl start cv-fleet
journalctl -u cv-fleet -f                    # fleet daemon up: ceiling ..., idle release 600s
```

### 5. Prove it survives a reboot

The claim being tested is the only one that matters, so test it:

```sh
sudo reboot
# ...
systemctl is-enabled cv-api cv-web cv-fleet      # enabled x3
systemctl is-active  cv-api cv-web cv-fleet      # active x3
journalctl -u cv-fleet -b | head
```

`cv-tunnel` reads `disabled` here until phase 3; that is the install script
doing what it was told, not a failed unit. Give the box a hundred seconds
before the first reconnect and do not loop on it — see trap 7.

An honest version of this test rents something first: `fleet.py up`, reboot,
then watch `journalctl -u cv-fleet -b` reap it once the idle window passes.
That costs one boot of GPU time and is worth it, because a reaper that does
not come back after a reboot fails exactly when nobody is watching.

## Which variable reaches which process

There is one configuration file, `.env` at the repository root, and three
different ways it gets read. Knowing which is which is what makes a change
take effect.

| process | reads `.env` how | after an edit |
|---|---|---|
| `cv-api` | `scripts/config.py`, at import | restart |
| `cv-fleet` | `scripts/config.py`, at import | restart |
| `cv-web` | `deploy/systemd/cv-web.sh`, at every start | restart |
| the build | `astro.config.mjs`, at build time | rebuild, for `WEB_HOST`/`WEB_PORT` |

No `EnvironmentFile=` anywhere. systemd is not a second place to configure
this, and a copy of the values under `/etc` would drift from `.env` silently —
the symptom being a login prompt that rejects the password you just rotated.

`cv-web.sh` exports a deliberate subset: `HOST`, `PORT`, `API_HOST`,
`API_PORT`, `WEBAPP_AUTH`, `WEBAPP_USER`, `WEBAPP_PASS`. Node gets nothing
else, and specifically not `VAST_API_KEY` or the R2 credentials. The loopback
guard in `webapp/server.py` exists so a tunnel cannot reach that key; handing
it to the process that *is* behind the tunnel would undo the guard.

## Traps

### 1. One reaper per Vast account, and not one more

`tick()` destroys every instance carrying `FLEET_LABEL` that its own state file
does not know about, because the only other way for one to exist is a crash
mid-rental. The state file and the lease are per-machine. So a workstation that
rents a worker while the VPS reaper is running has rented an instance the VPS
considers an orphan, and it will be destroyed mid-render — the lease protecting
it is a file on the wrong host.

The moment `cv-fleet` starts on the VPS, the workstation stops running
`cv web up` and becomes a browser pointed at the tunnel. That is phase 4, and
it stops being optional the moment phase 2 is live.

### 2. `HOST`, not `WEB_HOST`

`@astrojs/node` standalone resolves its address as
`process.env.HOST ?? options.host`, and `options.host` is whatever
`astro.config.mjs` read from `WEB_HOST` **at build time**. Export `WEB_HOST` at
runtime and nothing fails and nothing changes: the process binds the address
that was compiled into `dist/`, on the machine that ran the build, from that
machine's `.env`. That is a bind inherited from a file nobody is looking at.

It stays loopback today because the config's own fallback is `127.0.0.1`. It
becomes `0.0.0.0` the moment a build sees `WEB_HOST=0.0.0.0` — and then the
site answers on the VPS's public IP, outside the tunnel and outside Cloudflare
Access, with only Basic Auth left. `cv-web.sh` exports `HOST` explicitly and
defaults it to loopback for that reason; it is why the unit runs a script
instead of carrying a list of `Environment=` lines.

### 3. Stopping `cv-fleet` while a worker is rented

`systemctl stop cv-fleet` stops the only thing that releases GPUs. Nothing else
notices: the API renews a lease while a job runs, but the release itself is the
daemon's idle rule. Before stopping it for any length of time:

```sh
.venv/bin/python scripts/fleet.py status     # "no worker", or:
.venv/bin/python scripts/fleet.py down
```

The units deliberately have no `ExecStop` that releases the worker, because
`systemctl restart` would then destroy a rental in the middle of a render.

### 4. Renaming `FLEET_LABEL` orphans whatever is running

`ours()` matches on the label. Change it while an instance is rented and the
reaper stops recognising that instance as its own — it is not destroyed, it
becomes invisible, and it bills until someone reads the invoice. Rename only
with `fleet.py status` showing no worker.

### 5. `git pull` will not overwrite an untracked file

If anything was ever copied into the checkout by hand at a path that later
becomes tracked, the pull fails with a wall of "untracked working tree files
would be overwritten". `.env` is safe — it is gitignored. Nothing else that
arrived over scp is.

### 6. The gallery is the disk risk, not the build

61 of 72 GB were already used before any of this. `WEB_RETENTION_GB=5` caps
`output/web`, oldest job directories first, and `_prune_output()` runs after
every save. If the box gets tighter, lower that number; `cv-api` reads it from
`.env` at start.

### 7. The SSH lockout renews itself while you poll it

The provider's image ships an iptables firewall, not `fail2ban` — there is no
`fail2ban-client` on this box, so nothing answers `unbanip` and there is no jail
to inspect. Port 22 goes through the `SSHBRUTE` chain:

```
-A INPUT -p tcp --dport 22 --tcp-flags FIN,SYN,RST,ACK SYN -m conntrack --ctstate NEW -j SSHBRUTE
-A SSHBRUTE -m recent --set    --name SSH --rsource
-A SSHBRUTE -m recent --update --seconds 300 --hitcount 10 --name SSH --rsource -j DROP
-A SSHBRUTE -j ACCEPT
```

Ten new connections in five minutes and the source is dropped. The trap is
`--update`: it refreshes the timestamp on every matching packet, so a script
that polls port 22 waiting for the block to lift is the thing keeping it up.
Fourteen polls, ninety seconds apart, held it open for over an hour here.

Two rules follow from that. Connect with `-o IdentitiesOnly=yes -i <key>`, or
the agent offers every key it holds and one login burns several attempts. And
when you are locked out, wait a full five minutes without sending anything —
polling is not free, it is the ban.

The state is a kernel list, not a config file, and root can edit it directly:

```sh
grep 201.245.250.253 /proc/net/xt_recent/SSH    # is this address held
echo -201.245.250.253 > /proc/net/xt_recent/SSH # release it
```

A reboot clears both lists. The same mechanism guards ICMP under
`/proc/net/xt_recent/ICMP`, which is why a ping flood stops answering.

## Operating it

```sh
journalctl -u cv-fleet -f                  # what the reaper is doing
journalctl -u cv-api -n 200 --no-pager     # dispatch, retention, errors
sudo systemctl restart cv-api cv-web       # after an .env edit
cd ~/comfy-vast && git pull && pnpm install --frozen-lockfile && pnpm web:build \
  && sudo systemctl restart cv-api cv-web cv-fleet    # after a deploy
```

Rotating the password: edit `WEBAPP_PASS` in `.env` and
`sudo systemctl restart cv-web`. There is no subcommand for it on this path —
`cv web up --tunnel` generates one interactively, and that command is not what
runs here. No other file holds a copy, which is the property that makes the
edit sufficient.

Backing out entirely:

```sh
sudo systemctl disable --now cv-tunnel cv-web cv-api cv-fleet
.venv/bin/python scripts/fleet.py status   # make sure nothing is left rented
sudo rm /etc/systemd/system/cv-*.service && sudo systemctl daemon-reload
```

## Phase 3 — what the tunnel adds

`cv-tunnel` is installed by phase 2 but cannot start until a named tunnel
exists. Named, not the quick tunnel the CLI uses on the workstation: the
hostname has to survive a restart, and a quick tunnel's does not.

```sh
cloudflared tunnel create autoscaler-vast          # on the VPS; needs ~/.cloudflared/cert.pem
cloudflared tunnel route dns autoscaler-vast autoscaler-vast.<your-domain>
```

`~/.cloudflared/config.yml`, mode 600:

```yaml
tunnel: <tunnel-uuid>
credentials-file: /home/<user>/.cloudflared/<tunnel-uuid>.json
ingress:
  - hostname: autoscaler-vast.<your-domain>
    service: http://127.0.0.1:4321
  - service: http_status:404
```

`cloudflared tunnel ingress validate` reads that file and answers `OK`
before anything is started; use it, the unit restarts on a bad one.

The two commands do not need the same credential, and on this account they
did not have it. Creating a tunnel is an account-level operation; routing a
hostname edits a zone. The `cert.pem` sitting on the VPS was old enough to
predate the zone and `route dns` failed with `code: 10000, reason:
Authentication error` while `create` had just succeeded with the same file.
The fix is not to re-login on the server: run `route dns` from whichever
machine holds the cert that already wrote the zone's other records — the
tunnel is visible account-wide, so a workstation can route a hostname to a
tunnel that only exists on the VPS. The CNAME is the whole handoff.

Then `sudo systemctl enable --now cv-tunnel`. Look for four `Registered
tunnel connection` lines in `journalctl -u cv-tunnel -b`; fewer than four
means it is reaching the edge but not redundantly. Smoke test from outside
the box, not from it: `401` without credentials on `/`, `200` with them, and
`/api/status` returning the same JSON the loopback port serves. A `404`
instead of a `401` is the catch-all answering, which means the hostname in
`config.yml` and the hostname you asked for are not the same string.

The hostname is a placeholder
here for the same reason as in `docs/handoff.md`: this repository is public,
and a hostname that fronts a private service is the one thing in it worth not
publishing. The live value belongs in `.env`.

Cloudflare Access goes in front of that hostname and the Basic Auth stays
behind it. Two prompts is the intent, not an oversight: Access is the one that
can be revoked per device and per email, and Basic Auth is the one that still
holds if the hostname is ever reached another way.
