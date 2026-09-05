/**
 * MetricsCharts — the two chart cards, all 22 variants.
 *
 * These are ports of the legacy canvas charts, and each variant is its own
 * branch that reshapes the same rows a different way. The failure mode worth
 * guarding is not "the chart looks wrong" — it is that one variant throws on a
 * shape the others tolerate (a null url, a zero denominator, a single row, an
 * empty set) and takes the whole page down with it. So every variant is driven
 * against four hostile datasets rather than one happy one.
 *
 * recharts measures its container, and jsdom reports zero for everything, so
 * ResponsiveContainer is replaced with one that hands its child a fixed size.
 * Without that the charts render nothing and every assertion below would pass
 * vacuously.
 */
import { cloneElement, type ReactElement } from 'react'
import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'

vi.mock('recharts', async importOriginal => {
  const actual = await importOriginal<typeof import('recharts')>()
  return {
    ...actual,
    ResponsiveContainer: ({ children }: { children: ReactElement }) =>
      cloneElement(children, { width: 800, height: 400 }),
  }
})

import { PerformanceChartCard, CostChartCard, type MetricsRow } from './MetricsCharts'

const PERFORMANCE_VARIANTS = [
  'exec-llm', 'cost-trend', 'success-trend', 'multi-metric', 'tokens',
  'cost-efficiency', 'elements-bar', 'success-elements', 'element-efficiency',
  'scatter', 'fallback-depth', 'llm-cleaning-rate', 'context-reduction',
]
const COST_VARIANTS = [
  'cost-pie', 'cost-stacked', 'cost-comparison', 'domain-cost', 'llm-split',
  'llm-histogram', 'workflow-radar', 'domain-performance', 'time-distribution',
]

/**
 * What "this variant survived that dataset" actually has to mean.
 *
 * `select.toHaveValue(key)` proves nothing — the select changes whether or not
 * the chart rendered. These three checks are what catch the real failure modes:
 * a variant that renders NOTHING on data the others handle, and a variant that
 * lets NaN or Infinity reach an SVG attribute, which recharts does not throw on
 * — it silently draws nothing, so the page looks fine and the chart is empty.
 */
function assertChartIsSane(key: string, rowCount: number) {
  if (rowCount === 0) return          // an empty dataset legitimately draws nothing
  const svg = document.querySelector('svg.recharts-surface')
  if (!svg) {
    // A variant may legitimately decline to draw - but only by SAYING so.
    // A silently blank card is the failure this helper exists to catch, and
    // writing this check is how fallback-depth's empty state was found.
    const explained = screen.queryByText(/^No .* (data|window)/i)
    expect(explained, `${key} rendered neither a chart nor an explanation`).not.toBeNull()
    return
  }
  const markup = svg.outerHTML
  expect(markup, `${key} leaked NaN into the SVG`).not.toMatch(/NaN/)
  expect(markup, `${key} leaked Infinity into the SVG`).not.toMatch(/Infinity/)
}

/** One located element, at the given locator fallback depth. */
const el = (fallback_depth: number) => ({ fallback_depth, success: true })

function row(over: Partial<MetricsRow> = {}): MetricsRow {
  return {
    workflow_id: 'wf-1', url: 'https://www.flipkart.com/search', timestamp: '2026-09-01T10:00:00Z',
    total_llm_calls: 5, total_cost: 0.059, execution_time: 40.5,
    total_elements: 4, successful_elements: 3, failed_elements: 1,
    prompt_tokens: 12000, completion_tokens: 900, success: true,
    browser_use_cost: 0.02, crewai_cost: 0.039,
    browser_use_llm_calls: 3, crewai_llm_calls: 2,
    ...over,
  } as MetricsRow
}

/** Four datasets that between them cover the shapes that break naive maths. */
const DATASETS: [string, MetricsRow[]][] = [
  ['a normal multi-row set', [row(), row({ workflow_id: 'wf-2', url: 'https://amazon.in/x', total_cost: 0.12, success: false })]],
  ['a single row', [row()]],
  ['an empty set', []],
  ['rows with null urls and zeroed denominators', [
    row({ url: null, total_elements: 0, successful_elements: 0, failed_elements: 0,
          total_llm_calls: 0, total_cost: 0, execution_time: 0,
          prompt_tokens: 0, completion_tokens: 0 }),
  ]],
]

describe('PerformanceChartCard — every variant against every shape', () => {
  it.each(DATASETS)('renders all 13 performance variants for %s', (_label, rows) => {
    render(<PerformanceChartCard rows={rows} />)
    const select = screen.getByRole('combobox')

    for (const key of PERFORMANCE_VARIANTS) {
      fireEvent.change(select, { target: { value: key } })
      assertChartIsSane(key, rows.length)
    }
  })
})

describe('CostChartCard — every variant against every shape', () => {
  it.each(DATASETS)('renders all 9 cost variants for %s', (_label, rows) => {
    render(<CostChartCard rows={rows} />)
    const select = screen.getByRole('combobox')

    for (const key of COST_VARIANTS) {
      fireEvent.change(select, { target: { value: key } })
      assertChartIsSane(key, rows.length)
    }
  })
})

describe('MetricsCharts — the cards themselves', () => {
  it('offers all 13 performance options', () => {
    render(<PerformanceChartCard rows={[row()]} />)

    expect(screen.getAllByRole('option')).toHaveLength(13)
    expect(screen.getByRole('option', { name: 'Execution Time & LLM Calls' })).toBeInTheDocument()
  })

  it('offers all 9 cost options', () => {
    render(<CostChartCard rows={[row()]} />)

    expect(screen.getAllByRole('option')).toHaveLength(9)
    expect(screen.getByRole('option', { name: 'Cost Breakdown (Pie)' })).toBeInTheDocument()
  })

  it('starts each card on its first option', () => {
    render(<PerformanceChartCard rows={[row()]} />)

    expect(screen.getByRole('combobox')).toHaveValue('exec-llm')
  })

  it('renders both cards side by side without their selects colliding', () => {
    // Two cards, two independent selects — changing one must not move the other.
    render(<><PerformanceChartCard rows={[row()]} /><CostChartCard rows={[row()]} /></>)
    const [perf, cost] = screen.getAllByRole('combobox')

    fireEvent.change(perf, { target: { value: 'scatter' } })

    expect(perf).toHaveValue('scatter')
    expect(cost).toHaveValue('cost-pie')
  })
})

describe('MetricsCharts — locator strategy distribution', () => {
  it('explains the absence instead of drawing an empty chart', () => {
    // element_approach_metrics is absent on most rows (it is written only when
    // per-element data was captured). A blank card would read as a broken
    // chart; this says the window has no data.
    render(<PerformanceChartCard rows={[row()]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'fallback-depth' } })

    expect(screen.getByText('No per-element locator data in this window.')).toBeInTheDocument()
    expect(document.querySelector('svg.recharts-surface')).toBeNull()
  })

  it('draws the distribution once any row carries per-element data', () => {
    render(<PerformanceChartCard rows={[
      row({ element_approach_metrics: [el(0), el(0), el(3)] } as Partial<MetricsRow>),
      row({ workflow_id: 'wf-2', element_approach_metrics: [el(7)] } as Partial<MetricsRow>),
    ]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'fallback-depth' } })

    expect(screen.queryByText('No per-element locator data in this window.')).toBeNull()
    assertChartIsSane('fallback-depth', 2)
  })

  it('counts depths ACROSS rows, not per row', () => {
    // The window total is what tells you the locator stack is degrading; a
    // per-row reset would hide a fleet-wide shift to deep fallbacks.
    render(<PerformanceChartCard rows={[
      row({ element_approach_metrics: [el(7)] } as Partial<MetricsRow>),
      row({ workflow_id: 'wf-2', element_approach_metrics: [el(7)] } as Partial<MetricsRow>),
    ]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'fallback-depth' } })

    const labels = Array.from(document.querySelectorAll('.recharts-cartesian-axis-tick-value'))
      .map(e => e.textContent)
    expect(labels.some(l => l?.includes('Coordinate'))).toBe(true)
  })
})

describe('MetricsCharts — domain grouping (domainOf)', () => {
  it('groups by hostname with the www stripped', () => {
    render(<CostChartCard rows={[
      row({ url: 'https://www.flipkart.com/a' }),
      row({ workflow_id: 'wf-2', url: 'https://flipkart.com/b' }),
    ]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'domain-cost' } })

    // Both rows collapse to ONE domain: www. and bare are the same site, and
    // two bars here would double-count a customer's spend.
    const labels = Array.from(document.querySelectorAll('.recharts-cartesian-axis-tick-value'))
      .map(el => el.textContent)
    expect(labels.filter(l => l === 'flipkart.com')).toHaveLength(1)
    expect(labels).not.toContain('www.flipkart.com')
  })

  it('labels a null url "unknown" rather than dropping the run', () => {
    // The backend never fabricates a url. A run whose query named none still
    // cost money, so it must appear in a cost-by-domain chart.
    render(<CostChartCard rows={[row({ url: null })]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'domain-cost' } })

    const labels = Array.from(document.querySelectorAll('.recharts-cartesian-axis-tick-value'))
      .map(el => el.textContent)
    expect(labels).toContain('unknown')
  })

  it('survives a url that is not parseable at all', () => {
    // `new URL()` throws on these; the catch falls back to a truncated string.
    render(<CostChartCard rows={[row({ url: 'not a url at all ::::' })]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'domain-cost' } })

    // The catch falls back to a truncated raw string rather than dropping the run.
    assertChartIsSane('domain-cost', 1)
  })

  it('survives an empty-string url', () => {
    render(<CostChartCard rows={[row({ url: '' })]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'domain-performance' } })

    const labels = Array.from(document.querySelectorAll('.recharts-cartesian-axis-tick-value'))
      .map(el => el.textContent)
    expect(labels).toContain('unknown')
  })
})

describe('MetricsCharts — normalize() against an all-zero series', () => {
  it('does not divide by zero when every value is 0', () => {
    // normalize() scales each series to its own max. With max 0 it must return
    // the values untouched rather than producing NaN, which recharts renders
    // as a blank chart with no error.
    render(<PerformanceChartCard rows={[
      row({ total_cost: 0, execution_time: 0, total_llm_calls: 0, total_elements: 0 }),
      row({ workflow_id: 'wf-2', total_cost: 0, execution_time: 0, total_llm_calls: 0, total_elements: 0 }),
    ]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'multi-metric' } })

    // 0/0 would be NaN, which recharts renders as an empty chart rather than
    // an error - exactly the silent failure assertChartIsSane exists to catch.
    assertChartIsSane('multi-metric', 2)
  })

  it('handles the radar variant on an all-zero row', () => {
    render(<CostChartCard rows={[row({ total_cost: 0, execution_time: 0, total_llm_calls: 0, total_elements: 0 })]} />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'workflow-radar' } })

    assertChartIsSane('workflow-radar', 1)
  })
})
