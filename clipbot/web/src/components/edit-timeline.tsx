import { cn } from "cn"
import { useLayoutEffect, useMemo, useRef, useState } from "react"

import type { HandEffect, Word, Zoom } from "@/lib/api"
import { MIN_SEGMENT, type Segment } from "@/lib/edit"

export type Selection = { kind: "segment" | "sfx" | "zoom"; index: number } | null

const RULER = 20
const CLIP = 64
const TRACK = 28
const GAP = 6
const LANES = { clip: RULER, words: RULER + CLIP + GAP, sfx: RULER + CLIP + GAP * 2 + TRACK, zoom: RULER + CLIP + GAP * 3 + TRACK * 2 }
const HEIGHT = LANES.zoom + TRACK + 4

type Drag =
  | { kind: "seek" }
  | { kind: "segment"; index: number; edge: "start" | "end" }
  | { kind: "sfx"; index: number; offset: number }
  | { kind: "zoom"; index: number; edge: "start" | "end" | "move"; offset: number }

/** The editor's timeline, in source seconds. Changes during a drag go to onLive; the final value
 * goes to onCommit (for undo). */
export function EditTimeline(props: {
  duration: number
  time: number
  segments: Segment[]
  words: Word[]
  sfx: HandEffect[]
  zooms: Zoom[]
  peaks: number[]
  perSecond: number
  selection: Selection
  scale: number
  onSeek: (t: number) => void
  onSelect: (s: Selection) => void
  onLive: (patch: { segments?: Segment[]; sfx?: HandEffect[]; zooms?: Zoom[] }) => void
  onCommit: () => void
}) {
  const { duration, segments, sfx, zooms, scale } = props
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
              <text x={x(z.s) + 4} y={LANES.zoom + 16} className="pointer-events-none fill-foreground text-[10px]">{z.scale.toFixed(2)}×</text>
              <rect x={x(z.s) - 3} y={LANES.zoom} width={6} height={TRACK - 4} className="cursor-ew-resize fill-transparent"
                onPointerDown={(ev) => start(ev, { kind: "zoom", index: i, edge: "start", offset: 0 })} />
              <rect x={x(z.e) - 3} y={LANES.zoom} width={6} height={TRACK - 4} className="cursor-ew-resize fill-transparent"
                onPointerDown={(ev) => start(ev, { kind: "zoom", index: i, edge: "end", offset: 0 })} />
            </g>
          )
        })}

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
  { label: "Effects", height: TRACK + GAP },
  { label: "Zooms", height: TRACK + 4 },
]
