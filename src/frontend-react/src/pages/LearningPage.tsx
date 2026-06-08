import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useFetch } from '@/lib/useFetch'
import { api } from '@/lib/api'
import { useAuth } from '@/auth/AuthContext'
import { cn } from '@/lib/utils'

/* ── Shapes (only the fields we render) ── */
interface Hint {
  id: number
  feedback_text: string
  scope?: string
  domain?: string
  is_active: number
  conflict_flagged: number
  llm_review_disabled?: number
  success_count?: number
  applied_count?: number
  last_seen?: string
}
interface HintsResp { total: number; hints: Hint[] }
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

type View = 'overview' | 'hints' | 'runs'
const HINT_FILTERS = [
  { key: '', label: 'All' },
  { key: 'flagged', label: 'Flagged' },
  { key: 'active', label: 'Active' },
  { key: 'auto_disabled', label: 'Auto-disabled' },
  { key: 'retracted', label: 'Retracted' },
] as const

const pctOrDash = (n: number | null | undefined) => (n == null ? '—' : `${(n * 100).toFixed(1)}%`)

function hintStatus(h: Hint): { label: string; cls: string } {
  if (h.is_active === 1 && h.conflict_flagged === 1) return { label: 'Flagged', cls: 'bg-amber-100 text-amber-700 border-amber-200' }
  if (h.is_active === 1) return { label: 'Active', cls: 'bg-green-100 text-green-700 border-green-200' }
  if (h.llm_review_disabled === 1) return { label: 'LLM-disabled', cls: 'bg-red-100 text-red-700 border-red-200' }
  return { label: 'Disabled', cls: 'bg-muted text-muted-foreground' }
}

function ErrorNote({ msg }: { msg: string }) {
  return <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">{msg}</div>
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
  const path = `/api/learning/hints?limit=100${filter ? `&status=${filter}` : ''}`
  const { data, loading, error, reload } = useFetch<HintsResp>(path)

  async function act(id: number, action: 'unflag' | 'retract' | 'reactivate') {
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
      <div className="flex flex-wrap gap-1.5">
        {HINT_FILTERS.map(f => (
          <Button key={f.key} size="sm" variant={filter === f.key ? 'default' : 'outline'} className="h-7 text-xs" onClick={() => setFilter(f.key)}>
            {f.label}
          </Button>
        ))}
        <span className="ml-auto self-center text-xs text-muted-foreground">{data ? `${data.total} hint${data.total !== 1 ? 's' : ''}` : ''}</span>
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
                    <th className="px-4 py-2.5 hidden sm:table-cell">Success/Applied</th>
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
                        </td>
                        <td className="px-4 py-3 hidden md:table-cell text-xs text-muted-foreground">{h.scope || '—'}</td>
                        <td className="px-4 py-3"><Badge className={cn('text-xs', st.cls)}>{st.label}</Badge></td>
                        <td className="px-4 py-3 hidden sm:table-cell text-xs text-muted-foreground whitespace-nowrap">
                          {(h.success_count ?? 0)} / {(h.applied_count ?? 0)}
                        </td>
                        <td className="px-4 py-3">
                          <div className="flex justify-end gap-1.5">
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
    </div>
  )
}

/* ── Runs ── */
function Runs() {
  const { data, loading, error } = useFetch<RunsResp>('/api/learning/runs?limit=50')
  if (loading) return <p className="text-sm text-muted-foreground">Loading…</p>
  if (error) return <ErrorNote msg={error} />
  return (
    <Card>
      <CardContent className="p-0">
        {data && data.runs.length === 0 && <p className="px-4 py-6 text-sm text-muted-foreground">No runs recorded yet.</p>}
        {data && data.runs.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                  <th className="px-4 py-2.5">Query</th>
                  <th className="px-4 py-2.5">Status</th>
                  <th className="px-4 py-2.5 hidden md:table-cell">Hints used</th>
                  <th className="px-4 py-2.5 hidden lg:table-cell">When</th>
                </tr>
              </thead>
              <tbody>
                {data.runs.map((r, i) => (
                  <tr key={r.workflow_id + i} className="border-b last:border-0 hover:bg-muted/30">
                    <td className="px-4 py-2.5 max-w-md"><span className="line-clamp-1">{r.user_query || '—'}</span></td>
                    <td className="px-4 py-2.5">
                      <Badge className={cn('text-xs',
                        r.test_status === 'passed' ? 'bg-green-100 text-green-700 border-green-200'
                          : r.test_status === 'failed' ? 'bg-red-100 text-red-700 border-red-200'
                            : 'bg-muted text-muted-foreground')}>{r.test_status}</Badge>
                    </td>
                    <td className="px-4 py-2.5 hidden md:table-cell text-muted-foreground">{r.nl_injected_count ?? 0}</td>
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
  )
}

const VIEWS: { key: View; label: string }[] = [
  { key: 'overview', label: 'Overview' },
  { key: 'hints', label: 'Hints' },
  { key: 'runs', label: 'Runs' },
]

export default function LearningPage() {
  const [view, setView] = useState<View>('overview')
  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Learning</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">Review learned hints, runs, and the adaptive-learning system</p>
        </div>
      </div>
      <div className="flex gap-1.5 border-b pb-2">
        {VIEWS.map(v => (
          <Button key={v.key} size="sm" variant={view === v.key ? 'default' : 'ghost'} className="h-7 text-xs" onClick={() => setView(v.key)}>
            {v.label}
          </Button>
        ))}
      </div>
      {view === 'overview' && <Overview />}
      {view === 'hints' && <Hints />}
      {view === 'runs' && <Runs />}
    </div>
  )
}
