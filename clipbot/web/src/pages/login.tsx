import { useMutation, useQueryClient } from "@tanstack/react-query"
import { useState } from "react"

import { Wordmark } from "@/components/brand"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { api, ApiError } from "@/lib/api"

export function LoginPage({ configured }: { configured: boolean }) {
  const qc = useQueryClient()
  const [password, setPassword] = useState("")
  const login = useMutation({
    mutationFn: () => api.login(password),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["session"] }),
  })
  const error = login.error instanceof ApiError ? login.error.message : login.error ? "Couldn't sign in." : null

  return (
    <div className="grid min-h-screen place-items-center px-4">
      <div className="w-full max-w-sm">
        <Wordmark className="mb-10 text-lg" />
        {configured ? (
          <form className="space-y-4" onSubmit={(e) => { e.preventDefault(); login.mutate() }}>
            <div className="space-y-2">
              <Label htmlFor="password">Dashboard password</Label>
              <Input id="password" type="password" autoFocus autoComplete="current-password" value={password}
                onChange={(e) => setPassword(e.target.value)} aria-invalid={!!error} />
              <p className="text-sm text-muted-foreground">It's DASHBOARD_PASSWORD in the server's .env file.</p>
            </div>
            {error && <p className="text-sm text-destructive">{error}</p>}
            <Button type="submit" className="w-full" disabled={!password || login.isPending}>
              {login.isPending ? "Signing in…" : "Sign in"}
            </Button>
          </form>
        ) : (
          <div className="space-y-3 text-sm">
            <p>The dashboard has no password yet.</p>
            <p className="text-muted-foreground">
              Set <code className="font-mono text-foreground">DASHBOARD_PASSWORD</code> in the server's .env, then
              restart clipbot:
            </p>
            <pre className="rounded-md border border-border bg-card px-3 py-2 font-mono text-xs">docker compose up -d clipbot</pre>
          </div>
        )}
      </div>
    </div>
  )
}
