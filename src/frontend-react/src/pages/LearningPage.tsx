import { useEffect, useRef, useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Plus } from 'lucide-react'
import { useFetch } from '@/lib/useFetch'
import { api } from '@/lib/api'
import { useAuth } from '@/auth/AuthContext'
import { cn } from '@/lib/utils'
import TriggersTab from './learning/TriggersTab'
import StatsTab from './learning/StatsTab'
import HintDrawer from './learning/HintDrawer'
import AddFeedbackSheet from './learning/AddFeedbackSheet'
import RunDrawer from './learning/RunDrawer'
import type { Hint, HintsResp } from './learning/types'
interface Run {
  workflow_id: string
  timestamp: string
  user_query: string
  domain?: string
  test_status: string
  failure_category?: string | null
  nl_injected_count?: number
}
interface RunsResp { total: number; runs: Run[] }
interface Stats {
  llm_accuracy?: { flagged_events: number; reviewed_events: number; pending_review: number; engagement_rate: number | null; reversal_rate: number | null }
  manual_actions?: { unflags_30d: number; retracts_30d: number }
  llm_cost?: { total_estimated_usd: number }
}

interface ReviewSession {
  id: number
  status: string
  hint_count: number
  llm_latency_ms?: number | null
  warning?: string | null
  error_message?: string | null
  created_at: string
  completed_at?: string | null
}
interface ReviewSessionsResp { is_any_running: boolean; sessions: ReviewSession[] }
interface ReviewRec {
  id: number
  hint_id: number
  recommendation: string
  reason: string
  exoneration_count: number
  admin_decision: 'approved' | 'rejected' | null
  admin_notes?: string | null
  applied: number
  feedback_text: string
  scope?: string
  domain?: string
  applied_count?: number
  success_count?: number
  failure_count?: number
  is_active: number
  conflict_flagged: number
}
interface ReviewPage {
  id: number
  scope_type?: string | null
  scope_value?: string | null
  status: string
  hint_count?: number | null
  error_message?: string | null
  org_id?: string | null
}
interface ReviewSessionDetail {
  session: ReviewSession
  recommendations: ReviewRec[]
  pages?: ReviewPage[]
}

type View = 'overview' | 'hints' | 'triggers' | 'runs' | 'stats' | 'review'
const HINT_FILTERS = [
  { key: '', label: 'All' },
  { key: 'flagged', label: 'Flagged' },
  { key: 'active', label: 'Active' },
  { key: 'auto_disabled', label: 'Auto-disabled' },
  { key: 'llm_review_disabled', label: 'LLM-disabled' },
  { key: 'retracted', label: 'Retracted' },
] as const

const pctOrDash = (n: number | null | undefined) => (n == null ? '—' : `${(n * 100).toFixed(1)}%`)

function hintStatus(h: Hint): { label: string; cls: string } {
  if (h.is_active === 1 && h.conflict_flagged === 1) return { label: 'Flagged', cls: 'bg-amber-100 text-amber-700 border-amber-200' }
  if (h.is_active === 1) return { label: h.created_via === 'admin' ? 'Active (admin)' : 'Active', cls: 'bg-green-100 text-green-700 border-green-200' }
  // sort_priority=4 → has a retract audit row (manual); =3 → auto / LLM-review disabled
  if (h.sort_priority === 4) return { label: 'Retracted', cls: 'bg-muted text-muted-foreground' }
  if (h.llm_review_disabled === 1) return { label: 'LLM-disabled', cls: 'bg-purple-100 text-purple-700 border-purple-200' }
  return { label: 'Auto-disabled', cls: 'bg-amber-50 text-amber-600 border-amber-200' }
}

// F2: a review session can hold pages from multiple orgs (see hint_review_pages.org_id,
// Task 2/3) — two orgs sharing scope_type='global', or the same domain string, must not
// render identical labels, or an admin cannot tell which page belongs to which tenant.
export function reviewPageLabel(p: ReviewPage): string {
  const scope = p.scope_type === 'global' ? 'Global hints' : (p.scope_value || 'No domain')
  return `${scope} — ${p.org_id || 'no org'}`
}

function ErrorNote({ msg }: { msg: string }) {
  return <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">{msg}</div>
}

/* ── Learning-system health banner (OK / DEGRADED / FAILED / DISABLED) ── */
function HealthBanner() {
  const { data } = useFetch<{ status: string }>('/api/learning/health')
  if (!data) return null
  const s = (data.status || 'unknown').toUpperCase()
  const cls =
    s === 'OK' ? 'border-green-200 bg-green-50 text-green-700'
      : s === 'DISABLED' ? 'border-border bg-muted/40 text-muted-foreground'
        : s === 'FAILED' ? 'border-red-200 bg-red-50 text-red-700'
          : 'border-amber-200 bg-amber-50 text-amber-700'
  return (
    <div className={cn('rounded-md border px-4 py-2 text-sm', cls)}>
      {s === 'OK' ? '✓' : s === 'FAILED' ? '✕' : '⚠'} Learning system: {s}
      {(s === 'DEGRADED' || s === 'FAILED') && ' — a learning reliability check is failing; see server logs.'}
    </div>
  )
}

/* ── Overview ── */
function Overview() {
  const { data, loading, error } = useFetch<Stats>('/api/learning/stats')
  if (loading) return <p className="text-sm text-muted-foreground">Loading…</p>
  if (error) return <ErrorNote msg={error} />
  const a = data?.llm_accuracy
  const m = data?.manual_actions
  const c = data?.llm_cost
  return (
    <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
      <Stat label="Flagged events" value={a?.flagged_events ?? 0} />
      <Stat label="Pending review" value={a?.pending_review ?? 0} />
      <Stat label="LLM reversal rate" value={pctOrDash(a?.reversal_rate)} />
      <Stat label="Conflict-detection cost" value={`$${(c?.total_estimated_usd ?? 0).toFixed(4)}`} />
      <Stat label="Reviewed events" value={a?.reviewed_events ?? 0} />
      <Stat label="Engagement rate" value={pctOrDash(a?.engagement_rate)} />
      <Stat label="Unflags (30d)" value={m?.unflags_30d ?? 0} />
      <Stat label="Retracts (30d)" value={m?.retracts_30d ?? 0} />
    </div>
  )
}
function Stat({ label, value }: { label: string; value: number | string }) {
  return (
    <Card><CardContent className="p-5">
      <p className="text-xs font-medium text-muted-foreground mb-2">{label}</p>
      <p className="text-2xl font-bold tracking-tight">{value}</p>
    </CardContent></Card>
  )
}

/* ── Hints management ── */
function Hints() {
  const { user } = useAuth()
  const [filter, setFilter] = useState('')
  const [busyId, setBusyId] = useState<number | null>(null)
  const [actErr, setActErr] = useState('')
  const [detailId, setDetailId] = useState<number | null>(null)
  const [adding, setAdding] = useState(false)
  const [scopeFilter, setScopeFilter] = useState('')
  const [domainFilter, setDomainFilter] = useState('')
  const [search, setSearch] = useState('')
  // Debounce text inputs so we don't refetch per keystroke
  const [applied, setApplied] = useState({ domain: '', search: '' })
  useEffect(() => {
    const t = setTimeout(() => setApplied({ domain: domainFilter.trim(), search: search.trim() }), 350)
    return () => clearTimeout(t)
  }, [domainFilter, search])
  const path = `/api/learning/hints?limit=100${filter ? `&status=${filter}` : ''}` +
    (scopeFilter ? `&scope=${scopeFilter}` : '') +
    (applied.domain ? `&domain=${encodeURIComponent(applied.domain)}` : '') +
    (applied.search ? `&search=${encodeURIComponent(applied.search)}` : '')
  const { data, loading, error, reload } = useFetch<HintsResp>(path)

  async function act(id: number, action: 'unflag' | 'retract' | 'reactivate') {
    if (action === 'retract' && !window.confirm('Retract this hint? It will stop injecting into agent prompts.')) return
    setBusyId(id); setActErr('')
    try {
      await api(`/api/learning/hints/${id}/${action}`, {
        method: 'POST',
        body: JSON.stringify({ actor: user?.email || 'admin', reason: 'via admin dashboard' }),
      })
      await reload()
    } catch (e) {
      setActErr(e instanceof Error ? e.message : 'Action failed')
    } finally {
      setBusyId(null)
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {HINT_FILTERS.map(f => (
          <Button key={f.key} size="sm" variant={filter === f.key ? 'default' : 'outline'} className="h-7 text-xs" onClick={() => setFilter(f.key)}>
            {f.label}
          </Button>
        ))}
        <select
          className="h-7 rounded-md border border-input bg-background px-1.5 text-xs"
          value={scopeFilter}
          onChange={e => setScopeFilter(e.target.value)}
        >
          <option value="">Scope: all</option>
          <option value="url">url</option>
          <option value="domain">domain</option>
          <option value="global">global</option>
        </select>
        <input
          className="h-7 w-32 rounded-md border border-input bg-background px-2 text-xs"
          placeholder="Domain filter…"
          value={domainFilter}
          onChange={e => setDomainFilter(e.target.value)}
        />
        <input
          className="h-7 w-40 rounded-md border border-input bg-background px-2 text-xs"
          placeholder="Search hint text…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <span className="ml-auto self-center text-xs text-muted-foreground">{data ? `${data.total} hint${data.total !== 1 ? 's' : ''}` : ''}</span>
        <Button size="sm" className="h-7 gap-1 text-xs" onClick={() => setAdding(true)}>
          <Plus className="h-3 w-3" /> Add feedback
        </Button>
      </div>
      {actErr && <ErrorNote msg={actErr} />}
      {error && <ErrorNote msg={error} />}
      <Card>
        <CardContent className="p-0">
          {loading && <p className="px-4 py-6 text-sm text-muted-foreground">Loading…</p>}
          {data && data.hints.length === 0 && <p className="px-4 py-6 text-sm text-muted-foreground">No hints match this filter.</p>}
          {data && data.hints.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                    <th className="px-4 py-2.5">Hint</th>
                    <th className="px-4 py-2.5 hidden md:table-cell">Scope</th>
                    <th className="px-4 py-2.5">Status</th>
                    <th className="px-4 py-2.5 hidden sm:table-cell">OK / Fail / Applied</th>
                    <th className="px-4 py-2.5"></th>
                  </tr>
                </thead>
                <tbody>
                  {data.hints.map(h => {
                    const st = hintStatus(h)
                    const disabled = busyId === h.id
                    return (
                      <tr key={h.id} className="border-b last:border-0 align-top hover:bg-muted/30">
                        <td className="px-4 py-3 max-w-md">
                          <span className="line-clamp-2">{h.feedback_text}</span>
                          {h.domain && <span className="mt-0.5 block text-xs text-muted-foreground">{h.domain}</span>}
                          {h.conflict_flagged === 1 && h.conflict_flag_reason && (
                            <span className="mt-0.5 block text-xs italic text-amber-700 line-clamp-2">⚠ {h.conflict_flag_reason}</span>
                          )}
                        </td>
                        <td className="px-4 py-3 hidden md:table-cell text-xs text-muted-foreground">{h.scope || '—'}</td>
                        <td className="px-4 py-3"><Badge className={cn('text-xs', st.cls)}>{st.label}</Badge></td>
                        <td className="px-4 py-3 hidden sm:table-cell text-xs text-muted-foreground whitespace-nowrap">
                          {(h.success_count ?? 0)} / {(h.failure_count ?? 0)} / {(h.applied_count ?? 0)}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex justify-end gap-1.5">
                            <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => setDetailId(h.id)}>Detail</Button>
                            {h.is_active === 1 && h.conflict_flagged === 1 && (
                              <Button size="sm" variant="outline" className="h-7 text-xs" disabled={disabled} onClick={() => act(h.id, 'unflag')}>Unflag</Button>
                            )}
                            {h.is_active === 1 && (
                              <Button size="sm" variant="outline" className="h-7 text-xs text-destructive" disabled={disabled} onClick={() => act(h.id, 'retract')}>Retract</Button>
                            )}
                            {h.is_active === 0 && (
                              <Button size="sm" variant="outline" className="h-7 text-xs" disabled={disabled} onClick={() => act(h.id, 'reactivate')}>Reactivate</Button>
                            )}
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      {detailId != null && (
        <HintDrawer id={detailId} onChanged={reload} onClose={() => setDetailId(null)} />
      )}
      {adding && (
        <AddFeedbackSheet onCreated={reload} onClose={() => setAdding(false)} />
      )}
    </div>
  )
}

/* ── Runs ── */
function Runs() {
  const [status, setStatus] = useState('')
  const [search, setSearch] = useState('')
  const [applied, setApplied] = useState('')
  const [openRun, setOpenRun] = useState<string | null>(null)
  useEffect(() => {
    const t = setTimeout(() => setApplied(search.trim()), 350)
    return () => clearTimeout(t)
  }, [search])
  const path = `/api/learning/runs?limit=50${status ? `&status=${status}` : ''}${applied ? `&q=${encodeURIComponent(applied)}` : ''}`
  const { data, loading, error } = useFetch<RunsResp>(path)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        <input
          className="h-7 w-48 rounded-md border border-input bg-background px-2 text-xs"
          placeholder="Search query…"
          value={search}
          onChange={e => setSearch(e.target.value)}
        />
        <select
          className="h-7 rounded-md border border-input bg-background px-1.5 text-xs"
          value={status}
          onChange={e => setStatus(e.target.value)}
        >
          <option value="">All statuses</option>
          <option value="passed">passed</option>
          <option value="failed">failed</option>
        </select>
        <span className="ml-auto self-center text-xs text-muted-foreground">
          {data ? `${data.total} run${data.total !== 1 ? 's' : ''} · click a row for its learning journey` : ''}
        </span>
      </div>
      {error && <ErrorNote msg={error} />}
      <Card>
        <CardContent className="p-0">
          {loading && !data && <p className="px-4 py-6 text-sm text-muted-foreground">Loading…</p>}
          {data && data.runs.length === 0 && <p className="px-4 py-6 text-sm text-muted-foreground">No runs match.</p>}
          {data && data.runs.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                    <th className="px-4 py-2.5">Query</th>
                    <th className="px-4 py-2.5">Status</th>
                    <th className="px-4 py-2.5 hidden md:table-cell">Hints used</th>
                    {/* Full id (no truncation) so a workflow can be found with Ctrl+F */}
                    <th className="px-4 py-2.5 hidden md:table-cell">Workflow</th>
                    <th className="px-4 py-2.5 hidden lg:table-cell">When</th>
                  </tr>
                </thead>
                <tbody>
                  {data.runs.map((r, i) => (
                    <tr
                      key={r.workflow_id + i}
                      className="cursor-pointer border-b last:border-0 hover:bg-muted/30"
                      onClick={() => setOpenRun(r.workflow_id)}
                    >
                      <td className="px-4 py-2.5 max-w-md"><span className="line-clamp-1">{r.user_query || '(paste-and-execute)'}</span></td>
                      <td className="px-4 py-2.5 whitespace-nowrap">
                        <Badge className={cn('text-xs',
                          r.test_status === 'passed' ? 'bg-green-100 text-green-700 border-green-200'
                            : r.test_status === 'failed' ? 'bg-red-100 text-red-700 border-red-200'
                              : 'bg-muted text-muted-foreground')}>{r.test_status}</Badge>
                        {r.failure_category && <span className="ml-1.5 text-xs text-muted-foreground">({r.failure_category})</span>}
                      </td>
                      <td className="px-4 py-2.5 hidden md:table-cell text-muted-foreground">{r.nl_injected_count ?? 0}</td>
                      <td className="px-4 py-2.5 hidden md:table-cell whitespace-nowrap"><code className="text-xs text-muted-foreground">{r.workflow_id}</code></td>
                      <td className="px-4 py-2.5 hidden lg:table-cell text-xs text-muted-foreground whitespace-nowrap">
                        {r.timestamp ? new Date(r.timestamp).toLocaleString() : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      {openRun && <RunDrawer workflowId={openRun} onClose={() => setOpenRun(null)} />}
    </div>
  )
}

/* ── Review (LLM batch hint review) ── */
function sessionStatus(s: string): { label: string; cls: string } {
  if (s === 'pending_llm') return { label: 'Running…', cls: 'bg-amber-100 text-amber-700 border-amber-200' }
  if (s === 'pending_review') return { label: 'Awaiting review', cls: 'bg-blue-100 text-blue-700 border-blue-200' }
  if (s === 'failed') return { label: 'Failed', cls: 'bg-red-100 text-red-700 border-red-200' }
  if (s === 'completed' || s === 'applied') return { label: 'Applied', cls: 'bg-green-100 text-green-700 border-green-200' }
  return { label: s, cls: 'bg-muted text-muted-foreground' }
}

const REC_BADGES: Record<string, { label: string; cls: string }> = {
  disable: { label: 'Disable', cls: 'bg-red-100 text-red-700 border-red-200' },
  reactivate: { label: 'Reactivate', cls: 'bg-green-100 text-green-700 border-green-200' },
  unflag: { label: 'Unflag', cls: 'bg-blue-100 text-blue-700 border-blue-200' },
  keep: { label: 'Keep', cls: 'bg-muted text-muted-foreground' },
  flag_review: { label: 'Flag for review', cls: 'bg-amber-100 text-amber-700 border-amber-200' },
}

function Review() {
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [starting, setStarting] = useState(false)
  const [startErr, setStartErr] = useState('')
  const { data, loading, error, reload } = useFetch<ReviewSessionsResp>('/api/learning/review-hints/sessions')

  // Poll while the LLM is running so pending_llm flips to pending_review.
  useEffect(() => {
    if (!data?.is_any_running) return
    const t = setInterval(reload, 4000)
    return () => clearInterval(t)
  }, [data?.is_any_running, reload])

  async function startReview() {
    setStarting(true); setStartErr('')
    try {
      const resp = await api<{ session_id: number }>('/api/learning/review-hints/start', { method: 'POST' })
      setSelectedId(resp.session_id)
      await reload()
    } catch (e) {
      setStartErr(e instanceof Error ? e.message : 'Failed to start review')
    } finally {
      setStarting(false)
    }
  }

  const selected = data?.sessions.find(s => s.id === selectedId)

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3">
        <Button size="sm" className="h-7 text-xs" disabled={starting || !!data?.is_any_running} onClick={startReview}>
          {data?.is_any_running ? 'Review in progress…' : starting ? 'Starting…' : 'Start review'}
        </Button>
        <span className="text-xs text-muted-foreground">Runs an LLM batch review of all learned hints.</span>
      </div>
      {startErr && <ErrorNote msg={startErr} />}
      {error && <ErrorNote msg={error} />}
      <Card>
        <CardContent className="p-0">
          {loading && !data && <p className="px-4 py-6 text-sm text-muted-foreground">Loading…</p>}
          {data && data.sessions.length === 0 && <p className="px-4 py-6 text-sm text-muted-foreground">No review sessions yet.</p>}
          {data && data.sessions.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                    <th className="px-4 py-2.5">Started</th>
                    <th className="px-4 py-2.5">Status</th>
                    <th className="px-4 py-2.5 hidden sm:table-cell">Hints</th>
                    <th className="px-4 py-2.5 hidden md:table-cell">LLM latency</th>
                    <th className="px-4 py-2.5 hidden md:table-cell">Warning</th>
                    <th className="px-4 py-2.5 hidden lg:table-cell">Completed</th>
                    <th className="px-4 py-2.5"></th>
                  </tr>
                </thead>
                <tbody>
                  {data.sessions.map(s => {
                    const st = sessionStatus(s.status)
                    return (
                      <tr
                        key={s.id}
                        className={cn('cursor-pointer border-b last:border-0 hover:bg-muted/30', selectedId === s.id && 'bg-muted/40')}
                        onClick={() => setSelectedId(s.id)}
                      >
                        <td className="px-4 py-2.5 text-xs text-muted-foreground whitespace-nowrap">
                          {s.created_at ? new Date(s.created_at).toLocaleString() : '—'}
                        </td>
                        <td className="px-4 py-2.5"><Badge className={cn('text-xs', st.cls)}>{st.label}</Badge></td>
                        <td className="px-4 py-2.5 hidden sm:table-cell text-muted-foreground">{s.hint_count}</td>
                        <td className="px-4 py-2.5 hidden md:table-cell text-xs tabular-nums text-muted-foreground">
                          {s.llm_latency_ms != null ? `${(s.llm_latency_ms / 1000).toFixed(1)}s` : '—'}
                        </td>
                        <td className="px-4 py-2.5 hidden md:table-cell text-xs">
                          {s.warning ? <span className="text-amber-700">⚠ Yes</span> : <span className="text-muted-foreground">—</span>}
                        </td>
                        <td className="px-4 py-2.5 hidden lg:table-cell text-xs text-muted-foreground whitespace-nowrap">
                          {s.completed_at ? new Date(s.completed_at).toLocaleString() : '—'}
                        </td>
                        <td className="px-4 py-2.5 text-right text-xs text-muted-foreground">{selectedId === s.id ? 'Viewing' : 'View'}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      {selectedId != null && (
        <ReviewSessionPanel id={selectedId} listStatus={selected?.status} onSessionsChanged={reload} />
      )}
    </div>
  )
}

function ReviewSessionPanel({ id, listStatus, onSessionsChanged }: {
  id: number
  listStatus?: string
  onSessionsChanged: () => void
}) {
  const [busyId, setBusyId] = useState<number | null>(null)
  const [applying, setApplying] = useState(false)
  const [actErr, setActErr] = useState('')
  const [appliedMsg, setAppliedMsg] = useState('')
  const { data, loading, error, reload } = useFetch<ReviewSessionDetail>(`/api/learning/review-hints/sessions/${id}`)

  // Refetch the detail when the polled list reports a status change
  // (pending_llm → pending_review). Skip the initial render: useFetch
  // already loads on mount.
  const prevStatus = useRef(listStatus)
  useEffect(() => {
    if (prevStatus.current !== listStatus) {
      prevStatus.current = listStatus
      reload()
    }
  }, [listStatus, reload])

  async function decide(recId: number, decision: 'approved' | 'rejected') {
    // Optional rationale — stored as admin_notes and written into the hint's
    // audit trail when the session is applied (legacy-UI parity).
    const notes = window.prompt(
      decision === 'approved'
        ? 'Optional note — why you agree with this recommendation (leave blank to skip):'
        : 'Optional note — why you are overriding this recommendation (leave blank to skip):',
    )
    setBusyId(recId); setActErr(''); setAppliedMsg('')
    try {
      await api(`/api/learning/review-hints/sessions/${id}/recommendations/${recId}`, {
        method: 'PATCH',
        body: JSON.stringify({ admin_decision: decision, admin_notes: notes || null }),
      })
      await reload()
    } catch (e) {
      setActErr(e instanceof Error ? e.message : 'Failed to save decision')
    } finally {
      setBusyId(null)
    }
  }

  async function applyApproved() {
    const n = recommendations.filter(r => r.admin_decision === 'approved' && r.applied === 0).length
    if (!window.confirm(`Apply ${n} approved change${n !== 1 ? 's' : ''}? This updates the live hints.`)) return
    setApplying(true); setActErr(''); setAppliedMsg('')
    try {
      const resp = await api<{ applied_count: number }>(`/api/learning/review-hints/sessions/${id}/apply`, { method: 'POST' })
      setAppliedMsg(`Applied ${resp.applied_count} recommendation${resp.applied_count !== 1 ? 's' : ''}.`)
      await reload()
      onSessionsChanged()
    } catch (e) {
      setActErr(e instanceof Error ? e.message : 'Failed to apply')
    } finally {
      setApplying(false)
    }
  }

  if (loading && !data) return <p className="text-sm text-muted-foreground">Loading session…</p>
  if (error) return <ErrorNote msg={error} />
  if (!data) return null

  const { session, recommendations } = data
  const reviewable = session.status === 'pending_review'
  const approvedPending = recommendations.filter(r => r.admin_decision === 'approved' && r.applied === 0).length
  const decided = recommendations.filter(r => r.admin_decision != null).length

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-3">
        <h2 className="text-sm font-semibold">Session #{session.id}</h2>
        <Badge className={cn('text-xs', sessionStatus(session.status).cls)}>{sessionStatus(session.status).label}</Badge>
        <span className="text-xs text-muted-foreground">{decided} of {recommendations.length} decided</span>
        {reviewable && (
          <Button size="sm" className="ml-auto h-7 text-xs" disabled={applying || approvedPending === 0} onClick={applyApproved}>
            {applying ? 'Applying…' : `Apply approved (${approvedPending})`}
          </Button>
        )}
      </div>
      {session.warning && <ErrorNote msg={session.warning} />}
      {session.status === 'failed' && session.error_message && <ErrorNote msg={session.error_message} />}
      {actErr && <ErrorNote msg={actErr} />}
      {appliedMsg && <div className="rounded-md border border-green-200 bg-green-50 px-4 py-2 text-sm text-green-700">{appliedMsg}</div>}
      {session.status === 'pending_llm' && (
        <p className="text-sm text-muted-foreground">The LLM review is running — recommendations will appear here when it finishes.</p>
      )}
      {/* Per-scope chunk progress (one LLM page per domain / global scope) */}
      {(data.pages?.length ?? 0) > 0 && (
        <div className="rounded-md border bg-muted/30 px-3 py-2 text-xs space-y-0.5">
          {data.pages!.map(p => {
            const label = reviewPageLabel(p)
            const icon = p.status === 'succeeded' ? '✅' : p.status === 'failed' ? '❌' : session.status === 'pending_llm' ? '⟳' : '⏳'
            const note = p.status === 'succeeded' ? `${p.hint_count ?? 0} hints reviewed`
              : p.status === 'failed' ? (p.error_message || 'Failed')
                : session.status === 'pending_llm' ? 'In progress…' : 'Queued'
            return (
              <p key={p.id}>{icon} <span className="font-medium">{label}</span> — <span className="text-muted-foreground">{note}</span></p>
            )
          })}
        </div>
      )}
      {recommendations.length === 0 && session.status !== 'pending_llm' && (
        <p className="text-sm text-muted-foreground">No recommendations in this session.</p>
      )}
      {recommendations.map(r => {
        const rec = REC_BADGES[r.recommendation] ?? { label: r.recommendation, cls: 'bg-muted text-muted-foreground' }
        const busy = busyId === r.id
        return (
          <Card key={r.id}>
            <CardContent className="space-y-2 p-4">
              <div className="flex flex-wrap items-start gap-2">
                <div className="min-w-0 flex-1">
                  <p className="text-sm">{r.feedback_text}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {[r.scope, r.domain].filter(Boolean).join(' · ') || '—'}
                    {' · '}{r.success_count ?? 0} ok / {r.failure_count ?? 0} fail / {r.applied_count ?? 0} applied
                    {' · '}{r.exoneration_count} exoneration{r.exoneration_count !== 1 ? 's' : ''}
                  </p>
                </div>
                <Badge className={cn('text-xs', rec.cls)}>{rec.label}</Badge>
              </div>
              <p className="rounded-md bg-muted/40 px-3 py-2 text-xs text-muted-foreground">{r.reason}</p>
              {r.admin_notes?.trim() && (
                <p className="text-xs text-muted-foreground">📝 {r.admin_notes}</p>
              )}
              <div className="flex items-center gap-1.5">
                {r.applied === 1 ? (
                  <span className="text-xs font-medium text-green-700">✓ Applied</span>
                ) : reviewable ? (
                  <>
                    <Button
                      size="sm"
                      variant={r.admin_decision === 'approved' ? 'default' : 'outline'}
                      className="h-7 text-xs"
                      disabled={busy}
                      onClick={() => decide(r.id, 'approved')}
                    >Approve</Button>
                    <Button
                      size="sm"
                      variant={r.admin_decision === 'rejected' ? 'default' : 'outline'}
                      className="h-7 text-xs text-destructive"
                      disabled={busy}
                      onClick={() => decide(r.id, 'rejected')}
                    >Reject</Button>
                  </>
                ) : r.admin_decision ? (
                  <span className="text-xs text-muted-foreground">Decision: {r.admin_decision}</span>
                ) : (
                  <span className="text-xs text-muted-foreground">No decision</span>
                )}
              </div>
            </CardContent>
          </Card>
        )
      })}
    </div>
  )
}

const VIEWS: { key: View; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'hints', label: 'Hints' },
  { key: 'triggers', label: 'Triggers' },
  { key: 'runs', label: 'Runs' },
  { key: 'stats', label: 'Stats' },
  { key: 'review', label: 'LLM Review' },
]

export default function LearningPage() {
  const [view, setView] = useState<View>('overview')
  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Learning</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">Review learned hints, runs, and the adaptive-learning system</p>
        </div>
      </div>
      <HealthBanner />
      <div className="flex gap-1.5 border-b pb-2">
        {VIEWS.map(v => (
          <Button key={v.key} size="sm" variant={view === v.key ? 'default' : 'ghost'} className="h-7 text-xs" onClick={() => setView(v.key)}>
            {v.label}
          </Button>
        ))}
      </div>
      {view === 'overview' && <Overview />}
      {view === 'hints' && <Hints />}
      {view === 'triggers' && <TriggersTab />}
      {view === 'runs' && <Runs />}
      {view === 'stats' && <StatsTab />}
      {view === 'review' && <Review />}
    </div>
  )
}
