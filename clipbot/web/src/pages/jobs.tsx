import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { Trash2 } from "lucide-react"
import { useState } from "react"
import { Link, useNavigate, useParams } from "react-router"
import { toast } from "sonner"

import { ClipThumb } from "@/components/clip-thumb"
import { NativeSelect } from "@/components/native-select"
import { LogLines } from "@/components/log-lines"
import { isActive, Status } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, type Job, type JobDetail } from "@/lib/api"
import { ago, clock, elapsed, parseTc, platformName, size, sourceLabel, tc } from "@/lib/format"
import { useDeleteJobFiles } from "@/pages/storage"

type Filter = "all" | "active" | "done" | "failed"

function useJobActions() {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["jobs"] })
    qc.invalidateQueries({ queryKey: ["job"] })
    qc.invalidateQueries({ queryKey: ["overview"] })
  }
  const retry = useMutation({
    mutationFn: api.retryJob,
    onSuccess: (job) => { toast.success(`Job ${job.id} queued`); refresh() },
  })
  const cancel = useMutation({
    mutationFn: api.cancelJob,
    onSuccess: (job) => { toast(`Job ${job.id} cancelled`); refresh() },
  })
  const remove = useMutation({
    mutationFn: api.deleteJob,
    onSuccess: (r, id) => { toast(`Job ${id} deleted`, { description: r.freed ? `Freed ${size(r.freed)}.` : undefined }); refresh() },
  })
  const clearFailed = useMutation({
    mutationFn: api.clearFailedJobs,
    onSuccess: (r) => {
      toast(`Deleted ${r.deleted} job${r.deleted === 1 ? "" : "s"}`, { description: r.freed ? `Freed ${size(r.freed)}.` : undefined })
      refresh()
    },
  })
  return { retry, cancel, remove, clearFailed }
}

const deletable = (j: Job) => j.status === "failed" || j.status === "cancelled"

export function JobsPage() {
  const { jobId } = useParams()
  const navigate = useNavigate()
  const [filter, setFilter] = useState<Filter>("all")
  const [creating, setCreating] = useState(false)
  const [clearing, setClearing] = useState(false)
  const { remove, clearFailed } = useJobActions()
  const jobs = useQuery({ queryKey: ["jobs"], queryFn: () => api.jobs(), refetchInterval: 5000 })
  const items = (jobs.data?.items ?? []).filter((j) =>
    filter === "all" ? true : filter === "active" ? isActive(j.status)
      : filter === "done" ? j.status === "done" : deletable(j))
  const failedCount = (jobs.data?.items ?? []).filter(deletable).length

  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <div className="flex flex-wrap items-center gap-4">
        <h1 className="mr-auto text-2xl font-semibold tracking-tight">Jobs</h1>
        <ToggleGroup type="single" variant="outline" size="sm" value={filter}
          onValueChange={(v) => v && setFilter(v as Filter)}>
          <ToggleGroupItem value="all">All</ToggleGroupItem>
          <ToggleGroupItem value="active">Running</ToggleGroupItem>
          <ToggleGroupItem value="done">Done</ToggleGroupItem>
          <ToggleGroupItem value="failed">Failed</ToggleGroupItem>
        </ToggleGroup>
        {failedCount > 0 && (
          <Button variant="outline" onClick={() => setClearing(true)} disabled={clearFailed.isPending}>
            <Trash2 /> Clear failed ({failedCount})
          </Button>
        )}
        <Button onClick={() => setCreating(true)}>New job</Button>
      </div>

      {jobs.isPending ? null : !items.length ? (
        <div className="rounded-md border border-dashed border-border px-6 py-14 text-center text-sm text-muted-foreground">
          {filter === "all" ? "No jobs yet. Start one here, send a link in Telegram, or clip a moment from Scans." : "No jobs match this filter."}
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent">
                <TableHead className="w-44">Status</TableHead>
                <TableHead>Source</TableHead>
                <TableHead className="w-40">Range</TableHead>
                <TableHead className="w-20 text-right">Clips</TableHead>
                <TableHead className="w-32 text-right">Started</TableHead>
                <TableHead className="w-12"><span className="sr-only">Delete</span></TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {items.map((job) => (
                <TableRow key={job.id} className={cn("cursor-pointer", job.id === jobId && "bg-accent")}
                  onClick={() => navigate(`/jobs/${job.id}`)}>
                  <TableCell><Status status={job.status} /></TableCell>
                  <TableCell className="max-w-0">
                    <div className="truncate">
                      <span className="text-muted-foreground">{platformName[job.platform]}</span>{" "}
                      {sourceLabel(job.source_url)}
                    </div>
                    {job.status === "failed" && job.error && (
                      <div className="truncate text-xs text-destructive">{job.error}</div>
                    )}
                  </TableCell>
                  <TableCell className="timecode text-muted-foreground">
                    {job.range ? `${tc(job.range.start)}–${tc(job.range.end)}` : "Whole video"}
                  </TableCell>
                  <TableCell className="text-right tabular-nums">
                    {job.status === "done" ? job.clip_count : <span className="text-muted-foreground">{job.target_clips}</span>}
                  </TableCell>
                  <TableCell className="text-right text-muted-foreground">{ago(job.created)}</TableCell>
                  <TableCell className="py-0 text-right">
                    {deletable(job) && (
                      <Button variant="ghost" size="icon" aria-label={`Delete job ${job.id}`} disabled={remove.isPending}
                        onClick={(ev) => { ev.stopPropagation(); remove.mutate(job.id) }}>
                        <Trash2 />
                      </Button>
                    )}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}

      <JobSheet jobId={jobId} onClose={() => navigate("/jobs")} />
      <NewJobDialog open={creating} onOpenChange={setCreating} />
      <Dialog open={clearing} onOpenChange={setClearing}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>Delete {failedCount} failed job{failedCount === 1 ? "" : "s"}?</DialogTitle>
            <DialogDescription>
              Failed and cancelled jobs go for good: their files, their logs and anything they left unreviewed. Jobs
              with approved clips stay.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setClearing(false)}>Keep them</Button>
            <Button variant="destructive" disabled={clearFailed.isPending}
              onClick={() => clearFailed.mutate(undefined, { onSettled: () => setClearing(false) })}>
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}

function JobSheet({ jobId, onClose }: { jobId?: string; onClose: () => void }) {
  const job = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => api.job(jobId!),
    enabled: !!jobId,
    refetchInterval: (q) => (q.state.data && isActive(q.state.data.status) ? 3000 : false),
  })
  const { retry, cancel, remove } = useJobActions()
  const d = job.data

  return (
    <Sheet open={!!jobId} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="w-full gap-0 overflow-y-auto sm:max-w-xl">
        <SheetHeader className="border-b border-border">
          <SheetTitle>Job {jobId}</SheetTitle>
          <SheetDescription asChild>
            <div>{d ? <Status status={d.status} /> : "Loading…"}</div>
          </SheetDescription>
        </SheetHeader>
        {d && (
          <div className="space-y-6 p-4 text-sm">
            <dl className="grid grid-cols-[7rem_1fr] gap-y-2">
              <dt className="text-muted-foreground">Source</dt>
              <dd className="min-w-0 break-all">
                <a href={d.source_url} target="_blank" rel="noreferrer" className="hover:underline">{d.source_url}</a>
              </dd>
              <dt className="text-muted-foreground">Range</dt>
              <dd className="timecode">{d.range ? `${tc(d.range.start)}–${tc(d.range.end)} (${tc(d.range.end - d.range.start)})` : "Whole video"}</dd>
              <dt className="text-muted-foreground">Clips asked</dt>
              <dd>{d.target_clips}</dd>
              <dt className="text-muted-foreground">Started</dt>
              <dd>{clock(d.created)}{isActive(d.status) && <span className="ml-2 text-muted-foreground timecode">running {elapsed(d.created)}</span>}</dd>
              <dt className="text-muted-foreground">Template</dt>
              <dd>{d.template ?? "default"}</dd>
              {d.note && (<><dt className="text-muted-foreground">Looking for</dt><dd>{d.note}</dd></>)}
              {d.openshorts_job && (<><dt className="text-muted-foreground">OpenShorts job</dt><dd className="font-mono text-xs">{d.openshorts_job}</dd></>)}
              {d.retry_of && (<><dt className="text-muted-foreground">Retry of</dt><dd><Link to={`/jobs/${d.retry_of}`} className="hover:underline">job {d.retry_of}</Link></dd></>)}
              {d.retried_by && (<><dt className="text-muted-foreground">Retried as</dt><dd><Link to={`/jobs/${d.retried_by}`} className="hover:underline">job {d.retried_by}</Link></dd></>)}
            </dl>

            {d.error && (
              <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-destructive">
                <p className="break-words whitespace-pre-line">{d.error}</p>
              </div>
            )}

            <div className="flex gap-2">
              {(d.status === "failed" || d.status === "cancelled") && !d.retried_by && (
                <Button onClick={() => retry.mutate(d.id)} disabled={retry.isPending}>Retry job</Button>
              )}
              {isActive(d.status) && (
                <Button variant="outline" onClick={() => cancel.mutate(d.id)} disabled={cancel.isPending}>Cancel job</Button>
              )}
              {deletable(d) && (
                <Button variant="ghost" disabled={remove.isPending}
                  onClick={() => remove.mutate(d.id, { onSuccess: onClose })}>
                  <Trash2 /> Delete job
                </Button>
              )}
            </div>

            {d.picks.length > 0 && (
              <section>
                <h3 className="mb-1 font-medium">Gemini's picks</h3>
                <p className="mb-2 text-xs text-muted-foreground">
                  {d.status === "queued" ? "Spotted in the scan. Gemini watches the range again to cut them exactly." : "What Gemini watched and chose to cut."}
                </p>
                <ul className="space-y-1.5">
                  {d.picks.map((p, i) => (
                    <li key={i} className="flex gap-3">
                      <span className="timecode w-24 shrink-0 text-muted-foreground">{tc(p.start)}–{tc(p.end)}</span>
                      <span className="min-w-0 flex-1">{p.what_happened}</span>
                      <span className="shrink-0 tabular-nums text-muted-foreground" title="How well Gemini thinks it would do (0-100)">{p.score}</span>
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {d.clips.length > 0 && (
              <section>
                <h3 className="mb-2 font-medium">Clips</h3>
                <div className="grid grid-cols-4 gap-2">
                  {d.clips.map((c) => (
                    <Link key={c.id} to="/review" className="space-y-1">
                      <ClipThumb clip={c} />
                      <Status status={c.status} className="text-xs" />
                    </Link>
                  ))}
                </div>
              </section>
            )}

            <JobFilesSection job={d} />

            {d.openshorts && (
              <section>
                <h3 className="mb-2 font-medium">OpenShorts log</h3>
                <pre className="max-h-72 overflow-auto rounded-md border border-border bg-background p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-muted-foreground">
                  {d.openshorts.logs.length ? d.openshorts.logs.join("\n") : "No output yet."}
                </pre>
              </section>
            )}

            <section>
              <h3 className="mb-2 font-medium">clipbot log</h3>
              {d.logs.length ? <LogLines items={d.logs} compact /> : <p className="text-muted-foreground">Nothing logged for this job.</p>}
            </section>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}

function JobFilesSection({ job }: { job: JobDetail }) {
  const remove = useDeleteJobFiles()
  const f = job.files
  if (!f.openshorts && !f.rejected && !f.approved && !f.pending) return null
  const finished = ["done", "failed", "cancelled"].includes(job.status)
  return (
    <section>
      <h3 className="mb-2 font-medium">Files</h3>
      <dl className="grid grid-cols-[7rem_1fr] gap-y-1">
        <dt className="text-muted-foreground">Source + work</dt><dd className="tabular-nums">{size(f.openshorts)}</dd>
        <dt className="text-muted-foreground">Skipped clips</dt><dd className="tabular-nums">{size(f.rejected)}</dd>
        <dt className="text-muted-foreground">Kept clips</dt><dd className="tabular-nums">{size(f.approved + f.pending)}</dd>
      </dl>
      <div className="mt-3 flex items-center gap-3">
        {f.deleting ? <span className="text-muted-foreground">Deleting…</span> : (
          <Button variant="outline" size="sm" disabled={!finished || !f.freeable || remove.isPending}
            onClick={() => remove.mutate(job.id)}>
            Delete {f.freeable ? size(f.freeable) : "files"}
          </Button>
        )}
        <span className="text-xs text-muted-foreground">{finished ? "Approved and unreviewed clips stay." : "Available once the job has finished."}</span>
      </div>
    </section>
  )
}

export function NewJobDialog({ open, onOpenChange, initial }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  initial?: { url?: string; start?: string; end?: string }
}) {
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [url, setUrl] = useState(initial?.url ?? "")
  const [start, setStart] = useState(initial?.start ?? "")
  const [end, setEnd] = useState(initial?.end ?? "")
  const [clips, setClips] = useState("3")
  const [template, setTemplate] = useState("")
  const [note, setNote] = useState("")
  const templates = useQuery({ queryKey: ["templates"], queryFn: api.templates, enabled: open })
  const s = parseTc(start)
  const e = parseTc(end)
  const length = s !== null && e !== null && e > s ? e - s : null

  const create = useMutation({
    mutationFn: () => api.createJob({
      url, start: start || undefined, end: end || undefined, clips: Number(clips) || undefined, template: template || undefined,
      note: note.trim() || undefined,
    }),
    onSuccess: (job: Job) => {
      toast.success(`Job ${job.id} queued`)
      qc.invalidateQueries({ queryKey: ["jobs"] })
      qc.invalidateQueries({ queryKey: ["overview"] })
      onOpenChange(false)
      setUrl(""); setStart(""); setEnd(""); setNote("")
      navigate(`/jobs/${job.id}`)
    },
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>New job</DialogTitle>
          <DialogDescription>OpenShorts cuts vertical clips from the link. A range is much faster on this server.</DialogDescription>
        </DialogHeader>
        <form id="new-job" className="space-y-4" onSubmit={(ev) => { ev.preventDefault(); create.mutate() }}>
          <div className="space-y-1.5">
            <Label htmlFor="job-url">Video link</Label>
            <Input id="job-url" placeholder="https://www.twitch.tv/videos/…" value={url} onChange={(ev) => setUrl(ev.target.value)} autoFocus />
          </div>
          <div className="grid grid-cols-3 gap-3">
            <div className="space-y-1.5">
              <Label htmlFor="job-start">Start</Label>
              <Input id="job-start" placeholder="1:02:00" value={start} onChange={(ev) => setStart(ev.target.value)}
                className="timecode" aria-invalid={!!start && s === null} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="job-end">End</Label>
              <Input id="job-end" placeholder="1:12:00" value={end} onChange={(ev) => setEnd(ev.target.value)}
                className="timecode" aria-invalid={!!end && e === null} />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="job-clips">Clips</Label>
              <Input id="job-clips" type="number" min={1} max={15} value={clips} onChange={(ev) => setClips(ev.target.value)} />
            </div>
          </div>
          <p className="text-sm text-muted-foreground">
            {length !== null ? <>Clips <span className="timecode">{tc(length)}</span> of the video.</> : "Leave start and end empty to use the whole video."}
          </p>
          <div className="space-y-1.5">
            <Label htmlFor="job-note">What to look for <span className="font-normal text-muted-foreground">(optional)</span></Label>
            <Input id="job-note" placeholder="his friend smashes the monitor" value={note} maxLength={300}
              onChange={(ev) => setNote(ev.target.value)} />
            <p className="text-xs text-muted-foreground">Gemini watches the range for it and cuts it exactly.</p>
          </div>
          <div className="space-y-1.5">
            <Label htmlFor="job-template">Template</Label>
            <NativeSelect id="job-template" value={template} onChange={(ev) => setTemplate(ev.target.value)}>
              <option value="">Default{templates.data ? ` (${templates.data.items.find((t) => t.default)?.name ?? "Standard"})` : ""}</option>
              {(templates.data?.items ?? []).filter((t) => !t.default).map((t) => <option key={t.id} value={t.id}>{t.name}</option>)}
            </NativeSelect>
          </div>
        </form>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button type="submit" form="new-job" disabled={!url || create.isPending}>Start job</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
