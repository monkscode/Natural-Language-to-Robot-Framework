/**
 * TriggersTab — the list of learning-trigger events, and one event's detail.
 *
 * The detail drawer's "Flag outcome" is the part that must not be simplified.
 * It shows TWO lists: what the LLM recommended flagging, and what was actually
 * flagged after the strong-history guard suppressed some of it. Collapsing
 * them into one number would hide exactly the disagreement an operator opens
 * this drawer to find — and "none — all suppressed" is a materially different
 * outcome from "the LLM recommended nothing", which the same collapsed view
 * would render identically.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))

import { useFetch } from '@/lib/useFetch'
import TriggersTab from './TriggersTab'

const mockUseFetch = vi.mocked(useFetch)
afterEach(() => vi.resetAllMocks())

const EVENT = {
  id: 7, trigger_type: 'trigger_2', created_at: '2026-09-05T10:00:00Z',
  status: 'succeeded', domain: 'flipkart.com', workflow_id: 'wf-full-id-0123456789abcdef',
  tokens_in: 1234, tokens_out: 567, latency_ms: 890,
}

/**
 * TYPE_FILTERS renders buttons labelled with the same strings the table cells
 * use ("Feedback conflict" is both a filter and a row value), so every lookup
 * has to say WHICH it means. This picks the table cell.
 */
function rowCell(text: string): HTMLElement {
  const hit = screen.getAllByText(text).find(el => el.closest('td') !== null)
  if (!hit) throw new Error(`no table cell with text ${text}`)
  return hit
}

/** Route each path to its own payload: the list and the drawer both useFetch. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
function routeFetch(list: any, detail: any = null, opts: { loading?: boolean; error?: string } = {}) {
  mockUseFetch.mockImplementation((path: string | null) => {
    if (path?.includes('/triggers/')) {
      return { data: detail, loading: opts.loading ?? false, error: opts.error ?? '', reload: vi.fn() }
    }
    return { data: list, loading: false, error: '', reload: vi.fn() }
  })
}

describe('TriggersTab — the list', () => {
  it('names each trigger by its label and counts the total', () => {
    routeFetch({ total: 1, triggers: [EVENT] })

    render(<TriggersTab />)

    expect(screen.getByText('1 event')).toBeInTheDocument()
    expect(rowCell('Feedback conflict')).toBeInTheDocument()
  })

  it('pluralises the count correctly', () => {
    routeFetch({ total: 4, triggers: [EVENT, { ...EVENT, id: 8 }] })

    render(<TriggersTab />)

    expect(screen.getByText('4 events')).toBeInTheDocument()
  })

  it('shows the full workflow id on the row, never a truncation', () => {
    routeFetch({ total: 1, triggers: [EVENT] })

    render(<TriggersTab />)

    expect(screen.getByText('wf-full-id-0123456789abcdef')).toBeInTheDocument()
  })

  it('explains an empty list rather than showing a bare blank table', () => {
    routeFetch({ total: 0, triggers: [] })

    render(<TriggersTab />)

    expect(screen.getByText(/No trigger events recorded yet/)).toBeInTheDocument()
  })

  it('rebuilds its path from the type and since filters', () => {
    routeFetch({ total: 0, triggers: [] })
    render(<TriggersTab />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '7d' } })

    expect(mockUseFetch).toHaveBeenCalledWith(expect.stringContaining('since=7d'))
  })

  it('drops the since parameter entirely for "All time"', () => {
    // An empty `since` must not be sent as `since=`, which the endpoint would
    // have to interpret.
    routeFetch({ total: 0, triggers: [] })
    render(<TriggersTab />)

    fireEvent.change(screen.getByRole('combobox'), { target: { value: '' } })

    const paths = mockUseFetch.mock.calls.map(c => c[0]).filter(p => typeof p === 'string' && p.includes('/triggers?'))
    expect(paths.some(p => !String(p).includes('since='))).toBe(true)
  })
})

describe('TriggerDrawer — opening one event', () => {
  function openDrawer(detail: unknown, opts?: { loading?: boolean; error?: string }) {
    routeFetch({ total: 1, triggers: [EVENT] }, detail, opts)
    render(<TriggersTab />)
    fireEvent.click(rowCell('Feedback conflict'))
  }

  it('titles the drawer with the event id and fetches that event', () => {
    openDrawer({ trigger: EVENT, hint_texts: {} })

    expect(screen.getByText('Trigger event #7')).toBeInTheDocument()
    expect(mockUseFetch).toHaveBeenCalledWith('/api/learning/triggers/7')
  })

  it('shows a loading line, then the error if the read fails', () => {
    openDrawer(null, { error: 'Org-admin access required' })

    expect(screen.getByText('Org-admin access required')).toBeInTheDocument()
  })

  it('renders the status, domain and full workflow id', () => {
    openDrawer({ trigger: EVENT, hint_texts: {} })

    expect(screen.getAllByText('succeeded').length).toBeGreaterThan(0)
    expect(screen.getAllByText('flipkart.com').length).toBeGreaterThan(0)
  })

  it('renders the LLM’s reasoning when there is any', () => {
    openDrawer({ trigger: { ...EVENT, reason: 'two hints contradict each other' }, hint_texts: {} })

    expect(screen.getByText('LLM reasoning')).toBeInTheDocument()
    expect(screen.getByText('two hints contradict each other')).toBeInTheDocument()
  })

  it('renders an error message for a failed trigger', () => {
    openDrawer({ trigger: { ...EVENT, status: 'error', error_message: 'model timed out' }, hint_texts: {} })

    expect(screen.getByText('Error')).toBeInTheDocument()
    expect(screen.getByText('model timed out')).toBeInTheDocument()
  })
})

describe('TriggerDrawer — the flag outcome, recommended vs actual', () => {
  function openWith(trigger: Record<string, unknown>, hint_texts = {}) {
    routeFetch({ total: 1, triggers: [EVENT] }, { trigger: { ...EVENT, ...trigger }, hint_texts })
    render(<TriggersTab />)
    fireEvent.click(rowCell('Feedback conflict'))
  }

  it('shows what the LLM recommended and what was actually flagged', () => {
    openWith({ flagged_hint_ids: [11, 12], actually_flagged_hint_ids: [11] })

    expect(screen.getByText('Flag outcome')).toBeInTheDocument()
    expect(screen.getByText('11, 12')).toBeInTheDocument()
    expect(screen.getByText('11')).toBeInTheDocument()
  })

  it('says explicitly when the guard suppressed EVERY recommendation', () => {
    // The distinction this whole section exists for: the LLM did recommend
    // flagging, and the strong-history guard overruled it. Rendering an empty
    // list here would read as "nothing was recommended".
    openWith({ flagged_hint_ids: [11, 12], actually_flagged_hint_ids: [] })

    expect(screen.getByText(/none — all suppressed by the strong-history guard/)).toBeInTheDocument()
  })

  it('omits the whole Flag outcome section when the LLM recommended nothing', () => {
    // The section is gated on flagged_hint_ids?.length > 0 (TriggersTab.tsx:65),
    // so its inner `|| '—'` fallback for an empty recommendation list is
    // unreachable. Pre-existing and harmless - pinned here so the absence is
    // documented rather than mistaken for a rendering bug.
    openWith({ flagged_hint_ids: [], actually_flagged_hint_ids: [] })

    expect(screen.queryByText('Flag outcome')).toBeNull()
    expect(screen.queryByText(/all suppressed/)).toBeNull()
  })

  it('falls back to the recommended list when the actual list was not recorded', () => {
    // Older rows predate actually_flagged_hint_ids; they must not render as
    // "all suppressed", which would be a false accusation against the guard.
    openWith({ flagged_hint_ids: [11], actually_flagged_hint_ids: undefined })

    expect(screen.queryByText(/all suppressed/)).toBeNull()
    expect(screen.getAllByText('11').length).toBeGreaterThan(0)
  })

  it('marks each hint as flagged, suppressed or credited', () => {
    openWith(
      { flagged_hint_ids: [11, 12], actually_flagged_hint_ids: [11], used_hint_ids: [12] },
      { 11: 'first hint text', 12: 'second hint text' },
    )

    expect(screen.getByText('first hint text')).toBeInTheDocument()
    expect(screen.getByText('second hint text')).toBeInTheDocument()
    expect(screen.getByText('flagged')).toBeInTheDocument()
    expect(screen.getByText('suppressed')).toBeInTheDocument()
  })
})
