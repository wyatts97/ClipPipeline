import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { toast } from "sonner"

import { NativeSelect } from "@/components/native-select"
import { Button } from "@/components/ui/button"
import { api, type EditorData } from "@/lib/api"
import { fmt, type Segment } from "@/lib/edit"

export function LengthPanel({ data, segments, total, duration, disabled, onReset }: {
  data: EditorData; segments: Segment[]; total: number; duration: number; disabled: boolean; onReset: () => void
}) {
  const qc = useQueryClient()
  const [extendStart, setExtendStart] = useState(0)
  const [extendEnd, setExtendEnd] = useState(0)
  const [framing, setFraming] = useState("")
  const os = data.openshorts
  const recut = useMutation({
    mutationFn: () => api.recut(data.clip.id, { extend_start: extendStart, extend_end: extendEnd, framing: framing || null }),
    onSuccess: () => { toast("Re-cutting in OpenShorts…", { description: "Extending or reframing can take a few minutes on this CPU." }); qc.invalidateQueries({ queryKey: ["editor", data.clip.id] }) },
  })
  const changed = extendStart > 0 || extendEnd > 0 || framing !== ""
  const reach = os.source_kept || (!extendStart && !extendEnd && !framing)
  return (
    <div className="space-y-5 pt-2 text-sm">
      <div className="space-y-1.5">
        <h3 className="font-medium">Cuts</h3>
        <p className="text-muted-foreground">
          Keeping {segments.length} part{segments.length > 1 ? "s" : ""}: {fmt(total)} of {fmt(duration)}.
          Split with S, select a part and Delete it, or drag a part's edges.
        </p>
        <Button size="sm" variant="ghost" onClick={onReset} disabled={segments.length === 1 && segments[0][0] === 0 && segments[0][1] === duration}>
          Keep the whole clip
        </Button>
      </div>
      <div className="space-y-2 border-t border-border pt-4">
        <h3 className="font-medium">Longer or reframed (OpenShorts)</h3>
        <p className="text-xs text-muted-foreground">
          OpenShorts cuts the clip again from the stream: more at either end, or framed differently. Your cuts, zooms and
          effects move along; caption text edits start over from the new transcript.
        </p>
        {!os.available || !os.source_kept ? <p className="text-xs text-destructive">{os.reason}</p> : null}
        <div className="grid grid-cols-2 gap-2">
          <label className="space-y-1 text-xs text-muted-foreground">Start earlier by
            <NativeSelect value={extendStart} disabled={!os.source_kept} onChange={(e) => setExtendStart(Number(e.target.value))}>
              {[0, 1, 2, 3, 5, 10, 15].map((n) => <option key={n} value={n}>{n ? `${n} s` : "—"}</option>)}
            </NativeSelect>
          </label>
          <label className="space-y-1 text-xs text-muted-foreground">End later by
            <NativeSelect value={extendEnd} disabled={!os.source_kept} onChange={(e) => setExtendEnd(Number(e.target.value))}>
              {[0, 1, 2, 3, 5, 10, 15].map((n) => <option key={n} value={n}>{n ? `${n} s` : "—"}</option>)}
            </NativeSelect>
          </label>
        </div>
        <label className="block space-y-1 text-xs text-muted-foreground">Framing (now: {os.framing ?? "unknown"})
          <NativeSelect value={framing} disabled={!os.source_kept} onChange={(e) => setFraming(e.target.value)}>
            <option value="">Keep it</option>
            <option value="auto">Auto (OpenShorts picks per shot)</option>
            <option value="track">Follow the speaker</option>
            <option value="full">Whole frame (blurred bars)</option>
          </NativeSelect>
        </label>
        <Button size="sm" onClick={() => recut.mutate()} disabled={!os.available || !changed || !reach || disabled || recut.isPending}>
          Re-cut with OpenShorts
        </Button>
      </div>
    </div>
  )
}
