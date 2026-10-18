import { cn } from "cn"

import type { Clip } from "@/lib/api"
import { tc } from "@/lib/format"

/** A 9:16 poster: the generated thumbnail, or the video's first second when there is none. */
export function ClipThumb({ clip, className, showDuration = true }: { clip: Clip; className?: string; showDuration?: boolean }) {
  return (
    <div className={cn("relative aspect-[9/16] overflow-hidden rounded-md bg-muted", className)}>
      {clip.thumb_url ? (
        <img src={clip.thumb_url} alt="" loading="lazy" className="size-full object-cover" />
      ) : (
        <video src={`${clip.video_url}#t=1`} preload="metadata" muted className="size-full object-cover" />
      )}
      {showDuration && clip.duration ? (
        <span className="absolute right-1 bottom-1 rounded-sm bg-background/80 px-1 text-[11px] timecode">
          {tc(clip.duration)}
        </span>
      ) : null}
    </div>
  )
}
