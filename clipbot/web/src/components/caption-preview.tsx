import { cn } from "cn"
import { useEffect, useState } from "react"

import type { CaptionStyle, FontItem } from "@/lib/api"

const SAMPLE = ["NO", "WAY", "HE", "ACTUALLY", "DID", "THAT"]
const loaded = new Set<string>()

/** Loads a caption font from clipbot once, so the preview uses the real file. */
function useFont(fonts: FontItem[], family: string) {
  const [, bump] = useState(0)
  useEffect(() => {
    const font = fonts.find((f) => f.family === family)
    if (!font || loaded.has(family)) return
    loaded.add(family)
    const face = new FontFace(family, `url(/api/fonts/${encodeURIComponent(font.file)}/file)`)
    face.load().then((f) => { document.fonts.add(f); bump((n) => n + 1) }).catch(() => loaded.delete(family))
  }, [fonts, family])
}

/** A 9:16 frame with the caption look applied, the active word moving like on the clip. */
export function CaptionPreview({ style, fonts, className, words = SAMPLE }: {
  style: CaptionStyle
  fonts: FontItem[]
  className?: string
  words?: string[]
}) {
  useFont(fonts, style.font_name)
  const [active, setActive] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => setActive((i) => (i + 1) % words.length), 420)
    return () => window.clearInterval(id)
  }, [words.length])

  // Same grouping as the burned captions: up to max_chars per line.
  const lines: string[][] = []
  for (const w of words) {
    const line = lines[lines.length - 1]
    if (!line || line.join(" ").length + w.length + 1 > style.max_chars) lines.push([w])
    else line.push(w)
  }
  const lineIndex = lines.findIndex((l) => l.includes(words[active]))
  const shown = lines[Math.max(0, lineIndex)] ?? []
  const size = `${(style.font_size / 288) * 100 * 0.85}cqh`
  const stroke = style.bg_opacity > 0 ? 0 : Math.max(style.shadow ? 0 : 1, style.border_width)

  return (
    <div className={cn("@container relative aspect-[9/16] overflow-hidden rounded-md bg-gradient-to-b from-slate-600 via-slate-800 to-slate-950",
      className)} style={{ containerType: "size" }}>
      {style.enabled ? (
        <div className={cn("absolute inset-x-[4%] flex justify-center text-center leading-tight",
          style.position === "top" ? "top-[12%]" : style.position === "middle" ? "top-1/2 -translate-y-1/2" : "bottom-[15%]")}
          style={{ fontFamily: `"${style.font_name}", Anton, sans-serif`, fontSize: size, fontWeight: 700 }}>
          <span className={cn(style.bg_opacity > 0 && "rounded-sm px-[0.2em]")}
            style={{ background: style.bg_opacity > 0 ? hexAlpha(style.bg_color, style.bg_opacity) : undefined }}>
            {shown.map((w, i) => {
              const isActive = w === words[active]
              const hidden = style.reveal && shown.indexOf(words[active]) < i
              const box = isActive && style.effect === "highlight"
              return (
                <span key={`${w}-${i}`}
                  className={cn("inline-block px-[0.12em] transition-transform duration-100", box && "rounded-[0.12em]")}
                  style={{
                    color: box ? (lum(style.highlight_color) > 0.6 ? "#000" : style.font_color)
                      : isActive ? (style.effect === "glow" || style.effect === "box" ? "#fff" : style.highlight_color)
                        : dim(style.font_color, style.base_opacity),
                    background: box ? style.highlight_color : undefined,
                    WebkitTextStroke: box ? undefined : `${stroke * 0.05}em ${isActive && style.effect === "box" ? style.highlight_color : style.border_color}`,
                    paintOrder: "stroke fill",
                    textShadow: isActive && style.effect === "glow" ? `0 0 0.3em ${style.highlight_color}`
                      : style.shadow ? `0 ${style.shadow * 0.03}em ${style.shadow * 0.05}em rgba(0,0,0,.6)` : undefined,
                    transform: isActive && style.effect === "pop" ? "scale(1.08)" : undefined,
                    opacity: hidden ? 0 : 1,
                  }}>
                  {style.uppercase ? w : w.toLowerCase()}
                </span>
              )
            })}
          </span>
        </div>
      ) : (
        <div className="absolute inset-0 grid place-items-center text-xs text-white/60">No captions</div>
      )}
    </div>
  )
}

function lum(hex: string) {
  const n = parseInt(hex.slice(1), 16)
  return (0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255)) / 255
}

function dim(hex: string, opacity: number) {
  const f = 0.5 + 0.5 * opacity
  const n = parseInt(hex.slice(1), 16)
  const c = [(n >> 16) & 255, (n >> 8) & 255, n & 255].map((v) => Math.min(255, Math.round(v * f)))
  return `rgb(${c.join(",")})`
}

function hexAlpha(hex: string, opacity: number) {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${opacity})`
}
