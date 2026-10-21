// Typed client for clipbot's /api (see clipbot/app/dashboard.py).

export type JobStatus =
  | "queued" | "downloading" | "submitting" | "processing" | "done" | "failed" | "cancelled"
export type ScanStatus = "queued" | "running" | "done" | "failed"
export type ClipStatus = "pending" | "approved" | "rejected"
export type Platform = "twitch" | "youtube" | "kick" | "other"

export interface TimeRange { start: number; end: number }

export interface Job {
  id: string
  status: JobStatus
  source_url: string
  platform: Platform
  range: TimeRange | null
  target_clips: number
  error: string | null
  created: number
  updated: number
  retry_of: string | null
  retried_by: string | null
  openshorts_job: string | null
  clip_count: number
  template: string | null
  last_log?: string | null
}

export interface LogItem {
  id: number
  ts: number
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"
  source: string
  message: string
  job_id: string | null
  scan_id: string | null
  detail: string | null
}

export interface JobFiles {
  openshorts: number
  rejected: number
  pending: number
  approved: number
  freeable: number
  deleting: boolean
}

export interface JobDetail extends Job {
  clips: Clip[]
  logs: LogItem[]
  openshorts: { status: string | null; logs: string[] } | null
  files: JobFiles
}

export interface Clip {
  id: string
  job_id: string
  index: number
  status: ClipStatus
  title: string | null
  hook: string | null
  duration: number | null
  decided: number | null
  description_tiktok: string | null
  description_instagram: string | null
  source_url: string | null
  range: TimeRange | null
  created: number | null
  file: string
  video_url: string
  thumb_url: string | null
  template: string | null
  template_name: string | null
  sfx: HandEffect[]
  render_state: "rendering" | "failed" | null
  render_error: string | null
  can_render: boolean
  credit: string | null
  word_count: number
  download_url: string
  post_text: { youtube_title: string; youtube: string; tiktok: string; instagram: string }
  edited: boolean
  /** The hook line drawn on the video now ("" = none). null: OpenShorts drew it, so it can't change. */
  hook_rendered: string | null
  /** Whether a render draws the hook (the clip's own switch, else the template's and AUTO_HOOK). */
  hook_on: boolean
}

export interface Word { w: string; s: number; e: number }
export interface Zoom { s: number; e: number; scale: number }
/** This clip's own hook settings; a missing key means the template's. e null: to the end. */
export interface ClipHook { enabled?: boolean; s?: number; e?: number | null; position?: CaptionPosition }
export interface ClipEdit { segments: [number, number][]; zooms: Zoom[]; hook?: ClipHook }

export interface EditorData {
  clip: Clip
  source_url: string
  peaks_url: string
  source_duration: number
  words: Word[]
  words_original: Word[] | null
  edit: ClipEdit
  sfx: HandEffect[]
  template: string | null
  openshorts: { available: boolean; source_kept: boolean; framing: string | null; reason: string | null }
}

export interface StreamerVod {
  vod_id: string
  url: string
  title: string | null
  duration: number | null
  published: number | null
  seen: number
  live: boolean
  scan: { id: string; status: ScanStatus; moment_count: number; error: string | null } | null
}

export interface Streamer {
  id: string
  platform: "twitch" | "youtube" | "kick"
  channel: string
  label: string
  url: string
  auto_scan: boolean
  auto_clip: number
  template: string | null
  template_name: string | null
  checked: number | null
  check_error: string | null
  created: number
  vods: StreamerVod[]
}

export interface HandEffect { sound: string; t: number; volume?: number }

export type Trigger = "start" | "end" | "peak" | "word" | "moment"
export type MomentKind = "punchline" | "shock" | "fail" | "awkward" | "hype" | "sneaky"
export type CaptionEffect = "none" | "pop" | "glow" | "box" | "highlight"
export type CaptionPosition = "bottom" | "middle" | "top"

export interface CaptionStyle {
  enabled: boolean
  font_name: string
  font_size: number
  font_color: string
  highlight_color: string
  border_color: string
  border_width: number
  effect: CaptionEffect
  uppercase: boolean
  reveal: boolean
  max_chars: number
  max_duration: number
  position: CaptionPosition
  base_opacity: number
  shadow: number
  bg_opacity: number
  bg_color: string
}

export type HookStyleName = "box" | "outline" | "plain"

export interface HookStyle {
  enabled: boolean
  style: HookStyleName
  font_name: string
  font_size: number
  text_color: string
  box_color: string
  outline_color: string
  position: CaptionPosition
  duration: number      // seconds from the start; 0 = the whole clip
  uppercase: boolean
}

export interface SfxRule {
  sounds: string[]      // one is picked per effect (never the same twice in a row)
  at: Trigger
  kinds: MomentKind[]   // for at = "moment"
  words: string[]       // for at = "word"
  volume: number
  max: number
}

export interface TemplateBody {
  name: string
  captions: CaptionStyle
  hook: HookStyle
  music: { sound: string | null; volume: number; duck: boolean; start: number }
  voice_volume: number
  sfx: SfxRule[]
  credit: string
}

export interface Template extends TemplateBody {
  id: string
  default: boolean
  missing_sounds: string[]
}

/** em: the font's em next to its ASS size (how libass sizes it); weight: its OS/2 weight. For previews
 * that match the burn. */
export interface FontItem { family: string; file: string; bundled: boolean; em: number; weight: number }
export interface SoundItem { name: string; file: string; size: number; duration: number | null }

export interface TemplatesResponse {
  items: Template[]
  default: string | null
  presets: Record<string, CaptionStyle>
  effects: CaptionEffect[]
  positions: CaptionPosition[]
  triggers: Trigger[]
  fonts: FontItem[]
  sounds: string[]
  kinds: Record<MomentKind, string>
  gemini: boolean
  hook_styles: HookStyleName[]
  hook_default: HookStyle
  auto_hook: boolean
}

export interface Scan {
  id: string
  status: ScanStatus
  source_url: string
  title: string | null
  duration: number | null
  error: string | null
  created: number
  updated: number
  signals: string[]
  platform: Platform
  vod_id: string | null
  moment_count: number
}

export interface MomentDetails {
  chat_ratio?: number | null
  loud?: boolean
  clips?: number
  clip_views?: number
  replayed?: boolean
  top_tokens?: [string, number][]
}

export interface Moment {
  id: string
  rank: number
  peak: number
  start: number
  end: number
  score: number
  details: MomentDetails
  link: string
  job: { id: string; status: JobStatus } | null
}

export interface Timeline {
  step: number
  duration: number
  series: Partial<Record<"score" | "chat" | "hype" | "audio" | "clips" | "replayed", number[]>>
  markers: { t: number; duration: number; views: number }[]
}

export interface ScanDetail extends Scan {
  moments: Moment[]
  timeline: Timeline | null
  logs: LogItem[]
}

export interface Problem {
  kind: "job" | "scan" | "event"
  id: string | number
  ts: number
  title: string
  detail: string | null
  source_url?: string
}

export interface Activity {
  ts: number
  kind: "job" | "scan" | "clip"
  id: string
  status: string
  text: string
  range?: TimeRange | null
}

export interface Overview {
  scheduled_posts: number
  working: Job[]
  queued_jobs: number
  scans: Scan[]
  pending_clips: number
  pending_preview: Clip[]
  problems: Problem[]
  errors_24h: number
  activity: Activity[]
  week: { approved: number; rejected: number; jobs: number; scans: number }
  health: {
    openshorts: boolean
    n8n: boolean
    telegram_waiting: number
    telegram_failed: number
    postiz: boolean | null
    janitor_stuck: boolean
    disk: { total: number; free: number }
  }
}

export interface StorageReport {
  disk: { total: number; free: number }
  folders: Record<"pending" | "clean" | "approved" | "rejected" | "previews" | "thumbs" | "tmp" | "sounds", number>
  openshorts: { work: number; sources: number }
  jobs: (Job & { files: JobFiles })[]
  janitor_stuck: boolean
}

export type PublishPlatform = "youtube" | "tiktok" | "instagram"
export type PostStatus = "scheduled" | "published" | "failed" | "cancelled"

export interface Channel {
  id: string
  name: string
  identifier: string
  platform: PublishPlatform
  picture: string | null
  disabled: boolean
}

export interface PostItem {
  id: string
  platform: PublishPlatform
  channel: string | null
  status: PostStatus
  publish_at: number
  url: string | null
  error: string | null
}

export interface PostGroup {
  clip: Clip
  status: PostStatus
  publish_at: number
  posts: PostItem[]
}

export interface PublishQueueItem {
  clip: Clip
  status: "waiting" | "failed"
  error: string | null
  attempts: number
  next_try: number
}

export interface Publishing {
  configured: boolean
  postiz_url: string | null
  slots: string
  timezone: string
  tiktok_mode: string
  channels?: Channel[]
  error?: string | null
  next_slot?: number
  upcoming?: PostGroup[]
  recent?: PostGroup[]
  queue?: PublishQueueItem[]
  unscheduled?: Clip[]
}

export interface OutboxItem {
  id: number
  event: string | null
  attempts: number
  next_try: number
  failed: boolean
  text: string
}

export interface N8nFailure {
  id: string
  workflow_id: string | null
  started: string | null
  stopped: string | null
  mode: string | null
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

function describe(detail: unknown, fallback: string): string {
  if (typeof detail === "string") return detail
  if (Array.isArray(detail) && detail.length && typeof detail[0]?.msg === "string") {
    return detail.map((d) => d.msg).join("; ")
  }
  return fallback
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "same-origin",
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
  })
  if (!res.ok) {
    let message = res.statusText || `HTTP ${res.status}`
    try {
      message = describe((await res.json()).detail, message)
    } catch {
      /* not JSON */
    }
    throw new ApiError(res.status, message)
  }
  return res.json() as Promise<T>
}

/** PUT a file as the raw request body (sounds, fonts). */
async function upload<T>(path: string, file: File): Promise<T> {
  return request<T>(path, { method: "PUT", body: file, headers: { "Content-Type": file.type || "application/octet-stream" } })
}

const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) })

function query(params: Record<string, string | number | null | undefined>): string {
  const q = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== "") q.set(k, String(v))
  const s = q.toString()
  return s ? `?${s}` : ""
}

export type LogLevel = "error" | "warning" | "info"

/** Zip of every clip with this status (or just these ids), approved ones with their .json. */
export const clipsZipUrl = (status: ClipStatus, ids?: string[]) =>
  `/api/clips/download.zip?status=${status}${ids?.length ? `&ids=${ids.join(",")}` : ""}`

export const api = {
  session: () => request<{ configured: boolean; authenticated: boolean }>("/api/session"),
  login: (password: string) => post<{ authenticated: boolean }>("/api/login", { password }),
  logout: () => post<{ authenticated: boolean }>("/api/logout"),

  overview: () => request<Overview>("/api/overview"),

  clips: (status: ClipStatus) => request<{ items: Clip[] }>(`/api/clips${query({ status })}`),
  editClip: (id: string, body: Partial<Pick<Clip, "title" | "hook" | "description_tiktok" | "description_instagram">>) =>
    request<Clip>(`/api/clips/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  decide: (id: string, decision: "approve" | "reject") =>
    post<{ result: string; message: string | null; clip: Clip }>(`/api/clips/${id}/decision`, { decision }),

  jobs: (status?: string) => request<{ items: Job[] }>(`/api/jobs${query({ status, limit: 200 })}`),
  job: (id: string) => request<JobDetail>(`/api/jobs/${id}`),
  createJob: (body: { url: string; start?: string; end?: string; clips?: number; template?: string }) => post<Job>("/api/jobs", body),
  retryJob: (id: string) => post<Job>(`/api/jobs/${id}/retry`),
  cancelJob: (id: string) => post<Job>(`/api/jobs/${id}/cancel`),
  deleteJobFiles: (id: string) => post<{ freed: number }>(`/api/jobs/${id}/delete-files`),

  storage: () => request<StorageReport>("/api/storage"),
  cleanup: () => post<{ jobs: number; freed: number }>("/api/storage/cleanup"),

  templates: () => request<TemplatesResponse>("/api/templates"),
  createTemplate: (body: TemplateBody) => post<Template>("/api/templates", body),
  updateTemplate: (id: string, body: TemplateBody) =>
    request<Template>(`/api/templates/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteTemplate: (id: string) => request<{ ok: boolean }>(`/api/templates/${id}`, { method: "DELETE" }),
  defaultTemplate: (id: string) => post<{ default: string }>(`/api/templates/${id}/default`),
  sounds: () => request<{ items: SoundItem[] }>("/api/sounds"),
  uploadSound: (file: File) => upload<SoundItem>(`/api/sounds/${encodeURIComponent(file.name)}`, file),
  deleteSound: (name: string) => request<{ ok: boolean }>(`/api/sounds/${encodeURIComponent(name)}`, { method: "DELETE" }),
  uploadFont: (file: File) => upload<FontItem>(`/api/fonts/${encodeURIComponent(file.name)}`, file),
  deleteFont: (file: string) => request<{ ok: boolean }>(`/api/fonts/${encodeURIComponent(file)}`, { method: "DELETE" }),
  renderClip: (id: string, body: { template?: string; sfx?: HandEffect[] }) => post<Clip>(`/api/clips/${id}/render`, body),

  streamers: () => request<{ items: Streamer[]; interval_minutes: number }>("/api/streamers"),
  addStreamer: (body: { url: string; auto_scan: boolean; auto_clip: number; template?: string | null; backfill: number }) =>
    post<Streamer>("/api/streamers", body),
  editStreamer: (id: string, body: { auto_scan?: boolean; auto_clip?: number; template?: string }) =>
    request<Streamer>(`/api/streamers/${id}`, { method: "PATCH", body: JSON.stringify(body) }),
  removeStreamer: (id: string) => request<{ ok: boolean }>(`/api/streamers/${id}`, { method: "DELETE" }),
  refreshStreamer: (id: string) => post<Streamer>(`/api/streamers/${id}/refresh`),
  backfillStreamer: (id: string, count: number) =>
    post<{ queued: string[]; streamer: Streamer }>(`/api/streamers/${id}/backfill`, { count }),
  scanStreamerVod: (id: string, vodId: string) => post<Scan>(`/api/streamers/${id}/scan`, { vod_id: vodId }),

  editor: (clipId: string) => request<EditorData>(`/api/clips/${clipId}/editor`),
  peaks: (url: string) => request<{ per_second: number; peaks: number[] }>(url),
  saveEdit: (clipId: string, body: { template?: string; sfx?: HandEffect[]; edit?: ClipEdit; words?: Word[]; hook_text?: string }) =>
    post<Clip>(`/api/clips/${clipId}/edit`, body),
  suggestHooks: (clipId: string) => post<{ suggestions: string[] }>(`/api/clips/${clipId}/hook-suggestions`),
  recut: (clipId: string, body: { extend_start: number; extend_end: number; framing?: string | null }) =>
    post<Clip>(`/api/clips/${clipId}/recut`, body),

  publishing: () => request<Publishing>("/api/publishing"),
  publish: (clipId: string, body: { at?: number | null; now?: boolean }) =>
    post<{ queued: boolean }>(`/api/clips/${clipId}/publish`, body),
  unschedule: (clipId: string) => post<{ cancelled: number }>(`/api/clips/${clipId}/unschedule`),

  scans: () => request<{ items: Scan[] }>("/api/scans"),
  scan: (id: string) => request<ScanDetail>(`/api/scans/${id}`),
  createScan: (url: string) => post<Scan>("/api/scans", { url }),
  rescan: (id: string) => post<Scan>(`/api/scans/${id}/rescan`),
  clipFromScan: (id: string, body: { start: number | string; end: number | string; clips: number; moment_id?: string }) =>
    post<Job>(`/api/scans/${id}/clip`, body),

  logs: (params: { level?: LogLevel; source?: string; q?: string; before?: number | null }) =>
    request<{ items: LogItem[]; sources: string[]; next_before: number | null }>(`/api/logs${query({ ...params, limit: 150 })}`),
  outbox: () => request<{ items: OutboxItem[] }>("/api/outbox"),
  retryEvent: (id: number) => post<{ ok: boolean }>(`/api/outbox/${id}/retry`),
  deleteEvent: (id: number) => request<{ ok: boolean }>(`/api/outbox/${id}`, { method: "DELETE" }),
  n8nFailures: () => request<{ configured: boolean; items: N8nFailure[]; error?: string }>("/api/n8n/failures"),
}
