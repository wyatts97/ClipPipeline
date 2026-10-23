import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { Copy, Download, Scissors, Sparkles } from "lucide-react"
import { useCallback, useEffect, useMemo, useRef, useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"

import { stripEmoji } from "@/components/caption-preview"
import { ClipThumb } from "@/components/clip-thumb"
import { NativeSelect } from "@/components/native-select"
import { Status } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { Textarea } from "@/components/ui/textarea"
import { api, clipsZipUrl, type Clip, type ClipStatus, type HandEffect } from "@/lib/api"
import { ago, copyText, platformName, sourceLabel, tc } from "@/lib/format"
import type { Platform } from "@/lib/api"
import { SoundSelect } from "@/pages/templates"

type Draft = Pick<Clip, "title" | "hook" | "description_tiktok" | "description_instagram">

const draftOf = (c: Clip): Draft => ({
  title: c.title ?? "", hook: c.hook ?? "",
  description_tiktok: c.description_tiktok ?? "", description_instagram: c.description_instagram ?? "",
})

function platformOf(url: string | null): Platform {
  if (!url) return "other"
  if (url.includes("twitch.tv")) return "twitch"
  if (url.includes("youtu")) return "youtube"
  if (url.includes("kick.com")) return "kick"
  return "other"
}

export function ReviewPage() {
  const [status, setStatus] = useState<ClipStatus>("pending")
  const clips = useQuery({
    queryKey: ["clips", status],
    queryFn: () => api.clips(status),
    refetchInterval: (q) => (q.state.data?.items.some((c) => c.render_state === "rendering") ? 2000
      : status === "pending" ? 10_000 : false),
  })
  const items = useMemo(() => clips.data?.items ?? [], [clips.data])
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const selected = items.find((c) => c.id === selectedId) ?? items[0] ?? null
  const index = selected ? items.indexOf(selected) : -1

  const move = useCallback((delta: number) => {
    if (!items.length) return
    const next = items[Math.min(items.length - 1, Math.max(0, index + delta))]
    setSelectedId(next.id)
  }, [items, index])

  return (
    <div className="mx-auto flex max-w-7xl min-w-0 flex-col gap-6">
      <div className="flex flex-wrap items-center gap-4">
        <h1 className="mr-auto text-2xl font-semibold tracking-tight">Review</h1>
        {status === "approved" && items.length > 0 && (
          <Button variant="outline" asChild>
            <a href={clipsZipUrl("approved")} download><Download /> Download all {items.length} (zip)</a>
          </Button>
        )}
        <Tabs value={status} onValueChange={(v) => { setStatus(v as ClipStatus); setSelectedId(null) }}>
          <TabsList>
            <TabsTrigger value="pending">Needs review</TabsTrigger>
            <TabsTrigger value="approved">Approved</TabsTrigger>
            <TabsTrigger value="rejected">Skipped</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {clips.isPending ? null : !items.length ? (
        <Empty status={status} />
      ) : (
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[15rem_minmax(0,1fr)_22rem]">
          <ClipList items={items} selected={selected} onSelect={setSelectedId} />
          {selected && <Station key={selected.id} clip={selected} onNext={() => move(1)} onPrev={() => move(-1)}
            isLast={index === items.length - 1} />}
        </div>
      )}
    </div>
  )
}

function Empty({ status }: { status: ClipStatus }) {
  return (
    <div className="rounded-md border border-dashed border-border px-6 py-16 text-center">
      {status === "pending" ? (
        <>
          <p className="text-lg">You're caught up</p>
          <p className="mt-2 text-sm text-muted-foreground">
            Clips appear here as soon as OpenShorts finishes them. Pick moments to clip in <Link to="/scans" className="text-primary hover:underline">Scans</Link>.
          </p>
        </>
      ) : (
        <p className="text-sm text-muted-foreground">No {status === "approved" ? "approved" : "skipped"} clips yet.</p>
      )}
    </div>
  )
}

function ClipList({ items, selected, onSelect }: { items: Clip[]; selected: Clip | null; onSelect: (id: string) => void }) {
  return (
    <ol className="flex gap-2 overflow-x-auto pb-2 lg:max-h-[calc(100vh-11rem)] lg:flex-col lg:overflow-x-visible lg:overflow-y-auto lg:pr-1 lg:pb-0">
      {items.map((clip, i) => (
        <li key={clip.id} className="shrink-0">
          <button onClick={() => onSelect(clip.id)} aria-current={clip.id === selected?.id}
            className={cn("flex w-56 items-center gap-3 rounded-md border p-2 text-left transition-colors lg:w-full",
              clip.id === selected?.id ? "border-primary bg-accent" : "border-transparent hover:bg-accent/60")}>
            <ClipThumb clip={clip} className="w-12 shrink-0" showDuration={false} />
            <span className="min-w-0">
              <span className="line-clamp-2 text-sm">{clip.title || `Clip ${i + 1}`}</span>
              <span className="mt-1 block text-xs text-muted-foreground timecode">{tc(clip.duration)}</span>
            </span>
          </button>
        </li>
      ))}
    </ol>
  )
}

function isTyping(target: EventTarget | null) {
  const el = target as HTMLElement | null
  return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable)
}

function Station({ clip, onNext, onPrev, isLast }: { clip: Clip; onNext: () => void; onPrev: () => void; isLast: boolean }) {
  const qc = useQueryClient()
  const video = useRef<HTMLVideoElement>(null)
  const [draft, setDraft] = useState<Draft>(() => draftOf(clip))
  const dirty = JSON.stringify(draft) !== JSON.stringify(draftOf(clip))
  const pending = clip.status === "pending"

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["clips"] })
    qc.invalidateQueries({ queryKey: ["overview"] })
  }
  const save = useMutation({
    mutationFn: () => api.editClip(clip.id, draft),
    onSuccess: () => { toast.success("Saved"); refresh() },
  })
  const decide = useMutation({
    mutationFn: async (decision: "approve" | "reject") => {
      if (dirty) await api.editClip(clip.id, draft)
      return api.decide(clip.id, decision)
    },
    onSuccess: (res, decision) => {
      if (decision === "approve") {
        const file = res.message?.match(/approved\/\S+/)?.[0]
        toast.success("Approved", { description: file ? `Saved as ${file}` : undefined })
      }
      else toast("Skipped", { description: "Moved to rejected/." })
      if (!isLast) onNext()
      refresh()
    },
  })

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (isTyping(e.target) || e.metaKey || e.ctrlKey || e.altKey) return
      const k = e.key.toLowerCase()
      if (k === "j" || e.key === "ArrowDown") { e.preventDefault(); onNext() }
      else if (k === "k" || e.key === "ArrowUp") { e.preventDefault(); onPrev() }
      else if (pending && k === "a" && !decide.isPending) decide.mutate("approve")
      else if (pending && k === "s" && !decide.isPending) decide.mutate("reject")
    }
    window.addEventListener("keydown", onKey)
    return () => window.removeEventListener("keydown", onKey)
  }, [onNext, onPrev, pending, decide])

  const field = (key: keyof Draft) => ({
    value: draft[key] ?? "",
    onChange: (e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>) => setDraft({ ...draft, [key]: e.target.value }),
  })

  return (
    <>
      <div className="flex justify-center rounded-md bg-black/40 p-3">
        <video ref={video} key={clip.video_url} src={clip.video_url} controls autoPlay playsInline
          className="aspect-[9/16] max-h-[calc(100vh-13rem)] w-auto max-w-full rounded-sm bg-black" />
      </div>

      <div className="space-y-5">
        <div className="space-y-1.5 text-sm">
          <Status status={clip.status} />
          <p className="text-muted-foreground">
            {platformName[platformOf(clip.source_url)]}{" "}
            {clip.source_url ? (
              <a href={clip.source_url} target="_blank" rel="noreferrer" className="text-foreground hover:underline">
                {sourceLabel(clip.source_url)}
              </a>
            ) : "–"}
            {clip.range && <span className="ml-2 timecode">{tc(clip.range.start)}–{tc(clip.range.end)}</span>}
          </p>
          <p className="text-muted-foreground">
            From <Link to={`/jobs/${clip.job_id}`} className="text-foreground hover:underline">job {clip.job_id}</Link>,
            finished {ago(clip.created)}
          </p>
        </div>

        {clip.what_happened && (
          <p className="rounded-md border border-border bg-card px-3 py-2 text-sm">
            <span className="text-muted-foreground">Gemini saw: </span>{clip.what_happened}
          </p>
        )}

        <div className="space-y-4">
          <div className="space-y-1.5">
            <Label htmlFor="title">Title</Label>
            <Input id="title" {...field("title")} />
          </div>
          <HookField clip={clip} value={draft.hook ?? ""} onChange={(hook) => setDraft({ ...draft, hook })} />
          <div className="space-y-1.5">
            <Label htmlFor="tiktok">TikTok caption</Label>
            <Textarea id="tiktok" rows={3} {...field("description_tiktok")} />
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="instagram">Instagram caption</Label>
            <Textarea id="instagram" rows={3} {...field("description_instagram")} />
          </div>
          {dirty && !pending && (
            <Button variant="outline" onClick={() => save.mutate()} disabled={save.isPending}>Save changes</Button>
          )}
        </div>

        <Posting clip={clip} />

        {clip.can_render && <Look key={`${clip.template}-${JSON.stringify(clip.sfx)}`} clip={clip} video={video} />}

        {pending && (
          <div className="space-y-2 border-t border-border pt-5">
            <div className="flex gap-2">
              <Button className="flex-1 bg-success text-[#08140d] hover:bg-success/85" disabled={decide.isPending}
                onClick={() => decide.mutate("approve")}>
                Approve <Kbd>A</Kbd>
              </Button>
              <Button variant="outline" className="flex-1" disabled={decide.isPending} onClick={() => decide.mutate("reject")}>
                Skip <Kbd>S</Kbd>
              </Button>
            </div>
            <p className="text-xs text-muted-foreground">
              {dirty ? "Your edits are saved first. " : ""}J and K move between clips.
            </p>
          </div>
        )}
      </div>
    </>
  )
}

/** The hook line: edit it, get ideas from Gemini, and draw the new one onto the video. */
export function HookField({ clip, value, onChange, id = "hook" }: {
  clip: Clip; value: string; onChange: (v: string) => void; id?: string
}) {
  const qc = useQueryClient()
  const [ideas, setIdeas] = useState<string[] | null>(null)
  const suggest = useMutation({
    mutationFn: () => api.suggestHooks(clip.id),
    onSuccess: (r) => { setIdeas(r.suggestions); if (!r.suggestions.length) toast("No ideas this time. Try again.") },
  })
  const apply = useMutation({
    mutationFn: () => api.saveEdit(clip.id, { hook_text: value }),
    onSuccess: () => {
      toast("Rendering…", { description: "The clip reloads with the new hook." })
      qc.invalidateQueries({ queryKey: ["clips"] })
    },
  })
  const rendering = clip.render_state === "rendering"
  const drawn = clip.hook_rendered          // null: OpenShorts drew it, it can't change
  const differs = drawn !== null && clip.hook_on && stripEmoji(value) !== drawn

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2">
        <Label htmlFor={id}>Hook text on the video</Label>
        {clip.can_render && drawn !== null && (
          <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" disabled={suggest.isPending}
            onClick={() => suggest.mutate()}>
            <Sparkles /> {suggest.isPending ? "Thinking…" : "Suggest"}
          </Button>
        )}
      </div>
      <Input id={id} value={value} maxLength={200} onChange={(e) => onChange(e.target.value)} />
      {ideas && ideas.length > 0 && (
        <ul className="space-y-1 rounded-md border border-border p-1.5" aria-label="Hook ideas">
          {ideas.map((idea) => (
            <li key={idea}>
              <button type="button" onClick={() => { onChange(idea); setIdeas(null) }}
                className="w-full rounded-sm px-2 py-1 text-left text-sm hover:bg-accent">{idea}</button>
            </li>
          ))}
        </ul>
      )}
      {differs && clip.can_render && (
        <Button size="sm" className="w-full" disabled={rendering || apply.isPending} onClick={() => apply.mutate()}>
          {rendering ? "Rendering…" : "Put this hook on the video"}
        </Button>
      )}
      <p className="text-xs text-muted-foreground">
        {drawn === null ? "OpenShorts drew this clip's hook into the video, so changing it only updates the saved text."
          : !clip.hook_on ? "This clip shows no hook (turned off in its template or the editor). The text is still saved."
            : !clip.can_render ? "Drawn into this render; editing it updates the saved text."
              : differs ? "The video still shows the old hook until you put this one on. Emoji are left off the video."
                : "Drawn by clipbot in the template's hook look. Change when and where it shows in the editor."}
      </p>
    </div>
  )
}

function Kbd({ children }: { children: React.ReactNode }) {
  return <kbd className="ml-1.5 rounded-sm border border-current/30 px-1 font-mono text-[10px] opacity-70">{children}</kbd>
}

/** Template and hand-placed sound effects for a clip that's waiting for review. */
function Look({ clip, video }: { clip: Clip; video: React.RefObject<HTMLVideoElement | null> }) {
  const qc = useQueryClient()
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates, staleTime: 30_000 })
  const [template, setTemplate] = useState(clip.template ?? "")
  const [effects, setEffects] = useState<HandEffect[]>(clip.sfx)
  const [sound, setSound] = useState<string | null>(null)
  const sounds = templates.data?.sounds ?? []
  const pick = sound ?? sounds[0] ?? null
  const rendering = clip.render_state === "rendering"
  const changed = template !== (clip.template ?? "") || JSON.stringify(effects) !== JSON.stringify(clip.sfx)
  const render = useMutation({
    mutationFn: () => api.renderClip(clip.id, { template: template || undefined, sfx: effects }),
    onSuccess: () => {
      toast("Rendering…", { description: "The clip reloads when it's done." })
      qc.invalidateQueries({ queryKey: ["clips"] })
    },
  })

  return (
    <div className="space-y-3 border-t border-border pt-5 text-sm">
      <div className="flex items-baseline justify-between">
        <h3 className="font-medium">Look</h3>
        <Link to="/templates" className="text-xs text-muted-foreground hover:text-foreground">Edit templates</Link>
      </div>
      <Button asChild variant="secondary" className="w-full">
        <Link to={`/review/${clip.id}/edit`}><Scissors /> Open the editor</Link>
      </Button>
      <div className="space-y-1.5">
        <Label htmlFor="look-template" className="text-xs text-muted-foreground">Template</Label>
        <NativeSelect id="look-template" value={template} onChange={(e) => setTemplate(e.target.value)} disabled={rendering}>
          {(templates.data?.items ?? []).map((t) => (
            <option key={t.id} value={t.id}>{t.name}{t.default ? " (default)" : ""}</option>
          ))}
        </NativeSelect>
      </div>
      <div className="space-y-1.5">
        <span className="text-xs text-muted-foreground">Sound effects placed by hand</span>
        {effects.length > 0 && (
          <ul className="space-y-1">
            {effects.map((e, i) => (
              <li key={i} className="flex items-center gap-2">
                <button className="timecode text-primary hover:underline"
                  onClick={() => { if (video.current) video.current.currentTime = e.t }}>{tc(e.t)}</button>
                <span className="flex-1 truncate">{e.sound}</span>
                <button className="text-xs text-muted-foreground hover:text-destructive"
                  onClick={() => setEffects(effects.filter((_, j) => j !== i))}>Remove</button>
              </li>
            ))}
          </ul>
        )}
        <div className="flex gap-2">
          <SoundSelect sounds={sounds} value={pick} onChange={setSound} />
          <Button variant="outline" size="sm" className="h-9 shrink-0" disabled={!pick || rendering}
            onClick={() => {
              const t = Math.round((video.current?.currentTime ?? 0) * 10) / 10
              if (pick) setEffects([...effects, { sound: pick, t }].sort((a, b) => a.t - b.t))
            }}>
            Add at playhead
          </Button>
        </div>
        {!sounds.length && (
          <p className="text-xs text-muted-foreground">
            Upload sounds on the <Link to="/templates" className="text-primary hover:underline">Templates</Link> page.
          </p>
        )}
      </div>
      {clip.render_error && !rendering && (
        <p className={cn("text-xs", clip.render_state === "failed" ? "text-destructive" : "text-muted-foreground")}>{clip.render_error}</p>
      )}
      <Button className="w-full" variant={changed ? "default" : "outline"}
        disabled={rendering || render.isPending || (!changed && clip.render_state !== "failed")}
        onClick={() => render.mutate()}>
        {rendering ? "Rendering…" : "Re-render with these"}
      </Button>
      {clip.word_count === 0 && (
        <p className="text-xs text-muted-foreground">OpenShorts sent no words for this clip, so it can't get captions.</p>
      )}
    </div>
  )
}

/** Download the clip and copy its post text, for posting by hand. */
function Posting({ clip }: { clip: Clip }) {
  const copy = (label: string, text: string) =>
    copyText(text).then(() => toast.success(`${label} copied`), () => toast.error("Couldn't copy; select the text instead."))
  const t = clip.post_text
  return (
    <div className="space-y-2 border-t border-border pt-5 text-sm">
      <div className="flex items-baseline justify-between">
        <h3 className="font-medium">Post it yourself</h3>
        <a href={clip.download_url} download className="inline-flex items-center gap-1.5 text-primary hover:underline">
          <Download className="size-4" /> Download
        </a>
      </div>
      <div className="flex flex-wrap gap-1.5">
        <Button size="sm" variant="outline" onClick={() => copy("TikTok caption", t.tiktok)}><Copy /> TikTok</Button>
        <Button size="sm" variant="outline" onClick={() => copy("Instagram caption", t.instagram)}><Copy /> Instagram</Button>
        <Button size="sm" variant="outline" onClick={() => copy("YouTube title", t.youtube_title)}><Copy /> YouTube title</Button>
        <Button size="sm" variant="outline" onClick={() => copy("YouTube description", t.youtube)}><Copy /> YouTube description</Button>
      </div>
      <p className="text-xs text-muted-foreground">The same text publishing uses: the captions above, plus the template's credit line.</p>
    </div>
  )
}
