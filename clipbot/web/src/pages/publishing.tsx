import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { cn } from "cn"
import { Download, ExternalLink } from "lucide-react"
import { useState } from "react"
import { Link } from "react-router"
import { toast } from "sonner"

import { ClipThumb } from "@/components/clip-thumb"
import { Light, Status } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { Skeleton } from "@/components/ui/skeleton"
import { api, type Channel, type Clip, type PostGroup, type PostItem, type Publishing, type PublishQueueItem } from "@/lib/api"
import { ago, fromLocalInput, publishPlatformName, toLocalInput, when } from "@/lib/format"

function usePublishActions() {
  const qc = useQueryClient()
  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["publishing"] })
    qc.invalidateQueries({ queryKey: ["overview"] })
  }
  const publish = useMutation({
    mutationFn: ({ clip, at, now }: { clip: Clip; at?: number | null; now?: boolean }) => api.publish(clip.id, { at, now }),
    onSuccess: (_, { now, at }) => { toast.success(now ? "Posting now" : at ? `Moved to ${when(at)}` : "Scheduling for the next free slot"); refresh() },
  })
  const unschedule = useMutation({
    mutationFn: (clip: Clip) => api.unschedule(clip.id),
    onSuccess: (r) => { toast(`Cancelled ${r.cancelled} post${r.cancelled === 1 ? "" : "s"}`); refresh() },
  })
  return { publish, unschedule }
}

export function PublishingPage() {
  const q = useQuery({ queryKey: ["publishing"], queryFn: api.publishing, refetchInterval: 10_000 })
  const [moving, setMoving] = useState<{ clip: Clip; at: number } | null>(null)
  const d = q.data

  return (
    <div className="mx-auto max-w-7xl space-y-8">
      <div className="flex flex-wrap items-center gap-4">
        <h1 className="mr-auto text-2xl font-semibold tracking-tight">Publishing</h1>
        {d?.postiz_url && (
          <Button variant="outline" asChild>
            <a href={d.postiz_url} target="_blank" rel="noreferrer">Open Postiz <ExternalLink /></a>
          </Button>
        )}
      </div>

      {q.isPending ? <Skeleton className="h-40" /> : !d ? null : !d.configured ? <NotSetUp data={d} /> : (
        <>
          <Setup data={d} />
          {d.error && (
            <div className="rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 text-sm text-destructive">{d.error}</div>
          )}
          <Queue items={d.queue ?? []} />
          <Upcoming groups={d.upcoming ?? []} onMove={(clip, at) => setMoving({ clip, at })} />
          <Unscheduled clips={d.unscheduled ?? []} nextSlot={d.next_slot} onPick={(clip) => setMoving({ clip, at: d.next_slot ?? Date.now() / 1000 + 3600 })} />
          <Recent groups={d.recent ?? []} />
        </>
      )}

      <MoveDialog target={moving} nextSlot={d?.next_slot} onClose={() => setMoving(null)} />
    </div>
  )
}

function NotSetUp({ data }: { data: Publishing }) {
  return (
    <section className="max-w-2xl space-y-3 rounded-md border border-dashed border-border px-6 py-8 text-sm">
      <h2 className="text-base font-medium">Publishing is off</h2>
      <p className="text-muted-foreground">
        With Postiz running, ✅ Post schedules each approved clip on YouTube, TikTok and Instagram at the next free
        time of day ({data.slots}). Postiz needs its own hostname and a developer app per platform.
      </p>
      <p>On the server, run:</p>
      <pre className="rounded-md border border-border bg-background px-3 py-2 font-mono text-xs">bash setup.sh --publish</pre>
      <p className="text-muted-foreground">It walks you through the tunnel route, the app keys and the Postiz API key. The README's Publishing section explains each platform.</p>
    </section>
  )
}

function Setup({ data }: { data: Publishing }) {
  const channels = data.channels ?? []
  return (
    <section className="flex flex-wrap items-center gap-x-8 gap-y-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        {channels.length ? channels.map((c) => <ChannelChip key={c.id} channel={c} />) : (
          <span className="text-muted-foreground">No channels connected yet. Add them in Postiz (Add Channel).</span>
        )}
      </div>
      <div className="text-muted-foreground">
        Daily at <span className="text-foreground timecode">{data.slots}</span> ({data.timezone})
      </div>
      {data.next_slot && <div className="text-muted-foreground">Next free slot <span className="text-foreground">{when(data.next_slot)}</span></div>}
      {data.tiktok_mode !== "DIRECT_POST" && channels.some((c) => c.platform === "tiktok") && (
        <div className="text-muted-foreground">TikTok clips go to your TikTok drafts</div>
      )}
    </section>
  )
}

function ChannelChip({ channel }: { channel: Channel }) {
  return (
    <span className={cn("inline-flex items-center gap-2 rounded-sm border border-border px-2 py-1", channel.disabled && "opacity-50")}>
      {channel.picture ? <img src={channel.picture} alt="" className="size-4 rounded-full" /> : <Light tone="ok" />}
      <span className="text-muted-foreground">{publishPlatformName[channel.platform]}</span>
      <span>{channel.name}</span>
      {channel.disabled && <span className="text-xs text-muted-foreground">disabled</span>}
    </span>
  )
}

function Queue({ items }: { items: PublishQueueItem[] }) {
  const { publish } = usePublishActions()
  if (!items.length) return null
  return (
    <section className="space-y-3">
      <h2 className="font-medium">Being scheduled</h2>
      <ul className="divide-y divide-border rounded-md border border-border bg-card">
        {items.map((item) => (
          <li key={item.clip.id} className="flex items-center gap-4 px-3 py-2.5">
            <ClipThumb clip={item.clip} className="w-10 shrink-0" showDuration={false} />
            <div className="min-w-0 flex-1">
              <div className="truncate">{item.clip.title || item.clip.id}</div>
              {item.error && <div className={cn("text-xs", item.status === "failed" ? "text-destructive" : "text-muted-foreground")}>{item.error}</div>}
            </div>
            <Status status={item.status} />
            {item.status === "failed" && (
              <Button size="sm" onClick={() => publish.mutate({ clip: item.clip })} disabled={publish.isPending}>Retry</Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}

function PostChips({ posts }: { posts: PostItem[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {posts.map((p) => {
        const tone = p.status === "published" ? "ok" : p.status === "failed" ? "bad" : "idle"
        const label = (
          <>
            <Light tone={tone} />
            {publishPlatformName[p.platform]}
            {p.url && <ExternalLink className="size-3" />}
          </>
        )
        const className = cn("inline-flex items-center gap-1.5 rounded-sm border border-border px-1.5 py-0.5 text-xs",
          p.status === "failed" && "border-destructive/50 text-destructive", p.status === "cancelled" && "text-muted-foreground line-through")
        return p.url ? (
          <a key={p.id} href={p.url} target="_blank" rel="noreferrer" className={cn(className, "hover:bg-accent")} title={p.channel ?? undefined}>{label}</a>
        ) : (
          <span key={p.id} className={className} title={p.error ?? p.channel ?? undefined}>{label}</span>
        )
      })}
    </div>
  )
}

function Upcoming({ groups, onMove }: { groups: PostGroup[]; onMove: (clip: Clip, at: number) => void }) {
  const { publish, unschedule } = usePublishActions()
  return (
    <section className="space-y-3">
      <h2 className="font-medium">Up next</h2>
      {!groups.length ? (
        <p className="text-sm text-muted-foreground">Nothing scheduled. Approve clips in <Link to="/review" className="text-primary hover:underline">Review</Link> or Telegram.</p>
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border bg-card">
          {groups.map((g) => (
            <li key={g.clip.id} className="flex flex-wrap items-center gap-4 px-3 py-3">
              <ClipThumb clip={g.clip} className="w-12 shrink-0" />
              <div className="min-w-0 flex-1 space-y-1.5">
                <div className="flex flex-wrap items-baseline gap-x-3">
                  <span className="font-medium">{when(g.publish_at)}</span>
                  <span className="truncate text-muted-foreground">{g.clip.title || g.clip.id}</span>
                </div>
                <PostChips posts={g.posts} />
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={() => publish.mutate({ clip: g.clip, now: true })} disabled={publish.isPending}>Post now</Button>
                <Button size="sm" variant="outline" onClick={() => onMove(g.clip, g.publish_at)}>Move</Button>
                <Button size="sm" variant="ghost" onClick={() => unschedule.mutate(g.clip)} disabled={unschedule.isPending}>Unschedule</Button>
                <Button size="icon" variant="ghost" asChild aria-label="Download the clip">
                  <a href={g.clip.download_url} download><Download /></a>
                </Button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function Unscheduled({ clips, nextSlot, onPick }: { clips: Clip[]; nextSlot?: number; onPick: (clip: Clip) => void }) {
  const { publish } = usePublishActions()
  if (!clips.length) return null
  return (
    <section className="space-y-3">
      <div>
        <h2 className="font-medium">Approved, not scheduled</h2>
        <p className="text-sm text-muted-foreground">Approved before publishing was set up, or unscheduled since.</p>
      </div>
      <div className="grid grid-cols-2 gap-4 sm:grid-cols-4 lg:grid-cols-6">
        {clips.map((clip) => (
          <div key={clip.id} className="space-y-2">
            <ClipThumb clip={clip} />
            <div className="truncate text-sm">{clip.title || clip.id}</div>
            <div className="flex gap-1.5">
              <Button size="sm" className="flex-1" onClick={() => publish.mutate({ clip })} disabled={publish.isPending}
                title={nextSlot ? `Next free slot: ${when(nextSlot)}` : undefined}>Schedule</Button>
              <Button size="sm" variant="outline" onClick={() => onPick(clip)}>Pick…</Button>
            </div>
          </div>
        ))}
      </div>
    </section>
  )
}

function Recent({ groups }: { groups: PostGroup[] }) {
  if (!groups.length) return null
  return (
    <section className="space-y-3">
      <h2 className="font-medium">Posted and past</h2>
      <ul className="divide-y divide-border rounded-md border border-border bg-card">
        {groups.map((g) => (
          <li key={g.clip.id} className="flex flex-wrap items-center gap-4 px-3 py-2.5">
            <ClipThumb clip={g.clip} className="w-9 shrink-0" showDuration={false} />
            <div className="min-w-0 flex-1">
              <div className="truncate">{g.clip.title || g.clip.id}</div>
              <div className="text-xs text-muted-foreground">{when(g.publish_at)} · {ago(g.publish_at)}</div>
            </div>
            <PostChips posts={g.posts} />
            <Status status={g.status} className="w-24" />
            <Button size="icon" variant="ghost" asChild aria-label="Download the clip">
              <a href={g.clip.download_url} download><Download /></a>
            </Button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function MoveDialog({ target, nextSlot, onClose }: { target: { clip: Clip; at: number } | null; nextSlot?: number; onClose: () => void }) {
  return (
    <Dialog open={!!target} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-md">
        {target && <MoveForm key={`${target.clip.id}-${target.at}`} target={target} nextSlot={nextSlot} onDone={onClose} />}
      </DialogContent>
    </Dialog>
  )
}

function MoveForm({ target, nextSlot, onDone }: { target: { clip: Clip; at: number }; nextSlot?: number; onDone: () => void }) {
  const { publish } = usePublishActions()
  const [value, setValue] = useState(() => toLocalInput(target.at))
  const at = fromLocalInput(value)
  const tooSoon = at !== null && at < Date.now() / 1000 + 60

  return (
    <>
      <DialogHeader>
        <DialogTitle>Post {target.clip.title ? `“${target.clip.title}”` : "this clip"} at…</DialogTitle>
        <DialogDescription>All its channels move together. Times are in your browser's time zone.</DialogDescription>
      </DialogHeader>
      <form id="move-post" className="space-y-1.5" onSubmit={(ev) => {
        ev.preventDefault()
        if (at && !tooSoon) publish.mutate({ clip: target.clip, at }, { onSuccess: onDone })
      }}>
        <Label htmlFor="move-at">Date and time</Label>
        <Input id="move-at" type="datetime-local" value={value} onChange={(ev) => setValue(ev.target.value)} aria-invalid={tooSoon} />
        {tooSoon && <p className="text-xs text-destructive">Pick a time in the future, or use Post now.</p>}
      </form>
      <DialogFooter className="gap-2 sm:justify-between">
        {nextSlot ? (
          <Button variant="ghost" onClick={() => publish.mutate({ clip: target.clip, at: nextSlot }, { onSuccess: onDone })}>
            Next free slot ({when(nextSlot)})
          </Button>
        ) : <span />}
        <Button type="submit" form="move-post" disabled={!at || tooSoon || publish.isPending}>Schedule</Button>
      </DialogFooter>
    </>
  )
}
