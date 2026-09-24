# Monorepo contract

This repository is a pnpm workspace. Two JavaScript packages sit beside the
Python engine that already existed; the Python is the engine, the JavaScript is
the interface.

```
comfy-vast/
├─ package.json          root scripts, orchestration only
├─ pnpm-workspace.yaml   apps/*  packages/*
├─ apps/web/             @comfy-vast/web  — Astro front end + Basic Auth + proxy
├─ packages/cli/         @comfy-vast/cli  — `cv`, the unified toolchain
├─ webapp/server.py      FastAPI, API only, loopback only
├─ scripts/              Python engine (workflows, Vast state, rendering)
├─ deploy/systemd/       unit templates; the VPS deployment, see docs/vps-deploy.md
└─ workflows/ docs/ output/
```

## Boundaries

| Path | Owner |
|---|---|
| `apps/web/**`, `webapp/server.py` | web package |
| `packages/cli/**` | cli package |
| `scripts/**` | neither; treat as a stable library |

`scripts/*.py` must keep working when invoked directly, exactly as today. Both
packages may read them and shell out to them; neither rewrites them.

## Process topology

```
 browser ──▶ Astro (WEB_PORT, Basic Auth) ──▶ FastAPI (API_PORT, loopback)
                     ▲                                   │
                     │                                   ▼
               cloudflared                        scripts/*.py ──▶ Vast
```

FastAPI never binds anything but `127.0.0.1`. Authentication lives in Astro, so
exposing `API_PORT` through a tunnel would bypass it.

## Interface: web → cli

`apps/web` builds with `@astrojs/node` (v11) in **`mode: 'standalone'`**. The
build emits `apps/web/dist/server/entry.mjs`, whose exports are fixed by the
adapter:

```js
export { handler, options, startServer }
```

The CLI embeds the site by importing that module **with
`process.env.ASTRO_NODE_AUTOSTART = 'disabled'` already set**, then calling
`startServer()`. Standalone mode is deliberate: the adapter then serves
`dist/client/**` itself, so the CLI never reimplements static file handling.

```js
process.env.ASTRO_NODE_AUTOSTART = 'disabled'
process.env.HOST = webHost
process.env.PORT = String(webPort)
const { startServer } = await import(entryUrl)
const { server } = startServer()   // later: await server.stop()
```

`startServer()` returns `{ server: { host, port, stop(), closed(), server }, done }`.
The CLI runs the site in its own process; it does not spawn a second Node.

If `apps/web/dist/server/entry.mjs` is missing, the CLI must fail with a clear
instruction to run `pnpm web:build`, never with a raw module-not-found error.

Host and port reach the adapter through the `HOST` and `PORT` environment
variables, which is how `@astrojs/node` standalone reads them.

## Interface: both → FastAPI

Astro proxies these prefixes to `http://API_HOST:API_PORT` untouched:

- `/api/**` — JSON, plus `GET /api/jobs/:id/stream`, which is Server-Sent
  Events and must be streamed through with buffering disabled.
- `/results/**` — rendered image files.

The CLI talks to the same FastAPI routes when it needs job state, and to the
Vast REST API directly for infrastructure.

## Configuration

Every setting is read from `.env` at the repository root, the same file
`scripts/config.py` already reads. Keys are documented in `.env.example`. No
package introduces a second configuration file.
