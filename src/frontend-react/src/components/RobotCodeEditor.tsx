/**
 * Editable Robot Framework code editor with syntax highlighting.
 *
 * Overlay technique: a highlighted <pre> sits behind a transparent <textarea>
 * with identical text metrics; scrolling is kept in sync. Highlighting rules
 * are ported from the legacy UI (src/frontend/script.js applySyntaxHighlighting):
 * sections, comments, settings keywords, variables, test names, keyword calls.
 */

import { Fragment, useRef } from 'react'
import type { ReactNode } from 'react'
import { cn } from '@/lib/utils'

const SETTINGS_KEYWORDS =
  /^(Library|Resource|Variables|Documentation|Metadata|Suite Setup|Suite Teardown|Test Setup|Test Teardown|Test Template|Test Timeout|Force Tags|Default Tags|Test Tags)(\s+)(.*)$/

const VAR_SPLIT = /([$@&]\{[^}]*\})/g

function vars(text: string): ReactNode[] {
  return text.split(VAR_SPLIT).map((part, i) =>
    /^[$@&]\{/.test(part)
      ? <span key={i} className="text-[#d2a8ff]">{part}</span>
      : <Fragment key={i}>{part}</Fragment>,
  )
}

function highlightLine(line: string): ReactNode {
  const trimmed = line.trim()
  if (!trimmed) return line

  // *** Sections ***
  if (trimmed.startsWith('***')) {
    return <span className="font-semibold text-[#ff7b72]">{line}</span>
  }
  // # comments
  if (trimmed.startsWith('#')) {
    return <span className="italic text-[#8b949e]">{line}</span>
  }

  const indentMatch = line.match(/^\s+/)
  if (!indentMatch) {
    // Unindented: settings entry, variable definition, or test-case name
    const m = line.match(SETTINGS_KEYWORDS)
    if (m) {
      return (
        <>
          <span className="text-[#79c0ff]">{m[1]}</span>
          {m[2]}
          {vars(m[3])}
        </>
      )
    }
    if (/^[$@&]\{/.test(line)) return <>{vars(line)}</>
    return <span className="font-semibold text-[#7ee787]">{line}</span>
  }

  // Indented: keyword call (first cell = up to the first 2+ space separator)
  const indent = indentMatch[0]
  const rest = line.slice(indent.length)
  if (/^[$@&]\{/.test(rest)) return <>{indent}{vars(rest)}</>
  const cells = rest.match(/^(.+?)(\s{2,}.*)?$/)
  if (!cells) return line
  return (
    <>
      {indent}
      <span className="text-[#79c0ff]">{cells[1]}</span>
      {cells[2] != null && vars(cells[2])}
    </>
  )
}

export default function RobotCodeEditor({ value, onChange, disabled, placeholder, className }: {
  value: string
  onChange: (v: string) => void
  disabled?: boolean
  placeholder?: string
  className?: string
}) {
  const taRef = useRef<HTMLTextAreaElement>(null)
  const preRef = useRef<HTMLPreElement>(null)

  function syncScroll() {
    if (taRef.current && preRef.current) {
      preRef.current.scrollTop = taRef.current.scrollTop
      preRef.current.scrollLeft = taRef.current.scrollLeft
    }
  }

  const metrics = 'm-0 p-4 font-mono text-[13px] leading-relaxed whitespace-pre'
  const lines = value.split('\n')

  return (
    <div className={cn('relative min-h-0 overflow-hidden bg-[#0d1117]', className)}>
      <pre ref={preRef} aria-hidden className={cn(metrics, 'absolute inset-0 overflow-hidden text-[#e6edf3]')}>
        {lines.map((l, i) => (
          <Fragment key={i}>{highlightLine(l)}{'\n'}</Fragment>
        ))}
      </pre>
      <textarea
        ref={taRef}
        value={value}
        onChange={e => onChange(e.target.value)}
        onScroll={syncScroll}
        disabled={disabled}
        placeholder={placeholder}
        spellCheck={false}
        wrap="off"
        className={cn(
          metrics,
          'absolute inset-0 w-full resize-none overflow-auto border-0 bg-transparent',
          'text-transparent caret-white outline-none selection:bg-blue-500/40',
          'placeholder:text-[#6e7681] disabled:cursor-not-allowed',
        )}
      />
    </div>
  )
}
