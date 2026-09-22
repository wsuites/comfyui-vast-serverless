import { Command } from 'commander'

import { SECRET_KEYS, galleryRoot, loadConfig, validateConfig } from '../lib/config.ts'
import { EXIT } from '../lib/errors.ts'
import { c, json, mask, out, printTable } from '../lib/output.ts'
import { ASTRO_ENTRY, ENV_FILE, ROOT } from '../lib/paths.ts'
import { common, ctx, type CommonOptions } from '../lib/program.ts'

/** Mask anything whose name says it is a credential, wherever it came from. */
function show(key: string, value: string): string {
  if (!value) return ''
  return SECRET_KEYS.has(key) || /KEY|TOKEN|SECRET|PASS|WEBHOOK/i.test(key) ? mask(value) : value
}

export function configCommand(): Command {
  const cmd = common(new Command('config'))
    .description('the resolved configuration, with secrets masked, and what is wrong with it')
    .option('--raw', 'every key from .env and the environment, not just the ones the CLI uses')
    .option('--unmask', 'print secrets in full; for pasting into a shell, not into a chat')
    .option('--check', 'exit non-zero if validation finds an error')
    .action(async (o: CommonOptions & { raw?: boolean; unmask?: boolean; check?: boolean }) => {
      const { json: asJson } = ctx(o)
      const cfg = loadConfig()
      const findings = validateConfig(cfg)
      const reveal = (key: string, value: string) => (o.unmask ? value : show(key, value))

      if (o.raw) {
        const rows = Object.keys(cfg.raw)
          .sort()
          .map((k) => [k, reveal(k, cfg.raw[k] ?? '')])
        if (asJson) return json(Object.fromEntries(rows))
        printTable(['KEY', 'VALUE'], rows, 'nothing resolved')
        return
      }

      const resolved: Record<string, string> = {
        root: ROOT,
        env_file: cfg.envFileExists ? ENV_FILE : `${ENV_FILE} (missing)`,
        vast_api_key: cfg.apiKey ? reveal('VAST_API_KEY', cfg.apiKey) : '',
        vast_api_key_source: cfg.apiKeySource ?? '',
        endpoint_name: cfg.endpointName,
        endpoint_id: cfg.endpointId === null ? '' : String(cfg.endpointId),
        workergroup_id: cfg.workergroupId === null ? '' : String(cfg.workergroupId),
        template_id: cfg.templateId === null ? '' : String(cfg.templateId),
        image: cfg.imageTag ? `${cfg.image}:${cfg.imageTag}` : cfg.image,
        disk_space: String(cfg.diskSpace),
        search_params: cfg.searchParams,
        api: `http://${cfg.apiHost}:${cfg.apiPort}`,
        web: `http://${cfg.webHost}:${cfg.webPort}`,
        web_auth: cfg.webappAuth ? `on, user ${cfg.webappUser}` : 'off',
        web_password: cfg.webappPass ? reveal('WEBAPP_PASS', cfg.webappPass) : '(empty)',
        cloudflared: cfg.cloudflaredBin + (cfg.cfTunnelName ? ` (named: ${cfg.cfTunnelName})` : ''),
        tunnel_hostname: cfg.cfTunnelHostname,
        python_bin: cfg.pythonBin,
        ssh_key: cfg.sshKey,
        gallery_dir: galleryRoot(cfg),
        discord_webhook: cfg.discordWebhook ? reveal('DISCORD_WEBHOOK', cfg.discordWebhook) : '',
        astro_entry: ASTRO_ENTRY,
      }

      if (asJson) {
        json({ config: resolved, findings })
      } else {
        printTable(
          ['KEY', 'VALUE'],
          Object.entries(resolved).map(([k, v]) => [c.dim(k), v || c.dim('(unset)')]),
        )
        out('')
        out(c.bold('validation'))
        const marks = { ok: c.green('ok  '), warn: c.yellow('warn'), error: c.red('err ') }
        for (const f of findings) out(`  ${marks[f.level]} ${c.dim(f.key.padEnd(20))} ${f.message}`)
        const errors = findings.filter((f) => f.level === 'error').length
        const warns = findings.filter((f) => f.level === 'warn').length
        out('')
        out(errors ? c.red(`${errors} error(s), ${warns} warning(s)`) : c.green(`no errors, ${warns} warning(s)`))
      }

      if (o.check && findings.some((f) => f.level === 'error')) process.exitCode = EXIT.CONFIG
    })

  return cmd
}
