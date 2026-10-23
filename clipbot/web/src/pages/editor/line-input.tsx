import { useEffect, useState } from "react"

import { Input } from "@/components/ui/input"

export function LineInput({ text, onCommit, id, className = "h-8" }: {
  text: string; onCommit: (text: string) => void; id?: string; className?: string
}) {
  const [value, setValue] = useState(text)
  useEffect(() => setValue(text), [text])
  const done = () => { if (value.trim() !== text) onCommit(value.trim()) }
  return (
    <Input id={id} value={value} className={className} maxLength={200} onChange={(e) => setValue(e.target.value)} onBlur={done}
      onKeyDown={(e) => { if (e.key === "Enter") (e.target as HTMLInputElement).blur(); if (e.key === "Escape") setValue(text) }} />
  )
}
