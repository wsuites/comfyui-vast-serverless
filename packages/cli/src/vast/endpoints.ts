import { CliError, EXIT } from '../lib/errors.ts'
import type { VastClient } from './client.ts'
import type { AutoscalerKnobs, VastEndpoint, VastWorker, VastWorkergroup } from './types.ts'

interface Wrapped<T> {
  success?: boolean
  results?: T
  msg?: string
}

/** Strip the per-endpoint JWT the API returns; it is a credential. */
function scrub<T extends Record<string, unknown>>(row: T): T {
  const copy = { ...row }
  delete copy['api_key']
  delete copy['auto_delete_in_seconds']
  delete copy['auto_delete_due_24h']
  return copy
}

export async function listEndpoints(client: VastClient): Promise<VastEndpoint[]> {
  const res = await client.get<Wrapped<VastEndpoint[]>>('/endptjobs/')
  return (res.results ?? []).map(scrub)
}

export async function listWorkergroups(client: VastClient): Promise<VastWorkergroup[]> {
  const res = await client.get<Wrapped<VastWorkergroup[]>>('/autojobs/')
  return (res.results ?? []).map(scrub)
}

/**
 * Resolve an endpoint by id, by name, or from .env.
 * Name matching is how `.env` refers to it (VAST_ENDPOINT_NAME).
 */
export async function findEndpoint(
  client: VastClient,
  ref: { id?: number | null; name?: string | null },
): Promise<VastEndpoint> {
  const all = await listEndpoints(client)
  if (all.length === 0) {
    throw new CliError('This account has no serverless endpoints.', {
      code: EXIT.NOT_FOUND,
      hint: 'Create one with: cv endpoint create --name <name>',
    })
  }
  if (ref.id) {
    const hit = all.find((e) => Number(e.id) === Number(ref.id))
    if (hit) return hit
    throw new CliError(`No endpoint with id ${ref.id}.`, {
      code: EXIT.NOT_FOUND,
      hint: `Known ids: ${all.map((e) => e.id).join(', ')}`,
    })
  }
  if (ref.name) {
    const hit = all.find((e) => e.endpoint_name === ref.name)
    if (hit) return hit
    throw new CliError(`No endpoint named ${JSON.stringify(ref.name)}.`, {
      code: EXIT.NOT_FOUND,
      hint: `Known names: ${all.map((e) => e.endpoint_name).join(', ')}`,
    })
  }
  if (all.length === 1 && all[0]) return all[0]
  throw new CliError('Several endpoints exist and none was selected.', {
    code: EXIT.USAGE,
    hint: 'Pass --id or --name, or set VAST_ENDPOINT_NAME in .env.',
  })
}

export interface CreateEndpointOptions extends AutoscalerKnobs {
  endpoint_name: string
  auto_instance?: string
}

export function createEndpoint(client: VastClient, o: CreateEndpointOptions): Promise<unknown> {
  return client.post('/endptjobs/', {
    client_id: 'me',
    min_load: o.min_load ?? 0.0,
    min_cold_load: o.min_cold_load ?? 0.0,
    target_util: o.target_util ?? 0.9,
    cold_mult: o.cold_mult ?? 2.5,
    cold_workers: o.cold_workers ?? 5,
    max_workers: o.max_workers ?? 20,
    endpoint_name: o.endpoint_name,
    max_queue_time: o.max_queue_time ?? null,
    target_queue_time: o.target_queue_time ?? null,
    inactivity_timeout: o.inactivity_timeout ?? null,
    autoscaler_instance: o.auto_instance ?? 'prod',
  })
}

/**
 * Update an endpoint.
 *
 * The API replaces the whole record, so the current values are read first and
 * only the requested knobs are changed. Sending a partial body resets every
 * omitted knob to a default, which on this endpoint means silently jumping
 * max_workers from 1 to 20.
 */
export async function updateEndpoint(
  client: VastClient,
  current: VastEndpoint,
  changes: AutoscalerKnobs & { endpoint_name?: string },
): Promise<unknown> {
  const merged = {
    client_id: 'me',
    endpoint_id: current.id,
    min_load: changes.min_load ?? current.min_load ?? 0.0,
    min_cold_load: changes.min_cold_load ?? current.min_cold_load ?? 0.0,
    target_util: changes.target_util ?? current.target_util ?? 0.9,
    cold_mult: changes.cold_mult ?? current.cold_mult ?? 2.5,
    cold_workers: changes.cold_workers ?? current.cold_workers ?? 0,
    max_workers: changes.max_workers ?? current.max_workers ?? 1,
    max_queue_time: changes.max_queue_time ?? current.max_queue_time ?? null,
    target_queue_time: changes.target_queue_time ?? current.target_queue_time ?? null,
    inactivity_timeout: changes.inactivity_timeout ?? current.inactivity_timeout ?? null,
    endpoint_name: changes.endpoint_name ?? current.endpoint_name ?? null,
  }
  return client.put(`/endptjobs/${current.id}/`, merged)
}

export function deleteEndpoint(client: VastClient, id: number): Promise<unknown> {
  return client.delete(`/endptjobs/${id}/`, {})
}

export interface WorkergroupChanges extends AutoscalerKnobs {
  test_workers?: number
  template_id?: number
  template_hash?: string
  search_params?: string
  launch_args?: string
  gpu_ram?: number
  endpoint_name?: string
  endpoint_id?: number
}

/**
 * Update a workergroup, merging with what is already stored for the same
 * reason `updateEndpoint` does.
 *
 * `search_params` is sent verbatim. The SDK appends
 * " verified=True rentable=True rented=False" unless told not to; that would
 * rewrite a deliberately tuned VAST_SEARCH_PARAMS behind the operator's back,
 * so the CLI never appends anything.
 */
export async function updateWorkergroup(
  client: VastClient,
  current: VastWorkergroup,
  changes: WorkergroupChanges,
): Promise<unknown> {
  const merged: Record<string, unknown> = {
    client_id: 'me',
    autojob_id: current.id,
    min_load: changes.min_load ?? current.min_load ?? 0.0,
    target_util: changes.target_util ?? current.target_util ?? 0.9,
    cold_mult: changes.cold_mult ?? current.cold_mult ?? 1.0,
    cold_workers: changes.cold_workers ?? current.cold_workers ?? 0,
    test_workers: changes.test_workers ?? current.test_workers ?? 0,
    template_hash: changes.template_hash ?? current.template_hash ?? null,
    template_id: changes.template_id ?? current.template_id ?? null,
    search_params: changes.search_params ?? current.search_params ?? '',
    launch_args: changes.launch_args ?? current.launch_args ?? '',
    gpu_ram: changes.gpu_ram ?? current.gpu_ram ?? null,
    endpoint_name: changes.endpoint_name ?? current.endpoint_name ?? null,
    endpoint_id: changes.endpoint_id ?? current.endpoint_id ?? null,
  }
  return client.put(`/autojobs/${current.id}/`, merged)
}

export function createWorkergroup(client: VastClient, o: WorkergroupChanges): Promise<unknown> {
  return client.post('/autojobs/', {
    client_id: 'me',
    min_load: o.min_load ?? 0.0,
    target_util: o.target_util ?? 0.9,
    cold_mult: o.cold_mult ?? 2.5,
    cold_workers: o.cold_workers ?? 0,
    test_workers: o.test_workers ?? 3,
    template_hash: o.template_hash ?? null,
    template_id: o.template_id ?? null,
    search_params: o.search_params ?? '',
    launch_args: o.launch_args ?? '',
    gpu_ram: o.gpu_ram ?? null,
    endpoint_name: o.endpoint_name ?? null,
    endpoint_id: o.endpoint_id ?? null,
    autoscaler_instance: 'prod',
  })
}

export function deleteWorkergroup(client: VastClient, id: number): Promise<unknown> {
  return client.delete(`/autojobs/${id}/`, {})
}

/** Live worker instances under one endpoint. Autoscaler service, not console. */
export async function endpointWorkers(client: VastClient, id: number): Promise<VastWorker[]> {
  const res = await client.request<VastWorker[] | Wrapped<VastWorker[]>>('/get_endpoint_workers/', {
    method: 'POST',
    absolute: client.autoscaler + '/get_endpoint_workers/',
    body: { id },
  })
  if (Array.isArray(res)) return res
  return res.results ?? []
}

export interface EndpointLogs {
  [k: string]: unknown
}

export function endpointLogs(client: VastClient, id: number, tail?: number): Promise<EndpointLogs> {
  const body: Record<string, unknown> = { id }
  if (tail !== undefined) body['tail'] = tail
  return client.autoscalerPost<EndpointLogs>('/get_endpoint_logs/', body)
}

export function workergroupLogs(client: VastClient, id: number, tail?: number): Promise<EndpointLogs> {
  const body: Record<string, unknown> = { id }
  if (tail !== undefined) body['tail'] = tail
  return client.autoscalerPost<EndpointLogs>('/get_autojob_logs/', body)
}

/** Roll every worker in a workergroup onto the current template. */
export function updateWorkers(client: VastClient, workergroupId: number, cancel = false): Promise<unknown> {
  const body: Record<string, unknown> = { workergroup_id: workergroupId }
  if (cancel) body['cancel_update'] = true
  return client.autoscalerPost('/update_workers/', body)
}
