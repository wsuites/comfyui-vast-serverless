import { Command } from 'commander'

import { confirmDestructive } from '../lib/confirm.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { c, err, fmtDuration, json, out, printTable } from '../lib/output.ts'
import { assertOwnsFleet, common, ctx, destructive, float, int, type CommonOptions } from '../lib/program.ts'
import { VastClient } from '../vast/client.ts'
import {
  createEndpoint,
  createWorkergroup,
  deleteEndpoint,
  deleteWorkergroup,
  endpointLogs,
  endpointWorkers,
  findEndpoint,
  listEndpoints,
  listWorkergroups,
  updateEndpoint,
  updateWorkergroup,
  updateWorkers,
  workergroupLogs,
} from '../vast/endpoints.ts'
import type { AutoscalerKnobs, VastEndpoint, VastWorkergroup } from '../vast/types.ts'
import { loadConfig, type Config } from '../lib/config.ts'

function client(cfg: Config): VastClient {
  if (!cfg.apiKey) {
    throw new CliError('No Vast API key.', {
      code: EXIT.CONFIG,
      hint: 'Set VAST_API_KEY in .env or run: vastai set api-key <KEY>',
    })
  }
  return new VastClient(cfg.apiKey)
}

/**
 * A client for the calls that put hardware up. Scaling down, deleting and
 * every read stay open from anywhere: those end spending, they never start it.
 */
function spender(cfg: Config, what: string): VastClient {
  assertOwnsFleet(cfg, what)
  return client(cfg)
}

/** The endpoint the command is about: --id, --name, or .env. */
async function resolve(api: VastClient, cfg: Config, o: { id?: number; name?: string }): Promise<VastEndpoint> {
  return findEndpoint(api, {
    id: o.id ?? (cfg.endpointId || null),
    name: o.name ?? (cfg.endpointName || null),
  })
}

/** The workergroup attached to an endpoint, or the one named in .env. */
async function resolveGroup(api: VastClient, cfg: Config, endpointId: number): Promise<VastWorkergroup> {
  const groups = await listWorkergroups(api)
  const byEndpoint = groups.filter((g) => Number(g.endpoint_id) === Number(endpointId))
  if (byEndpoint.length === 1 && byEndpoint[0]) return byEndpoint[0]

  if (cfg.workergroupId) {
    const hit = groups.find((g) => Number(g.id) === Number(cfg.workergroupId))
    if (hit) return hit
  }
  if (byEndpoint.length > 1) {
    throw new CliError(`Endpoint ${endpointId} has ${byEndpoint.length} workergroups; say which with --workergroup.`, {
      code: EXIT.USAGE,
      hint: `ids: ${byEndpoint.map((g) => g.id).join(', ')}`,
    })
  }
  throw new CliError(`No workergroup attached to endpoint ${endpointId}.`, {
    code: EXIT.NOT_FOUND,
    hint: 'Set VAST_WORKERGROUP_ID in .env, or create one with `cv endpoint create --workergroup`.',
  })
}

/** The autoscaler knobs, shared by create/update/scale. */
function knobOptions(cmd: Command): Command {
  return cmd
    .option('--min-load <perf>', 'floor load in perf units/s', (v) => float(v, '--min-load'))
    .option('--min-cold-load <perf>', 'floor load that still allows cold workers', (v) => float(v, '--min-cold-load'))
    .option('--target-util <ratio>', 'target capacity utilisation, max 1.0', (v) => float(v, '--target-util'))
    .option('--cold-mult <x>', 'cold capacity as a multiple of hot', (v) => float(v, '--cold-mult'))
    .option('--cold-workers <n>', 'cold workers kept when there is no load', (v) => int(v, '--cold-workers'))
    .option('--max-workers <n>', 'ceiling on workers for this endpoint', (v) => int(v, '--max-workers'))
    .option('--max-queue-time <s>', 'queue time above which it scales out', (v) => float(v, '--max-queue-time'))
    .option('--target-queue-time <s>', 'queue time it aims for', (v) => float(v, '--target-queue-time'))
    .option('--inactivity-timeout <s>', 'idle seconds before workers go away', (v) => float(v, '--inactivity-timeout'))
}

function collectKnobs(o: Record<string, unknown>): AutoscalerKnobs {
  const k: AutoscalerKnobs = {}
  const map: [keyof AutoscalerKnobs, string][] = [
    ['min_load', 'minLoad'],
    ['min_cold_load', 'minColdLoad'],
    ['target_util', 'targetUtil'],
    ['cold_mult', 'coldMult'],
    ['cold_workers', 'coldWorkers'],
    ['max_workers', 'maxWorkers'],
    ['max_queue_time', 'maxQueueTime'],
    ['target_queue_time', 'targetQueueTime'],
    ['inactivity_timeout', 'inactivityTimeout'],
  ]
  for (const [apiKey, optKey] of map) {
    const v = o[optKey]
    if (typeof v === 'number') k[apiKey] = v
  }
  return k
}

function num(v: unknown, digits = 2): string {
  return v === null || v === undefined || !Number.isFinite(Number(v)) ? '-' : Number(v).toFixed(digits)
}

export function endpointCommand(): Command {
  const cmd = new Command('endpoint').alias('ep').description('serverless endpoints, workergroups and their workers')

  const idOpts = (c2: Command) =>
    c2
      .option('--id <n>', 'endpoint id; defaults to VAST_ENDPOINT_ID', (v) => int(v, '--id'))
      .option('--name <name>', 'endpoint name; defaults to VAST_ENDPOINT_NAME')

  common(cmd.command('ls'))
    .description('every endpoint and workergroup on the account')
    .action(async (o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const api = client(cfg)
      const [eps, groups] = await Promise.all([listEndpoints(api), listWorkergroups(api)])
      if (asJson) return json({ endpoints: eps, workergroups: groups })

      out(c.bold('endpoints'))
      printTable(
        ['ID', 'NAME', 'STATE', 'MIN LOAD', 'TARGET UTIL', 'COLD MULT', 'COLD', 'MAX'],
        eps.map((e) => [
          e.id,
          e.endpoint_name ?? '-',
          e.endpoint_state === 'active' ? c.green('active') : c.yellow(String(e.endpoint_state ?? '?')),
          num(e.min_load),
          num(e.target_util),
          num(e.cold_mult),
          e.cold_workers ?? '-',
          e.max_workers ?? '-',
        ]),
      )
      out('')
      out(c.bold('workergroups'))
      printTable(
        ['ID', 'ENDPOINT', 'TEMPLATE', 'GPU RAM', 'MIN LOAD', 'TARGET UTIL', 'COLD MULT', 'COLD', 'TEST'],
        groups.map((g) => [
          g.id,
          `${g.endpoint_name ?? '-'} (${g.endpoint_id ?? '-'})`,
          g.template_id ?? '-',
          g.gpu_ram ? `${g.gpu_ram}G` : '-',
          num(g.min_load),
          num(g.target_util),
          num(g.cold_mult),
          g.cold_workers ?? '-',
          g.test_workers ?? '-',
        ]),
      )
    })

  idOpts(common(cmd.command('show')))
    .description('full configuration of one endpoint and its workergroup')
    .action(async (o: CommonOptions & { id?: number; name?: string }) => {
      const { cfg, json: asJson } = ctx(o)
      const api = client(cfg)
      const ep = await resolve(api, cfg, o)

      let group: VastWorkergroup | null = null
      try {
        group = await resolveGroup(api, cfg, Number(ep.id))
      } catch {
        // an endpoint with no workergroup is a valid, if useless, state
      }

      if (asJson) return json({ endpoint: ep, workergroup: group })

      out(c.bold(`endpoint ${ep.endpoint_name ?? ep.id}`))
      printTable(
        ['FIELD', 'VALUE'],
        [
          ['id', String(ep.id)],
          ['state', String(ep.endpoint_state ?? '-')],
          ['min_load', num(ep.min_load)],
          ['min_cold_load', num(ep.min_cold_load)],
          ['target_util', num(ep.target_util)],
          ['cold_mult', num(ep.cold_mult)],
          ['cold_workers', String(ep.cold_workers ?? '-')],
          ['max_workers', String(ep.max_workers ?? '-')],
          ['max_queue_time', num(ep.max_queue_time, 1)],
          ['target_queue_time', num(ep.target_queue_time, 1)],
          ['inactivity_timeout', String(ep.inactivity_timeout ?? '-')],
          ['created', ep.created_at ? new Date(Number(ep.created_at) * 1000).toISOString() : '-'],
        ],
      )

      if (!group) {
        out('')
        out(c.yellow('no workergroup attached: nothing will ever be rented for this endpoint'))
        return
      }

      out('')
      out(c.bold(`workergroup ${group.id}`))
      printTable(
        ['FIELD', 'VALUE'],
        [
          ['template_id', String(group.template_id ?? '-')],
          ['template_hash', String(group.template_hash ?? '-')],
          ['gpu_ram', group.gpu_ram ? `${group.gpu_ram} GB` : '-'],
          ['min_load', num(group.min_load)],
          ['target_util', num(group.target_util)],
          ['cold_mult', num(group.cold_mult)],
          ['cold_workers', String(group.cold_workers ?? '-')],
          ['test_workers', String(group.test_workers ?? '-')],
          ['launch_args', String(group.launch_args ?? '-')],
        ],
      )
      if (group.search_query) {
        out('')
        out(c.bold('search query'))
        out(JSON.stringify(group.search_query, null, 2))
      }
    })

  idOpts(common(cmd.command('workers')))
    .description('live workers under an endpoint')
    .action(async (o: CommonOptions & { id?: number; name?: string }) => {
      const { cfg, json: asJson } = ctx(o)
      const api = client(cfg)
      const ep = await resolve(api, cfg, o)
      const workers = await endpointWorkers(api, Number(ep.id))
      if (asJson) return json(workers)

      printTable(
        ['ID', 'STATUS', 'GPU', 'LOAD', 'PERF', 'MEASURED', 'READY', 'DISK', 'UP'],
        workers.map((w) => [
          w.id,
          String(w.status ?? (w.available ? 'available' : '?')),
          w.gpu_name ?? '-',
          num(w.cur_load, 1),
          num(w.perf, 1),
          num(w.measured_perf, 1),
          w.ready_ever ? c.green('yes') : c.yellow('no'),
          w.disk_usage ? `${Number(w.disk_usage).toFixed(1)}G` : '-',
          w.loaded_at ? fmtDuration(Date.now() / 1000 - Number(w.loaded_at)) : '-',
        ]),
        'the endpoint has no workers right now',
      )
    })

  idOpts(common(cmd.command('logs')))
    .description('autoscaler logs for an endpoint or its workergroup')
    .option('-n, --tail <n>', 'last N lines', (v) => int(v, '--tail'))
    .option('--workergroup', 'the workergroup log instead of the endpoint log')
    .action(async (o: CommonOptions & { id?: number; name?: string; tail?: number; workergroup?: boolean }) => {
      const { cfg, json: asJson } = ctx(o)
      const api = client(cfg)
      const ep = await resolve(api, cfg, o)

      const target = o.workergroup ? await resolveGroup(api, cfg, Number(ep.id)) : ep
      const res = o.workergroup
        ? await workergroupLogs(api, Number(target.id), o.tail)
        : await endpointLogs(api, Number(target.id), o.tail)

      if (asJson) return json(res)

      // The autoscaler returns either a blob of text or a map of named logs.
      for (const [key, value] of Object.entries(res)) {
        if (key === 'success' || key === 'api_key') continue
        if (typeof value === 'string') {
          if (Object.keys(res).length > 2) out(c.bold(`--- ${key} ---`))
          out(value)
        } else if (value !== null && value !== undefined) {
          out(c.bold(`--- ${key} ---`))
          out(JSON.stringify(value, null, 2))
        }
      }
    })

  knobOptions(common(cmd.command('create')))
    .description('create an endpoint, and optionally the workergroup that feeds it')
    .requiredOption('--name <name>', 'endpoint name')
    .option('--workergroup', 'also create a workergroup bound to it')
    .option('--template-id <id>', 'template for the workergroup', (v) => int(v, '--template-id'))
    .option('--template-hash <hash>', 'template hash for the workergroup')
    .option('--search-params <query>', 'offer filters; defaults to VAST_SEARCH_PARAMS')
    .option('--gpu-ram <gb>', 'GPU RAM the workergroup asks for', (v) => float(v, '--gpu-ram'))
    .option('--test-workers <n>', 'workers used to measure performance', (v) => int(v, '--test-workers'))
    .action(
      async (
        o: CommonOptions & {
          name: string
          workergroup?: boolean
          templateId?: number
          templateHash?: string
          searchParams?: string
          gpuRam?: number
          testWorkers?: number
        },
      ) => {
        const { cfg, json: asJson } = ctx(o)
        const api = spender(cfg, 'cv endpoint create')
        const knobs = collectKnobs(o as unknown as Record<string, unknown>)

        const created = (await createEndpoint(api, { ...knobs, endpoint_name: o.name })) as {
          id?: number
          [k: string]: unknown
        }

        let group: unknown = null
        if (o.workergroup) {
          const endpointId =
            typeof created.id === 'number' ? created.id : Number((await findEndpoint(api, { name: o.name })).id)
          const wg: Parameters<typeof createWorkergroup>[1] = {
            ...knobs,
            endpoint_id: endpointId,
            endpoint_name: o.name,
            search_params: o.searchParams ?? cfg.searchParams,
          }
          if (o.templateId !== undefined) wg.template_id = o.templateId
          else if (cfg.templateId) wg.template_id = cfg.templateId
          if (o.templateHash !== undefined) wg.template_hash = o.templateHash
          if (o.gpuRam !== undefined) wg.gpu_ram = o.gpuRam
          if (o.testWorkers !== undefined) wg.test_workers = o.testWorkers
          group = await createWorkergroup(api, wg)
        }

        if (asJson) return json({ endpoint: created, workergroup: group })
        out(c.green(`endpoint ${o.name} created`))
        out(JSON.stringify(created, null, 2))
        if (group) {
          out(c.green('workergroup created'))
          out(JSON.stringify(group, null, 2))
        }
        out('')
        out(c.dim('Record the ids in .env as VAST_ENDPOINT_ID / VAST_WORKERGROUP_ID.'))
      },
    )

  idOpts(knobOptions(common(cmd.command('update'))))
    .description('change endpoint and workergroup settings')
    .option('--workergroup', 'apply the knobs to the workergroup instead')
    .option('--search-params <query>', 'replace the workergroup offer filters')
    .option('--from-env', 'push VAST_SEARCH_PARAMS from .env to the workergroup')
    .option('--template-id <id>', 'change the workergroup template', (v) => int(v, '--template-id'))
    .option('--roll', 'after updating, roll existing workers onto the new template')
    .action(
      async (
        o: CommonOptions & {
          id?: number
          name?: string
          workergroup?: boolean
          searchParams?: string
          fromEnv?: boolean
          templateId?: number
          roll?: boolean
        },
      ) => {
        const { cfg, json: asJson } = ctx(o)
        const api = spender(cfg, 'cv endpoint update')
        const ep = await resolve(api, cfg, o)
        const knobs = collectKnobs(o as unknown as Record<string, unknown>)

        const touchesGroup =
          o.workergroup === true || o.searchParams !== undefined || o.fromEnv === true || o.templateId !== undefined

        const result: Record<string, unknown> = {}

        if (!touchesGroup || o.workergroup !== true) {
          if (Object.keys(knobs).length === 0 && !touchesGroup) {
            throw new CliError('Nothing to change.', {
              code: EXIT.USAGE,
              hint: 'Pass at least one knob, e.g. --max-workers 2.',
            })
          }
          if (Object.keys(knobs).length > 0) {
            result['endpoint'] = await updateEndpoint(api, ep, knobs)
          }
        }

        if (touchesGroup) {
          const group = await resolveGroup(api, cfg, Number(ep.id))
          const changes: Parameters<typeof updateWorkergroup>[2] = o.workergroup === true ? { ...knobs } : {}
          if (o.fromEnv) changes.search_params = cfg.searchParams
          if (o.searchParams !== undefined) changes.search_params = o.searchParams
          if (o.templateId !== undefined) changes.template_id = o.templateId
          result['workergroup'] = await updateWorkergroup(api, group, changes)

          if (o.roll) result['roll'] = await updateWorkers(api, Number(group.id))
        }

        if (asJson) return json(result)
        out(c.green(`endpoint ${ep.endpoint_name ?? ep.id} updated`))
        out(c.dim('Run `cv endpoint show` to confirm the stored values.'))
      },
    )

  idOpts(common(cmd.command('scale')))
    .description('shorthand for the knobs that decide how much hardware is up')
    .option('--max <n>', 'max_workers', (v) => int(v, '--max'))
    .option('--cold <n>', 'cold_workers', (v) => int(v, '--cold'))
    .option('--cold-mult <x>', 'cold_mult', (v) => float(v, '--cold-mult'))
    .option('--target-util <ratio>', 'target_util', (v) => float(v, '--target-util'))
    .option('--min-load <perf>', 'min_load', (v) => float(v, '--min-load'))
    .option('--down', 'park the endpoint: max_workers 0, cold_workers 0')
    .action(
      async (
        o: CommonOptions & {
          id?: number
          name?: string
          max?: number
          cold?: number
          coldMult?: number
          targetUtil?: number
          minLoad?: number
          down?: boolean
        },
      ) => {
        const { cfg, json: asJson } = ctx(o)
        const api = o.down === true ? client(cfg) : spender(cfg, 'cv endpoint scale')
        const ep = await resolve(api, cfg, o)

        const knobs: AutoscalerKnobs = {}
        if (o.down) {
          knobs.max_workers = 0
          knobs.cold_workers = 0
        }
        if (o.max !== undefined) knobs.max_workers = o.max
        if (o.cold !== undefined) knobs.cold_workers = o.cold
        if (o.coldMult !== undefined) knobs.cold_mult = o.coldMult
        if (o.targetUtil !== undefined) knobs.target_util = o.targetUtil
        if (o.minLoad !== undefined) knobs.min_load = o.minLoad

        if (Object.keys(knobs).length === 0) {
          throw new CliError('Nothing to scale.', {
            code: EXIT.USAGE,
            hint: 'e.g. cv endpoint scale --max 2 --cold 1, or --down to park it.',
          })
        }

        const before = {
          max_workers: ep.max_workers,
          cold_workers: ep.cold_workers,
          cold_mult: ep.cold_mult,
          target_util: ep.target_util,
          min_load: ep.min_load,
        }
        const res = await updateEndpoint(api, ep, knobs)

        if (asJson) return json({ before, applied: knobs, result: res })
        out(c.green(`endpoint ${ep.endpoint_name ?? ep.id} scaled`))
        printTable(
          ['KNOB', 'WAS', 'NOW'],
          Object.entries(knobs).map(([k, v]) => [k, String((before as Record<string, unknown>)[k] ?? '-'), String(v)]),
        )
      },
    )

  idOpts(destructive(cmd.command('delete')))
    .description('delete an endpoint, and optionally its workergroup')
    .option('--workergroup', 'delete the attached workergroup too')
    .action(async (o: CommonOptions & { id?: number; name?: string; workergroup?: boolean; yes?: boolean }) => {
      const { cfg, json: asJson, yes } = ctx(o)
      const api = client(cfg)
      const ep = await resolve(api, cfg, o)

      let group: VastWorkergroup | null = null
      if (o.workergroup) {
        try {
          group = await resolveGroup(api, cfg, Number(ep.id))
        } catch {
          group = null
        }
      }

      await confirmDestructive({
        yes,
        message: `Delete endpoint ${ep.endpoint_name ?? ep.id} (${ep.id})${group ? ` and workergroup ${group.id}` : ''}?`,
        detail: [
          'Workers under it are released.',
          'Anything pointing at VAST_ENDPOINT_NAME stops working until it is recreated.',
        ],
      })

      // The workergroup goes first: deleting the endpoint under a live
      // workergroup leaves the workergroup orphaned and still renting.
      const result: Record<string, unknown> = {}
      if (group) result['workergroup'] = await deleteWorkergroup(api, Number(group.id))
      result['endpoint'] = await deleteEndpoint(api, Number(ep.id))

      if (asJson) return json(result)
      out(c.green(`endpoint ${ep.endpoint_name ?? ep.id} deleted`))
    })

  idOpts(common(cmd.command('roll')))
    .description('roll every worker onto the current template')
    .option('--cancel', 'cancel a rolling update in progress')
    .action(async (o: CommonOptions & { id?: number; name?: string; cancel?: boolean }) => {
      const { cfg, json: asJson } = ctx(o)
      const api = spender(cfg, 'cv endpoint roll')
      const ep = await resolve(api, cfg, o)
      const group = await resolveGroup(api, cfg, Number(ep.id))
      const res = await updateWorkers(api, Number(group.id), o.cancel === true)
      if (asJson) return json(res)
      out(c.green(o.cancel ? `rolling update cancelled on workergroup ${group.id}` : `workergroup ${group.id} rolling`))
    })

  return cmd
}

/** Used by `cv doctor`, which needs the endpoint without a command context. */
export async function probeEndpoint(): Promise<{ endpoint: VastEndpoint; workers: number } | null> {
  const cfg = loadConfig()
  if (!cfg.apiKey) return null
  const api = new VastClient(cfg.apiKey, { timeoutMs: 20_000 })
  try {
    const ep = await findEndpoint(api, { id: cfg.endpointId, name: cfg.endpointName })
    const workers = await endpointWorkers(api, Number(ep.id)).catch(() => [])
    return { endpoint: ep, workers: workers.length }
  } catch (e) {
    err(c.dim(`  (${e instanceof Error ? e.message : String(e)})`))
    return null
  }
}
