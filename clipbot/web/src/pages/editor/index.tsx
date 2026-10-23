import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import {
  ArrowLeft, ChevronLeft, ChevronRight, Pause, Play, Plus, Redo2, Scissors, Snowflake, Trash2, Undo2, ZoomIn,
} from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Link, useParams } from "react-router"
import { toast } from "sonner"

import { CaptionLayer, emOf, HookLayer, useFont } from "@/components/caption-preview"
import { EditTimeline, TRACK_LABELS, type Selection } from "@/components/edit-timeline"
import { NativeSelect } from "@/components/native-select"
import { useSounds } from "@/components/sounds"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import {
  api, type ClipEdit, type DraftState, type EditorData, type Freeze, type HandEffect, type TemplatesResponse, type Word,
  type Zoom, type ZoomStyle,
} from "@/lib/api"
import {
  accentZooms, captionAt, FRAME, fmt, framePoint, groupWords, hookShows, hookWindow, keptWords, mapTime, nextKept,
  outputDuration, reconcile, screenPoint, segmentAt, split, type Segment, timeline, transitionLook, zoomAt,
} from "@/lib/edit"
import { CaptionsPanel } from "@/pages/editor/captions"
import { CutsPanel } from "@/pages/editor/cuts"
import { HookPanel } from "@/pages/editor/hook"
import { LengthPanel } from "@/pages/editor/length"
import { newOverlay, PictureLayer, PicturesPanel } from "@/pages/editor/pictures"
import { hotkeys, SoundBoard } from "@/pages/editor/sounds"
import { ZOOM_NEW, ZoomsPanel } from "@/pages/editor/zooms"

// The clip editor: a live preview over the clip without captions, the timeline, and a panel per kind
// of edit (pages/editor/*). Render saves the edit and makes the clip again.

interface Doc { edit: ClipEdit; sfx: HandEffect[]; words: Word[]; template: string; hookText: string }

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
  const initial = useMemo<Doc>(() => ({
    edit: data.edit, sfx: data.sfx, words: data.words, template: fallbackTemplate, hookText: data.clip.hook ?? "",
  }),
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
    const edit: ClipEdit = {
      ...cur.edit, segments: patch.segments ?? cur.edit.segments, zooms: patch.zooms ?? cur.edit.zooms,
      hook: patch.hook ?? cur.edit.hook, freezes: patch.freezes ?? cur.edit.freezes, overlays: patch.overlays ?? cur.edit.overlays,
    }
    setDoc({ ...cur, sfx: patch.sfx ?? cur.sfx, edit: patch.segments ? reconcile(cur.edit.segments as Segment[], edit) : edit })
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
  const look = template?.hook ?? templates.hook_default
  useFont(templates.fonts, style?.font_name ?? "Anton")
  useFont(templates.fonts, look.font_name)
  const hookFixed = data.clip.hook_rendered === null     // OpenShorts drew it into the video
  const dirty = JSON.stringify(doc) !== JSON.stringify(saved)
  const rendering = data.clip.render_state === "rendering"

  // ---- player -------------------------------------------------------------------------------
  const video = useRef<HTMLVideoElement>(null)
  const [time, setTime] = useState(segments[0]?.[0] ?? 0)
  const [playing, setPlaying] = useState(false)
  const [view, setView] = useState<"edit" | "render" | "draft">("edit")
  const [tab, setTab] = useState("captions")
  // "Grab a box": drag a box on the preview, and that part of the frame becomes a picture.
  const [grabbing, setGrabbing] = useState(false)
  const [box, setBox] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  const [selection, setSelection] = useState<Selection>(null)
  // A freeze holding while the preview plays: the video waits on its frame until `until`.
  const hold = useRef<{ f: Freeze; until: number } | null>(null)
  const released = useRef<Freeze | null>(null)      // held until the video moves on from its frame
  const [held, setHeld] = useState<{ f: Freeze; p: number } | null>(null)
  const [scale, setScale] = useState(1)
  const [aspect, setAspect] = useState(9 / 16)
  const [zoomStyle, setZoomStyle] = useState<ZoomStyle>("punch")
  const [pick, setPick] = useState<string | null>(null)
  const sounds = useRef(new Map<string, HTMLAudioElement>())
  const library = useSounds()
  const keys = hotkeys(library.data?.items ?? [])
  const current = pick ?? keys[0] ?? templates.sounds[0] ?? null      // what E drops

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
        const h = hold.current
        if (h) {
          const left = h.until - performance.now()
          if (left <= 0) {
            hold.current = null
            released.current = h.f
            setHeld({ f: h.f, p: 1 })
            void v.play()
          } else setHeld({ f: h.f, p: 1 - left / (h.f.d * 1000) })
        } else if (released.current && t > released.current.t) {
          released.current = null
          setHeld(null)
        }
        if (!h && !v.paused) {
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
            const f = (docRef.current.edit.freezes ?? []).find((x) => x.t > prev && x.t <= t)
            if (f) {             // hold the frame, like the render
              v.pause()
              v.currentTime = f.t
              t = f.t
              hold.current = { f, until: performance.now() + f.d * 1000 }
              setHeld({ f, p: 0 })
              if (f.style === "scratch") playSound("record-scratch")
            }
          }
        }
        prev = t
        setTime(t)
        setPlaying(!v.paused || !!hold.current)
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [segments, playSound])

  const seek = useCallback((t: number) => {
    const v = video.current
    hold.current = released.current = null
    setHeld(null)
    if (v) v.currentTime = Math.min(duration, Math.max(0, t))
    setTime(t)
  }, [duration])
  const toggle = useCallback(() => {
    const v = video.current
    if (!v) return
    if (hold.current) { hold.current = released.current = null; setHeld(null); return }
    if (!v.paused) { v.pause(); return }
    if (!nextKept(v.currentTime, segments)) v.currentTime = segments[0][0]
    void v.play()
  }, [segments])

  // ---- edits ----------------------------------------------------------------------------------
  const setEdit = (edit: Partial<ClipEdit>) => {
    const next = { ...doc.edit, ...edit }
    commit({ ...doc, edit: edit.segments ? reconcile(segments, next) : next })
  }
  const playhead = () => video.current?.currentTime ?? time      // exact even between animation frames
  const splitHere = () => setEdit({ segments: split(segments, playhead()) })
  const addZoom = (style: ZoomStyle = zoomStyle) => {
    const s = +playhead().toFixed(2)
    const { d, scale: amount } = ZOOM_NEW[style]
    const zoom: Zoom = { s, e: +Math.min(duration, s + d).toFixed(2), scale: amount, ...(style !== "punch" && { style }) }
    commit({ ...doc, edit: { ...doc.edit, zooms: [...doc.edit.zooms, zoom].sort((a, b) => a.s - b.s) } })
    setSelection({ kind: "zoom", index: doc.edit.zooms.filter((z) => z.s <= s).length })
  }
  const addEffect = (sound: string | null) => {
    if (!sound) return toast("No sounds yet: install the meme pack in the Sounds tab.")
    setPick(sound)
    commit({ ...doc, sfx: [...doc.sfx, { sound, t: +playhead().toFixed(2), volume: 1 }].sort((a, b) => a.t - b.t) })
  }
  const preview = (sound: string, volume?: number) => { setPick(sound); playSound(sound, volume) }
  const addFreeze = () => {
    const t = +playhead().toFixed(2)
    if (segmentAt(t, segments) < 0) return toast("Move the playhead onto a kept part to freeze it.")
    const freezes = [...(doc.edit.freezes ?? []).filter((f) => Math.abs(f.t - t) >= 0.05), { t, d: 1 }].sort((a, b) => a.t - b.t)
    setEdit({ freezes })
    setSelection({ kind: "freeze", index: freezes.findIndex((f) => f.t === t) })
    setTab("cuts")
  }
  const select = (s: Selection) => {
    setSelection(s)
    if (s?.kind === "cut" || s?.kind === "freeze") setTab("cuts")
    if (s?.kind === "picture") setTab("pictures")
  }
  const addPicture = (ref: string) => {
    const overlays = [...(doc.edit.overlays ?? []), newOverlay(ref, playhead(), duration)]
    setEdit({ overlays })
    setSelection({ kind: "picture", index: overlays.length - 1 })
  }
  const removeSelected = () => {
    if (!selection) return
    if (selection.kind === "segment") {
      if (segments.length <= 1) return toast("Split the clip first (S), then delete the part you don't want.")
      setEdit({ segments: segments.filter((_, i) => i !== selection.index) })
    } else if (selection.kind === "sfx") commit({ ...doc, sfx: doc.sfx.filter((_, i) => i !== selection.index) })
    else if (selection.kind === "freeze") setEdit({ freezes: (doc.edit.freezes ?? []).filter((_, i) => i !== selection.index) })
    else if (selection.kind === "picture") setEdit({ overlays: (doc.edit.overlays ?? []).filter((_, i) => i !== selection.index) })
    else if (selection.kind === "cut") {
      setEdit({ transitions: (doc.edit.transitions ?? []).filter((tr) => tr.after !== segments[selection.index]?.[1]) })
    } else setEdit({ zooms: doc.edit.zooms.filter((_, i) => i !== selection.index) })
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
      else if (k === "f") addFreeze()
      else if (k === "e") addEffect(current)
      else if (/^[1-9]$/.test(k) && keys[Number(k) - 1]) { addEffect(keys[Number(k) - 1]); playSound(keys[Number(k) - 1]) }
      else if (k === "delete" || k === "backspace") removeSelected()
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  })

  // ---- render ------------------------------------------------------------------------------------
  const render = useMutation({
    mutationFn: () => api.saveEdit(clipId, {
      template: doc.template || undefined, sfx: doc.sfx, edit: doc.edit, words: doc.words,
      hook_text: hookFixed ? undefined : doc.hookText,
    }),
    onSuccess: () => { setSaved(doc); qc.invalidateQueries({ queryKey: ["editor", clipId] }) },
  })
  const [draft, setDraft] = useState<DraftState | null>(null)
  const makeDraft = useMutation({
    mutationFn: () => api.draft(clipId, {
      template: doc.template || undefined, sfx: doc.sfx, edit: doc.edit, words: doc.words,
      hook_text: hookFixed ? undefined : doc.hookText,
    }),
    onSuccess: setDraft,
    onError: (e) => toast.error(e.message),
  })
  const drafting = draft?.state === "rendering"
  const draftPoll = useQuery({ queryKey: ["draft", clipId], queryFn: () => api.draftState(clipId), enabled: drafting, refetchInterval: 1500 })
  useEffect(() => {
    const s = draftPoll.data
    if (!s || !drafting || s.state === "rendering") return
    setDraft(s)
    if (s.state === "done") setView("draft")
    else toast.error("The draft didn't render", { description: s.error ?? undefined })
  }, [draftPoll.data, drafting])
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
  const parts = useMemo(() => timeline(doc.edit), [doc.edit])
  const blocks = useMemo(() => (style ? groupWords(keptWords(doc.words, segments), style.max_chars, style.max_duration, doc.edit) : []),
    [doc.words, segments, style, doc.edit])
  const caption = captionAt(blocks, time)
  const zoom = zoomAt({ ...doc.edit, zooms: [...doc.edit.zooms, ...accentZooms(parts)] }, time, aspect)
  const cutLook = transitionLook(parts, time)
  const grayHold = held && (held.f.style === "gray" || held.f.style === "scratch")
  const holdScale = held?.f.style === "zoom" ? 1 + 0.15 * held.p : 1
  const aiming = selection?.kind === "zoom" ? doc.edit.zooms[selection.index] : undefined
  const focus = aiming && screenPoint(zoom, aiming.x ?? 0.5, aiming.y ?? 0.5)
  const onPreviewClick = (e: React.MouseEvent<HTMLDivElement>) => {
    if (selection?.kind === "picture") return setSelection(null)
    if (!aiming || !selection) return toggle()
    const box = e.currentTarget.getBoundingClientRect()
    const [x, y] = framePoint(zoom, (e.clientX - box.left) / box.width, (e.clientY - box.top) / box.height)
    const at = (v: number) => +Math.min(1, Math.max(0, v)).toFixed(3)
    setEdit({ zooms: doc.edit.zooms.map((z, i) => (i === selection.index ? { ...z, x: at(x), y: at(y) } : z)) })
  }
  const grab = useMutation({
    mutationFn: (crop: { x: number; y: number; w: number; h: number }) => api.grabFrame(clipId, { t: playhead(), crop }),
    onSuccess: (p) => { toast.success(`Added ${p.name} to your pictures`); qc.invalidateQueries({ queryKey: ["images"] }) },
    onError: (e) => toast.error(e.message),
  })
  const boxAt = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect()
    return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]
  }
  const boxEnd = () => {
    if (!box) return
    setBox(null)
    setGrabbing(false)
    // The box on the screen, back on the frame (the preview may be zoomed in).
    const [ax, ay] = framePoint(zoom, Math.min(box.x0, box.x1), Math.min(box.y0, box.y1))
    const [bx, by] = framePoint(zoom, Math.max(box.x0, box.x1), Math.max(box.y0, box.y1))
    if (bx - ax < 0.03 || by - ay < 0.03) return toast("Drag a bigger box to grab part of the frame.")
    grab.mutate({ x: +ax.toFixed(4), y: +ay.toFixed(4), w: +(bx - ax).toFixed(4), h: +(by - ay).toFixed(4) })
  }
  const hookWin = hookWindow(doc.edit, look, templates.auto_hook, duration)
  const outTime = held ? (mapTime(held.f.t, doc.edit) ?? 0) + held.p * held.f.d : mapTime(time, doc.edit)
  const outTotal = outputDuration(doc.edit)
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
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="icon" aria-label="Undo" disabled={!past.current.length} onClick={undo}><Undo2 /></Button>
          <Button variant="ghost" size="icon" aria-label="Redo" disabled={!future.current.length} onClick={redo}><Redo2 /></Button>
          <span className={cn("text-sm whitespace-nowrap", data.clip.render_state === "failed" ? "text-destructive" : "text-muted-foreground")}>
            {rendering ? "Rendering…" : data.clip.render_state === "failed" ? "Last render failed" : dirty ? "Unsaved changes" : "Saved"}
          </span>
          <Button variant="outline" onClick={() => makeDraft.mutate()} disabled={drafting || makeDraft.isPending}
            title="A quick 480p render of the edit as it is now, to check freezes and transitions. Nothing is saved or sent.">
            {drafting ? "Drafting…" : "Draft"}
          </Button>
          <Button onClick={() => render.mutate()} disabled={rendering || render.isPending}>Render</Button>
        </div>
      </header>
      {data.clip.render_error && !rendering && (
        <p className={cn("text-sm", data.clip.render_state === "failed" ? "text-destructive" : "text-muted-foreground")}>{data.clip.render_error}</p>
      )}

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_24rem]">
        <section className="flex flex-col items-center gap-3 rounded-md bg-black/40 p-3">
          <div className="flex gap-1 self-stretch">
            <Button size="sm" variant={view === "edit" ? "secondary" : "ghost"} onClick={() => setView("edit")}>Edit preview</Button>
            <Button size="sm" variant={view === "render" ? "secondary" : "ghost"} onClick={() => setView("render")}>Last render</Button>
            {draft?.url && <Button size="sm" variant={view === "draft" ? "secondary" : "ghost"} onClick={() => setView("draft")}>Draft</Button>}
            <span className="ml-auto self-center text-xs text-muted-foreground">
              {view === "edit"
                ? grabbing ? <span className="text-primary">Drag a box over the part of this frame to keep as a picture.</span>
                  : aiming ? <span className="text-primary">Click the picture to aim the selected zoom.</span>
                  : "Live preview: cuts, captions, the hook, zooms and your sounds; transitions are hinted. Music and template effects play in the render."
                : view === "draft" ? "A quick 480p draft of the edit as it was when you pressed Draft. Nothing was saved or sent."
                : "The finished clip as Telegram got it."}
            </span>
          </div>
          {view === "edit" ? (
            <div className={cn("relative aspect-[9/16] h-[min(62vh,44rem)] touch-none overflow-hidden rounded-sm bg-black",
              (aiming || grabbing) && "cursor-crosshair")}
              style={{ containerType: "size" }} onClick={grabbing ? undefined : onPreviewClick}
              onPointerDown={grabbing ? (e) => { e.currentTarget.setPointerCapture(e.pointerId); const [x, y] = boxAt(e); setBox({ x0: x, y0: y, x1: x, y1: y }) } : undefined}
              onPointerMove={grabbing && box ? (e) => { const [x, y] = boxAt(e); setBox({ ...box, x1: x, y1: y }) } : undefined}
              onPointerUp={grabbing ? boxEnd : undefined}>
              <video ref={video} src={data.source_url} playsInline preload="auto" className="absolute inset-0 size-full object-contain"
                style={{
                  transform: `${cutLook.translate ? `translate(${cutLook.translate[0]}%, ${cutLook.translate[1]}%) ` : ""}scale(${zoom.scale * (cutLook.scale ?? 1) * holdScale})`,
                  transformOrigin: `${zoom.ox * 100}% ${zoom.oy * 100}%`,
                  filter: [cutLook.filter, grayHold && "grayscale(1)"].filter(Boolean).join(" ") || undefined,
                  clipPath: cutLook.clipPath,
                }}
                onLoadedMetadata={(e) => { const v = e.currentTarget; if (v.videoWidth && v.videoHeight) setAspect(v.videoWidth / v.videoHeight) }} />
              {cutLook.overlay && (
                <div className="pointer-events-none absolute inset-0" style={{ background: cutLook.overlay, opacity: cutLook.overlayOpacity }} />
              )}
              <PictureLayer overlays={doc.edit.overlays ?? []} time={time} aspect={aspect}
                selected={selection?.kind === "picture" ? selection.index : -1}
                onSelect={(index) => select({ kind: "picture", index })} onLive={(overlays) => live({ overlays })} onCommit={endDrag} />
              {!hookFixed && hookShows(hookWin, time, doc.edit) && <HookLayer hook={look} text={doc.hookText} position={hookWin.position}
                em={emOf(templates.fonts, look.font_name)} />}
              {style && <CaptionLayer style={style} line={caption.line} active={caption.active} em={emOf(templates.fonts, style.font_name)} />}
              {outTime === null && <div className="pointer-events-none absolute inset-0 grid place-items-center bg-black/60 text-sm text-white/80">Cut</div>}
              {box && (
                <div className="pointer-events-none absolute border-2 border-dashed border-primary bg-primary/10"
                  style={{ left: `${Math.min(box.x0, box.x1) * 100}%`, top: `${Math.min(box.y0, box.y1) * 100}%`,
                    width: `${Math.abs(box.x1 - box.x0) * 100}%`, height: `${Math.abs(box.y1 - box.y0) * 100}%` }} />
              )}
              {focus && (
                <div data-zoom-focus className="pointer-events-none absolute size-6 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-primary shadow-[0_0_0_2px_rgba(0,0,0,.5)]"
                  style={{ left: `${focus[0] * 100}%`, top: `${focus[1] * 100}%` }} aria-hidden />
              )}
            </div>
          ) : view === "draft" && draft?.url ? (
            <video key={draft.url} src={draft.url} controls autoPlay playsInline className="aspect-[9/16] h-[min(62vh,44rem)] rounded-sm bg-black" />
          ) : (
            <video key={data.clip.video_url} src={data.clip.video_url} controls playsInline className="aspect-[9/16] h-[min(62vh,44rem)] rounded-sm bg-black" />
          )}
        </section>

        <Tabs value={tab} onValueChange={setTab} className="min-w-0">
          <TabsList className="w-full">
            <TabsTrigger value="captions">Captions</TabsTrigger>
            <TabsTrigger value="hook">Hook</TabsTrigger>
            <TabsTrigger value="sounds">Sounds</TabsTrigger>
            <TabsTrigger value="zooms">Zooms</TabsTrigger>
            <TabsTrigger value="pictures">Pictures</TabsTrigger>
            <TabsTrigger value="cuts">Cuts</TabsTrigger>
          </TabsList>
          <TabsContent value="captions">
            <CaptionsPanel words={doc.words} segments={segments} maxChars={style?.max_chars ?? 16} maxDuration={style?.max_duration ?? 1.4}
              original={data.words_original ?? data.words} onSeek={seek} onChange={(words) => commit({ ...doc, words })} />
          </TabsContent>
          <TabsContent value="hook">
            <HookPanel clipId={clipId} fixed={hookFixed} text={doc.hookText} own={doc.edit.hook ?? {}} window={hookWin} look={look}
              time={time} playhead={playhead} duration={duration} onSeek={seek}
              onText={(hookText) => commit({ ...doc, hookText })}
              onChange={(hook) => setEdit({ hook: Object.keys(hook).length ? hook : undefined })} />
          </TabsContent>
          <TabsContent value="sounds">
            <SoundBoard sfx={doc.sfx} sounds={templates.sounds} onSeek={seek} onAdd={addEffect} onPlay={preview}
              onChange={(sfx) => commit({ ...doc, sfx })} />
          </TabsContent>
          <TabsContent value="zooms">
            <ZoomsPanel zooms={doc.edit.zooms} selected={selection?.kind === "zoom" ? selection.index : -1} style={zoomStyle}
              onStyle={setZoomStyle} onSelect={(index) => setSelection({ kind: "zoom", index })} onSeek={seek}
              onAdd={() => addZoom()} onChange={(zooms) => setEdit({ zooms })} />
          </TabsContent>
          <TabsContent value="pictures">
            <PicturesPanel clipId={clipId} overlays={doc.edit.overlays ?? []} selected={selection?.kind === "picture" ? selection.index : -1}
              grabbing={grabbing} onAdd={addPicture} onSelect={(index) => setSelection({ kind: "picture", index })}
              onChange={(overlays) => setEdit({ overlays })} onSeek={seek} playhead={playhead}
              onGrabBox={() => { setGrabbing(!grabbing); setBox(null); if (!grabbing) video.current?.pause() }} />
          </TabsContent>
          <TabsContent value="cuts" className="space-y-5">
            <CutsPanel segments={segments} transitions={doc.edit.transitions ?? []} freezes={doc.edit.freezes ?? []}
              selection={selection} onSelect={setSelection} onSeek={seek} onAddFreeze={addFreeze}
              onTransitions={(transitions) => setEdit({ transitions })} onFreezes={(freezes) => setEdit({ freezes })} />
            <div className="border-t border-border">
              <LengthPanel data={data} segments={segments} total={outTotal} duration={duration} disabled={rendering}
                onReset={() => setEdit({ segments: [[0, duration]] })} />
            </div>
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
          <Button size="sm" variant="outline" onClick={() => addZoom()} title="Z"><ZoomIn /> Zoom here</Button>
          <Button size="sm" variant="outline" onClick={addFreeze} title="F"><Snowflake /> Freeze here</Button>
          <Button size="sm" variant="outline" onClick={() => addEffect(current)} title={`E${current ? `: ${current}` : ""}`}><Plus /> Sound here</Button>
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
            zooms={doc.edit.zooms} freezes={doc.edit.freezes ?? []} transitions={doc.edit.transitions ?? []}
            overlays={doc.edit.overlays ?? []}
            peaks={peaks.data?.peaks ?? []} perSecond={peaks.data?.per_second ?? 50}
            hook={hookFixed ? null : { window: hookWin, own: doc.edit.hook ?? {}, text: doc.hookText }}
            selection={selection} scale={scale} onSeek={seek} onSelect={select}
            onLive={live} onCommit={endDrag} />
        </div>
        <p className="text-xs text-muted-foreground">
          Space plays · ←/→ one frame (Shift: one second) · S splits at the playhead · select a part and Delete cuts it · drag the
          edges of a part to trim it · ⧉ on a cut picks its transition · F freezes the frame · Pictures: stickers and your own,
          dragged and turned on the preview · drag the hook or its ends · Z adds a
          zoom (select one, then click the picture to aim it) · E drops the last sound, 1–9 your ★ sounds · Ctrl+Z undoes. Draft
          renders a quick check; Render sends the new version to Telegram.
        </p>
      </section>
    </div>
  )
}
