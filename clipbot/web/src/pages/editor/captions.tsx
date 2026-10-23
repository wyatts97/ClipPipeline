import { cn } from "cn"
import { useMemo } from "react"

import { Button } from "@/components/ui/button"
import type { Word } from "@/lib/api"
import { fmt, groupWords, retime, type Segment, segmentAt } from "@/lib/edit"
import { LineInput } from "@/pages/editor/line-input"

export function CaptionsPanel({ words, segments, maxChars, maxDuration, original, onSeek, onChange }: {
  words: Word[]; segments: Segment[]; maxChars: number; maxDuration: number; original: Word[]
  onSeek: (t: number) => void; onChange: (words: Word[]) => void
}) {
  const lines = useMemo(() => {
    const out: { from: number; words: Word[] }[] = []
    let from = 0
    for (const block of groupWords(words, maxChars, maxDuration)) {
      out.push({ from, words: block.words })
      from += block.words.length
    }
    return out
  }, [words, maxChars, maxDuration])

  const replace = (from: number, count: number, next: Word[]) =>
    onChange([...words.slice(0, from), ...next, ...words.slice(from + count)])

  return (
    <div className="space-y-2 pt-2 text-sm">
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">Fix words here; their timing stays. Empty a line to drop it. Lines in cut parts are greyed.</p>
        <Button size="sm" variant="ghost" onClick={() => onChange(original)}>Reset</Button>
      </div>
      {!lines.length && <p className="text-muted-foreground">No words: OpenShorts sent no transcript for this clip.</p>}
      <ul className="max-h-[52vh] space-y-1 overflow-y-auto pr-1">
        {lines.map((line) => {
          const kept = segmentAt(line.words[0].s, segments) >= 0
          return (
            <li key={`${line.from}-${line.words[0].s}`} className={cn("flex items-center gap-2", !kept && "opacity-45")}>
              <button className="timecode w-12 shrink-0 text-left text-xs text-primary hover:underline" onClick={() => onSeek(line.words[0].s)}>
                {fmt(line.words[0].s)}
              </button>
              <LineInput text={line.words.map((w) => w.w).join(" ")} onCommit={(text) => replace(line.from, line.words.length, retime(line.words, text))} />
            </li>
          )
        })}
      </ul>
    </div>
  )
}
