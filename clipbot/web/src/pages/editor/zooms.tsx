import { cn } from "cn"
import { Trash2, ZoomIn } from "lucide-react"

import { NativeSelect } from "@/components/native-select"
import { Button } from "@/components/ui/button"
import type { Zoom, ZoomStyle } from "@/lib/api"
import { fmt } from "@/lib/edit"

export const ZOOM_LABEL: Record<ZoomStyle, string> = {
  punch: "Punch in", smooth: "Smooth in and out", push: "Slow push", shake: "Shake", out: "Zoom out",
}
// A new zoom of each style: how long and how far.
export const ZOOM_NEW: Record<ZoomStyle, { d: number; scale: number }> = {
  punch: { d: 0.8, scale: 1.2 }, smooth: { d: 1.5, scale: 1.25 }, push: { d: 3, scale: 1.15 },
  shake: { d: 0.6, scale: 1.15 }, out: { d: 1.2, scale: 1.3 },
}

export function ZoomsPanel({ zooms, selected, style, onStyle, onSelect, onSeek, onAdd, onChange }: {
  zooms: Zoom[]; selected: number; style: ZoomStyle; onStyle: (style: ZoomStyle) => void; onSelect: (index: number) => void
  onSeek: (t: number) => void; onAdd: () => void; onChange: (zooms: Zoom[]) => void
}) {
  const set = (i: number, patch: Partial<Zoom>) => onChange(zooms.map((z, j) => (j === i ? { ...z, ...patch } : z)))
  return (
    <div className="space-y-3 pt-2 text-sm">
      <p className="text-xs text-muted-foreground">
        Zooms move the picture (captions stay put). Select one, then click the picture to aim it at a face or the
        action. Great with a sound on the same beat.
      </p>
      <div className="flex gap-2">
        <NativeSelect aria-label="New zoom style" className="h-8" value={style} onChange={(e) => onStyle(e.target.value as ZoomStyle)}>
          {Object.entries(ZOOM_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
        </NativeSelect>
        <Button size="sm" variant="outline" className="shrink-0" onClick={onAdd}><ZoomIn /> Add at playhead</Button>
      </div>
      <ul className="max-h-[46vh] space-y-2 overflow-y-auto pr-1">
        {zooms.map((z, i) => {
          const aimed = (z.x ?? 0.5) !== 0.5 || (z.y ?? 0.5) !== 0.5
          return (
            <li key={i} onClick={() => onSelect(i)}
              className={cn("space-y-2 rounded-md border p-2", i === selected ? "border-primary bg-primary/5" : "border-border")}>
              <div className="flex items-center gap-2">
                <button className="timecode shrink-0 text-left text-xs text-primary hover:underline" onClick={() => onSeek(z.s)}>
                  {fmt(z.s)}–{fmt(z.e)}
                </button>
                <NativeSelect aria-label="Zoom style" className="h-7 text-xs" value={z.style ?? "punch"}
                  onChange={(e) => set(i, { style: e.target.value === "punch" ? undefined : e.target.value as ZoomStyle })}>
                  {Object.entries(ZOOM_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </NativeSelect>
                <Button size="icon" variant="ghost" className="size-7 shrink-0" aria-label="Remove zoom"
                  onClick={(e) => { e.stopPropagation(); onChange(zooms.filter((_, j) => j !== i)) }}><Trash2 /></Button>
              </div>
              <div className="flex items-center gap-2 text-xs text-muted-foreground">
                <input type="range" aria-label="Zoom amount" min={1.05} max={1.6} step={0.05} value={z.scale}
                  onChange={(e) => set(i, { scale: Number(e.target.value) })} className="w-full accent-primary" />
                <span className="w-10 shrink-0 tabular-nums">{z.scale.toFixed(2)}×</span>
                {aimed ? (
                  <button className="shrink-0 text-primary hover:underline" onClick={() => set(i, { x: undefined, y: undefined })}>Centre</button>
                ) : (
                  <span className="shrink-0">centre</span>
                )}
              </div>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
