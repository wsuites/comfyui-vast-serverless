import { randomUUID } from 'node:crypto'
import { mkdir, readFile, readdir, stat, writeFile } from 'node:fs/promises'
import { join, relative } from 'node:path'

import { Command } from 'commander'

import type { Config } from '../lib/config.ts'
import { galleryRoot } from '../lib/config.ts'
import { DiscordNotifier, describeRender } from '../lib/discord.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { c, err, fmtBytes, json, out } from '../lib/output.ts'
import { assertOwnsFleet, common, ctx, float, int, type CommonOptions } from '../lib/program.ts'
import { ROOT, fromRoot } from '../lib/paths.ts'
import { runPython } from '../python/bridge.ts'
import { ARMS, callEndpointArgs, genOptions, validateGenOptions, type GenCliOptions } from '../python/gen.ts'
import { extractImageUrls } from '../python/urls.ts'

interface DiscordOptions {
  discord?: boolean
  discordWebhook?: string
}

/** Resolve the webhook: --discord-webhook wins over DISCORD_WEBHOOK in .env. */
function notifier(cfg: Config, o: DiscordOptions): DiscordNotifier | null {
  if (!o.discord && !o.discordWebhook) return null
  const url = o.discordWebhook ?? cfg.discordWebhook
  if (!url) {
    throw new CliError('--discord was asked for but no webhook is configured.', {
      code: EXIT.CONFIG,
      hint: 'Set DISCORD_WEBHOOK in .env, or pass --discord-webhook <url>.',
    })
  }
  return new DiscordNotifier(url)
}

function discordOptions(cmd: Command): Command {
  return cmd
    .option('--discord', 'post each image to the Discord webhook as it finishes')
    .option('--discord-webhook <url>', 'webhook to use instead of DISCORD_WEBHOOK')
}

/**
 * Provenance for a CLI-driven run, in the same shape and the same filename
 * `scripts/config.py:registrar_run` writes, so `cv gallery` reads both without
 * a special case.
 */
async function writeRunInfo(dir: string, extra: Record<string, unknown>): Promise<void> {
  const info = {
    command: ['cv', ...process.argv.slice(2)].join(' '),
    argv: process.argv.slice(3),
    cwd: process.cwd(),
    extra,
  }
  await writeFile(join(dir, 'run_info.json'), JSON.stringify(info, null, 2), 'utf8')
}

async function download(url: string, dest: string): Promise<number> {
  const res = await fetch(url)
  if (!res.ok) throw new CliError(`Could not download ${dest}: HTTP ${res.status}`, { code: EXIT.UPSTREAM })
  const buf = Buffer.from(await res.arrayBuffer())
  await writeFile(dest, buf)
  return buf.length
}

/**
 * Watch an output directory and hand every new image to Discord the moment it
 * appears, rather than collecting them and posting a batch at the end.
 *
 * Polling rather than fs.watch: the Python writes these files from a thread
 * pool across several subdirectories, and recursive watching is not reliable
 * on every platform this runs on. A second of latency on a render that takes
 * minutes is not worth the fragility.
 */
class ImageWatcher {
  private readonly seen = new Set<string>()
  private timer: NodeJS.Timeout | null = null

  constructor(
    private readonly dir: string,
    private readonly onNew: (file: string) => void,
    private readonly intervalMs = 1000,
  ) {}

  /** Record what is already there so a re-run does not repost old images. */
  async prime(): Promise<void> {
    for (const f of await this.list()) this.seen.add(f)
  }

  start(): void {
    this.timer = setInterval(() => {
      void this.tick()
    }, this.intervalMs)
    this.timer.unref?.()
  }

  async stop(): Promise<void> {
    if (this.timer) clearInterval(this.timer)
    this.timer = null
    await this.tick()
  }

  private async tick(): Promise<void> {
    for (const f of await this.list()) {
      if (this.seen.has(f)) continue
      this.seen.add(f)
      // Give the writer a moment to finish; a half-written PNG uploads as a
      // broken image and Discord keeps it forever.
      try {
        const a = (await stat(f)).size
        await new Promise((r) => setTimeout(r, 250))
        const b = (await stat(f)).size
        if (a !== b || b === 0) {
          this.seen.delete(f)
          continue
        }
      } catch {
        continue
      }
      this.onNew(f)
    }
  }

  private async list(depth = 0, dir = this.dir): Promise<string[]> {
    if (depth > 4) return []
    let entries
    try {
      entries = await readdir(dir, { withFileTypes: true })
    } catch {
      return []
    }
    const found: string[] = []
    for (const e of entries) {
      const full = join(dir, e.name)
      if (e.isDirectory()) found.push(...(await this.list(depth + 1, full)))
      else if (/\.(png|jpe?g|webp)$/i.test(e.name)) found.push(full)
    }
    return found
  }
}

export function genCommand(): Command {
  const cmd = new Command('gen').description('render through the Python engine and pull the images down')

  // --- cv gen [prompt] : the single-image path, scripts/call_endpoint.py ----
  const image = common(cmd)
    .argument('[prompt]', 'positive prompt')
    .option('-o, --out <dir>', 'where to put the images; default GALLERY_DIR/cli/<id>')
    .option('--cost <n>', 'cost units for the autoscaler', (v) => int(v, '--cost'))
    .option('--timeout <s>', 'seconds to wait on the worker', (v) => float(v, '--timeout'))
    .option('--workflow <path>', 'workflow JSON to parameterise', undefined)
    .option('--keep-raw', 'keep the raw worker response next to the images')
  genOptions(image)
  discordOptions(image)

  image.action(
    async (
      prompt: string | undefined,
      o: CommonOptions & GenCliOptions & DiscordOptions & { out?: string; keepRaw?: boolean },
    ) => {
      const { cfg, json: asJson } = ctx(o)
      // The Python engine calls the Vast endpoint itself, on this machine's
      // key. With a fleet elsewhere that is a second spender: render through
      // `cv job submit`, which goes to the API that owns the workers.
      assertOwnsFleet(cfg, 'cv gen')

      const problems = validateGenOptions(o)
      if (problems.length) throw new CliError(problems.join('\n'), { code: EXIT.USAGE })
      if (!prompt && !o.workflow) {
        err(c.dim('no prompt given; rendering the workflow as it stands'))
      }

      const runId = randomUUID().replace(/-/g, '').slice(0, 12)
      const dest = o.out ? fromRoot(o.out) : join(galleryRoot(cfg), 'cli', runId)
      await mkdir(dest, { recursive: true })

      const responseFile = join(dest, 'raw.json')
      const args = [...callEndpointArgs(o, prompt), '--out', responseFile]

      if (!asJson) err(c.dim(`run ${runId} -> ${relative(ROOT, dest) || dest}`))

      const result = await runPython({
        script: 'scripts/call_endpoint.py',
        args,
        cfg,
        passthroughStdout: false,
      })

      if (result.code !== 0) {
        throw new CliError(`call_endpoint.py exited ${result.code}.`, {
          code: EXIT.ERROR,
          hint: 'The Python output above has the reason.',
        })
      }

      let raw: unknown
      try {
        raw = JSON.parse(await readFile(responseFile, 'utf8'))
      } catch {
        throw new CliError(`The worker response was not written to ${responseFile}.`, { code: EXIT.ERROR })
      }

      const urls = extractImageUrls(raw)
      if (urls.length === 0) {
        throw new CliError('The worker replied without a single image URL.', {
          code: EXIT.ERROR,
          hint: `The raw response is in ${responseFile}.`,
        })
      }

      const discord = notifier(cfg, o)
      const params: Record<string, unknown> = {
        prompt: prompt ?? null,
        seed: o.seed ?? null,
        width: o.width ?? 1024,
        height: o.height ?? 1024,
        steps: o.steps ?? null,
        cfg: o.cfg ?? null,
        family: o.family ?? 'wai',
        lora: o.lora ?? null,
        remove_bg: o.removeBg ?? null,
        no_upscale: o.upscale === false,
        no_face: o.noFace === true || (o as { face?: boolean }).face === false,
        face_cap: o.faceCap ?? null,
      }

      const saved: { file: string; bytes: number }[] = []
      for (const [i, url] of urls.entries()) {
        const ext = (url.split('?', 1)[0]?.match(/\.(png|jpe?g|webp)$/i)?.[0] ?? '.png').toLowerCase()
        const file = join(dest, `${String(i + 1).padStart(3, '0')}${ext}`)
        const bytes = await download(url, file)
        saved.push({ file, bytes })
        if (!asJson) err(`  ${c.green('saved')} ${relative(ROOT, file)} ${c.dim(fmtBytes(bytes))}`)
        // Posted here, inside the loop: the operator watching Discord sees
        // each image land as it is fetched, not all of them at the end.
        if (discord) void discord.post({ file, content: describeRender(params, file) })
      }

      await writeRunInfo(dest, { run: runId, urls, params, images: saved.map((s) => s.file) })
      if (!o.keepRaw) {
        await writeFile(responseFile, JSON.stringify(raw, null, 2), 'utf8')
      }

      const discordResults = discord ? await discord.drain() : []

      if (asJson) {
        return json({
          run: runId,
          dir: dest,
          images: saved.map((s) => s.file),
          urls,
          discord: discordResults,
        })
      }
      out('')
      out(c.green(`${saved.length} image(s) in ${relative(ROOT, dest) || dest}`))
      out(c.dim('the presigned R2 links in raw.json expire in 7 days'))
    },
  )

  // --- cv gen scene : scripts/escena.py -------------------------------------
  const scene = common(cmd.command('scene'))
    .description('the three-prompt scene pipeline: scene + character + fusion')
    .option('--scene <text>', 'scene prompt')
    .option('--character <text>', 'character prompt')
    .option('--fusion <text>', 'fusion prompt')
    .option('--scene-neg <text>', 'scene negative')
    .option('--character-neg <text>', 'character negative')
    .option('--fusion-neg <text>', 'fusion negative')
    .option('--detail-prompt <text>', 'second pass prompt')
    .option('--seed-scene <n>', 'scene seed', (v) => int(v, '--seed-scene'))
    .option('--seed-character <n>', 'character seed', (v) => int(v, '--seed-character'))
    .option('--seed-fusion <n>', 'fusion seed', (v) => int(v, '--seed-fusion'))
    .option('--denoise <x>', 'fusion denoise', (v) => float(v, '--denoise'))
    .option('--sweep <list>', 'comma-separated denoise values to sweep')
    .option('--size <px>', 'character size in px', (v) => int(v, '--size'))
    .option('--x <px>', 'character x offset', (v) => int(v, '--x'))
    .option('--y <px>', 'character y offset', (v) => int(v, '--y'))
    .option('-o, --out <dir>', 'output directory', 'output/scenes')
    .option('--timeout <s>', 'seconds to wait on the worker', (v) => float(v, '--timeout'))
  discordOptions(scene)
  scene.action(async (o: CommonOptions & DiscordOptions & Record<string, unknown>) => {
    await delegate('scripts/escena.py', o, [
      'scene',
      'character',
      'fusion',
      'scene-neg',
      'character-neg',
      'fusion-neg',
      'detail-prompt',
      'seed-scene',
      'seed-character',
      'seed-fusion',
      'denoise',
      'sweep',
      'size',
      'x',
      'y',
      'out',
      'timeout',
    ])
  })

  // --- cv gen compare : scripts/ab_modelo.py --------------------------------
  const compare = common(cmd.command('compare'))
    .description(`A/B the model arms: ${ARMS.join(', ')}`)
    .option('--arms <list>', `comma-separated arms; any of ${ARMS.join(', ')}`)
    .option('--seeds <n>', 'seeds per arm', (v) => int(v, '--seeds'))
    .option('--base-seed <n>', 'first seed', (v) => int(v, '--base-seed'))
    .option('--prompt <text>', 'overrides BOTH arms; breaks the per-family formatting')
    .option('--negative <text>', 'negative prompt')
    .option('--anima-model <file>', 'UNET file for the anima arm')
    .option('-W, --width <px>', 'width', (v) => int(v, '--width'))
    .option('-H, --height <px>', 'height', (v) => int(v, '--height'))
    .option('-o, --out <dir>', 'output directory', 'output/ab-model')
    .option('--timeout <s>', 'seconds to wait on the worker', (v) => float(v, '--timeout'))
  discordOptions(compare)
  compare.action(async (o: CommonOptions & DiscordOptions & Record<string, unknown>) => {
    await delegate('scripts/ab_modelo.py', o, [
      'arms',
      'seeds',
      'base-seed',
      'prompt',
      'negative',
      'anima-model',
      'width',
      'height',
      'out',
      'timeout',
    ])
  })

  return cmd
}

/**
 * Run one of the multi-image scripts verbatim, streaming it through and
 * posting each PNG to Discord as the Python writes it.
 *
 * These scripts own their own output layout and provenance (`registrar_run`),
 * so the CLI adds nothing to the directory and only watches it.
 */
async function delegate(
  script: string,
  o: CommonOptions & DiscordOptions & Record<string, unknown>,
  flags: string[],
): Promise<void> {
  const { cfg, json: asJson } = ctx(o)
  assertOwnsFleet(cfg, `cv gen (${script})`)

  const args: string[] = []
  for (const flag of flags) {
    const camel = flag.replace(/-([a-z])/g, (_, ch: string) => ch.toUpperCase())
    const value = o[camel]
    if (value === undefined || value === null) continue
    if (typeof value === 'boolean') {
      if (value) args.push(`--${flag}`)
      continue
    }
    args.push(`--${flag}`, String(value))
  }

  const outDir = fromRoot(String(o['out'] ?? 'output'))
  await mkdir(outDir, { recursive: true })

  const discord = notifier(cfg, o)
  const watcher = new ImageWatcher(outDir, (file) => {
    if (!discord) return
    void discord.post({ file, content: describeRender(o as Record<string, unknown>, file, `from \`${script}\``) })
  })
  await watcher.prime()
  if (discord) watcher.start()

  const result = await runPython({ script, args, cfg, passthroughStdout: true })
  await watcher.stop()
  const discordResults = discord ? await discord.drain() : []

  if (result.code !== 0) {
    throw new CliError(`${script} exited ${result.code}.`, { code: EXIT.ERROR })
  }
  if (asJson) json({ script, args, dir: outDir, discord: discordResults })
}
