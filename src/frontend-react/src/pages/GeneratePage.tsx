import { useEffect, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Textarea } from '@/components/ui/textarea'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { cn } from '@/lib/utils'
import { api } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import { Zap, Play, Plus, Download, Copy, Check, ChevronDown, ExternalLink } from 'lucide-react'
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

/* ── Collapsible logs card with an indeterminate bar while running ── */
function LogsSection({ title, desc, logs, running }: {
  title: string; desc: string; logs: LogEntry[]; running: boolean
}) {
  const [open, setOpen] = useState(true)
  const listRef = useRef<HTMLDivElement>(null)
  // Keep the newest log line in view while streaming (legacy-UI behaviour)
  useEffect(() => {
    if (listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight
  }, [logs.length])
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

/* ── Feedback panel (pass = optional, fail = prompted); posts /api/feedback ── */
function FeedbackPanel({ outcome, workflowId }: { outcome: Exclude<Outcome, null>; workflowId: string | null }) {
  const [open, setOpen] = useState(outcome === 'fail')
  const [text, setText] = useState('')
  const [status, setStatus] = useState<'idle' | 'sending' | 'done'>('idle')
  const [err, setErr] = useState('')

  async function submit() {
    if (!workflowId) return
    setStatus('sending'); setErr('')
    try {
      await api('/api/feedback', {
        method: 'POST',
        body: JSON.stringify({
          workflow_id: workflowId,
          feedback_text: text.slice(0, 500),
          feedback_type: outcome === 'pass' ? 'close_enough' : 'completely_wrong',
        }),
      })
      setStatus('done')
    } catch (e) {
      setStatus('idle')
      setErr(e instanceof Error ? e.message : 'Could not send feedback')
    }
  }

  if (status === 'done') {
    return (
      <div className="flex items-center gap-2 rounded-lg border bg-card px-4 py-3 text-sm">
        <Check className="h-4 w-4 text-green-600" /> Thanks — your feedback helps the system learn.
      </div>
    )
  }

  // Pass + not expanded: celebration banner with an opt-in link.
  if (outcome === 'pass' && !open) {
    return (
      <div className="flex items-center justify-between rounded-lg border bg-card px-4 py-3">
        <span className="text-sm font-semibold text-green-600">🎉 All tests passed successfully!</span>
        {workflowId && (
          <button onClick={() => setOpen(true)} className="text-xs text-muted-foreground hover:text-foreground transition-colors">
            Something not quite right? Tell us →
          </button>
        )}
      </div>
    )
  }

  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle className="text-sm">
          {outcome === 'fail' ? '💡 Help us get it right next time' : 'Share what was off'}
        </CardTitle>
        <CardDescription className="text-xs">
          {outcome === 'fail'
            ? 'Tell us what the test should have done — this trains the generator.'
            : 'Optional — describe anything that wasn’t quite right.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        <Textarea
          value={text}
          maxLength={500}
          onChange={e => setText(e.target.value)}
          placeholder="e.g. it clicked the wrong button; the search box locator was off…"
          className="min-h-[80px] text-sm"
        />
        <div className="flex items-center justify-between">
          <span className="text-xs text-muted-foreground">{text.length}/500</span>
          {err && <span className="text-xs text-destructive">{err}</span>}
          <div className="flex gap-2">
            {outcome === 'pass' && (
              <Button variant="ghost" size="sm" onClick={() => setOpen(false)} disabled={status === 'sending'}>
                Cancel
              </Button>
            )}
            <Button size="sm" onClick={submit} disabled={status === 'sending' || !workflowId}>
              {status === 'sending' ? 'Sending…' : 'Submit feedback'}
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
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
  const [reportUrl, setReportUrl] = useState<string | null>(null)
  const [error, setError]   = useState('')
  const [copied, setCopied] = useState(false)

  const workflowId = useRef<string | null>(null)   // set on generation complete
  const feedbackId = useRef<string | null>(null)   // run_id used for feedback
  const generatedQuery = useRef<string>('')        // query that produced `code`
  const progressRef = useRef<HTMLDivElement>(null) // auto-scroll target while generating
  const execRef = useRef<HTMLDivElement>(null)     // auto-scroll target while executing

  const busy = phase === 'generating' || phase === 'executing'

  // Bring the active progress/logs section into view when a run starts —
  // it renders below the fold and the user otherwise gets no cue (legacy-UI parity).
  useEffect(() => {
    const target = phase === 'generating' ? progressRef.current
      : phase === 'executing' ? execRef.current : null
    if (target) setTimeout(() => target.scrollIntoView({ behavior: 'smooth', block: 'start' }), 80)
  }, [phase])

  const addGen  = (kind: LogEntry['kind'], msg: string) =>
    setGenLogs(l => [...l, { kind, ts: nowTs(), msg }])
  const addExec = (kind: LogEntry['kind'], msg: string) =>
    setExecLogs(l => [...l, { kind, ts: nowTs(), msg }])

  async function handleGenerate() {
    if (!query.trim() || busy) return
    setError(''); setGenLogs([]); setExecLogs([]); setOutcome(null); setReportUrl(null); setCode('')
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
            addGen('error', data.dryrun_status === 'failed'
              ? 'Delivered — dryrun found issues you may want to review'
              : 'Delivered — dryrun could not run (Docker unavailable)')
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
    setError(''); setExecLogs([]); setOutcome(null); setReportUrl(null)
    setPhase('executing')
    try {
      await streamSSE('/execute-test', {
        robot_code: robot,
        user_query: generatedQuery.current || query || null,
        workflow_id: workflowId.current || null,
      }, (data) => {
        if (data.status === 'running') {
          addExec('info', data.message || data.log || '…')
        } else if (data.status === 'complete' && data.result) {
          const passed = data.test_status === 'passed'
          setOutcome(passed ? 'pass' : 'fail')
          const rpt = data.result.report_html || data.result.log_html || null
          setReportUrl(rpt)
          feedbackId.current = runIdFromReport(rpt) || workflowId.current
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
    setQuery(''); setCode(''); setPhase('idle')
    setGenProgress(0); setGenStage('')
    setGenLogs([]); setExecLogs([]); setOutcome(null); setReportUrl(null); setError('')
    workflowId.current = null; feedbackId.current = null; generatedQuery.current = ''
  }

  return (
    <div className="w-full space-y-0">
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
      <div className="mb-4 grid grid-cols-5 gap-4 max-lg:grid-cols-1 lg:h-[calc(100vh-235px)] lg:min-h-[480px]">
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
              disabled={busy}
            />
            <Button onClick={handleGenerate} disabled={!query.trim() || busy} className="w-full gap-2">
              {phase === 'generating'
                ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                : <Zap className="h-4 w-4" />}
              {phase === 'generating' ? `Generating… ${genProgress}%` : 'Generate Test'}
            </Button>
          </CardContent>
        </Card>

        {/* Generated / editable code card */}
        <Card className="col-span-3 flex flex-col max-lg:col-span-1">
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
              {outcome === 'pass' && <Badge className="bg-green-100 text-green-700 hover:bg-green-100 text-xs border-green-200">Passed</Badge>}
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
            {/* Inset, bordered dark code block (GitHub-style) rather than an
                edge-to-edge black panel */}
            <RobotCodeEditor
              value={code}
              onChange={setCode}
              disabled={busy}
              placeholder={'*** Settings ***\nLibrary    Browser\n\nGenerated code appears here, or paste your own…'}
              className="min-h-[300px] flex-1 rounded-lg border border-border shadow-sm"
            />
            <div className="pb-1">
              <Button onClick={handleRun} disabled={!code.trim() || busy} className="w-full gap-2">
                {phase === 'executing'
                  ? <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                  : <Play className="h-4 w-4" />}
                {phase === 'executing' ? 'Executing…' : 'Run Test'}
              </Button>
            </div>
          </CardContent>
        </Card>
      </div>

      {/* Generation progress — driven by the pipeline's stage events */}
      {(phase === 'generating' || (genProgress > 0 && genProgress < 100 && genLogs.length > 0)) && (
        <Card ref={progressRef} className="mb-4 scroll-mt-4">
          <CardContent className="space-y-2.5 py-4">
            <div className="flex items-center justify-between gap-3 text-sm">
              <span className="min-w-0 truncate font-medium">{genStage || 'Working…'}</span>
              <span className="shrink-0 font-semibold tabular-nums text-muted-foreground">{genProgress}%</span>
            </div>
            <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary transition-[width] duration-700 ease-out"
                style={{ width: `${Math.max(genProgress, 2)}%` }}
              />
            </div>
            <p className="text-xs text-muted-foreground">
              Planning steps → finding page elements → writing code → verifying
            </p>
          </CardContent>
        </Card>
      )}

      {/* Logs */}
      {genLogs.length > 0 && (
        <LogsSection title="Generation Logs" desc="Real-time test generation progress" logs={genLogs} running={phase === 'generating'} />
      )}
      {execLogs.length > 0 && (
        <div ref={execRef} className="scroll-mt-4">
          <LogsSection title="Execution Logs" desc="Real-time Docker execution output" logs={execLogs} running={phase === 'executing'} />
        </div>
      )}

      {/* Report link */}
      {reportUrl && (
        <a
          href={reportUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="mb-3 inline-flex items-center gap-1.5 text-sm text-primary underline-offset-4 hover:underline"
        >
          <ExternalLink className="h-3.5 w-3.5" /> View detailed Robot Framework report
        </a>
      )}

      {/* Feedback */}
      {outcome && !busy && (
        <FeedbackPanel outcome={outcome} workflowId={feedbackId.current} />
      )}
    </div>
  )
}
