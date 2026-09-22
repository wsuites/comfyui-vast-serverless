import { spawn, type ChildProcess } from 'node:child_process'

import type { Config } from '../lib/config.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { c, err } from '../lib/output.ts'
import { stopChild } from './apiserver.ts'

export interface Tunnel {
  url: string | null
  child: ChildProcess
  stop(): Promise<void>
}

const QUICK_URL = /https:\/\/[a-z0-9-]+\.trycloudflare\.com/i

/**
 * Publish the Astro port through cloudflared.
 *
 * The tunnel always points at WEB_HOST:WEB_PORT, never at the API: the API is
 * unauthenticated by design and lives behind Astro, so exposing it would hand
 * out the console with no password.
 *
 * With CF_TUNNEL_NAME set, a named tunnel is run and the public hostname comes
 * from CF_TUNNEL_HOSTNAME (cloudflared does not print it). Otherwise this is a
 * quick tunnel and the assigned trycloudflare.com URL is parsed out of the
 * child's output.
 */
export async function startTunnel(cfg: Config, target: { host: string; port: number }): Promise<Tunnel> {
  const host = target.host === '0.0.0.0' || target.host === '::' ? '127.0.0.1' : target.host
  const url = `http://${host}:${target.port}`

  const args = ['tunnel', '--no-autoupdate', '--url', url]
  if (cfg.cfTunnelName) args.push('run', cfg.cfTunnelName)

  let child: ChildProcess
  try {
    child = spawn(cfg.cloudflaredBin, args, {
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    })
  } catch (e) {
    throw new CliError(`Could not start ${cfg.cloudflaredBin}: ${e instanceof Error ? e.message : String(e)}`, {
      code: EXIT.CONFIG,
      hint: 'Install cloudflared, or point CLOUDFLARED_BIN at it in .env.',
    })
  }

  let failed: string | null = null
  child.on('error', (e: NodeJS.ErrnoException) => {
    failed = e.code === 'ENOENT' ? `cloudflared not found at ${JSON.stringify(cfg.cloudflaredBin)}` : e.message
  })

  let found: string | null = cfg.cfTunnelHostname ? normalise(cfg.cfTunnelHostname) : null
  const waiters: ((u: string) => void)[] = []

  const onChunk = (chunk: string) => {
    const hit = QUICK_URL.exec(chunk)
    if (hit && !found) {
      found = hit[0]
      for (const w of waiters.splice(0)) w(found)
    }
    for (const line of chunk.split(/\r?\n/)) {
      if (!line.trim()) continue
      // cloudflared is very chatty at info level; only the lines that matter
      // to an operator watching a terminal are surfaced.
      if (/ERR|error|failed|Registered tunnel|trycloudflare/i.test(line)) {
        err(c.dim('tunnel ' + line.trim()))
      }
    }
  }
  child.stdout?.setEncoding('utf8')
  child.stderr?.setEncoding('utf8')
  child.stdout?.on('data', onChunk)
  child.stderr?.on('data', onChunk)

  if (!found) {
    const waited = await Promise.race([
      new Promise<string>((resolve) => waiters.push(resolve)),
      sleep(25_000).then(() => null),
    ])
    found = waited
  }

  if (failed) {
    await stopChild(child)
    throw new CliError(`cloudflared failed: ${failed}`, {
      code: EXIT.CONFIG,
      hint: 'Install cloudflared, or point CLOUDFLARED_BIN at it in .env.',
    })
  }

  if (!found && cfg.cfTunnelName) {
    err(c.yellow('tunnel named tunnel is up but no hostname is known; set CF_TUNNEL_HOSTNAME in .env'))
  }

  return {
    url: found,
    child,
    stop: () => stopChild(child),
  }
}

function normalise(hostname: string): string {
  return /^https?:\/\//.test(hostname) ? hostname : `https://${hostname}`
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms))
}
