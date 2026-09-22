import { stat } from 'node:fs/promises'
import { basename } from 'node:path'

import { openAsBlob } from 'node:fs'

import { c, err } from './output.ts'

/**
 * Discord's attachment ceiling for a webhook on a non-boosted guild. Files
 * above it are rejected with a 40005, so the CLI checks before uploading and
 * posts a link-less notice instead of silently losing the image.
 */
export const DISCORD_MAX_BYTES = 8 * 1024 * 1024

export interface DiscordPost {
  /** Absolute path of the image to attach. */
  file: string
  /** Free-form line above the attachment. */
  content: string
}

export interface DiscordResult {
  file: string
  ok: boolean
  skipped?: 'too-large' | 'missing'
  status?: number
  error?: string
}

/**
 * Posts one image per call, as it finishes.
 *
 * Sequential on purpose: webhooks are rate limited per-webhook, and firing a
 * batch in parallel earns a 429 for every request after the first. Retries
 * honour `retry_after` from the body, which is the only accurate number
 * Discord gives (the header is rounded).
 */
export class DiscordNotifier {
  private readonly url: string
  private queue: Promise<unknown> = Promise.resolve()
  private readonly results: DiscordResult[] = []
  private readonly verbose: boolean

  constructor(webhook: string, opts: { verbose?: boolean } = {}) {
    this.url = webhook
    this.verbose = opts.verbose ?? true
  }

  /** Enqueue a post. Returns when this particular post has been attempted. */
  post(p: DiscordPost): Promise<DiscordResult> {
    const run = this.queue.then(() => this.send(p))
    this.queue = run.catch(() => undefined)
    return run
  }

  /** Wait for everything enqueued so far. */
  async drain(): Promise<DiscordResult[]> {
    await this.queue
    return this.results
  }

  private record(r: DiscordResult): DiscordResult {
    this.results.push(r)
    if (this.verbose) {
      if (r.ok) err(c.dim(`  discord: posted ${basename(r.file)}`))
      else if (r.skipped === 'too-large') err(c.yellow(`  discord: ${basename(r.file)} is over 8 MB, not attached`))
      else err(c.yellow(`  discord: ${basename(r.file)} failed (${r.error ?? r.status})`))
    }
    return r
  }

  private async send(p: DiscordPost): Promise<DiscordResult> {
    let size: number
    try {
      size = (await stat(p.file)).size
    } catch {
      return this.record({ file: p.file, ok: false, skipped: 'missing', error: 'file not found' })
    }

    if (size > DISCORD_MAX_BYTES) {
      // Still worth telling the channel the render exists.
      const note = `${p.content}\n_(${basename(p.file)} is ${(size / 1024 / 1024).toFixed(1)} MB, over the 8 MB webhook limit; it is on disk only)_`
      await this.sendJson(note)
      return this.record({ file: p.file, ok: false, skipped: 'too-large' })
    }

    const blob = await openAsBlob(p.file, { type: contentType(p.file) })
    for (let attempt = 0; attempt < 5; attempt++) {
      const form = new FormData()
      form.set('payload_json', JSON.stringify({ content: p.content, allowed_mentions: { parse: [] } }))
      form.set('files[0]', blob, basename(p.file))

      const res = await fetch(this.url, { method: 'POST', body: form })
      if (res.ok) return this.record({ file: p.file, ok: true, status: res.status })

      if (res.status === 429) {
        await sleep(await retryAfterMs(res))
        continue
      }
      if (res.status >= 500) {
        await sleep(1000 * (attempt + 1))
        continue
      }
      const body = await res.text()
      return this.record({ file: p.file, ok: false, status: res.status, error: body.slice(0, 300) })
    }
    return this.record({ file: p.file, ok: false, error: 'still rate limited after 5 attempts' })
  }

  /** Plain message, no attachment. */
  async sendJson(content: string): Promise<boolean> {
    for (let attempt = 0; attempt < 5; attempt++) {
      const res = await fetch(this.url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ content, allowed_mentions: { parse: [] } }),
      })
      if (res.ok) return true
      if (res.status === 429) {
        await sleep(await retryAfterMs(res))
        continue
      }
      if (res.status >= 500) {
        await sleep(1000 * (attempt + 1))
        continue
      }
      return false
    }
    return false
  }
}

async function retryAfterMs(res: Response): Promise<number> {
  try {
    const body = (await res.clone().json()) as { retry_after?: number }
    if (typeof body.retry_after === 'number') return Math.ceil(body.retry_after * 1000) + 100
  } catch {
    // fall through to the header
  }
  const header = res.headers.get('retry-after')
  const secs = header ? Number(header) : NaN
  return Number.isFinite(secs) ? secs * 1000 + 100 : 2000
}

function contentType(path: string): string {
  const ext = path.slice(path.lastIndexOf('.')).toLowerCase()
  if (ext === '.jpg' || ext === '.jpeg') return 'image/jpeg'
  if (ext === '.webp') return 'image/webp'
  return 'image/png'
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}

/** Compose the message that goes with one rendered image. */
export function describeRender(params: Record<string, unknown>, file: string, extra?: string): string {
  const bits: string[] = []
  const push = (label: string, key: string) => {
    const v = params[key]
    if (v !== undefined && v !== null && v !== '' && v !== false) bits.push(`${label} \`${String(v)}\``)
  }
  push('seed', 'seed')
  if (params['width'] && params['height']) bits.push(`size \`${params['width']}x${params['height']}\``)
  push('steps', 'steps')
  push('cfg', 'cfg')
  push('family', 'family')
  push('lora', 'lora')
  push('denoise', 'denoise')
  push('bg', 'remove_bg')
  if (params['no_upscale'] === true) bits.push('upscale `off`')
  if (params['no_face'] === true) bits.push('face pass `off`')

  const prompt = typeof params['prompt'] === 'string' ? params['prompt'] : ''
  const head = `**${basename(file)}**`
  const line = bits.length ? `\n${bits.join(' · ')}` : ''
  const body = prompt ? `\n> ${prompt.slice(0, 900)}` : ''
  return `${head}${line}${body}${extra ? `\n${extra}` : ''}`
}
