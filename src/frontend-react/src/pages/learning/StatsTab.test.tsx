/**
 * StatsTab — the learning dashboard's numbers, and the formatters underneath
 * them.
 *
 * Almost every value on this page is optional and defaulted with `??`. That is
 * deliberate — the endpoint's shape grows — but it means a missing section
 * renders "0" rather than nothing, and 0 is a claim. These tests pin which
 * absences show as 0, which show as "—", and which show as "n/a" or
 * "insufficient data", because those three say very different things to
 * someone deciding whether learning is working.
 *
 * fmtLift is tested directly as well: it is the one formatter whose OUTPUT is
 * a judgement (a signed percentage), and it uses a typographic minus (U+2212),
 * not a hyphen, which is exactly the sort of detail a rewrite loses.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))

import { useFetch } from '@/lib/useFetch'
import StatsTab from './StatsTab'
import { fmtLift, pctOrDash, fmtWhen } from './types'

const mockUseFetch = vi.mocked(useFetch)
afterEach(() => vi.resetAllMocks())

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function setup(data: any, { loading = false, error = '' } = {}) {
  mockUseFetch.mockReturnValue({ data, loading, error, reload: vi.fn() })
  render(<StatsTab />)
}

describe('StatsTab — the three load states', () => {
  it('shows a loading line while the stats are in flight', () => {
    setup(null, { loading: true })

    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the error instead of the page when the read fails', () => {
    setup(null, { error: 'Org-admin access required' })

    expect(screen.getByText('Org-admin access required')).toBeInTheDocument()
    expect(screen.queryByText('Hint inventory')).toBeNull()
  })

  it('renders nothing at all when there is no data and no error', () => {
    const { container } = render(<div />)
    setup(null)

    expect(screen.queryByText('Hint inventory')).toBeNull()
    expect(container).toBeInTheDocument()
  })
})

describe('StatsTab — hint inventory', () => {
  it('renders every inventory count it is given', () => {
    setup({
      hint_inventory: {
        active: 36, flagged: 2, auto_disabled: 5, llm_review_disabled: 1,
        retracted: 4, admin_created: 7, workflow_created: 29,
      },
    })

    expect(screen.getByText('36')).toBeInTheDocument()
    expect(screen.getByText('29')).toBeInTheDocument()
    expect(screen.getByText('Auto-disabled')).toBeInTheDocument()
  })

  it('falls back to 0 for a missing inventory rather than blanking the section', () => {
    setup({})

    expect(screen.getByText('Hint inventory')).toBeInTheDocument()
    expect(screen.getAllByText('0').length).toBeGreaterThan(0)
  })
})

describe('StatsTab — learning effectiveness', () => {
  const nc = {
    lift: 0.042,
    honest_lift: -0.013,
    no_hints_available: { total: 40, pass_rate: 0.6 },
    hints_injected: { total: 60, pass_rate: 0.75 },
    holdout_suppressed: { total: 0, pass_rate: null },
  }

  it('shows each category’s run count and pass rate', () => {
    setup({ learning_effectiveness: { natural_comparison: nc } })

    expect(screen.getByText('40')).toBeInTheDocument()
    expect(screen.getByText('60.0% pass rate')).toBeInTheDocument()
    expect(screen.getByText('75.0% pass rate')).toBeInTheDocument()
  })

  it('says "no runs yet" for a category with zero runs, not a 0% pass rate', () => {
    // 0% pass rate and "we have not measured this" are completely different
    // claims about the holdout arm.
    setup({ learning_effectiveness: { natural_comparison: nc } })

    expect(screen.getByText('no runs yet')).toBeInTheDocument()
  })

  it('renders a positive lift with an explicit + sign', () => {
    setup({ learning_effectiveness: { natural_comparison: nc } })

    expect(screen.getByText('+4.2%')).toBeInTheDocument()
  })

  it('says "insufficient data" — not 0% — when honest lift is unknown', () => {
    setup({ learning_effectiveness: { natural_comparison: { ...nc, honest_lift: null } } })

    expect(screen.getByText('insufficient data')).toBeInTheDocument()
  })

  it('shows an em dash for lift when the whole comparison is absent', () => {
    setup({ learning_effectiveness: {} })

    expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  })
})

describe('StatsTab — attribution health and cost', () => {
  it('renders attribution counts with their credited sub-line', () => {
    setup({
      attribution_health: {
        events_total: 118, credited_events: 41,
        retirement_reversal_rate: 0.25, retired_never_used_total: 8,
        holdout_lift: 0.031,
      },
    })

    expect(screen.getByText('118')).toBeInTheDocument()
    expect(screen.getByText('41 credited ≥1 hint')).toBeInTheDocument()
    expect(screen.getByText('25.0%')).toBeInTheDocument()
    expect(screen.getByText('+3.1%')).toBeInTheDocument()
  })

  it('says "n/a" — not 0 — for an unmeasured reversal rate or holdout lift', () => {
    setup({ attribution_health: { events_total: 0, credited_events: 0 } })

    expect(screen.getAllByText('n/a')).toHaveLength(2)
  })

  it('formats cost per successful test to four decimals', () => {
    // Runs cost fractions of a cent; two decimals would round most of them to
    // $0.00 and make the figure useless.
    setup({ learning_effectiveness: { cost_per_successful_test: 0.0593, total_executions: 122 } })

    expect(screen.getByText('$0.0593')).toBeInTheDocument()
    expect(screen.getByText('122 executions')).toBeInTheDocument()
  })
})

describe('StatsTab — the four tables', () => {
  it('names each trigger by its label rather than its raw key', () => {
    setup({ trigger_activity: [{ trigger_type: 'trigger_1', total: 9, flagged: 2 }] })

    expect(screen.getByText('Fix-after-fail (Case B)')).toBeInTheDocument()
  })

  it('falls back to the raw key for a trigger type it has no label for', () => {
    // The backend owns these keys. A new one must still render, not vanish.
    setup({ trigger_activity: [{ trigger_type: 'trigger_99_new', total: 1, flagged: 0 }] })

    expect(screen.getByText('trigger_99_new')).toBeInTheDocument()
  })

  it('renders token counts with thousands separators and cost to four decimals', () => {
    setup({
      llm_cost: {
        total_estimated_usd: 1.2345,
        by_model: [{ model: 'gemini-3.5-flash', input_tokens: 1234567, output_tokens: 8901, calls: 42, estimated_cost_usd: 1.2345 }],
      },
    })

    expect(screen.getByText('1,234,567')).toBeInTheDocument()
    expect(screen.getByText('8,901')).toBeInTheDocument()
    expect(screen.getByText('$1.2345')).toBeInTheDocument()
  })

  it('renders each table’s empty state when its list is absent', () => {
    setup({})

    expect(screen.getByText('Trigger activity (30d)')).toBeInTheDocument()
    expect(screen.getByText('Review candidates')).toBeInTheDocument()
    expect(screen.getByText('Never attributed')).toBeInTheDocument()
  })
})

describe('the shared formatters', () => {
  it('pctOrDash renders an em dash for null and undefined, not 0%', () => {
    expect(pctOrDash(null)).toBe('—')
    expect(pctOrDash(undefined)).toBe('—')
    expect(pctOrDash(0)).toBe('0.0%')
  })

  it('pctOrDash scales a 0–1 fraction and honours the digits argument', () => {
    expect(pctOrDash(0.9673)).toBe('96.7%')
    expect(pctOrDash(0.9673, 2)).toBe('96.73%')
  })

  it('fmtLift always carries an explicit sign', () => {
    expect(fmtLift(0.042)).toBe('+4.2%')
    expect(fmtLift(0)).toBe('+0.0%')
  })

  it('fmtLift uses a typographic minus, not a hyphen', () => {
    // U+2212. It lines up with the + in tabular numerals; a hyphen does not.
    const out = fmtLift(-0.042)
    expect(out).toBe('−4.2%')
    expect(out.startsWith('-')).toBe(false)
  })

  it('fmtLift renders an em dash for an unknown lift', () => {
    expect(fmtLift(null)).toBe('—')
    expect(fmtLift(undefined)).toBe('—')
  })

  it('fmtWhen renders an em dash for a missing timestamp', () => {
    expect(fmtWhen(null)).toBe('—')
    expect(fmtWhen(undefined)).toBe('—')
    expect(fmtWhen('')).toBe('—')
  })

  it('fmtWhen renders a real timestamp as a locale string', () => {
    expect(fmtWhen('2026-09-05T10:00:00Z')).toBe(new Date('2026-09-05T10:00:00Z').toLocaleString())
  })
})
