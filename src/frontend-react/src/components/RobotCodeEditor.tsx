/**
 * Editable Robot Framework code editor with syntax highlighting.
 *
 * Uses react-simple-code-editor (overlay textarea technique) with Prism's
 * official `robotframework` grammar — sections, test/keyword names, keyword
 * calls (property), variables, [tags], documentation, comments. Token colors
 * live in index.css under `.robot-editor`.
 */

import Editor from 'react-simple-code-editor'
import Prism from 'prismjs'
import 'prismjs/components/prism-robotframework'
import { cn } from '@/lib/utils'

function highlight(code: string): string {
  return Prism.highlight(code, Prism.languages.robotframework, 'robotframework')
}

export default function RobotCodeEditor({ value, onChange, disabled, placeholder, className }: {
  value: string
  onChange: (v: string) => void
  disabled?: boolean
  placeholder?: string
  className?: string
}) {
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
      />
    </div>
  )
}
