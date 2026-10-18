import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"
import { useSearchParams } from "react-router"
import { toast } from "sonner"

import { LogLines } from "@/components/log-lines"
import { Light } from "@/components/status"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group"
import { api, type LogLevel } from "@/lib/api"
import { clock } from "@/lib/format"

export function LogsPage() {
  const [params, setParams] = useSearchParams()
  const tab = params.get("tab") ?? "log"
  return (
    <div className="mx-auto max-w-7xl space-y-6">
      <h1 className="text-2xl font-semibold tracking-tight">Logs & errors</h1>
      <Tabs value={tab} onValueChange={(v) => setParams(v === "log" ? {} : { tab: v })}>
        <TabsList>
          <TabsTrigger value="log">clipbot log</TabsTrigger>
          <TabsTrigger value="telegram">Telegram delivery</TabsTrigger>
          <TabsTrigger value="n8n">n8n runs</TabsTrigger>
        </TabsList>
        <TabsContent value="log" className="mt-4"><LogTab /></TabsContent>
        <TabsContent value="telegram" className="mt-4"><TelegramTab /></TabsContent>
        <TabsContent value="n8n" className="mt-4"><N8nTab /></TabsContent>
      </Tabs>
    </div>
  )
}

function LogTab() {
  const [level, setLevel] = useState<LogLevel>("warning")
  const [source, setSource] = useState("all")
  const [text, setText] = useState("")
  const [q, setQ] = useState("")
  const logs = useInfiniteQuery({
    queryKey: ["logs", level, source, q],
    queryFn: ({ pageParam }) => api.logs({ level, source: source === "all" ? undefined : source, q, before: pageParam }),
    initialPageParam: null as number | null,
    getNextPageParam: (last) => last.next_before,
    refetchInterval: 10_000,
  })
  const items = logs.data?.pages.flatMap((p) => p.items) ?? []
  const sources = logs.data?.pages[0]?.sources ?? []

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <ToggleGroup type="single" variant="outline" size="sm" value={level} onValueChange={(v) => v && setLevel(v as LogLevel)}>
          <ToggleGroupItem value="error">Errors</ToggleGroupItem>
          <ToggleGroupItem value="warning">Warnings and errors</ToggleGroupItem>
          <ToggleGroupItem value="info">Everything</ToggleGroupItem>
        </ToggleGroup>
        <Select value={source} onValueChange={setSource}>
          <SelectTrigger size="sm" className="w-40"><SelectValue /></SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All parts</SelectItem>
            {sources.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
          </SelectContent>
        </Select>
        <form className="ml-auto flex gap-2" onSubmit={(e) => { e.preventDefault(); setQ(text) }}>
          <Input value={text} onChange={(e) => setText(e.target.value)} placeholder="Search messages" className="h-8 w-56" />
          <Button size="sm" variant="outline" type="submit">Search</Button>
        </form>
      </div>
      {logs.isPending ? null : !items.length ? (
        <div className="rounded-md border border-dashed border-border px-6 py-12 text-center text-sm text-muted-foreground">
          {level === "info" && !q ? "Nothing logged yet." : "Nothing matches. Clean so far."}
        </div>
      ) : (
        <>
          <LogLines items={items} />
          {logs.hasNextPage && (
            <Button variant="outline" onClick={() => logs.fetchNextPage()} disabled={logs.isFetchingNextPage}>Show older</Button>
          )}
        </>
      )}
    </div>
  )
}

function TelegramTab() {
  const qc = useQueryClient()
  const outbox = useQuery({ queryKey: ["outbox"], queryFn: api.outbox, refetchInterval: 5000 })
  const refresh = () => { qc.invalidateQueries({ queryKey: ["outbox"] }); qc.invalidateQueries({ queryKey: ["overview"] }) }
  const resend = useMutation({ mutationFn: api.retryEvent, onSuccess: () => { toast.success("Sending it again"); refresh() } })
  const drop = useMutation({ mutationFn: api.deleteEvent, onSuccess: () => { toast("Deleted"); refresh() } })
  const items = outbox.data?.items ?? []

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Messages clipbot hands to n8n for Telegram. They wait here while n8n is down or the workflow is unpublished, and are
        marked failed after 40 tries.
      </p>
      {outbox.isPending ? null : !items.length ? (
        <div className="rounded-md border border-dashed border-border px-6 py-12 text-center text-sm text-muted-foreground">
          Everything was delivered.
        </div>
      ) : (
        <ol className="divide-y divide-border rounded-md border border-border bg-card">
          {items.map((e) => (
            <li key={e.id} className="flex flex-col gap-3 px-4 py-3 sm:flex-row sm:items-start">
              <div className="min-w-0 flex-1 text-sm">
                <div className="flex items-center gap-3">
                  <Light tone={e.failed ? "bad" : "working"} />
                  <span className={e.failed ? "text-destructive" : undefined}>{e.failed ? "Gave up" : "Waiting to send"}</span>
                  <span className="text-muted-foreground">{e.event === "clip_ready" ? "Clip preview" : e.event === "choice" ? "Scan moment" : "Message"}</span>
                  <span className="text-muted-foreground">{e.attempts} {e.attempts === 1 ? "try" : "tries"}</span>
                  {!e.failed && <span className="text-muted-foreground">{e.next_try <= Date.now() / 1000 ? "sending now" : `next try ${clock(e.next_try)}`}</span>}
                </div>
                <p className="mt-1 line-clamp-3 whitespace-pre-line text-muted-foreground">{e.text}</p>
              </div>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" onClick={() => resend.mutate(e.id)} disabled={resend.isPending}>Send again</Button>
                <Button size="sm" variant="ghost" onClick={() => drop.mutate(e.id)} disabled={drop.isPending}>Delete</Button>
              </div>
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

function N8nTab() {
  const runs = useQuery({ queryKey: ["n8n-failures"], queryFn: api.n8nFailures, refetchInterval: 30_000 })
  const editor = `${window.location.protocol}//${window.location.hostname}:5678`
  if (runs.isPending) return null
  if (!runs.data?.configured) {
    return (
      <div className="max-w-2xl space-y-2 text-sm">
        <p>Failed n8n workflow runs show up here once clipbot can ask n8n for them.</p>
        <ol className="list-decimal space-y-1 pl-5 text-muted-foreground">
          <li>In n8n, open Settings, then n8n API, and create an API key.</li>
          <li>Put it in the server's .env as <code className="font-mono text-foreground">N8N_API_KEY=</code></li>
          <li>Restart clipbot: <code className="font-mono text-foreground">docker compose up -d clipbot</code></li>
        </ol>
      </div>
    )
  }
  if (runs.data.error) return <p className="text-sm text-destructive">{runs.data.error}</p>
  if (!runs.data.items.length) {
    return <div className="rounded-md border border-dashed border-border px-6 py-12 text-center text-sm text-muted-foreground">No failed runs.</div>
  }
  return (
    <ol className="divide-y divide-border rounded-md border border-border bg-card">
      {runs.data.items.map((r) => (
        <li key={r.id} className="flex items-center gap-4 px-4 py-3 text-sm">
          <Light tone="bad" />
          <span>Run {r.id}</span>
          <span className="text-muted-foreground">{r.started ? clock(Date.parse(r.started) / 1000) : "–"}</span>
          <span className="text-muted-foreground">{r.mode}</span>
          <a className="ml-auto text-primary hover:underline" target="_blank" rel="noreferrer"
            href={`${editor}/workflow/${r.workflow_id}/executions/${r.id}`}>Open in n8n</a>
        </li>
      ))}
    </ol>
  )
}
