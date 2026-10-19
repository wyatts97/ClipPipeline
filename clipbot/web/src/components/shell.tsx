import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { Activity, AlertTriangle, CalendarClock, Clapperboard, Film, HardDrive, LogOut, ScanSearch } from "lucide-react"
import type { ReactNode } from "react"
import { NavLink, Outlet } from "react-router"

import { Wordmark } from "@/components/brand"
import { Light, statusLabel } from "@/components/status"
import { api } from "@/lib/api"
import { bytes, elapsed } from "@/lib/format"

export function useOverview() {
  return useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 5000 })
}

const NAV = [
  { to: "/", label: "Overview", icon: Activity, end: true },
  { to: "/review", label: "Review", icon: Film, count: "pending" as const },
  { to: "/publishing", label: "Publishing", icon: CalendarClock, count: "scheduled" as const },
  { to: "/jobs", label: "Jobs", icon: Clapperboard },
  { to: "/scans", label: "Scans", icon: ScanSearch },
  { to: "/storage", label: "Storage", icon: HardDrive },
  { to: "/logs", label: "Logs & errors", icon: AlertTriangle, count: "errors" as const },
]

export function Shell() {
  const overview = useOverview()
  const qc = useQueryClient()
  const logout = useMutation({ mutationFn: api.logout, onSuccess: () => qc.invalidateQueries({ queryKey: ["session"] }) })
  const counts = {
    pending: overview.data?.pending_clips ?? 0,
    errors: overview.data?.errors_24h ?? 0,
    scheduled: overview.data?.scheduled_posts ?? 0,
  }

  return (
    <div className="flex min-h-screen flex-col md:flex-row">
      <aside className="flex shrink-0 flex-col border-b border-sidebar-border bg-sidebar md:sticky md:top-0 md:h-screen md:w-56 md:border-r md:border-b-0">
        <div className="flex items-center justify-between px-4 py-3.5 md:py-5">
          <Wordmark className="text-[15px] text-foreground" />
          <button onClick={() => logout.mutate()} className="text-muted-foreground hover:text-foreground md:hidden"
            aria-label="Sign out">
            <LogOut className="size-4" />
          </button>
        </div>
        <nav className="flex gap-1 overflow-x-auto px-2 pb-2 md:flex-col md:overflow-visible md:px-3 md:pb-0">
          {NAV.map(({ to, label, icon: Icon, end, count }) => (
            <NavLink key={to} to={to} end={end}
              className={({ isActive }) => cn(
                "flex items-center gap-3 rounded-md px-2.5 py-2 text-sm whitespace-nowrap text-sidebar-foreground transition-colors hover:bg-sidebar-accent hover:text-sidebar-accent-foreground",
                isActive && "bg-sidebar-accent text-sidebar-accent-foreground")}>
              {({ isActive }) => (
                <>
                  <Icon className={cn("size-4", isActive ? "text-primary" : "text-muted-foreground")} />
                  <span className="flex-1">{label}</span>
                  {count && counts[count] > 0 && (
                    <span className={cn("rounded-sm px-1.5 text-xs tabular-nums",
                      count === "errors" ? "bg-destructive/15 text-destructive"
                        : count === "scheduled" ? "bg-muted text-muted-foreground" : "bg-primary/15 text-primary")}>
                      {counts[count]}
                    </span>
                  )}
                </>
              )}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto hidden px-3 pb-4 md:block">
          <button onClick={() => logout.mutate()}
            className="flex w-full items-center gap-3 rounded-md px-2.5 py-2 text-sm text-muted-foreground hover:bg-sidebar-accent hover:text-foreground">
            <LogOut className="size-4" />
            Sign out
          </button>
        </div>
      </aside>
      <div className="flex min-w-0 flex-1 flex-col">
        <StatusStrip />
        <main className="flex-1 px-4 py-6 md:px-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}

function Item({ children, className }: { children: ReactNode; className?: string }) {
  return <div className={cn("flex items-center gap-2 whitespace-nowrap", className)}>{children}</div>
}

/** What the machine is doing right now, always in view. */
function StatusStrip() {
  const { data, isError } = useOverview()
  if (isError) {
    return (
      <div className="border-b border-border bg-destructive/10 px-4 py-2 text-sm text-destructive md:px-8">
        Can't reach clipbot. The numbers below may be stale.
      </div>
    )
  }
  if (!data) return <div className="h-10 border-b border-border" />
  const job = data.working[0]
  const h = data.health
  return (
    <div className="sticky top-0 z-20 flex flex-wrap items-center gap-x-6 gap-y-1 border-b border-border bg-background/90 px-4 py-2.5 text-sm backdrop-blur md:px-8">
      <Item>
        <Light tone={!h.openshorts ? "bad" : job ? "working" : "ok"} />
        {!h.openshorts ? (
          <span className="text-destructive">OpenShorts is down</span>
        ) : job ? (
          <span>
            {statusLabel(job.status)}: job <span className="font-medium">{job.id}</span>
            <span className="ml-2 text-muted-foreground timecode">{elapsed(job.updated)}</span>
          </span>
        ) : (
          <span className="text-muted-foreground">OpenShorts idle</span>
        )}
      </Item>
      <Item className="text-muted-foreground">
        {data.queued_jobs} waiting, {data.scans.filter((s) => s.status === "running").length} scanning
      </Item>
      <Item>
        <Light tone={h.telegram_failed ? "bad" : h.n8n ? "ok" : "bad"} />
        <span className={cn(!(h.n8n && !h.telegram_failed) ? "text-destructive" : "text-muted-foreground")}>
          {!h.n8n ? "n8n unreachable"
            : h.telegram_failed ? `${h.telegram_failed} Telegram message${h.telegram_failed > 1 ? "s" : ""} failed`
              : h.telegram_waiting ? `${h.telegram_waiting} Telegram message${h.telegram_waiting > 1 ? "s" : ""} queued`
                : "Telegram delivered"}
        </span>
      </Item>
      {h.postiz !== null && (
        <Item>
          <Light tone={h.postiz ? "ok" : "bad"} />
          <span className={h.postiz ? "text-muted-foreground" : "text-destructive"}>
            {h.postiz ? `${data.scheduled_posts} post${data.scheduled_posts === 1 ? "" : "s"} scheduled` : "Postiz unreachable"}
          </span>
        </Item>
      )}
      <Item className={cn("ml-auto", h.disk.free / h.disk.total < 0.1 || h.janitor_stuck ? "text-destructive" : "text-muted-foreground")}>
        <NavLink to="/storage" className="hover:underline">
          {h.janitor_stuck ? "Deletions waiting for the janitor · " : ""}{bytes(h.disk.free)} free
        </NavLink>
      </Item>
    </div>
  )
}
