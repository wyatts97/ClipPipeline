import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { Play, Plus, Trash2, Upload } from "lucide-react"
import { useRef, useState } from "react"
import { toast } from "sonner"

import { CaptionPreview } from "@/components/caption-preview"
import { NativeSelect } from "@/components/native-select"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Sheet, SheetContent, SheetDescription, SheetFooter, SheetHeader, SheetTitle } from "@/components/ui/sheet"
import { Skeleton } from "@/components/ui/skeleton"
import { Textarea } from "@/components/ui/textarea"
import {
  api, type CaptionStyle, type HookStyle, type HookStyleName, type MomentKind, type SfxRule, type SoundItem, type Template,
  type TemplateBody, type TemplatesResponse, type Trigger,
} from "@/lib/api"
import { size, tc } from "@/lib/format"

export const TRIGGER_LABEL: Record<Trigger, string> = {
  start: "at the start",
  end: "at the end",
  peak: "where chat exploded",
  word: "on certain words",
  moment: "on moments (Gemini)",
}

const KIND_LABEL: Record<MomentKind, string> = {
  punchline: "Punchlines", shock: "Shocks", fail: "Fails", awkward: "Awkward", hype: "Hype", sneaky: "Sneaky",
}

const EFFECT_LABEL: Record<string, string> = {
  none: "Color only", pop: "Pop", glow: "Glow", box: "Outline box", highlight: "Box behind the word",
}

export const HOOK_STYLE_LABEL: Record<HookStyleName, string> = {
  box: "Box behind", outline: "Outlined", plain: "Plain with a shadow",
}

function useTemplates() {
  return useQuery({ queryKey: ["templates"], queryFn: api.templates })
}

export function TemplatesPage() {
  const q = useTemplates()
  const qc = useQueryClient()
  const [editing, setEditing] = useState<{ id: string | null; body: TemplateBody } | null>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: ["templates"] })
  const makeDefault = useMutation({ mutationFn: api.defaultTemplate, onSuccess: () => { toast.success("Default template changed"); refresh() } })
  const remove = useMutation({ mutationFn: api.deleteTemplate, onSuccess: () => { toast("Template deleted"); refresh() } })
  const d = q.data

  return (
    <div className="mx-auto max-w-7xl space-y-10">
      <section className="space-y-4">
        <div className="flex flex-wrap items-center gap-4">
          <div className="mr-auto">
            <h1 className="text-2xl font-semibold tracking-tight">Templates</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              The look of a clip: captions, background music and sound effects. New clips get the default; pick another
              with <code className="font-mono">t=name</code> in Telegram, in New job, or per clip in Review.
            </p>
          </div>
          <Button disabled={!d} onClick={() => d && setEditing({ id: null, body: blank(d) })}><Plus /> New template</Button>
        </div>
        {!d ? <Skeleton className="h-72" /> : (
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {d.items.map((t) => (
              <article key={t.id} className="flex gap-4 rounded-md border border-border bg-card p-3">
                <CaptionPreview style={t.captions} hook={t.hook} fonts={d.fonts} className="w-24 shrink-0" />
                <div className="flex min-w-0 flex-1 flex-col gap-2 text-sm">
                  <div className="flex items-baseline gap-2">
                    <h2 className="truncate font-medium">{t.name}</h2>
                    {t.default && <span className="rounded-sm bg-primary/15 px-1.5 text-xs text-primary">default</span>}
                  </div>
                  <p className="font-mono text-xs text-muted-foreground">t={t.id}</p>
                  <Summary t={t} />
                  {t.missing_sounds.length > 0 && (
                    <p className="text-xs text-destructive">Missing sound{t.missing_sounds.length > 1 ? "s" : ""}: {t.missing_sounds.join(", ")}. Upload below.</p>
                  )}
                  <div className="mt-auto flex flex-wrap gap-1.5 pt-1">
                    <Button size="sm" variant="outline" onClick={() => setEditing({ id: t.id, body: bodyOf(t) })}>Edit</Button>
                    <Button size="sm" variant="ghost" onClick={() => setEditing({ id: null, body: { ...bodyOf(t), name: `${t.name} copy` } })}>Copy</Button>
                    {!t.default && <Button size="sm" variant="ghost" onClick={() => makeDefault.mutate(t.id)}>Make default</Button>}
                    {!t.default && (
                      <Button size="sm" variant="ghost" aria-label={`Delete ${t.name}`} onClick={() => remove.mutate(t.id)}><Trash2 /></Button>
                    )}
                  </div>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>

      {d && <Library data={d} />}
      {d && <Editor data={d} editing={editing} onClose={() => setEditing(null)} />}
    </div>
  )
}

function Summary({ t }: { t: Template }) {
  const parts = [t.captions.enabled ? `${t.captions.font_name}, ${EFFECT_LABEL[t.captions.effect]?.toLowerCase()}` : "No captions"]
  parts.push(t.hook.enabled
    ? `Hook: ${HOOK_STYLE_LABEL[t.hook.style].toLowerCase()}, ${t.hook.duration ? `first ${t.hook.duration} s` : "whole clip"}`
    : "No hook")
  if (t.music.sound) parts.push(`♪ ${t.music.sound}`)
  for (const r of t.sfx) {
    const where = r.at === "moment" ? `on ${r.kinds.map((k) => KIND_LABEL[k].toLowerCase()).join(", ")}` : TRIGGER_LABEL[r.at]
    parts.push(`🔊 ${r.sounds.join(" / ")} ${where}`)
  }
  return <ul className="space-y-0.5 text-xs text-muted-foreground">{parts.map((p) => <li key={p} className="truncate">{p}</li>)}</ul>
}

const bodyOf = (t: Template): TemplateBody => ({
  name: t.name, captions: { ...t.captions }, hook: { ...t.hook }, music: { ...t.music }, voice_volume: t.voice_volume,
  sfx: t.sfx.map((r) => ({ ...r, sounds: [...r.sounds], kinds: [...r.kinds], words: [...r.words] })), credit: t.credit,
})

const blank = (d: TemplatesResponse): TemplateBody => ({
  name: "New template", captions: { ...d.presets.default }, hook: { ...d.hook_default }, music: { sound: null, volume: 0.15, duck: true, start: 0 },
  voice_volume: 1, sfx: [], credit: "",
})

// ---- the editor ---------------------------------------------------------------------------
function Editor({ data, editing, onClose }: { data: TemplatesResponse; editing: { id: string | null; body: TemplateBody } | null; onClose: () => void }) {
  return (
    <Sheet open={!!editing} onOpenChange={(open) => !open && onClose()}>
      <SheetContent className="gap-0 overflow-y-auto data-[side=right]:w-full data-[side=right]:sm:max-w-3xl">
        {editing && <EditorForm key={editing.id ?? "new"} data={data} id={editing.id} initial={editing.body} onDone={onClose} />}
      </SheetContent>
    </Sheet>
  )
}

function EditorForm({ data, id, initial, onDone }: { data: TemplatesResponse; id: string | null; initial: TemplateBody; onDone: () => void }) {
  const qc = useQueryClient()
  const [body, setBody] = useState<TemplateBody>(initial)
  const c = body.captions
  const setC = (patch: Partial<CaptionStyle>) => setBody({ ...body, captions: { ...c, ...patch } })
  const h = body.hook
  const setH = (patch: Partial<HookStyle>) => setBody({ ...body, hook: { ...h, ...patch } })
  const save = useMutation({
    mutationFn: () => (id ? api.updateTemplate(id, body) : api.createTemplate(body)),
    onSuccess: (t) => { toast.success(`Saved ${t.name}`); qc.invalidateQueries({ queryKey: ["templates"] }); onDone() },
  })
  const setRule = (i: number, patch: Partial<SfxRule>) =>
    setBody({ ...body, sfx: body.sfx.map((r, j) => (j === i ? { ...r, ...patch } : r)) })

  return (
    <>
      <SheetHeader className="border-b border-border">
        <SheetTitle>{id ? `Edit ${initial.name}` : "New template"}</SheetTitle>
        <SheetDescription>Clips already rendered keep their look until you re-render them in Review.</SheetDescription>
      </SheetHeader>
      <div className="grid grid-cols-1 gap-6 p-4 sm:grid-cols-[minmax(0,1fr)_12rem]">
        <div className="space-y-6 text-sm">
          <Field label="Name" id="t-name"><Input id="t-name" value={body.name} onChange={(e) => setBody({ ...body, name: e.target.value })} /></Field>

          <fieldset className="space-y-3">
            <legend className="mb-2 flex w-full items-center justify-between font-medium">
              Captions
              <label className="flex items-center gap-2 text-sm font-normal text-muted-foreground">
                <Checkbox checked={c.enabled} onCheckedChange={(v) => setC({ enabled: !!v })} /> Burn captions
              </label>
            </legend>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Field label="Start from" id="t-preset">
                <NativeSelect id="t-preset" value="" onChange={(e) => e.target.value && setC({ ...data.presets[e.target.value], enabled: c.enabled })}>
                  <option value="">A preset…</option>
                  {Object.keys(data.presets).map((p) => <option key={p} value={p}>{p}</option>)}
                </NativeSelect>
              </Field>
              <Field label="Font" id="t-font">
                <NativeSelect id="t-font" value={c.font_name} onChange={(e) => setC({ font_name: e.target.value })}>
                  {!data.fonts.some((f) => f.family === c.font_name) && <option value={c.font_name}>{c.font_name} (missing)</option>}
                  {data.fonts.map((f) => <option key={f.family} value={f.family}>{f.family}</option>)}
                </NativeSelect>
              </Field>
              <Field label={`Size ${c.font_size}`} id="t-size">
                <input id="t-size" type="range" min={20} max={90} value={c.font_size} onChange={(e) => setC({ font_size: Number(e.target.value) })} className="w-full accent-primary" />
              </Field>
              <ColorField label="Text" value={c.font_color} onChange={(v) => setC({ font_color: v })} />
              <ColorField label="Active word" value={c.highlight_color} onChange={(v) => setC({ highlight_color: v })} />
              <ColorField label="Outline" value={c.border_color} onChange={(v) => setC({ border_color: v })} />
              <Field label="Effect" id="t-effect">
                <NativeSelect id="t-effect" value={c.effect} onChange={(e) => setC({ effect: e.target.value as CaptionStyle["effect"] })}>
                  {data.effects.map((x) => <option key={x} value={x}>{EFFECT_LABEL[x] ?? x}</option>)}
                </NativeSelect>
              </Field>
              <Field label="Position" id="t-pos">
                <NativeSelect id="t-pos" value={c.position} onChange={(e) => setC({ position: e.target.value as CaptionStyle["position"] })}>
                  {data.positions.map((x) => <option key={x} value={x}>{x}</option>)}
                </NativeSelect>
              </Field>
              <Field label={`Letters per line ${c.max_chars}`} id="t-chars">
                <input id="t-chars" type="range" min={1} max={30} value={c.max_chars} onChange={(e) => setC({ max_chars: Number(e.target.value) })} className="w-full accent-primary" />
              </Field>
              <Field label={`Outline width ${c.border_width}`} id="t-bw">
                <input id="t-bw" type="range" min={0} max={10} value={c.border_width} onChange={(e) => setC({ border_width: Number(e.target.value) })} className="w-full accent-primary" />
              </Field>
              <Field label={`Shadow ${c.shadow}`} id="t-shadow">
                <input id="t-shadow" type="range" min={0} max={6} value={c.shadow} onChange={(e) => setC({ shadow: Number(e.target.value) })} className="w-full accent-primary" />
              </Field>
              <Field label={`Other words ${Math.round(c.base_opacity * 100)}%`} id="t-dim">
                <input id="t-dim" type="range" min={5} max={100} value={Math.round(c.base_opacity * 100)} onChange={(e) => setC({ base_opacity: Number(e.target.value) / 100 })} className="w-full accent-primary" />
              </Field>
            </div>
            <div className="flex flex-wrap gap-x-6 gap-y-2">
              <label className="flex items-center gap-2"><Checkbox checked={c.uppercase} onCheckedChange={(v) => setC({ uppercase: !!v })} /> UPPERCASE</label>
              <label className="flex items-center gap-2"><Checkbox checked={c.reveal} onCheckedChange={(v) => setC({ reveal: !!v })} /> Words appear as they're said</label>
              <label className="flex items-center gap-2"><Checkbox checked={c.bg_opacity > 0} onCheckedChange={(v) => setC({ bg_opacity: v ? 0.6 : 0 })} /> Box behind the line</label>
            </div>
          </fieldset>

          <fieldset className="space-y-3">
            <legend className="mb-2 flex w-full items-center justify-between font-medium">
              Hook line
              <label className="flex items-center gap-2 text-sm font-normal text-muted-foreground">
                <Checkbox checked={h.enabled} onCheckedChange={(v) => setH({ enabled: !!v })} /> Show the hook
              </label>
            </legend>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Field label="Look" id="t-hstyle">
                <NativeSelect id="t-hstyle" value={h.style} disabled={!h.enabled} onChange={(e) => setH({ style: e.target.value as HookStyleName })}>
                  {data.hook_styles.map((x) => <option key={x} value={x}>{HOOK_STYLE_LABEL[x] ?? x}</option>)}
                </NativeSelect>
              </Field>
              <Field label="Font" id="t-hfont">
                <NativeSelect id="t-hfont" value={h.font_name} disabled={!h.enabled} onChange={(e) => setH({ font_name: e.target.value })}>
                  {!data.fonts.some((f) => f.family === h.font_name) && <option value={h.font_name}>{h.font_name} (missing)</option>}
                  {data.fonts.map((f) => <option key={f.family} value={f.family}>{f.family}</option>)}
                </NativeSelect>
              </Field>
              <Field label={`Size ${h.font_size}`} id="t-hsize">
                <input id="t-hsize" type="range" min={12} max={90} value={h.font_size} disabled={!h.enabled}
                  onChange={(e) => setH({ font_size: Number(e.target.value) })} className="w-full accent-primary" />
              </Field>
              <ColorField label="Hook text" value={h.text_color} onChange={(v) => setH({ text_color: v })} />
              {h.style === "box" && <ColorField label="Box" value={h.box_color} onChange={(v) => setH({ box_color: v })} />}
              {h.style === "box" && (
                <Field label={`Rounded corners ${h.box_radius}%`} id="t-hradius">
                  <input id="t-hradius" type="range" min={0} max={100} step={5} value={h.box_radius} disabled={!h.enabled}
                    onChange={(e) => setH({ box_radius: Number(e.target.value) })} className="w-full accent-primary" />
                </Field>
              )}
              {h.style === "outline" && <ColorField label="Hook outline" value={h.outline_color} onChange={(v) => setH({ outline_color: v })} />}
              <Field label="Position" id="t-hpos">
                <NativeSelect id="t-hpos" value={h.position} disabled={!h.enabled} onChange={(e) => setH({ position: e.target.value as HookStyle["position"] })}>
                  {data.positions.map((x) => <option key={x} value={x}>{x}</option>)}
                </NativeSelect>
              </Field>
              <Field label="Show for (s, 0 = whole clip)" id="t-hdur">
                <Input id="t-hdur" type="number" min={0} max={60} step={0.5} value={h.duration} disabled={!h.enabled}
                  onChange={(e) => setH({ duration: Math.max(0, Number(e.target.value) || 0) })} />
              </Field>
            </div>
            <label className="flex items-center gap-2"><Checkbox checked={h.uppercase} disabled={!h.enabled} onCheckedChange={(v) => setH({ uppercase: !!v })} /> UPPERCASE</label>
            <p className="text-xs text-muted-foreground">
              The line OpenShorts' AI writes for each clip, drawn by clipbot in this look. Change a clip's text, or when and
              where it shows, in Review or the editor. Emoji are left off the video.
              {!data.auto_hook && " AUTO_HOOK is false, so hooks only show on clips you turn them on for."}
            </p>
          </fieldset>

          <fieldset className="space-y-3">
            <legend className="mb-2 font-medium">Background music</legend>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
              <Field label="Track" id="t-music">
                <SoundSelect id="t-music" sounds={data.sounds} value={body.music.sound} allowNone
                  onChange={(v) => setBody({ ...body, music: { ...body.music, sound: v } })} />
              </Field>
              <Field label={`Volume ${Math.round(body.music.volume * 100)}%`} id="t-mvol">
                <input id="t-mvol" type="range" min={0} max={60} value={Math.round(body.music.volume * 100)} disabled={!body.music.sound}
                  onChange={(e) => setBody({ ...body, music: { ...body.music, volume: Number(e.target.value) / 100 } })} className="w-full accent-primary" />
              </Field>
              <Field label="Start the track at (s)" id="t-mstart">
                <Input id="t-mstart" type="number" min={0} step={0.5} value={body.music.start} disabled={!body.music.sound}
                  onChange={(e) => setBody({ ...body, music: { ...body.music, start: Number(e.target.value) || 0 } })} />
              </Field>
            </div>
            <label className="flex items-center gap-2">
              <Checkbox checked={body.music.duck} disabled={!body.music.sound} onCheckedChange={(v) => setBody({ ...body, music: { ...body.music, duck: !!v } })} />
              Quieter while someone talks
            </label>
          </fieldset>

          <fieldset className="space-y-3">
            <legend className="mb-2 font-medium">Sound effects</legend>
            {body.sfx.map((r, i) => (
              <RuleEditor key={i} index={i} rule={r} data={data} onChange={(patch) => setRule(i, patch)}
                onRemove={() => setBody({ ...body, sfx: body.sfx.filter((_, j) => j !== i) })} />
            ))}
            <Button variant="outline" size="sm" disabled={!data.sounds.length}
              onClick={() => setBody({ ...body, sfx: [...body.sfx, {
                sounds: [data.sounds[0]], at: "moment", kinds: ["punchline", "shock"], words: [], volume: 1, max: 2,
              }] })}>
              <Plus /> Add a sound effect
            </Button>
            {!data.sounds.length && <p className="text-xs text-muted-foreground">Upload sounds below first.</p>}
            <p className="text-xs text-muted-foreground">
              Add one rule per kind of moment, e.g. a vine boom on punchlines and a sad trombone on fails. A rule with
              several sounds picks one each time. Effects keep 1.5 s apart; place them by hand on a clip in Review.
            </p>
          </fieldset>

          <Field label="Credit line (added to every post's caption)" id="t-credit">
            <Textarea id="t-credit" rows={2} value={body.credit} placeholder='e.g. Music: "Sneaky Snitch" Kevin MacLeod (incompetech.com), CC BY 4.0'
              onChange={(e) => setBody({ ...body, credit: e.target.value })} />
          </Field>
        </div>
        <div className="sm:sticky sm:top-4 sm:self-start">
          <CaptionPreview style={c} hook={h} fonts={data.fonts} className="w-full" />
          <p className="mt-2 text-xs text-muted-foreground">Preview. The render uses the same font file.</p>
        </div>
      </div>
      <SheetFooter className="flex-row justify-end border-t border-border">
        <Button variant="outline" onClick={onDone}>Cancel</Button>
        <Button onClick={() => save.mutate()} disabled={!body.name.trim() || save.isPending}>Save template</Button>
      </SheetFooter>
    </>
  )
}

function RuleEditor({ index, rule, data, onChange, onRemove }: {
  index: number; rule: SfxRule; data: TemplatesResponse; onChange: (patch: Partial<SfxRule>) => void; onRemove: () => void
}) {
  const id = `t-sfx-${index}`
  const toggleKind = (kind: MomentKind) => {
    const kinds = rule.kinds.includes(kind) ? rule.kinds.filter((k) => k !== kind) : [...rule.kinds, kind]
    if (kinds.length) onChange({ kinds })
  }
  return (
    <div className="space-y-3 rounded-md border border-border p-3">
      <div className="grid grid-cols-[minmax(0,1.3fr)_4.5rem_minmax(0,1fr)_auto] items-end gap-2">
        <Field label="When" id={`${id}-at`}>
          <NativeSelect id={`${id}-at`} value={rule.at} onChange={(e) => onChange({ at: e.target.value as Trigger })}>
            {data.triggers.map((x) => <option key={x} value={x}>{TRIGGER_LABEL[x]}</option>)}
          </NativeSelect>
        </Field>
        <Field label="Up to" id={`${id}-max`}>
          <Input id={`${id}-max`} type="number" min={1} max={10} value={rule.max} onChange={(e) => onChange({ max: Number(e.target.value) || 1 })} />
        </Field>
        <Field label={`Volume ${Math.round(rule.volume * 100)}%`} id={`${id}-vol`}>
          <input id={`${id}-vol`} type="range" min={10} max={200} step={10} value={Math.round(rule.volume * 100)}
            onChange={(e) => onChange({ volume: Number(e.target.value) / 100 })} className="h-9 w-full accent-primary" />
        </Field>
        <Button variant="ghost" size="icon" aria-label="Remove effect" onClick={onRemove}><Trash2 /></Button>
      </div>

      {rule.at === "moment" && (
        <div className="space-y-1.5">
          <span className="text-xs text-muted-foreground">Kinds of moment</span>
          <div className="flex flex-wrap gap-1.5">
            {(Object.keys(data.kinds) as MomentKind[]).map((kind) => (
              <button key={kind} type="button" title={data.kinds[kind]} aria-pressed={rule.kinds.includes(kind)}
                onClick={() => toggleKind(kind)}
                className={cn("rounded-sm border px-2 py-1 text-xs transition-colors",
                  rule.kinds.includes(kind) ? "border-primary bg-primary/15 text-primary" : "border-border text-muted-foreground hover:text-foreground")}>
                {KIND_LABEL[kind]}
              </button>
            ))}
          </div>
          {!data.gemini && <p className="text-xs text-destructive">Needs GEMINI_API_KEY for clipbot (docker compose up -d clipbot after setting it).</p>}
        </div>
      )}
      {rule.at === "word" && (
        <Input placeholder="Words, comma separated: what, no way, bro" value={rule.words.join(", ")}
          onChange={(e) => onChange({ words: e.target.value.split(",").map((w) => w.trim()).filter(Boolean) })} />
      )}

      <div className="space-y-1.5">
        <span className="text-xs text-muted-foreground">{rule.sounds.length > 1 ? "Sounds (one picked each time)" : "Sound"}</span>
        <div className="flex flex-wrap items-center gap-1.5">
          {rule.sounds.map((name) => (
            <span key={name} className={cn("inline-flex items-center gap-1 rounded-sm bg-accent px-2 py-1 text-xs",
              !data.sounds.includes(name) && "text-destructive")}>
              {name}{!data.sounds.includes(name) && " (missing)"}
              {rule.sounds.length > 1 && (
                <button type="button" aria-label={`Remove ${name}`} className="text-muted-foreground hover:text-foreground"
                  onClick={() => onChange({ sounds: rule.sounds.filter((x) => x !== name) })}>×</button>
              )}
            </span>
          ))}
          <NativeSelect aria-label="Add a sound" className="h-7 w-auto text-xs" value=""
            onChange={(e) => e.target.value && onChange({ sounds: [...rule.sounds, e.target.value] })}>
            <option value="">+ sound</option>
            {data.sounds.filter((x) => !rule.sounds.includes(x)).map((x) => <option key={x} value={x}>{x}</option>)}
          </NativeSelect>
        </div>
      </div>
    </div>
  )
}

function Field({ label, id, children }: { label: string; id: string; children: React.ReactNode }) {
  return <div className="min-w-0 space-y-1.5"><Label htmlFor={id} className="text-xs text-muted-foreground">{label}</Label>{children}</div>
}

function ColorField({ label, value, onChange }: { label: string; value: string; onChange: (v: string) => void }) {
  const id = `c-${label.replace(/\s/g, "")}`
  return (
    <Field label={label} id={id}>
      <div className="flex h-9 items-center gap-2 rounded-sm border border-input px-1.5">
        <input id={id} type="color" value={value} onChange={(e) => onChange(e.target.value.toUpperCase())} className="size-6 shrink-0 cursor-pointer bg-transparent" />
        <span className="font-mono text-xs text-muted-foreground">{value}</span>
      </div>
    </Field>
  )
}

export function SoundSelect({ id, sounds, value, onChange, allowNone }: {
  id?: string; sounds: string[]; value: string | null; onChange: (v: string | null) => void; allowNone?: boolean
}) {
  return (
    <NativeSelect id={id} value={value ?? ""} onChange={(e) => onChange(e.target.value || null)}>
      {allowNone && <option value="">None</option>}
      {value && !sounds.includes(value) && <option value={value}>{value} (missing)</option>}
      {sounds.map((s) => <option key={s} value={s}>{s}</option>)}
    </NativeSelect>
  )
}

// ---- sounds and fonts ---------------------------------------------------------------------
function Library({ data }: { data: TemplatesResponse }) {
  const qc = useQueryClient()
  const sounds = useQuery({ queryKey: ["sounds"], queryFn: api.sounds })
  const soundInput = useRef<HTMLInputElement>(null)
  const fontInput = useRef<HTMLInputElement>(null)
  const refresh = () => { qc.invalidateQueries({ queryKey: ["sounds"] }); qc.invalidateQueries({ queryKey: ["templates"] }) }
  const uploadSounds = useMutation({
    mutationFn: async (files: File[]) => { for (const f of files) await api.uploadSound(f) },
    onSuccess: (_, files) => { toast.success(`Uploaded ${files.length} sound${files.length > 1 ? "s" : ""}`); refresh() },
    onSettled: refresh,
  })
  const uploadFonts = useMutation({
    mutationFn: async (files: File[]) => Promise.all(files.map((f) => api.uploadFont(f))),
    onSuccess: (items) => { toast.success(`Added ${items.map((f) => f.family).join(", ")}`); refresh() },
  })
  const deleteSound = useMutation({ mutationFn: api.deleteSound, onSuccess: refresh })
  const deleteFont = useMutation({ mutationFn: api.deleteFont, onSuccess: refresh })

  return (
    <div className="grid grid-cols-1 gap-10 lg:grid-cols-2">
      <section className="space-y-3">
        <div className="flex items-center gap-3">
          <div className="mr-auto">
            <h2 className="font-medium">Sounds</h2>
            <p className="text-sm text-muted-foreground">Music beds and effects. Templates use them by name, so a file named <code className="font-mono">vine-boom.mp3</code> is "vine-boom".</p>
          </div>
          <input ref={soundInput} type="file" accept="audio/*,.mp3,.wav,.ogg,.m4a,.flac,.opus" multiple hidden
            onChange={(e) => { const files = [...(e.target.files ?? [])]; e.target.value = ""; if (files.length) uploadSounds.mutate(files) }} />
          <Button variant="outline" onClick={() => soundInput.current?.click()} disabled={uploadSounds.isPending}><Upload /> Upload</Button>
        </div>
        {!sounds.data?.items.length ? (
          <div className="rounded-md border border-dashed border-border px-6 py-8 text-center text-sm text-muted-foreground">
            No sounds yet. The built-in templates look for <code className="font-mono">vine-boom</code> and <code className="font-mono">sneaky-snitch</code>.
          </div>
        ) : (
          <ul className="divide-y divide-border rounded-md border border-border bg-card">
            {sounds.data.items.map((s) => <SoundRow key={s.file} sound={s} onDelete={() => deleteSound.mutate(s.name)} />)}
          </ul>
        )}
      </section>

      <section className="space-y-3">
        <div className="flex items-center gap-3">
          <div className="mr-auto">
            <h2 className="font-medium">Fonts</h2>
            <p className="text-sm text-muted-foreground">Six free caption fonts are built in. Add your own .ttf or .otf.</p>
          </div>
          <input ref={fontInput} type="file" accept=".ttf,.otf" multiple hidden
            onChange={(e) => { const files = [...(e.target.files ?? [])]; e.target.value = ""; if (files.length) uploadFonts.mutate(files) }} />
          <Button variant="outline" onClick={() => fontInput.current?.click()} disabled={uploadFonts.isPending}><Upload /> Upload</Button>
        </div>
        <ul className="divide-y divide-border rounded-md border border-border bg-card">
          {data.fonts.map((f) => (
            <li key={f.file} className="flex items-center gap-3 px-3 py-2">
              <span className="min-w-0 flex-1 truncate text-lg" style={{ fontFamily: `"${f.family}", sans-serif` }}>{f.family}</span>
              <span className="text-xs text-muted-foreground">{f.bundled ? "built in" : f.file}</span>
              {!f.bundled && <Button variant="ghost" size="icon" aria-label={`Delete ${f.family}`} onClick={() => deleteFont.mutate(f.file)}><Trash2 /></Button>}
            </li>
          ))}
        </ul>
        <FontFaces fonts={data.fonts} />
      </section>
    </div>
  )
}

function SoundRow({ sound, onDelete }: { sound: SoundItem; onDelete: () => void }) {
  const audio = useRef<HTMLAudioElement>(null)
  const [playing, setPlaying] = useState(false)
  return (
    <li className="flex items-center gap-3 px-3 py-2 text-sm">
      <Button variant="ghost" size="icon" aria-label={`Play ${sound.name}`} onClick={() => {
        const a = audio.current
        if (!a) return
        if (playing) { a.pause(); a.currentTime = 0 } else void a.play()
      }}>
        <Play className={cn(playing && "text-primary")} />
      </Button>
      <audio ref={audio} src={`/api/sounds/${encodeURIComponent(sound.name)}/file`} preload="none"
        onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} />
      <span className="min-w-0 flex-1 truncate">{sound.name}</span>
      <span className="text-xs text-muted-foreground timecode">{sound.duration ? tc(sound.duration) : "–"}</span>
      <span className="w-14 text-right text-xs text-muted-foreground">{size(sound.size)}</span>
      <Button variant="ghost" size="icon" aria-label={`Delete ${sound.name}`} onClick={onDelete}><Trash2 /></Button>
    </li>
  )
}

/** @font-face rules so the font list shows each font in itself. */
function FontFaces({ fonts }: { fonts: { family: string; file: string }[] }) {
  const css = fonts.map((f) => `@font-face{font-family:"${f.family}";src:url(/api/fonts/${encodeURIComponent(f.file)}/file);font-display:swap}`).join("")
  return <style>{css}</style>
}
