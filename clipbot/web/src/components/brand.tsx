import { cn } from "cn"

/** Five bars climbing the heat scale: the same colors the scan timeline uses. */
export function HeatGlyph({ className }: { className?: string }) {
  const bars = [
    { h: 5, c: "var(--chart-1)" },
    { h: 8, c: "var(--chart-2)" },
    { h: 12, c: "var(--chart-3)" },
    { h: 9, c: "var(--chart-4)" },
    { h: 15, c: "var(--chart-5)" },
  ]
  return (
    <svg viewBox="0 0 22 16" className={cn("h-4 w-[22px]", className)} aria-hidden>
      {bars.map((b, i) => (
        <rect key={i} x={i * 4.5} y={16 - b.h} width={3} height={b.h} rx={0.75} fill={b.c} />
      ))}
    </svg>
  )
}

export function Wordmark({ className }: { className?: string }) {
  return (
    <span className={cn("inline-flex items-center gap-2.5 font-semibold tracking-tight", className)}>
      <HeatGlyph />
      <span>ClipPipeline</span>
    </span>
  )
}
