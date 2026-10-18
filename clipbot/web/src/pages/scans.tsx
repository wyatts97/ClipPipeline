import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { useEffect, useState } from "react"
import { Link, Navigate, useNavigate, useParams } from "react-router"
import { toast } from "sonner"

import { HeatTimeline, type Selection } from "@/components/heat-timeline"
import { LogLines } from "@/components/log-lines"
import { isActive, Status } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, type Moment, type Scan, type ScanDetail } from "@/lib/api"
import { ago, parseTc, platformName, sourceLabel, tc, timestampLink } from "@/lib/format"

export function ScansPage() {
  const { scanId } = useParams()
  const [creating, setCreating] = useState(false)
  const scans = useQuery({ queryKey: ["scans"], queryFn: api.scans, refetchInterval: 5000 })
  const items = scans.data?.items ?? []

  if (!scanId && items.length) {
    const first = items.find((s) => s.status === "done") ?? items[0]
    return <Navigate to={`/scans/${first.id}`} replace />
  }

  return (
    <div className="mx-auto grid max-w-[96rem] grid-cols-1 gap-8 xl:grid-cols-[17rem_minmax(0,1fr)]">
      <aside className="space-y-3">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">Scans</h1>
          <Button size="sm" onClick={() => setCreating(true)}>New scan</Button>
        </div>
        {scans.isPending ? null : !items.length ? (
          <p className="text-sm text-muted-foreground">
            No scans yet. A scan reads a stream's chat replay, loudness and viewer clips, then marks the moments worth clipping.
          </p>
        ) : (
          <ol className="flex gap-2 overflow-x-auto pb-2 xl:max-h-[calc(100vh-12rem)] xl:flex-col xl:overflow-x-visible xl:overflow-y-auto xl:pb-0">
            {items.map((s) => <ScanRow key={s.id} scan={s} active={s.id === scanId} />)}
          </ol>
        )}
      </aside>
      <section className="min-w-0">
        {scanId ? <ScanView scanId={scanId} /> : null}
      </section>
      <NewScanDialog open={creating} onOpenChange={setCreating} />
    </div>
  )
}

function ScanRow({ scan, active }: { scan: Scan; active: boolean }) {
  return (
    <li className="shrink-0">
      <Link to={`/scans/${scan.id}`} aria-current={active}
        className={cn("block w-64 rounded-md border px-3 py-2.5 transition-colors xl:w-auto",
          active ? "border-primary bg-accent" : "border-transparent hover:bg-accent/60")}>
        <span className="line-clamp-2 text-sm">{scan.title ?? sourceLabel(scan.source_url)}</span>
        <span className="mt-1.5 flex items-center gap-3 text-xs text-muted-foreground">
          {isActive(scan.status) || scan.status === "failed" ? <Status status={scan.status} className="text-xs" />
            : <span>{platformName[scan.platform]}</span>}
          {scan.duration ? <span className="timecode">{tc(scan.duration)}</span> : null}
          {scan.status === "done" && <span>{scan.moment_count} moments</span>}
          <span className="ml-auto">{ago(scan.created)}</span>
        </span>
      </Link>
    </li>
  )
}

function ScanView({ scanId }: { scanId: string }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const scan = useQuery({
    queryKey: ["scan", scanId],
    queryFn: () => api.scan(scanId),
    refetchInterval: (q) => (q.state.data && (isActive(q.state.data.status) || q.state.data.moments.some((m) => m.job && isActive(m.job.status))) ? 4000 : false),
  })
  const [selection, setSelection] = useState<Selection | null>(null)
  const [activeMoment, setActiveMoment] = useState<string | null>(null)
  useEffect(() => { setSelection(null); setActiveMoment(null) }, [scanId])

  const rescan = useMutation({
    mutationFn: () => api.rescan(scanId),
    onSuccess: (s) => {
      toast.success(`Scan ${s.id} queued`)
      qc.invalidateQueries({ queryKey: ["scans"] })
      navigate(`/scans/${s.id}`)
    },
  })

  const d = scan.data
  if (!d) return scan.isError ? <p className="text-sm text-destructive">{scan.error.message}</p> : null

  const pick = (m: Moment) => { setActiveMoment(m.id); setSelection({ start: m.start, end: m.end }) }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-start gap-x-6 gap-y-3">
        <div className="min-w-0 flex-1">
          <h2 className="text-xl font-semibold tracking-tight break-words">{d.title ?? sourceLabel(d.source_url)}</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            {platformName[d.platform]}{" "}
            <a href={d.source_url} target="_blank" rel="noreferrer" className="text-foreground hover:underline">{sourceLabel(d.source_url)}</a>
            {d.duration ? <span className="ml-3 timecode">{tc(d.duration)} long</span> : null}
            <span className="ml-3">scanned {ago(d.updated)}</span>
          </p>
          {d.signals.length > 0 && (
            <p className="mt-1 text-sm text-muted-foreground">Read from {d.signals.join(", ")}</p>
          )}
        </div>
        <Button variant="outline" onClick={() => rescan.mutate()} disabled={rescan.isPending || isActive(d.status)}>Scan again</Button>
      </header>

      {isActive(d.status) && (
        <div className="flex items-center gap-3 rounded-md border border-border bg-card px-4 py-3 text-sm">
          <Status status={d.status} />
          <span className="text-muted-foreground">Reading the chat replay and loudness. A long stream takes a few minutes.</span>
        </div>
      )}
      {d.status === "failed" && (
        <div className="rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
          The scan failed: {d.error}
        </div>
      )}

      {d.timeline && (
        <div className="rounded-md border border-border bg-card px-3 pt-3 pb-1">
          <HeatTimeline timeline={d.timeline} moments={d.moments} selection={selection} onSelect={(s) => { setSelection(s); setActiveMoment(null) }}
            activeMoment={activeMoment} onPickMoment={pick} />
        </div>
      )}

      {d.timeline && (
        <SelectionPanel scan={d} selection={selection} setSelection={setSelection}
          moment={d.moments.find((m) => m.id === activeMoment) ?? null} />
      )}

      {d.status === "done" && (
        <Moments scan={d} activeMoment={activeMoment} onPick={pick} />
      )}

      {d.logs.some((l) => l.level !== "INFO") && (
        <section>
          <h3 className="mb-2 font-medium">Scan log</h3>
          <LogLines items={d.logs} compact />
        </section>
      )}
    </div>
  )
}

function useClipFromScan(scanId: string) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: { start: number | string; end: number | string; clips: number; moment_id?: string }) =>
      api.clipFromScan(scanId, body),
    onSuccess: (job) => {
      toast.success(`Job ${job.id} queued`, {
        description: job.range ? `${tc(job.range.start)}–${tc(job.range.end)}, ${job.target_clips} clip${job.target_clips > 1 ? "s" : ""}` : undefined,
      })
      qc.invalidateQueries({ queryKey: ["scan", scanId] })
      qc.invalidateQueries({ queryKey: ["jobs"] })
      qc.invalidateQueries({ queryKey: ["overview"] })
    },
  })
}

function SelectionPanel({ scan, selection, setSelection, moment }: {
  scan: ScanDetail
  selection: Selection | null
  setSelection: (s: Selection | null) => void
  moment: Moment | null
}) {
  const clip = useClipFromScan(scan.id)
  const [count, setCount] = useState("1")
  const [inText, setInText] = useState("")
  const [outText, setOutText] = useState("")
  useEffect(() => {
    setInText(selection ? tc(selection.start) : "")
    setOutText(selection ? tc(selection.end) : "")
  }, [selection])

  if (!selection) {
    return (
      <p className="text-sm text-muted-foreground">
        Drag across the timeline to choose your own range, click a spot to drop a 2:15 window there, or click a numbered moment.
      </p>
    )
  }
  const length = selection.end - selection.start
  const tooShort = length < 45
  const commit = () => {
    const s = parseTc(inText)
    const e = parseTc(outText)
    if (s !== null && e !== null && e > s) setSelection({ start: Math.max(0, s), end: Math.min(scan.duration ?? e, e) })
    else { setInText(tc(selection.start)); setOutText(tc(selection.end)) }
  }

  return (
    <div className="flex flex-wrap items-end gap-x-5 gap-y-3 rounded-md border border-primary/40 bg-primary/5 px-4 py-3">
      <div className="space-y-1">
        <Label htmlFor="sel-in" className="text-xs text-muted-foreground">In</Label>
        <Input id="sel-in" value={inText} onChange={(e) => setInText(e.target.value)} onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && commit()} className="h-8 w-24 timecode" />
      </div>
      <div className="space-y-1">
        <Label htmlFor="sel-out" className="text-xs text-muted-foreground">Out</Label>
        <Input id="sel-out" value={outText} onChange={(e) => setOutText(e.target.value)} onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && commit()} className="h-8 w-24 timecode" />
      </div>
      <div className="pb-1.5 text-sm">
        <span className={cn("timecode", tooShort && "text-destructive")}>{tc(length)}</span>
        <span className="ml-1 text-muted-foreground">{tooShort ? "too short, needs 0:45" : moment ? `moment ${moment.rank}` : "selected"}</span>
      </div>
      <div className="space-y-1">
        <span className="block text-xs text-muted-foreground">Clips</span>
        <ToggleGroup type="single" variant="outline" size="sm" value={count} onValueChange={(v) => v && setCount(v)}>
          {["1", "2", "3"].map((n) => <ToggleGroupItem key={n} value={n} className="w-8">{n}</ToggleGroupItem>)}
        </ToggleGroup>
      </div>
      <div className="ml-auto flex gap-2">
        <Button variant="ghost" asChild>
          <a href={timestampLink(scan.platform, scan.vod_id, scan.source_url, selection.start)} target="_blank" rel="noreferrer">Watch from in-point</a>
        </Button>
        <Button variant="ghost" onClick={() => setSelection(null)}>Clear</Button>
        <Button disabled={tooShort || clip.isPending || !!moment?.job}
          onClick={() => clip.mutate({ start: selection.start, end: selection.end, clips: Number(count), moment_id: moment?.id })}>
          {moment?.job ? "Already clipped" : "Clip this range"}
        </Button>
      </div>
    </div>
  )
}

function Moments({ scan, activeMoment, onPick }: { scan: ScanDetail; activeMoment: string | null; onPick: (m: Moment) => void }) {
  const clip = useClipFromScan(scan.id)
  if (!scan.moments.length) {
    return <p className="text-sm text-muted-foreground">Nothing stood out above this stream's normal level. Drag on the timeline to clip a range yourself.</p>
  }
  return (
    <section>
      <h3 className="mb-2 font-medium">Top moments</h3>
      <ol className="divide-y divide-border rounded-md border border-border bg-card">
        {scan.moments.map((m) => {
          const d = m.details
          const reasons = [
            d.chat_ratio ? `chat ${d.chat_ratio}× normal` : null,
            d.loud ? "loud" : null,
            d.clips ? `${d.clips} viewer clip${d.clips > 1 ? "s" : ""}` : null,
            d.replayed ? "most replayed" : null,
          ].filter(Boolean)
          return (
            <li key={m.id} className={cn("grid gap-3 px-4 py-3 sm:grid-cols-[2.5rem_minmax(0,1fr)_auto] sm:items-center",
              m.id === activeMoment && "bg-accent/60")}>
              <button onClick={() => onPick(m)} aria-label={`Select moment ${m.rank}`}
                className={cn("grid size-8 place-items-center rounded-md border text-sm font-semibold",
                  m.id === activeMoment ? "border-primary bg-primary text-primary-foreground" : "border-border hover:border-primary")}>
                {m.rank}
              </button>
              <div className="min-w-0 text-sm">
                <button onClick={() => onPick(m)} className="text-left hover:underline">
                  Peak at <span className="timecode font-medium">{tc(m.peak)}</span>
                  <span className="ml-2 text-muted-foreground timecode">window {tc(m.start)}–{tc(m.end)}</span>
                </button>
                <p className="mt-0.5 text-muted-foreground">
                  {reasons.join(", ")}
                  {d.top_tokens?.length ? <span className="ml-2 text-foreground/80">“{d.top_tokens.map(([t, n]) => `${t} ×${n}`).join("  ")}”</span> : null}
                </p>
              </div>
              <div className="flex flex-wrap items-center gap-2">
                {m.job ? (
                  <Link to={`/jobs/${m.job.id}`} className="text-sm hover:underline"><Status status={m.job.status} /></Link>
                ) : (
                  <>
                    <Button size="sm" disabled={clip.isPending}
                      onClick={() => clip.mutate({ start: m.start, end: m.end, clips: 1, moment_id: m.id })}>Clip it</Button>
                    <Button size="sm" variant="outline" disabled={clip.isPending}
                      onClick={() => clip.mutate({ start: Math.max(0, m.start - 60), end: Math.min(scan.duration ?? m.end + 60, m.end + 60), clips: 2, moment_id: m.id })}>
                      Wider ×2
                    </Button>
                  </>
                )}
                <Button size="sm" variant="ghost" asChild>
                  <a href={m.link} target="_blank" rel="noreferrer">Watch</a>
                </Button>
              </div>
            </li>
          )
        })}
      </ol>
    </section>
  )
}

function NewScanDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [url, setUrl] = useState("")
  const create = useMutation({
    mutationFn: () => api.createScan(url),
    onSuccess: (scan) => {
      toast.success(`Scan ${scan.id} queued`)
      qc.invalidateQueries({ queryKey: ["scans"] })
      onOpenChange(false)
      setUrl("")
      navigate(`/scans/${scan.id}`)
    },
  })
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New scan</DialogTitle>
          <DialogDescription>Paste a finished stream: a Twitch or Kick VOD, or a YouTube stream replay.</DialogDescription>
        </DialogHeader>
        <form id="new-scan" onSubmit={(e) => { e.preventDefault(); create.mutate() }} className="space-y-1.5">
          <Label htmlFor="scan-url">VOD link</Label>
          <Input id="scan-url" autoFocus placeholder="https://www.twitch.tv/videos/…" value={url} onChange={(e) => setUrl(e.target.value)} />
        </form>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="new-scan" disabled={!url || create.isPending}>Start scan</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
