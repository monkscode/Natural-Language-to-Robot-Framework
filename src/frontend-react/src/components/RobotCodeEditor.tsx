/**
 * Editable Robot Framework code editor with syntax highlighting.
 *
 * Uses react-simple-code-editor (overlay textarea technique) with Prism's
 * official `robotframework` grammar — sections, test/keyword names, keyword
 * calls (property), variables, [tags], documentation, comments. Token colors
 * live in index.css under `.robot-editor`.
 */

import type { ComponentProps, KeyboardEventHandler } from 'react'
import Editor from 'react-simple-code-editor'
import Prism from 'prismjs'
import 'prismjs/components/prism-robotframework'
import { cn } from '@/lib/utils'

function highlight(code: string): string {
  return Prism.highlight(code, Prism.languages.robotframework, 'robotframework')
}

export default function RobotCodeEditor({ value, onChange, disabled, placeholder, className, onKeyDown }: Readonly<{
  value: string
  onChange: (v: string) => void
  disabled?: boolean
  placeholder?: string
  className?: string
  // Forwarded straight to the underlying textarea. react-simple-code-editor
  // calls this before its own key handling and honours preventDefault, so a
  // caller's shortcut lands on the real interactive element instead of a
  // wrapping div that would need a fake ARIA role to explain itself.
  onKeyDown?: KeyboardEventHandler<HTMLTextAreaElement>
}>) {
  return (
    <div className={cn('robot-editor min-h-0 overflow-auto bg-[#0d1117]', className)}>
      <Editor
        value={value}
        onValueChange={onChange}
        highlight={highlight}
        disabled={disabled}
        placeholder={placeholder}
        padding={16}
        textareaClassName="focus:outline-none"
        className="min-h-full text-[13px] leading-relaxed text-[#e6edf3] caret-white"
        style={{ fontFamily: "Consolas, Menlo, Monaco, 'Droid Sans Mono', 'Courier New', monospace" }}
        // Editor's own .d.ts spreads unrecognised props onto its container
        // div, so it types onKeyDown against HTMLDivElement as well as the
        // textarea it is actually wired to at runtime (lib/index.js) — an
        // intersection our textarea-only handler can never honestly satisfy.
        // Casting through Editor's own declared prop type (rather than
        // hand-writing that intersection) keeps this tied to its real type,
        // not a guess, if the library's declarations ever change.
        onKeyDown={onKeyDown as ComponentProps<typeof Editor>['onKeyDown']}
      />
    </div>
  )
}
