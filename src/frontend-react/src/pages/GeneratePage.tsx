import { useEffect, useReducer, useRef, useState, type CSSProperties, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { api, isAccessLoss } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import { Zap, Play, Plus, Download, Copy, Check, ChevronDown, ExternalLink, CheckCircle2, XCircle, FileText, X, ThumbsUp, ThumbsDown, Brain, ScanSearch, Code2, ShieldCheck, Crosshair, AlertTriangle, Info } from 'lucide-react'
import RobotCodeEditor from '@/components/RobotCodeEditor'
import { ALREADY_RECORDED_HEADING, SWITCHED_OFF_MARKER } from '@/components/RecordedCorrections'

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
  const m = /\/reports\/([^/]+)\//.exec(url)
  return m ? m[1] : null
}

/* ── Per-test results parsed from the backend's output.xml summary blob ── */
interface RobotSummary {
  tests: { name: string; status: 'PASS' | 'FAIL' }[]
  failures: { test: string; message: string }[]
  passed: number | null
  failed: number | null
}

/** One pass/fail count: the `Results:` line's own number when there is one,
    else a count of the per-test lines this blob actually parsed. */
function testCount(
  counts: RegExpExecArray | null,
  index: 1 | 2,
  tests: RobotSummary['tests'],
  status: 'PASS' | 'FAIL',
): number | null {
  if (counts) return Number(counts[index])
  if (tests.length) return tests.filter(t => t.status === status).length
  return null
}

function parseRobotSummary(blob: string): RobotSummary {
  const tests: RobotSummary['tests'] = []
  const failures: RobotSummary['failures'] = []
  let failing: string | null = null
  for (const line of blob.split('\n')) {
    const t = /^\s*Test: (.+) - (PASS|FAIL)$/.exec(line)
    if (t) {
      tests.push({ name: t[1], status: t[2] as 'PASS' | 'FAIL' })
      failing = t[2] === 'FAIL' ? t[1] : null
      continue
    }
    const e = /^\s*Error: (.+)$/.exec(line)
    if (e && failing) {
      failures.push({ test: failing, message: e[1] })
      failing = null
    }
  }
  const counts = /^Results: (\d+) passed, (\d+) failed$/m.exec(blob)
  return {
    tests,
    failures,
    passed: testCount(counts, 1, tests, 'PASS'),
    failed: testCount(counts, 2, tests, 'FAIL'),
  }
}

const LOG_BORDER_CLASS: Record<LogEntry['kind'], string> = {
  success: 'border-l-green-500',
  error: 'border-l-destructive',
  info: 'border-l-blue-400',
}

/* ── Collapsible logs card with an indeterminate bar while running ── */
function LogsSection({ title, desc, logs, running, collapseOnDone }: Readonly<{
  title: string; desc: string; logs: LogEntry[]; running: boolean; collapseOnDone?: boolean
}>) {
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
            {/* Index is a correct key: addGen/addExec only ever append, this
                array is reset to [] (not spliced or resorted) at the start of
                the next run, so no index here is ever reused for a different
                line. */}
            {logs.map((log, i) => (
              <div
                key={i}
                className={cn(
                  'flex gap-3 border-b px-4 py-1.5 last:border-0 border-l-2',
                  LOG_BORDER_CLASS[log.kind],
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
function PlanActivity({ progress }: Readonly<{ progress: number }>) {
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
function LocateActivity({ progress, elementCount }: Readonly<{ progress: number; elementCount: number | null }>) {
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
      {/* Index is a correct key: ASSEMBLE_LINES is a fixed module-level
          constant, mapped in full and in the same order on every render —
          there is no filter, reorder or runtime source that could shift it. */}
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
      <span className="h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-[#30363d] border-t-sky-400" />{' '}
      Compiling the test in a clean Docker container — auto-fixing anything that fails
    </div>
  )
}

type StageState = 'done' | 'active' | 'pending'

function stageState(done: boolean, active: boolean): StageState {
  if (done) return 'done'
  if (active) return 'active'
  return 'pending'
}

const STAGE_ICON_CLASSES: Record<StageState, string> = {
  done: 'border-emerald-500/40 bg-emerald-500/15 text-emerald-400',
  active: 'stage-active border-sky-400/50 bg-sky-400/10 text-sky-300',
  pending: 'border-[#30363d] text-[#484f58]',
}

const STAGE_LABEL_CLASSES: Record<StageState, string> = {
  done: 'text-[#8b949e]',
  active: 'text-[#e6edf3]',
  pending: 'text-[#484f58]',
}

function GenerationPipeline({ progress, stage, logs }: Readonly<{ progress: number; stage: string; logs: LogEntry[] }>) {
  // The element-scan SSE event carries the only real artifact count we get
  // ("📍 Found N elements on the page") — surface it in the Locate block.
  const elMatch = logs.map(l => /Found (\d+) elements/.exec(l.msg)).find(Boolean)
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
            const state = stageState(done, active)
            const Icon = s.icon
            return (
              <div key={s.label} className="flex items-center gap-2">
                <span
                  className={cn(
                    'flex h-6 w-6 items-center justify-center rounded-full border transition-colors',
                    STAGE_ICON_CLASSES[state],
                  )}
                >
                  {done ? <Check className="h-3.5 w-3.5" /> : <Icon className="h-3.5 w-3.5" />}
                </span>
                <span className={cn('text-xs font-medium', STAGE_LABEL_CLASSES[state])}>
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
function ExecElapsed({ start }: Readonly<{ start: number | null }>) {
  // useReducer, not useState: this value is never read, only ever bumped to
  // force a re-render every second, so there is no "state" here to name.
  const [, forceRerender] = useReducer(x => x + 1, 0)
  useEffect(() => {
    const id = setInterval(forceRerender, 1000)
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

/** The result card's headline. Single-test runs are the norm — counts and the
    per-test list only earn their place when there is more than one test. */
function resultTitle(pass: boolean, total: number | null, failed: number | null | undefined): string {
  if (pass) return total === 1 ? 'Test passed! 🎉' : 'All tests passed! 🎉'
  if (failed && total) return total > 1 ? `${failed} of ${total} tests failed` : 'Test failed'
  return 'Test execution failed'
}

/* ── Post-run result card: outcome banner, per-test breakdown, report links,
   and an optional feedback footer (children) ── */
function ExecutionResult({ outcome, summary, secs, reportUrl, logUrl, children }: Readonly<{
  outcome: Exclude<Outcome, null>; summary: RobotSummary | null; secs: number | null
  reportUrl: string | null; logUrl: string | null; children?: ReactNode
}>) {
  const pass = outcome === 'pass'
  const tests = summary?.tests ?? []
  const failures = summary?.failures ?? []
  const total = summary?.passed != null && summary?.failed != null
    ? summary.passed + summary.failed
    : tests.length || null
  const title = resultTitle(pass, total, summary?.failed)
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
            {/* Index is a correct key: `tests` is written once, at the same
                moment `outcome` turns truthy, and the parent's `{outcome &&
                ...}` gate (GeneratePage) unmounts this whole card at the
                start of every new run — so this array is never reordered or
                filtered while a single instance of this list stays mounted. */}
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
            {/* Index is a correct key here for the same reason as the tests
                list above: `failures` comes from the same once-per-run
                `summary`, and this card remounts before a next one exists. */}
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
    "no_org" | "queued" | "hint_inactive" | "error"); it is absent when learning is switched off,
    which is equally not a success. `message` is the backend's own sentence for
    whichever case fired; the panel writes its own only when the body carries
    none (FeedbackPanel's `submit`). */
interface FeedbackResponse { status?: string; outcome?: string; message?: string }

/** GET /api/feedback/{run_id}. The corrections this run has already
    contributed — the hints it created AND the ones it reinforced. T5 makes a
    second submission of the same text from the same run a no-op, and this is
    the answer to that: the user sees their own words on file rather than a
    warning about a duplicate the backend could not report anyway.

    can_retract is server-computed per correction (hint_mutation_verdict,
    auth/ownership.py — the same rule that gates the admin dashboard's hint
    mutations): true for the hint's own author or an org admin, false for
    anyone else. Optional because an older backend, or the learning-disabled
    response shape, never sends it — absence must render exactly like false.

    active is the server's own word for is_active. Optional for the same
    reason can_retract is: an older backend, or the learning-disabled
    response shape, never sends it — absence must render exactly like today,
    never like switched off. */
interface RecordedCorrection { hint_id: number; feedback_text: string; recorded_at: string; active?: boolean; can_retract?: boolean }
interface RecordedResponse { corrections?: RecordedCorrection[] }

/** A recorded correction as the PANEL holds it: the server's row plus what
    this session has since done to it. `retracted` is client-only, and it is
    not the inverse of can_retract — can_retract is also false for a hint that
    is perfectly active and simply not this caller's to touch. 'already' is
    the changed:false answer below: retracted, but not by this click. */
interface PanelCorrection extends RecordedCorrection { retracted?: 'now' | 'already' }

/**
 * The ONE marker a recorded correction shows, in precedence order.
 *
 * `retracted` is client-only, set by THIS session's own retract click, and it
 * wins because it is the more specific fact — it knows WHO did it.
 * `active === false` covers every other way the hint went inactive (another
 * caller's retract, auto-disable, an LLM review); the server cannot tell those
 * apart, so this marker does not pretend to either.
 *
 * The test is `active === false`, never `!active`, so a missing field — an
 * older backend, or the learning-disabled response — renders exactly as it
 * does today, with no marker at all.
 */
function correctionMarker(c: PanelCorrection): string | null {
  if (c.retracted === 'already') return '— already retracted'
  if (c.retracted) return '— retracted'
  if (c.active === false) return SWITCHED_OFF_MARKER
  return null
}

/**
 * What to say about a correction this run has already contributed.
 *
 * All three sentences are scoped to THIS run on purpose. Sending the same text
 * again here dedups to the existing hint and the claim row for this run already
 * exists, so nothing reactivates it — but the same text from a LATER run does
 * reinforce, and an unqualified "it can't come back" would be a new false
 * claim. These fire BEFORE the click: they are the only thing that warns while
 * the user can still change their mind, which is why they are kept even though
 * the backend now answers such a resubmission honestly ("hint_inactive").
 *
 * The first two say the same "won't come back" fact; only the first claims WHO.
 * `active === false` alone does not know the retract was this user's own, so
 * the second must not say "you" — and neither may `retracted: 'already'`, which
 * is the route answering that the hint was inactive BEFORE this click. The
 * click happened; it is not what switched the correction off. It cannot fall
 * through to the third sentence either: `active` is whatever the GET said and
 * is not refreshed by the retract, so a stale `active: true` would drop the
 * "won't come back" fact altogether.
 */
function alreadySentNotice(c: PanelCorrection): string {
  if (c.retracted === 'now') return 'You retracted this correction. Sending it again on this run won’t restore it.'
  if (c.retracted === 'already' || c.active === false) {
    return 'This correction is switched off. Sending it again on this run won’t turn it back on.'
  }
  return 'You already sent this for this run — it won’t be counted again.'
}

/** POST /api/learning/hints/{id}/retract → { hint, changed, note }
    (learning_endpoints.py). `changed: false` is the route's own answer for a
    hint that was ALREADY inactive — an org admin who retracted it between
    this panel's GET and this click. */
interface RetractResponse { changed?: boolean }

/* POST /api/learning/hints/{id}/retract requires a non-empty `actor`, but
   _audit_actor (learning_endpoints.py) overrides it with the verified
   token's email whenever a token is present — the client-supplied value is
   never trusted and never reaches the audit row. This placeholder exists
   only to satisfy the 400-on-blank validation; sending the signed-in user's
   own email here would misleadingly imply the client decides identity. */
const RETRACT_ACTOR_PLACEHOLDER = 'feedback-panel'

/* ── Feedback footer, rendered inside the result card (generated runs only).
   Pass: thumbs row — 👍 is a UI-only acknowledgment (passing runs already feed
   learning automatically at execution time; an empty positive carried no
   signal, so it no longer calls the backend). 👎 expands an inline correction
   form — the corrective text is the signal that actually trains the system.
   Fail: form open by default; Skip still records an empty completely_wrong
   label on the execution record, but nothing is learned from it (outcome
   "no_text") — the empty text carries nothing for any engine to route. ── */
export function FeedbackPanel({ outcome, workflowId }: Readonly<{ outcome: Exclude<Outcome, null>; workflowId: string | null }>) {
  const [open, setOpen] = useState(outcome === 'fail')
  const [ack, setAck] = useState(false)
  const [text, setText] = useState('')
  const [status, setStatus] = useState<'idle' | 'sending'>('idle')
  // Three flags rather than the raw outcome string: each one names a
  // PRESENTATION this panel draws (terminal green, terminal muted, a notice
  // beside a live form), and several outcomes share the last of them.
  const [result, setResult] = useState<{ ok: boolean; neutral: boolean; inactive: boolean; message: string } | null>(null)
  const [err, setErr] = useState('')
  const [recorded, setRecorded] = useState<PanelCorrection[]>([])
  // Two fetches write `recorded`: this panel's mount read and the post-submit
  // refresh below. Whichever STARTED last is the newer question, so a slow
  // earlier response must not overwrite it.
  //
  // No regression test accompanies this, deliberately, and the reason is worth
  // keeping: the overwrite is currently UNOBSERVABLE. The refresh runs only
  // when `ok` is true, and an `ok` submission retires the form — the
  // "Already recorded" list is not rendered in that terminal state, so a stale
  // value cannot reach the screen. The guard is three lines of insurance
  // against that render condition changing, not a fix for a live defect.
  const recordedGen = useRef(0)
  // Which items' Retract is in flight, and the message for whichever ones
  // failed — both keyed by hint id, because this list can hold several
  // corrections and each one's control acts independently. A single id and a
  // single string made them interfere: starting B re-enabled A's button while
  // A's POST was still in flight, and wiped A's error before the user could
  // read it. Separate from `err` above: that field is the main correction
  // FORM's error, and a failed retract is a different action from a different
  // control.
  const [retracting, setRetracting] = useState<ReadonlySet<number>>(new Set())
  const [retractErrs, setRetractErrs] = useState<Record<number, string>>({})

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
    const gen = ++recordedGen.current
    api<RecordedResponse>(`/api/feedback/${encodeURIComponent(workflowId)}`, { cache: 'no-store' })
      .then(b => { if (live && gen === recordedGen.current) setRecorded(Array.isArray(b?.corrections) ? b.corrections : []) })
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
  //
  // The matching ITEM, not a boolean: a correction the user has since
  // retracted needs a different sentence from one that is simply on file.
  const alreadySent = recorded.find(c => c.feedback_text === text.trim())

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
      // The run-level gate refused this resubmission because the hint it
      // matches is switched off. Nothing was stored and nothing broke, so
      // neither terminal state fits — and unlike every other answer below,
      // "send it again" is not the way out, so the amber retry notice would be
      // wrong too. Its own presentation, with the form left alive: the user
      // did not decline to speak, and they may have something different to
      // say. The panel adds no words of its own here — the backend's sentence
      // is the only one that knows which hint and why.
      const inactive = body?.outcome === 'hint_inactive'
      // M9: the mount-effect fetch above only ever runs once, so a correction
      // filed during THIS session never showed up in "Already recorded for
      // this run" until the page reloaded. Re-fetch once the backend confirms
      // this one actually landed — display only, not the storage check (that
      // is `outcome`, already read above).
      if (ok && workflowId) {
        const gen = ++recordedGen.current
        api<RecordedResponse>(`/api/feedback/${encodeURIComponent(workflowId)}`, { cache: 'no-store' })
          .then(b => { if (gen === recordedGen.current) setRecorded(Array.isArray(b?.corrections) ? b.corrections : []) })
          .catch(() => { /* silence is the honest degradation here, same as the mount fetch */ })
      }
      setResult({
        ok,
        neutral,
        inactive,
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

  // can_retract is server-decided (hint_mutation_verdict) — this only fires
  // the POST the control's own visibility already cleared.
  //
  // Confirmed first, like the two admin surfaces that offer the same action
  // (LearningPage's Hints table and HintDrawer). This is a small destructive
  // button sitting against the user's own text; it is also the ONLY retract
  // control a plain org member ever sees, and they have the fewest ways back
  // — /learning is closed to them, and reactivate is org-admin-and-above.
  // The wording is theirs, not the admin sentence: "stop injecting into agent
  // prompts" is not a thing to ask a plain member to reason about.
  //
  // On success the row STAYS, with its control cleared and a retracted
  // marker added. Dropping it hid the user's own words — and the words are
  // deliberately kept: get_corrections_for_run is unfiltered on is_active
  // because "these are the user's own words", so the very next mount showed
  // the correction again, without a button, and the two mounts disagreed
  // about what had been said. Keeping the row also keeps the duplicate
  // notice below armed, which WARNS before a resubmission that does
  // nothing: re-sending the identical text on THIS run dedups to the same
  // hint, and _claim_feedback_run's ON CONFLICT DO NOTHING returns before
  // the reinforcement that would set is_active back to 1. It does not stop
  // the resubmission — Submit stays enabled — but the answer is no longer
  // "Thanks…": the gate reports the refusal on a switched-off hint all the
  // way up, and the response arrives as outcome "hint_inactive". This marker
  // is the warning BEFORE the click; that outcome is the truthful answer
  // after it, and it survives the reload this client-only marker does not.
  async function retract(hintId: number) {
    if (!window.confirm('Retract this correction? It will stop shaping future tests.')) return
    setRetracting(prev => new Set(prev).add(hintId))
    setRetractErrs(prev => {
      const next = { ...prev }
      delete next[hintId]
      return next
    })
    try {
      const body = await api<RetractResponse>(`/api/learning/hints/${hintId}/retract`, {
        method: 'POST',
        body: JSON.stringify({
          actor: RETRACT_ACTOR_PLACEHOLDER,
          reason: 'retracted from feedback panel',
        }),
      })
      // The hint is retracted under both answers, so the row is marked under
      // both. changed:false only means this click was not what did it, and
      // reporting a no-op as the user's own action is the thing to avoid.
      const mark: PanelCorrection['retracted'] = body?.changed === false ? 'already' : 'now'
      setRecorded(prev => prev.map(c =>
        c.hint_id === hintId ? { ...c, can_retract: false, retracted: mark } : c))
    } catch (e) {
      // Unlike the read above, this is a WRITE the user explicitly asked
      // for — silence here would let a failed retract look like it worked.
      setRetractErrs(prev => ({
        ...prev,
        [hintId]: e instanceof Error ? e.message : 'Could not retract this correction',
      }))
      // 403/404 say the caller cannot act on this hint AT ALL — it is not
      // theirs, or it is gone — so the control is dead and leaving it draws
      // a button that can only fail again. The row still stays (their words
      // are still their words) and it is NOT marked retracted: a refusal is
      // not a retraction. Every other failure is transient or ours, so
      // Retract survives for a retry.
      if (isAccessLoss(e)) {
        setRecorded(prev => prev.map(c =>
          c.hint_id === hintId ? { ...c, can_retract: false } : c))
      }
    } finally {
      setRetracting(prev => {
        const next = new Set(prev)
        next.delete(hintId)
        return next
      })
    }
  }

  if (ack) {
    return (
      <div className="flex items-center gap-2 text-sm">
        <Check className="h-4 w-4 text-green-600" /> Great — glad it did exactly what you asked.
      </div>
    )
  }

  // Only a recorded correction retires the form. Every other answer that tells
  // the user to send it again leaves the textarea, their typed text and Submit
  // alive — replacing them with the message would be advice the UI makes
  // impossible to follow. Rendered like `err` below: a notice beside a
  // still-usable form, not a terminal state.
  //
  // Two answers do not say "send it again", and each gets its own treatment:
  //
  //   no_text — the user clicked Skip, so a retry is advice they already
  //   declined. It retires the form, with a neutral presentation: no green
  //   check (nothing was learned) and no amber warning (nothing failed).
  //
  //   hint_inactive — the correction is on file but switched off, and
  //   re-sending on this run can never turn it back on. The form STAYS (they
  //   may have a different correction to make) and the notice is neutral for
  //   the same reason no_text's is: nothing was learned, and nothing broke.
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
      {result && (result.inactive ? (
        /* Same muted card as "Already recorded for this run" below, and for
           the same reason: this is a fact about what is on file, not a
           problem to fix. Amber + AlertTriangle would report a failure that
           did not happen. */
        <div className="flex items-start gap-2 rounded-md border bg-muted/40 px-3 py-2 text-xs text-muted-foreground">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{result.message}</span>
        </div>
      ) : (
        <div className="flex items-start gap-2 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
          <span>{result.message}</span>
        </div>
      ))}
      {recorded.length > 0 && (
        <div className="rounded-md border bg-muted/40 px-3 py-2 text-xs">
          <div className="font-medium">{ALREADY_RECORDED_HEADING}</div>
          <ul className="mt-1 space-y-0.5 text-muted-foreground">
            {recorded.map(c => (
              <li key={c.hint_id}>
                <div className="flex items-center justify-between gap-2">
                  <span>
                    “{c.feedback_text}”
                    {/* Exactly one marker, in precedence order. `retracted` is
                        client-only, set by THIS session's own retract click,
                        and wins because it is the more specific fact — it
                        knows WHO did it. `active === false` covers every
                        other way the hint went inactive (another caller's
                        retract, auto-disable, an LLM review) — the server
                        can't tell those apart, so this marker doesn't
                        pretend to either. */}
                    {correctionMarker(c) && (
                      <span className="ml-1.5 italic">{correctionMarker(c)}</span>
                    )}
                  </span>
                  {/* can_retract is absent or false on an older backend and on
                      the learning-disabled shape — both render exactly like
                      today, with no control. */}
                  {c.can_retract && (
                    <Button
                      variant="outline"
                      size="sm"
                      className="h-6 shrink-0 px-1.5 text-[11px] text-destructive"
                      disabled={retracting.has(c.hint_id)}
                      onClick={() => retract(c.hint_id)}
                    >
                      Retract
                    </Button>
                  )}
                </div>
                {/* Beside the correction it belongs to, not under the list:
                    with several rows, one shared line cannot say which
                    Retract failed. */}
                {retractErrs[c.hint_id] && (
                  <p className="mt-0.5 text-destructive">{retractErrs[c.hint_id]}</p>
                )}
              </li>
            ))}
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
          {alreadySentNotice(alreadySent)}
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

/* ── The single adaptive action button: code present → Run Test; otherwise
   a query → Generate Test (see the button's own comment at its call site
   for why this stays one control rather than a combined generate-and-run) ── */
function runButtonIcon(busy: boolean, hasCode: boolean): ReactNode {
  if (busy) return <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
  return hasCode ? <Play className="h-4 w-4" /> : <Zap className="h-4 w-4" />
}

function runButtonLabel(phase: Phase, genProgress: number, hasCode: boolean, hasQuery: boolean): string {
  if (phase === 'generating') return `Generating… ${genProgress}%`
  if (phase === 'executing') return 'Executing…'
  if (hasCode) return 'Run Test'
  if (hasQuery) return 'Generate Test'
  return 'Enter a query or paste code'
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
                {runButtonIcon(busy, !!code.trim())}
                {runButtonLabel(phase, genProgress, !!code.trim(), !!query.trim())}
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
                  edge-to-edge black panel.
                  role="presentation": this div is not a control — it only
                  relays the Ctrl/Cmd+Enter keydown that bubbles up from the
                  editor's own textarea inside it. A bare div with a key
                  handler and no role reads to Sonar's S6848 as a fake
                  interactive element, but jsx-a11y's own docs name this exact
                  shape ("an element catching bubbled events from elements it
                  contains") and prescribe role="presentation" as the fix.
                  It is purely an ARIA hint — the shortcut still fires
                  identically for mouse and keyboard users either way. */
              <div
                className="relative flex min-h-0 flex-1 flex-col"
                role="presentation"
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
