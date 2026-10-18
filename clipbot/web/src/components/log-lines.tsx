import { cn } from "cn"
import { useState } from "react"
import { Link } from "react-router"

import type { LogItem } from "@/lib/api"
import { clock } from "@/lib/format"

const LEVEL_BAR: Record<string, string> = {
  CRITICAL: "bg-destructive",
  ERROR: "bg-destructive",
  WARNING: "bg-primary",
  INFO: "bg-muted-foreground/30",
  DEBUG: "bg-muted-foreground/20",
}

const LEVEL_WORD: Record<string, string> = {
  CRITICAL: "Error", ERROR: "Error", WARNING: "Warning", INFO: "Info", DEBUG: "Debug",
}

/** Log rows: a level bar on the left, time, source, the message in mono; click to see the traceback. */
export function LogLines({ items, compact = false }: { items: LogItem[]; compact?: boolean }) {
  const [open, setOpen] = useState<number | null>(null)
  return (
    <ol className="divide-y divide-border overflow-hidden rounded-md border border-border bg-card">
      {items.map((item) => {
        const expandable = !!item.detail
        const isOpen = open === item.id
        return (
          <li key={item.id} className="relative">
            <span aria-hidden className={cn("absolute inset-y-0 left-0 w-0.5", LEVEL_BAR[item.level])} />
            <button type="button" disabled={!expandable} onClick={() => setOpen(isOpen ? null : item.id)}
              className={cn("grid w-full gap-x-4 gap-y-1 py-2 pr-3 pl-4 text-left text-sm",
                compact ? "grid-cols-[5.5rem_1fr]" : "grid-cols-[9.5rem_5rem_6rem_1fr]",
                expandable && "hover:bg-accent/50")}>
              <time className="text-xs text-muted-foreground timecode">{clock(item.ts)}</time>
              {!compact && (
                <>
                  <span className={cn("text-xs", item.level === "ERROR" || item.level === "CRITICAL" ? "text-destructive"
                    : item.level === "WARNING" ? "text-primary" : "text-muted-foreground")}>{LEVEL_WORD[item.level]}</span>
                  <span className="truncate text-xs text-muted-foreground">{item.source}</span>
                </>
              )}
              <span className="min-w-0">
                <span className="font-mono text-xs break-words whitespace-pre-wrap">{item.message}</span>
                {!compact && (item.job_id || item.scan_id) && (
                  <span className="ml-2 inline-flex gap-2 text-xs">
                    {item.job_id && <Link to={`/jobs/${item.job_id}`} onClick={(e) => e.stopPropagation()} className="text-primary hover:underline">job {item.job_id}</Link>}
                    {item.scan_id && <Link to={`/scans/${item.scan_id}`} onClick={(e) => e.stopPropagation()} className="text-primary hover:underline">scan {item.scan_id}</Link>}
                  </span>
                )}
                {expandable && !isOpen && <span className="ml-2 text-xs text-muted-foreground">Show details</span>}
              </span>
            </button>
            {isOpen && item.detail && (
              <pre className="mx-3 mb-3 ml-4 max-h-80 overflow-auto rounded-sm bg-background p-3 font-mono text-xs leading-relaxed whitespace-pre-wrap text-muted-foreground">
                {item.detail}
              </pre>
            )}
          </li>
        )
      })}
    </ol>
  )
}
