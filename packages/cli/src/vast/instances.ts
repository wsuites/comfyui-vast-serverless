import { CliError, EXIT } from '../lib/errors.ts'
import type { VastClient } from './client.ts'
import type { VastInstance } from './types.ts'

interface InstancesPage {
  instances?: VastInstance[]
  next_token?: string | null
  total_instances?: number
}

/**
 * Every instance the account owns.
 *
 * Pages through /api/v1/instances/ following next_token, like the SDK does.
 * `select_cols` is omitted on purpose so the backend returns full rows.
 */
export async function listInstances(client: VastClient): Promise<VastInstance[]> {
  const rows: VastInstance[] = []
  const params: Record<string, string | number | object> = {
    select_filters: {},
    order_by: [{ col: 'id', dir: 'asc' }],
    limit: 25,
  }

  for (let page = 0; page < 200; page++) {
    const data = await client.get<InstancesPage>('/api/v1/instances/', params)
    for (const row of data.instances ?? []) rows.push(row)
    const next = data.next_token
    if (!next) break
    params['after_token'] = next
  }
  return rows
}

export async function getInstance(client: VastClient, id: number): Promise<VastInstance> {
  const data = await client.get<{ instances?: VastInstance }>(`/instances/${id}/`, { owner: 'me' })
  const inst = data.instances
  if (!inst || typeof inst !== 'object') {
    throw new CliError(`No instance ${id} on this account.`, { code: EXIT.NOT_FOUND })
  }
  return inst
}

export function startInstance(client: VastClient, id: number): Promise<unknown> {
  return client.put(`/instances/${id}/`, { state: 'running' })
}

export function stopInstance(client: VastClient, id: number): Promise<unknown> {
  return client.put(`/instances/${id}/`, { state: 'stopped' })
}

export function rebootInstance(client: VastClient, id: number): Promise<unknown> {
  return client.put(`/instances/reboot/${id}/`, {})
}

export function destroyInstance(client: VastClient, id: number): Promise<unknown> {
  return client.delete(`/instances/${id}/`, {})
}

export interface CreateInstanceOptions {
  image: string
  disk: number
  env?: Record<string, string>
  label?: string
  onstart?: string
  price?: number
  templateHash?: string
  templateId?: number
  runtype?: string
}

/** Rent one offer. `id` is the offer (ask) id, not a machine id. */
export function createInstance(client: VastClient, offerId: number, o: CreateInstanceOptions): Promise<unknown> {
  const body: Record<string, unknown> = {
    client_id: 'me',
    image: o.image,
    env: o.env ?? {},
    disk: o.disk,
    label: o.label ?? null,
    onstart: o.onstart ?? null,
    price: o.price ?? null,
    template_hash_id: o.templateHash ?? null,
  }
  if (o.templateId !== undefined) body['template_id'] = o.templateId
  if (o.runtype) body['runtype'] = o.runtype
  return client.put(`/asks/${offerId}/`, body)
}

interface LogsResponse {
  result_url?: string
  [k: string]: unknown
}

/**
 * Instance logs.
 *
 * The API does not return the text: it stages a file and hands back a URL that
 * only becomes available a moment later, so the URL has to be polled.
 */
export async function instanceLogs(
  client: VastClient,
  id: number,
  opts: { tail?: number; filter?: string; daemon?: boolean } = {},
): Promise<string> {
  const body: Record<string, unknown> = {}
  if (opts.tail) body['tail'] = opts.tail
  if (opts.filter) body['filter'] = opts.filter
  if (opts.daemon) body['daemon_logs'] = 'true'

  const res = await client.put<LogsResponse>(`/instances/request_logs/${id}/`, body)
  const url = res.result_url
  if (!url) {
    throw new CliError(`Vast did not stage a log file for instance ${id}.`, {
      code: EXIT.UPSTREAM,
      details: res,
    })
  }

  for (let attempt = 0; attempt < 30; attempt++) {
    await new Promise((r) => setTimeout(r, 300))
    try {
      const r = await fetch(url)
      if (r.ok) {
        const text = await r.text()
        if (text) return text
      }
    } catch {
      // the object is not there yet; keep polling
    }
  }
  throw new CliError(`The log file for instance ${id} never appeared.`, {
    code: EXIT.UPSTREAM,
    hint: 'The instance may still be booting; retry in a few seconds.',
  })
}

export interface SshTarget {
  host: string
  port: number
  user: string
  direct: boolean
}

/**
 * Where to SSH.
 *
 * Prefer the machine's own address and its mapped port 22 when the instance
 * exposes one: the proxy hop through sshN.vast.ai works but adds latency and
 * dies with the proxy. Falls back to the proxy when there is no direct port.
 */
export function sshTarget(inst: VastInstance): SshTarget | null {
  const ports = inst.ports ?? null
  const mapped = ports?.['22/tcp']?.find((p) => p.HostPort)
  const ip = inst.public_ipaddr ?? null
  if (mapped && ip) {
    const port = Number(mapped.HostPort)
    if (Number.isFinite(port)) return { host: ip.trim(), port, user: 'root', direct: true }
  }
  if (inst.ssh_host && inst.ssh_port) {
    return { host: String(inst.ssh_host).trim(), port: Number(inst.ssh_port), user: 'root', direct: false }
  }
  return null
}

export function sshCommand(t: SshTarget, keyPath: string | null): string {
  const key = keyPath ? ` -i "${keyPath}"` : ''
  return `ssh${key} -p ${t.port} ${t.user}@${t.host}`
}
