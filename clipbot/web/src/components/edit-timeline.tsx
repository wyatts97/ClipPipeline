import { cn } from "cn"
import { useLayoutEffect, useMemo, useRef, useState } from "react"

import type { ClipHook, Freeze, HandEffect, Overlay, Transition, Word, Zoom } from "@/lib/api"
import { type HookWindow, MIN_SEGMENT, type Segment } from "@/lib/edit"

/** A cut's index is the kept part before it. */
export type Selection = { kind: "segment" | "sfx" | "zoom" | "freeze" | "cut" | "picture"; index: number } | null

/** Short names for the transition markers on the timeline. */
export const TRANSITION_SHORT: Record<Transition["type"], string> = {
  fade: "fade", white: "white", black: "black", slideleft: "slide ←", slideright: "slide →", slideup: "slide ↑",
  slidedown: "slide ↓", wipe: "wipe", zoomin: "zoom", circle: "circle", pixelize: "pixels", blur: "blur",
  flash: "flash", glitch: "glitch", shake: "shake", punch: "punch",
}

const RULER = 20
const CLIP = 64
const TRACK = 28
const GAP = 6
const LANES = {
  clip: RULER, words: RULER + CLIP + GAP, sfx: RULER + CLIP + GAP * 2 + TRACK, zoom: RULER + CLIP + GAP * 3 + TRACK * 2,
  picture: RULER + CLIP + GAP * 4 + TRACK * 3, hook: RULER + CLIP + GAP * 5 + TRACK * 4,
}
const HEIGHT = LANES.hook + TRACK + 4

type Drag =
  | { kind: "seek" }
  | { kind: "segment"; index: number; edge: "start" | "end" }
  | { kind: "sfx"; index: number; offset: number }
  | { kind: "zoom"; index: number; edge: "start" | "end" | "move"; offset: number }
  | { kind: "hook"; edge: "start" | "end" | "move"; offset: number }
  | { kind: "freeze"; index: number }
  | { kind: "picture"; index: number; edge: "start" | "end" | "move"; offset: number }

/** The editor's timeline, in source seconds. Changes during a drag go to onLive; the final value
 * goes to onCommit (for undo). */
export function EditTimeline(props: {
  duration: number
  time: number
  segments: Segment[]
  words: Word[]
  sfx: HandEffect[]
  zooms: Zoom[]
  freezes: Freeze[]
  transitions: Transition[]
  overlays: Overlay[]
  /** The hook's window (null: this clip's hook can't change) and the clip's own hook settings. */
  hook: { window: HookWindow; own: ClipHook; text: string } | null
  peaks: number[]
  perSecond: number
  selection: Selection
  scale: number
  onSeek: (t: number) => void
  onSelect: (s: Selection) => void
  onLive: (patch: { segments?: Segment[]; sfx?: HandEffect[]; zooms?: Zoom[]; hook?: ClipHook; freezes?: Freeze[]; overlays?: Overlay[] }) => void
  onCommit: () => void
}) {
  const { duration, segments, sfx, zooms, scale, hook } = props
  const wrap = useRef<HTMLDivElement>(null)
  const svg = useRef<SVGSVGElement>(null)
  const drag = useRef<Drag | null>(null)
  const [width, setWidth] = useState(0)

  useLayoutEffect(() => {
    const el = wrap.current
    if (!el) return
    const ro = new ResizeObserver(() => setWidth(el.clientWidth))
    ro.observe(el)
    setWidth(el.clientWidth)
    return () => ro.disconnect()
  }, [])

  const total = Math.max(1, width * scale)
  const pps = total / Math.max(0.1, duration)
  const x = (t: number) => t * pps
  const timeAt = (clientX: number) => {
    const box = svg.current?.getBoundingClientRect()
    return box ? Math.min(duration, Math.max(0, (clientX - box.left) / pps)) : 0
  }

  // Keep the playhead in view while playing on a zoomed-in timeline.
  useLayoutEffect(() => {
    const el = wrap.current
    if (!el || scale <= 1) return
    const px = x(props.time)
    if (px < el.scrollLeft + 20 || px > el.scrollLeft + el.clientWidth - 20) el.scrollLeft = px - el.clientWidth / 3
  })

  function start(e: React.PointerEvent, d: Drag) {
    e.stopPropagation()
    drag.current = d
    svg.current?.setPointerCapture(e.pointerId)
    if (d.kind === "seek") props.onSeek(timeAt(e.clientX))
  }

  function move(e: React.PointerEvent) {
    const d = drag.current
    if (!d) return
    const t = timeAt(e.clientX)
    if (d.kind === "seek") props.onSeek(t)
    else if (d.kind === "segment") {
      const segs = segments.map((s) => [...s] as Segment)
      const [s, en] = segs[d.index]
      const prev = d.index > 0 ? segs[d.index - 1][1] : 0
      const next = d.index < segs.length - 1 ? segs[d.index + 1][0] : duration
      if (d.edge === "start") segs[d.index][0] = Math.min(en - MIN_SEGMENT, Math.max(prev, t))
      else segs[d.index][1] = Math.max(s + MIN_SEGMENT, Math.min(next, t))
      props.onLive({ segments: segs })
      props.onSeek(d.edge === "start" ? segs[d.index][0] : Math.max(0, segs[d.index][1] - 0.05))
    } else if (d.kind === "sfx") {
      props.onLive({ sfx: sfx.map((x, i) => (i === d.index ? { ...x, t: +Math.max(0, t - d.offset).toFixed(2) } : x)) })
    } else if (d.kind === "picture") {
      const o = props.overlays[d.index]
      let next: Overlay
      if (d.edge === "move") {
        const len = o.e - o.s
        const s = Math.min(duration - len, Math.max(0, t - d.offset))
        next = { ...o, s: +s.toFixed(2), e: +(s + len).toFixed(2) }
      } else if (d.edge === "start") next = { ...o, s: +Math.min(o.e - 0.2, t).toFixed(2) }
      else next = { ...o, e: +Math.max(o.s + 0.2, t).toFixed(2) }
      props.onLive({ overlays: props.overlays.map((x, i) => (i === d.index ? next : x)) })
    } else if (d.kind === "freeze") {
      // Freezes stay inside a kept part.
      const seg = segments.find(([s, e]) => t >= s && t < e)
      if (seg) props.onLive({ freezes: props.freezes.map((f, i) => (i === d.index ? { ...f, t: +t.toFixed(2) } : f)) })
    } else if (d.kind === "hook") {
      if (!hook) return
      const { s, e } = hook.window
      let ns = s
      let ne = e
      if (d.edge === "move") {
        ns = Math.min(duration - (e - s), Math.max(0, t - d.offset))
        ne = ns + (e - s)
      } else if (d.edge === "start") ns = Math.min(e - MIN_SEGMENT, Math.max(0, t))
      else ne = Math.max(s + MIN_SEGMENT, Math.min(duration, t))
      // Dragged to the very end: "until the end", so it stays there after a trim.
      props.onLive({ hook: { ...hook.own, s: +ns.toFixed(2), e: ne >= duration - 0.05 ? null : +ne.toFixed(2) } })
    } else {
      const z = zooms[d.index]
      let next: Zoom
      if (d.edge === "move") {
        const len = z.e - z.s
        const s = Math.min(duration - len, Math.max(0, t - d.offset))
        next = { ...z, s: +s.toFixed(2), e: +(s + len).toFixed(2) }
      } else if (d.edge === "start") next = { ...z, s: +Math.min(z.e - 0.1, t).toFixed(2) }
      else next = { ...z, e: +Math.max(z.s + 0.1, t).toFixed(2) }
      props.onLive({ zooms: zooms.map((x, i) => (i === d.index ? next : x)) })
    }
  }

  function end() {
    const d = drag.current
    drag.current = null
    if (d && d.kind !== "seek") props.onCommit()
  }

  // The waveform is thousands of bars: draw it again only when the cut or the zoom changes.
  const { peaks, perSecond } = props
  const waveform = useMemo(() => {
    const bar = Math.max(1, pps / perSecond)
    return peaks.map((p, i) => {
      const t = i / perSecond
      const kept = segments.some(([s, e]) => t >= s && t < e)
      const h = Math.max(1, p * (CLIP - 10))
      return <rect key={i} x={t * pps} y={LANES.clip + CLIP / 2 - h / 2} width={bar} height={h}
        className={kept ? "fill-foreground/70" : "fill-muted-foreground/25"} />
    })
  }, [peaks, perSecond, pps, segments])

  const step = pps > 60 ? 1 : pps > 25 ? 2 : pps > 12 ? 5 : 10
  const ticks = Array.from({ length: Math.floor(duration / step) + 1 }, (_, i) => i * step)
  const sel = props.selection

  return (
    <div ref={wrap} className="overflow-x-auto rounded-md border border-border bg-background select-none">
      <svg ref={svg} width={total} height={HEIGHT} className="block touch-none"
        onPointerMove={move} onPointerUp={end} onPointerCancel={end}
        onPointerDown={(e) => { start(e, { kind: "seek" }); props.onSelect(null) }}>
        {/* ruler */}
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={RULER - 6} y2={HEIGHT} className="stroke-border" strokeWidth={1} />
            <text x={x(t) + 3} y={12} className="fill-muted-foreground text-[10px] tabular-nums">{t}s</text>
          </g>
        ))}

        {/* clip: cut stretches dimmed, kept ones bright, waveform across */}
        <rect x={0} y={LANES.clip} width={total} height={CLIP} className="fill-muted/40" />
        {segments.map(([s, e], i) => (
          <rect key={`seg-${i}`} x={x(s)} y={LANES.clip} width={Math.max(1, x(e) - x(s))} height={CLIP} rx={3}
            className={cn("cursor-pointer fill-primary/15 stroke-primary/60", sel?.kind === "segment" && sel.index === i && "fill-primary/30 stroke-primary")}
            strokeWidth={sel?.kind === "segment" && sel.index === i ? 2 : 1}
            onPointerDown={(ev) => { ev.stopPropagation(); props.onSelect({ kind: "segment", index: i }); props.onSeek(timeAt(ev.clientX)) }} />
        ))}
        <g className="pointer-events-none">{waveform}</g>
        {segments.map(([s, e], i) => (
          <g key={`handles-${i}`}>
            <rect x={x(s) - 4} y={LANES.clip} width={8} height={CLIP} className="cursor-ew-resize fill-transparent hover:fill-primary/40"
              onPointerDown={(ev) => start(ev, { kind: "segment", index: i, edge: "start" })} />
            <rect x={x(e) - 4} y={LANES.clip} width={8} height={CLIP} className="cursor-ew-resize fill-transparent hover:fill-primary/40"
              onPointerDown={(ev) => start(ev, { kind: "segment", index: i, edge: "end" })} />
          </g>
        ))}

        {/* cuts: where one kept part ends and the next starts, with its transition */}
        {segments.slice(0, -1).map(([, e], i) => {
          const next = segments[i + 1][0]
          const tr = props.transitions.find((x) => x.after === e)
          const selected = sel?.kind === "cut" && sel.index === i
          const cx = (x(e) + x(next)) / 2
          const label = tr ? TRANSITION_SHORT[tr.type] : "⧉"
          const w = tr ? Math.max(18, label.length * 6 + 8) : 16
          return (
            <g key={`cut-${i}`} className="cursor-pointer" aria-label={tr ? `Transition: ${tr.type}` : "Add a transition"}
              onPointerDown={(ev) => { ev.stopPropagation(); props.onSelect({ kind: "cut", index: i }) }}>
              <rect x={cx - w / 2} y={LANES.clip + CLIP - 18} width={w} height={15} rx={3}
                className={cn(tr ? "fill-chart-1" : "fill-background stroke-muted-foreground/60", selected && "stroke-foreground")}
                strokeWidth={selected ? 2 : 1} />
              <text x={cx} y={LANES.clip + CLIP - 7} textAnchor="middle"
                className={cn("pointer-events-none text-[9px]", tr ? "fill-background" : "fill-muted-foreground")}>{label}</text>
            </g>
          )
        })}

        {/* freezes: the held frame, and for how long */}
        {props.freezes.map((f, i) => {
          const selected = sel?.kind === "freeze" && sel.index === i
          return (
            <g key={`fr-${i}`} transform={`translate(${x(f.t)},${LANES.clip + 3})`} className="cursor-grab"
              onPointerDown={(ev) => { props.onSelect({ kind: "freeze", index: i }); start(ev, { kind: "freeze", index: i }) }}>
              <line x1={0} x2={0} y1={0} y2={CLIP - 6} className="stroke-chart-3" strokeWidth={2} />
              <rect x={-1} y={0} width={34} height={14} rx={3} className={cn("fill-chart-3", selected && "stroke-foreground")}
                strokeWidth={selected ? 2 : 0} />
              <text x={3} y={10.5} className="pointer-events-none fill-background text-[9px] font-medium">❄ {f.d}s</text>
            </g>
          )
        })}

        {/* captions */}
        {props.words.map((w, i) => (
          <g key={`w-${i}`} className="pointer-events-none">
            <rect x={x(w.s)} y={LANES.words} width={Math.max(2, x(w.e) - x(w.s))} height={TRACK - 4} rx={2}
              className={segments.some(([s, e]) => w.s >= s && w.s < e) ? "fill-chart-2/50" : "fill-muted-foreground/20"} />
            {x(w.e) - x(w.s) > 24 && (
              <text x={x(w.s) + 3} y={LANES.words + 16} className="fill-foreground text-[10px]">{w.w.slice(0, Math.floor((x(w.e) - x(w.s)) / 6))}</text>
            )}
          </g>
        ))}

        {/* effects */}
        {sfx.map((fx, i) => {
          const selected = sel?.kind === "sfx" && sel.index === i
          return (
            <g key={`fx-${i}`} transform={`translate(${x(fx.t)},${LANES.sfx + TRACK / 2 - 2})`} className="cursor-grab"
              onPointerDown={(ev) => { props.onSelect({ kind: "sfx", index: i }); start(ev, { kind: "sfx", index: i, offset: timeAt(ev.clientX) - fx.t }) }}>
              <rect x={-7} y={-7} width={14} height={14} transform="rotate(45)" rx={2}
                className={cn(selected ? "fill-primary stroke-foreground" : "fill-primary/70 stroke-background")} strokeWidth={1.5} />
              {pps * 1 > 40 && <text x={10} y={4} className="pointer-events-none fill-muted-foreground text-[10px]">{fx.sound}</text>}
            </g>
          )
        })}

        {/* zooms */}
        {zooms.map((z, i) => {
          const selected = sel?.kind === "zoom" && sel.index === i
          return (
            <g key={`z-${i}`}>
              <rect x={x(z.s)} y={LANES.zoom} width={Math.max(4, x(z.e) - x(z.s))} height={TRACK - 4} rx={3}
                className={cn("cursor-grab fill-chart-4/40 stroke-chart-4", selected && "fill-chart-4/70")} strokeWidth={selected ? 2 : 1}
                onPointerDown={(ev) => { props.onSelect({ kind: "zoom", index: i }); start(ev, { kind: "zoom", index: i, edge: "move", offset: timeAt(ev.clientX) - z.s }) }} />
              <text x={x(z.s) + 4} y={LANES.zoom + 16} className="pointer-events-none fill-foreground text-[10px]">{z.scale.toFixed(2)}×{z.style && z.style !== "punch" ? ` ${z.style}` : ""}</text>
              <rect x={x(z.s) - 3} y={LANES.zoom} width={6} height={TRACK - 4} className="cursor-ew-resize fill-transparent"
                onPointerDown={(ev) => start(ev, { kind: "zoom", index: i, edge: "start", offset: 0 })} />
              <rect x={x(z.e) - 3} y={LANES.zoom} width={6} height={TRACK - 4} className="cursor-ew-resize fill-transparent"
                onPointerDown={(ev) => start(ev, { kind: "zoom", index: i, edge: "end", offset: 0 })} />
            </g>
          )
        })}

        {/* pictures */}
        {props.overlays.map((o, i) => {
          const selected = sel?.kind === "picture" && sel.index === i
          const name = o.image.replace(/^sticker:/, "")
          return (
            <g key={`pic-${i}`}>
              <rect x={x(o.s)} y={LANES.picture} width={Math.max(4, x(o.e) - x(o.s))} height={TRACK - 4} rx={3}
                className={cn("cursor-grab fill-chart-2/35 stroke-chart-2", selected && "fill-chart-2/60")} strokeWidth={selected ? 2 : 1}
                onPointerDown={(ev) => { props.onSelect({ kind: "picture", index: i }); start(ev, { kind: "picture", index: i, edge: "move", offset: timeAt(ev.clientX) - o.s }) }} />
              <text x={x(o.s) + 4} y={LANES.picture + 16} className="pointer-events-none fill-foreground text-[10px]">
                {name.slice(0, Math.max(0, Math.floor((x(o.e) - x(o.s) - 8) / 6)))}
              </text>
              <rect x={x(o.s) - 3} y={LANES.picture} width={6} height={TRACK - 4} className="cursor-ew-resize fill-transparent"
                onPointerDown={(ev) => start(ev, { kind: "picture", index: i, edge: "start", offset: 0 })} />
              <rect x={x(o.e) - 3} y={LANES.picture} width={6} height={TRACK - 4} className="cursor-ew-resize fill-transparent"
                onPointerDown={(ev) => start(ev, { kind: "picture", index: i, edge: "end", offset: 0 })} />
            </g>
          )
        })}

        {/* hook */}
        {hook && (
          <g>
            <rect x={x(hook.window.s)} y={LANES.hook} width={Math.max(4, x(hook.window.e) - x(hook.window.s))} height={TRACK - 4} rx={3}
              className={cn("cursor-grab", hook.window.on ? "fill-chart-5/35 stroke-chart-5" : "fill-muted-foreground/10 stroke-muted-foreground/50")}
              strokeDasharray={hook.window.own ? undefined : "4 3"}
              onPointerDown={(ev) => start(ev, { kind: "hook", edge: "move", offset: timeAt(ev.clientX) - hook.window.s })} />
            <text x={x(hook.window.s) + 4} y={LANES.hook + 16} className="pointer-events-none fill-foreground text-[10px]">
              {(hook.window.on ? hook.text || "No hook text" : "Hook off").slice(0, Math.max(0, Math.floor((x(hook.window.e) - x(hook.window.s) - 8) / 6)))}
            </text>
            <rect x={x(hook.window.s) - 3} y={LANES.hook} width={6} height={TRACK - 4} className="cursor-ew-resize fill-transparent"
              onPointerDown={(ev) => start(ev, { kind: "hook", edge: "start", offset: 0 })} />
            <rect x={x(hook.window.e) - 3} y={LANES.hook} width={6} height={TRACK - 4} className="cursor-ew-resize fill-transparent"
              onPointerDown={(ev) => start(ev, { kind: "hook", edge: "end", offset: 0 })} />
          </g>
        )}

        {/* playhead */}
        <line x1={x(props.time)} x2={x(props.time)} y1={0} y2={HEIGHT} className="pointer-events-none stroke-primary" strokeWidth={2} />
        <path d={`M ${x(props.time) - 5} 0 L ${x(props.time) + 5} 0 L ${x(props.time)} 7 Z`} className="pointer-events-none fill-primary" />
      </svg>
    </div>
  )
}

export const TRACK_LABELS = [
  { label: "", height: RULER },
  { label: "Clip", height: CLIP + GAP },
  { label: "Captions", height: TRACK + GAP },
  { label: "Sounds", height: TRACK + GAP },
  { label: "Zooms", height: TRACK + GAP },
  { label: "Pictures", height: TRACK + GAP },
  { label: "Hook", height: TRACK + 4 },
]
