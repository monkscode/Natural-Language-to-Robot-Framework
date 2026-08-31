import { useEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { api } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import { Zap, Play, Plus, Download, Copy, Check, ChevronDown, ExternalLink, CheckCircle2, XCircle, FileText, X, ThumbsUp, ThumbsDown, Brain, ScanSearch, Code2, ShieldCheck, Crosshair, AlertTriangle } from 'lucide-react'
import RobotCodeEditor from '@/components/RobotCodeEditor'

/* ── Types ── */
type Phase = 'idle' | 'generating' | 'executing'
type Outcome = 'pass' | 'fail' | null
interface LogEntry { kind: 'info' | 'success' | 'error'; ts: string; msg: string }

const PLACEHOLDER = `Examples:

• Open Google, search for 'Robot Framework tutorials', click the first result
• Navigate to GitHub, search for 'selenium automation', open the top repository
• Go to Amazon, search for 'wireless headphones', add the top result to cart

Or paste an existing .robot test on the right and just run it.`

function nowTs() {
  return new Date().toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

/** /reports/{run_id}/log.html -> run_id (for feedback attribution) */
function runIdFromReport(url: string | null): string | null {
  if (!url) return null
  const m = url.match(/\/reports\/([^/]+)\//)
  return m ? m[1] : null
}

/* ── Per-test results parsed from the backend's output.xml summary blob ── */
interface RobotSummary {
  tests: { name: string; status: 'PASS' | 'FAIL' }[]
  failures: { test: string; message: string }[]
  passed: number | null
  failed: number | null
}

function parseRobotSummary(blob: string): RobotSummary {
  const tests: RobotSummary['tests'] = []
  const failures: RobotSummary['failures'] = []
  let failing: string | null = null
  for (const line of blob.split('\n')) {
    const t = line.match(/^\s*Test: (.+) - (PASS|FAIL)$/)
    if (t) {
      tests.push({ name: t[1], status: t[2] as 'PASS' | 'FAIL' })
      failing = t[2] === 'FAIL' ? t[1] : null
      continue
    }
    const e = line.match(/^\s*Error: (.+)$/)
    if (e && failing) {
      failures.push({ test: failing, message: e[1] })
      failing = null
    }
  }
  const counts = blob.match(/^Results: (\d+) passed, (\d+) failed$/m)
  return {
    tests,
    failures,
    passed: counts ? Number(counts[1]) : tests.length ? tests.filter(t => t.status === 'PASS').length : null,
    failed: counts ? Number(counts[2]) : tests.length ? tests.filter(t => t.status === 'FAIL').length : null,
  }
}

/* ── Collapsible logs card with an indeterminate bar while running ── */
function LogsSection({ title, desc, logs, running, collapseOnDone }: {
  title: string; desc: string; logs: LogEntry[]; running: boolean; collapseOnDone?: boolean
}) {
  const [open, setOpen] = useState(true)
  const listRef = useRef<HTMLDivElement>(null)
  // Keep the newest log line in view while streaming (legacy-UI behaviour)
  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight
  }, [logs.length])
  // Once the run finishes the result card carries the outcome — tuck the raw
  // stream away (still expandable). Re-expanding afterwards sticks: the dep
  // only changes when a new run completes.
  useEffect(() => {
    if (collapseOnDone) setOpen(false)
  }, [collapseOnDone])
  return (
    <Card className="mb-3">
      <CardHeader className="flex-row items-center justify-between space-y-0 py-3 px-4">
        <div>
          <CardTitle className="text-sm">{title}</CardTitle>
          <CardDescription className="text-xs mt-0.5">{desc}</CardDescription>
        </div>
        <Button variant="ghost" size="sm" className="h-7 gap-1 text-xs" onClick={() => setOpen(o => !o)}>
          <ChevronDown className={cn('h-3 w-3 transition-transform', !open && 'rotate-180')} />
          {open ? 'Collapse' : 'Expand'}
        </Button>
      </CardHeader>
      {open && (
        <CardContent className="p-0">
          {running && (
            <div className="h-1 w-full overflow-hidden bg-muted">
              <div className="progress-shimmer h-full w-1/3 animate-pulse bg-primary" />
            </div>
          )}
          <div ref={listRef} className="max-h-56 overflow-y-auto font-mono text-xs">
            {logs.map((log, i) => (
              <div
                key={i}
                className={cn(
                  'flex gap-3 border-b px-4 py-1.5 last:border-0 border-l-2',
                  log.kind === 'success' ? 'border-l-green-500'
                    : log.kind === 'error' ? 'border-l-destructive'
                      : 'border-l-blue-400',
                )}
              >
                <span className="shrink-0 text-muted-foreground">{log.ts}</span>
                <span className="whitespace-pre-wrap break-words">{log.msg}</span>
              </div>
            ))}
          </div>
        </CardContent>
      )}
    </Card>
  )
}

/* ── In-panel generation view (replaces the old standalone progress card):
   a pipeline stage rail over a feed of per-stage artifacts. Each stage shows
   only what it actually produces — plan steps, element locators, then the
   .robot file (the whole file is written by the Assembler, so no code appears
   before that stage), then the dry-run check. Completed stages collapse to a
   one-line summary. Stage boundaries follow the backend's SSE progress values
   (progress_events.py: planner 5–20, element scan 22–60, assembler 62–80;
   dryrun_service.py: verify 88–100). ── */
const PIPELINE_STAGES = [
  { key: 'plan', label: 'Plan', icon: Brain, at: 0, title: 'Breaking your description into test steps', doneLabel: 'Test plan ready' },
  { key: 'locate', label: 'Locate', icon: ScanSearch, at: 22, title: 'Visiting the page and pinning down each element', doneLabel: 'Element locators captured' },
  { key: 'assemble', label: 'Assemble', icon: Code2, at: 62, title: 'Writing test.robot from the plan and locators', doneLabel: 'test.robot assembled' },
  { key: 'verify', label: 'Verify', icon: ShieldCheck, at: 88, title: 'Dry-run verification', doneLabel: 'Verified' },
] as const

/* Plan: numbered ghost steps, revealed as the planner's SSE events arrive */
function PlanActivity({ progress }: { progress: number }) {
  const widths = ['w-64', 'w-52', 'w-72']
  return (
    <div>
      {[5, 8, 14].filter(at => progress >= at).map((at, i) => (
        <div key={at} className="ghost-line flex h-[20px] items-center gap-3">
          <span className="w-4 shrink-0 select-none text-right text-[11px] text-[#484f58]">{i + 1}.</span>
          <span className={cn('ghost-bar relative h-3 overflow-hidden rounded-sm bg-[#21262d]', widths[i])} />
        </div>
      ))}
    </div>
  )
}

/* Locate: element → locator pairs being captured from the live page */
function LocateActivity({ progress, elementCount }: { progress: number; elementCount: number | null }) {
  const widths: [string, string][] = [['w-24', 'w-48'], ['w-28', 'w-40'], ['w-20', 'w-56']]
  return (
    <div>
      {[24, 30, 55].filter(at => progress >= at).map((at, i) => (
        <div key={at} className="ghost-line flex h-[20px] items-center gap-3">
          <Crosshair className="h-3 w-3 shrink-0 text-[#484f58]" />
          <span className={cn('ghost-bar relative h-3 overflow-hidden rounded-sm bg-[#21262d]', widths[i][0])} />
          <span className="select-none text-[11px] text-[#30363d]">→</span>
          <span className={cn('ghost-bar relative h-3 overflow-hidden rounded-sm bg-[#21262d]', widths[i][1])} />
        </div>
      ))}
      {elementCount != null && (
        <p className="ghost-line mt-1 text-[11px] text-[#484f58]">{elementCount} elements found on the page</p>
      )}
    </div>
  )
}

/* Assemble: only now does code appear — the Assembler writes the whole file */
const ASSEMBLE_LINES: { header?: string; w?: string; indent?: boolean }[] = [
  { header: '*** Settings ***' },
  { w: 'w-44' },
  { header: '*** Test Cases ***' },
  { w: 'w-40' },
  { w: 'w-72', indent: true },
  { w: 'w-60', indent: true },
  { w: 'w-80', indent: true },
]

function AssembleActivity() {
  return (
    <div>
      {ASSEMBLE_LINES.map((l, i) => (
        <div key={i} className="ghost-line flex h-[20px] items-center" style={{ animationDelay: `${i * 110}ms` }}>
          {/* Muted gray, NOT the editor's red section-header token: in a
              progress skeleton red reads as an error, not as syntax. */}
          {l.header
            ? <span className="font-semibold text-[#8b949e]/70">{l.header}</span>
            : <span className={cn('ghost-bar relative h-3 overflow-hidden rounded-sm bg-[#21262d]', l.w, l.indent && 'ml-8')} />}
        </div>
      ))}
      <div className="flex h-[20px] items-center" style={{ animationDelay: `${ASSEMBLE_LINES.length * 110}ms` }}>
        <span aria-hidden className="ghost-caret ml-8 h-4 w-[7px] rounded-[1px] bg-[#58a6ff]" />
      </div>
    </div>
  )
}

function VerifyActivity() {
  return (
    <div className="ghost-line flex items-center gap-2.5 text-xs text-[#8b949e]">
      <span className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-[#30363d] border-t-sky-400" />
      Compiling the test in a clean Docker container — auto-fixing anything that fails
    </div>
  )
}

function GenerationPipeline({ progress, stage, logs }: { progress: number; stage: string; logs: LogEntry[] }) {
  // The element-scan SSE event carries the only real artifact count we get
  // ("📍 Found N elements on the page") — surface it in the Locate block.
  const elMatch = logs.map(l => l.msg.match(/Found (\d+) elements/)).find(Boolean)
  const elementCount = elMatch ? Number(elMatch[1]) : null
  return (
    <div className="flex min-h-[300px] flex-1 flex-col overflow-hidden rounded-lg border border-border bg-[#0d1117] shadow-sm">
      {/* Stage rail + live status line */}
      <div className="border-b border-[#21262d] px-4 pb-2.5 pt-3">
        <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
          {PIPELINE_STAGES.map((s, i) => {
            const nextAt = PIPELINE_STAGES[i + 1]?.at ?? 100
            const done = progress >= nextAt
            const active = !done && progress >= s.at
            const Icon = s.icon
            return (
              <div key={s.label} className="flex items-center gap-2">
                <span
                  className={cn(
                    'flex h-6 w-6 items-center justify-center rounded-full border transition-colors',
                    done ? 'border-emerald-500/40 bg-emerald-500/15 text-emerald-400'
                      : active ? 'stage-active border-sky-400/50 bg-sky-400/10 text-sky-300'
                        : 'border-[#30363d] text-[#484f58]',
                  )}
                >
                  {done ? <Check className="h-3.5 w-3.5" /> : <Icon className="h-3.5 w-3.5" />}
                </span>
                <span className={cn('text-xs font-medium',
                  done ? 'text-[#8b949e]' : active ? 'text-[#e6edf3]' : 'text-[#484f58]')}>
                  {s.label}
                </span>
                {i < PIPELINE_STAGES.length - 1 && (
                  <span className={cn('h-px w-3 sm:w-6', done ? 'bg-emerald-500/40' : 'bg-[#30363d]')} />
                )}
              </div>
            )
          })}
          <span className="ml-auto text-xs font-semibold tabular-nums text-[#8b949e]">{progress}%</span>
        </div>
        <p className="mt-2 truncate text-xs text-[#8b949e]">{stage || 'Starting test generation…'}</p>
      </div>
      <div className="h-0.5 w-full shrink-0 bg-[#161b22]">
        <div
          className="h-full bg-sky-500/60 transition-[width] duration-700 ease-out"
          style={{ width: `${Math.max(progress, 2)}%` }}
        />
      </div>
      {/* Stage-artifact feed — the active stage shows the kind of artifact it
          is producing right now; completed stages collapse to one summary line.
          Keys are stable so revealed rows don't re-run entrance animations. */}
      <div
        className="min-h-0 flex-1 space-y-3 overflow-hidden px-4 py-3 text-[13px] leading-relaxed"
        style={{ fontFamily: "Consolas, Menlo, Monaco, 'Droid Sans Mono', 'Courier New', monospace" }}
      >
        {PIPELINE_STAGES.map((s, i) => {
          const nextAt = PIPELINE_STAGES[i + 1]?.at ?? 100
          const done = progress >= nextAt
          const active = !done && progress >= s.at
          if (!done && !active) return null
          if (done) {
            return (
              <div key={s.key} className="ghost-line flex items-center gap-2 text-xs">
                <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" />
                <span className="text-[#8b949e]">
                  {s.doneLabel}
                  {s.key === 'locate' && elementCount != null ? ` — ${elementCount} elements` : ''}
                </span>
              </div>
            )
          }
          const Icon = s.icon
          return (
            <div key={s.key} className="space-y-1.5">
              <div className="ghost-line flex items-center gap-2 text-xs font-medium text-[#e6edf3]">
                <Icon className="h-3.5 w-3.5 shrink-0 text-sky-300" /> {s.title}
              </div>
              {s.key === 'plan' && <PlanActivity progress={progress} />}
              {s.key === 'locate' && <LocateActivity progress={progress} elementCount={elementCount} />}
              {s.key === 'assemble' && <AssembleActivity />}
              {s.key === 'verify' && <VerifyActivity />}
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ── Live elapsed-seconds counter for the execution strip ── */
function ExecElapsed({ start }: { start: number | null }) {
  const [, force] = useState(0)
  useEffect(() => {
    const id = setInterval(() => force(t => t + 1), 1000)
    return () => clearInterval(id)
  }, [])
  if (start == null) return null
  return (
    <span className="ml-auto shrink-0 tabular-nums text-[#8b949e]">
      {Math.max(0, Math.round((Date.now() - start) / 1000))}s
    </span>
  )
}

/* ── One-shot confetti burst for passing runs (keyframes in index.css) ── */
const CONFETTI_COLORS = ['#22c55e', '#10b981', '#f59e0b', '#38bdf8', '#a78bfa', '#f472b6']

function ConfettiBurst() {
  return (
    <div aria-hidden className="pointer-events-none absolute inset-0 overflow-hidden">
      {Array.from({ length: 16 }, (_, i) => (
        <span
          key={i}
          className="confetti-piece absolute top-0 h-1.5 w-1.5 rounded-[1px]"
          style={{
            left: `${(5 + i * 6.3) % 95}%`,
            background: CONFETTI_COLORS[i % CONFETTI_COLORS.length],
            animationDelay: `${(i % 8) * 70}ms`,
            '--x': `${(((i * 37) % 5) - 2) * 24}px`,
            '--r': `${140 + ((i * 53) % 280)}deg`,
          } as CSSProperties}
        />
      ))}
    </div>
  )
}

/* ── Post-run result card: outcome banner, per-test breakdown, report links,
   and an optional feedback footer (children) ── */
function ExecutionResult({ outcome, summary, secs, reportUrl, logUrl, children }: {
  outcome: Exclude<Outcome, null>; summary: RobotSummary | null; secs: number | null
  reportUrl: string | null; logUrl: string | null; children?: ReactNode
}) {
  const pass = outcome === 'pass'
  const tests = summary?.tests ?? []
  const failures = summary?.failures ?? []
  const total = summary && summary.passed != null && summary.failed != null
    ? summary.passed + summary.failed
    : tests.length || null
  // Single-test runs are the norm — counts and the per-test list only earn
  // their place when there is more than one test.
  const title = pass
    ? total === 1 ? 'Test passed! 🎉' : 'All tests passed! 🎉'
    : summary?.failed && total
      ? total > 1 ? `${summary.failed} of ${total} tests failed` : 'Test failed'
      : 'Test execution failed'
  const subtitle = pass
    ? [total && total > 1 ? `${total} tests` : null, secs != null ? `finished in ${secs.toFixed(1)}s` : null]
        .filter(Boolean).join(' · ') || 'Execution finished'
    : 'Open the detailed log to see exactly which step went wrong.'
  return (
    <Card className={cn('result-pop relative overflow-hidden', pass ? 'border-green-500/40' : 'border-destructive/40')}>
      <div
        className={cn(
          'pointer-events-none absolute inset-0',
          pass
            ? 'bg-gradient-to-br from-green-500/15 via-emerald-500/5 to-transparent'
            : 'bg-gradient-to-br from-red-500/10 via-red-500/5 to-transparent',
        )}
      />
      {pass && <ConfettiBurst />}
      <CardContent className="relative space-y-3 p-5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            {pass
              ? <CheckCircle2 className="check-pop h-10 w-10 shrink-0 text-green-500" />
              : <XCircle className="h-10 w-10 shrink-0 text-destructive" />}
            <div>
              <div className="text-lg font-bold leading-tight">{title}</div>
              <p className="text-sm text-muted-foreground">{subtitle}</p>
            </div>
          </div>
          <div className="flex shrink-0 gap-2">
            {reportUrl && (
              <Button asChild size="sm" className={cn('gap-1.5', pass && 'bg-green-600 text-white hover:bg-green-700')}>
                <a href={reportUrl} target="_blank" rel="noopener noreferrer">
                  <ExternalLink className="h-3.5 w-3.5" /> View Report
                </a>
              </Button>
            )}
            {logUrl && (
              <Button asChild variant="outline" size="sm" className="gap-1.5">
                <a href={logUrl} target="_blank" rel="noopener noreferrer">
                  <FileText className="h-3.5 w-3.5" /> Detailed Log
                </a>
              </Button>
            )}
          </div>
        </div>
        {tests.length > 1 && (
          <div className="max-h-36 divide-y overflow-y-auto rounded-lg border bg-background/70">
            {tests.map((t, i) => (
              <div key={i} className="flex items-center gap-2.5 px-3 py-2 text-sm">
                {t.status === 'PASS'
                  ? <Check className="h-4 w-4 shrink-0 text-green-500" />
                  : <X className="h-4 w-4 shrink-0 text-destructive" />}
                <span className="min-w-0 truncate">{t.name}</span>
                <span className={cn('ml-auto shrink-0 text-xs font-bold tracking-wide',
                  t.status === 'PASS' ? 'text-green-600 dark:text-green-400' : 'text-destructive')}>
                  {t.status}
                </span>
              </div>
            ))}
          </div>
        )}
        {!pass && failures.length > 0 && (
          <div className="space-y-1 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 font-mono text-xs text-destructive">
            {failures.slice(0, 3).map((f, i) => (
              <div key={i} className="break-words">
                {tests.length > 1 ? `${f.test}: ` : ''}{f.message}
              </div>
            ))}
            {failures.length > 3 && (
              <div className="text-destructive/70">…and {failures.length - 3} more — see the detailed log</div>
            )}
          </div>
        )}
        {children && (
          <div className={cn('border-t pt-3', pass ? 'border-green-500/20' : 'border-destructive/20')}>
            {children}
          </div>
        )}
      </CardContent>
    </Card>
  )
}

/** POST /api/feedback. `outcome` is the only authority on what happened to the
    correction ("processed" | "no_text" | "no_record" | "learning_paused" |
    "no_org" | "queued" | "error"); it is absent when learning is switched off,
    which is equally not a success. `message` is the backend's own sentence for
    whichever case fired; the panel writes its own only when the body carries
    none (FeedbackPanel's `submit`). */
interface FeedbackResponse { status?: string; outcome?: string; message?: string }

/** GET /api/feedback/{run_id}. The corrections this run has already
    contributed — the hints it created AND the ones it reinforced. T5 makes a
    second submission of the same text from the same run a no-op, and this is
    the answer to that: the user sees their own words on file rather than a
    warning about a duplicate the backend could not report anyway. */
interface RecordedCorrection { hint_id: number; feedback_text: string; recorded_at: string }
interface RecordedResponse { corrections?: RecordedCorrection[] }

/* ── Feedback footer, rendered inside the result card (generated runs only).
   Pass: thumbs row — 👍 is a UI-only acknowledgment (passing runs already feed
   learning automatically at execution time; an empty positive carried no
   signal, so it no longer calls the backend). 👎 expands an inline correction
   form — the corrective text is the signal that actually trains the system.
   Fail: form open by default; Skip still records an empty completely_wrong
   label on the execution record, but nothing is learned from it (outcome
   "no_text") — the empty text carries nothing for any engine to route. ── */
export function FeedbackPanel({ outcome, workflowId }: { outcome: Exclude<Outcome, null>; workflowId: string | null }) {
  const [open, setOpen] = useState(outcome === 'fail')
  const [ack, setAck] = useState(false)
  const [text, setText] = useState('')
  const [status, setStatus] = useState<'idle' | 'sending'>('idle')
  const [result, setResult] = useState<{ ok: boolean; neutral: boolean; message: string } | null>(null)
  const [err, setErr] = useState('')
  const [recorded, setRecorded] = useState<RecordedCorrection[]>([])

  // What this run already told the system. Fetched on mount rather than when
  // the form opens: the list exists to be read BEFORE typing, and it must not
  // arrive mid-sentence. Normally empty on a first run — the interesting case
  // is a second run of the same code, which reuses the workflow id and so
  // remounts this panel over the corrections the first run filed.
  //
  // Failures are swallowed on purpose. The panel adds a note when something is
  // on file and is silent otherwise, so a failed read degrades to exactly the
  // form that shipped before this feature — it never claims nothing was
  // recorded, it just says nothing.
  useEffect(() => {
    if (!workflowId) return
    let live = true
    api<RecordedResponse>(`/api/feedback/${encodeURIComponent(workflowId)}`)
      .then(b => { if (live) setRecorded(Array.isArray(b?.corrections) ? b.corrections : []) })
      .catch(() => { /* silence is the honest degradation here */ })
    return () => { live = false }
  }, [workflowId])

  // Nothing to attribute feedback to — don't render a dead form.
  if (!workflowId) return null

  // Same text, same run: T5 gates it on the backend, so this is a courtesy
  // notice and never a block. A plain compare is exactly as accurate as the
  // gate — same text on the same run means the same triage category, hence
  // the same scope and the same dedup key — and re-deriving that key here
  // would put a second copy of it beside the first.
  const alreadySent = recorded.some(c => c.feedback_text === text.trim())

  async function submit(feedbackText: string = text) {
    setStatus('sending'); setErr(''); setResult(null)
    try {
      const body = await api<FeedbackResponse>('/api/feedback', {
        method: 'POST',
        body: JSON.stringify({
          workflow_id: workflowId,
          feedback_text: feedbackText.slice(0, 500),
          feedback_type: outcome === 'pass' ? 'close_enough' : 'completely_wrong',
        }),
      })
      // Only "processed" means the correction reached the learning store. The
      // panel used to thank the user for every one of the others too — no
      // record found, learning paused, an internal error, learning switched
      // off — while the text was discarded. The sentence itself is the
      // backend's, so one place says what the system did.
      const ok = body?.outcome === 'processed'
      // Skip submits empty text, which every engine treats as a free no-op —
      // nothing was learned, but the user already declined to say more, so
      // "send it again" (the amber notice below) is advice they cannot act
      // on. This is its own terminal state, distinct from `ok`.
      const neutral = body?.outcome === 'no_text'
      // M9: the mount-effect fetch above only ever runs once, so a correction
      // filed during THIS session never showed up in "Already recorded for
      // this run" until the page reloaded. Re-fetch once the backend confirms
      // this one actually landed — display only, not the storage check (that
      // is `outcome`, already read above).
      if (ok && workflowId) {
        api<RecordedResponse>(`/api/feedback/${encodeURIComponent(workflowId)}`)
          .then(b => setRecorded(Array.isArray(b?.corrections) ? b.corrections : []))
          .catch(() => { /* silence is the honest degradation here, same as the mount fetch */ })
      }
      setResult({
        ok,
        neutral,
        message: body?.message || (ok
          ? 'Thanks — your feedback helps the system learn.'
          : 'Your feedback was sent, but the system did not confirm it was recorded.'),
      })
      setStatus('idle')
    } catch (e) {
      setStatus('idle')
      setErr(e instanceof Error ? e.message : 'Could not send feedback')
    }
  }

  if (ack) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <Check className="h-4 w-4 text-green-600" /> Great — glad it did exactly what you asked.
      </div>
    )
  }

  // Only a recorded correction retires the form. Every other answer tells the
  // user to send it again, so the textarea, their typed text and Submit all
  // have to survive — replacing them with the message would be advice the UI
  // makes impossible to follow. Rendered like `err` below: a notice beside a
  // still-usable form, not a terminal state.
  //
  // no_text is the one exception: the user clicked Skip, so "send it again"
  // is advice they already declined. It retires the form too, but with a
  // neutral presentation — no green check (nothing was learned) and no amber
  // warning (nothing failed).
  if (result?.ok) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <Check className="h-4 w-4 text-green-600" /> {result.message}
      </div>
    )
  }
  if (result?.neutral) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        {result.message}
      </div>
    )
  }

  // Pass + not expanded: one-click thumbs verdict.
  if (outcome === 'pass' && !open) {
    return (
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-sm text-muted-foreground">Did the test do what you asked?</span>
        <div className="flex gap-2">
          <Button variant="outline" size="sm" className="h-8 gap-1.5" onClick={() => setAck(true)}>
            <ThumbsUp className="h-3.5 w-3.5" /> Spot on
          </Button>
          <Button variant="outline" size="sm" className="h-8 gap-1.5" onClick={() => setOpen(true)}>
            <ThumbsDown className="h-3.5 w-3.5" /> Not quite
          </Button>
        </div>
      </div>
    )
  }

  return (
    <div className="space-y-2">
      {result && (
        <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{result.message}</span>
        </div>
      )}
      {recorded.length > 0 && (
        <div className="rounded-md border bg-muted/40 px-3 py-2 text-xs">
          <div className="font-medium">Already recorded for this run</div>
          <ul className="mt-1 space-y-0.5 text-muted-foreground">
            {recorded.map(c => <li key={c.hint_id}>“{c.feedback_text}”</li>)}
          </ul>
        </div>
      )}
      <div>
        <div className="text-sm font-semibold">
          {outcome === 'fail' ? '💡 Help us get it right next time' : 'What was off?'}
        </div>
        <p className="text-xs text-muted-foreground">
          {outcome === 'fail'
            ? 'Tell us what the test should have done — this trains the generator.'
            : 'Optional — describe anything that wasn’t quite right.'}
        </p>
      </div>
      <Textarea
        value={text}
        maxLength={500}
        onChange={e => setText(e.target.value)}
        placeholder="e.g. it clicked the wrong button; the search box locator was off…"
        className="min-h-[72px] text-sm"
      />
      {/* Its own row, not a third item in the counter/buttons flex below: at
          this width the sentence wraps and collides with the 0/500 counter. */}
      {alreadySent && (
        <p className="text-xs text-muted-foreground">
          You already sent this for this run — it won’t be counted again.
        </p>
      )}
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">{text.length}/500</span>
        {err && <span className="text-xs text-destructive">{err}</span>}
        <div className="flex gap-2">
          {outcome === 'pass' && (
            <Button variant="ghost" size="sm" onClick={() => setOpen(false)} disabled={status === 'sending'}>
              Cancel
            </Button>
          )}
          {outcome === 'fail' && (
            // Legacy-UI behaviour: skipping still records a completely_wrong
            // signal (empty text) on the execution record, but the backend
            // answers "no_text" — empty text carries nothing for any engine
            // to learn from.
            <Button variant="ghost" size="sm" onClick={() => submit('')} disabled={status === 'sending'}>
              Skip →
            </Button>
          )}
          {/* Empty Submit is an ATTEMPT to correct, not a decline, but it
              sent the identical empty-text request Skip does — so it drew the
              same `no_text`, which retires this panel, and the panel is
              rendered in one place and never remounts for a run. Measured in
              the SPA: after an empty Submit every feedback control was gone.
              Skip keeps sending the empty decline, and the passing path keeps
              Cancel, so neither is a dead end. */}
          <Button size="sm" onClick={() => submit()} disabled={status === 'sending' || !text.trim()}>
            {status === 'sending' ? 'Sending…' : 'Submit feedback'}
          </Button>
        </div>
      </div>
    </div>
  )
}

/* ── Main page ── */
export default function GeneratePage() {
  const [query, setQuery]   = useState('')
  const [code, setCode]     = useState('')
  const [phase, setPhase]   = useState<Phase>('idle')
  const [genProgress, setGenProgress] = useState(0)
  const [genStage, setGenStage]       = useState('')
  const [genLogs, setGenLogs]   = useState<LogEntry[]>([])
  const [execLogs, setExecLogs] = useState<LogEntry[]>([])
  const [outcome, setOutcome]   = useState<Outcome>(null)
  const [execSecs, setExecSecs] = useState<number | null>(null)
  const [reportUrl, setReportUrl] = useState<string | null>(null)
  const [logUrl, setLogUrl]     = useState<string | null>(null)
  const [summary, setSummary]   = useState<RobotSummary | null>(null)
  const [error, setError]   = useState('')
  const [copied, setCopied] = useState(false)

  const workflowId = useRef<string | null>(null)   // set on generation complete
  const feedbackId = useRef<string | null>(null)   // run_id used for feedback
  const generatedQuery = useRef<string>('')        // query that produced `code`
  const execStart = useRef<number | null>(null)    // wall-clock start of the run
  const execRef = useRef<HTMLDivElement>(null)     // auto-scroll target while executing
  const resultRef = useRef<HTMLDivElement>(null)   // auto-scroll target on completion

  const busy = phase === 'generating' || phase === 'executing'

  // Prefill from History's Re-run action. One-shot: the router state is
  // cleared immediately so a refresh starts from a clean page.
  const location = useLocation()
  useEffect(() => {
    const prefill = (location.state as { prefillQuery?: string } | null)?.prefillQuery
    if (prefill) {
      setQuery(prefill)
      window.history.replaceState({}, '')
    }
  }, [location.state])

  // Bring the execution logs into view when a run starts — they render below
  // the fold and the user otherwise gets no cue (legacy-UI parity). Generation
  // progress lives inside the code panel itself, so no scroll is needed there.
  useEffect(() => {
    if (phase === 'executing') {
      setTimeout(() => execRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80)
    }
  }, [phase])

  // When the run finishes, bring the result card + report links back into
  // view — the user may have scrolled elsewhere during a long execution.
  useEffect(() => {
    if (outcome) setTimeout(() => resultRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' }), 120)
  }, [outcome])

  const addGen  = (kind: LogEntry['kind'], msg: string) =>
    setGenLogs(l => [...l, { kind, ts: nowTs(), msg }])
  const addExec = (kind: LogEntry['kind'], msg: string) =>
    setExecLogs(l => [...l, { kind, ts: nowTs(), msg }])

  async function handleGenerate() {
    if (!query.trim() || busy) return
    setError(''); setGenLogs([]); setExecLogs([]); setOutcome(null); setReportUrl(null); setLogUrl(null); setSummary(null); setCode('')
    setGenProgress(0); setGenStage('Starting test generation…')
    workflowId.current = null
    generatedQuery.current = query
    setPhase('generating')
    try {
      await streamSSE('/generate-test', { query }, (data) => {
        if (data.status === 'running') {
          // The backend sends a monotonic 0–100 `progress` on stage boundaries;
          // events without it (e.g. dryrun repair iterations) hold the bar.
          if (typeof data.progress === 'number') {
            setGenProgress(p => Math.max(p, data.progress))
          }
          if (data.message) setGenStage(data.message)
          addGen('info', data.message || data.log || '…')
        } else if (data.status === 'complete' && data.robot_code) {
          setGenProgress(100)
          setCode(data.robot_code)
          workflowId.current = data.workflow_id || null
          if (data.dryrun_status === 'failed' || data.dryrun_status === 'unverified') {
            // Do NOT name a cause here. 'unverified' means the gate could not
            // run, and Docker being down is only one of several reasons — the
            // backend sends the actual one as dryrun_message. Asserting
            // "Docker unavailable" sent people to check a healthy Docker while
            // the real fault was elsewhere.
            addGen('error', data.dryrun_status === 'failed'
              ? 'Delivered — dryrun found issues you may want to review'
              : 'Delivered — this test was NOT verified, because the dryrun could not run')
            if (data.dryrun_message) addGen('error', String(data.dryrun_message))
            if (data.dryrun_errors) addGen('error', String(data.dryrun_errors))
          } else {
            addGen('success', `Generated test.robot (${String(data.robot_code).split('\n').length} lines)`)
          }
        } else if (data.status === 'error') {
          addGen('error', data.message || 'Generation failed')
          setError(data.message || 'Generation failed')
        }
      })
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Generation failed'
      addGen('error', msg); setError(msg)
    } finally {
      setPhase('idle')
    }
  }

  async function handleRun() {
    const robot = code.trim()
    if (!robot || busy) return
    setError(''); setExecLogs([]); setOutcome(null); setReportUrl(null); setLogUrl(null); setSummary(null)
    execStart.current = Date.now()
    setExecSecs(null)
    setPhase('executing')
    try {
      await streamSSE('/execute-test', {
        robot_code: robot,
        // Only the query that actually produced this code. Pasted code must
        // run with user_query=null so the backend skips pattern learning
        // (paste-and-execute invariant) — a typed-but-not-generated query
        // would otherwise pollute the embeddings.
        user_query: generatedQuery.current || null,
        workflow_id: workflowId.current || null,
      }, (data) => {
        if (data.status === 'running') {
          addExec('info', data.message || data.log || '…')
        } else if (data.status === 'complete' && data.result) {
          const passed = data.test_status === 'passed'
          setOutcome(passed ? 'pass' : 'fail')
          setExecSecs(execStart.current ? (Date.now() - execStart.current) / 1000 : null)
          const report = data.result.report_html || null
          const log = data.result.log_html || null
          setReportUrl(report); setLogUrl(log)
          feedbackId.current = runIdFromReport(report || log) || workflowId.current
          // result.logs carries the parsed Robot summary (per-test status,
          // failure messages, pass/fail counts). The result card renders it
          // structured, so the stream just gets a one-line closing status.
          if (data.result.logs) setSummary(parseRobotSummary(String(data.result.logs)))
          addExec(passed ? 'success' : 'error', data.message || (passed ? 'All tests passed' : 'Some tests failed'))
        } else if (data.status === 'error') {
          addExec('error', data.message || 'Execution failed')
          setError(data.message || 'Execution failed')
        }
      })
    } catch (e) {
      const msg = e instanceof Error ? e.message : 'Execution failed'
      addExec('error', msg); setError(msg)
    } finally {
      setPhase('idle')
    }
  }

  function handleCopy() {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true); setTimeout(() => setCopied(false), 2000)
    }).catch(() => {})
  }

  function handleDownload() {
    const blob = new Blob([code], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = 'test.robot'; a.click()
    URL.revokeObjectURL(url)
  }

  function handleNew() {
    // Same guard as the legacy UI: starting over with code present is destructive.
    if (code.trim() && !window.confirm('This will clear your current test. Start a new test?')) return
    setQuery(''); setCode(''); setPhase('idle')
    setGenProgress(0); setGenStage('')
    setGenLogs([]); setExecLogs([]); setOutcome(null); setExecSecs(null); setReportUrl(null); setLogUrl(null); setSummary(null); setError('')
    workflowId.current = null; feedbackId.current = null; generatedQuery.current = ''; execStart.current = null
  }

  return (
    // No space-y here: its child-margin override outranks the sections' own
    // mb-*/mt-* utilities and zeroes them (Tailwind specificity), so each
    // section sets its own margin instead.
    <div className="w-full">
      {/* Page header */}
      <div className="mb-5 flex items-start justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Generate Test</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Describe what to test in plain English — AI generates ready-to-run Robot Framework code
          </p>
        </div>
        {(code || query || genLogs.length > 0) && (
          <Button variant="outline" size="sm" onClick={handleNew} disabled={busy} className="gap-1.5 shrink-0">
            <Plus className="h-3.5 w-3.5" /> New Test
          </Button>
        )}
      </div>

      {error && (
        <div className="mb-4 rounded-md border border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      {/* Two-column workspace — fills the viewport like the legacy runner */}
      <div className="mb-6 grid grid-cols-5 gap-4 max-lg:grid-cols-1 lg:h-[calc(100vh-235px)] lg:min-h-[480px]">
        {/* Input card */}
        <Card className="col-span-2 flex flex-col max-lg:col-span-1">
          <CardHeader className="pb-3">
            <CardTitle className="text-sm">Test Description</CardTitle>
            <CardDescription className="text-xs">Write what you want to test in plain English</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-1 flex-col gap-3 pt-0">
            <Textarea
              className="min-h-[260px] flex-1 resize-none font-mono text-[13px]"
              placeholder={PLACEHOLDER}
              value={query}
              onChange={e => setQuery(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && !code.trim()) {
                  e.preventDefault(); handleGenerate()
                }
              }}
              disabled={busy}
            />
            {/* Single adaptive action (legacy-UI pattern): code present → Run Test;
                otherwise a query → Generate Test. Review-before-run is deliberate —
                there is no combined generate-and-run. */}
            <div className="flex items-center gap-2">
              <Button
                onClick={code.trim() ? handleRun : handleGenerate}
                disabled={busy || (!code.trim() && !query.trim())}
                className="h-10 flex-1 gap-2"
              >
                {busy
                  ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                  : code.trim() ? <Play className="h-4 w-4" /> : <Zap className="h-4 w-4" />}
                {phase === 'generating' ? `Generating… ${genProgress}%`
                  : phase === 'executing' ? 'Executing…'
                    : code.trim() ? 'Run Test'
                      : query.trim() ? 'Generate Test'
                        : 'Enter a query or paste code'}
              </Button>
              {!busy && code.trim() && query.trim() && (
                <Button variant="outline" onClick={handleGenerate} className="h-10 gap-1.5" title="Discard the current code and regenerate from the description">
                  <Zap className="h-3.5 w-3.5" /> Regenerate
                </Button>
              )}
            </div>
          </CardContent>
        </Card>

        {/* Generated / editable code card. overflow-hidden is load-bearing: as a
            grid item its automatic minimum size would otherwise be the editor's
            full content height, so a long test grew the row past the grid's own
            fixed box and the cards spilled underneath the result card. Clipping
            pins the card to the row; the editor scrolls internally instead. */}
        <Card className="col-span-3 flex flex-col overflow-hidden max-lg:col-span-1">
          <CardHeader className="flex-row items-start justify-between space-y-0 pb-3">
            <div>
              <CardTitle className="text-sm">Generated Code</CardTitle>
              <CardDescription className="text-xs">Editable — tweak it or paste your own, then run</CardDescription>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {phase === 'executing' && (
                <Badge variant="secondary" className="gap-1.5 text-xs">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-current" /> Executing
                </Badge>
              )}
              {outcome === 'pass' && (
                <Badge className="bg-green-100 text-green-700 hover:bg-green-100 text-xs border-green-200">
                  Passed{execSecs != null ? ` in ${execSecs.toFixed(1)}s` : ''}
                </Badge>
              )}
              {outcome === 'fail' && <Badge className="bg-red-100 text-red-700 hover:bg-red-100 text-xs border-red-200">Failed</Badge>}
              <Button variant="outline" size="sm" className="h-7 gap-1 text-xs" onClick={handleCopy} disabled={!code}>
                {copied ? <Check className="h-3 w-3 text-green-600" /> : <Copy className="h-3 w-3" />} Copy
              </Button>
              <Button variant="outline" size="sm" className="h-7 gap-1 text-xs" onClick={handleDownload} disabled={!code}>
                <Download className="h-3 w-3" /> Export
              </Button>
            </div>
          </CardHeader>
          <CardContent className="flex min-h-0 flex-1 flex-col gap-3 px-4 pt-0">
            {phase === 'generating' ? (
              <GenerationPipeline progress={genProgress} stage={genStage} logs={genLogs} />
            ) : (
              /* Inset, bordered dark code block (GitHub-style) rather than an
                  edge-to-edge black panel */
              <div
                className="relative flex min-h-0 flex-1 flex-col"
                onKeyDown={e => {
                  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && code.trim()) {
                    e.preventDefault(); handleRun()
                  }
                }}
              >
                <RobotCodeEditor
                  value={code}
                  onChange={v => {
                    setCode(v)
                    // Clearing the editor severs the link to the last generation
                    // (the clear-then-paste flow). Whatever is typed/pasted next
                    // runs unattributed, so old query/workflow ids can't pollute
                    // the learning records. Partial edits keep attribution —
                    // tweaking generated code before running is the intended flow.
                    if (!v.trim()) {
                      workflowId.current = null
                      generatedQuery.current = ''
                    }
                  }}
                  disabled={busy}
                  placeholder={'*** Settings ***\nLibrary    Browser\n\nGenerated code appears here, or paste your own…'}
                  className="min-h-[300px] flex-1 rounded-lg border border-border shadow-sm"
                />
                {/* Live-run strip while the scenario executes: the code is not
                    being "scanned" — it is running against a real browser in an
                    isolated Docker container, so say exactly that. */}
                {phase === 'executing' && (
                  <div className="pointer-events-none absolute inset-x-0 bottom-0 rounded-b-lg border-t border-sky-500/20 bg-[#0d1117]/90 px-4 py-2 backdrop-blur-sm">
                    <div className="flex items-center gap-2.5 text-xs text-[#8b949e]">
                      <span className="relative flex h-2 w-2 shrink-0">
                        <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-sky-400 opacity-60" />
                        <span className="relative inline-flex h-2 w-2 rounded-full bg-sky-400" />
                      </span>
                      <span className="font-medium text-[#e6edf3]">Running your scenario</span>
                      <span className="truncate max-sm:hidden">— live browser session in an isolated Docker container</span>
                      <ExecElapsed start={execStart.current} />
                    </div>
                  </div>
                )}
              </div>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Result card — the headline, above both log streams: outcome,
          per-test breakdown, report links and the feedback footer.
          mt-6 collapses with the workspace margin for even breathing room. */}
      {outcome && (
        <div ref={resultRef} className="mt-6 mb-3 scroll-mt-4">
          <ExecutionResult outcome={outcome} summary={summary} secs={execSecs} reportUrl={reportUrl} logUrl={logUrl}>
            {/* Feedback — only for generated runs. Paste-and-execute has no
                query, so there is nothing for the learning system to
                attribute (legacy guard). The id check lives here so the
                card's footer divider doesn't render around an empty panel. */}
            {!busy && generatedQuery.current.trim() !== '' && feedbackId.current != null && (
              <FeedbackPanel outcome={outcome} workflowId={feedbackId.current} />
            )}
          </ExecutionResult>
        </div>
      )}

      {/* Logs — supporting detail below the result; both tuck away once the
          run completes (still expandable). Generation logs stay hidden WHILE
          generating: the code panel already mirrors every event live, and
          rendering them below would grow the page mid-run and pop in a
          scrollbar the user isn't using. They appear after delivery as the
          post-mortem record. */}
      {phase !== 'generating' && genLogs.length > 0 && (
        <LogsSection
          title="Generation Logs" desc="Real-time test generation progress"
          logs={genLogs} running={false} collapseOnDone={outcome !== null}
        />
      )}
      {execLogs.length > 0 && (
        <div ref={execRef} className="scroll-mt-4">
          <LogsSection
            title="Execution Logs" desc="Real-time Docker execution output"
            logs={execLogs} running={phase === 'executing'} collapseOnDone={outcome !== null}
          />
        </div>
      )}

    </div>
  )
}
