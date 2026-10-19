import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { useState } from "react"
import { useNavigate } from "react-router"
import { toast } from "sonner"

import { Status } from "@/components/status"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription,
  AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, type StorageReport } from "@/lib/api"
import { ago, bytes, platformName, size, sourceLabel, tc } from "@/lib/format"

const FINISHED = ["done", "failed", "cancelled"]

export function useDeleteJobFiles() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: api.deleteJobFiles,
    onSuccess: (r) => {
      toast.success(r.freed ? `Deleting ${size(r.freed)}` : "Nothing left to delete")
      qc.invalidateQueries({ queryKey: ["storage"] })
      qc.invalidateQueries({ queryKey: ["job"] })
    },
  })
}

export function StoragePage() {
  const q = useQuery({ queryKey: ["storage"], queryFn: api.storage, refetchInterval: 10_000 })
  const qc = useQueryClient()
  const navigate = useNavigate()
  const [confirming, setConfirming] = useState(false)
  const remove = useDeleteJobFiles()
  const cleanup = useMutation({
    mutationFn: api.cleanup,
    onSuccess: (r) => {
      toast.success(r.freed ? `Deleting ${size(r.freed)} from ${r.jobs} job${r.jobs === 1 ? "" : "s"}` : "Nothing to clean up")
      qc.invalidateQueries({ queryKey: ["storage"] })
    },
  })
  const d = q.data
  const finished = (d?.jobs ?? []).filter((j) => FINISHED.includes(j.status) && !j.files.deleting && j.files.freeable > 0)
  const freeable = finished.reduce((n, j) => n + j.files.freeable, 0)

  return (
    <div className="mx-auto max-w-7xl space-y-8">
      <div className="flex flex-wrap items-center gap-4">
        <h1 className="mr-auto text-2xl font-semibold tracking-tight">Storage</h1>
        <Button onClick={() => setConfirming(true)} disabled={!freeable || cleanup.isPending}>
          {freeable ? `Clean up finished jobs (${size(freeable)})` : "Nothing to clean up"}
        </Button>
      </div>

      {!d ? <Skeleton className="h-40" /> : (
        <>
          {d.janitor_stuck && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">
              Deletions are waiting: the janitor container isn't running. On the server: <code className="font-mono">docker compose up -d janitor</code>
            </div>
          )}
          <Disk report={d} />
          <section className="space-y-3">
            <div>
              <h2 className="font-medium">By job</h2>
              <p className="text-sm text-muted-foreground">
                Deleting removes a finished job's source, OpenShorts' work files and its skipped clips. Approved clips and
                clips still waiting for review stay. OpenShorts deletes its own copies after 24 hours anyway.
              </p>
            </div>
            {!d.jobs.length ? (
              <div className="rounded-md border border-dashed border-border px-6 py-10 text-center text-sm text-muted-foreground">No job has files on disk.</div>
            ) : (
              <div className="overflow-x-auto rounded-md border border-border bg-card">
                <Table>
                  <TableHeader>
                    <TableRow className="hover:bg-transparent">
                      <TableHead className="w-36">Status</TableHead>
                      <TableHead>Source</TableHead>
                      <TableHead className="w-28 text-right">Source + work</TableHead>
                      <TableHead className="w-24 text-right">Skipped</TableHead>
                      <TableHead className="w-24 text-right">Kept</TableHead>
                      <TableHead className="w-28 text-right">Started</TableHead>
                      <TableHead className="w-32" />
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {d.jobs.map((job) => {
                      const canDelete = FINISHED.includes(job.status) && job.files.freeable > 0 && !job.files.deleting
                      return (
                        <TableRow key={job.id} className="cursor-pointer" onClick={() => navigate(`/jobs/${job.id}`)}>
                          <TableCell><Status status={job.status} /></TableCell>
                          <TableCell className="max-w-0">
                            <div className="truncate">
                              <span className="text-muted-foreground">{platformName[job.platform]}</span> {sourceLabel(job.source_url)}
                              {job.range && <span className="ml-2 text-muted-foreground timecode">{tc(job.range.start)}–{tc(job.range.end)}</span>}
                            </div>
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{size(job.files.openshorts)}</TableCell>
                          <TableCell className="text-right tabular-nums">{size(job.files.rejected)}</TableCell>
                          <TableCell className="text-right text-muted-foreground tabular-nums">{size(job.files.approved + job.files.pending)}</TableCell>
                          <TableCell className="text-right text-muted-foreground">{ago(job.created)}</TableCell>
                          <TableCell className="text-right" onClick={(ev) => ev.stopPropagation()}>
                            {job.files.deleting ? <span className="text-sm text-muted-foreground">Deleting…</span> : (
                              <Button size="sm" variant="outline" disabled={!canDelete || remove.isPending}
                                onClick={() => remove.mutate(job.id)}
                                title={FINISHED.includes(job.status) ? undefined : "The job is still running"}>
                                Delete files
                              </Button>
                            )}
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>
            )}
          </section>
        </>
      )}

      <AlertDialog open={confirming} onOpenChange={setConfirming}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Delete {size(freeable)} from {finished.length} finished job{finished.length === 1 ? "" : "s"}?</AlertDialogTitle>
            <AlertDialogDescription>
              Sources, OpenShorts' work files and skipped clips go. Approved clips, clips waiting for review and the job history stay.
              A new clip from the same stream downloads its part again.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Keep them</AlertDialogCancel>
            <AlertDialogAction onClick={() => cleanup.mutate()}>Delete</AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}

function Disk({ report }: { report: StorageReport }) {
  const used = report.disk.total - report.disk.free
  const parts = [
    { label: "OpenShorts sources", value: report.openshorts.sources },
    { label: "OpenShorts work files", value: report.openshorts.work },
    { label: "Waiting for review", value: report.folders.pending + report.folders.clean },
    { label: "Approved clips", value: report.folders.approved },
    { label: "Skipped clips", value: report.folders.rejected },
    { label: "Previews and thumbnails", value: report.folders.previews + report.folders.thumbs },
  ]
  const low = report.disk.free / report.disk.total < 0.1
  return (
    <section className="space-y-4">
      <div className="flex flex-wrap items-baseline gap-x-3">
        <span className={cn("text-[2rem] leading-none font-semibold tracking-tight tabular-nums", low && "text-destructive")}>{bytes(report.disk.free)}</span>
        <span className="text-muted-foreground">free of {bytes(report.disk.total)}</span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-muted" role="img" aria-label={`${Math.round((used / report.disk.total) * 100)}% used`}>
        <div className={cn("h-full", low ? "bg-destructive" : "bg-primary")} style={{ width: `${(used / report.disk.total) * 100}%` }} />
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3 lg:grid-cols-6">
        {parts.map((p) => (
          <div key={p.label}>
            <dt className="text-muted-foreground">{p.label}</dt>
            <dd className="tabular-nums">{size(p.value)}</dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
