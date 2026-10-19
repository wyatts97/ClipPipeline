import type { Platform } from "./api"

/** 3723 -> "1:02:03", 75 -> "1:15" (same as clipbot's format_timestamp). */
export function tc(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "–"
  const s = Math.max(0, Math.round(seconds))
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  const sec = s % 60
  const mm = String(m).padStart(h ? 2 : 1, "0")
  return h ? `${h}:${mm}:${String(sec).padStart(2, "0")}` : `${mm}:${String(sec).padStart(2, "0")}`
}

/** "1:02:03", "62:03", "3723", "1h2m3s" -> seconds; null when it isn't a time. */
export function parseTc(text: string): number | null {
  const t = text.trim().toLowerCase()
  if (!t) return null
  if (/^\d+(\.\d+)?$/.test(t)) return Number(t)
  if (/^\d+(:\d{1,2}){1,2}$/.test(t)) return t.split(":").reduce((acc, part) => acc * 60 + Number(part), 0)
  const m = t.match(/^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$/)
  if (m && (m[1] || m[2] || m[3])) return Number(m[1] ?? 0) * 3600 + Number(m[2] ?? 0) * 60 + Number(m[3] ?? 0)
  return null
}

export function ago(ts: number | null | undefined, now = Date.now() / 1000): string {
  if (!ts) return "–"
  const d = Math.max(0, now - ts)
  if (d < 45) return "just now"
  if (d < 3600) return `${Math.round(d / 60)} min ago`
  if (d < 86400) return `${Math.round(d / 3600)} h ago`
  if (d < 2 * 86400) return "yesterday"
  return `${Math.round(d / 86400)} days ago`
}

export function clock(ts: number | null | undefined, seconds = true): string {
  if (!ts) return "–"
  const date = new Date(ts * 1000)
  const today = new Date()
  const time = date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", ...(seconds ? { second: "2-digit" } : {}) })
  if (date.toDateString() === today.toDateString()) return time
  return `${date.toLocaleDateString([], { month: "short", day: "numeric" })}, ${time}`
}

export function bytes(n: number): string {
  const gb = n / 1024 ** 3
  return gb >= 10 ? `${Math.round(gb)} GB` : `${gb.toFixed(1)} GB`
}

/** File sizes: "820 MB", "1.4 GB", "–" for nothing. */
export function size(n: number | null | undefined): string {
  if (!n) return "–"
  if (n >= 1024 ** 3) return bytes(n)
  if (n >= 1024 ** 2) return `${Math.round(n / 1024 ** 2)} MB`
  return "< 1 MB"
}

/** "Tue 21 Oct, 17:00" in the browser's time zone. */
export function when(ts: number | null | undefined): string {
  if (!ts) return "–"
  const date = new Date(ts * 1000)
  const day = date.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" })
  return `${day}, ${date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`
}

/** For <input type="datetime-local">: local "YYYY-MM-DDTHH:mm" and back. */
export function toLocalInput(ts: number): string {
  const d = new Date(ts * 1000)
  const pad = (n: number) => String(n).padStart(2, "0")
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

export function fromLocalInput(value: string): number | null {
  const t = new Date(value).getTime()
  return Number.isFinite(t) ? t / 1000 : null
}

export const publishPlatformName = { youtube: "YouTube", tiktok: "TikTok", instagram: "Instagram" } as const

export function elapsed(since: number, now = Date.now() / 1000): string {
  return tc(Math.max(0, now - since))
}

export const platformName: Record<Platform, string> = {
  twitch: "Twitch",
  youtube: "YouTube",
  kick: "Kick",
  other: "Link",
}

/** What to show for a source link: the platform's own id or path, not the whole URL. */
export function sourceLabel(url: string | null | undefined): string {
  if (!url) return "–"
  try {
    const u = new URL(url)
    const v = u.searchParams.get("v")
    if (v) return v
    const path = u.pathname.replace(/\/+$/, "").split("/").filter(Boolean)
    if (u.hostname.includes("twitch.tv") && path[0] === "videos") return path[1] ?? url
    if (u.hostname.includes("kick.com") && path.length >= 3) return `${path[0]}/${path[2].slice(0, 8)}`
    return path.slice(-2).join("/") || u.hostname
  } catch {
    return url
  }
}

/** Mirrors clipbot's platforms.timestamp_link. */
export function timestampLink(platform: Platform, vodId: string | null, url: string, seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (platform === "twitch" && vodId) {
    const h = Math.floor(s / 3600)
    const m = Math.floor((s % 3600) / 60)
    return `https://www.twitch.tv/videos/${vodId}?t=${h}h${m}m${s % 60}s`
  }
  if (platform === "youtube" && vodId) return `https://youtu.be/${vodId}?t=${s}`
  if (platform === "kick") return `${url.split("?")[0]}?t=${s}`
  return url
}
