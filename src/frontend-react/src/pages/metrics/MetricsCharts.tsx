/**
 * Metrics charts — recharts ports of the legacy canvas charts
 * (src/frontend/metrics-charts.js). Two cards: performance/analysis
 * (selectable series over time) and cost/advanced (breakdowns).
 */

import { useState } from 'react'
import {
  Area, Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, Line,
  LineChart, Pie, PieChart, ResponsiveContainer, Scatter, ScatterChart,
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
  crewai_llm_calls?: number
  crewai_cost?: number
  crewai_prompt_tokens?: number
  crewai_completion_tokens?: number
  browser_use_llm_calls?: number
  browser_use_cost?: number
  browser_use_prompt_tokens?: number
  browser_use_completion_tokens?: number
}

const BLUE = '#3b82f6'
const SLATE = '#64748b'
const GREEN = '#22c55e'
const AMBER = '#f59e0b'
const PURPLE = '#a855f7'
const PIE_COLORS = [BLUE, SLATE, GREEN, AMBER, PURPLE, '#ef4444', '#14b8a6', '#f97316']

const PERF_CHARTS = [
  { key: 'exec-llm', label: 'Execution Time & LLM Calls' },
  { key: 'cost-trend', label: 'Cost Trend Over Time' },
  { key: 'success-trend', label: 'Success Rate Trend' },
  { key: 'tokens', label: 'Token Usage (Prompt vs Completion)' },
  { key: 'scatter', label: 'Time vs Cost Scatter' },
] as const

const COST_CHARTS = [
  { key: 'cost-pie', label: 'Cost Breakdown (Pie)' },
  { key: 'llm-split', label: 'LLM Call Distribution' },
  { key: 'domain-cost', label: 'Cost by Domain' },
] as const

function domainOf(url: string): string {
  try { return new URL(url).hostname || 'unknown' } catch { return url?.slice(0, 30) || 'unknown' }
}

const shortTime = (iso: string) =>
  new Date(iso).toLocaleDateString('en-US', { month: 'short', day: 'numeric' })

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
  const data = [...rows].reverse().map((r, i) => ({
    i: i + 1,
    when: shortTime(r.timestamp),
    time: Number((r.execution_time ?? 0).toFixed(1)),
    llm: r.total_llm_calls ?? 0,
    cost: Number((r.total_cost ?? 0).toFixed(4)),
    success: Number(((r.success_rate ?? 0) * 100).toFixed(1)),
    prompt: (r.crewai_prompt_tokens ?? 0) + (r.browser_use_prompt_tokens ?? 0),
    completion: (r.crewai_completion_tokens ?? 0) + (r.browser_use_completion_tokens ?? 0),
  }))

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-2">
        <CardTitle className="text-sm">Performance & Analysis</CardTitle>
        {selectEl(chart, setChart, PERF_CHARTS)}
      </CardHeader>
      <CardContent className="h-72 pt-2">
        {data.length === 0 ? (
          <p className="pt-10 text-center text-sm text-muted-foreground">No data for this window.</p>
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

  const browserCost = rows.reduce((s, r) => s + (r.browser_use_cost ?? 0), 0)
  const crewCost = rows.reduce((s, r) => s + (r.crewai_cost ?? 0), 0)
  const browserCalls = rows.reduce((s, r) => s + (r.browser_use_llm_calls ?? 0), 0)
  const crewCalls = rows.reduce((s, r) => s + (r.crewai_llm_calls ?? 0), 0)

  const byDomain = Object.entries(
    rows.reduce<Record<string, number>>((acc, r) => {
      const d = domainOf(r.url)
      acc[d] = (acc[d] ?? 0) + (r.total_cost ?? 0)
      return acc
    }, {}),
  )
    .map(([domain, cost]) => ({ domain, cost: Number(cost.toFixed(4)) }))
    .sort((a, b) => b.cost - a.cost)
    .slice(0, 8)

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
        ) : chart === 'domain-cost' ? (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={byDomain} layout="vertical" margin={{ left: 40 }}>
              <CartesianGrid strokeDasharray="3 3" className="stroke-muted" />
              <XAxis type="number" tick={{ fontSize: 11 }} />
              <YAxis type="category" dataKey="domain" width={140} tick={{ fontSize: 10 }} />
              <Tooltip />
              <Bar dataKey="cost" name="Cost (USD)" fill={BLUE}>
                {byDomain.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <PieChart>
              <Pie data={pie} dataKey="value" nameKey="name" innerRadius="55%" outerRadius="80%" paddingAngle={2}>
                {pie.map((_, i) => <Cell key={i} fill={PIE_COLORS[i % PIE_COLORS.length]} />)}
              </Pie>
              <Tooltip />
              <Legend wrapperStyle={{ fontSize: 12 }} />
            </PieChart>
          </ResponsiveContainer>
        )}
      </CardContent>
    </Card>
  )
}
