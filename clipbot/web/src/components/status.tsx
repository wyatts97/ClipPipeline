import { cn } from "cn"

// Tally semantics: green = done/approved, red = failed, amber = waiting on you,
// a pulsing paper-white light = the machine is working on it. Nothing else gets color.
type Tone = "working" | "ok" | "bad" | "yours" | "idle"

const STATUS: Record<string, { label: string; tone: Tone }> = {
  queued: { label: "Waiting", tone: "idle" },
  downloading: { label: "Downloading", tone: "working" },
  submitting: { label: "Sending to OpenShorts", tone: "working" },
  processing: { label: "Rendering", tone: "working" },
  running: { label: "Scanning", tone: "working" },
  done: { label: "Done", tone: "ok" },
  failed: { label: "Failed", tone: "bad" },
  cancelled: { label: "Cancelled", tone: "idle" },
  pending: { label: "Needs review", tone: "yours" },
  approved: { label: "Approved", tone: "ok" },
  rejected: { label: "Skipped", tone: "idle" },
  waiting: { label: "Scheduling", tone: "working" },
  scheduled: { label: "Scheduled", tone: "idle" },
  published: { label: "Posted", tone: "ok" },
}

const LIGHT: Record<Tone, string> = {
  working: "bg-foreground animate-pulse",
  ok: "bg-success",
  bad: "bg-destructive",
  yours: "bg-primary",
  idle: "bg-muted-foreground/50",
}

export function Light({ tone, className }: { tone: Tone; className?: string }) {
  return <span aria-hidden className={cn("inline-block size-2 shrink-0 rounded-full", LIGHT[tone], className)} />
}

export function Status({ status, className }: { status: string; className?: string }) {
  const s = STATUS[status] ?? { label: status, tone: "idle" as Tone }
  return (
    <span className={cn("inline-flex items-center gap-2 text-sm whitespace-nowrap",
      s.tone === "bad" && "text-destructive", s.tone === "idle" && "text-muted-foreground", className)}>
      <Light tone={s.tone} />
      {s.label}
    </span>
  )
}

export const statusLabel = (status: string) => STATUS[status]?.label ?? status

export const isActive = (status: string) =>
  ["queued", "downloading", "submitting", "processing", "running"].includes(status)
