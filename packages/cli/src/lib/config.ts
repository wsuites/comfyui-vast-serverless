import { existsSync } from 'node:fs'

import { readDotenv, resolveApiKey } from './env.ts'
import { ENV_FILE, expandHome, fromRoot } from './paths.ts'

/** Keys whose value must never be printed in full. */
export const SECRET_KEYS = new Set([
  'VAST_API_KEY',
  'VAST_HF_TOKEN',
  'S3_ACCESS_KEY_ID',
  'S3_SECRET_ACCESS_KEY',
  'DISCORD_WEBHOOK',
  'WEBAPP_PASS',
])

export interface Config {
  /** Raw merged view, same content `scripts/config.py:ENV` would produce. */
  raw: Record<string, string>
  envFileExists: boolean
  envFile: string

  apiKey: string | null
  apiKeySource: string | null

  endpointName: string
  endpointId: number | null
  workergroupId: number | null
  templateId: number | null
  templateName: string
  creatorId: number | null

  image: string
  imageTag: string
  diskSpace: number
  searchParams: string

  apiHost: string
  apiPort: number
  webHost: string
  webPort: number
  webappAuth: boolean
  webappUser: string
  webappPass: string

  cloudflaredBin: string
  cfTunnelName: string
  cfTunnelHostname: string

  sshKey: string
  pythonBin: string
  galleryDir: string
  discordWebhook: string
}

function num(raw: Record<string, string>, key: string): number | null {
  const v = (raw[key] ?? '').trim()
  if (!v) return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

function int(raw: Record<string, string>, key: string, fallback: number): number {
  const n = num(raw, key)
  return n === null ? fallback : Math.trunc(n)
}

function str(raw: Record<string, string>, key: string, fallback = ''): string {
  const v = raw[key]
  return v === undefined || v === '' ? fallback : v
}

/** `WEBAPP_AUTH` is on unless it is explicitly one of the off spellings. */
function authOn(raw: Record<string, string>): boolean {
  const v = str(raw, 'WEBAPP_AUTH', 'on').trim().toLowerCase()
  return !['off', '0', 'false', 'no', ''].includes(v)
}

export function loadConfig(): Config {
  const raw = readDotenv()
  const key = resolveApiKey(raw)

  return {
    raw,
    envFileExists: existsSync(ENV_FILE),
    envFile: ENV_FILE,

    apiKey: key?.key ?? null,
    apiKeySource: key?.source ?? null,

    endpointName: str(raw, 'VAST_ENDPOINT_NAME'),
    endpointId: num(raw, 'VAST_ENDPOINT_ID'),
    workergroupId: num(raw, 'VAST_WORKERGROUP_ID'),
    templateId: num(raw, 'VAST_TEMPLATE_ID'),
    templateName: str(raw, 'VAST_TEMPLATE_NAME'),
    creatorId: num(raw, 'VAST_CREATOR_ID'),

    image: str(raw, 'VAST_IMAGE'),
    imageTag: str(raw, 'VAST_IMAGE_TAG'),
    diskSpace: int(raw, 'VAST_DISK_SPACE', 26),
    searchParams: str(raw, 'VAST_SEARCH_PARAMS'),

    apiHost: str(raw, 'API_HOST', '127.0.0.1'),
    apiPort: int(raw, 'API_PORT', 8800),
    webHost: str(raw, 'WEB_HOST', '127.0.0.1'),
    webPort: int(raw, 'WEB_PORT', 4321),
    webappAuth: authOn(raw),
    webappUser: str(raw, 'WEBAPP_USER', 'admin'),
    webappPass: str(raw, 'WEBAPP_PASS'),

    cloudflaredBin: str(raw, 'CLOUDFLARED_BIN', 'cloudflared'),
    cfTunnelName: str(raw, 'CF_TUNNEL_NAME'),
    cfTunnelHostname: str(raw, 'CF_TUNNEL_HOSTNAME'),

    sshKey: expandHome(str(raw, 'SSH_KEY', '~/.ssh/id_ed25519')),
    pythonBin: str(raw, 'PYTHON_BIN', 'python'),
    galleryDir: str(raw, 'GALLERY_DIR', 'output'),
    discordWebhook: str(raw, 'DISCORD_WEBHOOK'),
  }
}

export function galleryRoot(cfg: Config): string {
  return fromRoot(cfg.galleryDir)
}

export interface Finding {
  level: 'error' | 'warn' | 'ok'
  key: string
  message: string
}

/**
 * Static validation: everything checkable without touching the network.
 * `cv doctor` adds the live probes on top of this.
 */
export function validateConfig(cfg: Config): Finding[] {
  const f: Finding[] = []
  const add = (level: Finding['level'], key: string, message: string) => f.push({ level, key, message })

  if (!cfg.envFileExists) {
    add('error', '.env', `not found at ${cfg.envFile}; copy .env.example to .env`)
  } else {
    add('ok', '.env', cfg.envFile)
  }

  if (cfg.apiKey) add('ok', 'VAST_API_KEY', `resolved from ${cfg.apiKeySource}`)
  else add('error', 'VAST_API_KEY', 'not set and no ~/.config/vastai/vast_api_key; run `vastai set api-key <KEY>`')

  if (!cfg.endpointName) add('error', 'VAST_ENDPOINT_NAME', 'empty; every client needs the endpoint name')
  if (!cfg.endpointId) add('warn', 'VAST_ENDPOINT_ID', 'unset or 0; `endpoint workers` and `endpoint logs` need it')
  if (!cfg.workergroupId) add('warn', 'VAST_WORKERGROUP_ID', 'unset or 0; `endpoint scale` targets the workergroup')

  if (cfg.webPort === cfg.apiPort) {
    add('error', 'WEB_PORT', `collides with API_PORT (${cfg.apiPort})`)
  }
  if (cfg.apiHost !== '127.0.0.1' && cfg.apiHost !== 'localhost' && cfg.apiHost !== '::1') {
    add('error', 'API_HOST', `${cfg.apiHost} is not loopback; the contract binds FastAPI to loopback only`)
  }

  if (cfg.webappAuth && !cfg.webappPass) {
    add('warn', 'WEBAPP_PASS', 'empty while WEBAPP_AUTH is on; `web up --tunnel` will refuse to publish')
  }

  if (!existsSync(cfg.sshKey)) {
    add('warn', 'SSH_KEY', `${cfg.sshKey} does not exist; \`instance ssh\` will fall back to the agent`)
  }

  if (!existsSync(galleryRoot(cfg))) {
    add('warn', 'GALLERY_DIR', `${galleryRoot(cfg)} does not exist yet`)
  }

  if (cfg.discordWebhook && !/^https:\/\/(discord\.com|discordapp\.com)\/api\/webhooks\//.test(cfg.discordWebhook)) {
    add('warn', 'DISCORD_WEBHOOK', 'does not look like a Discord webhook URL')
  }

  const searchDisk = /disk_space\s*>=\s*(\d+)/.exec(cfg.searchParams)
  if (searchDisk && Number(searchDisk[1]) !== cfg.diskSpace) {
    add(
      'warn',
      'VAST_SEARCH_PARAMS',
      `disk_space>=${searchDisk[1]} disagrees with VAST_DISK_SPACE=${cfg.diskSpace}; ` +
        'the autoscaler would filter for machines that cannot host what it allocates',
    )
  }

  return f
}
