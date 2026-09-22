export interface VastInstance {
  id: number
  machine_id?: number
  label?: string | null
  actual_status?: string | null
  cur_state?: string | null
  status_msg?: string | null
  gpu_name?: string | null
  num_gpus?: number | null
  gpu_ram?: number | null
  dph_total?: number | null
  disk_space?: number | null
  disk_usage?: number | null
  image_uuid?: string | null
  start_date?: number | null
  duration?: number | null
  ssh_host?: string | null
  ssh_port?: number | null
  public_ipaddr?: string | null
  ports?: Record<string, { HostIp: string; HostPort: string }[]> | null
  [k: string]: unknown
}

export interface VastOffer {
  id: number
  machine_id?: number
  gpu_name?: string | null
  num_gpus?: number | null
  gpu_ram?: number | null
  dph_total?: number | null
  storage_cost?: number | null
  inet_down?: number | null
  inet_down_cost?: number | null
  dlperf?: number | null
  disk_space?: number | null
  reliability2?: number | null
  cuda_max_good?: number | null
  geolocation?: string | null
  verified?: boolean | null
  [k: string]: unknown
}

export interface VastEndpoint {
  id: number
  endpoint_name?: string | null
  endpoint_state?: string | null
  min_load?: number | null
  min_cold_load?: number | null
  target_util?: number | null
  cold_mult?: number | null
  cold_workers?: number | null
  max_workers?: number | null
  max_queue_time?: number | null
  target_queue_time?: number | null
  inactivity_timeout?: number | null
  created_at?: number | null
  [k: string]: unknown
}

export interface VastWorkergroup {
  id: number
  endpoint_id?: number | null
  endpoint_name?: string | null
  template_id?: number | null
  template_hash?: string | null
  min_load?: number | null
  target_util?: number | null
  cold_mult?: number | null
  cold_workers?: number | null
  test_workers?: number | null
  gpu_ram?: number | null
  launch_args?: string | null
  search_params?: string | null
  search_query?: Record<string, unknown> | null
  [k: string]: unknown
}

export interface VastWorker {
  id: number
  status?: string | null
  gpu_name?: string | null
  cur_load?: number | null
  measured_perf?: number | null
  perf?: number | null
  ready_ever?: boolean | null
  loaded_at?: number | null
  available?: boolean | null
  disk_usage?: number | null
  dlperf?: number | null
  num_gpus?: number | null
  reliability?: number | null
  [k: string]: unknown
}

/** Every autoscaler knob the CLI is allowed to write. */
export interface AutoscalerKnobs {
  min_load?: number
  min_cold_load?: number
  target_util?: number
  cold_mult?: number
  cold_workers?: number
  max_workers?: number
  max_queue_time?: number
  target_queue_time?: number
  inactivity_timeout?: number
}
