/**
 * TestsPage — the landing list (spec section 7.2, P2 Task 8) and the test
 * drawer it opens (section 7.3, P2 Task 9).
 *
 * Driven through mocked useAuth / useRunGroups / api / sse, as
 * HistoryPage.test.tsx does, so nothing here reaches the network. The api
 * mock answers GET /api/tests by the `health` it is asked for, so a tab that
 * sent the wrong filter shows the wrong rows rather than passing by luck.
 *
 * What this pins: rows render from the payload (folder tag, run count, last
 * run, sparkline); every tab carries its own count and asks the server for
 * its own rows; the empty state a brand-new user lands on, and that a
 * FILTERED empty page never shows it; Run is disabled with the reason when
 * the current version has no code and fires nothing, and otherwise sends
 * {test_id} alone; the row is reachable by keyboard; Move is offered exactly
 * where can_move says and files through the tests route; and a click inside
 * Move's portalled dialog never opens the row behind it.
 *
 * And of the drawer: it reads the test it was opened on by id and believes
 * that read over the row; it lists the results newest first with every run
 * id in full; it offers a report only where the server says there is one; it
 * rules the timeline where the code changed and nowhere else, keeping a
 * result that names no version with the code that was current when it ran;
 * it pages off results_total and keeps the pages already loaded when a run
 * ends; it shows the CURRENT version's code, says so plainly when there is
 * none, and names a version's author only when the server did.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('@/components/history/RunGroupsContext', () => ({ useRunGroups: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))
vi.mock('@/lib/sse', () => ({ streamSSE: vi.fn() }))

import { useAuth } from '@/auth/AuthContext'
import { useRunGroups } from '@/components/history/RunGroupsContext'
import { ApiError, api } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import TestsPage from './TestsPage'

const mockUseAuth = vi.mocked(useAuth)
const mockUseRunGroups = vi.mocked(useRunGroups)
const mockApi = vi.mocked(api)
const mockStreamSSE = vi.mocked(streamSSE)

afterEach(() => { vi.resetAllMocks() })

const hoursAgo = (h: number) => new Date(Date.now() - h * 3_600_000).toISOString()

const PASSING = {
  test_id: 't-pass', name: null, user_query: 'search flipkart for shoes', user_email: 'a@b.com',
  group_id: 'g-1', group_name: 'Checkout', current_version: 2, version_count: 2,
  result_count: 5, pass_count: 4, last_status: 'passed', last_run_at: hoursAgo(2),
  last_run_id: 'run-9', health: 'passing' as const, running: false,
  spark: ['fail', 'pass', 'pass', 'pass', 'pass'] as Array<'pass' | 'fail'>,
  can_run: true, can_move: true,
}
const FAILING = {
  ...PASSING, test_id: 't-fail', user_query: 'log in and open orders',
  group_id: null, group_name: null, current_version: 1, version_count: 1,
  result_count: 2, pass_count: 1, last_status: 'failed', health: 'failing' as const,
  spark: ['pass', 'fail'] as Array<'pass' | 'fail'>, can_move: false,
}
// What a viewer sees of a test whose only results they cannot open, or a
// migrated test with no code: a normal row that has simply never run.
const CODELESS = {
  ...PASSING, test_id: 't-nocode', user_query: 'legacy migrated test',
  group_id: null, group_name: null, result_count: 0, pass_count: 0,
  last_status: null, last_run_at: null, last_run_id: null,
  health: 'not_run' as const, spark: [] as Array<'pass' | 'fail'>,
  can_run: false, can_move: false,
}
const GROUPS = [
  { group_id: 'g-1', name: 'Checkout', created_by: 'u1', run_count: 7, test_count: 1 },
  { group_id: 'g-2', name: 'Regression', created_by: 'u1', run_count: 3, test_count: 2 },
]

const CODE_V2 = '*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nSearch\n    New Page    https://flipkart.com'
const CODE_V1 = '*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nSearch\n    Go To    https://flipkart.com'

/** The payload shape, written out rather than inferred from the fixture
 *  below: every field the route can answer with null must be nullable here,
 *  or a case that pins the null branch fails `tsc`, which is half the
 *  frontend gate. */
interface DetailPayload {
  test: {
    test_id: string; name: string | null; user_query: string | null
    user_email: string | null; group_id: string | null; group_name: string | null
    current_version: number | null; created_at: string; updated_at: string
    health: string
  }
  versions: Array<{
    n: number | null; user_query: string | null; robot_code: string | null
    created_by_email: string | null; reason: string | null; created_at: string
    // Required, not optional: get_test_detail SELECTs it and assigns it on
    // every version, so a fixture without it is a payload the route cannot
    // produce -- which is the whole reason this local type exists.
    creator_is_platform_admin: boolean
  }>
  results: Array<{
    run_id: string; status: string; n: number | null; created_at: string
    has_report: boolean; failure_class: string | null; failure_locator: string | null
  }>
  results_total: number
}

/** GET /api/tests/{test_id} for t-pass, shaped as the route answers it (spec
 *  6.2): the test plus health, every version newest-first, ONE page of
 *  results, and results_total for the whole (caller-scoped) set. The results
 *  deliberately hold two of version 2, then a version-less row, then one of
 *  version 1 — one code change, and a result that names no version. */
const DETAIL: DetailPayload = {
  test: {
    test_id: 't-pass', name: null, user_query: 'search flipkart for shoes',
    user_email: 'a@b.com', group_id: 'g-1', group_name: 'Checkout',
    current_version: 2, created_at: hoursAgo(50), updated_at: hoursAgo(2),
    health: 'passing',
  },
  versions: [
    { n: 2, user_query: 'search flipkart for shoes', robot_code: CODE_V2, created_by_email: 'b@b.com', creator_is_platform_admin: false, reason: 'edited', created_at: hoursAgo(30) },
    { n: 1, user_query: 'search flipkart', robot_code: CODE_V1, created_by_email: 'a@b.com', creator_is_platform_admin: false, reason: null, created_at: hoursAgo(50) },
  ],
  results: [
    { run_id: '11111111-1111-4111-8111-111111111111', status: 'passed', n: 2, created_at: hoursAgo(2), has_report: true, failure_class: null, failure_locator: null },
    { run_id: '22222222-2222-4222-8222-222222222222', status: 'failed', n: 2, created_at: hoursAgo(3), has_report: true, failure_class: null, failure_locator: null },
    { run_id: '33333333-3333-4333-8333-333333333333', status: 'error', n: null, created_at: hoursAgo(4), has_report: false, failure_class: null, failure_locator: null },
    { run_id: '44444444-4444-4444-8444-444444444444', status: 'passed', n: 1, created_at: hoursAgo(5), has_report: true, failure_class: null, failure_locator: null },
  ],
  results_total: 4,
}

type Detail = DetailPayload
type Row = typeof PASSING | typeof FAILING | typeof CODELESS

/** What GET /api/tests/{test_id} answers for a row other than t-pass: the
 *  same test, told the way the detail route tells it, so a drawer opened on
 *  the wrong id shows the wrong test rather than passing by luck. */
const detailOf = (r: Row) => ({
  test: {
    test_id: r.test_id, name: r.name, user_query: r.user_query, user_email: r.user_email,
    group_id: r.group_id, group_name: r.group_name, current_version: r.current_version,
    created_at: hoursAgo(60), updated_at: r.last_run_at ?? hoursAgo(60), health: r.health,
  },
  versions: [{
    n: r.current_version, user_query: r.user_query, robot_code: r.can_run ? CODE_V1 : null,
    created_by_email: r.user_email, reason: null, created_at: hoursAgo(60),
  }],
  results: r.last_run_id && r.last_run_at ? [{
    run_id: r.last_run_id, status: r.last_status!, n: r.current_version,
    created_at: r.last_run_at, has_report: true, failure_class: null, failure_locator: null,
  }] : [],
  results_total: r.result_count,
})

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true, writable: true })
  return writeText
}

function setup(opts: {
  rows?: Row[]
  counts?: { all: number; passing: number; failing: number }
  total?: number
  groupFilter?: string | null
  // The drawer's own read. A function so a test can answer the second page
  // differently from the first, or refuse the read outright.
  detail?: Detail | ((path: string) => Detail | Promise<Detail>)
} = {}) {
  const rows = opts.rows ?? [PASSING, FAILING, CODELESS]
  const counts = opts.counts ?? {
    all: rows.length,
    passing: rows.filter(r => r.health === 'passing').length,
    failing: rows.filter(r => r.health === 'failing').length,
  }
  mockUseAuth.mockReturnValue({
    user: { id: 'u1', email: 'a@b.com', display_name: 'A', role: 'user', status: 'active' },
    loading: false, isAuthenticated: true, isAdmin: false, status: 'active',
    isOrgAdmin: false, canViewLearning: false,
    login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  })
  const spies = {
    assignTests: vi.fn().mockResolvedValue(undefined),
    refresh: vi.fn().mockResolvedValue(undefined),
    createGroup: vi.fn(),
  }
  mockUseRunGroups.mockReturnValue({
    groups: GROUPS, ungroupedCount: 11, ungroupedTestCount: 2, error: '', loaded: true,
    refresh: spies.refresh, createGroup: spies.createGroup, renameGroup: vi.fn(),
    deleteGroup: vi.fn(), assignRuns: vi.fn(), assignTests: spies.assignTests,
    groupFilter: opts.groupFilter ?? null, setGroupFilter: vi.fn(),
  })
  // Answer by the filter actually asked for: a tab that sends the wrong
  // health gets the wrong rows back and the test sees it. The drawer's read
  // is a different path shape (/api/tests/{id}) and is answered separately —
  // a list payload returned there would let a drawer that ignores its own
  // request pass.
  mockApi.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/tests/')) {
      if (opts.detail) return typeof opts.detail === 'function' ? opts.detail(path) : opts.detail
      const id = path.slice('/api/tests/'.length).split('?')[0]
      return id === 't-pass' ? DETAIL : detailOf(rows.find(r => r.test_id === id)!)
    }
    const health = new URL(path, 'http://x').searchParams.get('health')
    const page = health ? rows.filter(r => r.health === health) : rows
    return { tests: page, total: opts.total ?? page.length, counts, scope: 'own' }
  })
  return spies
}

function renderPage() {
  return render(
    <MemoryRouter initialEntries={['/tests']}>
      <Routes>
        <Route path="/tests" element={<TestsPage />} />
        <Route path="/generate" element={<span>GENERATE_PAGE</span>} />
      </Routes>
    </MemoryRouter>,
  )
}

const listCalls = () => mockApi.mock.calls.map(c => String(c[0])).filter(p => p.startsWith('/api/tests?'))
// Anchored at both ends: the folder row's "All Groups · 2 groups" control is
// also a button whose name starts with "All".
const tab = (label: string) => screen.getByRole('button', { name: new RegExp(`^${label}( \\d+)?$`) })

// Radix's dropdown trigger opens on pointerdown with button 0, which jsdom's
// fireEvent.pointerDown cannot carry — see MoveToGroupMenu.test.tsx.
const pointerDown = (el: Element) =>
  fireEvent(el, new MouseEvent('pointerdown', { bubbles: true, cancelable: true, button: 0 }))

describe('TestsPage — rows', () => {
  it('renders a row per test with its folder tag, run count and last run', async () => {
    setup()
    renderPage()

    const row = (await screen.findByRole('button', { name: 'search flipkart for shoes' })).closest('tr')!
    expect(within(row).getByText('Checkout')).toBeInTheDocument()
    expect(within(row).getByText('5')).toBeInTheDocument()
    expect(within(row).getByText('2h ago')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'log in and open orders' })).toBeInTheDocument()
  })

  it('draws the sparkline as the current version’s results, counted', async () => {
    setup()
    renderPage()

    expect(await screen.findByRole('img', {
      name: 'Last 5 completed runs of the current version you can see: 4 passed, 1 failed',
    })).toBeInTheDocument()
  })

  it('renders a test with no result the viewer can open as never run, not as an error', async () => {
    setup()
    renderPage()

    const row = (await screen.findByRole('button', { name: 'legacy migrated test' })).closest('tr')!
    expect(within(row).getByText('Never')).toBeInTheDocument()
    expect(within(row).getByText('0')).toBeInTheDocument()
    expect(within(row).getByTitle(/^Not run/)).toBeInTheDocument()
  })

  it('hides the folder tag while a folder filter is active', async () => {
    setup({ groupFilter: 'g-1' })
    renderPage()

    const row = (await screen.findByRole('button', { name: 'search flipkart for shoes' })).closest('tr')!
    expect(within(row).queryByText('Checkout')).not.toBeInTheDocument()
    expect(listCalls().some(p => p.includes('group=g-1'))).toBe(true)
  })

  it('shows a test that is running, from the server’s flag', async () => {
    setup({ rows: [{ ...PASSING, running: true }] })
    renderPage()

    const row = (await screen.findByRole('button', { name: 'search flipkart for shoes' })).closest('tr')!
    expect(within(row).getByText('Running')).toBeInTheDocument()
  })
})

describe('TestsPage — the health tabs', () => {
  it('carries a count on every tab, whichever tab is open', async () => {
    setup({ counts: { all: 12, passing: 7, failing: 3 } })
    renderPage()

    await screen.findByRole('button', { name: 'search flipkart for shoes' })
    expect(tab('All').textContent).toContain('12')
    expect(tab('Failing').textContent).toContain('3')
    expect(tab('Passing').textContent).toContain('7')
  })

  it('asks the server for the open tab’s rows', async () => {
    setup()
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    fireEvent.click(tab('Failing'))

    await waitFor(() => expect(screen.queryByRole('button', { name: 'search flipkart for shoes' })).not.toBeInTheDocument())
    expect(screen.getByRole('button', { name: 'log in and open orders' })).toBeInTheDocument()
    expect(listCalls().some(p => p.includes('health=failing'))).toBe(true)
    expect(tab('Failing')).toHaveAttribute('aria-pressed', 'true')
  })

  it('sends the search to the server', async () => {
    setup()
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    fireEvent.change(screen.getByLabelText('Search tests'), { target: { value: ' shoes ' } })

    await waitFor(() => expect(listCalls().some(p => p.includes('q=shoes'))).toBe(true))
  })

  it('offers the next page at the offset it has reached', async () => {
    setup({ total: 5 })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    fireEvent.click(screen.getByRole('button', { name: /Load more/ }))

    await waitFor(() => expect(listCalls().some(p => p.includes('offset=3'))).toBe(true))
  })
})

describe('TestsPage — the empty state', () => {
  it('explains what a test is and offers Generate to a caller with none', async () => {
    setup({ rows: [], counts: { all: 0, passing: 0, failing: 0 } })
    renderPage()

    expect(await screen.findByText('No tests yet')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Generate your first test/ }))
    expect(await screen.findByText('GENERATE_PAGE')).toBeInTheDocument()
  })

  it('does not tell a caller WITH tests that they have none when a tab is empty', async () => {
    setup({ rows: [PASSING], counts: { all: 1, passing: 1, failing: 0 } })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    fireEvent.click(tab('Failing'))

    expect(await screen.findByText('No failing tests.')).toBeInTheDocument()
    expect(screen.queryByText('No tests yet')).not.toBeInTheDocument()
  })

  it('does not greet a search with no matches as a brand-new user', async () => {
    // counts honour the search, so a search that matches nothing reads
    // all: 0 — the same number a caller with no tests at all gets.
    setup()
    mockApi.mockImplementation(async (path: string) => (
      new URL(path, 'http://x').searchParams.get('q')
        ? { tests: [], total: 0, counts: { all: 0, passing: 0, failing: 0 }, scope: 'own' }
        : { tests: [PASSING], total: 1, counts: { all: 1, passing: 1, failing: 0 }, scope: 'own' }
    ))
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    fireEvent.change(screen.getByLabelText('Search tests'), { target: { value: 'zzz' } })

    expect(await screen.findByText('No tests match your search.')).toBeInTheDocument()
    expect(screen.queryByText('No tests yet')).not.toBeInTheDocument()
  })
})

describe('TestsPage — Run', () => {
  it('is disabled with the reason when the current version has no code, and fires nothing', async () => {
    setup()
    renderPage()

    const run = await screen.findByRole('button', { name: /^Run legacy migrated test/ })
    expect(run).toBeDisabled()
    expect(run.closest('[title]')!.getAttribute('title')).toMatch(/no runnable code/)
    fireEvent.click(run)
    expect(mockStreamSSE).not.toHaveBeenCalled()
  })

  it('sends the test_id alone, and refreshes the list once the run exists', async () => {
    setup()
    // The stream stays OPEN after its first event, so the refresh seen below
    // can only have come from that event — not from the one every run makes
    // when its stream ends.
    let finish: () => void = () => {}
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ stage: 'execution', status: 'running', run_id: 'run-new' })
      await new Promise<void>(resolve => { finish = resolve })
    })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })
    const before = listCalls().length

    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))

    await waitFor(() => expect(listCalls().length).toBeGreaterThan(before))
    expect(mockStreamSSE.mock.calls[0][0]).toBe('/execute-test')
    expect(mockStreamSSE.mock.calls[0][1]).toEqual({ test_id: 't-pass' })
    // While its own run is in flight the row says so and cannot fire twice.
    const row = screen.getByRole('button', { name: 'search flipkart for shoes' }).closest('tr')!
    expect(within(row).getByText('Running')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run search flipkart for shoes' })).toBeDisabled()

    finish()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Run search flipkart for shoes' })).not.toBeDisabled())
  })

  it('says which run failed, with its full id, when execution errors', async () => {
    setup()
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ stage: 'execution', status: 'error', run_id: '3f2b0c1e-8d4a-4c7e-9b1a-2e6f5d4c3b2a', message: 'runner unreachable' })
    })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))

    expect(await screen.findByText(
      'Run of “search flipkart for shoes” failed (run 3f2b0c1e-8d4a-4c7e-9b1a-2e6f5d4c3b2a): runner unreachable',
    )).toBeInTheDocument()
  })

  it('shows the server’s own words when it refuses to start the run', async () => {
    setup()
    mockStreamSSE.mockRejectedValue(new ApiError(409, 'This test has no runnable code in its current version — regenerate it first.'))
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))

    expect(await screen.findByText(/Couldn’t run “search flipkart for shoes” — This test has no runnable code/)).toBeInTheDocument()
  })

  // The 409 above is deliberately NOT access loss, so it leaves the row in
  // place — which is why it could never cover the two guards below. Both
  // exist to stop a PHANTOM row: one the caller has lost access to, still
  // carrying Run, Update and Move, every one of which can now only 404. The
  // row survives refreshLoaded's merge on purpose (it keeps the Load-more
  // tail alive), so dropping it takes an explicit id.
  it('drops a row the server has stopped showing after a refused run', async () => {
    setup()
    // The colleague unfiled the test between the page load and the click:
    // the run is refused 404, and the row is gone from the next list page.
    let listed = [PASSING, FAILING]
    mockApi.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/tests/')) return DETAIL as never
      return {
        tests: listed, total: listed.length, scope: 'own',
        counts: { all: listed.length, passing: 1, failing: 1 },
      } as never
    })
    mockStreamSSE.mockRejectedValue(new ApiError(404, 'Test not found'))
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    listed = [FAILING]
    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))

    // The banner is not the point — the row going away is.
    expect(await screen.findByText(/Couldn’t run “search flipkart for shoes”/)).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: 'search flipkart for shoes' })).toBeNull()
    })
    // The control, in the same test: the row the server still lists stays.
    expect(screen.getByRole('button', { name: 'log in and open orders' })).toBeInTheDocument()
  })

  // Owner ruling R7-5: the new run's id rides the FIRST execution event,
  // because /execute-test is a stream with no response body to carry it.
  // Both of these pin the KEY rather than the event: a client that refreshed
  // on `ev.stage === 'execution'` instead passed every existing test, and the
  // server could drop the key with its own suite green, so the two halves
  // could drift to green independently. The server half is in
  // tests/test_services/test_execution_progress_order.py.
  it('refreshes on the first event that NAMES the run, not merely on a stage', async () => {
    setup()
    // The stream is held OPEN so the assertion lands while the run is still
    // in flight. Counting reloads after it closes cannot tell the two rules
    // apart: the finally block reloads either way, and a waitFor on the
    // total passes on the transient value before the second one arrives.
    let release: () => void = () => {}
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      // An execution-stage event with NO run_id: as far as this client can
      // tell the row does not exist yet, so there is nothing to refresh for.
      onEvent({ stage: 'execution', status: 'running', message: 'Preparing…' })
      await new Promise<void>(resolve => { release = resolve })
    })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })
    const before = listCalls().length

    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))

    // In flight, and the event has been delivered — a client keying off
    // ev.stage would already have reloaded by now.
    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Run search flipkart for shoes' })).toBeDisabled()
    })
    expect(listCalls().length).toBe(before)

    release()
    // ...and the finally block still reloads exactly once when it ends.
    await waitFor(() => expect(listCalls().length).toBe(before + 1))
  })

  it('names the run in the failure banner from the event, not from a guess', async () => {
    setup()
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ stage: 'execution', status: 'running', run_id: 'run-42' })
      onEvent({ stage: 'execution', status: 'error', run_id: 'run-42', message: 'boom' })
    })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })
    const before = listCalls().length

    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))

    expect(await screen.findByText(
      'Run of “search flipkart for shoes” failed (run run-42): boom',
    )).toBeInTheDocument()
    // The id-bearing event refreshed the list WHILE the run was in flight —
    // one more reload than the no-run_id case above.
    await waitFor(() => expect(listCalls().length).toBe(before + 2))
  })

  it('keeps a row the server still lists when the run merely errored', async () => {
    // The known negative for the test above: a 503 is not access loss, so
    // the row must survive even though the refreshed page happens not to
    // carry it — that is what the Load-more tail is for.
    setup()
    let listed = [PASSING, FAILING]
    mockApi.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/tests/')) return DETAIL as never
      return {
        tests: listed, total: listed.length, scope: 'own',
        counts: { all: listed.length, passing: 1, failing: 1 },
      } as never
    })
    mockStreamSSE.mockRejectedValue(new ApiError(503, 'Docker is unavailable.'))
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    listed = [FAILING]
    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))

    expect(await screen.findByText(/Couldn’t run “search flipkart for shoes”/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'search flipkart for shoes' })).toBeInTheDocument()
  })
})

describe('TestsPage — opening a test', () => {
  it('reaches the drawer from a real button, so the keyboard can too', async () => {
    setup()
    renderPage()

    const name = await screen.findByRole('button', { name: 'search flipkart for shoes' })
    expect(name.tagName).toBe('BUTTON')
    fireEvent.click(name)

    const drawer = await screen.findByRole('dialog')
    expect(within(drawer).getByText('search flipkart for shoes')).toBeInTheDocument()
    expect(within(drawer).getByText('Passing')).toBeInTheDocument()
    expect(within(drawer).getByText('Current version 2')).toBeInTheDocument()
    expect(within(drawer).getByText('In Checkout')).toBeInTheDocument()
  })

  it('opens from a click anywhere on the row', async () => {
    setup()
    renderPage()
    const row = (await screen.findByRole('button', { name: 'log in and open orders' })).closest('tr')!

    fireEvent.click(within(row).getByText('2'))

    const drawer = await screen.findByRole('dialog')
    expect(within(drawer).getByText('Failing')).toBeInTheDocument()
    expect(within(drawer).getByText('Not in a group')).toBeInTheDocument()
  })
})

/* ────────────────────────────────────────────────────────────────────────
 * Task 9 — the drawer's own read (spec 7.3, GET /api/tests/{test_id}).
 * ──────────────────────────────────────────────────────────────────────── */

const detailCalls = () => mockApi.mock.calls.map(c => String(c[0])).filter(p => p.startsWith('/api/tests/'))
const openDrawer = async (name = 'search flipkart for shoes') => {
  fireEvent.click(await screen.findByRole('button', { name }))
  return screen.findByRole('dialog')
}
/** The run ids on screen, in the order the timeline lists them. */
const timelineIds = (drawer: HTMLElement) =>
  within(drawer).getAllByTitle('Copy run id').map(b => b.textContent!.trim())
/** Each result row, in the same order — the id's own row, so a row is read
 *  whole rather than by a word that repeats across results. */
const timelineRows = (drawer: HTMLElement) =>
  within(drawer).getAllByTitle('Copy run id').map(b => b.closest('li')!)
/** The timeline exactly as it is laid out: each result as its run id, each
 *  version rule as RULE:<its label>, in order. */
const timelineItems = (drawer: HTMLElement) =>
  [...within(drawer).getByRole('list', { name: 'Results, newest first' }).children].map(li => {
    const id = within(li as HTMLElement).queryByTitle('Copy run id')
    return id ? id.textContent!.trim() : `RULE:${li.textContent!.trim()}`
  })

describe('TestsPage — the drawer’s results timeline', () => {
  it('reads the open test by id and lists its results newest first', async () => {
    setup()
    renderPage()

    const drawer = await openDrawer()

    await waitFor(() => expect(detailCalls()).toContain('/api/tests/t-pass?limit=50&offset=0'))
    await waitFor(() => expect(timelineIds(drawer)).toEqual([
      '11111111-1111-4111-8111-111111111111',
      '22222222-2222-4222-8222-222222222222',
      '33333333-3333-4333-8333-333333333333',
      '44444444-4444-4444-8444-444444444444',
    ]))
    const rows = timelineRows(drawer)
    expect(rows.map(li => within(li).getByText(/^(Passed|Failed|Error|Generated|Running)$/).textContent))
      .toEqual(['Passed', 'Failed', 'Error', 'Passed'])
    // The version each result RAN, including the one that names none.
    expect(rows.map(li => within(li).getByText(/^(v\d+|no version)$/).textContent))
      .toEqual(['v2', 'v2', 'no version', 'v1'])
  })

  it('asks for nothing until a test is opened', async () => {
    setup()
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    expect(detailCalls()).toEqual([])
  })

  it('shows every run id in full and copies the one that was clicked', async () => {
    const writeText = stubClipboard()
    setup()
    renderPage()
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineIds(drawer)).toHaveLength(4))

    fireEvent.click(within(drawer).getAllByTitle('Copy run id')[1])

    await waitFor(() => expect(writeText).toHaveBeenCalledWith('22222222-2222-4222-8222-222222222222'))
  })

  it('offers a report only for the results the server says have one', async () => {
    setup()
    renderPage()
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineIds(drawer)).toHaveLength(4))

    const reports = within(drawer).getAllByRole('link', { name: /report/i })
    expect(reports.map(a => a.getAttribute('href'))).toEqual([
      '/reports/11111111-1111-4111-8111-111111111111/log.html',
      '/reports/22222222-2222-4222-8222-222222222222/log.html',
      '/reports/44444444-4444-4444-8444-444444444444/log.html',
    ])
  })

  it('believes the detail, not the row it was opened from', async () => {
    // Same test, read twice: the row said passing, the drawer's own read says
    // failing. The later read wins — a result can land between the two.
    setup({ detail: { ...DETAIL, test: { ...DETAIL.test, health: 'failing', group_name: 'Regression' } } })
    renderPage()

    const drawer = await openDrawer()

    await waitFor(() => expect(within(drawer).getByText('Failing')).toBeInTheDocument())
    expect(within(drawer).getByText('In Regression')).toBeInTheDocument()
    expect(within(drawer).queryByText('Passing')).not.toBeInTheDocument()
  })

  it('shows the server’s refusal instead of a timeline it could not read', async () => {
    setup({ detail: () => Promise.reject(new ApiError(404, 'Test not found')) as never })
    renderPage()

    const drawer = await openDrawer()

    expect(await within(drawer).findByText('Test not found')).toBeInTheDocument()
    expect(within(drawer).queryByTitle('Copy run id')).not.toBeInTheDocument()
  })

  it('re-reads the open test when a run of it ends', async () => {
    setup()
    let finish: () => void = () => {}
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ stage: 'execution', status: 'running', run_id: 'run-new' })
      await new Promise<void>(resolve => { finish = resolve })
    })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })
    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineIds(drawer)).toHaveLength(4))
    const before = detailCalls().length

    finish()

    await waitFor(() => expect(detailCalls().length).toBeGreaterThan(before))
  })
})

describe('TestsPage — the drawer’s version rules', () => {
  it('rules off where the code changed, and nowhere else', async () => {
    // Two results of version 2, then a version-less one, then version 1:
    // ONE code change, so ONE rule — never one per result.
    setup()
    renderPage()
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineRows(drawer)).toHaveLength(4))

    expect(timelineItems(drawer)).toEqual([
      '11111111-1111-4111-8111-111111111111',
      '22222222-2222-4222-8222-222222222222',
      'RULE:Version 1',
      '33333333-3333-4333-8333-333333333333',
      '44444444-4444-4444-8444-444444444444',
    ])
  })

  it('keeps a result that names no version with the code that was current when it ran', async () => {
    // The version-less row above sits BELOW the rule, with version 1: a
    // regeneration that failed left the old code in place, so a streak
    // across it is not broken. Here there is nothing older to inherit from,
    // so it stands in its own block and says so.
    setup({
      detail: {
        ...DETAIL,
        results: DETAIL.results.slice(0, 1).concat(DETAIL.results.slice(2, 3)),
        results_total: 2,
      },
    })
    renderPage()
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineRows(drawer)).toHaveLength(2))

    expect(timelineItems(drawer)).toEqual([
      '11111111-1111-4111-8111-111111111111',
      'RULE:No version recorded',
      '33333333-3333-4333-8333-333333333333',
    ])
  })
})

describe('TestsPage — paging the drawer’s timeline', () => {
  const PAGE_TWO = {
    ...DETAIL,
    results: [{
      run_id: '55555555-5555-4555-8555-555555555555', status: 'passed', n: 1,
      created_at: hoursAgo(9), has_report: false, failure_class: null, failure_locator: null,
    }],
    results_total: 5,
  }

  it('offers the rest off results_total and appends them at the right offset', async () => {
    setup({
      detail: (path: string) => (path.includes('offset=4')
        ? PAGE_TWO
        : { ...DETAIL, results_total: 5 }),
    })
    renderPage()
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineRows(drawer)).toHaveLength(4))

    fireEvent.click(within(drawer).getByRole('button', { name: 'Load more (1 more)' }))

    await waitFor(() => expect(timelineRows(drawer)).toHaveLength(5))
    expect(detailCalls().some(p => p.includes('offset=4'))).toBe(true)
    // Newest first: an older page belongs at the BOTTOM, never on top of
    // the results already on screen.
    expect(timelineIds(drawer)[4]).toBe('55555555-5555-4555-8555-555555555555')
  })

  it('replaces the timeline with the server’s words when a re-read fails', async () => {
    // A re-read that fails is not a blip to paper over: the commonest reason
    // is that this caller may no longer open the test, and going on showing
    // its results would be a lie about what they can see.
    let fail = false
    setup({ detail: () => (fail ? Promise.reject(new ApiError(404, 'Test not found')) : DETAIL) as never })
    let finish: () => void = () => {}
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ stage: 'execution', status: 'running', run_id: 'run-new' })
      await new Promise<void>(resolve => { finish = resolve })
    })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })
    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineRows(drawer)).toHaveLength(4))

    fail = true
    finish()

    expect(await within(drawer).findByText('Test not found')).toBeInTheDocument()
    expect(within(drawer).queryByTitle('Copy run id')).not.toBeInTheDocument()
  })

  it('offers nothing more when the timeline already holds every result', async () => {
    setup()
    renderPage()
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineRows(drawer)).toHaveLength(4))

    expect(within(drawer).queryByRole('button', { name: /Load more/ })).not.toBeInTheDocument()
  })

  it('keeps the pages already loaded when a run of the test ends', async () => {
    setup({
      detail: (path: string) => (path.includes('offset=4')
        ? PAGE_TWO
        : { ...DETAIL, results_total: 5 }),
    })
    let finish: () => void = () => {}
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ stage: 'execution', status: 'running', run_id: 'run-new' })
      await new Promise<void>(resolve => { finish = resolve })
    })
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })
    fireEvent.click(screen.getByRole('button', { name: 'Run search flipkart for shoes' }))
    const drawer = await openDrawer()
    await waitFor(() => expect(timelineRows(drawer)).toHaveLength(4))
    fireEvent.click(within(drawer).getByRole('button', { name: 'Load more (1 more)' }))
    await waitFor(() => expect(timelineRows(drawer)).toHaveLength(5))

    finish()

    // The refresh re-reads page zero; the page the user asked for stays.
    await waitFor(() => expect(detailCalls().filter(p => p.includes('offset=0')).length).toBeGreaterThan(1))
    expect(timelineIds(drawer)).toContain('55555555-5555-4555-8555-555555555555')
  })
})

describe('TestsPage — the drawer’s code and versions', () => {
  it('shows the code of the CURRENT version, not of the newest one', async () => {
    // current_version names version 1 while version 2 is the newest row —
    // reading versions[0] would show code this test does not run.
    setup({ detail: { ...DETAIL, test: { ...DETAIL.test, current_version: 1 } } })
    renderPage()
    const drawer = await openDrawer()

    const code = await within(drawer).findByRole('region', { name: 'Robot code' })
    expect(code.textContent).toContain('Go To    https://flipkart.com')
    expect(code.textContent).not.toContain('New Page    https://flipkart.com')
  })

  it('copies the current version’s code', async () => {
    const writeText = stubClipboard()
    setup()
    renderPage()
    const drawer = await openDrawer()

    fireEvent.click(await within(drawer).findByRole('button', { name: 'Copy code' }))

    await waitFor(() => expect(writeText).toHaveBeenCalledWith(CODE_V2))
  })

  it('says the current version has no code rather than showing an empty block', async () => {
    setup({
      detail: {
        ...DETAIL,
        test: { ...DETAIL.test, current_version: 1 },
        versions: [{ ...DETAIL.versions[1], robot_code: null }],
      },
    })
    renderPage()
    const drawer = await openDrawer()

    expect(await within(drawer).findByText(/no runnable code in its current version/)).toBeInTheDocument()
    expect(within(drawer).queryByRole('button', { name: 'Copy code' })).not.toBeInTheDocument()
  })

  it('lists every version, newest first, with who wrote it and why', async () => {
    setup()
    renderPage()
    const drawer = await openDrawer()

    const list = await within(drawer).findByRole('list', { name: 'Versions, newest first' })
    const rows = [...list.children].map(li => li.textContent!.replace(/\s+/g, ' ').trim())
    expect(rows).toHaveLength(2)
    expect(rows[0]).toContain('Version 2')
    expect(rows[0]).toContain('current')
    expect(rows[0]).toContain('b@b.com')
    expect(rows[0]).toContain('description edited')
    expect(rows[1]).toContain('Version 1')
    expect(rows[1]).toContain('a@b.com')
    expect(rows[1]).not.toContain('current')
  })

  it('names no author rather than the wrong one when the server gives none', async () => {
    setup({
      detail: { ...DETAIL, versions: [{ ...DETAIL.versions[0], created_by_email: null }] },
    })
    renderPage()
    const drawer = await openDrawer()

    const list = await within(drawer).findByRole('list', { name: 'Versions, newest first' })
    expect(list.textContent).toContain('author unknown')
    expect(list.textContent).not.toContain('a@b.com')
  })

  it('says Platform admin where the server withheld the address under D7', async () => {
    // The server blanks created_by_email on a version made with
    // platform-admin authority and leaves the flag, exactly as it does for a
    // run. Activity already labels that case; the drawer must not fall
    // through to "author unknown", which says less than the server knows.
    setup({
      detail: {
        ...DETAIL,
        versions: [{
          ...DETAIL.versions[0],
          created_by_email: null,
          creator_is_platform_admin: true,
        }],
      },
    })
    renderPage()
    const drawer = await openDrawer()

    const list = await within(drawer).findByRole('list', { name: 'Versions, newest first' })
    expect(list.textContent).toContain('Platform admin')
    expect(list.textContent).not.toContain('author unknown')
  })
})

describe('TestsPage — Move', () => {
  it('is offered exactly where can_move is true', async () => {
    setup()
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    expect(screen.getByRole('button', { name: 'Move search flipkart for shoes to a group' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Move log in and open orders to a group' })).not.toBeInTheDocument()
  })

  it('files the test through the tests route', async () => {
    const spies = setup()
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    pointerDown(screen.getByRole('button', { name: 'Move search flipkart for shoes to a group' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /Regression/ }))

    await waitFor(() => expect(spies.assignTests).toHaveBeenCalledWith(['t-pass'], 'g-2'))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('never opens the row behind Move’s New-group dialog', async () => {
    setup()
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    pointerDown(screen.getByRole('button', { name: 'Move search flipkart for shoes to a group' }))
    fireEvent.click(await screen.findByRole('menuitem', { name: /New group/ }))
    const dialog = await screen.findByRole('dialog')
    expect(within(dialog).getByText(/The selected tests move into it/)).toBeInTheDocument()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})

/* ─────────────────────────────────────────────────────────────────────────
 * Task 10 — the Update dialog (spec 7.4, POST /api/tests/{test_id}/versions).
 * ─────────────────────────────────────────────────────────────────────── */

const openUpdate = async (name = 'search flipkart for shoes') => {
  fireEvent.click(await screen.findByRole('button', { name: `Update ${name}` }))
  return screen.findByRole('dialog', { name: 'Update test' })
}
const description = (dialog: HTMLElement) =>
  within(dialog).getByRole('textbox', { name: 'Description' }) as HTMLTextAreaElement

describe('TestsPage — the Update dialog', () => {
  it('is offered exactly where can_move is true', async () => {
    setup()
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    expect(screen.getByRole('button', { name: 'Update search flipkart for shoes' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Update log in and open orders' })).not.toBeInTheDocument()
    // A test with no runnable code is exactly one you would regenerate — it
    // is withheld here only because THIS row may not be moved.
    expect(screen.queryByRole('button', { name: 'Update legacy migrated test' })).not.toBeInTheDocument()
  })

  it('reads the test itself and prefills the description the server holds', async () => {
    setup()
    renderPage()

    const dialog = await openUpdate()

    // limit=1: the newest result is the one whose failure motivated the
    // update, and it is the route's own floor (it clamps to [1, 200]).
    await waitFor(() => expect(detailCalls()).toContain('/api/tests/t-pass?limit=1&offset=0'))
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
  })

  it('shows the current version’s code for reference and lets nobody edit it', async () => {
    setup()
    renderPage()

    const dialog = await openUpdate()

    const code = await within(dialog).findByRole('region', { name: 'Robot code' })
    expect(code.textContent).toContain('New Page    https://flipkart.com')
    // One editable control in the whole dialog: the description. The code
    // block is a <pre>, so it cannot be one.
    expect(within(dialog).getAllByRole('textbox')).toHaveLength(1)
  })

  it('writes nothing when the dialog is cancelled', async () => {
    setup()
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.change(description(dialog), { target: { value: 'a completely different test' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }))

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update test' })).not.toBeInTheDocument())
    expect(mockStreamSSE).not.toHaveBeenCalled()
  })

  it('offers no write while the description is empty', async () => {
    setup()
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.change(description(dialog), { target: { value: '   ' } })

    expect(within(dialog).getByRole('button', { name: 'Update this test' })).toBeDisabled()
    expect(within(dialog).getByRole('button', { name: 'Save as a new test' })).toBeDisabled()
  })

  it('sends the description as typed, under the mode the button names', async () => {
    setup()
    mockStreamSSE.mockResolvedValue(undefined)
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    // Sent untrimmed on purpose: the route trims, and it decides from the
    // trimmed comparison whether this version reads 'regenerated' or
    // 'edited'. One rule, and it is the server's.
    fireEvent.change(description(dialog), { target: { value: '  search flipkart for boots  ' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    await waitFor(() => expect(mockStreamSSE).toHaveBeenCalled())
    expect(mockStreamSSE.mock.calls[0][0]).toBe('/api/tests/t-pass/versions')
    expect(mockStreamSSE.mock.calls[0][1]).toEqual({
      user_query: '  search flipkart for boots  ', mode: 'update',
    })
  })

  it('asks for a new test when that is the button pressed', async () => {
    setup()
    mockStreamSSE.mockResolvedValue(undefined)
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save as a new test' }))

    await waitFor(() => expect(mockStreamSSE).toHaveBeenCalled())
    expect(mockStreamSSE.mock.calls[0][1]).toEqual({
      user_query: 'search flipkart for shoes', mode: 'new_test',
    })
  })

  it('claims nothing when generation finishes but no version has landed', async () => {
    setup()
    let finish: () => void = () => {}
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', message: 'Finding elements on the page…' })
      // Generation's own terminal event. It says the model produced code,
      // which is a different fact from "version n+1 exists".
      onEvent({ status: 'complete', robot_code: 'NEW CODE', workflow_id: 'wf-1' })
      await new Promise<void>(resolve => { finish = resolve })
    })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    expect(await within(dialog).findByText('Saving the new version…')).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Update test' })).toBeInTheDocument()
    finish()
  })

  it('cannot be dismissed while the generation is in flight', async () => {
    setup()
    let finish: () => void = () => {}
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', message: 'Planning the steps…' })
      await new Promise<void>(resolve => { finish = resolve })
    })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    expect(await within(dialog).findByText('Planning the steps…')).toBeInTheDocument()
    // The server keeps generating whatever the client does, so every way
    // out is withheld: a version landing with the page told nothing is the
    // dishonesty this prevents.
    expect(within(dialog).queryByRole('button', { name: 'Cancel' })).not.toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: 'Close' })).not.toBeInTheDocument()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    expect(screen.getByRole('dialog', { name: 'Update test' })).toBeInTheDocument()
    finish()
  })

  it('closes and refreshes only once the server confirms the version', async () => {
    const spies = setup()
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'complete', robot_code: 'NEW CODE', workflow_id: 'wf-1' })
      onEvent({ stage: 'version', status: 'complete', test_id: 't-pass', n: 3, run_id: 'wf-1' })
    })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    const before = listCalls().length
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update test' })).not.toBeInTheDocument())
    await waitFor(() => expect(listCalls().length).toBeGreaterThan(before))
    expect(spies.refresh).toHaveBeenCalled()
  })

  it('says why, and keeps what was typed, when generation fails', async () => {
    setup()
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', message: 'Planning the steps…' })
      onEvent({ status: 'error', message: 'Vertex refused the request (429)' })
    })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.change(description(dialog), { target: { value: 'log in and open orders' } })
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    expect(await within(dialog).findByText('Vertex refused the request (429)')).toBeInTheDocument()
    // Still open, still holding their words: no version was written, so a
    // retry must not cost them the edit.
    expect(description(dialog).value).toBe('log in and open orders')
  })

  it('repeats the server’s words when the version could not be confirmed', async () => {
    const MSG = 'Generation finished, but the new version could not be confirmed. Reload the test list to see whether it is there.'
    setup()
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'complete', robot_code: 'NEW CODE', workflow_id: 'wf-1' })
      onEvent({ stage: 'version', status: 'error', run_id: 'wf-1', message: MSG })
    })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    expect(await within(dialog).findByText(MSG)).toBeInTheDocument()
    expect(screen.getByRole('dialog', { name: 'Update test' })).toBeInTheDocument()
  })

  it('does not read a silent stream as success', async () => {
    setup()
    mockStreamSSE.mockResolvedValue(undefined)
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    expect(await within(dialog).findByText(
      'The server ended the stream without saying whether a version was written. Reload the list to see whether it is there.',
    )).toBeInTheDocument()
  })

  it('shows the server’s own refusal, and asks the list for nothing', async () => {
    setup()
    mockStreamSSE.mockRejectedValue(new ApiError(404, 'Test not found'))
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    const before = listCalls().length
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    expect(await within(dialog).findByText('Test not found')).toBeInTheDocument()
    // A refusal is answered before the stream starts, so no run of this
    // test exists to refresh the list for.
    expect(listCalls().length).toBe(before)
  })
})

/** Answers the /versions stream with a generation that produced `code` and
 *  then the terminal version event the route builds from its read-back;
 *  every other stream (the follow-up run) gets one execution event. Records
 *  [path, body] per call, in order. */
function updateStream(opts: { testId: string; n: number; runId: string; code?: string | null }) {
  const calls: Array<[string, unknown]> = []
  mockStreamSSE.mockImplementation(async (path, body, onEvent) => {
    calls.push([String(path), body])
    if (String(path).endsWith('/versions')) {
      if (opts.code !== null) {
        onEvent({ status: 'complete', robot_code: opts.code ?? 'NEW CODE', workflow_id: opts.runId })
      }
      onEvent({ stage: 'version', status: 'complete', test_id: opts.testId, n: opts.n, run_id: opts.runId })
      return
    }
    onEvent({ stage: 'execution', status: 'running', run_id: opts.runId })
  })
  return calls
}
const runAfter = (dialog: HTMLElement) =>
  within(dialog).getByRole('checkbox', { name: /Run the new version once it is saved/ })

describe('TestsPage — the Update dialog’s first run', () => {
  it('runs the new version through the one form that records learning', async () => {
    setup()
    const calls = updateStream({ testId: 't-pass', n: 3, runId: 'wf-1' })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    expect(runAfter(dialog)).toBeChecked()
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    await waitFor(() => expect(calls).toHaveLength(2))
    // The generation's OWN workflow_id, so the run reuses that row rather
    // than minting a second one, and a user_query, which is the whole of
    // what makes this run teach anything. {test_id} — what the row's Run
    // sends — would be a re-run and would record nothing.
    expect(calls[1]).toEqual(['/execute-test', {
      robot_code: 'NEW CODE', workflow_id: 'wf-1', user_query: 'search flipkart for shoes',
    }])
  })

  it('runs nothing when the reader unticks it', async () => {
    setup()
    const calls = updateStream({ testId: 't-pass', n: 3, runId: 'wf-1' })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(runAfter(dialog))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update test' })).not.toBeInTheDocument())
    expect(calls.map(c => c[0])).toEqual(['/api/tests/t-pass/versions'])
  })

  it('does not try to run a version whose code it never saw', async () => {
    setup()
    // The version landed, but this stream never carried the code — and
    // /execute-test is given the code itself, not a version to look up.
    const calls = updateStream({ testId: 't-pass', n: 3, runId: 'wf-1', code: null })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update test' })).not.toBeInTheDocument())
    expect(calls.map(c => c[0])).toEqual(['/api/tests/t-pass/versions'])
  })

  it('opens the test the server named, not the one it was opened on', async () => {
    setup()
    // mode 'new_test' mints a different test, and the terminal event names
    // THAT one. A test with no result sorts last (last_run_at DESC NULLS
    // LAST), so a refresh alone can leave it off the loaded page entirely.
    const calls = updateStream({ testId: 't-fail', n: 1, runId: 'wf-2' })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(runAfter(dialog))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save as a new test' }))

    await waitFor(() => expect(detailCalls()).toContain('/api/tests/t-fail?limit=50&offset=0'))
    expect(calls[0][1]).toEqual({ user_query: 'search flipkart for shoes', mode: 'new_test' })
  })

  it('says the version was saved even when running it fails', async () => {
    setup()
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (String(path).endsWith('/versions')) {
        onEvent({ status: 'complete', robot_code: 'NEW CODE', workflow_id: 'wf-1' })
        onEvent({ stage: 'version', status: 'complete', test_id: 't-pass', n: 3, run_id: 'wf-1' })
        return
      }
      onEvent({ stage: 'execution', status: 'error', run_id: 'wf-1', message: 'runner unreachable' })
    })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    // Two facts, and they are not the same fact: the version IS written.
    expect(await screen.findByText(
      'Version 3 of “search flipkart for shoes” was saved. Running it failed (run wf-1): runner unreachable',
    )).toBeInTheDocument()
  })

  it('refreshes the list when a regeneration fails, because its run attaches to the test', async () => {
    setup()
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', message: 'Planning the steps…' })
      onEvent({ status: 'error', message: 'Vertex refused the request (429)' })
    })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    const before = listCalls().length
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    await within(dialog).findByText('Vertex refused the request (429)')
    // Spec case 10 / 37(b): no version is written, and the run that failed
    // is still attached to this test — so the test has one more result than
    // it did, and the list is where that shows.
    await waitFor(() => expect(listCalls().length).toBeGreaterThan(before))
  })

  it('does not move the drawer when an existing test is updated', async () => {
    setup()
    updateStream({ testId: 't-pass', n: 3, runId: 'wf-1' })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    fireEvent.click(runAfter(dialog))
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update test' })).not.toBeInTheDocument())
    // Only a test that did not exist a moment ago is opened. Updating in
    // place changes the row the reader is already looking at, and going and
    // opening it is not what they asked for.
    expect(detailCalls().filter(c => c.includes('limit=50'))).toEqual([])
  })

  it('says the version is saved when the run cannot even start, and still refreshes', async () => {
    setup()
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (String(path).endsWith('/versions')) {
        onEvent({ status: 'complete', robot_code: 'NEW CODE', workflow_id: 'wf-1' })
        onEvent({ stage: 'version', status: 'complete', test_id: 't-pass', n: 3, run_id: 'wf-1' })
        return
      }
      throw new ApiError(503, 'Docker is unavailable.')
    })
    renderPage()

    const dialog = await openUpdate()
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    const before = listCalls().length
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    expect(await screen.findByText(
      'Version 3 of “search flipkart for shoes” was saved, but the run could not start — Docker is unavailable.',
    )).toBeInTheDocument()
    // This run never reached an event carrying its id, so the refresh made
    // when the stream ends is the only one there is.
    await waitFor(() => expect(listCalls().length).toBeGreaterThan(before))
  })
})

describe('TestsPage — Update from the drawer', () => {
  it('is offered on the same term the row is', async () => {
    setup()
    renderPage()

    const drawer = await openDrawer()
    expect(await within(drawer).findByRole('button', { name: 'Update' })).toBeInTheDocument()

    fireEvent.keyDown(drawer, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    // can_move false on this row, and GET /api/tests/{id} answers no
    // can_move of its own — so the control is withheld rather than offered
    // and refused.
    const other = await openDrawer('log in and open orders')
    await within(other).findByRole('list', { name: 'Results, newest first' })
    expect(within(other).queryByRole('button', { name: 'Update' })).not.toBeInTheDocument()
  })

  it('opens over the drawer, on the test the drawer is showing', async () => {
    setup()
    renderPage()

    const drawer = await openDrawer()
    fireEvent.click(await within(drawer).findByRole('button', { name: 'Update' }))

    const dialog = await screen.findByRole('dialog', { name: 'Update test' })
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    // The drawer is still there behind it, timeline and all: the dialog
    // is the confirm step for the test being read, not a replacement for
    // reading it. Measured: Radix marks the layer underneath
    // aria-hidden while the dialog is over it, which is why this looks
    // at the element rather than asking for its role.
    expect(drawer).toBeInTheDocument()
    expect(drawer.getAttribute('aria-hidden')).toBe('true')
    expect(within(drawer).queryAllByTitle('Copy run id')).toHaveLength(4)
  })

  it('leaves the open drawer to re-read once a version lands', async () => {
    setup()
    updateStream({ testId: 't-pass', n: 3, runId: 'wf-1' })
    renderPage()

    const drawer = await openDrawer()
    fireEvent.click(await within(drawer).findByRole('button', { name: 'Update' }))
    const dialog = await screen.findByRole('dialog', { name: 'Update test' })
    await waitFor(() => expect(description(dialog).value).toBe('search flipkart for shoes'))
    const before = detailCalls().filter(p => p.includes('limit=50')).length
    fireEvent.click(within(dialog).getByRole('button', { name: 'Update this test' }))

    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Update test' })).not.toBeInTheDocument())
    await waitFor(() => expect(
      detailCalls().filter(p => p.includes('limit=50')).length,
    ).toBeGreaterThan(before))
  })
})

describe('TestsPage — the folder chips', () => {
  it('count tests, not runs', async () => {
    // The shared state carries both: 11 results and 2 tests are in no
    // folder. This page files tests, so its chips say 2.
    setup()
    renderPage()
    await screen.findByRole('button', { name: 'search flipkart for shoes' })

    const ungrouped = screen.getByTitle('Show only tests that are in no group')
    expect(ungrouped.textContent).toContain('· 2')
    expect(ungrouped.textContent).not.toContain('11')
  })
})
