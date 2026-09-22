import { existsSync } from 'node:fs'
import { spawnTool } from '../lib/spawn.ts'

import { Command } from 'commander'

import { ApiClient } from '../lib/api.ts'
import { loadConfig, validateConfig, type Config } from '../lib/config.ts'
import { EXIT } from '../lib/errors.ts'
import { c, json, out } from '../lib/output.ts'
import { ASTRO_ENTRY, ROOT, WEBAPP_SERVER } from '../lib/paths.ts'
import { common, ctx, type CommonOptions } from '../lib/program.ts'
import { VastClient } from '../vast/client.ts'
import { listWorkergroups } from '../vast/endpoints.ts'
import { listInstances } from '../vast/instances.ts'
import { parseQuery, type Query } from '../vast/query.ts'
import { probeEndpoint } from './endpoint.ts'

/**
 * Vast refuses to hold an endpoint group below this balance, and it stops the
 * ones you already have: the endpoint goes to `stopped`, the workers are
 * destroyed, and every request queues against nothing. The error only shows up
 * if you try to write to the endpoint, so check it before anything else.
 */
const ENDPOINT_GROUP_MIN_CREDIT = 5

/** `{ field: { op: value } }` flattened to `field op value` lines, for diffing. */
function queryLines(q: Query): Set<string> {
  const lines = new Set<string>()
  for (const [field, ops] of Object.entries(q)) {
    for (const [op, value] of Object.entries(ops)) {
      lines.add(`${field} ${op} ${Array.isArray(value) ? `[${value.join(',')}]` : String(value)}`)
    }
  }
  return lines
}

interface Check {
  name: string
  status: 'ok' | 'warn' | 'fail' | 'skip'
  detail: string
  fix?: string | undefined
}

/** Run a binary just to see whether it exists and what version it claims. */
function probeBin(bin: string, args: string[]): Promise<{ ok: boolean; line: string }> {
  return new Promise((resolve) => {
    let child
    try {
      child = spawnTool(bin, args, { stdio: ['ignore', 'pipe', 'pipe'] })
    } catch {
      resolve({ ok: false, line: 'could not be executed' })
      return
    }
    let buf = ''
    const grab = (chunk: Buffer) => {
      buf += chunk.toString('utf8')
    }
    child.stdout?.on('data', grab)
    child.stderr?.on('data', grab)
    child.on('error', (e: NodeJS.ErrnoException) => {
      resolve({ ok: false, line: e.code === 'ENOENT' ? 'not found on PATH' : e.message })
    })
    child.on('close', (code) => {
      const line = buf.split(/\r?\n/).find((l) => l.trim()) ?? ''
      resolve({ ok: code === 0, line: line.trim() || `exited ${code}` })
    })
    setTimeout(() => {
      child?.kill()
      resolve({ ok: false, line: 'timed out' })
    }, 10_000).unref?.()
  })
}

async function checks(cfg: Config, quick: boolean): Promise<Check[]> {
  const list: Check[] = []
  const add = (c1: Check) => list.push(c1)

  // --- static configuration -------------------------------------------------
  const findings = validateConfig(cfg)
  const configErrors = findings.filter((f) => f.level === 'error')
  add({
    name: 'config',
    status: configErrors.length ? 'fail' : findings.some((f) => f.level === 'warn') ? 'warn' : 'ok',
    detail: configErrors.length
      ? configErrors.map((f) => `${f.key}: ${f.message}`).join('; ')
      : `${findings.filter((f) => f.level === 'warn').length} warning(s)`,
    fix: configErrors.length ? 'cv config' : undefined,
  })

  // --- toolchain ------------------------------------------------------------
  const py = await probeBin(cfg.pythonBin, ['--version'])
  add({
    name: 'python',
    status: py.ok ? 'ok' : 'fail',
    detail: `${cfg.pythonBin}: ${py.line}`,
    fix: py.ok ? undefined : 'Set PYTHON_BIN in .env to the interpreter that has the project requirements.',
  })

  add({
    name: 'scripts',
    status: existsSync(WEBAPP_SERVER) ? 'ok' : 'fail',
    detail: existsSync(WEBAPP_SERVER) ? WEBAPP_SERVER : `${WEBAPP_SERVER} is missing`,
  })

  const cf = await probeBin(cfg.cloudflaredBin, ['--version'])
  add({
    name: 'cloudflared',
    status: cf.ok ? 'ok' : 'warn',
    detail: `${cfg.cloudflaredBin}: ${cf.line}`,
    fix: cf.ok ? undefined : 'Only needed for `cv web up --tunnel`. Install it or set CLOUDFLARED_BIN.',
  })

  // --- the web build --------------------------------------------------------
  const built = existsSync(ASTRO_ENTRY)
  add({
    name: 'web build',
    status: built ? 'ok' : 'warn',
    detail: built ? ASTRO_ENTRY : 'apps/web/dist/server/entry.mjs does not exist',
    fix: built ? undefined : 'pnpm web:build',
  })

  // --- local API ------------------------------------------------------------
  const api = new ApiClient(cfg)
  const apiUp = await api.healthy()
  add({
    name: 'api',
    status: apiUp ? 'ok' : 'warn',
    detail: apiUp ? `answering on ${api.base}` : `nothing on ${api.base}`,
    fix: apiUp ? undefined : 'cv web up  (starts it), or run webapp/server.py yourself.',
  })

  if (quick) return list

  // --- Vast, live -----------------------------------------------------------
  if (!cfg.apiKey) {
    add({ name: 'vast auth', status: 'fail', detail: 'no API key to test with', fix: 'vastai set api-key <KEY>' })
    return list
  }

  const vast = new VastClient(cfg.apiKey, { timeoutMs: 20_000 })
  try {
    const me = await vast.get<Record<string, unknown>>('/users/current/')
    const credit = typeof me['credit'] === 'number' ? (me['credit'] as number) : null
    add({
      name: 'vast auth',
      status: 'ok',
      detail: `${String(me['username'] ?? me['email'] ?? 'authenticated')}, balance ${
        credit === null ? 'unknown' : `$${credit.toFixed(2)}`
      }`,
    })

    // Judged separately from auth: the key can be perfectly good and the
    // account still too poor to run anything.
    const broke = credit !== null && credit < ENDPOINT_GROUP_MIN_CREDIT
    add({
      name: 'credit',
      status: credit === null ? 'skip' : broke ? 'fail' : credit < ENDPOINT_GROUP_MIN_CREDIT * 2 ? 'warn' : 'ok',
      detail:
        credit === null
          ? 'the account reported no balance'
          : broke
            ? `$${credit.toFixed(2)} — below the $${ENDPOINT_GROUP_MIN_CREDIT.toFixed(2)} Vast requires for an endpoint group`
            : `$${credit.toFixed(2)}`,
      fix: broke
        ? 'Top up. Below this Vast stops the endpoint and destroys the workers; renting by hand still works.'
        : undefined,
    })
  } catch (e) {
    add({
      name: 'vast auth',
      status: 'fail',
      detail: e instanceof Error ? e.message : String(e),
      fix: 'Check VAST_API_KEY; `cv config` shows where the CLI read it from.',
    })
    return list
  }

  try {
    const instances = await listInstances(vast)
    const running = instances.filter((i) => i.actual_status === 'running').length
    const dph = instances.reduce((n, i) => n + (i.dph_total ?? 0), 0)
    add({
      name: 'instances',
      status: 'ok',
      detail: `${instances.length} rented, ${running} running, $${dph.toFixed(3)}/h`,
    })
  } catch (e) {
    add({ name: 'instances', status: 'warn', detail: e instanceof Error ? e.message : String(e) })
  }

  const probe = await probeEndpoint()
  if (probe) {
    const ep = probe.endpoint
    // A stopped endpoint accepts requests and scales nothing: they queue until
    // the client's timeout. Nothing else in the snapshot says so.
    const state = String(ep.endpoint_state ?? 'unknown')
    const active = state === 'active'
    add({
      name: 'endpoint',
      status: active ? 'ok' : 'fail',
      detail: `${String(ep.endpoint_name ?? cfg.endpointName)} (id ${String(ep.id)}) is ${state}, ${probe.workers} worker(s), ` +
        `max ${String(ep.max_workers ?? '?')}, cold ${String(ep.cold_workers ?? '?')}`,
      fix: active
        ? undefined
        : `vastai update endpoint ${String(ep.id)} --endpoint_state active   (needs $${ENDPOINT_GROUP_MIN_CREDIT.toFixed(2)} of credit)`,
    })

    // --- .env against the workergroup actually doing the searching ----------
    // These two drift silently: .env is what you read, the workergroup is what
    // rents. A .env that matches no offer looks exactly like a dead market.
    try {
      const groups = await listWorkergroups(vast)
      const mine = groups.filter((g) => Number(g.endpoint_id) === Number(ep.id))
      if (mine.length === 0) {
        add({
          name: 'workergroup',
          status: 'fail',
          detail: `endpoint ${String(ep.id)} has no workergroup, so nothing can be rented for it`,
          fix: 'cv endpoint create --workergroup',
        })
      } else {
        for (const g of mine) {
          const liveText = String(g.search_params ?? '')
          const live = liveText ? parseQuery(liveText) : ((g.search_query as Query | null) ?? {})
          const mineQ = parseQuery(cfg.searchParams ?? '')
          const liveL = queryLines(live)
          const envL = queryLines(mineQ)
          const onlyLive = [...liveL].filter((l) => !envL.has(l))
          const onlyEnv = [...envL].filter((l) => !liveL.has(l))
          const drifted = onlyLive.length > 0 || onlyEnv.length > 0
          add({
            name: 'workergroup',
            status: drifted ? 'warn' : 'ok',
            detail: drifted
              ? `${String(g.id)}: search filters differ from .env — live only: ${onlyLive.join(', ') || 'none'}; ` +
                `.env only: ${onlyEnv.join(', ') || 'none'}`
              : `${String(g.id)}: search filters match VAST_SEARCH_PARAMS`,
            fix: drifted
              ? 'Decide which is right, then `cv endpoint update --from-env` to push .env, or copy the live values back into .env.'
              : undefined,
          })
        }
      }
    } catch (e) {
      add({ name: 'workergroup', status: 'warn', detail: e instanceof Error ? e.message : String(e) })
    }
  } else {
    add({
      name: 'endpoint',
      status: 'warn',
      detail: `no endpoint named ${JSON.stringify(cfg.endpointName)} is reachable`,
      fix: 'cv endpoint ls   — then set VAST_ENDPOINT_ID / VAST_ENDPOINT_NAME in .env.',
    })
  }

  return list
}

export function doctorCommand(): Command {
  return common(new Command('doctor'))
    .description('check the toolchain, the configuration and the live endpoint')
    .option('--quick', 'skip everything that touches the network')
    .action(async (o: CommonOptions & { quick?: boolean }) => {
      const { json: asJson } = ctx(o)
      const cfg = loadConfig()
      const result = await checks(cfg, o.quick === true)

      if (asJson) {
        json({ root: ROOT, checks: result })
      } else {
        const marks = { ok: c.green(' ok '), warn: c.yellow('warn'), fail: c.red('fail'), skip: c.dim('skip') }
        out('')
        for (const ck of result) {
          out(`  ${marks[ck.status]}  ${c.bold(ck.name.padEnd(12))} ${ck.detail}`)
          if (ck.fix) out(`        ${c.dim('fix:')} ${c.dim(ck.fix)}`)
        }
        out('')
        const failed = result.filter((r) => r.status === 'fail')
        out(
          failed.length
            ? c.red(`${failed.length} check(s) failed: ${failed.map((f) => f.name).join(', ')}`)
            : c.green('everything the CLI needs is in place'),
        )
      }

      if (result.some((r) => r.status === 'fail')) process.exitCode = EXIT.CONFIG
    })
}
