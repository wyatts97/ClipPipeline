import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { ExternalLink, RefreshCw, Trash2 } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"

import { NativeSelect } from "@/components/native-select"
import { Light, Status } from "@/components/status"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import { api, type ScanMode, type Streamer, type StreamerVod, type Template } from "@/lib/api"
import { ago, tc } from "@/lib/format"

const PLATFORM = { twitch: "Twitch", youtube: "YouTube", kick: "Kick" } as const

function useStreamerActions() {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["streamers"] })
    qc.invalidateQueries({ queryKey: ["scans"] })
    qc.invalidateQueries({ queryKey: ["overview"] })
  }
  return {
    refresh,
    edit: useMutation({ mutationFn: ({ id, body }: { id: string; body: Parameters<typeof api.editStreamer>[1] }) =>
      api.editStreamer(id, body), onSuccess: refresh }),
    check: useMutation({ mutationFn: api.refreshStreamer, onSuccess: () => { toast.success("VOD list updated"); refresh() } }),
    backfill: useMutation({
      mutationFn: ({ id, count }: { id: string; count: number }) => api.backfillStreamer(id, count),
      onSuccess: (r) => { toast.success(r.queued.length ? `Scanning ${r.queued.length} VOD${r.queued.length > 1 ? "s" : ""}` : "Every listed VOD is scanned already"); refresh() },
    }),
    scan: useMutation({
      mutationFn: ({ id, vodId }: { id: string; vodId: string }) => api.scanStreamerVod(id, vodId),
      onSuccess: () => { toast.success("Scan queued"); refresh() },
    }),
    remove: useMutation({ mutationFn: api.removeStreamer, onSuccess: () => { toast("Streamer removed"); refresh() } }),
  }
}

export function StreamersPage() {
  const q = useQuery({
    queryKey: ["streamers"], queryFn: api.streamers,
    refetchInterval: (query) => (query.state.data?.items.some((s) => s.vods.some((v) => v.scan && ["queued", "running"].includes(v.scan.status))) ? 5000 : 30_000),
  })
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates, staleTime: 60_000 })
  const items = q.data?.items ?? []

  return (
    <div className="mx-auto max-w-7xl space-y-8">
      <div>
        <h1 className="text-2xl font-semibold tracking-tight">Streamers</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          Channels you clip. Each stream gets scanned once it ends (checked every {q.data?.interval_minutes ?? 15} minutes), and
          its top moments come to Telegram, or get clipped right away if you turn on auto-clip.
        </p>
      </div>
      <AddStreamer templates={templates.data?.items ?? []} />
      {q.isPending ? <Skeleton className="h-60" /> : !items.length ? (
        <div className="rounded-md border border-dashed border-border px-6 py-12 text-center text-sm text-muted-foreground">
          No streamers yet. Add a channel above, or send /watch &lt;channel link&gt; to the bot.
        </div>
      ) : (
        <div className="space-y-6">
          {items.map((s) => <StreamerCard key={s.id} streamer={s} templates={templates.data?.items ?? []} />)}
        </div>
      )}
    </div>
  )
}

function AddStreamer({ templates }: { templates: Template[] }) {
  const { refresh } = useStreamerActions()
  const [url, setUrl] = useState("")
  const [autoScan, setAutoScan] = useState(true)
  const [autoClip, setAutoClip] = useState(0)
  const [template, setTemplate] = useState("")
  const [backfill, setBackfill] = useState(2)
  const [mode, setMode] = useState<ScanMode>("signals")
  const add = useMutation({
    mutationFn: () => api.addStreamer({ url, auto_scan: autoScan, auto_clip: autoClip, template: template || null, backfill, mode }),
    onSuccess: (s) => { toast.success(`Watching ${s.label}`); setUrl(""); refresh() },
  })
  return (
    <form className="grid grid-cols-1 gap-4 rounded-md border border-border bg-card p-4 md:grid-cols-[minmax(0,2fr)_repeat(4,minmax(0,1fr))_auto] md:items-end"
      onSubmit={(e) => { e.preventDefault(); if (url.trim()) add.mutate() }}>
      <div className="space-y-1.5">
        <Label htmlFor="streamer-url">Channel</Label>
        <Input id="streamer-url" placeholder="https://twitch.tv/name, kick.com/name or youtube.com/@handle"
          value={url} onChange={(e) => setUrl(e.target.value)} />
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="streamer-backfill">Scan right away</Label>
        <NativeSelect id="streamer-backfill" value={backfill} onChange={(e) => setBackfill(Number(e.target.value))}>
          <option value={0}>No older VODs</option>
          {[1, 2, 3, 5].map((n) => <option key={n} value={n}>Latest {n} VOD{n > 1 ? "s" : ""}</option>)}
        </NativeSelect>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="streamer-autoclip">Auto-clip</Label>
        <NativeSelect id="streamer-autoclip" value={autoClip} onChange={(e) => setAutoClip(Number(e.target.value))}>
          <option value={0}>Off: send moments</option>
          {[1, 2, 3, 5].map((n) => <option key={n} value={n}>Top {n} moment{n > 1 ? "s" : ""}</option>)}
        </NativeSelect>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="streamer-mode">Moments first</Label>
        <NativeSelect id="streamer-mode" value={mode} onChange={(e) => setMode(e.target.value as ScanMode)}>
          <option value="signals">Chat and loudness</option>
          <option value="viewers">Viewer clips</option>
        </NativeSelect>
      </div>
      <div className="space-y-1.5">
        <Label htmlFor="streamer-template">Template</Label>
        <NativeSelect id="streamer-template" value={template} onChange={(e) => setTemplate(e.target.value)}>
          <option value="">Default</option>
          {templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
        </NativeSelect>
      </div>
      <div className="flex items-center gap-3 md:flex-col md:items-stretch">
        <label className="flex items-center gap-2 text-sm"><Checkbox checked={autoScan} onCheckedChange={(v) => setAutoScan(!!v)} /> Scan new streams</label>
        <Button type="submit" disabled={!url.trim() || add.isPending}>{add.isPending ? "Reading VODs…" : "Add streamer"}</Button>
      </div>
    </form>
  )
}

/** What Gemini should know about this streamer when it picks clips and writes their copy. */
function StreamerNotes({ streamer: s }: { streamer: Streamer }) {
  const { edit } = useStreamerActions()
  const [notes, setNotes] = useState(s.notes)
  const changed = notes.trim() !== s.notes
  return (
    <details className="border-b border-border px-4 py-3 text-sm">
      <summary className="cursor-pointer text-muted-foreground select-none hover:text-foreground">
        Notes for clipping{s.notes ? "" : " (none yet)"}
      </summary>
      <div className="mt-2 space-y-2">
        <Textarea aria-label={`Notes about ${s.label}`} rows={3} value={notes} onChange={(e) => setNotes(e.target.value)}
          placeholder="Who's who, nicknames, running jokes, the game. E.g. Kevin is his roommate and breaks everything; chat calls him the menace." />
        <div className="flex flex-wrap items-center gap-3">
          <Button size="sm" disabled={!changed || edit.isPending}
            onClick={() => edit.mutate({ id: s.id, body: { notes } }, { onSuccess: () => toast.success("Notes saved") })}>
            Save notes
          </Button>
          <span className="text-xs text-muted-foreground">
            Gemini reads these for every clip of this streamer, so moments and titles name the right people.
          </span>
        </div>
      </div>
    </details>
  )
}

function StreamerCard({ streamer: s, templates }: { streamer: Streamer; templates: Template[] }) {
  const { edit, check, backfill, scan, remove } = useStreamerActions()
  const [confirming, setConfirming] = useState(false)
  const [count, setCount] = useState(3)
  const unscanned = s.vods.filter((v) => !v.live && (!v.scan || v.scan.status === "failed")).length

  return (
    <section className="rounded-md border border-border bg-card">
      <header className="flex flex-wrap items-center gap-x-6 gap-y-3 border-b border-border px-4 py-3">
        <div className="mr-auto min-w-0">
          <a href={s.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-base font-medium hover:underline">
            <span className="text-muted-foreground">{PLATFORM[s.platform]}</span> {s.channel} <ExternalLink className="size-3.5 text-muted-foreground" />
          </a>
          <p className={cn("text-xs", s.check_error ? "text-destructive" : "text-muted-foreground")}>
            {s.check_error ? `Couldn't read the VOD list: ${s.check_error}` : s.checked ? `Checked ${ago(s.checked)}` : "Not checked yet"}
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox checked={s.auto_scan} onCheckedChange={(v) => edit.mutate({ id: s.id, body: { auto_scan: !!v } })} />
          Scan new streams
        </label>
        <div className="flex items-center gap-2 text-sm">
          <span className="text-muted-foreground">Auto-clip</span>
          <NativeSelect aria-label="Auto-clip" className="h-8 w-auto" value={s.auto_clip}
            onChange={(e) => edit.mutate({ id: s.id, body: { auto_clip: Number(e.target.value) } })}>
            <option value={0}>Off</option>
            {[1, 2, 3, 5].map((n) => <option key={n} value={n}>Top {n}</option>)}
          </NativeSelect>
          <NativeSelect aria-label="Moments first" className="h-8 w-auto" value={s.mode}
            title="How its scans rank moments (and what auto-clip clips)"
            onChange={(e) => edit.mutate({ id: s.id, body: { mode: e.target.value as ScanMode } })}>
            <option value="signals">Chat and loudness</option>
            <option value="viewers">Viewer clips first</option>
          </NativeSelect>
          <NativeSelect aria-label="Template" className="h-8 w-auto" value={s.template ?? ""}
            onChange={(e) => edit.mutate({ id: s.id, body: { template: e.target.value } })}>
            <option value="">Default template</option>
            {templates.map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
          </NativeSelect>
        </div>
        <div className="flex items-center gap-1.5">
          <Button size="sm" variant="outline" onClick={() => check.mutate(s.id)} disabled={check.isPending}>
            <RefreshCw className={cn(check.isPending && "animate-spin")} /> Check now
          </Button>
          <Button size="icon" variant="ghost" aria-label={`Remove ${s.label}`} onClick={() => setConfirming(true)}><Trash2 /></Button>
        </div>
      </header>

      <StreamerNotes key={s.notes} streamer={s} />

      {!s.vods.length ? (
        <p className="px-4 py-6 text-sm text-muted-foreground">No past broadcasts found yet.</p>
      ) : (
        <ul className="divide-y divide-border">
          {s.vods.map((v) => <VodRow key={v.vod_id} vod={v} onScan={() => scan.mutate({ id: s.id, vodId: v.vod_id })} busy={scan.isPending} />)}
        </ul>
      )}

      {unscanned > 0 && (
        <footer className="flex flex-wrap items-center gap-2 border-t border-border px-4 py-3 text-sm">
          <span className="text-muted-foreground">{unscanned} listed VOD{unscanned > 1 ? "s" : ""} not scanned.</span>
          <NativeSelect aria-label="How many" className="h-8 w-auto" value={count} onChange={(e) => setCount(Number(e.target.value))}>
            {[1, 2, 3, 5, 10].map((n) => <option key={n} value={n}>{n}</option>)}
          </NativeSelect>
          <Button size="sm" variant="outline" onClick={() => backfill.mutate({ id: s.id, count })} disabled={backfill.isPending}>
            Scan the latest {count}
          </Button>
          {s.auto_clip > 0 && <span className="text-xs text-muted-foreground">Auto-clip applies to these too.</span>}
        </footer>
      )}

      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Stop watching {s.label}?</AlertDialogTitle>
            <AlertDialogDescription>Its scans, moments and clips stay. New streams won't be scanned.</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep</AlertDialogCancel>
            <AlertDialogAction onClick={() => remove.mutate(s.id)}>Stop watching</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </section>
  )
}

function VodRow({ vod, onScan, busy }: { vod: StreamerVod; onScan: () => void; busy: boolean }) {
  const when = vod.published ?? vod.seen
  return (
    <li className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2.5 text-sm">
      <div className="min-w-0 flex-1">
        <a href={vod.url} target="_blank" rel="noreferrer" className="block truncate hover:underline">{vod.title || vod.vod_id}</a>
        <span className="text-xs text-muted-foreground">
          {ago(when)}{vod.duration ? <> · <span className="timecode">{tc(vod.duration)}</span></> : null}
        </span>
      </div>
      <div className="flex items-center gap-3">
        {vod.live ? (
          <span className="inline-flex items-center gap-2 text-destructive"><Light tone="bad" className="animate-pulse" /> Live, scanned when it ends</span>
        ) : vod.scan && vod.scan.status !== "failed" ? (
          vod.scan.status === "done" ? (
            <Link to={`/scans/${vod.scan.id}`} className="text-primary hover:underline">
              {vod.scan.moment_count} moment{vod.scan.moment_count === 1 ? "" : "s"}
            </Link>
          ) : <Status status={vod.scan.status} />
        ) : (
          <>
            {vod.scan?.status === "failed" && <span className="max-w-64 truncate text-xs text-destructive" title={vod.scan.error ?? undefined}>Scan failed</span>}
            <Button size="sm" variant="outline" onClick={onScan} disabled={busy}>{vod.scan ? "Scan again" : "Scan"}</Button>
          </>
        )}
      </div>
    </li>
  )
}
