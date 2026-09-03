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
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
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
