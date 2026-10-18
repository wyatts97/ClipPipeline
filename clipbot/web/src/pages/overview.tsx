import { useMutation, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { Link } from "react-router"
import { toast } from "sonner"

import { ClipThumb } from "@/components/clip-thumb"
import { useOverview } from "@/components/shell"
import { Status } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { api, type Activity, type Clip, type Overview, type Problem } from "@/lib/api"
import { ago, clock, elapsed, platformName, sourceLabel, tc } from "@/lib/format"

export function OverviewPage() {
  const { data } = useOverview()
  if (!data) return <OverviewSkeleton />

  return (
    <div className="mx-auto grid max-w-7xl grid-cols-1 gap-10 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <div className="space-y-10">
        <NeedsYou count={data.pending_clips} preview={data.pending_preview} />
        <Working data={data} />
        <Problems problems={data.problems} />
      </div>
      <aside className="space-y-10">
        <Week week={data.week} errors={data.errors_24h} />
        <ActivityList items={data.activity} />
      </aside>
    </div>
  )
}

function NeedsYou({ count, preview }: { count: number; preview: Clip[] }) {
  if (!count) {
    return (
      <section>
        <h1 className="text-2xl font-semibold tracking-tight">Nothing waiting on you</h1>
        <p className="mt-2 text-muted-foreground">
          New clips land in Review and in Telegram. Find moments in a stream from <Link to="/scans" className="text-primary hover:underline">Scans</Link>.
        </p>
      </section>
    )
  }
  return (
    <section className="flex flex-col gap-6 sm:flex-row sm:items-end">
      <div className="sm:w-56">
        <h1 className="text-[2.75rem] leading-none font-semibold tracking-tight tabular-nums">{count}</h1>
        <p className="mt-2 text-lg">{count === 1 ? "clip needs" : "clips need"} your call</p>
        <Button asChild className="mt-5">
          <Link to="/review">Review clips</Link>
        </Button>
      </div>
      <Link to="/review" className="flex min-w-0 flex-1 gap-2 overflow-hidden" aria-label="Open Review">
        {preview.slice(0, 6).map((clip) => (
          <ClipThumb key={clip.id} clip={clip} className="w-[18%] min-w-16 shrink-0" />
        ))}
      </Link>
    </section>
  )
}

function Working({ data }: { data: Overview }) {
  const running = data.scans
  const empty = !data.working.length && !running.length && !data.queued_jobs
  return (
    <section>
      <h2 className="mb-3 text-base font-medium">Working on</h2>
      {empty ? (
        <p className="text-sm text-muted-foreground">Nothing running. Send a link in Telegram, or start a job from Jobs.</p>
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border bg-card">
          {data.working.map((job) => (
            <li key={job.id}>
              <Link to={`/jobs/${job.id}`} className="block px-4 py-3 hover:bg-accent/50">
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <Status status={job.status} />
                  <span className="font-medium">Job {job.id}</span>
                  <span className="text-sm text-muted-foreground">
                    {platformName[job.platform]} {sourceLabel(job.source_url)}
                    {job.range && <span className="ml-2 timecode">{tc(job.range.start)}–{tc(job.range.end)}</span>}
                  </span>
                  <span className="ml-auto text-sm text-muted-foreground timecode">{elapsed(job.updated)}</span>
                </div>
                {job.last_log && <p className="mt-1.5 truncate font-mono text-xs text-muted-foreground">{job.last_log}</p>}
              </Link>
            </li>
          ))}
          {running.map((scan) => (
            <li key={scan.id}>
              <Link to={`/scans/${scan.id}`} className="flex flex-wrap items-center gap-x-4 gap-y-1 px-4 py-3 hover:bg-accent/50">
                <Status status={scan.status} />
                <span className="font-medium">Scan {scan.id}</span>
                <span className="truncate text-sm text-muted-foreground">{scan.title ?? sourceLabel(scan.source_url)}</span>
              </Link>
            </li>
          ))}
          {data.queued_jobs > 0 && (
            <li className="px-4 py-3 text-sm text-muted-foreground">
              {data.queued_jobs} more {data.queued_jobs === 1 ? "job is" : "jobs are"} waiting their turn
            </li>
          )}
        </ul>
      )}
    </section>
  )
}

function Problems({ problems }: { problems: Problem[] }) {
  const qc = useQueryClient()
  const refresh = () => qc.invalidateQueries({ queryKey: ["overview"] })
  const fix = useMutation({
    mutationFn: async (p: Problem) => {
      if (p.kind === "job") return { text: `Job ${(await api.retryJob(String(p.id))).id} queued` }
      if (p.kind === "scan") return { text: `Scan ${(await api.rescan(String(p.id))).id} queued` }
      await api.retryEvent(Number(p.id))
      return { text: "Sending it again" }
    },
    onSuccess: ({ text }) => { toast.success(text); refresh() },
  })
  if (!problems.length) return null
  return (
    <section>
      <h2 className="mb-3 text-base font-medium">Problems</h2>
      <ul className="divide-y divide-border rounded-md border border-destructive/30 bg-card">
        {problems.map((p) => (
          <li key={`${p.kind}-${p.id}`} className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-start">
            <div className="min-w-0 flex-1">
              <div className="flex items-baseline gap-3">
                <Link to={p.kind === "job" ? `/jobs/${p.id}` : p.kind === "scan" ? `/scans/${p.id}` : "/logs?tab=telegram"}
                  className="font-medium hover:underline">{p.title}</Link>
                <span className="text-sm text-muted-foreground">{ago(p.ts)}</span>
              </div>
              {p.detail && <p className="mt-1 line-clamp-3 text-sm break-words whitespace-pre-line text-muted-foreground">{p.detail}</p>}
            </div>
            <Button size="sm" variant="outline" disabled={fix.isPending} onClick={() => fix.mutate(p)}>
              {p.kind === "job" ? "Retry job" : p.kind === "scan" ? "Scan again" : "Send again"}
            </Button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function Week({ week, errors }: { week: { approved: number; rejected: number; jobs: number; scans: number }; errors: number }) {
  const rows: [string, number, string?][] = [
    ["Clips approved", week.approved],
    ["Clips skipped", week.rejected],
    ["Jobs", week.jobs],
    ["Scans", week.scans],
    ["Errors today", errors, errors ? "text-destructive" : undefined],
  ]
  return (
    <section>
      <h2 className="mb-3 text-base font-medium">Last 7 days</h2>
      <dl className="divide-y divide-border border-y border-border">
        {rows.map(([label, value, tone]) => (
          <div key={label} className="flex items-baseline justify-between py-2 text-sm">
            <dt className="text-muted-foreground">{label}</dt>
            <dd className={cn("font-medium tabular-nums", tone)}>
              {label === "Errors today" && value ? <Link to="/logs" className="hover:underline">{value}</Link> : value}
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}

const ACTIVITY_VERB: Record<string, string> = {
  "job:queued": "Job queued", "job:downloading": "Downloading", "job:submitting": "Sending to OpenShorts",
  "job:processing": "Rendering", "job:done": "Job finished", "job:failed": "Job failed", "job:cancelled": "Job cancelled",
  "scan:queued": "Scan queued", "scan:running": "Scanning", "scan:done": "Scan finished", "scan:failed": "Scan failed",
  "clip:approved": "Approved", "clip:rejected": "Skipped",
}

function ActivityList({ items }: { items: Activity[] }) {
  return (
    <section>
      <h2 className="mb-3 text-base font-medium">Activity</h2>
      {!items.length ? (
        <p className="text-sm text-muted-foreground">Nothing yet.</p>
      ) : (
        <ol className="relative space-y-3 border-l border-border pl-4">
          {items.map((a) => (
            <li key={`${a.kind}-${a.id}-${a.ts}`} className="text-sm">
              <span className={cn("absolute -left-[3px] mt-1.5 size-1.5 rounded-full",
                a.status === "failed" ? "bg-destructive" : a.status === "approved" || a.status === "done" ? "bg-success" : "bg-muted-foreground/60")} />
              <div className="flex items-baseline justify-between gap-3">
                <Link to={a.kind === "job" ? `/jobs/${a.id}` : a.kind === "scan" ? `/scans/${a.id}` : "/review"}
                  className={cn("hover:underline", a.status === "failed" && "text-destructive")}>
                  {ACTIVITY_VERB[`${a.kind}:${a.status}`] ?? a.status}
                </Link>
                <time className="shrink-0 text-xs text-muted-foreground timecode">{clock(a.ts, false)}</time>
              </div>
              <p className="truncate text-muted-foreground">
                {a.kind === "clip" ? a.text : sourceLabel(a.text)}
                {a.range && <span className="ml-2 timecode">{tc(a.range.start)}–{tc(a.range.end)}</span>}
              </p>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function OverviewSkeleton() {
  return (
    <div className="mx-auto grid max-w-7xl gap-10 lg:grid-cols-[2fr_1fr]">
      <div className="space-y-4">
        <Skeleton className="h-12 w-40" />
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-24 w-full" />
      </div>
      <Skeleton className="h-64 w-full" />
    </div>
  )
}
