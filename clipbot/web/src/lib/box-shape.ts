// The hook's rounded box, drawn the way clipbot burns it (captions.rounded_outline): the outline of
// the per-line boxes, every corner rounded, also where lines of different widths meet.

export interface Box { top: number; bottom: number; left: number; right: number }

type Point = [number, number]

const CURVE = 0.5523     // Bezier control distance for a quarter circle

/** An SVG path for these line boxes (any order, overlapping) with corners of this radius. */
export function roundedBoxPath(boxes: Box[], radius: number): string {
  return shapes(boxes).map((bands) => outline(bands, radius)).join(" ")
}

/** The boxes' union as runs of stacked bands, top to bottom, each run one shape. */
function shapes(boxes: Box[]): Box[][] {
  const ys = [...new Set(boxes.flatMap((b) => [b.top, b.bottom]))].sort((a, b) => a - b)
  const out: Box[][] = []
  let run: Box[] = []
  for (let i = 0; i < ys.length - 1; i++) {
    const [top, bottom] = [ys[i], ys[i + 1]]
    const cover = boxes.filter((b) => b.top <= top + 0.01 && b.bottom >= bottom - 0.01)
    if (!cover.length) {
      if (run.length) out.push(run)
      run = []
      continue
    }
    const left = Math.min(...cover.map((b) => b.left))
    const right = Math.max(...cover.map((b) => b.right))
    const last = run[run.length - 1]
    if (last && Math.abs(last.left - left) < 0.5 && Math.abs(last.right - right) < 0.5) last.bottom = bottom
    else run.push({ top, bottom, left, right })
  }
  if (run.length) out.push(run)
  return out
}

function outline(bands: Box[], radius: number): string {
  const corners: Point[] = []
  for (const b of bands) corners.push([b.right, b.top], [b.right, b.bottom])          // down the right side...
  for (const b of [...bands].reverse()) corners.push([b.left, b.bottom], [b.left, b.top])   // ...and up the left
  const points: Point[] = []
  for (const p of corners) {
    const last = points[points.length - 1]
    if (!last || last[0] !== p[0] || last[1] !== p[1]) points.push(p)
  }
  const first = points[0]
  const last = points[points.length - 1]
  if (points.length > 1 && first[0] === last[0] && first[1] === last[1]) points.pop()
  for (let changed = true; changed && points.length > 3;) {   // drop points in the middle of a straight edge
    changed = false
    for (let i = 0; i < points.length; i++) {
      const a = points[(i - 1 + points.length) % points.length]
      const b = points[i]
      const c = points[(i + 1) % points.length]
      if ((a[0] === b[0] && b[0] === c[0]) || (a[1] === b[1] && b[1] === c[1])) {
        points.splice(i, 1)
        changed = true
        break
      }
    }
  }
  const n = points.length
  // Corners share edges: each first gets half of both its edges, then may grow into what its
  // neighbour leaves over, and an edge still claimed twice shrinks both its corners alike.
  const at = (i: number) => (i + n) % n
  const edges = points.map((p, i) => dist(p, points[at(i + 1)]))      // edge i: corner i to i+1
  const half = points.map((_, i) => Math.min(radius, edges[at(i - 1)] / 2, edges[i] / 2))
  const grown = points.map((_, i) => Math.min(radius, edges[at(i - 1)] - half[at(i - 1)], edges[i] - half[at(i + 1)]))
  const fit = points.map((_, i) => Math.min(1, edges[i] / (grown[i] + grown[at(i + 1)] || 1)))
  const radii = points.map((_, i) => grown[i] * Math.min(fit[at(i - 1)], fit[i]))
  const parts: string[] = []
  for (let i = 0; i < n; i++) {
    const p = points[(i - 1 + n) % n]
    const v = points[i]
    const q = points[(i + 1) % n]
    const d1 = dist(v, p)
    const d2 = dist(v, q)
    const a: Point = [v[0] + ((p[0] - v[0]) * radii[i]) / d1, v[1] + ((p[1] - v[1]) * radii[i]) / d1]
    const b: Point = [v[0] + ((q[0] - v[0]) * radii[i]) / d2, v[1] + ((q[1] - v[1]) * radii[i]) / d2]
    const c1: Point = [a[0] + (v[0] - a[0]) * CURVE, a[1] + (v[1] - a[1]) * CURVE]
    const c2: Point = [b[0] + (v[0] - b[0]) * CURVE, b[1] + (v[1] - b[1]) * CURVE]
    parts.push(`${i ? "L" : "M"}${fmt(a)} C${fmt(c1)} ${fmt(c2)} ${fmt(b)}`)
  }
  return parts.join(" ") + " Z"
}

const dist = (a: Point, b: Point) => Math.hypot(a[0] - b[0], a[1] - b[1])
const fmt = (p: Point) => `${p[0].toFixed(2)},${p[1].toFixed(2)}`
