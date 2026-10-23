import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { Camera, Crop, Link2, Trash2, Upload, X } from "lucide-react"
import { useRef, useState } from "react"
import { toast } from "sonner"

import { NativeSelect } from "@/components/native-select"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, type Overlay, type PictureMotion, type PictureMove } from "@/lib/api"
import { fmt, pictureLook, pictureUrl } from "@/lib/edit"

const MOVE_LABEL: Record<PictureMove, string> = {
  none: "None", fade: "Fade", pop: "Pop", zoom: "Zoom", spin: "Spin",
  "slide-left": "Slide, left side", "slide-right": "Slide, right side", "slide-up": "Slide, bottom", "slide-down": "Slide, top",
}
const MOTION_LABEL: Record<PictureMotion, string> = { none: "Still", shake: "Shake", pulse: "Pulse", float: "Float", spin: "Spin" }

export function usePictures() {
  return useQuery({ queryKey: ["images"], queryFn: api.images, staleTime: 60_000 })
}

/** A new picture on the clip at the playhead: stickers smaller and higher, your pictures bigger. */
export function newOverlay(ref: string, at: number, duration: number): Overlay {
  const sticker = ref.startsWith("sticker:")
  return {
    image: ref, s: +at.toFixed(2), e: +Math.min(duration, at + 2.5).toFixed(2), x: 0.5, y: sticker ? 0.35 : 0.4,
    w: sticker ? 0.3 : 0.5, rot: 0, opacity: 1, enter: "pop", exit: "fade", motion: "none",
  }
}

/** The Pictures tab: stickers and your pictures to drop at the playhead, ways to add more (upload, a link,
 * a frame of this clip), and the selected picture's settings. */
export function PicturesPanel({ clipId, overlays, selected, grabbing, onAdd, onSelect, onChange, onSeek, playhead, onGrabBox }: {
  clipId: string; overlays: Overlay[]; selected: number; grabbing: boolean
  onAdd: (ref: string) => void; onSelect: (index: number) => void; onChange: (overlays: Overlay[]) => void
  onSeek: (t: number) => void; playhead: () => number; onGrabBox: () => void
}) {
  const qc = useQueryClient()
  const pictures = usePictures()
  const [shelf, setShelf] = useState<"stickers" | "yours">("stickers")
  const [link, setLink] = useState("")
  const file = useRef<HTMLInputElement>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: ["images"] })
  const added = (name: string) => { toast.success(`Added ${name} to your pictures`); setShelf("yours"); refresh() }
  const upload = useMutation({
    mutationFn: async (files: File[]) => { for (const f of files) await api.uploadImage(f) },
    onSuccess: (_, files) => added(files.length > 1 ? `${files.length} pictures` : files[0].name), onError: (e) => toast.error(e.message),
  })
  const fromLink = useMutation({
    mutationFn: () => api.importImage(link.trim()), onSuccess: (p) => { setLink(""); added(p.name) }, onError: (e) => toast.error(e.message),
  })
  const grab = useMutation({
    mutationFn: () => api.grabFrame(clipId, { t: playhead() }), onSuccess: (p) => added(p.name), onError: (e) => toast.error(e.message),
  })
  const remove = useMutation({ mutationFn: api.deleteImage, onSuccess: refresh, onError: (e) => toast.error(e.message) })
  const o = overlays[selected]
  const set = (patch: Partial<Overlay>) => onChange(overlays.map((x, i) => (i === selected ? { ...x, ...patch } : x)))

  return (
    <div className="space-y-4 pt-2 text-sm">
      {o && (
        <section className="space-y-2.5 rounded-md border border-primary/60 bg-primary/5 p-2.5" aria-label="Selected picture">
          <div className="flex items-center gap-2">
            <img src={pictureUrl(o.image)} alt="" className="size-9 shrink-0 rounded-sm bg-black/30 object-contain" />
            <div className="min-w-0 flex-1">
              <p className="truncate font-medium">{o.image.replace(/^sticker:/, "")}</p>
              <p className="text-xs text-muted-foreground">
                <button className="timecode text-primary hover:underline" onClick={() => onSeek(o.s)}>{fmt(o.s)}</button> to{" "}
                <button className="timecode text-primary hover:underline" onClick={() => onSeek(o.e)}>{fmt(o.e)}</button>
              </p>
            </div>
            <Button size="icon" variant="ghost" aria-label="Remove picture" onClick={() => onChange(overlays.filter((_, i) => i !== selected))}><Trash2 /></Button>
          </div>
          <div className="flex gap-1.5">
            <Button size="sm" variant="outline" className="h-7 flex-1 text-xs" onClick={() => { const t = +playhead().toFixed(2); if (t < o.e - 0.2) set({ s: t }) }}>Start here</Button>
            <Button size="sm" variant="outline" className="h-7 flex-1 text-xs" onClick={() => { const t = +playhead().toFixed(2); if (t > o.s + 0.2) set({ e: t }) }}>End here</Button>
          </div>
          <div className="grid grid-cols-3 gap-1.5 text-xs text-muted-foreground">
            <label className="space-y-1">Comes in
              <NativeSelect className="h-7 text-xs" value={o.enter} onChange={(e) => set({ enter: e.target.value as PictureMove })}>
                {Object.entries(MOVE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </NativeSelect>
            </label>
            <label className="space-y-1">Goes
              <NativeSelect className="h-7 text-xs" value={o.exit} onChange={(e) => set({ exit: e.target.value as PictureMove })}>
                {Object.entries(MOVE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </NativeSelect>
            </label>
            <label className="space-y-1">Moves
              <NativeSelect className="h-7 text-xs" value={o.motion} onChange={(e) => set({ motion: e.target.value as PictureMotion })}>
                {Object.entries(MOTION_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
              </NativeSelect>
            </label>
          </div>
          {([["Size", "w", 0.05, 1, 0.01, `${Math.round(o.w * 100)}%`], ["Turn", "rot", -180, 180, 1, `${Math.round(o.rot)}°`],
            ["Opacity", "opacity", 0.1, 1, 0.05, `${Math.round(o.opacity * 100)}%`]] as const).map(([label, key, min, max, step, shown]) => (
            <label key={key} className="flex items-center gap-2 text-xs text-muted-foreground">
              <span className="w-12 shrink-0">{label}</span>
              <input type="range" aria-label={label} min={min} max={max} step={step} value={o[key]}
                onChange={(e) => set({ [key]: Number(e.target.value) })} className="w-full accent-primary" />
              <span className="w-10 shrink-0 text-right tabular-nums">{shown}</span>
            </label>
          ))}
          <p className="text-xs text-muted-foreground">Drag it on the picture to move it; the square handle sizes it, the round one turns it.</p>
        </section>
      )}

      <section className="space-y-2">
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-medium">Add at the playhead</h3>
          <ToggleGroup type="single" variant="outline" size="sm" value={shelf} onValueChange={(v) => v && setShelf(v as typeof shelf)}>
            <ToggleGroupItem value="stickers">Stickers</ToggleGroupItem>
            <ToggleGroupItem value="yours">Yours</ToggleGroupItem>
          </ToggleGroup>
        </div>
        {shelf === "stickers" ? (
          <ul className="grid max-h-56 grid-cols-7 gap-1 overflow-y-auto pr-1" aria-label="Stickers">
            {(pictures.data?.stickers ?? []).map((s) => (
              <li key={s.name}>
                <button type="button" title={s.name} aria-label={`Add the ${s.name} sticker`} onClick={() => onAdd(s.ref)}
                  className="grid aspect-square w-full place-items-center rounded-sm hover:bg-accent">
                  <img src={s.url} alt="" loading="lazy" className="size-4/5 object-contain" />
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <div className="space-y-2">
            {!pictures.data?.items.length && (
              <p className="text-xs text-muted-foreground">No pictures yet: upload one, paste a link, or grab a frame of this clip below.</p>
            )}
            <ul className="grid max-h-56 grid-cols-4 gap-1.5 overflow-y-auto pr-1" aria-label="Your pictures">
              {(pictures.data?.items ?? []).map((p) => (
                <li key={p.name} className="group relative">
                  <button type="button" title={p.name} aria-label={`Add ${p.name}`} onClick={() => onAdd(p.ref)}
                    className="block w-full rounded-sm border border-border bg-black/30 p-1 hover:border-primary">
                    <img src={p.url} alt="" loading="lazy" className="mx-auto h-14 object-contain" />
                    <span className="block truncate text-[10px] text-muted-foreground">{p.animated ? "GIF · " : ""}{p.name}</span>
                  </button>
                  <button type="button" aria-label={`Delete ${p.name}`} onClick={() => remove.mutate(p.name)}
                    className="absolute top-0.5 right-0.5 hidden rounded-sm bg-black/70 p-0.5 text-white group-hover:block"><X className="size-3" /></button>
                </li>
              ))}
            </ul>
          </div>
        )}
        <div className="flex flex-wrap gap-1.5">
          <input ref={file} type="file" accept="image/png,image/jpeg,image/gif,image/webp" multiple hidden
            onChange={(e) => { const files = [...(e.target.files ?? [])]; e.target.value = ""; if (files.length) upload.mutate(files) }} />
          <Button size="sm" variant="outline" onClick={() => file.current?.click()} disabled={upload.isPending}><Upload /> Upload</Button>
          <Button size="sm" variant="outline" onClick={() => grab.mutate()} disabled={grab.isPending} title="This frame of the clip, as a picture"><Camera /> Grab frame</Button>
          <Button size="sm" variant={grabbing ? "secondary" : "outline"} onClick={onGrabBox}
            title="Drag a box on the preview: that part of this frame becomes a picture (a reaction face)"><Crop /> {grabbing ? "Drag a box…" : "Grab a box"}</Button>
        </div>
        <form className="flex gap-1.5" onSubmit={(e) => { e.preventDefault(); if (link.trim()) fromLink.mutate() }}>
          <div className="relative flex-1">
            <Link2 className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input aria-label="Picture link" placeholder="Paste a picture or GIF link…" className="h-8 pl-8" value={link} onChange={(e) => setLink(e.target.value)} />
          </div>
          <Button size="sm" type="submit" variant="outline" disabled={!link.trim() || fromLink.isPending}>Add</Button>
        </form>
      </section>

      {overlays.length > 0 && (
        <section className="space-y-1.5 border-t border-border pt-3">
          <h3 className="font-medium">On this clip</h3>
          <ul className="space-y-1">
            {overlays.map((x, i) => (
              <li key={i}>
                <button type="button" onClick={() => { onSelect(i); onSeek(x.s) }}
                  className={cn("flex w-full items-center gap-2 rounded-sm px-1.5 py-1 text-left hover:bg-accent", i === selected && "bg-primary/10")}>
                  <img src={pictureUrl(x.image)} alt="" className="size-6 object-contain" />
                  <span className="min-w-0 flex-1 truncate">{x.image.replace(/^sticker:/, "")}</span>
                  <span className="timecode text-xs text-muted-foreground">{fmt(x.s)}–{fmt(x.e)}</span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  )
}

type Drag = { kind: "move" | "size" | "turn"; index: number; start: Overlay; x: number; y: number }

/** The pictures on the preview, as the render places them. The selected one sits still where it's placed,
 * with handles: drag it to move it, the square to size it, the round one to turn it. */
export function PictureLayer({ overlays, time, aspect, selected, onSelect, onLive, onCommit }: {
  overlays: Overlay[]; time: number; aspect: number; selected: number
  onSelect: (index: number) => void; onLive: (overlays: Overlay[]) => void; onCommit: () => void
}) {
  const layer = useRef<HTMLDivElement>(null)
  const drag = useRef<Drag | null>(null)

  const begin = (e: React.PointerEvent, kind: Drag["kind"], index: number) => {
    e.stopPropagation()
    e.preventDefault()
    onSelect(index)
    drag.current = { kind, index, start: overlays[index], x: e.clientX, y: e.clientY }
    const move = (ev: PointerEvent) => {
      const d = drag.current
      const box = layer.current?.getBoundingClientRect()
      if (!d || !box) return
      const o = d.start
      const cx = box.left + o.x * box.width
      const cy = box.top + o.y * box.height
      let next: Overlay
      if (d.kind === "move") {
        const clamp = (v: number) => +Math.min(1, Math.max(0, v)).toFixed(3)
        next = { ...o, x: clamp(o.x + (ev.clientX - d.x) / box.width), y: clamp(o.y + (ev.clientY - d.y) / box.height) }
      } else if (d.kind === "size") {
        const ratio = Math.hypot(ev.clientX - cx, ev.clientY - cy) / Math.max(1, Math.hypot(d.x - cx, d.y - cy))
        next = { ...o, w: +Math.min(1, Math.max(0.05, o.w * ratio)).toFixed(3) }
      } else {
        const turned = o.rot + (Math.atan2(ev.clientY - cy, ev.clientX - cx) - Math.atan2(d.y - cy, d.x - cx)) * 180 / Math.PI
        let rot = ((turned + 540) % 360) - 180
        if (Math.abs(rot) < 4) rot = 0                       // snaps level
        next = { ...o, rot: Math.round(rot) }
      }
      onLive(overlays.map((x, i) => (i === d.index ? next : x)))
    }
    const up = () => {
      drag.current = null
      window.removeEventListener("pointermove", move)
      window.removeEventListener("pointerup", up)
      onCommit()
    }
    window.addEventListener("pointermove", move)
    window.addEventListener("pointerup", up)
  }

  return (
    <div ref={layer} className="pointer-events-none absolute inset-0">
      {overlays.map((o, i) => {
        const chosen = i === selected
        const live = pictureLook(o, time, aspect)
        if (!chosen && !live) return null
        // The selected one sits still where it's placed (faint when it isn't on screen now), so the handles line up.
        const look = chosen ? { scale: 1, rot: o.rot, dx: 0, dy: 0, opacity: live ? o.opacity : o.opacity * 0.4 } : live!
        return (
          <div key={i} className="absolute" onClick={(e) => e.stopPropagation()}
            style={{
              left: `${(o.x + look.dx) * 100}%`, top: `${(o.y + look.dy) * 100}%`, width: `${o.w * 100}%`,
              transform: `translate(-50%, -50%) rotate(${look.rot}deg) scale(${look.scale})`,
            }}>
            <img src={pictureUrl(o.image)} alt="" draggable={false} style={{ opacity: look.opacity }}
              className="pointer-events-auto block w-full cursor-move select-none" onPointerDown={(e) => begin(e, "move", i)} />
            {chosen && (
              <>
                <div className="pointer-events-none absolute inset-0 outline-2 outline-primary outline-dashed" />
                <div role="slider" aria-label="Size" aria-valuenow={Math.round(o.w * 100)} onPointerDown={(e) => begin(e, "size", i)}
                  className="pointer-events-auto absolute -right-2 -bottom-2 size-4 cursor-nwse-resize rounded-[3px] border-2 border-white bg-primary" />
                <div role="slider" aria-label="Turn" aria-valuenow={o.rot} onPointerDown={(e) => begin(e, "turn", i)}
                  className="pointer-events-auto absolute -top-7 left-1/2 size-4 -translate-x-1/2 cursor-grab rounded-full border-2 border-white bg-primary" />
              </>
            )}
          </div>
        )
      })}
    </div>
  )
}
