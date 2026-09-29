// Shapes returned by webapp/server.py. Mirrors Job.snapshot(), /api/options and
// the rows written to output/web/index.jsonl, so this file is the one place to
// change when the Python side grows a field.

/** One arm of the A/B catalogue, as defined in scripts/ab_modelo.py. */
export interface ArmConfig {
  family: string
  sampler: string
  steps: number
  cfg: number
  /** SDXL-only: absent on the Anima arms. */
  lora?: number
  /** beta57 arms carry a schedule pair instead of a named scheduler. */
  scheduler?: string
  beta57?: readonly number[]
}

export interface DetailDefaults {
  prompt?: string
  negative?: string
}

export interface Options {
  arms: Record<string, ArmConfig>
  anima_model: string
  remove_bg: string[]
  detail?: DetailDefaults
  init?: InitLimits
  /** Anima pose control; absent on servers without it. */
  pose?: PoseOptions
  /** Commit the API process started from; null or absent on older servers. */
  version?: string | null
}

/** Skeleton styles and defaults for Anima pose control. */
export interface PoseOptions {
  styles: string[]
  style_default: string
  strength_default: number
}

/** 17 COCO body joints in scene pixels, [x, y, score]; score 0 = not drawn. */
export interface PoseSpec {
  points: [number, number, number][]
}

/** img2img limits, served so the page enforces the API's numbers, not a copy. */
export interface InitLimits {
  max_upload_mb: number
  formats: string[]
  denoise_default: number
}

/** What POST /api/inputs hands back for an uploaded image. */
export interface UploadedInput {
  id: string
  width: number
  height: number
  bytes: number
  /** The SDXL-native box this renders at when the size fields are blank. */
  fit: { width: number; height: number }
}

/** Parameters echoed back with a job; every one may be missing on old rows. */
export interface JobParams {
  prompt?: string
  negative?: string | null
  seed?: number
  width?: number
  height?: number
  batch?: number
  steps?: number
  cfg?: number
  family?: string
  lora?: number | null
  no_face?: boolean
  detail_prompt?: string | null
  detail_negative?: string | null
  no_upscale?: boolean
  remove_bg?: string | null
}

export interface LogEntry {
  t: number
  detail: string
}

// 'cancelled': the server stopped waiting for the endpoint. The worker may
// still be finishing that render; its result is discarded either way.
export type JobState = 'running' | 'done' | 'error' | 'cancelled'

export interface Job {
  id: string
  state: JobState
  phase: string
  detail: string
  worker: string | null
  images: string[]
  error: string | null
  latency: number | null
  elapsed: number
  log: LogEntry[]
  params: JobParams
  kind: string
  label: string | null
  pair: string | null
  side: string | null
  created: number
}

export interface HistoryResponse {
  items: Job[]
}

/** GET /api/jobs — the 50 most recent jobs the server still holds in memory. */
export interface JobsResponse {
  jobs: Job[]
}

export interface Worker {
  id?: number | string
  gpu?: string
  status?: string
  dph?: number
  hours?: number
  spent?: number
  machine?: number | string
  /**
   * Host telemetry relayed by Vast, in percent, or null when the machine does
   * not report it. Sampled on Vast's cadence, not ours: a short render can
   * begin and end between two samples, so 0 % does not prove the card is idle.
   */
  gpu_util?: number | null
  /** Celsius. The one number that lags least when the card is working. */
  gpu_temp?: number | null
  /** VRAM in GB: in use, and the card's total. */
  vram?: number | null
  vram_total?: number | null
  /**
   * Seconds the worker has counted a request against itself while the GPU sat
   * idle. Present only once that has held long enough to not be Vast's polling
   * lag: a wedged slot, not a render.
   */
  stalled?: number | null
  /** host:port the endpoint router hands clients for this worker. */
  address?: string | null
  /**
   * Whether that address accepts a TCP connection from the server. False is
   * terminal: Vast will keep routing requests the client cannot deliver.
   */
  reachable?: boolean | null
}

export interface StatusResponse {
  worker: Worker | null
}

export interface SceneRequest {
  prompt: string
  negative: string | null
  seed: number | null
  width: number | null
  height: number | null
  batch: number | null
  steps: number | null
  cfg: number | null
  family: string
  /** Id from POST /api/inputs; null renders from noise. */
  init_image: string | null
  denoise: number
  lora: number | null
  no_face: boolean
  detail_prompt: string | null
  detail_negative: string | null
  no_upscale: boolean
  remove_bg: string | null
  bg_refine: boolean
  bg_sensitivity: number
  bg_blur: number
  bg_offset: number
  cost: number
  timeout: number
  /** Inpaint: another /api/inputs id, white = repaint, same size as init_image. */
  mask_image?: string | null
  /** Bounding box of the painted pixels, in the init image's coordinates. */
  mask_box?: MaskBox | null
  /** Context kept around the box, in source px. */
  mask_pad?: number
  /** Feather of the paste seam, 0-31 px. */
  mask_blur?: number
  /** Anima only: skeleton the render follows. */
  pose?: PoseSpec | null
  /** Control strength, 0-2. */
  pose_strength?: number | null
  /** How the skeleton is drawn for the control image. */
  pose_style?: string | null
}

export interface MaskBox {
  x: number
  y: number
  width: number
  height: number
}

export interface CompareRequest {
  prompt: string
  negative: string | null
  arm_a: string
  arm_b: string
  seed: number | null
  width: number | null
  height: number | null
  steps: number | null
  cfg: number | null
  anima_model: string | null
}

export interface CompareResponse {
  jobs: string[]
  arms: string[]
  seed: number
}

export interface JobCreated {
  job_id: string
}
