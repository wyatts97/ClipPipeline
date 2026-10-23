import { cn } from "cn"
import { useLayoutEffect, useMemo, useRef, useState } from "react"

import type { Moment, Timeline } from "@/lib/api"
import { tc } from "@/lib/format"

export interface Selection { start: number; end: number }

const LABEL_W = 92          // left gutter for lane names
const TOP = 22              // room for moment ranks above the score
const MAIN_H = 132
const LANE_H = 11
const LANE_GAP = 5
const AXIS_H = 22
const HEAT = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)", "var(--chart-4)", "var(--chart-5)"]
// score where each heat stop is reached
const HEAT_AT = [0, 1.5, 3, 4.5, 6]

const LANES = [
  { key: "chat", label: "Chat", color: "#c9c5bb" },
  { key: "hype", label: "Hype", color: "#d9a7c7" },
  { key: "audio", label: "Loudness", color: "#8fb3d9" },
  { key: "replayed", label: "Most replayed", color: "#c9c5bb" },
] as const

function useWidth<T extends HTMLElement>() {
  const ref = useRef<T>(null)
  const [width, setWidth] = useState(0)
  useLayoutEffect(() => {
    if (!ref.current) return
    setWidth(ref.current.clientWidth)
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(320, Math.floor(entry.contentRect.width))))
    ro.observe(ref.current)
    return () => ro.disconnect()
  }, [])
  return [ref, width] as const
}

function tickStep(duration: number, width: number) {
  const want = Math.max(3, Math.floor((width - LABEL_W) / 110))
  return [60, 120, 300, 600, 900, 1800, 3600, 7200, 10800].find((s) => duration / s <= want) ?? 21600
}

export function HeatTimeline({ timeline, moments, selection, onSelect, activeMoment, onPickMoment }: {
  timeline: Timeline
  moments: Moment[]
  selection: Selection | null
  onSelect: (s: Selection | null) => void
  activeMoment?: string | null
  onPickMoment?: (m: Moment) => void
}) {
  const [wrapRef, width] = useWidth<HTMLDivElement>()
  const svgRef = useRef<SVGSVGElement>(null)
  const [hover, setHover] = useState<number | null>(null)
  const drag = useRef<{ mode: "new" | "start" | "end"; anchor: number; moved: boolean } | null>(null)

  const duration = timeline.duration
  const score = timeline.series.score ?? []
  const lanes = LANES.filter((l) => (timeline.series[l.key]?.some((v) => v > 0.5)))
  const hasMarkers = timeline.markers.length > 0
  const laneCount = lanes.length + (hasMarkers ? 1 : 0)
  const height = TOP + MAIN_H + 8 + laneCount * (LANE_H + LANE_GAP) + AXIS_H
  const plotW = width - LABEL_W
  const x = (t: number) => LABEL_W + (t / duration) * plotW
  const tAt = (px: number) => Math.min(duration, Math.max(0, ((px - LABEL_W) / plotW) * duration))
  const yMax = Math.max(7, ...score) * 1.05
  const y = (v: number) => TOP + MAIN_H - (Math.min(Math.max(v, 0), yMax) / yMax) * MAIN_H

  const area = useMemo(() => {
    if (!score.length) return ""
    const step = timeline.step
    const pts = score.map((v, i) => `${x((i + 0.5) * step).toFixed(1)},${y(v).toFixed(1)}`)
    return `M${x(0)},${y(0)} L${pts.join(" L")} L${x(duration)},${y(0)} Z`
  }, [score, width, duration, yMax])

  const ticks = useMemo(() => {
    const step = tickStep(duration, width)
    const out: number[] = []
    for (let t = step; t < duration - step * 0.3; t += step) out.push(t)
    return out
  }, [duration, width])

  function px(e: React.PointerEvent) {
    const rect = svgRef.current!.getBoundingClientRect()
    return e.clientX - rect.left
  }

  function onPointerDown(e: React.PointerEvent) {
    const p = px(e)
    if (p < LABEL_W) return
    const t = tAt(p)
    if (selection) {
      const near = (edge: number) => Math.abs(x(edge) - p) < 7
      if (near(selection.start)) { drag.current = { mode: "start", anchor: selection.end, moved: false } }
      else if (near(selection.end)) { drag.current = { mode: "end", anchor: selection.start, moved: false } }
    }
    if (!drag.current) drag.current = { mode: "new", anchor: t, moved: false }
    svgRef.current!.setPointerCapture(e.pointerId)
  }

  function onPointerMove(e: React.PointerEvent) {
    const p = px(e)
    setHover(p >= LABEL_W ? tAt(p) : null)
    const d = drag.current
    if (!d) return
    const t = tAt(p)
    if (d.mode === "new" && Math.abs(x(t) - x(d.anchor)) < 4 && !d.moved) return
    d.moved = true
    onSelect({ start: Math.min(d.anchor, t), end: Math.max(d.anchor, t) })
  }

  function onPointerUp(e: React.PointerEvent) {
    const d = drag.current
    drag.current = null
    if (!d || d.moved) return
    // A click: pick the moment under the pointer, or drop a scan-sized window there.
    const t = tAt(px(e))
    const hit = moments.find((m) => t >= m.start && t <= m.end)
    if (hit && onPickMoment) return onPickMoment(hit)
    onSelect({ start: Math.max(0, t - 90), end: Math.min(duration, t + 45) })
  }

  const laneTop = (i: number) => TOP + MAIN_H + 8 + i * (LANE_H + LANE_GAP)
  const gradientStops = HEAT_AT.map((s, i) => ({ offset: Math.min(1, s / yMax), color: HEAT[i] }))

  return (
    <div ref={wrapRef} className="w-full min-w-0 overflow-hidden select-none">
      {width > 0 && <svg ref={svgRef} width={width} height={height} role="img"
        aria-label="Stream timeline. Drag to choose a range to clip; click a numbered moment to select it."
        className="touch-none" onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
        onPointerLeave={() => setHover(null)}>
        <defs>
          <linearGradient id="heat" gradientUnits="userSpaceOnUse" x1="0" x2="0" y1={y(0)} y2={y(yMax)}>
            {gradientStops.map((s, i) => <stop key={i} offset={s.offset} stopColor={s.color} />)}
          </linearGradient>
          <linearGradient id="heat-fade" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0" stopColor="white" stopOpacity="1" />
            <stop offset="1" stopColor="white" stopOpacity="0.35" />
          </linearGradient>
          <mask id="fade">
            <rect x={LABEL_W} y={TOP} width={plotW} height={MAIN_H} fill="url(#heat-fade)" />
          </mask>
        </defs>

        {/* baseline and the score */}
        <line x1={LABEL_W} x2={width} y1={y(0)} y2={y(0)} stroke="var(--border)" />
        <text x={LABEL_W - 10} y={TOP + 12} textAnchor="end" className="fill-muted-foreground text-[11px]">Score</text>
        <path d={area} fill="url(#heat)" mask="url(#fade)" />
        <path d={area.replace(/ Z$/, "")} fill="none" stroke="url(#heat)" strokeWidth={1.25} />

        {/* ranked moments */}
        {moments.map((m) => {
          const active = m.id === activeMoment
          return (
            <g key={m.id}>
              <rect x={x(m.start)} y={TOP} width={Math.max(2, x(m.end) - x(m.start))} height={MAIN_H}
                fill="var(--foreground)" opacity={active ? 0.1 : 0.04} />
              <line x1={x(m.peak)} x2={x(m.peak)} y1={TOP} y2={TOP + MAIN_H} stroke="var(--foreground)" strokeOpacity={0.35} strokeDasharray="2 3" />
              <g transform={`translate(${x(m.peak)}, ${TOP - 4})`}>
                <rect x={-9} y={-14} width={18} height={15} rx={3}
                  fill={active ? "var(--primary)" : "var(--popover)"} stroke="var(--border)" />
                <text y={-3} textAnchor="middle" className={cn("text-[10px] font-semibold", active ? "fill-primary-foreground" : "fill-foreground")}>
                  {m.rank}
                </text>
              </g>
            </g>
          )
        })}

        {/* signal lanes */}
        {lanes.map((lane, li) => {
          const values = timeline.series[lane.key] ?? []
          const w = Math.max(1, plotW / values.length + 0.5)
          return (
            <g key={lane.key}>
              <text x={LABEL_W - 10} y={laneTop(li) + LANE_H - 1.5} textAnchor="end" className="fill-muted-foreground text-[11px]">{lane.label}</text>
              <rect x={LABEL_W} y={laneTop(li)} width={plotW} height={LANE_H} fill="var(--muted)" />
              {values.map((v, i) => v > 1 && (
                <rect key={i} x={x(i * timeline.step)} y={laneTop(li)} width={w} height={LANE_H}
                  fill={lane.color} opacity={Math.min(1, (v - 1) / 6)} />
              ))}
            </g>
          )
        })}
        {hasMarkers && (
          <g>
            <text x={LABEL_W - 10} y={laneTop(lanes.length) + LANE_H - 1.5} textAnchor="end" className="fill-muted-foreground text-[11px]">Viewer clips</text>
            <rect x={LABEL_W} y={laneTop(lanes.length)} width={plotW} height={LANE_H} fill="var(--muted)" />
            {timeline.markers.map((mk, i) => (
              <rect key={i} x={x(mk.t)} y={laneTop(lanes.length)} width={Math.max(2, x(mk.t + mk.duration) - x(mk.t))}
                height={LANE_H} fill="var(--chart-5)" opacity={Math.min(1, 0.35 + Math.log10(1 + mk.views) / 4)}>
                <title>{mk.title ? `“${mk.title}” · ` : ""}{mk.views} views · {tc(mk.t)}</title>
              </rect>
            ))}
          </g>
        )}

        {/* time axis */}
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={TOP} y2={height - AXIS_H + 4} stroke="var(--border)" strokeOpacity={0.5} />
            <text x={x(t)} y={height - 6} textAnchor="middle" className="fill-muted-foreground text-[11px] timecode">{tc(t)}</text>
          </g>
        ))}

        {/* your in/out selection */}
        {selection && (
          <g>
            <rect x={x(selection.start)} y={TOP - 18} width={Math.max(1, x(selection.end) - x(selection.start))}
              height={height - AXIS_H - TOP + 18} fill="var(--primary)" fillOpacity={0.12} stroke="var(--primary)" strokeWidth={1.25} />
            {[selection.start, selection.end].map((edge, i) => (
              <rect key={i} x={x(edge) - 2.5} y={TOP + MAIN_H / 2 - 12} width={5} height={24} rx={2} fill="var(--primary)" className="cursor-ew-resize" />
            ))}
          </g>
        )}

        {/* hover readout */}
        {hover !== null && (
          <g pointerEvents="none">
            <line x1={x(hover)} x2={x(hover)} y1={TOP} y2={height - AXIS_H} stroke="var(--foreground)" strokeOpacity={0.5} />
            <text x={Math.min(width - 4, Math.max(LABEL_W + 30, x(hover)))} y={TOP + MAIN_H + 4} textAnchor="middle"
              className="fill-foreground text-[11px] timecode">
              {tc(hover)}
            </text>
          </g>
        )}
      </svg>}
    </div>
  )
}
