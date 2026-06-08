import { useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useFetch } from '@/lib/useFetch'
import { cn } from '@/lib/utils'
import { RefreshCw, Activity } from 'lucide-react'

/* ── API shapes ── */
interface Aggregate {
  total_workflows: number
  total_elements: number
  successful_elements: number
  failed_elements: number
  avg_success_rate: number
  total_llm_calls: number
  avg_llm_calls_per_element: number
  total_cost: number
  avg_cost_per_element: number
  custom_action_usage_rate: number
  avg_execution_time: number
}
interface Summary {
  last_24_hours: Aggregate
  last_7_days: Aggregate
  last_30_days: Aggregate
  all_time: Aggregate
}
interface LearningHealth {
  status: string
  total_rules: number
  total_executions: number
  structural_rules?: number
  anti_patterns?: number
  keyword_corrections?: number
  contradictions?: number
  circuit_breaker?: { is_open?: boolean; error_rate?: number; state?: string } | null
}
interface WorkflowRow {
  workflow_id: string
  url: string
  timestamp: string
  total_llm_calls: number
  total_cost: number
  execution_time: number
  success_rate: number
}

const WINDOWS = [
  { key: 'last_24_hours', label: '24h' },
  { key: 'last_7_days', label: '7d' },
  { key: 'last_30_days', label: '30d' },
  { key: 'all_time', label: 'All time' },
] as const
type WindowKey = (typeof WINDOWS)[number]['key']

const money = (n: number) => `$${(n ?? 0).toFixed(4)}`
const pct = (n: number) => `${((n ?? 0) * 100).toFixed(1)}%`
const secs = (n: number) => `${(n ?? 0).toFixed(1)}s`

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <Card>
      <CardContent className="p-5">
        <p className="text-xs font-medium text-muted-foreground mb-2">{label}</p>
        <p className="text-3xl font-bold tracking-tight leading-none">{value}</p>
        {sub && <p className="mt-2 text-xs text-muted-foreground">{sub}</p>}
      </CardContent>
    </Card>
  )
}

function LearningHealthCard({ h }: { h: LearningHealth }) {
  const tone =
    h.status === 'active' ? 'bg-green-100 text-green-700 border-green-200'
      : h.status === 'disabled' ? 'bg-muted text-muted-foreground'
        : 'bg-red-100 text-red-700 border-red-200'
  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="text-sm flex items-center gap-2"><Activity className="h-4 w-4" /> Learning System</CardTitle>
        <Badge className={cn('text-xs', tone)}>{h.status}</Badge>
      </CardHeader>
      <CardContent className="grid grid-cols-2 gap-x-6 gap-y-3 text-sm sm:grid-cols-3">
        <Metric label="Total rules" value={h.total_rules} />
        <Metric label="Executions" value={h.total_executions} />
        <Metric label="Structural" value={h.structural_rules ?? 0} />
        <Metric label="Anti-patterns" value={h.anti_patterns ?? 0} />
        <Metric label="Keyword fixes" value={h.keyword_corrections ?? 0} />
        <Metric label="Contradictions" value={h.contradictions ?? 0} />
      </CardContent>
    </Card>
  )
}

function Metric({ label, value }: { label: string; value: number | string }) {
  return (
    <div>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="text-lg font-semibold">{value}</p>
    </div>
  )
}

export default function MetricsPage() {
  const [win, setWin] = useState<WindowKey>('last_7_days')
  const summary = useFetch<Summary>('/api/workflow-metrics/summary')
  const learning = useFetch<LearningHealth>('/api/workflow-metrics/learning-health')
  const recent = useFetch<WorkflowRow[]>('/api/workflow-metrics/?limit=20')

  const agg = summary.data?.[win]

  function reloadAll() {
    summary.reload(); learning.reload(); recent.reload()
  }

  return (
    <div className="mx-auto max-w-5xl space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Metrics</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">Performance, cost, and learning insights across test runs</p>
        </div>
        <Button variant="outline" size="sm" className="gap-1.5" onClick={reloadAll}>
          <RefreshCw className="h-3.5 w-3.5" /> Refresh
        </Button>
      </div>

      {/* Window selector */}
      <div className="flex gap-1.5">
        {WINDOWS.map(w => (
          <Button key={w.key} size="sm" variant={win === w.key ? 'default' : 'outline'} className="h-7 text-xs" onClick={() => setWin(w.key)}>
            {w.label}
          </Button>
        ))}
      </div>

      {summary.error && <ErrorNote msg={summary.error} />}

      {/* Stat cards */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        {summary.loading && !agg
          ? Array.from({ length: 4 }).map((_, i) => <SkeletonCard key={i} />)
          : agg && (
            <>
              <StatCard label="Workflows" value={String(agg.total_workflows)} sub={`${agg.total_elements} elements`} />
              <StatCard label="Success rate" value={pct(agg.avg_success_rate)} sub={`${agg.successful_elements}/${agg.total_elements} elements`} />
              <StatCard label="Total cost" value={money(agg.total_cost)} sub={`${money(agg.avg_cost_per_element)}/element`} />
              <StatCard label="Avg exec time" value={secs(agg.avg_execution_time)} sub={`${agg.total_llm_calls} LLM calls`} />
            </>
          )}
      </div>

      {/* Learning health */}
      {learning.data && <LearningHealthCard h={learning.data} />}

      {/* Recent workflows */}
      <Card>
        <CardHeader className="pb-3"><CardTitle className="text-sm">Recent Workflows</CardTitle></CardHeader>
        <CardContent className="p-0">
          {recent.error && <div className="p-4"><ErrorNote msg={recent.error} /></div>}
          {recent.loading && <p className="px-4 py-6 text-sm text-muted-foreground">Loading…</p>}
          {recent.data && recent.data.length === 0 && (
            <p className="px-4 py-6 text-sm text-muted-foreground">No workflow metrics recorded yet.</p>
          )}
          {recent.data && recent.data.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                    <th className="px-4 py-2.5">URL</th>
                    <th className="px-4 py-2.5 hidden sm:table-cell">Success</th>
                    <th className="px-4 py-2.5 hidden md:table-cell">LLM calls</th>
                    <th className="px-4 py-2.5">Cost</th>
                    <th className="px-4 py-2.5 hidden sm:table-cell">Time</th>
                    <th className="px-4 py-2.5 hidden lg:table-cell">When</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.data.map((r, i) => (
                    <tr key={r.workflow_id + i} className="border-b last:border-0 hover:bg-muted/30">
                      <td className="px-4 py-2.5 max-w-xs"><span className="line-clamp-1 font-mono text-xs">{r.url || '—'}</span></td>
                      <td className="px-4 py-2.5 hidden sm:table-cell">{pct(r.success_rate)}</td>
                      <td className="px-4 py-2.5 hidden md:table-cell text-muted-foreground">{r.total_llm_calls}</td>
                      <td className="px-4 py-2.5">{money(r.total_cost)}</td>
                      <td className="px-4 py-2.5 hidden sm:table-cell text-muted-foreground">{secs(r.execution_time)}</td>
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
    </div>
  )
}

function SkeletonCard() {
  return <Card><CardContent className="p-5"><div className="h-3 w-16 rounded bg-muted mb-3" /><div className="h-8 w-20 rounded bg-muted" /></CardContent></Card>
}
function ErrorNote({ msg }: { msg: string }) {
  return <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">{msg}</div>
}
