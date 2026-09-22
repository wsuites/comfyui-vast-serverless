import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'

import { Command } from 'commander'

import { confirmDestructive } from '../lib/confirm.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { c, err, fmtDuration, fmtMoney, json, out, printTable } from '../lib/output.ts'
import { common, ctx, destructive, float, int, type CommonOptions } from '../lib/program.ts'
import { VastClient } from '../vast/client.ts'
import {
  createInstance,
  destroyInstance,
  getInstance,
  instanceLogs,
  listInstances,
  rebootInstance,
  sshCommand,
  sshTarget,
  startInstance,
  stopInstance,
} from '../vast/instances.ts'
import { buildSearch, searchOffers } from '../vast/offers.ts'
import type { VastInstance } from '../vast/types.ts'

function client(cfg: { apiKey: string | null }): VastClient {
  if (!cfg.apiKey) {
    throw new CliError('No Vast API key.', {
      code: EXIT.CONFIG,
      hint: 'Set VAST_API_KEY in .env or run: vastai set api-key <KEY>',
    })
  }
  return new VastClient(cfg.apiKey)
}

function statusColour(status: string): string {
  if (status === 'running') return c.green(status)
  if (status === 'exited' || status === 'offline') return c.red(status)
  return c.yellow(status)
}

export function instanceCommand(): Command {
  const cmd = new Command('instance').alias('i').description('rent, inspect and reach the GPU machines')

  common(cmd.command('ls'))
    .description('instances this account owns')
    .action(async (o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const rows = await listInstances(client(cfg))
      if (asJson) return json(rows)

      printTable(
        ['ID', 'STATUS', 'GPU', 'N', 'DISK', '$/h', 'UP', 'LABEL', 'IMAGE'],
        rows.map((i) => [
          i.id,
          statusColour(String(i.actual_status ?? i.cur_state ?? '?')),
          i.gpu_name ?? '-',
          i.num_gpus ?? '-',
          i.disk_space ? `${Math.round(Number(i.disk_space))}G` : '-',
          fmtMoney(i.dph_total, 3),
          i.start_date ? fmtDuration(Date.now() / 1000 - Number(i.start_date)) : '-',
          i.label ?? '-',
          String(i.image_uuid ?? '-'),
        ]),
        'no instances; the autoscaler has nothing rented',
      )
    })

  common(cmd.command('search'))
    .description('offers matching VAST_SEARCH_PARAMS from .env')
    .option('-f, --filter <query>', 'extra filters, same syntax as VAST_SEARCH_PARAMS')
    .option('-n, --limit <n>', 'how many offers to show', (v) => int(v, '--limit'), 20)
    .option('-o, --order <field>', 'sort field', 'dph_total')
    .option('--desc', 'sort descending')
    .option('--storage <gb>', 'allocated storage the price is computed against', (v) => float(v, '--storage'))
    .option('--bare', 'ignore VAST_SEARCH_PARAMS, search only --filter')
    .option('--no-defaults', 'drop the verified/rentable/rented baseline filters')
    .option('--show-query', 'print the query object that will be sent')
    .action(
      async (
        o: CommonOptions & {
          filter?: string
          limit: number
          order: string
          desc?: boolean
          storage?: number
          bare?: boolean
          defaults?: boolean
          showQuery?: boolean
        },
      ) => {
        const { cfg, json: asJson } = ctx(o)
        const search = {
          baseline: o.bare ? '' : cfg.searchParams,
          extra: o.filter ?? '',
          limit: o.limit,
          storage: o.storage ?? cfg.diskSpace,
          order: [[o.order, o.desc ? 'desc' : 'asc']] as [string, 'asc' | 'desc'][],
          noDefault: o.defaults === false,
        }

        if (o.showQuery) {
          const built = buildSearch(search)
          if (asJson) return json(built.query)
          out(JSON.stringify(built.query, null, 2))
          return
        }

        const offers = await searchOffers(client(cfg), search)
        if (asJson) return json(offers)

        if (!o.bare && cfg.searchParams) err(c.dim(`filters: ${cfg.searchParams}${o.filter ? ' ' + o.filter : ''}`))
        printTable(
          ['OFFER', 'MACHINE', 'GPU', 'N', 'VRAM', 'DLPERF', 'DISK', '$/h', 'STOR $/mo', 'NET $/GB', 'REL', 'WHERE'],
          offers.map((x) => [
            x.id,
            x.machine_id ?? '-',
            x.gpu_name ?? '-',
            x.num_gpus ?? '-',
            x.gpu_ram ? `${Math.round(Number(x.gpu_ram) / 1000)}G` : '-',
            x.dlperf ? Number(x.dlperf).toFixed(0) : '-',
            x.disk_space ? `${Math.round(Number(x.disk_space))}G` : '-',
            fmtMoney(x.dph_total, 3),
            x.storage_cost === null || x.storage_cost === undefined ? '-' : Number(x.storage_cost).toFixed(3),
            x.inet_down_cost === null || x.inet_down_cost === undefined ? '-' : Number(x.inet_down_cost).toFixed(4),
            x.reliability2 ? (Number(x.reliability2) * 100).toFixed(1) : '-',
            String(x.geolocation ?? '-'),
          ]),
          'no offers match those filters; relax them and try again',
        )
      },
    )

  destructive(cmd.command('rent <offer-id>'))
    .description('rent one offer as a standalone instance')
    .option('--image <ref>', 'container image; defaults to VAST_IMAGE:VAST_IMAGE_TAG')
    .option('--disk <gb>', 'disk to allocate', (v) => int(v, '--disk'))
    .option('--label <text>', 'instance label')
    .option('--onstart <cmd>', 'command to run on start')
    .option('--price <dph>', 'bid price ceiling in $/hour', (v) => float(v, '--price'))
    .option('--template-id <id>', 'launch from a Vast template', (v) => int(v, '--template-id'))
    .action(
      async (
        offerId: string,
        o: CommonOptions & {
          image?: string
          disk?: number
          label?: string
          onstart?: string
          price?: number
          templateId?: number
          yes?: boolean
        },
      ) => {
        const { cfg, json: asJson, yes } = ctx(o)
        const id = int(offerId, 'offer-id')
        const image = o.image ?? (cfg.imageTag ? `${cfg.image}:${cfg.imageTag}` : cfg.image)
        const disk = o.disk ?? cfg.diskSpace

        if (!image && o.templateId === undefined && cfg.templateId === null) {
          throw new CliError('Nothing to launch: no --image and no VAST_IMAGE in .env.', { code: EXIT.USAGE })
        }

        await confirmDestructive({
          yes,
          message: `Rent offer ${id} (${image}, ${disk} GB)? This starts billing immediately.`,
        })

        const opts: Parameters<typeof createInstance>[2] = { image, disk }
        if (o.label !== undefined) opts.label = o.label
        if (o.onstart !== undefined) opts.onstart = o.onstart
        if (o.price !== undefined) opts.price = o.price
        const tid = o.templateId ?? (cfg.templateId || undefined)
        if (tid !== undefined) opts.templateId = tid

        const res = await createInstance(client(cfg), id, opts)
        if (asJson) return json(res)
        out(c.green(`rented offer ${id}`))
        out(JSON.stringify(res, null, 2))
      },
    )

  for (const [name, verb, fn] of [
    ['start', 'start', startInstance],
    ['stop', 'stop', stopInstance],
    ['restart', 'reboot', rebootInstance],
  ] as const) {
    common(cmd.command(`${name} <id>`))
      .description(`${verb} an instance`)
      .action(async (idRaw: string, o: CommonOptions) => {
        const { cfg, json: asJson } = ctx(o)
        const id = int(idRaw, 'id')
        const res = await fn(client(cfg), id)
        if (asJson) return json(res)
        out(c.green(`${verb} requested for instance ${id}`))
      })
  }

  destructive(cmd.command('destroy <id>'))
    .description('destroy an instance; the disk and everything on it goes with it')
    .action(async (idRaw: string, o: CommonOptions & { yes?: boolean }) => {
      const { cfg, json: asJson, yes } = ctx(o)
      const id = int(idRaw, 'id')
      const api = client(cfg)

      let inst: VastInstance | null = null
      try {
        inst = await getInstance(api, id)
      } catch {
        // Confirming without the description is still better than refusing.
      }

      await confirmDestructive({
        yes,
        message: `Destroy instance ${id}? This cannot be undone.`,
        detail: inst
          ? [
              `gpu    ${inst.gpu_name ?? '?'} x${inst.num_gpus ?? '?'}`,
              `label  ${inst.label ?? '-'}`,
              `disk   ${inst.disk_space ?? '?'} GB (contents lost)`,
              `up     ${inst.start_date ? fmtDuration(Date.now() / 1000 - Number(inst.start_date)) : '?'}`,
            ]
          : [],
      })

      const res = await destroyInstance(api, id)
      if (asJson) return json(res)
      out(c.green(`instance ${id} destroyed`))
    })

  common(cmd.command('logs <id>'))
    .description('container logs for an instance')
    .option('-n, --tail <n>', 'last N lines', (v) => int(v, '--tail'))
    .option('--filter <text>', 'only lines containing this text')
    .option('--daemon', 'the daemon log instead of the container log')
    .action(async (idRaw: string, o: CommonOptions & { tail?: number; filter?: string; daemon?: boolean }) => {
      const { cfg, json: asJson } = ctx(o)
      const id = int(idRaw, 'id')
      const opts: { tail?: number; filter?: string; daemon?: boolean } = {}
      if (o.tail !== undefined) opts.tail = o.tail
      if (o.filter !== undefined) opts.filter = o.filter
      if (o.daemon) opts.daemon = true
      const text = await instanceLogs(client(cfg), id, opts)
      if (asJson) return json({ instance: id, logs: text })
      process.stdout.write(text.endsWith('\n') ? text : text + '\n')
    })

  common(cmd.command('creds <id>'))
    .description('host, port, user and a ready-to-paste ssh command')
    .action(async (idRaw: string, o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const id = int(idRaw, 'id')
      const inst = await getInstance(client(cfg), id)
      const target = sshTarget(inst)
      if (!target) {
        throw new CliError(`Instance ${id} exposes no SSH endpoint yet.`, {
          code: EXIT.NOT_FOUND,
          hint: 'It is probably still creating; check `cv instance ls`.',
        })
      }
      const key = existsSync(cfg.sshKey) ? cfg.sshKey : null

      if (asJson) {
        return json({
          id,
          host: target.host,
          port: target.port,
          user: target.user,
          direct: target.direct,
          key,
          command: sshCommand(target, key),
        })
      }

      out(`${c.dim('host   ')}${target.host}`)
      out(`${c.dim('port   ')}${target.port}`)
      out(`${c.dim('user   ')}${target.user}`)
      out(`${c.dim('route  ')}${target.direct ? 'direct port' : 'vast ssh proxy'}`)
      out(`${c.dim('key    ')}${key ?? c.yellow(`${cfg.sshKey} (missing, the agent will be used)`)}`)
      out('')
      out(c.bold(sshCommand(target, key)))
    })

  common(cmd.command('ssh [id]'))
    .description('open an interactive SSH session on an instance')
    .allowUnknownOption(true)
    .argument('[args...]', 'extra arguments passed straight to ssh')
    .action(async (idRaw: string | undefined, extra: string[], o: CommonOptions) => {
      const { cfg } = ctx(o)
      const api = client(cfg)

      let id: number
      if (idRaw) {
        id = int(idRaw, 'id')
      } else {
        // The autoscaler keeps exactly one worker here most of the time, so
        // omitting the id is the common case rather than an ambiguity.
        const all = await listInstances(api)
        if (all.length === 0) throw new CliError('No instances to connect to.', { code: EXIT.NOT_FOUND })
        if (all.length > 1) {
          throw new CliError('More than one instance; say which one.', {
            code: EXIT.USAGE,
            hint: `ids: ${all.map((i) => i.id).join(', ')}`,
          })
        }
        id = Number(all[0]?.id)
      }

      const inst = await getInstance(api, id)
      const target = sshTarget(inst)
      if (!target) {
        throw new CliError(`Instance ${id} exposes no SSH endpoint yet.`, { code: EXIT.NOT_FOUND })
      }

      const args: string[] = []
      if (existsSync(cfg.sshKey)) args.push('-i', cfg.sshKey)
      args.push('-p', String(target.port), `${target.user}@${target.host}`, ...extra)

      err(c.dim(`ssh ${args.join(' ')}`))
      const code = await new Promise<number>((resolve) => {
        const child = spawn('ssh', args, { stdio: 'inherit' })
        child.on('error', (e: NodeJS.ErrnoException) => {
          if (e.code === 'ENOENT') {
            err(c.red('ssh is not on PATH.'))
            resolve(EXIT.CONFIG)
            return
          }
          err(c.red(`ssh failed: ${e.message}`))
          resolve(EXIT.ERROR)
        })
        child.on('close', (c2) => resolve(c2 ?? 0))
      })
      process.exitCode = code
    })

  return cmd
}
