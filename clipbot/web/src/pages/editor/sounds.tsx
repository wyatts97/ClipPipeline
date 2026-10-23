import { useMutation, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { ChevronDown, Play, Plus, Search, Star, Trash2 } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { CATEGORY_LABEL, MemePackButton, SoundSearch, useSounds } from "@/components/sounds"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api, type HandEffect, type SoundCategory, type SoundItem } from "@/lib/api"
import { fmt } from "@/lib/edit"
import { SoundSelect } from "@/pages/templates"

/** The sounds on keys 1-9: the first nine starred ones. */
export function hotkeys(items: SoundItem[]): string[] {
  return items.filter((s) => s.favorite).map((s) => s.name).slice(0, 9)
}

type Shelf = "favorite" | "all" | SoundCategory

/** The editor's Sounds tab: a board of the library by category (preview, + drops one at the playhead,
 * ★ puts it on a number key), search online, and the sounds placed on this clip. */
export function SoundBoard({ sfx, sounds, onSeek, onAdd, onPlay, onChange }: {
  sfx: HandEffect[]; sounds: string[]; onSeek: (t: number) => void; onAdd: (sound: string) => void
  onPlay: (sound: string, volume?: number) => void; onChange: (sfx: HandEffect[]) => void
}) {
  const qc = useQueryClient()
  const library = useSounds()
  const items = library.data?.items ?? []
  const keys = hotkeys(items)
  const [picked, setShelf] = useState<Shelf | null>(null)
  const [text, setText] = useState("")
  const [online, setOnline] = useState(false)
  const star = useMutation({
    mutationFn: (s: SoundItem) => api.editSound(s.name, { favorite: !s.favorite }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["sounds"] }),
    onError: (e) => toast.error(e.message),
  })

  const count = (f: Shelf) => items.filter((s) => f === "all" || (f === "favorite" ? s.favorite : s.category === f)).length
  const shelves = (["favorite", "all", ...(library.data?.categories ?? [])] as Shelf[]).filter((f) => f === "all" || count(f))
  const shelf = picked && shelves.includes(picked) ? picked : keys.length ? "favorite" : "all"
  const needle = text.trim().toLowerCase()
  const shown = needle
    ? items.filter((s) => s.name.toLowerCase().includes(needle))
    : items.filter((s) => shelf === "all" || (shelf === "favorite" ? s.favorite : s.category === shelf))
  const set = (i: number, patch: Partial<HandEffect>) => onChange(sfx.map((x, j) => (j === i ? { ...x, ...patch } : x)))

  return (
    <div className="space-y-3 pt-2 text-sm">
      {library.isSuccess && !items.length ? (
        <div className="space-y-2 rounded-md border border-dashed border-border p-4 text-center text-muted-foreground">
          <p>No sounds yet. The meme pack has vine boom, HUH, bruh, metal pipe and about 30 more.</p>
          <MemePackButton data={library.data} />
        </div>
      ) : (
        <>
          <div className="relative">
            <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input aria-label="Search your sounds" placeholder="Search your sounds…" className="h-8 pl-8" value={text}
              onChange={(e) => setText(e.target.value)} />
          </div>
          {!needle && (
            <div className="flex flex-wrap gap-1" aria-label="Sound categories">
              {shelves.map((f) => (
                <button key={f} type="button" aria-pressed={shelf === f} onClick={() => setShelf(f)}
                  className={cn("rounded-sm border px-1.5 py-0.5 text-xs",
                    shelf === f ? "border-primary bg-primary/10 text-foreground" : "border-border text-muted-foreground hover:text-foreground")}>
                  {f === "all" ? "All" : f === "favorite" ? "★" : CATEGORY_LABEL[f]}
                </button>
              ))}
            </div>
          )}
          {shelf === "favorite" && !needle && !keys.length && (
            <p className="text-xs text-muted-foreground">Star sounds to keep them here, on keys 1–9.</p>
          )}
          <ul className="grid max-h-64 grid-cols-2 gap-1 overflow-y-auto pr-1" aria-label="Sound board">
            {shown.map((s) => {
              const key = keys.indexOf(s.name)
              return (
                <li key={s.name} className="flex min-w-0 items-center rounded-sm border border-border bg-card">
                  <button type="button" title={`Preview ${s.name}`} onClick={() => onPlay(s.name)}
                    className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 pl-2 text-left hover:text-primary">
                    <Play className="size-3 shrink-0 text-muted-foreground" />
                    <span className="truncate text-xs">{s.name}</span>
                  </button>
                  {key >= 0 && <kbd className="rounded-sm border border-border px-1 font-mono text-[10px] text-muted-foreground" title={`Key ${key + 1}`}>{key + 1}</kbd>}
                  <button type="button" aria-label={s.favorite ? `Unstar ${s.name}` : `Star ${s.name}`} aria-pressed={s.favorite}
                    onClick={() => star.mutate(s)} className="grid size-6 shrink-0 place-items-center text-muted-foreground hover:text-foreground">
                    <Star className={cn("size-3", s.favorite && "fill-primary text-primary")} />
                  </button>
                  <Button variant="ghost" size="icon" className="size-7 shrink-0" aria-label={`Add ${s.name} at the playhead`}
                    onClick={() => onAdd(s.name)}><Plus /></Button>
                </li>
              )
            })}
            {needle && !shown.length && <li className="col-span-2 text-xs text-muted-foreground">None of your sounds match. Search online below.</li>}
          </ul>
        </>
      )}

      <div className="rounded-md border border-border">
        <button type="button" className="flex w-full items-center justify-between px-2.5 py-1.5 text-xs text-muted-foreground hover:text-foreground"
          aria-expanded={online} onClick={() => setOnline(!online)}>
          Find more sounds online <ChevronDown className={cn("size-3.5 transition-transform", online && "rotate-180")} />
        </button>
        {online && (
          <div className="border-t border-border p-2">
            <SoundSearch compact freesound={!!library.data?.freesound} />
          </div>
        )}
      </div>

      <div className="space-y-2 border-t border-border pt-3">
        <div className="flex items-baseline justify-between">
          <h3 className="font-medium">On this clip</h3>
          <span className="text-xs text-muted-foreground">on top of the template's; drag them on the timeline</span>
        </div>
        {!sfx.length && <p className="text-xs text-muted-foreground">None yet: + on a sound, E, or a number key while it plays.</p>}
        <ul className="max-h-[30vh] space-y-2 overflow-y-auto pr-1">
          {sfx.map((fx, i) => (
            <li key={i} className="grid grid-cols-[3.5rem_minmax(0,1fr)_4.5rem_auto_auto] items-center gap-1.5">
              <button className="timecode text-left text-xs text-primary hover:underline" onClick={() => onSeek(fx.t)}>{fmt(fx.t)}</button>
              <SoundSelect sounds={sounds} value={fx.sound} onChange={(v) => v && set(i, { sound: v })} />
              <input type="range" aria-label="Volume" min={10} max={200} step={10} value={Math.round((fx.volume ?? 1) * 100)}
                onChange={(e) => set(i, { volume: Number(e.target.value) / 100 })} className="w-full accent-primary" />
              <Button size="icon" variant="ghost" aria-label={`Play ${fx.sound}`} onClick={() => onPlay(fx.sound, fx.volume)}><Play /></Button>
              <Button size="icon" variant="ghost" aria-label="Remove sound" onClick={() => onChange(sfx.filter((_, j) => j !== i))}><Trash2 /></Button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
