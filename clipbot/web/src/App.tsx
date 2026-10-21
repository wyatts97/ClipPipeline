import { useQuery } from "@tanstack/react-query"
import { Navigate, Route, Routes } from "react-router"

import { Shell } from "@/components/shell"
import { api } from "@/lib/api"
import { EditorPage } from "@/pages/editor"
import { JobsPage } from "@/pages/jobs"
import { LoginPage } from "@/pages/login"
import { LogsPage } from "@/pages/logs"
import { OverviewPage } from "@/pages/overview"
import { PublishingPage } from "@/pages/publishing"
import { ReviewPage } from "@/pages/review"
import { ScansPage } from "@/pages/scans"
import { StoragePage } from "@/pages/storage"
import { StreamersPage } from "@/pages/streamers"
import { TemplatesPage } from "@/pages/templates"

export function App() {
  const session = useQuery({ queryKey: ["session"], queryFn: api.session, staleTime: 60_000 })

  if (session.isPending) return <div className="min-h-screen bg-background" />
  if (session.isError) {
    return (
      <div className="grid min-h-screen place-items-center p-6 text-sm text-muted-foreground">
        clipbot isn't answering. Check that the container is running: docker compose ps clipbot
      </div>
    )
  }
  if (!session.data.configured || !session.data.authenticated) {
    return <LoginPage configured={session.data.configured} />
  }

  return (
    <Routes>
      <Route element={<Shell />}>
        <Route index element={<OverviewPage />} />
        <Route path="review" element={<ReviewPage />} />
        <Route path="review/:clipId/edit" element={<EditorPage />} />
        <Route path="publishing" element={<PublishingPage />} />
        <Route path="templates" element={<TemplatesPage />} />
        <Route path="jobs" element={<JobsPage />} />
        <Route path="jobs/:jobId" element={<JobsPage />} />
        <Route path="scans" element={<ScansPage />} />
        <Route path="scans/:scanId" element={<ScansPage />} />
        <Route path="streamers" element={<StreamersPage />} />
        <Route path="storage" element={<StoragePage />} />
        <Route path="logs" element={<LogsPage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  )
}
