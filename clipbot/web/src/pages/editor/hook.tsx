import { useMutation } from "@tanstack/react-query"
import { Sparkles } from "lucide-react"
import { useState } from "react"
import { toast } from "sonner"

import { NativeSelect } from "@/components/native-select"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Label } from "@/components/ui/label"
import { api, type CaptionPosition, type ClipHook, type HookStyle } from "@/lib/api"
import { fmt, type HookWindow, MIN_SEGMENT } from "@/lib/edit"
import { LineInput } from "@/pages/editor/line-input"

export function HookPanel({ clipId, fixed, text, own, window: w, look, time, playhead, duration, onSeek, onText, onChange }: {
  clipId: string; fixed: boolean; text: string; own: ClipHook; window: HookWindow; look: HookStyle; time: number
  playhead: () => number; duration: number; onSeek: (t: number) => void; onText: (text: string) => void
  onChange: (hook: ClipHook) => void
}) {
  const [ideas, setIdeas] = useState<string[] | null>(null)
  const suggest = useMutation({
    mutationFn: () => api.suggestHooks(clipId),
    onSuccess: (r) => { setIdeas(r.suggestions); if (!r.suggestions.length) toast("No ideas this time. Try again.") },
  })
  if (fixed) {
    return (
      <p className="pt-2 text-sm text-muted-foreground">
        OpenShorts drew this clip's hook into the video before clipbot drew hooks itself, so it can't change here. Clips
        made from now on, and this one after a re-cut (Cuts, Longer or reframed), get an editable hook.
      </p>
    )
  }
  const set = (patch: Partial<ClipHook>) => {
    const next: ClipHook = { ...own, ...patch }
    for (const k of Object.keys(next) as (keyof ClipHook)[]) if (next[k] === undefined) delete next[k]
    onChange(next)
  }
  const at = +time.toFixed(2)
  const withoutTiming = { ...own }
  delete withoutTiming.s
  delete withoutTiming.e
  return (
    <div className="space-y-5 pt-2 text-sm">
      <div className="space-y-1.5">
        <div className="flex items-center justify-between gap-2">
          <Label htmlFor="ed-hook">Hook text</Label>
          <Button variant="ghost" size="sm" className="h-7 px-2 text-xs" disabled={suggest.isPending} onClick={() => suggest.mutate()}>
            <Sparkles /> {suggest.isPending ? "Thinking…" : "Suggest"}
          </Button>
        </div>
        <LineInput id="ed-hook" text={text} onCommit={onText} className="h-9" />
        {ideas && ideas.length > 0 && (
          <ul className="space-y-1 rounded-md border border-border p-1.5" aria-label="Hook ideas">
            {ideas.map((idea) => (
              <li key={idea}>
                <button type="button" onClick={() => { onText(idea); setIdeas(null) }}
                  className="w-full rounded-sm px-2 py-1 text-left hover:bg-accent">{idea}</button>
              </li>
            ))}
          </ul>
        )}
        <p className="text-xs text-muted-foreground">Press Enter or click away to see it. Emoji are left off the video.</p>
      </div>

      <label className="flex items-center gap-2">
        <Checkbox checked={w.on} onCheckedChange={(v) => set({ enabled: !!v })} /> Show the hook on this clip
      </label>

      <label className="block space-y-1 text-xs text-muted-foreground">Position
        <NativeSelect value={own.position ?? ""} onChange={(e) => set({ position: (e.target.value || undefined) as CaptionPosition | undefined })}>
          <option value="">The template's ({look.position})</option>
          <option value="top">top</option>
          <option value="middle">middle</option>
          <option value="bottom">bottom</option>
        </NativeSelect>
      </label>

      <div className="space-y-2">
        <span className="text-xs text-muted-foreground">When it shows</span>
        <p>
          {w.own ? (
            <>
              <button className="timecode text-primary hover:underline" onClick={() => onSeek(w.s)}>{fmt(w.s)}</button> to{" "}
              {own.e == null ? "the end" : <button className="timecode text-primary hover:underline" onClick={() => onSeek(w.e)}>{fmt(w.e)}</button>}
            </>
          ) : look.duration ? `The template's: the first ${look.duration} s` : "The template's: the whole clip"}
        </p>
        <div className="flex flex-wrap gap-1.5">
          <Button size="sm" variant="outline" disabled={at > duration - MIN_SEGMENT}
            onClick={() => {
              const t = +playhead().toFixed(2)
              set({ s: t, e: own.s !== undefined && own.e == null ? null : w.e > t + MIN_SEGMENT ? +w.e.toFixed(2) : null })
            }}>
            Start at playhead
          </Button>
          <Button size="sm" variant="outline" disabled={at < MIN_SEGMENT}
            onClick={() => {
              const t = +playhead().toFixed(2)
              set({ s: +Math.max(0, Math.min(w.s, t - MIN_SEGMENT)).toFixed(2), e: t })
            }}>
            End at playhead
          </Button>
          <Button size="sm" variant="outline" onClick={() => set({ s: +w.s.toFixed(2), e: null })}>Until the end</Button>
          {w.own && <Button size="sm" variant="ghost" onClick={() => onChange(withoutTiming)}>Template's timing</Button>}
        </div>
      </div>

      <div className="space-y-2 border-t border-border pt-4">
        <p className="text-xs text-muted-foreground">
          The look (font, colors, box) comes from the template. Drag the hook on the timeline to move it, or its ends to change
          how long it shows. Render to put it on the video.
        </p>
        {Object.keys(own).length > 0 && <Button size="sm" variant="ghost" onClick={() => onChange({})}>Reset to the template</Button>}
      </div>
    </div>
  )
}
