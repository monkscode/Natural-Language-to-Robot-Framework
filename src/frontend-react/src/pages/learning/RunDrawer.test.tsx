/**
 * RunDrawer — one run's learning journey: which hints were considered, which
 * were injected, and what each one was credited with afterwards.
 *
 * The funnel is the part worth pinning. "considered" and "injected" are
 * different populations, and a hint that was dropped carries a reason that
 * explains why the run did not benefit from it. Collapsing those into one list
 * of "hints used" would make the page agree with itself and disagree with the
 * database.
 *
 * BUCKET_BADGES also carries aliases (success/failure) for older trace rows
 * alongside the buckets the backend emits today (used/harmful/unused). An
 * unknown bucket must still render its own name rather than disappearing.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))

import { useFetch } from '@/lib/useFetch'
import RunDrawer from './RunDrawer'

const mockUseFetch = vi.mocked(useFetch)
afterEach(() => vi.resetAllMocks())

const WF = 'wf-0123456789abcdef-0123456789abcdef'

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function setup(data: any, { loading = false, error = '' } = {}) {
  mockUseFetch.mockReturnValue({ data, loading, error, reload: vi.fn() })
  render(<RunDrawer workflowId={WF} onClose={vi.fn()} />)
}

const RUN = {
  test_status: 'failed', failure_category: 'B2', timestamp: '2026-09-05T10:00:00Z',
  user_query: 'search flipkart for shoes', url: 'https://flipkart.com', domain: 'flipkart.com',
  failed_keyword: 'Click', error_message: 'element not found', model_version: 'gemini-3.5-flash',
}
const base = { run: RUN, trace: [], triggers: [] }

describe('RunDrawer — load states and identity', () => {
  it('shows the full workflow id, never a truncation', () => {
    // Standing rule in this repo: a run id shown anywhere is shown whole.
    setup(base)

    expect(screen.getByText(WF)).toBeInTheDocument()
  })

  it('shows the id even while the detail is still loading', () => {
    setup(null, { loading: true })

    expect(screen.getByText(WF)).toBeInTheDocument()
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows the error and no body when the read fails', () => {
    setup(null, { error: 'Org-admin access required' })

    expect(screen.getByText('Org-admin access required')).toBeInTheDocument()
    expect(screen.queryByText(/Hint funnel/)).toBeNull()
  })

  it('requests the run detail with the id percent-encoded', () => {
    setup(base)

    expect(mockUseFetch).toHaveBeenCalledWith(`/api/learning/runs/${encodeURIComponent(WF)}`)
  })
})

describe('RunDrawer — the run summary', () => {
  it('renders status, failure category, query, url and model', () => {
    setup(base)

    expect(screen.getByText('failed')).toBeInTheDocument()
    expect(screen.getByText('B2')).toBeInTheDocument()
    expect(screen.getByText('search flipkart for shoes')).toBeInTheDocument()
    expect(screen.getByText('https://flipkart.com')).toBeInTheDocument()
    // The drawer also dumps the raw payload lower down, so match the
    // labelled line rather than the bare model string.
    expect(screen.getByText('Model: gemini-3.5-flash')).toBeInTheDocument()
  })

  it('renders the failed keyword and its error message together', () => {
    setup(base)

    expect(screen.getByText(/Failed keyword: Click/)).toBeInTheDocument()
    expect(screen.getByText('element not found')).toBeInTheDocument()
  })

  it('names paste-and-execute runs rather than showing an empty quote', () => {
    // A blank box would read as a missing query; this run genuinely had none.
    setup({ ...base, run: { ...RUN, user_query: '' } })

    expect(screen.getByText('(paste-and-execute — no query)')).toBeInTheDocument()
  })

  it('falls back to the domain when there is no full url', () => {
    setup({ ...base, run: { ...RUN, url: '' } })

    expect(screen.getByText('flipkart.com')).toBeInTheDocument()
  })

  it('renders "unknown" for a run with no recorded status', () => {
    setup({ ...base, run: { ...RUN, test_status: '' } })

    expect(screen.getByText('unknown')).toBeInTheDocument()
  })

  it('marks a holdout run, so its result is not read as a normal one', () => {
    // A holdout run had its hints deliberately suppressed. Without the badge
    // its failure looks like evidence that learning is not working.
    setup({ ...base, metrics: { was_holdout: 1 } })

    expect(screen.getByText('holdout')).toBeInTheDocument()
  })

  it('does not mark a non-holdout run', () => {
    setup({ ...base, metrics: { was_holdout: 0 } })

    expect(screen.queryByText('holdout')).toBeNull()
  })
})

describe('RunDrawer — the hint funnel', () => {
  const injected = {
    hint_id: 11, scope: 'domain', similarity_score: 0.8342, injected: 1,
    drop_reason: null, attribution_bucket: 'used', feedback_text: 'the search box locator was off',
    attribution_reason: 'credited on a pass',
  }
  const dropped = {
    hint_id: 12, scope: 'global', similarity_score: 0.11, injected: 0,
    drop_reason: 'below similarity threshold', attribution_bucket: null,
    feedback_text: 'unrelated hint', attribution_reason: null,
  }

  it('counts every hint CONSIDERED, not only those injected', () => {
    // The funnel's whole value is the gap between the two.
    setup({ ...base, trace: [injected, dropped] })

    expect(screen.getByText('Hint funnel (2)')).toBeInTheDocument()
  })

  it('distinguishes an injected hint from a dropped one, and gives the reason', () => {
    setup({ ...base, trace: [injected, dropped] })

    expect(screen.getByText('injected')).toBeInTheDocument()
    expect(screen.getByText('dropped')).toBeInTheDocument()
    expect(screen.getByText('(below similarity threshold)')).toBeInTheDocument()
  })

  it('rounds the similarity score to a whole percent', () => {
    setup({ ...base, trace: [injected] })

    expect(screen.getByText('sim 83%')).toBeInTheDocument()
  })

  it('omits similarity entirely when it was not recorded', () => {
    setup({ ...base, trace: [{ ...injected, similarity_score: null }] })

    expect(screen.queryByText(/^sim /)).toBeNull()
  })

  it('renders the attribution bucket and its reason', () => {
    setup({ ...base, trace: [injected] })

    expect(screen.getByText('used')).toBeInTheDocument()
    expect(screen.getByText('credited on a pass')).toBeInTheDocument()
  })

  it('still renders a bucket it has no colour for', () => {
    // BUCKET_BADGES is a lookup with a fallback; a new backend bucket must
    // show its name rather than silently vanish.
    setup({ ...base, trace: [{ ...injected, attribution_bucket: 'brand_new_bucket' }] })

    expect(screen.getByText('brand_new_bucket')).toBeInTheDocument()
  })

  it('renders the legacy success/failure aliases', () => {
    setup({ ...base, trace: [{ ...injected, attribution_bucket: 'success' }] })

    expect(screen.getByText('success')).toBeInTheDocument()
  })

  it('says a hint was deleted rather than rendering a blank row', () => {
    // The trace outlives the hint. An empty line would look like a bug.
    setup({ ...base, trace: [{ ...injected, feedback_text: null }] })

    expect(screen.getByText('(hint deleted)')).toBeInTheDocument()
  })

  it('says explicitly when no hints were considered at all', () => {
    setup(base)

    expect(screen.getByText('No hints were considered for this run.')).toBeInTheDocument()
    expect(screen.getByText('Hint funnel (0)')).toBeInTheDocument()
  })
})

describe('RunDrawer — trigger events', () => {
  it('lists trigger events with their reason when there are any', () => {
    setup({ ...base, triggers: [{ id: 1, trigger_type: 'trigger_1', reason: 'fix after fail' }] })

    expect(screen.getByText('Trigger events (1)')).toBeInTheDocument()
    expect(screen.getByText('fix after fail')).toBeInTheDocument()
  })

  it('omits the section entirely when nothing fired', () => {
    setup(base)

    expect(screen.queryByText(/Trigger events/)).toBeNull()
  })
})
