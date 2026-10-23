import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { LoaderCircle, PackageOpen, Play, Plus, Search, Square } from "lucide-react"
import { useEffect, useRef, useState } from "react"
import { toast } from "sonner"

import { NativeSelect } from "@/components/native-select"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, type FoundSound, type SoundCategory, type SoundItem, type SoundsResponse } from "@/lib/api"
import { tc } from "@/lib/format"

export const CATEGORY_LABEL: Record<SoundCategory, string> = {
  impact: "Impact", reaction: "Reactions", fail: "Fails", hype: "Hype", transition: "Transitions",
  funny: "Funny", suspense: "Suspense", music: "Music", other: "Other",
}

export const soundUrl = (name: string) => `/api/sounds/${encodeURIComponent(name)}/file`

/** The sound library, refreshed every second or two while the meme pack downloads. */
export function useSounds() {
  return useQuery({
    queryKey: ["sounds"], queryFn: api.sounds,
    refetchInterval: (q) => (q.state.data?.pack.running ? 1500 : false),
  })
}

/** Plays one preview at a time; playing the same key again stops it. */
export function usePreview() {
  const audio = useRef<HTMLAudioElement | null>(null)
  const [playing, setPlaying] = useState<string | null>(null)
  useEffect(() => () => audio.current?.pause(), [])
  const play = (key: string, url: string, volume = 1) => {
    audio.current?.pause()
    if (playing === key) {
      setPlaying(null)
      return
    }
    const a = new Audio(url)
    a.volume = Math.min(1, Math.max(0, volume))
    a.onended = a.onerror = () => setPlaying((p) => (p === key ? null : p))
    audio.current = a
    setPlaying(key)
    a.play().catch(() => setPlaying(null))
  }
  return { playing, play }
}

/** Where a sound came from and its license, in a few words. */
export function soundCredit(s: Pick<SoundItem, "source" | "license" | "author">): string {
  if (s.license) return [s.license, s.author].filter(Boolean).join(" · ")
  return { upload: "uploaded", myinstants: "Myinstants (meme, unlicensed)", freesound: "Freesound", incompetech: "incompetech" }[s.source]
}

export function PlayButton({ playing, label, onClick, className }: {
  playing: boolean; label: string; onClick: () => void; className?: string
}) {
  return (
    <Button variant="ghost" size="icon" aria-label={playing ? `Stop ${label}` : `Play ${label}`} onClick={onClick} className={className}>
      {playing ? <Square className="text-primary" /> : <Play />}
    </Button>
  )
}

/** "Install meme pack": downloads ~30 popular meme sounds onto the server, then says what it got. */
export function MemePackButton({ data }: { data: SoundsResponse | undefined }) {
  const qc = useQueryClient()
  const running = !!data?.pack.running
  const was = useRef(running)
  useEffect(() => {
    const result = data?.pack.result
    if (was.current && !running && result) {
      const failed = Object.keys(result.failed)
      const msg = `Meme pack: ${result.added.length} added${result.kept.length ? `, ${result.kept.length} already here` : ""}`
      if (failed.length) toast.warning(`${msg}, ${failed.length} couldn't download (${failed.slice(0, 4).join(", ")}${failed.length > 4 ? "…" : ""})`)
      else toast.success(msg)
      qc.invalidateQueries({ queryKey: ["templates"] })
    }
    was.current = running
  }, [running, data?.pack.result, qc])
  const install = useMutation({
    mutationFn: api.installMemePack,
    onSuccess: () => { was.current = true; qc.invalidateQueries({ queryKey: ["sounds"] }) },
    onError: (e) => toast.error(e.message),
  })
  const installed = data ? data.items.filter((s) => s.source === "myinstants").length : 0
  return (
    <Button variant="outline" onClick={() => install.mutate()} disabled={running || install.isPending}
      title={`About ${data?.pack.size ?? 30} meme sounds from Myinstants, downloaded onto your server`}>
      {running ? <LoaderCircle className="animate-spin" /> : <PackageOpen />}
      {running ? "Downloading…" : installed ? "Update meme pack" : "Install meme pack"}
    </Button>
  )
}

/** Search Myinstants (memes) or Freesound (CC0, safe anywhere) and import sounds into the library. */
export function SoundSearch({ freesound, onImported, compact }: {
  freesound: boolean; onImported?: (sound: SoundItem) => void; compact?: boolean
}) {
  const qc = useQueryClient()
  const [text, setText] = useState("")
  const [submitted, setSubmitted] = useState("")
  const [source, setSource] = useState<FoundSound["source"]>("myinstants")
  const [category, setCategory] = useState<SoundCategory>("other")
  const [added, setAdded] = useState<Record<string, string>>({})
  const { playing, play } = usePreview()
  const found = useQuery({
    queryKey: ["sound-search", source, submitted], queryFn: () => api.searchSounds(submitted, source),
    enabled: !!submitted && (source === "myinstants" || freesound), staleTime: 5 * 60_000, retry: false,
  })
  const add = useMutation({
    mutationFn: (item: FoundSound) => api.importSound(item, category),
    onSuccess: (sound, item) => {
      setAdded((a) => ({ ...a, [`${item.source}:${item.id}`]: sound.name }))
      toast.success(`Added ${sound.name}`)
      qc.invalidateQueries({ queryKey: ["sounds"] })
      qc.invalidateQueries({ queryKey: ["templates"] })
      onImported?.(sound)
    },
    onError: (e) => toast.error(e.message),
  })

  return (
    <div className="space-y-2">
      <form className="flex flex-wrap items-center gap-2" onSubmit={(e) => { e.preventDefault(); setSubmitted(text.trim()) }}>
        <div className="relative min-w-40 flex-1">
          <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input aria-label="Search for sounds" placeholder={source === "myinstants" ? "Search meme sounds…" : "Search royalty-free sounds…"}
            className="pl-8" value={text} onChange={(e) => setText(e.target.value)} />
        </div>
        <ToggleGroup type="single" variant="outline" size="sm" value={source}
          onValueChange={(v) => v && setSource(v as FoundSound["source"])}>
          <ToggleGroupItem value="myinstants" title="Myinstants: meme sounds, not licensed">Memes</ToggleGroupItem>
          <ToggleGroupItem value="freesound" title="Freesound: CC0, free to use anywhere">CC0</ToggleGroupItem>
        </ToggleGroup>
        {!compact && (
          <NativeSelect aria-label="Add found sounds as" className="h-8 w-auto text-xs" value={category}
            onChange={(e) => setCategory(e.target.value as SoundCategory)}>
            {Object.entries(CATEGORY_LABEL).map(([k, v]) => <option key={k} value={k}>Add to {v}</option>)}
          </NativeSelect>
        )}
      </form>
      {source === "freesound" && !freesound ? (
        <p className="text-xs text-muted-foreground">
          Royalty-free search needs a free Freesound key: get one at freesound.org/apiv2/apply and add
          <code className="mx-1 font-mono">FREESOUND_API_KEY</code>to .env.
        </p>
      ) : found.isError ? (
        <p className="text-xs text-destructive">{found.error.message}</p>
      ) : found.isFetching ? (
        <p className="flex items-center gap-2 text-xs text-muted-foreground"><LoaderCircle className="size-3.5 animate-spin" /> Searching…</p>
      ) : found.data && !found.data.items.length ? (
        <p className="text-xs text-muted-foreground">Nothing found for "{submitted}".</p>
      ) : found.data ? (
        <ul className={cn("divide-y divide-border overflow-y-auto rounded-md border border-border", compact ? "max-h-56" : "max-h-72")}>
          {found.data.items.map((item) => {
            const key = `${item.source}:${item.id}`
            return (
              <li key={key} className="flex items-center gap-2 px-2 py-1 text-sm">
                <PlayButton playing={playing === key} label={item.title} onClick={() => play(key, item.preview_url)} />
                <span className="min-w-0 flex-1 truncate" title={item.title}>{item.title}</span>
                {item.duration != null && <span className="text-xs text-muted-foreground timecode">{tc(item.duration)}</span>}
                {item.author && <span className="hidden max-w-24 truncate text-xs text-muted-foreground sm:inline">{item.author}</span>}
                <Button variant={added[key] ? "ghost" : "outline"} size="sm" disabled={!!added[key] || add.isPending}
                  onClick={() => add.mutate(item)}>
                  {added[key] ? "Added" : <><Plus /> Add</>}
                </Button>
              </li>
            )
          })}
        </ul>
      ) : null}
    </div>
  )
}
