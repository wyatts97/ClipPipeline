// The clip editor's time maps and caption grouping, mirroring clipbot/app/editing.py and
// captions.group_words so the live preview matches the render.
import type { CaptionPosition, ClipEdit, HookStyle, Word } from "./api"

export const MIN_SEGMENT = 0.3
export const FRAME = 1 / 30

export type Segment = [number, number]

/** Where source time t lands in the edited clip, or null when it was cut. */
export function mapTime(t: number, segments: Segment[]): number | null {
  let offset = 0
  for (const [s, e] of segments) {
    if (t >= s && t < e) return offset + t - s
    offset += e - s
  }
  const last = segments[segments.length - 1]
  return last && t === last[1] ? offset : null
}

export function outputDuration(segments: Segment[]): number {
  return segments.reduce((n, [s, e]) => n + e - s, 0)
}

/** The source time that plays at time t of the edited clip (mapTime the other way). */
export function sourceTime(t: number, segments: Segment[]): number {
  let offset = 0
  for (const [s, e] of segments) {
    if (t < offset + e - s) return s + t - offset
    offset += e - s
  }
  return segments[segments.length - 1]?.[1] ?? t
}

export interface HookWindow {
  s: number            // source seconds
  e: number
  on: boolean
  own: boolean         // the clip's own timing (else the template's)
  position: CaptionPosition
  outEnd: number | null   // template timing: where it stops on the edited clip
}

/** When and where the hook shows, like pipeline.hook_plan and editing.hook_window. */
export function hookWindow(edit: ClipEdit, look: HookStyle, autoHook: boolean, duration: number): HookWindow {
  const own = edit.hook ?? {}
  const segments = edit.segments as Segment[]
  const on = own.enabled ?? (look.enabled && autoHook)
  const position = own.position ?? look.position
  if (own.s !== undefined) return { s: own.s, e: own.e ?? duration, on, own: true, position, outEnd: null }
  const total = outputDuration(segments)
  const end = look.duration > 0 ? Math.min(total, look.duration) : total
  return { s: segments[0]?.[0] ?? 0, e: sourceTime(end, segments), on, own: false, position, outEnd: end }
}

/** Whether the hook is on screen at source time t. */
export function hookShows(w: HookWindow, t: number, segments: Segment[]): boolean {
  const out = mapTime(t, segments)
  if (!w.on || out === null) return false
  return w.outEnd !== null ? out < w.outEnd : t >= w.s && t < w.e
}

/** The kept segment containing t, or the next one after it (null past the end). */
export function nextKept(t: number, segments: Segment[]): Segment | null {
  for (const seg of segments) if (t < seg[1]) return seg
  return null
}

export function segmentAt(t: number, segments: Segment[]): number {
  return segments.findIndex(([s, e]) => t >= s && t < e)
}

/** Words whose start was kept, with their source times (the preview plays the source). */
export function keptWords(words: Word[], segments: Segment[]): Word[] {
  return words.filter((w) => w.w.trim() && segmentAt(w.s, segments) >= 0)
}

export interface Block { words: Word[]; start: number; end: number }

/** Words into on-screen blocks: up to maxChars letters and maxDuration seconds each (output time). */
export function groupWords(words: Word[], maxChars: number, maxDuration: number, segments?: Segment[]): Block[] {
  const at = (t: number) => (segments ? mapTime(t, segments) ?? t : t)
  const blocks: Block[] = []
  let current: Word[] = []
  for (const w of words) {
    if (!current.length) { current = [w]; continue }
    const length = current.reduce((n, x) => n + x.w.length + 1, 0)
    if (length + w.w.length > maxChars || at(w.e) - at(current[0].s) > maxDuration) {
      blocks.push({ words: current, start: current[0].s, end: current[current.length - 1].e })
      current = [w]
    } else current.push(w)
  }
  if (current.length) blocks.push({ words: current, start: current[0].s, end: current[current.length - 1].e })
  return blocks
}

/** The caption line and active word at source time t (like the burned captions: a word stays lit
 * until the next one starts, the last until its block ends). */
export function captionAt(blocks: Block[], t: number): { line: string[]; active: number } {
  const block = blocks.find((b) => t >= b.start && t < b.end)
  if (!block) return { line: [], active: -1 }
  let active = 0
  block.words.forEach((w, i) => { if (t >= w.s) active = i })
  return { line: block.words.map((w) => w.w), active }
}

/** Zoom factor at source time t (quick ease in, like render.zoom_filter). */
export function zoomAt(edit: ClipEdit, t: number): number {
  let z = 1
  for (const zoom of edit.zooms) {
    if (t >= zoom.s && t <= zoom.e) z += (zoom.scale - 1) * Math.min(1, (t - zoom.s) / 0.12)
  }
  return z
}

/** Split the segment under t in two (no-op near an edge). */
export function split(segments: Segment[], t: number): Segment[] {
  const i = segmentAt(t, segments)
  if (i < 0) return segments
  const [s, e] = segments[i]
  if (t - s < MIN_SEGMENT || e - t < MIN_SEGMENT) return segments
  return [...segments.slice(0, i), [s, t], [t, e], ...segments.slice(i + 1)]
}

/** Spread edited line text back over the original word timings: same count keeps each word's time,
 * otherwise the line's span is shared out evenly. */
export function retime(original: Word[], text: string): Word[] {
  const parts = text.split(/\s+/).filter(Boolean)
  if (!original.length) return []
  if (parts.length === original.length) return original.map((w, i) => ({ ...w, w: parts[i] }))
  const start = original[0].s
  const end = original[original.length - 1].e
  const step = (end - start) / Math.max(1, parts.length)
  return parts.map((w, i) => ({ w, s: +(start + i * step).toFixed(3), e: +(start + (i + 1) * step - 0.02).toFixed(3) }))
}

export function fmt(t: number): string {
  const s = Math.max(0, t)
  const m = Math.floor(s / 60)
  return `${m}:${(s % 60).toFixed(1).padStart(4, "0")}`
}
