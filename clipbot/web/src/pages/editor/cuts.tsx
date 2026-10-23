import { cn } from "cn"
import { Snowflake, Trash2 } from "lucide-react"

import type { Selection } from "@/components/edit-timeline"
import { NativeSelect } from "@/components/native-select"
import { Button } from "@/components/ui/button"
import type { Freeze, FreezeStyle, Transition } from "@/lib/api"
import { ACCENT_TRANSITIONS, fmt, isSmooth, type Segment, SMOOTH_TRANSITIONS } from "@/lib/edit"

export const TRANSITION_LABEL: Record<Transition["type"], string> = {
  fade: "Crossfade", white: "Fade through white", black: "Fade through black", slideleft: "Slide left",
  slideright: "Slide right", slideup: "Slide up", slidedown: "Slide down", wipe: "Wipe", zoomin: "Zoom in",
  circle: "Circle open", pixelize: "Pixelate", blur: "Blur whip",
  flash: "Flash", glitch: "Glitch", shake: "Camera shake", punch: "Zoom punch",
}
const FREEZE_LABEL: Record<FreezeStyle, string> = {
  plain: "Plain", gray: "Black and white", zoom: "Slow zoom in", scratch: "Record scratch",
}

/** The Cuts tab: a transition for each cut, and the freeze frames. */
export function CutsPanel({ segments, transitions, freezes, selection, onSelect, onSeek, onTransitions, onFreezes, onAddFreeze }: {
  segments: Segment[]; transitions: Transition[]; freezes: Freeze[]; selection: Selection
  onSelect: (s: Selection) => void; onSeek: (t: number) => void
  onTransitions: (transitions: Transition[]) => void; onFreezes: (freezes: Freeze[]) => void; onAddFreeze: () => void
}) {
  const cuts = segments.slice(0, -1).map((s, i) => ({ i, after: s[1] }))
  const setTransition = (after: number, type: string, d?: number) => {
    const rest = transitions.filter((x) => x.after !== after)
    if (!type) return onTransitions(rest)
    const tr: Transition = { after, type: type as Transition["type"] }
    if ((SMOOTH_TRANSITIONS as string[]).includes(type)) tr.d = d ?? transitions.find((x) => x.after === after)?.d ?? 0.4
    onTransitions([...rest, tr].sort((a, b) => a.after - b.after))
  }
  const setFreeze = (i: number, patch: Partial<Freeze>) => onFreezes(freezes.map((f, j) => (j === i ? { ...f, ...patch } : f)))

  return (
    <div className="space-y-5 pt-2 text-sm">
      <section className="space-y-2">
        <h3 className="font-medium">Transitions</h3>
        <p className="text-xs text-muted-foreground">
          How one kept part moves into the next. Smooth ones overlap the two sides, so the clip gets a little shorter;
          accents land right on the cut. The preview hints at them; <b>Draft</b> renders the real thing.
        </p>
        {!cuts.length && (
          <p className="text-xs text-muted-foreground">No cuts yet. Split the clip (S) and delete a part, then pick a transition here or on the ⧉ marker.</p>
        )}
        <ul className="max-h-[30vh] space-y-2 overflow-y-auto pr-1">
          {cuts.map(({ i, after }) => {
            const tr = transitions.find((x) => x.after === after)
            return (
              <li key={i} onClick={() => onSelect({ kind: "cut", index: i })}
                className={cn("space-y-2 rounded-md border p-2", selection?.kind === "cut" && selection.index === i ? "border-primary bg-primary/5" : "border-border")}>
                <div className="flex items-center gap-2">
                  <button className="timecode shrink-0 text-xs text-primary hover:underline" onClick={() => onSeek(Math.max(segments[i][0], after - 1))}>
                    Cut at {fmt(after)}
                  </button>
                  <NativeSelect aria-label={`Transition at ${fmt(after)}`} className="h-7 text-xs" value={tr?.type ?? ""}
                    onChange={(e) => setTransition(after, e.target.value)}>
                    <option value="">None (a plain cut)</option>
                    <optgroup label="Smooth">
                      {SMOOTH_TRANSITIONS.map((t) => <option key={t} value={t}>{TRANSITION_LABEL[t]}</option>)}
                    </optgroup>
                    <optgroup label="On the cut">
                      {ACCENT_TRANSITIONS.map((t) => <option key={t} value={t}>{TRANSITION_LABEL[t]}</option>)}
                    </optgroup>
                  </NativeSelect>
                </div>
                {isSmooth(tr) && (
                  <label className="flex items-center gap-2 text-xs text-muted-foreground">
                    Length
                    <input type="range" aria-label="Transition length" min={0.2} max={1} step={0.05} value={tr.d ?? 0.4}
                      onChange={(e) => setTransition(after, tr.type, Number(e.target.value))} className="w-full accent-primary" />
                    <span className="w-12 shrink-0 tabular-nums">{(tr.d ?? 0.4).toFixed(2)} s</span>
                  </label>
                )}
              </li>
            )
          })}
        </ul>
      </section>

      <section className="space-y-2 border-t border-border pt-4">
        <div className="flex items-center justify-between gap-2">
          <h3 className="font-medium">Freeze frames</h3>
          <Button size="sm" variant="outline" onClick={onAddFreeze} title="F"><Snowflake /> Freeze here</Button>
        </div>
        <p className="text-xs text-muted-foreground">
          Hold the frame at the playhead (F), for a reaction or a "wait for it". The sound stops while it holds; Record
          scratch adds the meme pack's scratch.
        </p>
        <ul className="max-h-[30vh] space-y-2 overflow-y-auto pr-1">
          {freezes.map((f, i) => (
            <li key={`${f.t}-${i}`} onClick={() => onSelect({ kind: "freeze", index: i })}
              className={cn("space-y-2 rounded-md border p-2", selection?.kind === "freeze" && selection.index === i ? "border-primary bg-primary/5" : "border-border")}>
              <div className="flex items-center gap-2">
                <button className="timecode shrink-0 text-xs text-primary hover:underline" onClick={() => onSeek(f.t)}>{fmt(f.t)}</button>
                <NativeSelect aria-label="Freeze style" className="h-7 text-xs" value={f.style ?? "plain"}
                  onChange={(e) => setFreeze(i, { style: e.target.value === "plain" ? undefined : e.target.value as FreezeStyle })}>
                  {Object.entries(FREEZE_LABEL).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
                </NativeSelect>
                <Button size="icon" variant="ghost" className="size-7 shrink-0" aria-label="Remove freeze"
                  onClick={(e) => { e.stopPropagation(); onFreezes(freezes.filter((_, j) => j !== i)) }}><Trash2 /></Button>
              </div>
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                Hold
                <input type="range" aria-label="Freeze length" min={0.3} max={5} step={0.1} value={f.d}
                  onChange={(e) => setFreeze(i, { d: Number(e.target.value) })} className="w-full accent-primary" />
                <span className="w-10 shrink-0 tabular-nums">{f.d.toFixed(1)} s</span>
              </label>
            </li>
          ))}
        </ul>
      </section>
    </div>
  )
}
