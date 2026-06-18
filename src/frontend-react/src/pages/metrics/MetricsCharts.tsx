/**
 * Metrics charts — recharts ports of the legacy canvas charts
 * (src/frontend/metrics-charts.js). Two cards mirroring the legacy dropdowns:
 * Performance & Analysis (13 variants) and Cost & Advanced Analytics (9).
 * Every legacy chart option is represented; data comes straight off the
 * /api/workflow-metrics rows passed in.
 */

import { useState } from 'react'
import {
  Area, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, Line,
  LineChart, Pie, PieChart, PolarAngleAxis, PolarGrid, PolarRadiusAxis,
  Radar, RadarChart, ResponsiveContainer, Scatter, ScatterChart,
  Tooltip, XAxis, YAxis,
} from 'recharts'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'

export interface MetricsRow {
  workflow_id: string
  url: string
  timestamp: string
  total_llm_calls: number
  total_cost: number
  execution_time: number
  success_rate: number
  total_elements?: number
  successful_elements?: number
  failed_elements?: number
  avg_llm_calls_per_element?: number
  avg_cost_per_element?: number
  crewai_llm_calls?: number
  crewai_cost?: number
  crewai_prompt_tokens?: number
  crewai_completion_tokens?: number
  browser_use_llm_calls?: number
  browser_use_cost?: number
  browser_use_prompt_tokens?: number
  browser_use_completion_tokens?: number
  element_approach_metrics?: { fallback_depth?: number }[] | null
  llm_cleaning_stats?: {
    total_responses?: number
    cleaned_responses?: number
    clean_rate?: number
    formatting_errors_detected?: number
  } | null
  context_reduction?: {
    baseline_tokens?: number
    optimized_tokens?: number
    reduction_percentage?: number
  } | null
  keyword_search_stats?: { calls?: number; avg_latency_ms?: number } | null
  pattern_learning_stats?: { prediction_used?: boolean; predicted_keywords_count?: number } | null
}

const BLUE = '#3b82f6'
const SLATE = '#64748b'
const GREEN = '#22c55e'
const AMBER = '#f59e0b'
const PURPLE = '#a855f7'
const RED = '#ef4444'
const TEAL = '#14b8a6'
const CYAN = '#06b6d4'
const PIE_COLORS = [BLUE, SLATE, GREEN, AMBER, PURPLE, RED, TEAL, '#f97316']

const PERF_CHARTS = [
  { key: 'exec-llm', label: 'Execution Time & LLM Calls' },
  { key: 'cost-trend', label: 'Cost Trend Over Time' },
  { key: 'success-trend', label: 'Success Rate Trend' },
  { key: 'multi-metric', label: 'Multi-Metric Comparison' },
  { key: 'tokens', label: 'Token Usage (Prompt vs Completion)' },
  { key: 'cost-efficiency', label: 'Cost Efficiency ($/Element)' },
  { key: 'elements-bar', label: 'Elements Found (Bar)' },
  { key: 'success-elements', label: 'Success vs Failed Elements' },
  { key: 'element-efficiency', label: 'LLM Calls per Element' },
  { key: 'scatter', label: 'Time vs Cost Scatter' },
  { key: 'fallback-depth', label: 'Locator Strategy Distribution' },
  { key: 'llm-cleaning-rate', label: 'LLM Cleaning Rate' },
  { key: 'context-reduction', label: 'Context Reduction (Optimization)' },
] as const

const COST_CHARTS = [
  { key: 'cost-pie', label: 'Cost Breakdown (Pie)' },
  { key: 'cost-stacked', label: 'Cost Stack Over Time' },
  { key: 'cost-comparison', label: 'Browser vs CrewAI' },
  { key: 'domain-cost', label: 'Cost by Domain' },
  { key: 'llm-split', label: 'LLM Call Split (Pie)' },
  { key: 'llm-histogram', label: 'LLM Call Distribution' },
  { key: 'workflow-radar', label: 'Workflow Metrics Radar' },
  { key: 'domain-performance', label: 'Domain Performance' },
  { key: 'time-distribution', label: 'Execution Time Distribution' },
] as const

function domainOf(url: string): string {
  try { return new URL(url).hostname.replace(/^www\./, '') || 'unknown' } catch { return url?.slice(0, 30) || 'unknown' }
}

const shortTime = (iso: string) =>
  new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })

/** Scale each series to 0–100 of its own max so shapes are comparable. */
function normalize(values: number[]): number[] {
  const max = Math.max(...values)
  return max > 0 ? values.map(v => (v / max) * 100) : values
}

// Locator fallback depths 0–7, best → last resort (legacy labels/colors).
const DEPTH_LABELS = [
  '0: LLM Candidate', '1: Element Data', '2: Candidate', '3: Collection',
  '4: Text First', '5: Semantic', '6: Accessibility', '7: Coordinate',
]
const DEPTH_COLORS = [GREEN, GREEN, CYAN, TEAL, AMBER, AMBER, CYAN, RED]

function selectEl(value: string, onChange: (v: string) => void, options: readonly { key: string; label: string }[]) {
  return (
    <select
      className="h-8 rounded-md border border-input bg-background px-2 text-xs"
      value={value}
      onChange={e => onChange(e.target.value)}
    >
      {options.map(o => <option key={o.key} value={o.key}>{o.label}</option>)}
    </select>
  )
}

export function PerformanceChartCard({ rows }: { rows: MetricsRow[] }) {
  const [chart, setChart] = useState<string>('exec-llm')
  // Oldest → newest so trends read left-to-right
  const ordered = [...rows].reverse()
  const data = ordered.map((r, i) => ({
    i: i + 1,
    when: shortTime(r.timestamp),
    time: Number((r.execution_time ?? 0).toFixed(1)),
    llm: r.total_llm_calls ?? 0,
    cost: Number((r.total_cost ?? 0).toFixed(4)),
    success: Number(((r.success_rate ?? 0) * 100).toFixed(1)),
    prompt: (r.crewai_prompt_tokens ?? 0) + (r.browser_use_prompt_tokens ?? 0),
    completion: (r.crewai_completion_tokens ?? 0) + (r.browser_use_completion_tokens ?? 0),
    elements: r.total_elements ?? 0,
    okEl: r.successful_elements ?? 0,
    failEl: r.failed_elements ?? 0,
    llmPerEl: Number((r.avg_llm_calls_per_element ?? 0).toFixed(2)),
    costPerEl: Number((r.avg_cost_per_element ?? 0).toFixed(4)),
    cleanRate: Number((r.llm_cleaning_stats?.clean_rate ?? 0).toFixed(1)),
    fmtErrors: r.llm_cleaning_stats?.formatting_errors_detected ?? 0,
    ctxBase: r.context_reduction?.baseline_tokens ?? 0,
    ctxOpt: r.context_reduction?.optimized_tokens ?? 0,
    ctxPct: r.context_reduction?.reduction_percentage ?? 0,
  }))

  // Normalized series for the multi-metric comparison
  const multi = data.map((d, i) => ({
    when: d.when,
    cost: Number(normalize(data.map(x => x.cost))[i].toFixed(1)),
    time: Number(normalize(data.map(x => x.time))[i].toFixed(1)),
    llm: Number(normalize(data.map(x => x.llm))[i].toFixed(1)),
    success: d.success,
  }))

  // Locator strategy distribution across every element in the window
  const depthCounts = DEPTH_LABELS.map((label, depth) => ({
    label,
    count: ordered.reduce(
      (sum, r) => sum + (r.element_approach_metrics ?? []).filter(e => e.fallback_depth === depth).length,
      0,
    ),
  }))
  const depthTotal = depthCounts.reduce((s, d) => s + d.count, 0)

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm">Performance & Analysis</CardTitle>
        {selectEl(chart, setChart, PERF_CHARTS)}
      </CardHeader>
      <CardContent className="h-72 pt-2">
        {data.length === 0 ? (
          <p className="pt-10 text-center text-sm text-muted-foreground">No data for this window.</p>
        ) : chart === 'fallback-depth' && depthTotal === 0 ? (
          <p className="pt-10 text-center text-sm text-muted-foreground">No per-element locator data in this window.</p>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            {chart === 'exec-llm' ? (
              <ComposedChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis yAxisId="t" tick={{ fontSize: 11 }} label={{ value: 'Time (s)', angle: -90, position: 'insideLeft', fontSize: 11 }} />
                <YAxis yAxisId="c" orientation="right" tick={{ fontSize: 11 }} label={{ value: 'LLM calls', angle: 90, position: 'insideRight', fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Area yAxisId="t" dataKey="time" name="Execution time (s)" stroke={BLUE} fill={BLUE} fillOpacity={0.15} />
                <Line yAxisId="c" dataKey="llm" name="LLM calls" stroke={SLATE} strokeDasharray="4 3" dot={{ r: 2 }} />
              </ComposedChart>
            ) : chart === 'cost-trend' ? (
              <LineChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Line dataKey="cost" name="Cost (USD)" stroke={GREEN} dot={{ r: 2 }} />
              </LineChart>
            ) : chart === 'success-trend' ? (
              <LineChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Line dataKey="success" name="Success rate (%)" stroke={AMBER} dot={{ r: 2 }} />
              </LineChart>
            ) : chart === 'multi-metric' ? (
              <LineChart data={multi}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Line dataKey="cost" name="Cost (normalized)" stroke={BLUE} dot={false} />
                <Line dataKey="time" name="Time (normalized)" stroke={SLATE} dot={false} />
                <Line dataKey="llm" name="LLM calls (normalized)" stroke={AMBER} dot={false} />
                <Line dataKey="success" name="Success rate (%)" stroke={GREEN} dot={false} />
              </LineChart>
            ) : chart === 'tokens' ? (
              <BarChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="prompt" name="Prompt tokens" stackId="t" fill={BLUE} />
                <Bar dataKey="completion" name="Completion tokens" stackId="t" fill={PURPLE} />
              </BarChart>
            ) : chart === 'cost-efficiency' ? (
              <BarChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="costPerEl" name="Cost per element (USD)" fill={AMBER} />
              </BarChart>
            ) : chart === 'elements-bar' ? (
              <BarChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="elements" name="Elements found" fill={AMBER} />
              </BarChart>
            ) : chart === 'success-elements' ? (
              <BarChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="okEl" name="Successful" stackId="e" fill={GREEN} />
                <Bar dataKey="failEl" name="Failed" stackId="e" fill={RED} />
              </BarChart>
            ) : chart === 'element-efficiency' ? (
              <LineChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Line dataKey="llmPerEl" name="LLM calls per element" stroke={CYAN} dot={{ r: 2 }} />
              </LineChart>
            ) : chart === 'fallback-depth' ? (
              <BarChart data={depthCounts} layout="vertical" margin={{ left: 50 }}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis type="number" tick={{ fontSize: 11 }} allowDecimals={false} label={{ value: 'Number of elements', position: 'insideBottom', offset: -2, fontSize: 11 }} />
                <YAxis type="category" dataKey="label" width={110} tick={{ fontSize: 10 }} />
                <Tooltip formatter={(v) => { const n = Number(v ?? 0); return [`${n} elements (${depthTotal > 0 ? ((n / depthTotal) * 100).toFixed(1) : 0}%)`, 'Elements'] }} />
                <Bar dataKey="count" name="Elements">
                  {depthCounts.map((_, i) => <Cell key={i} fill={DEPTH_COLORS[i]} />)}
                </Bar>
              </BarChart>
            ) : chart === 'llm-cleaning-rate' ? (
              <ComposedChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis yAxisId="r" domain={[0, 100]} tick={{ fontSize: 11 }} label={{ value: 'Clean rate (%)', angle: -90, position: 'insideLeft', fontSize: 11 }} />
                <YAxis yAxisId="e" orientation="right" allowDecimals={false} tick={{ fontSize: 11 }} label={{ value: 'Errors', angle: 90, position: 'insideRight', fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Area yAxisId="r" dataKey="cleanRate" name="Clean rate (%)" stroke={AMBER} fill={AMBER} fillOpacity={0.15} />
                <Line yAxisId="e" dataKey="fmtErrors" name="Formatting errors" stroke={RED} strokeDasharray="4 3" dot={{ r: 2 }} />
              </ComposedChart>
            ) : chart === 'context-reduction' ? (
              <BarChart data={data}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} label={{ value: 'Tokens', angle: -90, position: 'insideLeft', fontSize: 11 }} />
                <Tooltip formatter={(v, name, item) => {
                  const pct = (item as { payload?: { ctxPct?: number } } | undefined)?.payload?.ctxPct
                  return [`${Number(v ?? 0).toLocaleString()}${name === 'Optimized tokens' && pct ? ` (−${pct.toFixed(1)}%)` : ''}`, String(name)]
                }} />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="ctxBase" name="Baseline tokens" fill={SLATE} fillOpacity={0.6} />
                <Bar dataKey="ctxOpt" name="Optimized tokens" fill={GREEN} fillOpacity={0.7} />
              </BarChart>
            ) : (
              <ScatterChart>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="time" name="Time (s)" tick={{ fontSize: 11 }} label={{ value: 'Execution time (s)', position: 'insideBottom', offset: -2, fontSize: 11 }} />
                <YAxis dataKey="cost" name="Cost" tick={{ fontSize: 11 }} label={{ value: 'Cost (USD)', angle: -90, position: 'insideLeft', fontSize: 11 }} />
                <Tooltip cursor={{ strokeDasharray: '3 3' }} />
                <Scatter data={data} fill={BLUE} />
              </ScatterChart>
            )}
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}

export function CostChartCard({ rows }: { rows: MetricsRow[] }) {
  const [chart, setChart] = useState<string>('cost-pie')
  const ordered = [...rows].reverse()

  const browserCost = rows.reduce((s, r) => s + (r.browser_use_cost ?? 0), 0)
  const crewCost = rows.reduce((s, r) => s + (r.crewai_cost ?? 0), 0)
  const browserCalls = rows.reduce((s, r) => s + (r.browser_use_llm_calls ?? 0), 0)
  const crewCalls = rows.reduce((s, r) => s + (r.crewai_llm_calls ?? 0), 0)

  const perRun = ordered.map(r => ({
    when: shortTime(r.timestamp),
    browser: Number((r.browser_use_cost ?? 0).toFixed(4)),
    crew: Number((r.crewai_cost ?? 0).toFixed(4)),
  }))

  // Per-domain aggregates (cost + pass rate over runs)
  const domainStats = rows.reduce<Record<string, { cost: number; runs: number; passes: number }>>((acc, r) => {
    const d = domainOf(r.url)
    acc[d] = acc[d] ?? { cost: 0, runs: 0, passes: 0 }
    acc[d].cost += r.total_cost ?? 0
    acc[d].runs += 1
    if ((r.success_rate ?? 0) >= 1.0) acc[d].passes += 1
    return acc
  }, {})
  const byDomain = Object.entries(domainStats)
    .map(([domain, s]) => ({ domain, cost: Number(s.cost.toFixed(4)) }))
    .sort((a, b) => b.cost - a.cost)
    .slice(0, 8)
  const domainPerf = Object.entries(domainStats)
    .map(([domain, s]) => ({ domain, rate: Number(((s.passes / s.runs) * 100).toFixed(1)) }))
    .sort((a, b) => b.rate - a.rate)
    .slice(0, 8)

  // Histograms (legacy bin edges)
  const llmBins = [0, 10, 20, 30, 40, 50, 100]
  const llmHist = llmBins.slice(0, -1).map((b, i) => ({
    range: `${b}–${llmBins[i + 1]}`,
    count: rows.filter(r => (r.total_llm_calls ?? 0) >= b && (r.total_llm_calls ?? 0) < llmBins[i + 1]).length,
  }))
  const maxTime = Math.max(...rows.map(r => r.execution_time ?? 0), 0)
  const timeBins = [0, 20, 40, 60, 80, 100, Math.max(maxTime + 1, 101)]
  const timeHist = timeBins.slice(0, -1).map((b, i) => ({
    range: `${b}–${Math.round(timeBins[i + 1])}s`,
    count: rows.filter(r => (r.execution_time ?? 0) >= b && (r.execution_time ?? 0) < timeBins[i + 1]).length,
  }))

  // Radar over the last 5 runs, scaled like the legacy chart so the axes share a range
  const recent5 = ordered.slice(-5)
  const radarData = recent5.length === 0 ? [] : [
    { metric: 'Cost ($×100)', value: Number((recent5.reduce((a, r) => a + (r.total_cost ?? 0), 0) / recent5.length * 100).toFixed(1)) },
    { metric: 'Time (/10s)', value: Number((recent5.reduce((a, r) => a + (r.execution_time ?? 0), 0) / recent5.length / 10).toFixed(1)) },
    { metric: 'LLM calls', value: Number((recent5.reduce((a, r) => a + (r.total_llm_calls ?? 0), 0) / recent5.length).toFixed(1)) },
    { metric: 'Elements (×10)', value: Number((recent5.reduce((a, r) => a + (r.total_elements ?? 0), 0) / recent5.length * 10).toFixed(1)) },
    { metric: 'Success (%)', value: Number((recent5.reduce((a, r) => a + (r.success_rate ?? 0), 0) / recent5.length * 100).toFixed(1)) },
  ]

  const pie = chart === 'cost-pie'
    ? [
        { name: 'Browser Actions', value: Number(browserCost.toFixed(4)) },
        { name: 'CrewAI Logic', value: Number(crewCost.toFixed(4)) },
      ]
    : [
        { name: 'Browser Actions', value: browserCalls },
        { name: 'CrewAI Logic', value: crewCalls },
      ]

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm">Cost & Advanced Analytics</CardTitle>
        {selectEl(chart, setChart, COST_CHARTS)}
      </CardHeader>
      <CardContent className="h-72 pt-2">
        {rows.length === 0 ? (
          <p className="pt-10 text-center text-sm text-muted-foreground">No data for this window.</p>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            {chart === 'domain-cost' ? (
              <BarChart data={byDomain} layout="vertical" margin={{ left: 40 }}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis type="number" tick={{ fontSize: 11 }} />
                <YAxis type="category" dataKey="domain" width={140} tick={{ fontSize: 10 }} />
                <Tooltip />
                <Bar dataKey="cost" name="Cost (USD)" fill={BLUE}>
                  {byDomain.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                </Bar>
              </BarChart>
            ) : chart === 'domain-performance' ? (
              <BarChart data={domainPerf} layout="vertical" margin={{ left: 40 }}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis type="number" domain={[0, 100]} tick={{ fontSize: 11 }} label={{ value: 'Pass rate (%)', position: 'insideBottom', offset: -2, fontSize: 11 }} />
                <YAxis type="category" dataKey="domain" width={140} tick={{ fontSize: 10 }} />
                <Tooltip formatter={(v) => [`${Number(v ?? 0)}%`, 'Pass rate']} />
                <Bar dataKey="rate" name="Pass rate (%)">
                  {domainPerf.map((d, i) => (
                    <Cell key={i} fill={d.rate >= 80 ? GREEN : d.rate >= 50 ? AMBER : RED} />
                  ))}
                </Bar>
              </BarChart>
            ) : chart === 'cost-stacked' ? (
              <BarChart data={perRun}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Bar dataKey="browser" name="Browser cost" stackId="c" fill={BLUE} />
                <Bar dataKey="crew" name="CrewAI cost" stackId="c" fill={SLATE} />
              </BarChart>
            ) : chart === 'cost-comparison' ? (
              <ComposedChart data={perRun}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="when" tick={{ fontSize: 11 }} interval="preserveStartEnd" />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
                <Area dataKey="browser" name="Browser cost" stroke={BLUE} fill={BLUE} fillOpacity={0.15} />
                <Area dataKey="crew" name="CrewAI cost" stroke={SLATE} fill={SLATE} fillOpacity={0.15} />
              </ComposedChart>
            ) : chart === 'llm-histogram' ? (
              <BarChart data={llmHist}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="range" tick={{ fontSize: 11 }} label={{ value: 'LLM call range', position: 'insideBottom', offset: -2, fontSize: 11 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="count" name="Workflows" fill={PURPLE} />
              </BarChart>
            ) : chart === 'time-distribution' ? (
              <BarChart data={timeHist}>
                <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
                <XAxis dataKey="range" tick={{ fontSize: 11 }} label={{ value: 'Execution time range', position: 'insideBottom', offset: -2, fontSize: 11 }} />
                <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                <Tooltip />
                <Bar dataKey="count" name="Workflows" fill={CYAN} />
              </BarChart>
            ) : chart === 'workflow-radar' ? (
              <RadarChart data={radarData}>
                <PolarGrid />
                <PolarAngleAxis dataKey="metric" tick={{ fontSize: 10 }} />
                <PolarRadiusAxis tick={{ fontSize: 9 }} />
                <Radar name="Recent performance (avg of last 5)" dataKey="value" stroke={BLUE} fill={BLUE} fillOpacity={0.25} />
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
              </RadarChart>
            ) : (
              <PieChart>
                <Pie data={pie} dataKey="value" nameKey="name" innerRadius="55%" outerRadius="80%" paddingAngle={2}>
                  {pie.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
                </Pie>
                <Tooltip />
                <Legend wrapperStyle={{ fontSize: 12 }} />
              </PieChart>
            )}
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}
