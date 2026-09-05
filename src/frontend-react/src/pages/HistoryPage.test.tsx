/**
 * HistoryPage — the drawer's corrections fetch (Task 2 of
 * feedback-visibility). One thin wiring test, not page coverage: this file
 * had no test before this task. The fixture is the minimum needed to render
 * the page — MemoryRouter (HistoryPage calls useNavigate for Regenerate),
 * plus useAuth/useRunGroups/useFetch/api mocked so nothing here makes a real
 * network call — mirroring the mock shape LearningPage.test.tsx and
 * HintDrawer.test.tsx already use for the same three hooks.
 *
 * What this pins: opening a run in the drawer fires
 * GET /api/feedback/{run_id}; a successful read renders the correction
 * text on screen, and — when `applied_to` differs from the open row — the
 * "filed against the original run" notice with its full, untruncated id;
 * a refused read renders no error text (the behaviour GeneratePage's
 * FeedbackPanel already proved once for its own surface); and a row
 * switch never shows a PREVIOUS row's corrections under the newly-selected
 * row — not while that row's own fetch is still in flight, not once it has
 * settled to an error, and not in the single stale commit between the row
 * changing and the run-detail fetch catching up to it (useFetch clears
 * neither `data` nor, on the error path, anything at all beyond `error`
 * itself — see HistoryPage.tsx's comment above `feedbackPath` for why
 * `loading`, `error` AND `d` all have to gate `corrections`). The two
 * positive-rendering tests exist because the others are all
 * absence-assertions, which a mutation that hardcodes
 * `corrections` to `[]` sails through undetected — see the `it`s below for
 * that finding's own account. This file does not re-test
 * RecordedCorrections' own rendering rules (RecordedCorrections.test.tsx
 * already does that) or the rest of the page.
 */
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('@/components/history/RunGroupsContext', () => ({ useRunGroups: vi.fn() }))
vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))
vi.mock('@/lib/sse', () => ({ streamSSE: vi.fn() }))

import { useAuth } from '@/auth/AuthContext'
import { useRunGroups } from '@/components/history/RunGroupsContext'
import { useFetch } from '@/lib/useFetch'
import { api } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import HistoryPage from './HistoryPage'

const mockUseAuth = vi.mocked(useAuth)
const mockUseRunGroups = vi.mocked(useRunGroups)
const mockUseFetch = vi.mocked(useFetch)
const mockApi = vi.mocked(api)
const mockStreamSSE = vi.mocked(streamSSE)

afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals() })

const RUN = {
  run_id: 'run-1', status: 'passed' as const, user_query: 'search flipkart for shoes',
  created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
  has_report: false, can_move: false,
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function setup(feedbackFetch: { data: any; error: string; loading?: boolean }, detailRunId: string = RUN.run_id, detailExtra: Record<string, unknown> = {}) {
  mockUseAuth.mockReturnValue({
    user: { id: 'u1', email: 'a@b.com', display_name: 'A', role: 'user', status: 'active' },
    loading: false, isAuthenticated: true, isAdmin: false, status: 'active',
    isOrgAdmin: false, canViewLearning: false,
    login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  })
  mockUseRunGroups.mockReturnValue({
    groups: [], ungroupedCount: 0, error: '', loaded: true,
    refresh: vi.fn(), createGroup: vi.fn(), renameGroup: vi.fn(),
    deleteGroup: vi.fn(), assignRuns: vi.fn(),
    groupFilter: null, setGroupFilter: vi.fn(),
  })
  mockApi.mockResolvedValue({ runs: [RUN], total: 1, scope: 'own' })
  // Keyed by path: the drawer fires useFetch twice (run detail + this
  // task's corrections fetch), and only the second is this test's subject.
  // The detail fetch defaults to a payload for the OPENED row itself
  // (detailRunId defaults to RUN.run_id) — HistoryPage.tsx only trusts
  // `detail` once its own run_id echoes `selected` (its `d`), and now folds
  // `d` into `corrections` too, so a caller that passes a mismatched
  // detailRunId reproduces the stale-commit window on demand.
  mockUseFetch.mockImplementation((path: string | null) => {
    if (path?.startsWith('/api/feedback/')) {
      return { data: feedbackFetch.data, loading: feedbackFetch.loading ?? false, error: feedbackFetch.error, reload: vi.fn() }
    }
    return { data: { ...RUN, run_id: detailRunId, robot_code: null, ...detailExtra }, loading: false, error: '', reload: vi.fn() }
  })
}

async function openDrawer() {
  render(<MemoryRouter><HistoryPage /></MemoryRouter>)
  fireEvent.click(await screen.findByText('search flipkart for shoes'))
  await waitFor(() => expect(mockUseFetch).toHaveBeenCalledWith('/api/feedback/run-1'))
}

describe('HistoryPage drawer — the corrections fetch', () => {
  it('fires GET /api/feedback/{run_id} when a row is opened', async () => {
    setup({ data: null, error: '' })

    await openDrawer()

    // openDrawer's own waitFor is the assertion; this is just its
    // documented restatement.
    expect(mockUseFetch).toHaveBeenCalledWith('/api/feedback/run-1')
  })

  it('renders no error text when the corrections read is refused (403 — a published run whose corrections stay private)', async () => {
    setup({ data: null, error: 'You cannot read feedback for this run' })

    await openDrawer()

    // The page has an unrelated "Error" status filter button, so this checks
    // the specific refusal text rather than a bare /error/i (which would
    // false-positive on that button).
    expect(screen.queryByText(/cannot read feedback/i)).toBeNull()
  })

  it('does not render a previous row’s corrections while this row’s fetch is still in flight', async () => {
    // The corrections payload carries no run_id of its own (only
    // `applied_to`, the resolved ORIGINAL run — see HistoryPage.tsx), so a
    // just-left row's stale `data` can only be caught via `loading`, not by
    // comparing an echoed id the way the run-detail fetch does. loading:
    // true is useFetch's real state for exactly this window: the effect for
    // the newly-selected row's path has fired, but that fetch has not
    // resolved yet.
    setup({
      data: { applied_to: 'run-1', corrections: [{ hint_id: 1, feedback_text: 'stale from the last row' }] },
      error: '', loading: true,
    })

    await openDrawer()

    expect(screen.queryByText(/stale from the last row/)).toBeNull()
  })

  it('does not render a previous row’s stale corrections when this row’s fetch fails (e.g. a 403 refusing a colleague’s shared run)', async () => {
    // useFetch never clears `data` in its catch branch (useFetch.ts) — only
    // `error` is set, and `loading` is already back to false by then. This
    // is the fix-round-1 repro: open an owned run with corrections on
    // file, then a colleague's shared run whose corrections read the
    // server refuses (GET /api/history/{id} passes is_grouped=true and
    // 200s; GET /api/feedback/{id} deliberately does not and 403s). A
    // loading-only guard is blind to this: `loading` has already settled
    // false by the time the failure lands, so the FIRST run's stale `data`
    // — its correction text, and a "filed against" notice that may
    // describe a run that isn't even a re-run — would render under the
    // SECOND run's drawer.
    setup({
      data: {
        applied_to: 'run-A',
        corrections: [{ hint_id: 1, feedback_text: 'ROW A STALE TEXT MUST NOT APPEAR' }],
      },
      error: 'You cannot read feedback for this run',
      loading: false,
    })

    await openDrawer()

    expect(screen.queryByText(/ROW A STALE TEXT MUST NOT APPEAR/)).toBeNull()
    expect(screen.queryByText(/Filed against the original run/)).toBeNull()
  })

  it('does not render a previous row’s corrections in the single stale commit before the detail fetch has caught up to the newly-selected row', async () => {
    // This is the one window `loading`/`error` alone cannot see: feedback
    // for the new row has already landed and settled (loading: false,
    // error: '') carrying a real correction, but the run-detail fetch
    // (mocked via detailRunId below) still names a DIFFERENT run — the
    // render between `selected` changing and useFetch's own effect firing,
    // where useFetch still returns the PREVIOUS path's fully settled state.
    // Only `d`, folded into `corrections` in HistoryPage.tsx, catches this.
    setup(
      {
        data: {
          applied_to: 'run-9',
          corrections: [{ hint_id: 1, feedback_text: 'STALE COMMIT TEXT MUST NOT APPEAR' }],
        },
        error: '', loading: false,
      },
      'run-DIFFERENT-FROM-OPENED-ROW',
    )

    await openDrawer()

    expect(screen.queryByText(/STALE COMMIT TEXT MUST NOT APPEAR/)).toBeNull()
    expect(screen.queryByText(/Filed against the original run/)).toBeNull()
  })

  // The five tests above are all absence-assertions (no fetch call target,
  // no error text, no stale text across three different staleness windows)
  // — every one of them still passes if `corrections` were hardcoded to
  // [], which would silently disable this task's whole feature. These two
  // are the ones that actually prove a correction reaches the screen.
  it('renders a correction’s text once the fetch succeeds', async () => {
    setup({
      data: { applied_to: 'run-1', corrections: [{ hint_id: 1, feedback_text: 'the search box locator was off' }] },
      error: '', loading: false,
    })

    await openDrawer()

    expect(await screen.findByText(/the search box locator was off/)).toBeInTheDocument()
  })

  it('renders the "filed against the original run" notice with the full, untruncated id', async () => {
    // Long and clearly not an 8-char prefix, so a truncation regression
    // (the owner's standing "never truncate a run id" rule) would be
    // visible as a failed exact-text match, not just a shorter match.
    const ORIGINAL_ID = 'run-A-0123456789abcdef0123456789'
    setup({
      data: { applied_to: ORIGINAL_ID, corrections: [{ hint_id: 1, feedback_text: 'x' }] },
      error: '', loading: false,
    })

    await openDrawer()

    expect(await screen.findByText(/Filed against the original run/)).toBeInTheDocument()
    expect(screen.getByText(ORIGINAL_ID)).toBeInTheDocument()
  })
})

describe('HistoryPage drawer — the re-run-of id', () => {
  // The owner's standing rule is unconditional: a run/workflow id shown in
  // any UI is full AND click-to-copy. The accessible branch already satisfies
  // it — that id is a button (it opens the original). The INACCESSIBLE
  // branch deliberately is not a link, because it would 404, and it lost the
  // copy control along with the navigation. Losing the link is right; losing
  // the copy is not, and this is the case where copying matters most: the
  // only thing a user can still do with an id they cannot open is paste it to
  // someone who can.
  const ORIGINAL_ID = 'run-orig-0123456789abcdef0123456789'

  it('gives the id a copy control even when the original is inaccessible', async () => {
    setup({ data: null, error: '' }, RUN.run_id, { rerun_of: ORIGINAL_ID, rerun_of_accessible: false })

    await openDrawer()

    const id = await screen.findByText(ORIGINAL_ID)
    expect(id.closest('button')).not.toBeNull()
  })

  it('still shows that id in full, and does not turn it into an open-the-original link', async () => {
    setup({ data: null, error: '' }, RUN.run_id, { rerun_of: ORIGINAL_ID, rerun_of_accessible: false })

    await openDrawer()

    // Full id, not a prefix.
    const id = await screen.findByText(ORIGINAL_ID)
    expect(id.textContent).toContain(ORIGINAL_ID)
    // Copy, not navigate: the accessible branch's button is titled "Open the
    // original run", and this one must not be.
    expect(id.closest('button')!.getAttribute('title')).toMatch(/copy/i)
  })
})

/* ────────────────────────────────────────────────────────────────────────
 * Task 5 (frontend-coverage-gate) — the rest of the page: the runs table,
 * filters/search/paging, select mode and moving runs, re-running a row, and
 * the drawer's remaining actions (download/copy/move). The block above only
 * ever renders ONE row and never opens select mode or clicks a row action,
 * so none of it is touched by the tests above — this is new coverage, not a
 * rewrite. Two rows (RUN_A owned by the viewer, RUN_B a colleague's) are the
 * standard fixture so every "acts on the right row" assertion has a WRONG
 * row on screen to fail against, not just an absent one.
 * ──────────────────────────────────────────────────────────────────────── */

const RUN_A = {
  run_id: 'run-A', status: 'passed' as const, user_query: 'search flipkart for shoes',
  user_email: 'me@x.com', created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
  has_report: true, can_move: true,
}
const RUN_B = {
  run_id: 'run-B', status: 'failed' as const, user_query: 'checkout flow',
  user_email: 'colleague@x.com', created_at: '2026-08-30T00:00:00Z', updated_at: '2026-08-30T00:00:00Z',
  has_report: false, can_move: false,
}
const CHECKOUT = { group_id: 'g-1', name: 'Checkout', created_by: 'u-me', run_count: 0 }
const CODE = '*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nSearch\n    New Page    https://example.com'

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true, writable: true })
  return writeText
}

/** URL.createObjectURL/revokeObjectURL don't exist in jsdom, jsdom's own
 *  Blob has no .text()/.arrayBuffer() to read one back with, and a real
 *  anchor .click() logs a "Not implemented: navigation" jsdom error. Replace
 *  Blob with a constructor that just records what it was built from —
 *  nothing downstream needs a real blob, since createObjectURL is stubbed
 *  too (mirrors GeneratePage.test.tsx's stubDownload). */
function stubDownload() {
  let parts: unknown[] | null = null
  let anchor: HTMLAnchorElement | null = null
  vi.stubGlobal('Blob', vi.fn().mockImplementation((p: unknown[]) => { parts = p; return {} }))
  URL.createObjectURL = vi.fn(() => 'blob:mock-url') as typeof URL.createObjectURL
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { anchor = this })
  return { getParts: () => parts, getAnchor: () => anchor }
}

// Radix's DropdownMenuTrigger (MoveToGroupMenu) opens on pointerdown, not
// click — see MoveToGroupMenu.test.tsx for why a plain fireEvent.click /
// fireEvent.pointerDown do not open it under jsdom.
function pointerDown(el: Element) {
  fireEvent(el, new MouseEvent('pointerdown', { bubbles: true, cancelable: true, button: 0 }))
}

/** The table + toolbar fixture: a real (unmocked) useAuth/useRunGroups pair
 *  wired to spies, and api() answering only /api/history — anything else is
 *  a bug in the test, not a real endpoint, so it throws loudly instead of
 *  hanging. mockUseFetch defaults to "nothing" for every path; tests that
 *  open the drawer and care about its content override it afterwards. */
function setupList(runs: unknown[], opts: {
  total?: number; scope?: 'own' | 'all'
  // Typed off useRunGroups' own return, not `unknown[]`: this fixture is
  // handed straight to the mocked context, so an unknown[] here silently
  // stops TypeScript checking every group fixture in this file.
  groups?: ReturnType<typeof useRunGroups>['groups']; ungroupedCount?: number
  groupFilter?: string | null; groupsError?: string; isAdmin?: boolean
  assignRuns?: ReturnType<typeof vi.fn>
} = {}) {
  mockUseAuth.mockReturnValue({
    user: { id: 'u-me', email: 'me@x.com', display_name: 'Me', role: 'user', status: 'active' },
    loading: false, isAuthenticated: true, isAdmin: opts.isAdmin ?? false, status: 'active',
    isOrgAdmin: false, canViewLearning: false,
    login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  })
  const spies = {
    assignRuns: opts.assignRuns ?? vi.fn().mockResolvedValue(undefined),
    createGroup: vi.fn().mockResolvedValue({ group_id: 'g-new', name: 'New', created_by: 'u-me', run_count: 0 }),
    renameGroup: vi.fn().mockResolvedValue(undefined),
    deleteGroup: vi.fn().mockResolvedValue(undefined),
    refreshGroups: vi.fn().mockResolvedValue(undefined),
    setGroupFilter: vi.fn(),
  }
  mockUseRunGroups.mockReturnValue({
    groups: opts.groups ?? [], ungroupedCount: opts.ungroupedCount ?? 0, error: opts.groupsError ?? '', loaded: true,
    refresh: spies.refreshGroups, createGroup: spies.createGroup, renameGroup: spies.renameGroup,
    deleteGroup: spies.deleteGroup, assignRuns: spies.assignRuns,
    groupFilter: opts.groupFilter ?? null, setGroupFilter: spies.setGroupFilter,
  })
  mockApi.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/history')) return { runs, total: opts.total ?? runs.length, scope: opts.scope ?? 'own' }
    throw new Error(`setupList: unexpected api(${path})`)
  })
  mockUseFetch.mockImplementation(() => ({ data: null, loading: false, error: '', reload: vi.fn() }))
  return spies
}

function renderPage() {
  return render(<MemoryRouter><HistoryPage /></MemoryRouter>)
}

/** Points the /api/history/{runId} useFetch call at a real detail payload
 *  (robot_code included) for exactly one run — for the drawer actions that
 *  only appear once `d` has resolved. */
function withDetail(runId: string, extra: Record<string, unknown> = {}) {
  mockUseFetch.mockImplementation((path: string | null) => {
    if (path === `/api/history/${runId}`) {
      return { data: { ...RUN_A, run_id: runId, robot_code: CODE, ...extra }, loading: false, error: '', reload: vi.fn() }
    }
    return { data: null, loading: false, error: '', reload: vi.fn() }
  })
}

const RUN_AGAIN_TITLE = 'Run again — execute the saved code as-is (no regeneration)'

describe('HistoryPage — the runs table', () => {
  it('renders each run’s description and the "N of M" counter', async () => {
    setupList([RUN_A, RUN_B], { total: 2 })
    renderPage()

    expect(await screen.findByText('search flipkart for shoes')).toBeInTheDocument()
    expect(screen.getByText('checkout flow')).toBeInTheDocument()
    expect(screen.getByText('2 of 2 runs')).toBeInTheDocument()
  })

  it('shows the loading placeholder before the first page resolves', async () => {
    setupList([RUN_A])
    mockApi.mockImplementation(() => new Promise(() => {}))
    renderPage()

    expect(await screen.findByText('Loading runs…')).toBeInTheDocument()
  })

  it('shows the server’s error and no rows when the list request fails', async () => {
    setupList([RUN_A])
    mockApi.mockRejectedValue(new Error('Server exploded'))
    renderPage()

    expect(await screen.findByText('Server exploded')).toBeInTheDocument()
    expect(screen.queryByText('search flipkart for shoes')).toBeNull()
  })

  it('shows the all-runs empty state with no rows and no error', async () => {
    setupList([], { total: 0 })
    renderPage()

    expect(await screen.findByText('No test runs yet — generate your first test from the Generate page.')).toBeInTheDocument()
  })

  it('shows "No runs match your search." when a text search returns nothing', async () => {
    setupList([RUN_A])
    renderPage()
    await screen.findByText('search flipkart for shoes')
    mockApi.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/history')) return { runs: [], total: 0, scope: 'own' }
      throw new Error(`unexpected api(${path})`)
    })

    fireEvent.change(screen.getByPlaceholderText(/Search description/), { target: { value: 'nonexistent' } })

    expect(await screen.findByText('No runs match your search.', {}, { timeout: 1000 })).toBeInTheDocument()
  })

  it('shows the ungrouped-empty message when the Ungrouped filter has no runs', async () => {
    setupList([], { total: 0, groupFilter: 'ungrouped' })
    renderPage()

    expect(await screen.findByText('No ungrouped runs — everything is filed.')).toBeInTheDocument()
  })

  it('shows the group-empty message when a specific group has no runs', async () => {
    setupList([], { total: 0, groupFilter: 'g-1' })
    renderPage()

    expect(await screen.findByText('No runs in this group yet — move runs here with the folder button on any row.')).toBeInTheDocument()
  })

  it('falls back to "No {filter} runs yet." for a non-all filter with no matches', async () => {
    setupList([RUN_A])
    renderPage()
    await screen.findByText('search flipkart for shoes')
    mockApi.mockImplementation(async (path: string) => {
      if (path.startsWith('/api/history')) return { runs: [], total: 0, scope: 'own' }
      throw new Error(`unexpected api(${path})`)
    })

    fireEvent.click(screen.getByRole('button', { name: 'failed' }))

    expect(await screen.findByText('No failed runs yet.')).toBeInTheDocument()
  })

  it('re-queries the server, rather than filtering client-side, when a status filter is clicked', async () => {
    setupList([RUN_A])
    renderPage()
    await screen.findByText('search flipkart for shoes')

    fireEvent.click(screen.getByRole('button', { name: 'failed' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith(expect.stringContaining('status=failed')))
  })

  it('debounces typed search into a q= query parameter', async () => {
    setupList([RUN_A])
    renderPage()
    await screen.findByText('search flipkart for shoes')

    fireEvent.change(screen.getByPlaceholderText(/Search description/), { target: { value: 'flipkart' } })

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith(expect.stringContaining('q=flipkart')), { timeout: 1000 })
  })

  it('Refresh reloads both the run list and the folder counts', async () => {
    const spies = setupList([RUN_A])
    renderPage()
    await screen.findByText('search flipkart for shoes')
    const callsBefore = mockApi.mock.calls.length

    fireEvent.click(screen.getByRole('button', { name: /Refresh/ }))

    expect(spies.refreshGroups).toHaveBeenCalled()
    await waitFor(() => expect(mockApi.mock.calls.length).toBeGreaterThan(callsBefore))
  })

  it('"Load more" requests the next page at the correct offset', async () => {
    setupList([RUN_A], { total: 2 })
    renderPage()
    await screen.findByText('search flipkart for shoes')

    fireEvent.click(screen.getByRole('button', { name: /Load more/ }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith(expect.stringContaining('offset=1')))
  })

  it.each([
    [true, /All users. test runs \(admin view\)/],
    [false, /Every test run in your organization/],
  ])('shows the right scope=all subtitle for isAdmin=%s', async (isAdmin, expected) => {
    setupList([RUN_A], { scope: 'all', isAdmin })
    renderPage()

    expect(await screen.findByText(expected)).toBeInTheDocument()
  })

  it('shows the "your work in progress" subtitle for scope=own', async () => {
    setupList([RUN_A])
    renderPage()

    expect(await screen.findByText(/Your work in progress, plus every test your team has filed/)).toBeInTheDocument()
  })
})

describe('HistoryPage — who ran it', () => {
  it('hides the "Ran by" column when every loaded run is the viewer’s own', async () => {
    setupList([{ ...RUN_A, user_email: 'me@x.com' }])
    renderPage()
    await screen.findByText('search flipkart for shoes')

    expect(screen.queryByText('Ran by')).toBeNull()
  })

  it('shows "Ran by" once any row belongs to someone else, and filters by that email on click', async () => {
    setupList([RUN_A, RUN_B])
    renderPage()
    await screen.findByText('search flipkart for shoes')
    expect(screen.getByText('Ran by')).toBeInTheDocument()

    fireEvent.click(screen.getByText('colleague@x.com'))

    expect(screen.getByPlaceholderText(/Search description/)).toHaveValue('colleague@x.com')
  })
})

describe('HistoryPage — the table’s run id column (never truncated, click-to-copy)', () => {
  it('shows each row’s FULL id and copies the CLICKED row’s id, without opening its drawer', async () => {
    const writeText = stubClipboard()
    setupList([RUN_A, RUN_B])
    renderPage()
    const rowB = (await screen.findByText('checkout flow')).closest('tr')!
    // Full id on screen, not an 8-char prefix.
    expect(within(rowB).getByText('run-B')).toBeInTheDocument()

    fireEvent.click(within(rowB).getByTitle('Click to copy the run id'))

    expect(writeText).toHaveBeenCalledWith('run-B')
    expect(writeText).not.toHaveBeenCalledWith('run-A')
    // e.stopPropagation() on the copy button kept the drawer closed.
    expect(screen.queryByTitle('Copy run id')).toBeNull()
  })

  it('opens the CLICKED row’s drawer, not a different row’s', async () => {
    setupList([RUN_A, RUN_B])
    renderPage()
    await screen.findByText('search flipkart for shoes')

    fireEvent.click(screen.getByText('checkout flow'))

    const drawerId = await screen.findByTitle('Copy run id')
    expect(drawerId.textContent).toContain('run-B')
    expect(drawerId.textContent).not.toContain('run-A')
  })
})

describe('HistoryPage — select mode and moving runs', () => {
  const RUN_C = { ...RUN_A, run_id: 'run-C', user_query: 'add to cart', can_move: true }

  it('disables the checkbox for a row this caller may not move, and select-all skips it', async () => {
    setupList([RUN_A, RUN_B]) // A can_move true, B can_move false
    renderPage()
    await screen.findByText('search flipkart for shoes')
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))

    const checkboxB = screen.getByLabelText('Select run checkout flow (run-B)')
    expect(checkboxB).toBeDisabled()
    expect(checkboxB).not.toBeChecked()

    fireEvent.click(screen.getByLabelText('Select all loaded runs'))

    expect(screen.getByLabelText('Select run search flipkart for shoes (run-A)')).toBeChecked()
    expect(checkboxB).not.toBeChecked()
  })

  it('moves only the checked, movable runs — with all of their ids — into the chosen group', async () => {
    const spies = setupList([RUN_A, RUN_C], { groups: [CHECKOUT] })
    renderPage()
    await screen.findByText('search flipkart for shoes')
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    fireEvent.click(screen.getByLabelText('Select run search flipkart for shoes (run-A)'))
    fireEvent.click(screen.getByLabelText('Select run add to cart (run-C)'))

    pointerDown(screen.getByRole('button', { name: /Move 2 to…/ }))
    fireEvent.click(screen.getByText('Checkout'))

    await waitFor(() => expect(spies.assignRuns).toHaveBeenCalled())
    const [ids, groupId] = spies.assignRuns.mock.calls[0]
    expect(new Set(ids)).toEqual(new Set(['run-A', 'run-C']))
    expect(groupId).toBe('g-1')
    // A successful bulk move exits select mode.
    await waitFor(() => expect(screen.getByRole('button', { name: 'Select' })).toBeInTheDocument())
  })

  it('Cancel clears the selection and exits select mode without moving anything', async () => {
    const spies = setupList([RUN_A], { groups: [CHECKOUT] })
    renderPage()
    await screen.findByText('search flipkart for shoes')
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    fireEvent.click(screen.getByLabelText('Select run search flipkart for shoes (run-A)'))

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(spies.assignRuns).not.toHaveBeenCalled()
    expect(screen.queryByLabelText('Select all loaded runs')).toBeNull()
    expect(screen.getByRole('button', { name: 'Select' })).toBeInTheDocument()
  })

  it('shows the server’s rejection and keeps the selection when a move fails', async () => {
    const assignRuns = vi.fn().mockRejectedValue(new Error('That folder was deleted'))
    setupList([RUN_A], { groups: [CHECKOUT], assignRuns })
    renderPage()
    await screen.findByText('search flipkart for shoes')
    fireEvent.click(screen.getByRole('button', { name: 'Select' }))
    fireEvent.click(screen.getByLabelText('Select run search flipkart for shoes (run-A)'))
    pointerDown(screen.getByRole('button', { name: /Move 1 to…/ }))
    fireEvent.click(screen.getByText('Checkout'))

    expect(await screen.findByText('That folder was deleted')).toBeInTheDocument()
    expect(screen.getByLabelText('Select run search flipkart for shoes (run-A)')).toBeChecked()
  })

  it('per-row move sends only that row’s id, even with other rows on the table', async () => {
    const spies = setupList([RUN_A, RUN_C], { groups: [CHECKOUT] })
    renderPage()
    const rowC = (await screen.findByText('add to cart')).closest('tr')!

    pointerDown(within(rowC).getByTitle('Move to group…'))
    fireEvent.click(screen.getByText('Checkout'))

    expect(spies.assignRuns).toHaveBeenCalledWith(['run-C'], 'g-1')
  })
})

describe('HistoryPage — running a row again', () => {
  it('sends rerun_of for the CLICKED row’s id, not any other row on the table', async () => {
    setupList([RUN_A, RUN_B])
    mockStreamSSE.mockImplementation(() => new Promise(() => {}))
    renderPage()
    const rowB = (await screen.findByText('checkout flow')).closest('tr')!

    fireEvent.click(within(rowB).getByTitle(RUN_AGAIN_TITLE))

    await waitFor(() => expect(mockStreamSSE).toHaveBeenCalledWith(
      '/execute-test', { rerun_of: 'run-B' }, expect.any(Function),
    ))
    expect(mockStreamSSE).not.toHaveBeenCalledWith(
      '/execute-test', { rerun_of: 'run-A' }, expect.any(Function),
    )
  })

  it('shows the pass note in the drawer and refreshes the list once the re-run succeeds', async () => {
    setupList([RUN_A])
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => { onEvent({ test_status: 'passed' }) })
    renderPage()
    const rowA = (await screen.findByText('search flipkart for shoes')).closest('tr')!
    const callsBefore = mockApi.mock.calls.length

    fireEvent.click(within(rowA).getByTitle(RUN_AGAIN_TITLE))

    await screen.findByText('✓ Re-run passed')
    await waitFor(() => expect(mockApi.mock.calls.length).toBeGreaterThan(callsBefore))
  })

  it('disables that row’s Run-again button while its re-run is still in flight', async () => {
    setupList([RUN_A])
    mockStreamSSE.mockImplementation(() => new Promise(() => {}))
    renderPage()
    const rowA = (await screen.findByText('search flipkart for shoes')).closest('tr')!
    const playBtn = within(rowA).getByTitle(RUN_AGAIN_TITLE)

    fireEvent.click(playBtn)

    await waitFor(() => expect(playBtn).toBeDisabled())
  })

  it('shows the failure message and re-enables the row when the re-run cannot start', async () => {
    setupList([RUN_A])
    mockStreamSSE.mockRejectedValue(new Error('Server refused the re-run'))
    renderPage()
    const rowA = (await screen.findByText('search flipkart for shoes')).closest('tr')!
    const playBtn = within(rowA).getByTitle(RUN_AGAIN_TITLE)

    fireEvent.click(playBtn)

    await screen.findByText('Server refused the re-run')
    await waitFor(() => expect(playBtn).not.toBeDisabled())
  })

  it('clicking the Re-run pill opens the ORIGINAL run’s drawer, not the re-run’s own', async () => {
    const RUN_RERUN = { ...RUN_A, run_id: 'run-RERUN', rerun_of: 'run-ORIGINAL', rerun_of_accessible: true }
    setupList([RUN_RERUN])
    renderPage()
    const row = (await screen.findByText('search flipkart for shoes')).closest('tr')!

    fireEvent.click(within(row).getByTitle('Re-run of run-ORIGINAL — click to open the original run'))

    const idBtn = await screen.findByTitle('Copy run id')
    expect(idBtn.textContent).toContain('run-ORIGINAL')
    expect(idBtn.textContent).not.toContain('run-RERUN')
  })
})

describe('HistoryPage — the drawer’s remaining actions', () => {
  it('Download builds a .robot file containing the exact stored code, named after the run', async () => {
    const { getParts, getAnchor } = stubDownload()
    // Local fixture with id longer than 8 chars to test truncation.
    const RUN_WITH_LONG_ID = { ...RUN_A, run_id: 'run-A-0123456789abc' }
    setupList([RUN_WITH_LONG_ID])
    withDetail('run-A-0123456789abc')
    renderPage()
    fireEvent.click(await screen.findByText('search flipkart for shoes'))
    const downloadBtn = await screen.findByTitle('Download .robot file')

    // The drawer's code panel itself renders the stored code, not just the
    // download it produces. Radix's Sheet portals its content onto
    // document.body, outside the render container, so query the document.
    expect(document.querySelector('pre')?.textContent).toBe(CODE)

    fireEvent.click(downloadBtn)

    // Filename truncates run id to 8 chars: test-<first 8>-.robot.
    // This is a filename convention (not displayed in UI), distinct from the
    // "never truncate displayed ids" rule.
    const filename = getAnchor()?.download
    expect(filename).toBe('test-run-A-01.robot')
    // Ensure truncation happens: 9th char onward must NOT appear in filename.
    expect(filename).not.toContain('23456789')
    expect(getParts()).toEqual([CODE])
  })

  it('Copy code copies the exact stored code, not the query text', async () => {
    const writeText = stubClipboard()
    setupList([RUN_A])
    withDetail('run-A')
    renderPage()
    fireEvent.click(await screen.findByText('search flipkart for shoes'))
    const copyBtn = await screen.findByTitle('Copy code')

    fireEvent.click(copyBtn)

    expect(writeText).toHaveBeenCalledWith(CODE)
    expect(writeText).not.toHaveBeenCalledWith(RUN_A.user_query)
  })

  it('Move-to-group from the drawer moves only the OPEN run’s id', async () => {
    const spies = setupList([RUN_A, RUN_B], { groups: [CHECKOUT] })
    withDetail('run-A', { group_id: null, group_name: null, can_move: true })
    renderPage()
    fireEvent.click(await screen.findByText('search flipkart for shoes'))
    const trigger = await screen.findByRole('button', { name: /Move to group…/ })

    pointerDown(trigger)
    fireEvent.click(await screen.findByText('Checkout'))

    expect(spies.assignRuns).toHaveBeenCalledWith(['run-A'], 'g-1')
  })

  it('shows the run’s group as a read-only label, not a control, when this caller may not move it', async () => {
    setupList([RUN_B])
    withDetail('run-B', { group_name: 'Marketing', group_id: 'g-9', can_move: false })
    renderPage()
    fireEvent.click(await screen.findByText('checkout flow'))

    const label = await screen.findByTitle('Only the owner of a run, or an org admin, can move it')
    expect(label.textContent).toContain('Marketing')
    expect(label.closest('button')).toBeNull()
  })

  it('shows the refusal text when the detail fetch is refused, with no stale code and no "Loading…"', async () => {
    setupList([RUN_A])
    mockUseFetch.mockImplementation((path: string | null) => {
      if (path === '/api/history/run-A') return { data: null, loading: false, error: 'You cannot read this run', reload: vi.fn() }
      return { data: null, loading: false, error: '', reload: vi.fn() }
    })
    renderPage()

    fireEvent.click(await screen.findByText('search flipkart for shoes'))

    expect(await screen.findByText('Run unavailable')).toBeInTheDocument()
    expect(screen.getByText('You cannot read this run')).toBeInTheDocument()
    expect(screen.queryByText('Loading…')).toBeNull()
  })

  it('shows "Loading…" while the run detail fetch is still pending, with no stale code and no error', async () => {
    setupList([RUN_A])
    mockUseFetch.mockImplementation((path: string | null) => {
      if (path === '/api/history/run-A') return { data: null, loading: true, error: '', reload: vi.fn() }
      return { data: null, loading: false, error: '', reload: vi.fn() }
    })
    renderPage()

    fireEvent.click(await screen.findByText('search flipkart for shoes'))

    expect(await screen.findByText('Loading…')).toBeInTheDocument()
    expect(screen.queryByText('Run unavailable')).toBeNull()
  })

  it('shows "No stored code" with the Regenerate hint for a run with a query but nothing saved', async () => {
    setupList([RUN_A])
    withDetail('run-A', { robot_code: null })
    renderPage()

    fireEvent.click(await screen.findByText('search flipkart for shoes'))

    expect(await screen.findByText(/No stored code — this run predates code persistence\./)).toBeInTheDocument()
    expect(screen.getByText(/Use Regenerate to produce it again\./)).toBeInTheDocument()
  })

  it('shows a message above the table when the groups list fails to load', async () => {
    setupList([RUN_A], { groupsError: 'network down' })
    renderPage()

    expect(await screen.findByText(/Couldn.t load groups — network down/)).toBeInTheDocument()
  })
})
