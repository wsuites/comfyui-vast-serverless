import { Command } from 'commander'

import { ApiClient, PHASES, PHASE_LABELS, type JobSnapshot } from '../lib/api.ts'
import type { Config } from '../lib/config.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { c, err, fmtDuration, fmtMoney, isTTY, json, out, printTable } from '../lib/output.ts'
import { common, ctx, float, int, type CommonOptions } from '../lib/program.ts'
import { genOptions, jobParamsFromOptions, type GenCliOptions } from '../python/gen.ts'

function phaseIndex(phase: string): number {
  const i = (PHASES as readonly string[]).indexOf(phase)
  return i < 0 ? 0 : i
}

function stepper(snapshot: JobSnapshot): string {
  const current = phaseIndex(snapshot.phase)
  const failed = snapshot.state === 'error'
  return PHASES.map((p, i) => {
    const label = PHASE_LABELS[p] ?? p
    if (failed && i === current) return c.red(label)
    if (i < current) return c.green(label)
    if (i === current) return c.bold(c.cyan(label))
    return c.dim(label)
  }).join(c.dim(' > '))
}

/**
 * Render live phase progress.
 *
 * On a TTY the line is rewritten in place; otherwise each distinct phase is
 * printed once, so piping the output to a file does not produce a megabyte of
 * carriage returns for a seven-minute cold start.
 */
class ProgressLine {
  private lastKey = ''
  private readonly tty = isTTY()

  update(s: JobSnapshot): void {
    const key = `${s.phase}|${s.detail}`
    const elapsed = fmtDuration(s.elapsed)
    if (this.tty) {
      const line = `${stepper(s)}  ${c.dim(elapsed)}  ${s.detail}`
      process.stderr.write('\r\x1b[2K' + line.slice(0, (process.stderr.columns ?? 200) - 1))
      this.lastKey = key
      return
    }
    if (key === this.lastKey) return
    this.lastKey = key
    err(`[${elapsed}] ${PHASE_LABELS[s.phase] ?? s.phase}: ${s.detail}`)
  }

  end(): void {
    if (this.tty) process.stderr.write('\r\x1b[2K')
  }
}

async function watchJob(api: ApiClient, id: string, asJson: boolean): Promise<number> {
  const ac = new AbortController()
  const onSigint = () => ac.abort()
  process.once('SIGINT', onSigint)

  const progress = new ProgressLine()
  let last: JobSnapshot | null = null

  try {
    for await (const snapshot of api.stream(id, ac.signal)) {
      last = snapshot
      if (!asJson) progress.update(snapshot)
      if (snapshot.state === 'done' || snapshot.state === 'error') break
    }
  } catch (e) {
    if (!ac.signal.aborted) throw e
  } finally {
    progress.end()
    process.removeListener('SIGINT', onSigint)
  }

  if (ac.signal.aborted) {
    err(c.yellow(`stopped watching; job ${id} keeps running (cv job get ${id})`))
    return EXIT.CANCELLED
  }

  if (!last) {
    throw new CliError(`The stream for job ${id} closed without a single frame.`, { code: EXIT.UPSTREAM })
  }

  if (asJson) {
    json(last)
    return last.state === 'error' ? EXIT.ERROR : EXIT.OK
  }

  if (last.state === 'error') {
    err(c.red(`job ${id} failed after ${fmtDuration(last.elapsed)}: ${last.error ?? 'no reason given'}`))
    return EXIT.ERROR
  }

  out(c.green(`job ${id} done in ${fmtDuration(last.elapsed)}${last.latency ? ` (worker ${last.latency.toFixed(1)}s)` : ''}`))
  for (const img of last.images) out(`  ${img}`)
  return EXIT.OK
}

function apiFor(cfg: Config): ApiClient {
  return new ApiClient(cfg)
}

export function jobCommand(): Command {
  const cmd = new Command('job').description('render jobs on the local API, the same surface the web UI drives')

  const submit = common(cmd.command('submit'))
    .description('queue a render through the API and watch it')
    .argument('[prompt]', 'positive prompt')
    .option('--no-watch', 'return the job id immediately instead of following it')
  genOptions(submit)
  submit
    .option('--cost <n>', 'cost units for the autoscaler', (v) => int(v, '--cost'))
    .option('--timeout <s>', 'seconds to wait on the worker', (v) => float(v, '--timeout'))
    .action(async (prompt: string | undefined, o: CommonOptions & GenCliOptions & { watch?: boolean }) => {
      const { cfg, json: asJson } = ctx(o)
      const api = apiFor(cfg)

      const params = jobParamsFromOptions(o, prompt)
      if (!params['prompt']) {
        throw new CliError('A prompt is required.', {
          code: EXIT.USAGE,
          hint: 'cv job submit "1girl, standing" --no-upscale',
        })
      }

      const { job_id } = await api.submit(params)
      if (o.watch === false) {
        if (asJson) return json({ job_id })
        out(job_id)
        return
      }
      if (!asJson) err(c.dim(`job ${job_id}`))
      process.exitCode = await watchJob(api, job_id, asJson)
    })

  common(cmd.command('ls'))
    .description('jobs the running API knows about')
    .option('--history', 'the on-disk history instead of the in-memory list')
    .option('-n, --limit <n>', 'how many to show', (v) => int(v, '--limit'), 20)
    .action(async (o: CommonOptions & { history?: boolean; limit: number }) => {
      const { cfg, json: asJson } = ctx(o)
      const api = apiFor(cfg)

      if (o.history) {
        const { items } = await api.history(o.limit)
        if (asJson) return json(items)
        printTable(
          ['ID', 'WHEN', 'KIND', 'STATE', 'IMAGES'],
          items.slice(0, o.limit).map((it) => [
            String(it['id'] ?? '-'),
            it['created'] ? new Date(Number(it['created']) * 1000).toISOString().slice(0, 19).replace('T', ' ') : '-',
            String(it['kind'] ?? '-'),
            String(it['state'] ?? '-'),
            Array.isArray(it['images']) ? String((it['images'] as unknown[]).length) : '-',
          ]),
          'no history yet',
        )
        return
      }

      const { jobs } = await api.list()
      if (asJson) return json(jobs)
      printTable(
        ['ID', 'STATE', 'PHASE', 'ELAPSED', 'KIND', 'IMAGES', 'DETAIL'],
        jobs.slice(0, o.limit).map((j) => [
          j.id,
          j.state === 'done' ? c.green(j.state) : j.state === 'error' ? c.red(j.state) : c.yellow(j.state),
          PHASE_LABELS[j.phase] ?? j.phase,
          fmtDuration(j.elapsed),
          j.label ? `${j.kind}:${j.label}` : j.kind,
          j.images.length,
          j.detail,
        ]),
        'the API has no jobs in memory; try --history',
      )
    })

  common(cmd.command('get <id>'))
    .description('one job, with its full phase log')
    .action(async (id: string, o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const j = await apiFor(cfg).get(id)
      if (asJson) return json(j)

      out(`${c.bold(j.id)}  ${j.state === 'error' ? c.red(j.state) : c.green(j.state)}  ${fmtDuration(j.elapsed)}`)
      out(stepper(j))
      out('')
      if (j.error) out(c.red(`error: ${j.error}`))
      printTable(
        ['T', 'PHASE', 'DETAIL'],
        j.log.map((l) => [fmtDuration(l.t), PHASE_LABELS[l.phase] ?? l.phase, l.detail]),
      )
      if (j.images.length) {
        out('')
        out(c.bold('images'))
        for (const img of j.images) out(`  ${img}`)
      }
    })

  common(cmd.command('watch <id>'))
    .description('follow a running job over SSE until it finishes')
    .action(async (id: string, o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      process.exitCode = await watchJob(apiFor(cfg), id, asJson)
    })

  common(cmd.command('cancel <id>'))
    .description('stop following a job (the API has no server-side cancel)')
    .action(async (id: string, o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const api = apiFor(cfg)
      const j = await api.get(id)

      // There is no cancel route on webapp/server.py: a job is an in-flight
      // /generate/sync call against the worker, and the worker has no way to
      // abandon one. Saying so is more useful than a fake success.
      if (asJson) {
        return json({
          id,
          cancelled: false,
          state: j.state,
          reason: 'the API exposes no cancel route; the worker cannot abandon an in-flight /generate/sync',
        })
      }
      if (j.state !== 'running') {
        out(`job ${id} is already ${j.state}; nothing to cancel`)
        return
      }
      err(c.yellow(`job ${id} cannot be cancelled server-side.`))
      err(
        c.dim(
          'The request is an in-flight /generate/sync call on the worker, which has no abort.\n' +
            'It will end on its own at the timeout. To stop paying for the hardware instead:\n' +
            '  cv endpoint scale --down',
        ),
      )
      process.exitCode = EXIT.USAGE
    })

  common(cmd.command('status'))
    .description('live worker and accumulated cost, independent of any job')
    .action(async (o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const s = await apiFor(cfg).status()
      if (asJson) return json(s)

      const worker = (s['worker'] ?? null) as Record<string, unknown> | null
      out(`${c.dim('phase  ')}${PHASE_LABELS[String(s['phase'])] ?? String(s['phase'])}`)
      out(`${c.dim('detail ')}${String(s['detail'] ?? '-')}`)
      if (!worker) {
        out(c.yellow('no worker up'))
        return
      }
      printTable(
        ['FIELD', 'VALUE'],
        [
          ['id', String(worker['id'] ?? '-')],
          ['gpu', String(worker['gpu'] ?? '-')],
          ['machine', String(worker['machine'] ?? '-')],
          ['status', String(worker['status'] ?? '-')],
          ['ready', worker['ready'] ? 'yes' : 'no'],
          ['$/h', worker['dph'] === undefined ? '-' : fmtMoney(Number(worker['dph']), 3)],
          ['up', worker['hours'] ? fmtDuration(Number(worker['hours']) * 3600) : '-'],
          ['spent', worker['spent'] === undefined ? '-' : fmtMoney(Number(worker['spent']), 3)],
        ],
      )
    })

  return cmd
}
