import { type ChildProcess } from 'node:child_process'
import { spawnTool } from '../lib/spawn.ts'

import { Command } from 'commander'

import type { Config } from '../lib/config.ts'
import { askYesNo } from '../lib/confirm.ts'
import { CliError, EXIT } from '../lib/errors.ts'
import { c, err, isTTY, json, out } from '../lib/output.ts'
import { ROOT } from '../lib/paths.ts'
import { common, ctx, int, type CommonOptions } from '../lib/program.ts'
import { startApi, stopChild, type ApiProcess } from '../web/apiserver.ts'
import { astroBuildExists, requireBuild, serverEnv, startAstro, type EmbeddedSite } from '../web/astro.ts'
import { generatePassword, writeEnvKey } from '../web/password.ts'
import { startTunnel, type Tunnel } from '../web/tunnel.ts'

interface UpOptions extends CommonOptions {
  tunnel?: boolean
  dev?: boolean
  api?: boolean
  port?: number
}

/**
 * Refuse to publish an unprotected console, and offer the fix.
 *
 * The tunnel makes the render console reachable by anyone who guesses the
 * hostname, and the console spends real money on GPUs. Astro is where the
 * contract puts authentication, so an empty WEBAPP_PASS with WEBAPP_AUTH on is
 * an open door — the CLI stops here rather than after the URL is printed.
 *
 * Returns the password to use for the session.
 */
async function ensureTunnelPassword(cfg: Config, asJson: boolean): Promise<string> {
  if (!cfg.webappAuth) {
    throw new CliError('WEBAPP_AUTH is off, so a tunnel would publish the console with no password at all.', {
      code: EXIT.CONFIG,
      hint: 'Set WEBAPP_AUTH=on and WEBAPP_PASS=<something> in .env before using --tunnel.',
    })
  }
  if (cfg.webappPass) return cfg.webappPass

  const problem = 'WEBAPP_AUTH is on but WEBAPP_PASS is empty: a tunnel would publish the console unprotected.'

  if (asJson || !isTTY()) {
    throw new CliError(problem, {
      code: EXIT.CONFIG,
      hint: 'Set WEBAPP_PASS in .env, or run `cv web up --tunnel` on a terminal to have one generated for you.',
    })
  }

  err(c.yellow(problem))
  const generated = generatePassword(24)
  const ok = await askYesNo(`Generate a password and write WEBAPP_PASS to .env?`, true)
  if (!ok) {
    throw new CliError('No password set; refusing to open the tunnel.', {
      code: EXIT.CONFIG,
      hint: 'Set WEBAPP_PASS in .env and try again.',
    })
  }

  writeEnvKey('WEBAPP_PASS', generated)
  // The already-spawned API child inherited the old (empty) value, but Astro
  // reads it in this process, so the running config is updated too.
  process.env['WEBAPP_PASS'] = generated
  cfg.webappPass = generated
  cfg.raw['WEBAPP_PASS'] = generated
  err(c.green('WEBAPP_PASS written to .env'))
  return generated
}

/**
 * Run `astro dev` as a child.
 *
 * Dev mode is the one case the CLI does not embed the site: the dev server is
 * a Vite process with its own lifecycle and HMR, and there is no standalone
 * entry to import. It is spawned and torn down like any other child.
 */
function startAstroDev(cfg: Config, port: number): ChildProcess {
  const child = spawnTool('pnpm', ['--filter', '@comfy-vast/web', 'dev', '--host', cfg.webHost, '--port', String(port)], {
    cwd: ROOT,
    stdio: ['ignore', 'pipe', 'pipe'],
    // Same variables the embedded server gets: the dev server runs the very
    // same middleware, so it authenticates and proxies identically.
    env: { ...process.env, ...serverEnv(cfg), PORT: String(port) },
  })
  const pipe = (chunk: Buffer) => {
    for (const line of chunk.toString('utf8').split(/\r?\n/)) {
      if (line.trim()) err(c.dim('web   ') + line)
    }
  }
  child.stdout?.on('data', pipe)
  child.stderr?.on('data', pipe)
  child.on('error', (e) => err(c.red(`web   could not start the dev server: ${e.message}`)))
  return child
}

export function webCommand(): Command {
  const cmd = new Command('web').description('the browser front end, the API behind it, and an optional tunnel')

  common(cmd.command('up', { isDefault: true }))
    .description('start the API and the web UI, and hold them until Ctrl-C')
    .option('--tunnel', 'publish the UI through cloudflared')
    .option('--dev', 'run the Astro dev server instead of the built site')
    .option('--no-api', 'do not start webapp/server.py; assume it is already up')
    .option('-p, --port <n>', 'port for the web UI, overriding WEB_PORT', (v) => int(v, '--port'))
    .action(async (o: UpOptions) => {
      const { cfg, json: asJson } = ctx(o)
      if (o.port !== undefined) cfg.webPort = o.port

      // Checked before anything is spawned: finding out the build is missing
      // after the API is up means killing it again for nothing.
      if (!o.dev) requireBuild()

      let password = cfg.webappPass
      if (o.tunnel) password = await ensureTunnelPassword(cfg, asJson)

      let api: ApiProcess | null = null
      let site: EmbeddedSite | null = null
      let dev: ChildProcess | null = null
      let tunnel: Tunnel | null = null
      let closing = false

      /**
       * Tear down in the reverse of the order things were started: the tunnel
       * first so no request arrives at a server that is already gone, then the
       * web layer, then the API child last because the UI depends on it. Every
       * step is guarded so one failure cannot orphan the next process.
       */
      const shutdown = async (): Promise<void> => {
        if (closing) return
        closing = true
        err('')
        err(c.dim('shutting down'))
        if (tunnel) {
          await tunnel.stop().catch(() => undefined)
          err(c.dim('  tunnel  down'))
        }
        if (dev) {
          await stopChild(dev).catch(() => undefined)
          err(c.dim('  web     down'))
        }
        if (site) {
          await site.stop().catch(() => undefined)
          err(c.dim('  web     down'))
        }
        if (api?.child) {
          await api.stop().catch(() => undefined)
          err(c.dim('  api     down'))
        }
      }

      try {
        if (o.api !== false) {
          api = await startApi(cfg)
        } else {
          err(c.dim(`api   skipped; expecting one on http://${cfg.apiHost}:${cfg.apiPort}`))
        }

        let url: string
        if (o.dev) {
          dev = startAstroDev(cfg, cfg.webPort)
          url = `http://${cfg.webHost}:${cfg.webPort}`
          // The dev server prints its own ready line; give it a moment so the
          // summary below does not land in the middle of Vite's banner.
          await new Promise((r) => setTimeout(r, 1500))
        } else {
          site = await startAstro(cfg)
          url = site.url
        }

        if (o.tunnel) {
          tunnel = await startTunnel(cfg, { host: cfg.webHost, port: cfg.webPort })
        }

        if (asJson) {
          json({
            local: url,
            api: api?.base ?? `http://${cfg.apiHost}:${cfg.apiPort}`,
            tunnel: tunnel?.url ?? null,
            auth: cfg.webappAuth ? { user: cfg.webappUser, pass: password } : null,
            mode: o.dev ? 'dev' : 'standalone',
          })
        } else {
          out('')
          out(`  ${c.dim('local ')} ${c.bold(url)}`)
          out(`  ${c.dim('api   ')} ${api?.base ?? `http://${cfg.apiHost}:${cfg.apiPort}`}`)
          if (tunnel?.url) {
            out(`  ${c.dim('public')} ${c.bold(c.green(tunnel.url))}`)
            if (cfg.webappAuth) {
              out(`  ${c.dim('user  ')} ${cfg.webappUser}`)
              out(`  ${c.dim('pass  ')} ${password}`)
            }
          } else if (o.tunnel) {
            out(`  ${c.yellow('public')} cloudflared is up but did not report a URL`)
          }
          out('')
          out(c.dim('Ctrl-C to stop'))
        }

        await new Promise<void>((done) => {
          const onSignal = () => {
            void shutdown().then(done)
          }
          process.once('SIGINT', onSignal)
          process.once('SIGTERM', onSignal)
        })
      } catch (e) {
        await shutdown()
        throw e
      }
    })

  common(cmd.command('status'))
    .description('what the web stack would find if it started now')
    .action(async (o: CommonOptions) => {
      const { cfg, json: asJson } = ctx(o)
      const { ApiClient } = await import('../lib/api.ts')
      const apiUp = await new ApiClient(cfg).healthy()
      const built = astroBuildExists()

      const state = {
        api: { url: `http://${cfg.apiHost}:${cfg.apiPort}`, running: apiUp },
        web: { url: `http://${cfg.webHost}:${cfg.webPort}`, built },
        auth: { enabled: cfg.webappAuth, user: cfg.webappUser, password_set: Boolean(cfg.webappPass) },
        tunnel: { bin: cfg.cloudflaredBin, named: cfg.cfTunnelName || null, hostname: cfg.cfTunnelHostname || null },
      }
      if (asJson) return json(state)

      out(`${apiUp ? c.green('up  ') : c.dim('down')}  api  ${state.api.url}`)
      out(`${built ? c.green('built') : c.red('missing')}  web  ${state.web.url}`)
      if (!built) out(c.dim('       build it with: pnpm web:build'))
      out(
        `${cfg.webappAuth ? (cfg.webappPass ? c.green('on  ') : c.red('open')) : c.yellow('off ')}  auth  ${
          cfg.webappAuth ? `user ${cfg.webappUser}${cfg.webappPass ? '' : ', no password set'}` : 'disabled'
        }`,
      )
    })

  return cmd
}
