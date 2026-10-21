import { cn } from "cn"
import { useEffect, useLayoutEffect, useRef, useState } from "react"

import type { CaptionPosition, CaptionStyle, FontItem, HookStyle } from "@/lib/api"
import { roundedBoxPath } from "@/lib/box-shape"

const SAMPLE = ["NO", "WAY", "HE", "ACTUALLY", "DID", "THAT"]
const SAMPLE_HOOK = "Wait for the ending"
const loaded = new Set<string>()
const UNIT = 100 / 288            // one unit of the burned subtitles (PlayResY 288), in cqh
const UNIT_X = 100 / 384          // one horizontal unit (libass' PlayResX for PlayResY 288), in cqw
const HOOK_MARGIN = { top: 38, middle: 0, bottom: 88 }
// Same set clipbot leaves out of the burned hook (it has no emoji font).
const EMOJI = /[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2B00}-\u{2BFF}\u{FE00}-\u{FE0F}\u{200D}\u{20E3}]/gu

/** The hook text as it's burned: without emoji. */
export function stripEmoji(text: string) {
  return text.replace(EMOJI, "").replace(/[ \t]{2,}/g, " ").trim()
}

/** How big a font's em is next to its ASS font size (see fonts.em_ratio in clipbot). */
export function emOf(fonts: FontItem[], family: string) {
  return fonts.find((f) => f.family === family)?.em ?? 0.7
}

/** Loads a caption font from clipbot once, so previews use the real file. */
export function useFont(fonts: FontItem[], family: string) {
  const [, bump] = useState(0)
  useEffect(() => {
    const font = fonts.find((f) => f.family === family)
    if (!font || loaded.has(family)) return
    loaded.add(family)
    // The burn asks for bold; libass fakes it only for fonts under weight 550. A heavy font registered
    // for every weight keeps the browser from faking it too.
    const face = new FontFace(family, `url(/api/fonts/${encodeURIComponent(font.file)}/file)`,
      font.weight >= 550 ? { weight: "100 900" } : undefined)
    face.load().then((f) => { document.fonts.add(f); bump((n) => n + 1) }).catch(() => loaded.delete(family))
  }, [fonts, family])
}

/** One caption line in a caption style, placed like the burned captions. Sized from the height of
 * the nearest size container (a 9:16 frame), so it scales with the preview. `active` is the index of
 * the word being said (-1: none); `em` the font's emOf. */
export function CaptionLayer({ style, line, active, em = 0.7 }: { style: CaptionStyle; line: string[]; active: number; em?: number }) {
  if (!style.enabled || !line.length) return null
  const size = `${style.font_size * em * UNIT}cqh`
  const stroke = style.bg_opacity > 0 ? 0 : Math.max(style.shadow ? 0 : 1, style.border_width)
  return (
    <div className={cn("pointer-events-none absolute inset-x-[4%] flex justify-center text-center leading-tight",
      style.position === "top" ? "top-[12%]" : style.position === "middle" ? "top-1/2 -translate-y-1/2" : "bottom-[15%]")}
      style={{ fontFamily: `"${style.font_name}", Anton, sans-serif`, fontSize: size, fontWeight: 700 }}>
      <span className={cn(style.bg_opacity > 0 && "rounded-sm px-[0.2em]")}
        style={{ background: style.bg_opacity > 0 ? hexAlpha(style.bg_color, style.bg_opacity) : undefined }}>
        {line.map((w, i) => {
          const isActive = i === active
          const hidden = style.reveal && active >= 0 && i > active
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
              {style.uppercase ? w.toUpperCase() : w}
            </span>
          )
        })}
      </span>
    </div>
  )
}

/** The hook line in a hook style, placed and sized like the burned one. A rounded box is drawn
 * behind it the way clipbot burns it: the outline of the line boxes, every corner rounded. */
export function HookLayer({ hook, text, position, em = 0.7 }: {
  hook: HookStyle; text: string; position?: CaptionPosition; em?: number
}) {
  const shown = stripEmoji(text)
  const where = position ?? hook.position
  const size = Math.max(10, Math.floor(hook.font_size * 0.85))
  const pad = Math.max(3, Math.round(size * 0.28))
  const outline = Math.max(2, Math.round(size * 0.1))
  const rounded = hook.style === "box" && hook.box_radius > 0
  const radius = (hook.box_radius / 100) * ((size + 2 * pad) / 2)     // in units of the burn
  const root = useRef<HTMLDivElement>(null)
  const line = useRef<HTMLSpanElement>(null)
  const [path, setPath] = useState<string | null>(null)

  useLayoutEffect(() => {
    const frame = root.current?.parentElement
    const el = line.current
    if (!rounded || !frame || !el) { setPath(null); return }
    const measure = () => {
      const f = frame.getBoundingClientRect()
      if (!f.height) return
      const boxes = [...el.getClientRects()].map((r) => ({
        top: r.top - f.top, bottom: r.bottom - f.top, left: r.left - f.left, right: r.right - f.left,
      }))
      setPath(roundedBoxPath(boxes, radius * (f.height / 288)))
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(frame)
    document.fonts.addEventListener("loadingdone", measure)
    return () => { ro.disconnect(); document.fonts.removeEventListener("loadingdone", measure) }
  }, [rounded, radius, shown, where, hook, em])

  if (!shown) return null
  const drawn = rounded && path !== null
  return (
    <>
      {drawn && (
        <svg aria-hidden className="pointer-events-none absolute inset-0 size-full overflow-visible">
          <path d={path ?? undefined} fill={hook.box_color} />
        </svg>
      )}
      {/* A block (not flex), so the text stays inline and the box hugs each line like libass draws it;
          balanced lines like libass' WrapStyle 0. */}
      <div ref={root} className={cn("pointer-events-none absolute inset-x-[7%] whitespace-pre-line text-balance text-center",
        where === "middle" && "top-1/2 -translate-y-1/2")}
        style={{
          // An ASS font size is the font's whole line height (ascent + descent), so lines are 1/em apart.
          fontFamily: `"${hook.font_name}", Montserrat, sans-serif`, fontSize: `${size * em * UNIT}cqh`, fontWeight: 700, lineHeight: 1 / em,
          ...(where === "top" ? { top: `${HOOK_MARGIN.top * UNIT}cqh` } : where === "bottom" ? { bottom: `${HOOK_MARGIN.bottom * UNIT}cqh` } : {}),
        }}>
        <span ref={line} style={{
          color: hook.text_color,
          // Until the rounded outline is measured, a plain rounded background stands in.
          background: hook.style === "box" && !drawn ? hook.box_color : undefined,
          borderRadius: rounded && !drawn ? `${radius * UNIT}cqh` : undefined,
          // libass pads the box all round, scaling borders per axis: by PlayResY (288) up and down, by
          // the implied PlayResX (384) to the sides.
          padding: hook.style === "box" ? `${pad * UNIT}cqh ${pad * UNIT_X}cqw` : undefined,
          boxDecorationBreak: "clone", WebkitBoxDecorationBreak: "clone",
          // Twice the width (the fill covers the inner half), averaged over both axes.
          WebkitTextStroke: hook.style === "outline" ? `calc(${outline * UNIT}cqh + ${outline * UNIT_X}cqw) ${hook.outline_color}` : undefined,
          paintOrder: "stroke fill",
          textShadow: hook.style === "plain" ? `0 ${2 * UNIT}cqh 0 rgba(0,0,0,.55)` : undefined,
        }}>
          {hook.uppercase ? shown.toUpperCase() : shown}
        </span>
      </div>
    </>
  )
}

/** A 9:16 frame with the caption look applied, the active word moving like on the clip; with a hook
 * look, the hook line too. */
export function CaptionPreview({ style, fonts, className, words = SAMPLE, hook, hookText = SAMPLE_HOOK }: {
  style: CaptionStyle
  fonts: FontItem[]
  className?: string
  words?: string[]
  hook?: HookStyle
  hookText?: string
}) {
  useFont(fonts, style.font_name)
  useFont(fonts, hook?.font_name ?? style.font_name)
  const [active, setActive] = useState(0)
  useEffect(() => {
    const id = window.setInterval(() => setActive((i) => (i + 1) % words.length), 420)
    return () => window.clearInterval(id)
  }, [words.length])

  // Same grouping as the burned captions: up to max_chars per line.
  const lines: { start: number; words: string[] }[] = []
  words.forEach((w, i) => {
    const line = lines[lines.length - 1]
    if (!line || line.words.join(" ").length + w.length + 1 > style.max_chars) lines.push({ start: i, words: [w] })
    else line.words.push(w)
  })
  const shown = [...lines].reverse().find((l) => l.start <= active) ?? lines[0]

  return (
    <div className={cn("relative aspect-[9/16] overflow-hidden rounded-md bg-gradient-to-b from-slate-600 via-slate-800 to-slate-950",
      className)} style={{ containerType: "size" }}>
      {hook?.enabled && <HookLayer hook={hook} text={hookText} em={emOf(fonts, hook.font_name)} />}
      {style.enabled ? (
        <CaptionLayer style={{ ...style, uppercase: style.uppercase }} em={emOf(fonts, style.font_name)}
          line={shown?.words.map((w) => (style.uppercase ? w : w.toLowerCase())) ?? []}
          active={shown ? active - shown.start : -1} />
      ) : (
        <div className="absolute inset-x-0 bottom-[15%] text-center text-xs text-white/60">No captions</div>
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
