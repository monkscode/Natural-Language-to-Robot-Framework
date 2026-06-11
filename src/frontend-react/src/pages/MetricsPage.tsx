import { Fragment, useState } from 'react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { useFetch } from '@/lib/useFetch'
import { cn } from '@/lib/utils'
import { RefreshCw, Activity, ChevronRight } from 'lucide-react'
import { PerformanceChartCard, CostChartCard } from './metrics/MetricsCharts'
import type { MetricsRow } from './metrics/MetricsCharts'

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
const WINDOWS = [
  { key: 'last_24_hours', label: '24h', hours: 24 },
  { key: 'last_7_days', label: '7d', hours: 24 * 7 },
  { key: 'last_30_days', label: '30d', hours: 24 * 30 },
  { key: 'all_time', label: 'All time', hours: null },
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

function statusOf(r: MetricsRow): { label: string; cls: string } {
  if ((r.success_rate ?? 0) >= 0.999) return { label: 'PASS', cls: 'bg-green-100 text-green-700 border-green-200' }
  if ((r.success_rate ?? 0) > 0) return { label: 'PARTIAL', cls: 'bg-amber-100 text-amber-700 border-amber-200' }
  return { label: 'FAIL', cls: 'bg-red-100 text-red-700 border-red-200' }
}

function DetailCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border bg-background px-3 py-2">
      <p className="text-[11px] text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-xs font-medium tabular-nums">{value}</p>
    </div>
  )
}

export default function MetricsPage() {
  const [win, setWin] = useState<WindowKey>('last_7_days')
  const [openRow, setOpenRow] = useState<string | null>(null)
  const summary = useFetch<Summary>('/api/workflow-metrics/summary')
  const learning = useFetch<LearningHealth>('/api/workflow-metrics/learning-health')
  const recent = useFetch<MetricsRow[]>('/api/workflow-metrics/?limit=200')

  const agg = summary.data?.[win]
  const hours = WINDOWS.find(w => w.key === win)?.hours ?? null
  const cutoff = hours == null ? 0 : Date.now() - hours * 3600_000
  const windowRows = (recent.data ?? []).filter(r => !cutoff || new Date(r.timestamp).getTime() >= cutoff)

  function reloadAll() {
    summary.reload(); learning.reload(); recent.reload()
  }

  return (
    <div className="mx-auto w-full max-w-7xl space-y-4">
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
              {/* summary avg_success_rate is already 0–100 (unlike per-row 0–1) */}
              <StatCard label="Success rate" value={`${(agg.avg_success_rate ?? 0).toFixed(1)}%`} sub={`${agg.successful_elements}/${agg.total_elements} elements`} />
              <StatCard label="Total cost" value={money(agg.total_cost)} sub={`${money(agg.avg_cost_per_element)}/element`} />
              <StatCard label="Avg exec time" value={secs(agg.avg_execution_time)} sub={`${agg.total_llm_calls} LLM calls`} />
            </>
          )}
      </div>

      {/* Charts — performance over time + cost breakdowns (window-filtered) */}
      <div className="grid grid-cols-1 gap-4 xl:grid-cols-5">
        <div className="xl:col-span-3"><PerformanceChartCard rows={windowRows} /></div>
        <div className="xl:col-span-2"><CostChartCard rows={windowRows} /></div>
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
                    <th className="w-8 px-2 py-2.5"></th>
                    <th className="px-2 py-2.5">Status</th>
                    <th className="px-4 py-2.5 hidden xl:table-cell">Workflow</th>
                    <th className="px-4 py-2.5">URL / Task</th>
                    <th className="px-4 py-2.5 hidden md:table-cell">LLM calls</th>
                    <th className="px-4 py-2.5 hidden sm:table-cell">Elements</th>
                    <th className="px-4 py-2.5">Cost</th>
                    <th className="px-4 py-2.5 hidden sm:table-cell">Time</th>
                    <th className="px-4 py-2.5 hidden lg:table-cell">When</th>
                  </tr>
                </thead>
                <tbody>
                  {recent.data.slice(0, 25).map((r, i) => {
                    const st = statusOf(r)
                    const open = openRow === r.workflow_id
                    return (
                      <Fragment key={r.workflow_id + i}>
                        <tr
                          className="cursor-pointer border-b last:border-0 hover:bg-muted/30"
                          onClick={() => setOpenRow(open ? null : r.workflow_id)}
                        >
                          <td className="px-2 py-2.5">
                            <ChevronRight className={cn('h-3.5 w-3.5 text-muted-foreground transition-transform', open && 'rotate-90')} />
                          </td>
                          <td className="px-2 py-2.5"><Badge className={cn('text-[10px]', st.cls)}>{st.label}</Badge></td>
                          <td className="px-4 py-2.5 hidden xl:table-cell"><code className="text-xs text-muted-foreground">{r.workflow_id.slice(0, 13)}…</code></td>
                          <td className="px-4 py-2.5 max-w-xs"><span className="line-clamp-1 font-mono text-xs">{r.url || '—'}</span></td>
                          <td className="px-4 py-2.5 hidden md:table-cell text-muted-foreground">{r.total_llm_calls}</td>
                          <td className="px-4 py-2.5 hidden sm:table-cell text-muted-foreground">{r.total_elements ?? '—'}</td>
                          <td className="px-4 py-2.5 tabular-nums">{money(r.total_cost)}</td>
                          <td className="px-4 py-2.5 hidden sm:table-cell text-muted-foreground tabular-nums">{secs(r.execution_time)}</td>
                          <td className="px-4 py-2.5 hidden lg:table-cell text-xs text-muted-foreground whitespace-nowrap">
                            {r.timestamp ? new Date(r.timestamp).toLocaleString() : '—'}
                          </td>
                        </tr>
                        {open && (
                          <tr className="border-b bg-muted/20 last:border-0">
                            <td colSpan={9} className="px-6 py-3">
                              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                                <DetailCell label="Workflow ID" value={r.workflow_id} />
                                <DetailCell label="Element success" value={`${r.successful_elements ?? 0} ok / ${r.failed_elements ?? 0} failed (${pct(r.success_rate)})`} />
                                <DetailCell label="CrewAI" value={`${r.crewai_llm_calls ?? 0} calls · ${money(r.crewai_cost ?? 0)}`} />
                                <DetailCell label="Browser actions" value={`${r.browser_use_llm_calls ?? 0} calls · ${money(r.browser_use_cost ?? 0)}`} />
                                <DetailCell label="CrewAI tokens" value={`${(r.crewai_prompt_tokens ?? 0).toLocaleString()} in / ${(r.crewai_completion_tokens ?? 0).toLocaleString()} out`} />
                                <DetailCell label="Browser tokens" value={`${(r.browser_use_prompt_tokens ?? 0).toLocaleString()} in / ${(r.browser_use_completion_tokens ?? 0).toLocaleString()} out`} />
                                <DetailCell label="Execution time" value={secs(r.execution_time)} />
                                <DetailCell label="Total cost" value={money(r.total_cost)} />
                                {/* Optimization-system stats (legacy detail-panel parity) */}
                                <DetailCell
                                  label="LLM cleaning"
                                  value={r.llm_cleaning_stats?.total_responses != null
                                    ? `${r.llm_cleaning_stats.cleaned_responses ?? 0}/${r.llm_cleaning_stats.total_responses} cleaned (${(r.llm_cleaning_stats.clean_rate ?? 0).toFixed(1)}%)`
                                    : 'N/A'}
                                />
                                <DetailCell
                                  label="Formatting errors"
                                  value={r.llm_cleaning_stats?.formatting_errors_detected != null
                                    ? String(r.llm_cleaning_stats.formatting_errors_detected)
                                    : 'N/A'}
                                />
                                <DetailCell
                                  label="Context reduction"
                                  value={r.context_reduction?.baseline_tokens
                                    ? `${(r.context_reduction.baseline_tokens ?? 0).toLocaleString()} → ${(r.context_reduction.optimized_tokens ?? 0).toLocaleString()} (−${(r.context_reduction.reduction_percentage ?? 0).toFixed(1)}%)`
                                    : 'N/A'}
                                />
                                <DetailCell
                                  label="Pattern learning"
                                  value={r.keyword_search_stats?.calls != null
                                    ? `${r.keyword_search_stats.calls} searches · ${(r.keyword_search_stats.avg_latency_ms ?? 0).toFixed(1)}ms · ${r.pattern_learning_stats?.prediction_used ? `used (${r.pattern_learning_stats?.predicted_keywords_count ?? 0} kw)` : 'not used'}`
                                    : 'N/A'}
                                />
                              </div>
                            </td>
                          </tr>
                        )}
                      </Fragment>
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

function SkeletonCard() {
  return <Card><CardContent className="p-5"><div className="h-3 w-16 rounded bg-muted mb-3" /><div className="h-8 w-20 rounded bg-muted" /></CardContent></Card>
}
function ErrorNote({ msg }: { msg: string }) {
  return <div className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">{msg}</div>
}
