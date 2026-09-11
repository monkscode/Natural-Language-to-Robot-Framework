/**
 * TestsPage — the landing list (spec section 7.2, P2 Task 8).
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

type Row = typeof PASSING | typeof FAILING | typeof CODELESS

function setup(opts: {
  rows?: Row[]
  counts?: { all: number; passing: number; failing: number }
  total?: number
  groupFilter?: string | null
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
  // health gets the wrong rows back and the test sees it.
  mockApi.mockImplementation(async (path: string) => {
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
    expect(within(drawer).getByText('Version 2')).toBeInTheDocument()
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
