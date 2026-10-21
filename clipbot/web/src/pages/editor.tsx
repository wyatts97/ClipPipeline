import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import {
  ArrowLeft, ChevronLeft, ChevronRight, Pause, Play, Plus, Redo2, Scissors, Trash2, Undo2, ZoomIn,
} from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Link, useParams } from "react-router"
import { toast } from "sonner"

import { CaptionLayer, useFont } from "@/components/caption-preview"
import { EditTimeline, TRACK_LABELS, type Selection } from "@/components/edit-timeline"
import { NativeSelect } from "@/components/native-select"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, type ClipEdit, type EditorData, type HandEffect, type TemplatesResponse, type Word, type Zoom } from "@/lib/api"
import {
  captionAt, FRAME, fmt, groupWords, keptWords, mapTime, nextKept, outputDuration, retime, segmentAt, split,
  type Segment, zoomAt,
} from "@/lib/edit"
import { SoundSelect } from "@/pages/templates"

interface Doc { edit: ClipEdit; sfx: HandEffect[]; words: Word[]; template: string }

export function EditorPage() {
  const { clipId } = useParams()
  const q = useQuery({
    queryKey: ["editor", clipId],
    queryFn: () => api.editor(clipId!),
    enabled: !!clipId,
    retry: false,
    refetchInterval: (query) => (query.state.data?.clip.render_state === "rendering" ? 2000 : false),
  })
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates, staleTime: 60_000 })

  if (q.isError) {
    return (
      <div className="mx-auto max-w-xl space-y-3 py-16 text-center">
        <p>{(q.error as Error).message}</p>
        <Button variant="outline" asChild><Link to="/review"><ArrowLeft /> Back to Review</Link></Button>
      </div>
    )
  }
  if (!q.data || !templates.data) return <Skeleton className="mx-auto h-[70vh] max-w-7xl" />
  // A re-cut brings a new source file: start the editor over on it.
  return <Editor key={q.data.source_url} data={q.data} templates={templates.data} />
}

function Editor({ data, templates }: { data: EditorData; templates: TemplatesResponse }) {
  const qc = useQueryClient()
  const clipId = data.clip.id
  const fallbackTemplate = data.template ?? templates.default ?? templates.items[0]?.id ?? ""
  const initial = useMemo<Doc>(() => ({ edit: data.edit, sfx: data.sfx, words: data.words, template: fallbackTemplate }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [])
  const [doc, setDoc] = useState<Doc>(initial)
  const [saved, setSaved] = useState<Doc>(initial)
  const docRef = useRef(doc)
  docRef.current = doc
  const past = useRef<Doc[]>([])
  const future = useRef<Doc[]>([])
  const dragOrigin = useRef<Doc | null>(null)
  const [, bump] = useState(0)

  const commit = useCallback((next: Doc) => {
    past.current.push(docRef.current)
    future.current = []
    setDoc(next)
  }, [])
  const live = useCallback((patch: Partial<ClipEdit> & { sfx?: HandEffect[] }) => {
    if (!dragOrigin.current) dragOrigin.current = docRef.current
    const cur = docRef.current
    setDoc({
      ...cur,
      sfx: patch.sfx ?? cur.sfx,
      edit: { segments: patch.segments ?? cur.edit.segments, zooms: patch.zooms ?? cur.edit.zooms },
    })
  }, [])
  const endDrag = useCallback(() => {
    if (dragOrigin.current) {
      past.current.push(dragOrigin.current)
      future.current = []
      dragOrigin.current = null
      bump((n) => n + 1)
    }
  }, [])
  const undo = useCallback(() => {
    const prev = past.current.pop()
    if (!prev) return
    future.current.push(docRef.current)
    setDoc(prev)
  }, [])
  const redo = useCallback(() => {
    const next = future.current.pop()
    if (!next) return
    past.current.push(docRef.current)
    setDoc(next)
  }, [])

  const duration = data.source_duration
  const segments = doc.edit.segments as Segment[]
  const template = templates.items.find((t) => t.id === doc.template) ?? templates.items[0]
  const style = template?.captions
  useFont(templates.fonts, style?.font_name ?? "Anton")
  const dirty = JSON.stringify(doc) !== JSON.stringify(saved)
  const rendering = data.clip.render_state === "rendering"

  // ---- player -------------------------------------------------------------------------------
  const video = useRef<HTMLVideoElement>(null)
  const [time, setTime] = useState(segments[0]?.[0] ?? 0)
  const [playing, setPlaying] = useState(false)
  const [view, setView] = useState<"edit" | "render">("edit")
  const [selection, setSelection] = useState<Selection>(null)
  const [scale, setScale] = useState(1)
  const sounds = useRef(new Map<string, HTMLAudioElement>())

  const playSound = useCallback((name: string, volume = 1) => {
    let audio = sounds.current.get(name)
    if (!audio) {
      audio = new Audio(`/api/sounds/${encodeURIComponent(name)}/file`)
      sounds.current.set(name, audio)
    }
    const copy = audio.cloneNode() as HTMLAudioElement
    copy.volume = Math.min(1, volume)
    void copy.play().catch(() => undefined)
  }, [])

  useEffect(() => {
    let raf = 0
    let prev = video.current?.currentTime ?? 0
    const tick = () => {
      const v = video.current
      if (v) {
        let t = v.currentTime
        if (!v.paused) {
          const seg = nextKept(t, segments)
          if (!seg) {
            v.pause()
            t = segments[segments.length - 1][1]
          } else if (t < seg[0]) {
            v.currentTime = seg[0]   // skip what was cut
            t = seg[0]
          }
          if (t > prev && t - prev < 0.5) {
            for (const fx of docRef.current.sfx) if (fx.t > prev && fx.t <= t) playSound(fx.sound, fx.volume ?? 1)
          }
        }
        prev = t
        setTime(t)
        setPlaying(!v.paused)
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [segments, playSound])

  const seek = useCallback((t: number) => {
    const v = video.current
    if (v) v.currentTime = Math.min(duration, Math.max(0, t))
    setTime(t)
  }, [duration])
  const toggle = useCallback(() => {
    const v = video.current
    if (!v) return
    if (!v.paused) { v.pause(); return }
    if (!nextKept(v.currentTime, segments)) v.currentTime = segments[0][0]
    void v.play()
  }, [segments])

  // ---- edits ----------------------------------------------------------------------------------
  const setEdit = (edit: Partial<ClipEdit>) => commit({ ...doc, edit: { ...doc.edit, ...edit } })
  const playhead = () => video.current?.currentTime ?? time      // exact even between animation frames
  const splitHere = () => setEdit({ segments: split(segments, playhead()) })
  const addZoom = () => {
    const s = +playhead().toFixed(2)
    commit({ ...doc, edit: { ...doc.edit, zooms: [...doc.edit.zooms, { s, e: +Math.min(duration, s + 0.8).toFixed(2), scale: 1.2 }].sort((a, b) => a.s - b.s) } })
    setSelection({ kind: "zoom", index: doc.edit.zooms.filter((z) => z.s <= s).length })
  }
  const addEffect = (sound: string | null) => {
    if (!sound) return toast("Upload sounds on the Templates page first.")
    commit({ ...doc, sfx: [...doc.sfx, { sound, t: +playhead().toFixed(2), volume: 1 }].sort((a, b) => a.t - b.t) })
  }
  const removeSelected = () => {
    if (!selection) return
    if (selection.kind === "segment") {
      if (segments.length <= 1) return toast("Split the clip first (S), then delete the part you don't want.")
      setEdit({ segments: segments.filter((_, i) => i !== selection.index) })
    } else if (selection.kind === "sfx") commit({ ...doc, sfx: doc.sfx.filter((_, i) => i !== selection.index) })
    else setEdit({ zooms: doc.edit.zooms.filter((_, i) => i !== selection.index) })
    setSelection(null)
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const el = e.target as HTMLElement | null
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable)) return
      const k = e.key.toLowerCase()
      if ((e.ctrlKey || e.metaKey) && k === "z") { e.preventDefault(); if (e.shiftKey) redo(); else undo(); return }
      if ((e.ctrlKey || e.metaKey) && k === "y") { e.preventDefault(); redo(); return }
      if (e.ctrlKey || e.metaKey || e.altKey) return
      if (k === " ") { e.preventDefault(); toggle() }
      else if (k === "arrowleft") { e.preventDefault(); seek(time - (e.shiftKey ? 1 : FRAME)) }
      else if (k === "arrowright") { e.preventDefault(); seek(time + (e.shiftKey ? 1 : FRAME)) }
      else if (k === "s") splitHere()
      else if (k === "z") addZoom()
      else if (k === "e") addEffect(templates.sounds[0] ?? null)
      else if (k === "delete" || k === "backspace") removeSelected()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })

  // ---- render ------------------------------------------------------------------------------------
  const render = useMutation({
    mutationFn: () => api.saveEdit(clipId, { template: doc.template || undefined, sfx: doc.sfx, edit: doc.edit, words: doc.words }),
    onSuccess: () => { setSaved(doc); qc.invalidateQueries({ queryKey: ["editor", clipId] }) },
  })
  const wasRendering = useRef(rendering)
  useEffect(() => {
    if (wasRendering.current && !rendering) {
      if (data.clip.render_state === "failed") toast.error("Render failed", { description: data.clip.render_error ?? undefined })
      else {
        toast.success("Rendered", { description: "Sent to Telegram for review too." })
        setView("render")
      }
      qc.invalidateQueries({ queryKey: ["clips"] })
    }
    wasRendering.current = rendering
  }, [rendering, data.clip.render_state, data.clip.render_error, qc])

  // ---- what the preview shows ------------------------------------------------------------------
  const blocks = useMemo(() => (style ? groupWords(keptWords(doc.words, segments), style.max_chars, style.max_duration, segments) : []),
    [doc.words, segments, style])
  const caption = captionAt(blocks, time)
  const zoom = zoomAt(doc.edit, time)
  const outTime = mapTime(time, segments)
  const outTotal = outputDuration(segments)
  const peaks = useQuery({ queryKey: ["peaks", data.peaks_url], queryFn: () => api.peaks(data.peaks_url), staleTime: Infinity })

  return (
    <div className="mx-auto flex max-w-[110rem] flex-col gap-4">
      <header className="flex flex-wrap items-center gap-3">
        <Button variant="ghost" size="sm" asChild><Link to="/review"><ArrowLeft /> Review</Link></Button>
        <h1 className="mr-auto min-w-0 truncate text-lg font-semibold tracking-tight">{data.clip.title || `Clip ${data.clip.id}`}</h1>
        <NativeSelect aria-label="Template" className="h-8 w-auto" value={doc.template}
          onChange={(e) => commit({ ...doc, template: e.target.value })}>
          {templates.items.map((t) => <option key={t.id} value={t.id}>{t.name}{t.default ? " (default)" : ""}</option>)}
        </NativeSelect>
        <Button variant="ghost" size="icon" aria-label="Undo" disabled={!past.current.length} onClick={undo}><Undo2 /></Button>
        <Button variant="ghost" size="icon" aria-label="Redo" disabled={!future.current.length} onClick={redo}><Redo2 /></Button>
        <span className={cn("text-sm", data.clip.render_state === "failed" ? "text-destructive" : "text-muted-foreground")}>
          {rendering ? "Rendering…" : data.clip.render_state === "failed" ? "Last render failed" : dirty ? "Unsaved changes" : "Saved"}
        </span>
        <Button onClick={() => render.mutate()} disabled={rendering || render.isPending}>Render</Button>
      </header>
      {data.clip.render_error && !rendering && (
        <p className={cn("text-sm", data.clip.render_state === "failed" ? "text-destructive" : "text-muted-foreground")}>{data.clip.render_error}</p>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <section className="flex flex-col items-center gap-3 rounded-md bg-black/40 p-3">
          <div className="flex gap-1 self-stretch">
            <Button size="sm" variant={view === "edit" ? "secondary" : "ghost"} onClick={() => setView("edit")}>Edit preview</Button>
            <Button size="sm" variant={view === "render" ? "secondary" : "ghost"} onClick={() => setView("render")}>Last render</Button>
            <span className="ml-auto self-center text-xs text-muted-foreground">
              {view === "edit" ? "Live preview: cuts, captions, zooms and your effects. Music and template effects play in the render." : "The finished clip as Telegram got it."}
            </span>
          </div>
          {view === "edit" ? (
            <div className="relative aspect-[9/16] h-[min(62vh,44rem)] overflow-hidden rounded-sm bg-black" style={{ containerType: "size" }}>
              <video ref={video} src={data.source_url} playsInline preload="auto" className="absolute inset-0 size-full object-contain"
                style={{ transform: `scale(${zoom})` }} onClick={toggle} />
              {style && <CaptionLayer style={style} line={caption.line} active={caption.active} />}
              {outTime === null && <div className="pointer-events-none absolute inset-0 grid place-items-center bg-black/60 text-sm text-white/80">Cut</div>}
            </div>
          ) : (
            <video key={data.clip.video_url} src={data.clip.video_url} controls playsInline className="aspect-[9/16] h-[min(62vh,44rem)] rounded-sm bg-black" />
          )}
        </section>

        <Tabs defaultValue="captions" className="min-w-0">
          <TabsList className="w-full">
            <TabsTrigger value="captions">Captions</TabsTrigger>
            <TabsTrigger value="effects">Effects</TabsTrigger>
            <TabsTrigger value="zooms">Zooms</TabsTrigger>
            <TabsTrigger value="length">Length</TabsTrigger>
          </TabsList>
          <TabsContent value="captions">
            <CaptionsPanel words={doc.words} segments={segments} maxChars={style?.max_chars ?? 16} maxDuration={style?.max_duration ?? 1.4}
              original={data.words_original ?? data.words} onSeek={seek} onChange={(words) => commit({ ...doc, words })} />
          </TabsContent>
          <TabsContent value="effects">
            <EffectsPanel sfx={doc.sfx} sounds={templates.sounds} onSeek={seek} onAdd={addEffect} onPlay={playSound}
              onChange={(sfx) => commit({ ...doc, sfx })} />
          </TabsContent>
          <TabsContent value="zooms">
            <ZoomsPanel zooms={doc.edit.zooms} onSeek={seek} onAdd={addZoom} onChange={(zooms) => setEdit({ zooms })} />
          </TabsContent>
          <TabsContent value="length">
            <LengthPanel data={data} segments={segments} duration={duration} disabled={rendering}
              onReset={() => setEdit({ segments: [[0, duration]] })} />
          </TabsContent>
        </Tabs>
      </div>

      <section className="space-y-2">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <Button size="icon" variant="outline" aria-label={playing ? "Pause" : "Play"} onClick={toggle}>{playing ? <Pause /> : <Play />}</Button>
          <Button size="icon" variant="ghost" aria-label="Back one frame" onClick={() => seek(time - FRAME)}><ChevronLeft /></Button>
          <Button size="icon" variant="ghost" aria-label="Forward one frame" onClick={() => seek(time + FRAME)}><ChevronRight /></Button>
          <span className="timecode tabular-nums">{fmt(outTime ?? 0)} <span className="text-muted-foreground">/ {fmt(outTotal)}</span></span>
          <span className="text-xs text-muted-foreground">source {fmt(time)}</span>
          <span className="mx-2 h-5 w-px bg-border" />
          <Button size="sm" variant="outline" onClick={splitHere} title="S"><Scissors /> Split</Button>
          <Button size="sm" variant="outline" onClick={removeSelected} disabled={!selection} title="Delete"><Trash2 /> Delete</Button>
          <Button size="sm" variant="outline" onClick={addZoom} title="Z"><ZoomIn /> Zoom here</Button>
          <Button size="sm" variant="outline" onClick={() => addEffect(templates.sounds[0] ?? null)} title="E"><Plus /> Effect here</Button>
          <label className="ml-auto flex items-center gap-2 text-xs text-muted-foreground">
            Timeline zoom
            <input type="range" min={1} max={8} step={0.5} value={scale} onChange={(e) => setScale(Number(e.target.value))} className="w-28 accent-primary" />
          </label>
        </div>
        <div className="grid grid-cols-[4.5rem_minmax(0,1fr)] gap-2">
          <div className="text-xs text-muted-foreground">
            {TRACK_LABELS.map((l, i) => <div key={i} style={{ height: l.height }} className="flex items-center">{l.label}</div>)}
          </div>
          <EditTimeline duration={duration} time={time} segments={segments} words={doc.words} sfx={doc.sfx}
            zooms={doc.edit.zooms} peaks={peaks.data?.peaks ?? []} perSecond={peaks.data?.per_second ?? 50}
            selection={selection} scale={scale} onSeek={seek} onSelect={setSelection}
            onLive={live} onCommit={endDrag} />
        </div>
        <p className="text-xs text-muted-foreground">
          Space plays · ←/→ one frame (Shift: one second) · S splits at the playhead · select a part and Delete cuts it · drag the
          edges of a part to trim it · Z adds a zoom · E an effect · Ctrl+Z undoes. Rendering sends the new version to Telegram.
        </p>
      </section>
    </div>
  )
}

// ---- panels ---------------------------------------------------------------------------------------
function CaptionsPanel({ words, segments, maxChars, maxDuration, original, onSeek, onChange }: {
  words: Word[]; segments: Segment[]; maxChars: number; maxDuration: number; original: Word[]
  onSeek: (t: number) => void; onChange: (words: Word[]) => void
}) {
  const lines = useMemo(() => {
    const out: { from: number; words: Word[] }[] = []
    let from = 0
    for (const block of groupWords(words, maxChars, maxDuration)) {
      out.push({ from, words: block.words })
      from += block.words.length
    }
    return out
  }, [words, maxChars, maxDuration])

  const replace = (from: number, count: number, next: Word[]) =>
    onChange([...words.slice(0, from), ...next, ...words.slice(from + count)])

  return (
    <div className="space-y-2 pt-2 text-sm">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">Fix words here; their timing stays. Empty a line to drop it. Lines in cut parts are greyed.</p>
        <Button size="sm" variant="ghost" onClick={() => onChange(original)}>Reset</Button>
      </div>
      {!lines.length && <p className="text-muted-foreground">No words: OpenShorts sent no transcript for this clip.</p>}
      <ul className="max-h-[52vh] space-y-1 overflow-y-auto pr-1">
        {lines.map((line) => {
          const kept = segmentAt(line.words[0].s, segments) >= 0
          return (
            <li key={`${line.from}-${line.words[0].s}`} className={cn("flex items-center gap-2", !kept && "opacity-45")}>
              <button className="timecode w-12 shrink-0 text-left text-xs text-primary hover:underline" onClick={() => onSeek(line.words[0].s)}>
                {fmt(line.words[0].s)}
              </button>
              <LineInput text={line.words.map((w) => w.w).join(" ")} onCommit={(text) => replace(line.from, line.words.length, retime(line.words, text))} />
            </li>
          )
        })}
      </ul>
    </div>
  )
}

function LineInput({ text, onCommit }: { text: string; onCommit: (text: string) => void }) {
  const [value, setValue] = useState(text)
  useEffect(() => setValue(text), [text])
  const done = () => { if (value.trim() !== text) onCommit(value.trim()) }
  return (
    <Input value={value} className="h-8" onChange={(e) => setValue(e.target.value)} onBlur={done}
      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setValue(text) }} />
  )
}

function EffectsPanel({ sfx, sounds, onSeek, onAdd, onPlay, onChange }: {
  sfx: HandEffect[]; sounds: string[]; onSeek: (t: number) => void; onAdd: (sound: string | null) => void
  onPlay: (sound: string, volume?: number) => void; onChange: (sfx: HandEffect[]) => void
}) {
  const [pick, setPick] = useState<string | null>(sounds[0] ?? null)
  const set = (i: number, patch: Partial<HandEffect>) => onChange(sfx.map((x, j) => (j === i ? { ...x, ...patch } : x)))
  return (
    <div className="space-y-3 pt-2 text-sm">
      <p className="text-xs text-muted-foreground">
        Effects you place yourself, on top of the template's. Drag them on the timeline to move them.
      </p>
      <div className="flex gap-2">
        <SoundSelect sounds={sounds} value={pick} onChange={setPick} />
        <Button size="sm" variant="outline" className="h-9 shrink-0" onClick={() => onAdd(pick)} disabled={!pick}>Add at playhead</Button>
      </div>
      <ul className="max-h-[46vh] space-y-2 overflow-y-auto pr-1">
        {sfx.map((fx, i) => (
          <li key={i} className="grid grid-cols-[3.5rem_minmax(0,1fr)_5rem_auto_auto] items-center gap-2">
            <button className="timecode text-left text-xs text-primary hover:underline" onClick={() => onSeek(fx.t)}>{fmt(fx.t)}</button>
            <SoundSelect sounds={sounds} value={fx.sound} onChange={(v) => v && set(i, { sound: v })} />
            <input type="range" aria-label="Volume" min={10} max={200} step={10} value={Math.round((fx.volume ?? 1) * 100)}
              onChange={(e) => set(i, { volume: Number(e.target.value) / 100 })} className="w-full accent-primary" />
            <Button size="icon" variant="ghost" aria-label={`Play ${fx.sound}`} onClick={() => onPlay(fx.sound, fx.volume)}><Play /></Button>
            <Button size="icon" variant="ghost" aria-label="Remove effect" onClick={() => onChange(sfx.filter((_, j) => j !== i))}><Trash2 /></Button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function ZoomsPanel({ zooms, onSeek, onAdd, onChange }: {
  zooms: Zoom[]; onSeek: (t: number) => void; onAdd: () => void; onChange: (zooms: Zoom[]) => void
}) {
  const set = (i: number, patch: Partial<Zoom>) => onChange(zooms.map((z, j) => (j === i ? { ...z, ...patch } : z)))
  return (
    <div className="space-y-3 pt-2 text-sm">
      <p className="text-xs text-muted-foreground">A quick punch-in on the picture (captions stay put). Great with an effect on the same beat.</p>
      <Button size="sm" variant="outline" onClick={onAdd}><ZoomIn /> Add a zoom at the playhead</Button>
      <ul className="space-y-2">
        {zooms.map((z, i) => (
          <li key={i} className="grid grid-cols-[minmax(0,1fr)_6rem_auto] items-center gap-2">
            <button className="timecode text-left text-xs text-primary hover:underline" onClick={() => onSeek(z.s)}>
              {fmt(z.s)}–{fmt(z.e)}
            </button>
            <label className="flex items-center gap-1 text-xs text-muted-foreground">
              <input type="range" aria-label="Zoom amount" min={1.05} max={1.6} step={0.05} value={z.scale}
                onChange={(e) => set(i, { scale: Number(e.target.value) })} className="w-full accent-primary" />
              {z.scale.toFixed(2)}×
            </label>
            <Button size="icon" variant="ghost" aria-label="Remove zoom" onClick={() => onChange(zooms.filter((_, j) => j !== i))}><Trash2 /></Button>
          </li>
        ))}
      </ul>
    </div>
  )
}

function LengthPanel({ data, segments, duration, disabled, onReset }: {
  data: EditorData; segments: Segment[]; duration: number; disabled: boolean; onReset: () => void
}) {
  const qc = useQueryClient()
  const [extendStart, setExtendStart] = useState(0)
  const [extendEnd, setExtendEnd] = useState(0)
  const [framing, setFraming] = useState("")
  const os = data.openshorts
  const recut = useMutation({
    mutationFn: () => api.recut(data.clip.id, { extend_start: extendStart, extend_end: extendEnd, framing: framing || null }),
    onSuccess: () => { toast("Re-cutting in OpenShorts…", { description: "Extending or reframing can take a few minutes on this CPU." }); qc.invalidateQueries({ queryKey: ["editor", data.clip.id] }) },
  })
  const changed = extendStart > 0 || extendEnd > 0 || framing !== ""
  const reach = os.source_kept || (!extendStart && !extendEnd && !framing)
  return (
    <div className="space-y-5 pt-2 text-sm">
      <div className="space-y-1.5">
        <h3 className="font-medium">Cuts</h3>
        <p className="text-muted-foreground">
          Keeping {segments.length} part{segments.length > 1 ? "s" : ""}: {fmt(outputDuration(segments))} of {fmt(duration)}.
          Split with S, select a part and Delete it, or drag a part's edges.
        </p>
        <Button size="sm" variant="ghost" onClick={onReset} disabled={segments.length === 1 && segments[0][0] === 0 && segments[0][1] === duration}>
          Keep the whole clip
        </Button>
      </div>
      <div className="space-y-2 border-t border-border pt-4">
        <h3 className="font-medium">Longer or reframed (OpenShorts)</h3>
        <p className="text-xs text-muted-foreground">
          OpenShorts cuts the clip again from the stream: more at either end, or framed differently. Your cuts, zooms and
          effects move along; caption text edits start over from the new transcript.
        </p>
        {!os.available || !os.source_kept ? <p className="text-xs text-destructive">{os.reason}</p> : null}
        <div className="grid grid-cols-2 gap-2">
          <label className="space-y-1 text-xs text-muted-foreground">Start earlier by
            <NativeSelect value={extendStart} disabled={!os.source_kept} onChange={(e) => setExtendStart(Number(e.target.value))}>
              {[0, 1, 2, 3, 5, 10, 15].map((n) => <option key={n} value={n}>{n ? `${n} s` : "—"}</option>)}
            </NativeSelect>
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">End later by
            <NativeSelect value={extendEnd} disabled={!os.source_kept} onChange={(e) => setExtendEnd(Number(e.target.value))}>
              {[0, 1, 2, 3, 5, 10, 15].map((n) => <option key={n} value={n}>{n ? `${n} s` : "—"}</option>)}
            </NativeSelect>
          </label>
        </div>
        <label className="block space-y-1 text-xs text-muted-foreground">Framing (now: {os.framing ?? "unknown"})
          <NativeSelect value={framing} disabled={!os.source_kept} onChange={(e) => setFraming(e.target.value)}>
            <option value="">Keep it</option>
            <option value="auto">Auto (OpenShorts picks per shot)</option>
            <option value="track">Follow the speaker</option>
            <option value="full">Whole frame (blurred bars)</option>
          </NativeSelect>
        </label>
        <Button size="sm" onClick={() => recut.mutate()} disabled={!os.available || !changed || !reach || disabled || recut.isPending}>
          Re-cut with OpenShorts
        </Button>
      </div>
    </div>
  )
}
