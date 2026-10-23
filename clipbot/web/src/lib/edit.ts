// The clip editor's time maps and caption grouping, mirroring clipbot/app/editing.py and
// captions.group_words so the live preview matches the render.
import type {
  AccentTransition, CaptionPosition, ClipEdit, Freeze, HookStyle, Overlay, SmoothTransition, Transition, Word, Zoom,
} from "./api"

export const MIN_SEGMENT = 0.3
export const FRAME = 1 / 30

export type Segment = [number, number]

export const SMOOTH_TRANSITIONS: SmoothTransition[] = [
  "fade", "white", "black", "slideleft", "slideright", "slideup", "slidedown", "wipe", "zoomin", "circle", "pixelize", "blur",
]
export const ACCENT_TRANSITIONS: AccentTransition[] = ["flash", "glitch", "shake", "punch"]
export const isSmooth = (tr: Transition | null | undefined): tr is Transition =>
  !!tr && (SMOOTH_TRANSITIONS as string[]).includes(tr.type)

/** A kept part on the finished clip, like editing.timeline: where it starts (o), how long it plays
 * (len: freezes add time), how much a smooth transition into it overlaps the part before (x). */
export interface Part { s: number; e: number; o: number; len: number; x: number; into: Transition | null; freezes: Freeze[] }

/** Bare segments (cuts only) or a whole edit. */
export type Timed = Segment[] | Pick<ClipEdit, "segments" | "freezes" | "transitions">

export function timeline(edit: Timed): Part[] {
  const e = Array.isArray(edit) ? { segments: edit } : edit
  const into = new Map((e.transitions ?? []).map((tr) => [tr.after, tr]))
  const out: Part[] = []
  let o = 0
  let prev: number | null = null
  for (const [s, end] of e.segments as Segment[]) {
    const tr = prev !== null ? into.get(prev) ?? null : null
    const x = isSmooth(tr) ? tr.d ?? 0.4 : 0
    o -= x
    const held = (e.freezes ?? []).filter((f) => f.t >= s && f.t < end)
    const len = end - s + held.reduce((n, f) => n + f.d, 0)
    out.push({ s, e: end, o, len, x, into: tr, freezes: held })
    o += len
    prev = end
  }
  return out
}

/** Source time t (inside the part) on the finished clip; at a freeze, where the freeze starts. */
const place = (p: Part, t: number) => p.o + (t - p.s) + p.freezes.reduce((n, f) => n + (f.t < t ? f.d : 0), 0)

/** Where source time t lands in the edited clip, or null when it was cut. */
export function mapTime(t: number, edit: Timed): number | null {
  const parts = timeline(edit)
  for (const p of parts) if ((t >= p.s && t < p.e) || (t === p.e && p === parts[parts.length - 1])) return place(p, t)
  return null
}

export function outputDuration(edit: Timed): number {
  const parts = timeline(edit)
  const last = parts[parts.length - 1]
  return last ? last.o + last.len : 0
}

/** The source time that plays at time t of the edited clip (mapTime the other way; a freeze plays its frame). */
export function sourceTime(t: number, edit: Timed): number {
  const parts = timeline(edit)
  for (const p of parts) {
    if (t >= p.o + p.len) continue
    let left = Math.max(0, t - p.o)
    let at = p.s
    for (const f of p.freezes) {
      if (left < f.t - at) return at + left
      left -= f.t - at
      at = f.t
      if (left < f.d) return f.t
      left -= f.d
    }
    return Math.min(p.e, at + left)
  }
  return parts[parts.length - 1]?.e ?? t
}

/** After the segments change: transitions follow their cut (by position when the number of cuts is the
 * same, else by time) and freezes outside every kept part go. */
export function reconcile(prev: Segment[], edit: ClipEdit): ClipEdit {
  const segs = edit.segments as Segment[]
  const ends = segs.slice(0, -1).map((s) => s[1])
  const oldEnds = prev.slice(0, -1).map((s) => s[1])
  const transitions = (edit.transitions ?? []).flatMap((tr) => {
    const i = oldEnds.indexOf(tr.after)
    if (i >= 0 && oldEnds.length === ends.length) return [{ ...tr, after: ends[i] }]
    const near = ends.find((e) => Math.abs(e - tr.after) < 0.05)
    return near !== undefined ? [{ ...tr, after: near }] : []
  })
  const freezes = (edit.freezes ?? []).filter((f) => segs.some(([s, e]) => f.t >= s && f.t < e))
  return { ...edit, transitions, freezes }
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
  const total = outputDuration(edit)
  const end = look.duration > 0 ? Math.min(total, look.duration) : total
  return { s: segments[0]?.[0] ?? 0, e: sourceTime(end, edit), on, own: false, position, outEnd: end }
}

/** Whether the hook is on screen at source time t. */
export function hookShows(w: HookWindow, t: number, edit: Timed): boolean {
  const out = mapTime(t, edit)
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
export function groupWords(words: Word[], maxChars: number, maxDuration: number, edit?: Timed): Block[] {
  const at = (t: number) => (edit ? mapTime(t, edit) ?? t : t)
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

// Zoom curves, the same as render.zoom_curve / zoom_filter.
const ZOOM_EASE = 0.12
const SHAKE_EASE = 0.08
const SMOOTH_EASE = 0.4
const SHAKE = 0.03
const ease = (v: number) => { const c = Math.min(1, Math.max(0, v)); return c * c * (3 - 2 * c) }

/** How far into its zoom (0-1) a zoom is at source time t, by style. */
export function zoomCurve(z: Zoom, t: number): number {
  const d = Math.max(z.e - z.s, 0.001)
  const u = t - z.s
  switch (z.style ?? "punch") {
    case "smooth": { const a = Math.min(SMOOTH_EASE, d / 3); return ease(u / a) * ease((z.e - t) / a) }
    case "push": return Math.min(1, Math.max(0, u / d))
    case "out": return 1 - ease(u / d)
    case "shake": return Math.min(1, u / SHAKE_EASE)
    default: return Math.min(1, u / ZOOM_EASE)
  }
}

/** scale, and the CSS transform-origin (0-1) that shows the same part of the frame as the render's crop. */
export interface ZoomView { scale: number; ox: number; oy: number }

/** The picture at source time t, like render.zoom_filter. aspect: the frame's width / height. */
export function zoomAt(edit: ClipEdit, t: number, aspect = 9 / 16): ZoomView {
  let scale = 1, fx = 0.5, fy = 0.5, sx = 0, sy = 0, aimed = false
  for (const z of edit.zooms) {
    if (t < z.s || t > z.e) continue
    scale += (z.scale - 1) * zoomCurve(z, t)
    if (!aimed) { fx = z.x ?? 0.5; fy = z.y ?? 0.5; aimed = true }     // the first zoom on screen picks the point
    if (z.style === "shake") {
      const u = t - z.s
      const fade = SHAKE * (1 - u / Math.max(z.e - z.s, 0.001))
      sx += fade * (Math.sin(2 * Math.PI * 9 * u) + 0.5 * Math.sin(2 * Math.PI * 23 * u)) / 1.5
      sy += fade * aspect * (Math.sin(2 * Math.PI * 7 * u + 1.3) + 0.5 * Math.sin(2 * Math.PI * 19 * u)) / 1.5
    }
  }
  if (scale <= 1.0005) return { scale: 1, ox: fx, oy: fy }
  // The render crops the scaled frame at fx*scale - 1/2 (+ shake), kept inside it; scaling about origin o
  // shows that same window when o = crop / (scale - 1).
  const crop = (f: number, shake: number) => Math.min(scale - 1, Math.max(0, f * scale - 0.5 + shake)) / (scale - 1)
  return { scale, ox: crop(fx, sx), oy: crop(fy, sy) }
}

/** The frame point (0-1) under a point of the zoomed preview (0-1), and back. */
export const framePoint = (v: ZoomView, px: number, py: number): [number, number] =>
  [v.ox + (px - v.ox) / v.scale, v.oy + (py - v.oy) / v.scale]
export const screenPoint = (v: ZoomView, fx: number, fy: number): [number, number] =>
  [v.ox + (fx - v.ox) * v.scale, v.oy + (fy - v.oy) * v.scale]

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

/** How the preview hints at a transition around source time t: the render blends the two sides, the
 * preview plays one video, so it dips, slides, wipes or flashes it instead. */
export interface Look { overlay?: string; overlayOpacity?: number; translate?: [number, number]; filter?: string; clipPath?: string; scale?: number }

export function transitionLook(parts: Part[], t: number): Look {
  for (let i = 1; i < parts.length; i++) {
    const tr = parts[i].into
    if (!tr) continue
    const smooth = isSmooth(tr)
    const half = smooth ? (tr.d ?? 0.4) / 2 : 0.3
    const end = parts[i - 1].e
    const start = parts[i].s
    const leaving = smooth && t >= end - half && t < end ? (t - end + half) / half : null
    const arriving = t >= start && t < start + half ? 1 - (t - start) / half : null
    const k = leaving ?? arriving          // 1 at the cut, 0 away from it
    if (k === null) continue
    const side = leaving !== null ? 1 : -1
    switch (tr.type) {
      case "fade": return { overlay: "#000", overlayOpacity: 0.6 * k }
      case "black": return { overlay: "#000", overlayOpacity: k }
      case "white": return { overlay: "#fff", overlayOpacity: k }
      case "slideleft": return { translate: [-side * k * 100, 0] }
      case "slideright": return { translate: [side * k * 100, 0] }
      case "slideup": return { translate: [0, -side * k * 100] }
      case "slidedown": return { translate: [0, side * k * 100] }
      case "wipe": return arriving !== null ? { clipPath: `inset(0 ${k * 100}% 0 0)` } : {}
      case "circle": return arriving !== null ? { clipPath: `circle(${(1 - k) * 75}% at 50% 50%)` } : { overlay: "#000", overlayOpacity: 0.5 * k }
      case "zoomin": return leaving !== null ? { scale: 1 + 0.6 * k, overlay: "#000", overlayOpacity: 0.3 * k } : {}
      case "pixelize": case "blur": return { filter: `blur(${12 * k}px)` }
      case "flash": return { overlay: "#fff", overlayOpacity: 0.85 * k }
      case "glitch": return { filter: `hue-rotate(${90 * k}deg) saturate(${1 + 2 * k})`, translate: [Math.sin(t * 90) * 4 * k, 0] }
      default: return {}                 // shake and punch are zooms: accentZooms
    }
  }
  return {}
}

/** The shake and zoom-punch accents as zooms on source time, for the preview (like editing.accents). */
export function accentZooms(parts: Part[]): Zoom[] {
  return parts.flatMap((p): Zoom[] => p.into?.type === "shake" ? [{ s: p.s, e: p.s + 0.5, scale: 1.12, style: "shake" }]
    : p.into?.type === "punch" ? [{ s: p.s, e: p.s + 0.35, scale: 1.3, style: "out" }] : [])
}

// ---- pictures ---------------------------------------------------------------------------------------
const PICTURE_EASE = 0.3

/** Where a library picture or sticker is served from. */
export const pictureUrl = (ref: string) =>
  ref.startsWith("sticker:") ? `/api/stickers/${encodeURIComponent(ref.slice(8))}.png` : `/api/images/${encodeURIComponent(ref)}/file`

/** A picture at source time t, like render.overlay_graph: its size (1 = as placed), turn (degrees),
 * offset (dx, dy: shares of the frame) and opacity; null when it's not on screen. aspect: width / height. */
export function pictureLook(o: Overlay, t: number, aspect = 9 / 16) {
  if (t < o.s || t > o.e) return null
  const clip = (v: number) => Math.min(1, Math.max(0, v))
  const u = clip((t - o.s) / PICTURE_EASE)
  const v = clip((o.e - t) / PICTURE_EASE)
  const grow = (kind: string, p: number) =>
    kind === "pop" ? (p < 0.7 ? 1.2 * p / 0.7 : 1.2 - 0.2 * (p - 0.7) / 0.3) : kind === "zoom" || kind === "spin" ? ease(p) : 1
  const scale = grow(o.enter, u) * grow(o.exit, v) * (o.motion === "pulse" ? 1 + 0.08 * Math.sin(2 * Math.PI * 1.5 * t) : 1)
  let rot = o.rot
  if (o.motion === "spin") rot += 180 * (t - o.s)
  if (o.enter === "spin") rot -= 360 * (1 - ease(u))
  if (o.exit === "spin") rot += 360 * (1 - ease(v))
  let dx = 0
  let dy = 0
  const slide: Record<string, [number, number]> = { "slide-left": [-1, 0], "slide-right": [1, 0], "slide-up": [0, 1], "slide-down": [0, -1] }
  if (slide[o.enter]) { dx += slide[o.enter][0] * (1 - ease(u)); dy += slide[o.enter][1] * (1 - ease(u)) }
  if (slide[o.exit]) { dx += slide[o.exit][0] * (1 - ease(v)); dy -= slide[o.exit][1] * (1 - ease(v)) }
  if (o.motion === "float") dy += 0.015 * Math.sin(Math.PI * t)
  if (o.motion === "shake") {
    dx += 0.012 * Math.sin(2 * Math.PI * 13 * t)
    dy += 0.012 * aspect * Math.sin(2 * Math.PI * 11 * t + 1)
  }
  const opacity = o.opacity * (o.enter === "fade" ? u : 1) * (o.exit === "fade" ? v : 1)
  return { scale: Math.max(0.001, scale), rot, dx, dy, opacity }
}
