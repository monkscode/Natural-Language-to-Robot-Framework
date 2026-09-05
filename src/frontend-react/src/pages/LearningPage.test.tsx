/**
 * LearningPage — the org labels on review pages, and the tab the page opens on.
 *
 * The second half RENDERS the page. That is a deliberate change of policy for
 * this one wiring point: an audit rewrote the landing-tab initialiser to a
 * hardcoded 'overview' — verbatim the regression it was written to fix — and
 * every one of this package's 89 tests stayed green, because they all tested
 * the predicate and nothing tested the component that calls it. A predicate
 * nobody is proved to call is not coverage of a feature.
 *
 * Rendering it needs no Router: LearningPage imports no react-router hook, and
 * useAuth / useFetch / api are mocked below (the same shape
 * AddFeedbackSheet.test.tsx already uses for the drawer), so no tab fetches
 * anything.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: vi.fn() }))

import { useAuth } from '@/auth/AuthContext'
import { useFetch } from '@/lib/useFetch'
import { api } from '@/lib/api'
import LearningPage, { reviewPageLabel, visibleViews } from './LearningPage'

const mockUseAuth = vi.mocked(useAuth)
const mockUseFetch = vi.mocked(useFetch)
const mockApi = vi.mocked(api)

afterEach(() => vi.resetAllMocks())

/**
 * Task 3, F2: hint_review_pages.org_id (added by Task 2) flags which org an LLM
 * hint-review page belongs to. Before this test, the page label was
 * `p.scope_type === 'global' ? 'Global hints' : (p.scope_value || 'No domain')`
 * — with K orgs sharing the same scope/domain shape, every org's page rendered
 * an IDENTICAL label ("Global hints", or the same domain string twice), so an
 * admin reviewing a session with rows from two orgs could not tell which page
 * belonged to which tenant.
 *
 * reviewPageLabel is pure, so this half needs no render.
 */
describe('reviewPageLabel', () => {
  it('two orgs sharing scope_type=global produce distinguishable labels', () => {
    const a = reviewPageLabel({ id: 1, scope_type: 'global', scope_value: null, status: 'succeeded', org_id: 'org-a' })
    const b = reviewPageLabel({ id: 2, scope_type: 'global', scope_value: null, status: 'succeeded', org_id: 'org-b' })
    expect(a).not.toBe(b)
    expect(a).toContain('org-a')
    expect(b).toContain('org-b')
  })

  it('two orgs sharing the same domain produce distinguishable labels', () => {
    const a = reviewPageLabel({ id: 3, scope_type: 'domain', scope_value: 'shop.example.com', status: 'succeeded', org_id: 'org-a' })
    const b = reviewPageLabel({ id: 4, scope_type: 'domain', scope_value: 'shop.example.com', status: 'succeeded', org_id: 'org-b' })
    expect(a).not.toBe(b)
    expect(a).toContain('shop.example.com')
    expect(b).toContain('shop.example.com')
  })

  it('a null org_id (legacy pre-partition page) still renders a usable label', () => {
    const label = reviewPageLabel({ id: 5, scope_type: 'global', scope_value: null, status: 'succeeded', org_id: null })
    expect(label).toContain('Global hints')
  })

  it('a domain page with no domain value still falls back to "No domain"', () => {
    const label = reviewPageLabel({ id: 6, scope_type: 'domain', scope_value: null, status: 'succeeded', org_id: 'org-a' })
    expect(label).toContain('No domain')
    expect(label).toContain('org-a')
  })
})

/**
 * Which tabs the page offers, and which one it opens on.
 *
 * /learning now admits an org admin (can_view_learning), but four of this
 * page's six tabs are backed by routes that are still Depends(require_admin)
 * and answer an org admin 403: Overview and Stats (GET /learning/stats),
 * Triggers (GET /learning/triggers) and LLM Review
 * (GET /learning/review-hints/sessions). Only Hints and Runs are
 * is_dashboard_viewer. The page opened on 'overview', so an org admin
 * following the new nav item landed on an error box on arrival.
 *
 * Those four routes stay platform-only on purpose —
 * test_learning_dashboards_org.py::test_stats_remains_platform_admin_only
 * asserts the 403 — so the fix is fewer controls, not a wider API.
 *
 * visibleViews is pure, so this half is a table check. Which tab the PAGE
 * then opens on is the render below: the two are not the same claim, and the
 * predicate passing was never evidence the page consulted it.
 */
describe('visibleViews', () => {
  const keys = (isAdmin: boolean) => visibleViews(isAdmin).map(v => v.key)

  it('a non-platform-admin is offered only the tabs whose routes admit them', () => {
    expect(keys(false)).toEqual(['hints', 'runs'])
  })

  it('none of the require_admin tabs survive for a non-platform-admin', () => {
    for (const platformOnly of ['overview', 'triggers', 'stats', 'review']) {
      expect(keys(false)).not.toContain(platformOnly)
    }
  })

  it('a platform admin still sees every tab', () => {
    expect(keys(true)).toEqual(['overview', 'hints', 'triggers', 'runs', 'stats', 'review'])
  })
})

/**
 * The tab the page actually opens on.
 *
 * visibleViews above is pure and was already pinned, and that was not enough:
 * rewriting the component's own initialiser to a hardcoded
 * `useState<View>('overview')` left every one of those tests green. The
 * predicate was proved correct; nothing proved the page consulted it. This
 * half renders the page and reads the tab strip and the tab body, which is
 * what a user gets.
 *
 * The failure it guards is not cosmetic: Overview's only call is
 * GET /learning/stats, which is Depends(require_admin), so an org admin
 * landing there gets a red 403 box as the first thing /learning ever shows
 * them.
 */
describe('the tab LearningPage opens on', () => {
  /** The page with every fetch inert — the tab strip and the tab body are the
   *  subject here, not any tab's contents. */
  function renderAs(isAdmin: boolean) {
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'someone@test.local', display_name: 'S', role: isAdmin ? 'admin' : 'user', status: 'active' },
      isAdmin,
      isOrgAdmin: !isAdmin,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockReturnValue(
      { data: null, loading: false, error: '', reload: vi.fn() } as unknown as ReturnType<typeof useFetch>)
    render(<LearningPage />)
  }

  const tab = (label: string) => screen.queryByRole('button', { name: label })
  /** A control only the Hints tab draws, and a stat only Overview draws — the
   *  tab strip alone cannot say which tab is OPEN, only which exist. */
  const hintsIsOpen = () => screen.queryByPlaceholderText('Domain filter…')
  const overviewIsOpen = () => screen.queryByText('Flagged events')

  it('opens an org admin on Hints, not on the tab that 403s them', () => {
    renderAs(false)

    expect(hintsIsOpen()).toBeInTheDocument()
    expect(overviewIsOpen()).toBeNull()
  })

  it('offers an org admin only the two tabs whose routes admit them', () => {
    renderAs(false)

    expect(tab('Hints')).toBeInTheDocument()
    expect(tab('Runs')).toBeInTheDocument()
    for (const platformOnly of ['Overview', 'Triggers', 'Stats', 'LLM Review']) {
      expect(tab(platformOnly), `${platformOnly} is require_admin and must not be offered`).toBeNull()
    }
  })

  it('still opens a platform admin on Overview, with all six tabs', () => {
    renderAs(true)

    expect(overviewIsOpen()).toBeInTheDocument()
    expect(hintsIsOpen()).toBeNull()
    for (const label of ['Overview', 'Hints', 'Triggers', 'Runs', 'Stats', 'LLM Review']) {
      expect(tab(label), `${label} must be offered to a platform admin`).toBeInTheDocument()
    }
  })

  it('switches the body when a tab is clicked', () => {
    // The landing tab is one value read in seven places (the strip's active
    // styling and the six bodies). This pins that they read the SAME value —
    // a body left reading a stale one would still render the right first tab.
    renderAs(true)
    expect(overviewIsOpen()).toBeInTheDocument()

    fireEvent.click(tab('Runs')!)

    expect(screen.getByPlaceholderText('Search query…')).toBeInTheDocument()
    expect(overviewIsOpen()).toBeNull()
  })
})

/**
 * I3: whose hint is this?
 *
 * For a platform admin `list_hints` sets scope_org = None and returns hints
 * from EVERY org. Orgs T1 and T2 can each accumulate "always wait for the
 * spinner before reading the grid", scope global, both active — and the table
 * rendered text / scope / status / counters / actions, so those were two
 * visually identical rows with a Retract button each. Which tenant's live
 * guidance stopped injecting was a coin flip.
 *
 * This branch fixed the same class one tab over (reviewPageLabel, above).
 * This is that fix where the MUTATION buttons are.
 */
describe('the owning org on the Hints table', () => {
  const T1 = 'aaaaaaaa-0000-0000-0000-000000000001'
  const T2 = 'bbbbbbbb-0000-0000-0000-000000000002'
  const SAME_TEXT = 'always wait for the spinner before reading the grid'
  const HINTS = {
    total: 2,
    hints: [
      { id: 1, feedback_text: SAME_TEXT, is_active: 1, conflict_flagged: 0, scope: 'global', org_id: T1 },
      { id: 2, feedback_text: SAME_TEXT, is_active: 1, conflict_flagged: 0, scope: 'global', org_id: T2 },
    ],
  }

  /** Only the hints path answers with rows; every other tab's fetch stays
   *  inert. `orgs` is what GET /auth/admin/orgs answers — an Error to make
   *  it fail. */
  function renderHints(isAdmin: boolean, orgs: unknown = []) {
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'someone@test.local', display_name: 'S', role: isAdmin ? 'admin' : 'user', status: 'active' },
      isAdmin,
      isOrgAdmin: !isAdmin,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockImplementation((path: string | null) => ({
      data: path?.startsWith('/api/learning/hints') ? HINTS : null,
      loading: false,
      error: '',
      reload: vi.fn(),
    }) as unknown as ReturnType<typeof useFetch>)
    if (orgs instanceof Error) mockApi.mockRejectedValue(orgs)
    else mockApi.mockResolvedValue(orgs)
    render(<LearningPage />)
    // A platform admin lands on Overview; Hints is one click away.
    if (isAdmin) fireEvent.click(screen.getByRole('button', { name: 'Hints' }))
  }

  /** The Org cell of every body row — column 1, right after the hint text. */
  const orgCells = () =>
    screen.getAllByRole('row').slice(1).map(r => r.querySelectorAll('td')[1]?.textContent)

  it('names each row’s org for a platform admin, resolved to the org name', async () => {
    renderHints(true, [
      { id: T1, name: 'Tenant One', kind: 'team' },
      { id: T2, name: 'Tenant Two', kind: 'team' },
    ])

    expect(await screen.findByText('Tenant One')).toBeInTheDocument()
    // The whole point: the two rows are no longer indistinguishable.
    expect(orgCells()).toEqual(['Tenant One', 'Tenant Two'])
  })

  it('falls back to the full org id, never a truncation of it', async () => {
    // An org the directory does not name — created since the fetch, or one
    // this admin's list simply missed. The id IS the identity, so it is what
    // the column shows, whole.
    renderHints(true, [{ id: T1, name: 'Tenant One', kind: 'team' }])

    expect(await screen.findByText('Tenant One')).toBeInTheDocument()
    expect(screen.getByText(T2).textContent).toBe(T2)
  })

  it('keeps the table usable when the org directory fails to load', async () => {
    renderHints(true, new Error('Request failed (500)'))

    // Both rows still render, both still name their org by id, and no error
    // lands on the table that carries the Retract buttons.
    await waitFor(() => expect(orgCells()).toEqual([T1, T2]))
    expect(screen.queryByText(/Request failed/)).toBeNull()
    expect(screen.getAllByRole('button', { name: 'Retract' })).toHaveLength(2)
  })

  it('draws no org column for an org admin, and never calls the admin-only route', async () => {
    // Every row an org admin can see carries the same org, so the column
    // would be noise — and GET /auth/admin/orgs is require_admin, so the
    // fetch could only ever 403 them.
    renderHints(false)

    expect(screen.getByText('Hint')).toBeInTheDocument()   // the table did render
    expect(screen.queryByText('Org')).toBeNull()
    expect(screen.queryByText(T1)).toBeNull()
    await waitFor(() =>
      expect(mockApi.mock.calls.filter(([path]) => path === '/auth/admin/orgs')).toHaveLength(0))
  })
})

/**
 * Hint lifecycle actions (act()) and the review-session flow.
 *
 * Both mutate the learning store: act() posts unflag/retract/reactivate to
 * /api/learning/hints/{id}/{action}, and the Review tab reads
 * /api/learning/review-hints/sessions (polling every 4s while a review is
 * running), opens a session's detail, and writes decide()/applyApproved()
 * against it. A wrong hint id or a dropped refetch after any of these is
 * silent — retracting the wrong hint still "succeeds" from the caller's
 * point of view — so every write below pins the exact path/body sent and
 * that the right reload fired.
 */

describe('Hints: status badges, and opening the drawer/sheet', () => {
  const ROWS = {
    total: 6,
    hints: [
      { id: 1, feedback_text: 'row flagged', is_active: 1, conflict_flagged: 1, conflict_flag_reason: 'contradicts hint 9' },
      { id: 2, feedback_text: 'row active plain', is_active: 1, conflict_flagged: 0 },
      { id: 3, feedback_text: 'row active admin', is_active: 1, conflict_flagged: 0, created_via: 'admin' },
      { id: 4, feedback_text: 'row retracted', is_active: 0, sort_priority: 4 },
      { id: 5, feedback_text: 'row llm disabled', is_active: 0, sort_priority: 3, llm_review_disabled: 1 },
      { id: 6, feedback_text: 'row auto disabled', is_active: 0, sort_priority: 3, llm_review_disabled: 0 },
    ],
  }

  function renderRows() {
    mockUseAuth.mockReturnValue({
      // org_id is required for AddFeedbackSheet to show the "Your
      // organisation" field — without it, it renders its own "isn't in an
      // organisation" message instead (a different, also-real branch).
      user: { id: 'u9', email: 'reviewer@test.local', display_name: 'R', role: 'user', status: 'active', org_id: 'org-test-1' },
      isAdmin: false,
      isOrgAdmin: true,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockImplementation((path: string | null) => (
      path && /^\/api\/learning\/hints\?/.test(path)
        ? { data: ROWS, loading: false, error: '', reload: vi.fn() }
        : { data: null, loading: false, error: '', reload: vi.fn() }
    ) as unknown as ReturnType<typeof useFetch>)
    render(<LearningPage />)
  }

  it('labels each row Flagged / Active / Active (admin) / Retracted / LLM-disabled / Auto-disabled correctly', () => {
    renderRows()
    expect(within(screen.getByText('row flagged').closest('tr')!).getByText('Flagged')).toBeInTheDocument()
    expect(within(screen.getByText('row active plain').closest('tr')!).getByText('Active')).toBeInTheDocument()
    expect(within(screen.getByText('row active admin').closest('tr')!).getByText('Active (admin)')).toBeInTheDocument()
    expect(within(screen.getByText('row retracted').closest('tr')!).getByText('Retracted')).toBeInTheDocument()
    expect(within(screen.getByText('row llm disabled').closest('tr')!).getByText('LLM-disabled')).toBeInTheDocument()
    expect(within(screen.getByText('row auto disabled').closest('tr')!).getByText('Auto-disabled')).toBeInTheDocument()
    // The flag reason renders under the flagged row's own text, not floating loose.
    expect(within(screen.getByText('row flagged').closest('tr')!).getByText(/contradicts hint 9/)).toBeInTheDocument()
  })

  it('opens the detail drawer for the id of the row that was clicked', () => {
    renderRows()
    fireEvent.click(within(screen.getByText('row active plain').closest('tr')!).getByRole('button', { name: 'Detail' }))
    expect(screen.getByText('Hint #2')).toBeInTheDocument()
  })

  it('opens the add-feedback sheet', () => {
    renderRows()
    fireEvent.click(screen.getByRole('button', { name: /Add feedback/ }))
    expect(screen.getByText('Your organisation')).toBeInTheDocument()
  })
})

describe('Hints: hint lifecycle actions (act())', () => {
  const FLAGGED = { id: 101, feedback_text: 'always wait for the modal to close', is_active: 1, conflict_flagged: 1 }
  const DISABLED = { id: 202, feedback_text: 'scroll before clicking the buy button', is_active: 0 }
  const HINTS = { total: 2, hints: [FLAGGED, DISABLED] }

  /** Non-platform-admin: act() does not gate on role, and staying off Admin
   *  avoids also having to mock the unrelated /auth/admin/orgs directory call. */
  function renderHintsForAct() {
    const hintsReload = vi.fn()
    mockUseAuth.mockReturnValue({
      user: { id: 'u9', email: 'reviewer@test.local', display_name: 'R', role: 'user', status: 'active' },
      isAdmin: false,
      isOrgAdmin: true,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockImplementation((path: string | null) => (
      path && /^\/api\/learning\/hints\?/.test(path)
        ? { data: HINTS, loading: false, error: '', reload: hintsReload }
        : { data: null, loading: false, error: '', reload: vi.fn() }
    ) as unknown as ReturnType<typeof useFetch>)
    render(<LearningPage />)
    return { hintsReload }
  }

  const rowFor = (text: string) => screen.getByText(text).closest('tr')!

  it('unflags with the CORRECT id and action, sends the caller as actor, and refetches', async () => {
    const { hintsReload } = renderHintsForAct()
    mockApi.mockResolvedValueOnce({})

    fireEvent.click(within(rowFor(FLAGGED.feedback_text)).getByRole('button', { name: 'Unflag' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    const [path, options] = mockApi.mock.calls[0]
    expect(path).toBe('/api/learning/hints/101/unflag')
    expect((options as { method: string }).method).toBe('POST')
    expect(JSON.parse((options as { body: string }).body)).toEqual({
      actor: 'reviewer@test.local', reason: 'via admin dashboard',
    })
    await waitFor(() => expect(hintsReload).toHaveBeenCalledTimes(1))
  })

  it('reactivates the DISABLED row’s own id — not the flagged row’s', async () => {
    renderHintsForAct()
    mockApi.mockResolvedValueOnce({})

    fireEvent.click(within(rowFor(DISABLED.feedback_text)).getByRole('button', { name: 'Reactivate' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    expect(mockApi.mock.calls[0][0]).toBe('/api/learning/hints/202/reactivate')
  })

  it('asks for confirmation before retracting, and a cancelled confirm sends nothing', () => {
    renderHintsForAct()
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)

    fireEvent.click(within(rowFor(FLAGGED.feedback_text)).getByRole('button', { name: 'Retract' }))

    expect(confirmSpy).toHaveBeenCalledWith('Retract this hint? It will stop injecting into agent prompts.')
    expect(mockApi).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('retracts once the admin confirms', async () => {
    const { hintsReload } = renderHintsForAct()
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mockApi.mockResolvedValueOnce({})

    fireEvent.click(within(rowFor(FLAGGED.feedback_text)).getByRole('button', { name: 'Retract' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/api/learning/hints/101/retract', expect.anything()))
    await waitFor(() => expect(hintsReload).toHaveBeenCalledTimes(1))
    confirmSpy.mockRestore()
  })

  it('shows the failure and clears the busy state without refetching when the action rejects', async () => {
    const { hintsReload } = renderHintsForAct()
    mockApi.mockRejectedValueOnce(new Error('Request failed (409)'))

    const btn = within(rowFor(FLAGGED.feedback_text)).getByRole('button', { name: 'Unflag' })
    fireEvent.click(btn)

    expect(await screen.findByText('Request failed (409)')).toBeInTheDocument()
    expect(hintsReload).not.toHaveBeenCalled()
    await waitFor(() => expect(btn).not.toBeDisabled())
  })
})

describe('HealthBanner: renders per learning-health status', () => {
  function renderWithHealth(status: string) {
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'admin@test.local', display_name: 'A', role: 'admin', status: 'active' },
      isAdmin: true,
      isOrgAdmin: false,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockImplementation((path: string | null) => (
      path === '/api/learning/health'
        ? { data: { status }, loading: false, error: '', reload: vi.fn() }
        : { data: null, loading: false, error: '', reload: vi.fn() }
    ) as unknown as ReturnType<typeof useFetch>)
    render(<LearningPage />)
  }

  it('shows OK with its check mark and no reliability-check note', () => {
    renderWithHealth('OK')
    expect(screen.getByText(/^✓ Learning system: OK$/)).toBeInTheDocument()
  })

  it('adds the reliability-check note for FAILED, with its own icon', () => {
    renderWithHealth('FAILED')
    expect(screen.getByText(/^✕ Learning system: FAILED — a learning reliability check is failing/)).toBeInTheDocument()
  })

  it('adds the reliability-check note for DEGRADED too', () => {
    renderWithHealth('DEGRADED')
    expect(screen.getByText(/^⚠ Learning system: DEGRADED — a learning reliability check is failing/)).toBeInTheDocument()
  })

  it('renders DISABLED distinctly, without the reliability-check note', () => {
    renderWithHealth('DISABLED')
    expect(screen.getByText(/^⚠ Learning system: DISABLED$/)).toBeInTheDocument()
  })
})

describe('Runs: table rows and opening a run', () => {
  const RUNS = {
    total: 2,
    runs: [
      { workflow_id: 'wf-aaa-111', timestamp: '2026-08-20T10:00:00Z', user_query: 'search for shoes', test_status: 'passed', failure_category: null, nl_injected_count: 2 },
      { workflow_id: 'wf-bbb-222', timestamp: '2026-08-21T11:00:00Z', user_query: '', test_status: 'failed', failure_category: 'B1', nl_injected_count: 0 },
    ],
  }

  function renderRuns() {
    mockUseAuth.mockReturnValue({
      user: { id: 'u9', email: 'reviewer@test.local', display_name: 'R', role: 'user', status: 'active' },
      isAdmin: false,
      isOrgAdmin: true,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockImplementation((path: string | null) => (
      path && /^\/api\/learning\/runs\?/.test(path)
        ? { data: RUNS, loading: false, error: '', reload: vi.fn() }
        : { data: null, loading: false, error: '', reload: vi.fn() }
    ) as unknown as ReturnType<typeof useFetch>)
    render(<LearningPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Runs' }))
  }

  it('renders every run with its status, hints-used count, full workflow id, and a fallback for a blank query', () => {
    renderRuns()
    expect(screen.getByText('search for shoes')).toBeInTheDocument()
    expect(screen.getByText('(paste-and-execute)')).toBeInTheDocument()
    // 'passed'/'failed' also name options in the status-filter <select>, so
    // scope to each row (not screen) to read the STATUS BADGE specifically.
    expect(within(screen.getByText('search for shoes').closest('tr')!).getByText('passed')).toBeInTheDocument()
    expect(within(screen.getByText('(paste-and-execute)').closest('tr')!).getByText('failed')).toBeInTheDocument()
    expect(screen.getByText('(B1)')).toBeInTheDocument()
    expect(screen.getByText('wf-aaa-111')).toBeInTheDocument()
  })

  it('opens RunDrawer for the workflow id of the row clicked, not another row’s', () => {
    renderRuns()

    fireEvent.click(screen.getByText('wf-bbb-222').closest('tr')!)

    expect(screen.getByText('Run detail')).toBeInTheDocument()
    expect(screen.getAllByText('wf-bbb-222')).toHaveLength(2)   // table row + drawer description
    expect(screen.getAllByText('wf-aaa-111')).toHaveLength(1)   // only the OTHER table row
  })
})

describe('Review: session list, starting a review, and the running-poll', () => {
  const SESSIONS_IDLE = {
    is_any_running: false,
    sessions: [
      { id: 1, status: 'completed', hint_count: 5, llm_latency_ms: 3200, warning: null, created_at: '2026-08-01T00:00:00Z', completed_at: '2026-08-01T00:05:00Z' },
      { id: 2, status: 'failed', hint_count: 0, llm_latency_ms: null, warning: 'partial page failure', created_at: '2026-08-02T00:00:00Z', completed_at: null },
    ],
  }
  const SESSIONS_RUNNING = {
    is_any_running: true,
    sessions: [{ id: 3, status: 'pending_llm', hint_count: 8, llm_latency_ms: null, warning: null, created_at: '2026-08-03T00:00:00Z', completed_at: null }],
  }

  function renderReview(sessionsResp: unknown, opts: { sessionsReload?: ReturnType<typeof vi.fn>; details?: Record<number, unknown> } = {}) {
    const sessionsReload = opts.sessionsReload ?? vi.fn()
    const details = opts.details ?? {}
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'admin@test.local', display_name: 'A', role: 'admin', status: 'active' },
      isAdmin: true,
      isOrgAdmin: false,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockImplementation((path: string | null) => {
      if (path === '/api/learning/review-hints/sessions') {
        return { data: sessionsResp, loading: false, error: '', reload: sessionsReload } as unknown as ReturnType<typeof useFetch>
      }
      const m = path?.match(/^\/api\/learning\/review-hints\/sessions\/(\d+)$/)
      if (m && details[Number(m[1])] !== undefined) {
        return { data: details[Number(m[1])], loading: false, error: '', reload: vi.fn() } as unknown as ReturnType<typeof useFetch>
      }
      return { data: null, loading: false, error: '', reload: vi.fn() } as unknown as ReturnType<typeof useFetch>
    })
    render(<LearningPage />)
    fireEvent.click(screen.getByRole('button', { name: 'LLM Review' }))
    return { sessionsReload }
  }

  it('renders each session’s status, formatted latency, and warning marker', () => {
    renderReview(SESSIONS_IDLE)
    expect(screen.getByText('Applied')).toBeInTheDocument()   // session 1: completed
    expect(screen.getByText('Failed')).toBeInTheDocument()    // session 2
    expect(screen.getByText('3.2s')).toBeInTheDocument()      // 3200ms formatted
    expect(screen.getByText('⚠ Yes')).toBeInTheDocument()     // session 2's warning
  })

  it('lets an admin start a review, which becomes the selected session', async () => {
    const { sessionsReload } = renderReview(SESSIONS_IDLE, {
      details: { 77: { session: { id: 77, status: 'pending_llm', hint_count: 0, created_at: '2026-08-05T00:00:00Z' }, recommendations: [] } },
    })
    mockApi.mockResolvedValueOnce({ session_id: 77 })

    fireEvent.click(screen.getByRole('button', { name: 'Start review' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/api/learning/review-hints/start', { method: 'POST' }))
    await waitFor(() => expect(sessionsReload).toHaveBeenCalledTimes(1))
    expect(await screen.findByText('Session #77')).toBeInTheDocument()
  })

  it('shows the failure and selects nothing when starting a review fails', async () => {
    renderReview(SESSIONS_IDLE)
    mockApi.mockRejectedValueOnce(new Error('Request failed (500)'))

    fireEvent.click(screen.getByRole('button', { name: 'Start review' }))

    expect(await screen.findByText('Request failed (500)')).toBeInTheDocument()
    expect(screen.queryByText(/^Session #/)).toBeNull()
  })

  it('disables Start review and shows the running label while a review is in progress', () => {
    renderReview(SESSIONS_RUNNING)
    expect(screen.getByRole('button', { name: 'Review in progress…' })).toBeDisabled()
  })

  it('polls the session list every 4s while a review is running', async () => {
    vi.useFakeTimers()
    try {
      const { sessionsReload } = renderReview(SESSIONS_RUNNING)

      await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
      expect(sessionsReload).toHaveBeenCalledTimes(1)

      await act(async () => { await vi.advanceTimersByTimeAsync(4000) })
      expect(sessionsReload).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not poll at all when no review is running', async () => {
    vi.useFakeTimers()
    try {
      const { sessionsReload } = renderReview(SESSIONS_IDLE)

      await act(async () => { await vi.advanceTimersByTimeAsync(20000) })
      expect(sessionsReload).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('ReviewSessionPanel: recommendations, decisions, and applying', () => {
  const SESSION_REVIEWABLE = { id: 5, status: 'pending_review', hint_count: 2, created_at: '2026-08-01T00:00:00Z', warning: null, error_message: null }
  const REC_UNDECIDED = { id: 900, hint_id: 11, recommendation: 'disable', reason: 'never succeeded', exoneration_count: 0, admin_decision: null, admin_notes: null, applied: 0, feedback_text: 'wait for the spinner' }
  const REC_APPROVED_UNAPPLIED = { id: 901, hint_id: 12, recommendation: 'reactivate', reason: 'exonerated twice', exoneration_count: 2, admin_decision: 'approved', admin_notes: null, applied: 0, feedback_text: 'scroll before clicking buy' }

  function renderPanel(
    detail: { session: { id: number; status: string; hint_count: number; created_at: string; warning?: string | null; error_message?: string | null }; recommendations: unknown[]; pages?: unknown[] },
    opts: { detailReload?: ReturnType<typeof vi.fn>; sessionsReload?: ReturnType<typeof vi.fn> } = {},
  ) {
    const detailReload = opts.detailReload ?? vi.fn()
    const sessionsReload = opts.sessionsReload ?? vi.fn()
    const listStatus = detail.session.status
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'admin@test.local', display_name: 'A', role: 'admin', status: 'active' },
      isAdmin: true,
      isOrgAdmin: false,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockImplementation((path: string | null) => {
      if (path === '/api/learning/review-hints/sessions') {
        return {
          data: {
            is_any_running: listStatus === 'pending_llm',
            sessions: [{ id: detail.session.id, status: listStatus, hint_count: detail.session.hint_count, created_at: detail.session.created_at }],
          },
          loading: false, error: '', reload: sessionsReload,
        } as unknown as ReturnType<typeof useFetch>
      }
      if (path === `/api/learning/review-hints/sessions/${detail.session.id}`) {
        return { data: detail, loading: false, error: '', reload: detailReload } as unknown as ReturnType<typeof useFetch>
      }
      return { data: null, loading: false, error: '', reload: vi.fn() } as unknown as ReturnType<typeof useFetch>
    })
    render(<LearningPage />)
    fireEvent.click(screen.getByRole('button', { name: 'LLM Review' }))
    fireEvent.click(screen.getAllByRole('row')[1])
    return { detailReload, sessionsReload }
  }

  it('renders the session header, decided count, each recommendation’s badge/reason, and an admin note', () => {
    renderPanel({ session: SESSION_REVIEWABLE, recommendations: [REC_UNDECIDED, { ...REC_APPROVED_UNAPPLIED, admin_notes: 'confirmed with the domain owner' }] })
    expect(screen.getByText('Session #5')).toBeInTheDocument()
    // REC_APPROVED_UNAPPLIED already carries admin_decision: 'approved' — 1 of
    // the 2 recommendations is decided, REC_UNDECIDED is the other.
    expect(screen.getByText('1 of 2 decided')).toBeInTheDocument()
    expect(screen.getByText('Disable')).toBeInTheDocument()
    expect(screen.getByText('Reactivate')).toBeInTheDocument()
    expect(screen.getByText('never succeeded')).toBeInTheDocument()
    expect(screen.getByText(/confirmed with the domain owner/)).toBeInTheDocument()
  })

  it('shows the failure and does not refetch when saving a decision rejects', async () => {
    const { detailReload } = renderPanel({ session: SESSION_REVIEWABLE, recommendations: [REC_UNDECIDED] })
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue(null)
    mockApi.mockRejectedValueOnce(new Error('Request failed (409)'))

    fireEvent.click(screen.getAllByRole('button', { name: 'Approve' })[0])

    expect(await screen.findByText('Request failed (409)')).toBeInTheDocument()
    expect(detailReload).not.toHaveBeenCalled()
    promptSpy.mockRestore()
  })

  it('shows the failure and does not refetch when applying rejects', async () => {
    const { detailReload, sessionsReload } = renderPanel({ session: SESSION_REVIEWABLE, recommendations: [REC_APPROVED_UNAPPLIED] })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mockApi.mockRejectedValueOnce(new Error('Request failed (500)'))

    fireEvent.click(screen.getByRole('button', { name: 'Apply approved (1)' }))

    expect(await screen.findByText('Request failed (500)')).toBeInTheDocument()
    expect(detailReload).not.toHaveBeenCalled()
    expect(sessionsReload).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('sends the CORRECT recommendation id and the prompted note when Approve is clicked', async () => {
    const { detailReload } = renderPanel({ session: SESSION_REVIEWABLE, recommendations: [REC_UNDECIDED, REC_APPROVED_UNAPPLIED] })
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue('looks right')
    mockApi.mockResolvedValueOnce({})

    // REC_UNDECIDED renders first — its Approve button is index 0.
    fireEvent.click(screen.getAllByRole('button', { name: 'Approve' })[0])

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    const [path, options] = mockApi.mock.calls[0]
    expect(path).toBe('/api/learning/review-hints/sessions/5/recommendations/900')
    expect((options as { method: string }).method).toBe('PATCH')
    expect(JSON.parse((options as { body: string }).body)).toEqual({ admin_decision: 'approved', admin_notes: 'looks right' })
    await waitFor(() => expect(detailReload).toHaveBeenCalledTimes(1))
    promptSpy.mockRestore()
  })

  it('sends the id of the SPECIFIC recommendation clicked, not the other one on screen', async () => {
    renderPanel({ session: SESSION_REVIEWABLE, recommendations: [REC_UNDECIDED, REC_APPROVED_UNAPPLIED] })
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue(null)
    mockApi.mockResolvedValueOnce({})

    // REC_APPROVED_UNAPPLIED renders second — its Reject button is index 1.
    fireEvent.click(screen.getAllByRole('button', { name: 'Reject' })[1])

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    expect(mockApi.mock.calls[0][0]).toBe('/api/learning/review-hints/sessions/5/recommendations/901')
    promptSpy.mockRestore()
  })

  /* Surprising, and real: unlike Retract's window.confirm, decide()'s
   * window.prompt does not GATE the request — dismissing the note prompt
   * (returns null) still submits the decision, with admin_notes: null. Pinned
   * here rather than weakened, per source: `const notes = window.prompt(...)`
   * has no `if (!notes) return` before the api() call. */
  it('still submits the decision when the note prompt is dismissed — prompt does not gate the call', async () => {
    renderPanel({ session: SESSION_REVIEWABLE, recommendations: [REC_UNDECIDED] })
    const promptSpy = vi.spyOn(window, 'prompt').mockReturnValue(null)
    mockApi.mockResolvedValueOnce({})

    fireEvent.click(screen.getAllByRole('button', { name: 'Approve' })[0])

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    const body = JSON.parse((mockApi.mock.calls[0][1] as { body: string }).body)
    expect(body).toEqual({ admin_decision: 'approved', admin_notes: null })
    promptSpy.mockRestore()
  })

  it('asks for confirmation before applying, and a cancelled confirm sends nothing', () => {
    renderPanel({ session: SESSION_REVIEWABLE, recommendations: [REC_APPROVED_UNAPPLIED] })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)

    fireEvent.click(screen.getByRole('button', { name: 'Apply approved (1)' }))

    expect(confirmSpy).toHaveBeenCalledWith('Apply 1 approved change? This updates the live hints.')
    expect(mockApi).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('applies approved recommendations, shows the result, and refetches BOTH the detail and the session list', async () => {
    const { detailReload, sessionsReload } = renderPanel({ session: SESSION_REVIEWABLE, recommendations: [REC_APPROVED_UNAPPLIED] })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)
    mockApi.mockResolvedValueOnce({ applied_count: 1 })

    fireEvent.click(screen.getByRole('button', { name: 'Apply approved (1)' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/api/learning/review-hints/sessions/5/apply', { method: 'POST' }))
    expect(await screen.findByText('Applied 1 recommendation.')).toBeInTheDocument()
    await waitFor(() => expect(detailReload).toHaveBeenCalledTimes(1))
    // onSessionsChanged IS the list's own reload — the poll/list must also refresh.
    await waitFor(() => expect(sessionsReload).toHaveBeenCalledTimes(1))
    confirmSpy.mockRestore()
  })

  it('shows per-scope page progress with the right note for succeeded and failed pages', () => {
    renderPanel({
      session: SESSION_REVIEWABLE,
      recommendations: [],
      pages: [
        { id: 1, scope_type: 'global', scope_value: null, status: 'succeeded', hint_count: 4, org_id: 'org-a' },
        { id: 2, scope_type: 'domain', scope_value: 'shop.test', status: 'failed', error_message: 'LLM timeout', org_id: 'org-a' },
      ],
    })
    expect(screen.getByText(/4 hints reviewed/)).toBeInTheDocument()
    expect(screen.getByText('LLM timeout')).toBeInTheDocument()
  })

  it('shows the running message and hides Apply while the LLM is still working', () => {
    renderPanel({ session: { ...SESSION_REVIEWABLE, status: 'pending_llm' }, recommendations: [] })
    expect(screen.getByText(/The LLM review is running/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Apply approved/ })).toBeNull()
  })

  it('shows an applied recommendation as done, a decided one by its decision text, and an undecided one as "No decision" once the session closes', () => {
    renderPanel({
      session: { ...SESSION_REVIEWABLE, status: 'completed' },
      recommendations: [
        { ...REC_APPROVED_UNAPPLIED, applied: 1 },
        { ...REC_UNDECIDED, admin_decision: 'rejected' },
        { ...REC_UNDECIDED, id: 902, admin_decision: null },
      ],
    })
    expect(screen.getByText('✓ Applied')).toBeInTheDocument()
    expect(screen.getByText('Decision: rejected')).toBeInTheDocument()
    expect(screen.getByText('No decision')).toBeInTheDocument()
  })

  it('renders an unrecognized session status as its own raw label rather than crashing or hiding it', () => {
    renderPanel({ session: { ...SESSION_REVIEWABLE, status: 'archived' }, recommendations: [] })
    // 'archived' also names the SESSIONS-LIST row's badge (the list and the
    // open panel share the same status here) — scope to the panel header.
    const header = screen.getByText('Session #5').parentElement!
    expect(within(header).getByText('archived')).toBeInTheDocument()
  })

  it('marks a page "Queued" when it has not started and the LLM run itself has already moved on', () => {
    renderPanel({
      session: { ...SESSION_REVIEWABLE, status: 'pending_review' },
      recommendations: [],
      pages: [{ id: 3, scope_type: 'domain', scope_value: 'other.test', status: 'pending', org_id: 'org-a' }],
    })
    expect(screen.getByText(/Queued/)).toBeInTheDocument()
  })

  it('surfaces a session-level warning and says when there is nothing to review', () => {
    renderPanel({ session: { ...SESSION_REVIEWABLE, warning: 'two pages disagreed' }, recommendations: [] })
    expect(screen.getByText('two pages disagreed')).toBeInTheDocument()
    expect(screen.getByText('No recommendations in this session.')).toBeInTheDocument()
  })

  it('shows the failure reason on a failed session', () => {
    renderPanel({ session: { ...SESSION_REVIEWABLE, status: 'failed', error_message: 'LLM request timed out' }, recommendations: [] })
    expect(screen.getByText('LLM request timed out')).toBeInTheDocument()
  })

  it('refetches the open session’s detail when the polled list reports a status change', async () => {
    let listStatus = 'pending_llm'
    const detailReload = vi.fn()
    const sessionsReload = vi.fn()
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'admin@test.local', display_name: 'A', role: 'admin', status: 'active' },
      isAdmin: true,
      isOrgAdmin: false,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockImplementation((path: string | null) => {
      if (path === '/api/learning/review-hints/sessions') {
        return {
          data: { is_any_running: listStatus === 'pending_llm', sessions: [{ id: 5, status: listStatus, hint_count: 2, created_at: '2026-08-01T00:00:00Z' }] },
          loading: false, error: '', reload: sessionsReload,
        } as unknown as ReturnType<typeof useFetch>
      }
      if (path === '/api/learning/review-hints/sessions/5') {
        return {
          data: { session: { id: 5, status: listStatus, hint_count: 2, created_at: '2026-08-01T00:00:00Z' }, recommendations: [] },
          loading: false, error: '', reload: detailReload,
        } as unknown as ReturnType<typeof useFetch>
      }
      return { data: null, loading: false, error: '', reload: vi.fn() } as unknown as ReturnType<typeof useFetch>
    })
    const { rerender } = render(<LearningPage />)
    fireEvent.click(screen.getByRole('button', { name: 'LLM Review' }))
    fireEvent.click(screen.getAllByRole('row')[1])
    expect(detailReload).not.toHaveBeenCalled()   // the initial mount must NOT double-fetch

    listStatus = 'pending_review'
    rerender(<LearningPage />)

    await waitFor(() => expect(detailReload).toHaveBeenCalledTimes(1))
  })
})
