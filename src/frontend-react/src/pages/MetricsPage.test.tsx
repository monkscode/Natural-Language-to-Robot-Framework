/**
 * MetricsPage — the summary cards, the window filter, and the run table.
 *
 * Two units mismatch on purpose and are the thing most likely to be "fixed"
 * into a bug: the SUMMARY's avg_success_rate is already 0–100, while a per-ROW
 * success_rate is 0–1. Multiplying the summary by 100, or failing to multiply
 * the row, both produce a plausible-looking percentage that is wrong by two
 * orders of magnitude. Each is pinned separately.
 *
 * The window filter is client-side over rows the server already returned, so
 * "All time" must apply no cutoff at all rather than a very large one.
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))
// The charts are covered by their own file and would only add noise here.
vi.mock('./metrics/MetricsCharts', () => ({
  PerformanceChartCard: () => <div data-testid="perf-chart" />,
  CostChartCard: () => <div data-testid="cost-chart" />,
}))

import { useFetch } from '@/lib/useFetch'
import MetricsPage from './MetricsPage'

const mockUseFetch = vi.mocked(useFetch)
afterEach(() => vi.resetAllMocks())

const AGG = {
  total_workflows: 122, total_elements: 431, successful_elements: 247,
  avg_success_rate: 57.3, total_cost: 7.1938, avg_cost_per_element: 0.0167,
  avg_execution_time: 40.53, total_llm_calls: 610,
}

function metricsRow(over: Record<string, unknown> = {}) {
  return {
    workflow_id: 'wf-full-0123456789abcdef', url: 'https://flipkart.com',
    timestamp: new Date().toISOString(), total_llm_calls: 5, total_cost: 0.0593,
    execution_time: 40.5, total_elements: 4, successful_elements: 3,
    failed_elements: 1, success_rate: 0.75,
    crewai_llm_calls: 2, crewai_cost: 0.039, browser_use_llm_calls: 3, browser_use_cost: 0.02,
    crewai_prompt_tokens: 12000, crewai_completion_tokens: 900,
    browser_use_prompt_tokens: 3400, browser_use_completion_tokens: 210,
    ...over,
  }
}

const reloads = { summary: vi.fn(), learning: vi.fn(), recent: vi.fn() }

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function setup({ summary = { last_7_days: AGG }, learning = null, recent = [metricsRow()], loading = false,
                summaryError = '', recentError = '' }: any = {}) {
  reloads.summary = vi.fn(); reloads.learning = vi.fn(); reloads.recent = vi.fn()
  mockUseFetch.mockImplementation((path: string | null) => {
    if (path?.includes('summary')) return { data: summary, loading, error: summaryError, reload: reloads.summary }
    if (path?.includes('learning-health')) return { data: learning, loading: false, error: '', reload: reloads.learning }
    return { data: recent, loading, error: recentError, reload: reloads.recent }
  })
  render(<MetricsPage />)
}

describe('MetricsPage — the summary cards', () => {
  it('renders the aggregate figures for the selected window', () => {
    setup()

    expect(screen.getByText('122')).toBeInTheDocument()
    expect(screen.getByText('431 elements')).toBeInTheDocument()
    expect(screen.getByText('$7.1938')).toBeInTheDocument()
    // Also appears on the run row, so assert presence rather than uniqueness.
    expect(screen.getAllByText('40.5s').length).toBeGreaterThan(0)
    expect(screen.getByText('610 LLM calls')).toBeInTheDocument()
  })

  it('treats the SUMMARY success rate as already 0–100', () => {
    // 57.3 is a percentage. Scaling it again would print "5730.0%".
    setup()

    expect(screen.getByText('57.3%')).toBeInTheDocument()
  })

  it('formats money to four decimals, since a run costs cents', () => {
    setup()

    expect(screen.getByText('$0.0167/element')).toBeInTheDocument()
  })

  it('defaults a missing success rate to 0.0% rather than NaN', () => {
    setup({ summary: { last_7_days: { ...AGG, avg_success_rate: undefined } } })

    expect(screen.getByText('0.0%')).toBeInTheDocument()
  })

  it('shows skeletons while the summary is loading with nothing to show', () => {
    setup({ summary: null, loading: true })

    expect(screen.queryByText('122')).toBeNull()
  })

  it('surfaces a failed summary read', () => {
    setup({ summary: null, summaryError: 'Admin access required' })

    expect(screen.getByText('Admin access required')).toBeInTheDocument()
  })
})

describe('MetricsPage — the window filter', () => {
  it('starts on 7d', () => {
    setup()

    expect(screen.getByRole('button', { name: '7d' })).toBeInTheDocument()
  })

  it('switches the aggregate to the window that was clicked', () => {
    setup({ summary: { last_7_days: AGG, last_24_hours: { ...AGG, total_workflows: 9 } } })

    fireEvent.click(screen.getByRole('button', { name: '24h' }))

    expect(screen.getByText('9')).toBeInTheDocument()
  })

  it('renders no cards for a window the summary has no bucket for', () => {
    setup({ summary: { last_7_days: AGG } })

    fireEvent.click(screen.getByRole('button', { name: '30d' }))

    expect(screen.queryByText('122')).toBeNull()
  })

  it('applies NO cutoff for All time', () => {
    // hours is null for all_time, so the filter must pass every row through —
    // including one older than any finite window.
    const ancient = metricsRow({ workflow_id: 'wf-ancient', timestamp: '2020-01-01T00:00:00Z' })
    setup({ recent: [ancient] })

    fireEvent.click(screen.getByRole('button', { name: 'All time' }))

    expect(screen.getByText('wf-ancient')).toBeInTheDocument()
  })

  it('refreshes all three reads from one button', () => {
    setup()

    fireEvent.click(screen.getByRole('button', { name: /refresh/i }))

    expect(reloads.summary).toHaveBeenCalled()
    expect(reloads.learning).toHaveBeenCalled()
    expect(reloads.recent).toHaveBeenCalled()
  })
})

describe('MetricsPage — the recent-runs table', () => {
  it('shows the full workflow id, never a truncation', () => {
    setup()

    expect(screen.getByText('wf-full-0123456789abcdef')).toBeInTheDocument()
  })

  it('classifies a fully-successful run PASS', () => {
    setup({ recent: [metricsRow({ success_rate: 1 })] })

    expect(screen.getByText('PASS')).toBeInTheDocument()
  })

  it('classifies a partially-successful run PARTIAL, not FAIL', () => {
    // A run that located 3 of 4 elements is not a failure, and calling it one
    // hides the only signal that the locator stack is degrading.
    setup({ recent: [metricsRow({ success_rate: 0.75 })] })

    expect(screen.getByText('PARTIAL')).toBeInTheDocument()
  })

  it('classifies a run that located nothing FAIL', () => {
    setup({ recent: [metricsRow({ success_rate: 0 })] })

    expect(screen.getByText('FAIL')).toBeInTheDocument()
  })

  it('treats a hair under 1.0 as PASS, not PARTIAL', () => {
    // The threshold is >= 0.999 precisely because floating point division of
    // 247/247 does not always land on exactly 1.
    setup({ recent: [metricsRow({ success_rate: 0.9995 })] })

    expect(screen.getByText('PASS')).toBeInTheDocument()
  })

  it('renders an em dash for a run with no url', () => {
    setup({ recent: [metricsRow({ url: null })] })

    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('expands a row into its cost and token breakdown', () => {
    setup()

    fireEvent.click(screen.getByText('https://flipkart.com'))

    expect(screen.getByText('CrewAI')).toBeInTheDocument()
    expect(screen.getByText('2 calls · $0.0390')).toBeInTheDocument()
    expect(screen.getByText('3 calls · $0.0200')).toBeInTheDocument()
  })

  it('treats a per-ROW success rate as a 0–1 fraction', () => {
    // The mirror of the summary test: 0.75 here must render "75.0%", and the
    // two conventions must not be unified without changing the backend.
    setup()

    fireEvent.click(screen.getByText('https://flipkart.com'))

    expect(screen.getByText('3 ok / 1 failed (75.0%)')).toBeInTheDocument()
  })

  it('renders token counts with thousands separators', () => {
    setup()

    fireEvent.click(screen.getByText('https://flipkart.com'))

    expect(screen.getByText('12,000 in / 900 out')).toBeInTheDocument()
  })

  it('collapses an expanded row when clicked again', () => {
    setup()
    fireEvent.click(screen.getByText('https://flipkart.com'))
    expect(screen.getByText('CrewAI')).toBeInTheDocument()

    fireEvent.click(screen.getByText('https://flipkart.com'))

    expect(screen.queryByText('CrewAI')).toBeNull()
  })

  it('surfaces a failed recent-runs read inside the card', () => {
    setup({ recent: null, recentError: 'Recent runs unavailable' })

    expect(screen.getByText('Recent runs unavailable')).toBeInTheDocument()
  })

  it('renders the learning-health card only when that read returned', () => {
    // The page subtitle mentions "learning insights", so anchor on a label
    // only the card itself renders.
    setup({ learning: null })
    expect(screen.queryByText('Total rules')).toBeNull()

    cleanup()
    setup({ learning: { status: 'active', total_rules: 36, total_executions: 122 } })
    expect(screen.getByText('Total rules')).toBeInTheDocument()
    expect(screen.getByText('36')).toBeInTheDocument()
  })

  it('defaults the learning card’s optional counters to 0 rather than blank', () => {
    // total_rules and total_executions are required; the other four are not,
    // and a blank cell reads as "unknown" where the real answer is "none".
    setup({ learning: { status: 'active', total_rules: 36, total_executions: 122 } })

    expect(screen.getByText('Anti-patterns')).toBeInTheDocument()
    expect(screen.getAllByText('0').length).toBeGreaterThanOrEqual(4)
  })

  it('says the window is empty rather than showing a bare table', () => {
    setup({ recent: [] })

    expect(screen.getByText('No workflow metrics in this time window.')).toBeInTheDocument()
  })
})
