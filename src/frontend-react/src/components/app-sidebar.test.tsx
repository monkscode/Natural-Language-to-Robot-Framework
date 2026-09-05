/**
 * AppSidebar nav gating — the Learning nav item must never disagree with the
 * page it links to (App.test.tsx covers the page half; see its header for
 * why /learning moved off `admin: true`). gateAllowed (auth/pageGates.ts) is
 * NavGroup's filter predicate — the SAME function App.test.tsx exercises
 * against PAGES, here exercised against NAV_PLATFORM instead, so this is
 * testable without rendering the sidebar — this package tests no page
 * components (see FeedbackPanel.test.tsx / LearningPage.test.tsx).
 *
 * Added later: the suite above pins the DATA (NAV_PLATFORM's flags) and the
 * PREDICATE (gateAllowed) but never renders <AppSidebar/>, so a bug in
 * NavGroup's own `items.filter(item => gateAllowed(item, flags))` call — the
 * thing that actually decides what a signed-in user sees — could break and
 * every test above would stay green. The second suite below renders the real
 * component, table-driven over every nav item x every role, so the DOM output
 * itself is what gets checked, not just the data feeding it.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('@/components/history/RunGroupsContext', () => ({ useRunGroups: vi.fn() }))

import { useAuth } from '@/auth/AuthContext'
import { useRunGroups } from '@/components/history/RunGroupsContext'
import { SidebarProvider } from '@/components/ui/sidebar'
import { AppSidebar, NAV_PLATFORM } from './app-sidebar'
import { gateAllowed, type Gate, type GateFlags } from '@/auth/pageGates'

const learningItem = NAV_PLATFORM.find(i => i.url === '/learning')!

describe('the Learning NAV_PLATFORM entry', () => {
  it('gates on viewLearning, not admin', () => {
    expect(learningItem.viewLearning).toBe(true)
    expect(learningItem.admin).toBe(false)
  })
})

describe('gateAllowed for the Learning nav item', () => {
  it('is shown for an org admin: can_view_learning true, role user', () => {
    expect(gateAllowed(learningItem, { isAdmin: false, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })

  it('is hidden when can_view_learning is false, role user', () => {
    expect(gateAllowed(learningItem, { isAdmin: false, isOrgAdmin: false, canViewLearning: false })).toBe(false)
  })

  it('is still shown for a platform admin (no regression)', () => {
    expect(gateAllowed(learningItem, { isAdmin: true, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })
})

const mockUseAuth = vi.mocked(useAuth)
const mockUseRunGroups = vi.mocked(useRunGroups)

function renderSidebar(
  flags: GateFlags,
  runGroups: Partial<ReturnType<typeof useRunGroups>> = {},
) {
  mockUseAuth.mockReturnValue({
    isAdmin: flags.isAdmin, isOrgAdmin: flags.isOrgAdmin, canViewLearning: flags.canViewLearning,
    user: null, loading: false, isAuthenticated: true, status: 'active',
    login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
  mockUseRunGroups.mockReturnValue({
    groups: [], ungroupedCount: 0, error: '', loaded: true,
    refresh: vi.fn(), createGroup: vi.fn(), renameGroup: vi.fn(), deleteGroup: vi.fn(), assignRuns: vi.fn(),
    groupFilter: null, setGroupFilter: vi.fn(),
    ...runGroups,
  } as unknown as ReturnType<typeof useRunGroups>)
  return render(
    <MemoryRouter>
      <SidebarProvider>
        <AppSidebar />
      </SidebarProvider>
    </MemoryRouter>,
  )
}

const PLAIN: GateFlags = { isAdmin: false, isOrgAdmin: false, canViewLearning: false }
const ADMIN: GateFlags = { isAdmin: true, isOrgAdmin: false, canViewLearning: false }
const ORG_ADMIN: GateFlags = { isAdmin: false, isOrgAdmin: true, canViewLearning: false }
const LEARNING_VIEWER: GateFlags = { isAdmin: false, isOrgAdmin: false, canViewLearning: true }

const ROLES: Array<{ name: string; flags: GateFlags }> = [
  { name: 'plain user', flags: PLAIN },
  { name: 'platform admin', flags: ADMIN },
  { name: 'org admin', flags: ORG_ADMIN },
  { name: 'learning viewer', flags: LEARNING_VIEWER },
]

// Hand-pinned, not read off NAV_PLATFORM/NAV_WORKSPACE (the table under
// test) — reading expectations back from that same table would make a
// weakened gate invisible: drop `admin: true` from an entry and a test that
// re-derives its expectation from that same entry "passes" the weakened
// version too. Same reasoning as App.test.tsx's PAGE_CASES.
const NAV_CASES: Array<{ title: string; gate: Gate }> = [
  { title: 'Generate', gate: {} },
  { title: 'Test Runs', gate: {} },
  { title: 'Metrics', gate: { admin: true } },
  { title: 'Learning', gate: { viewLearning: true } },
  { title: 'Access', gate: { admin: true } },
  { title: 'Team', gate: { orgAdmin: true } },
  { title: 'Settings', gate: { admin: true } },
]

describe('AppSidebar — every nav item x every role, rendered for real', () => {
  beforeEach(() => {
    // SidebarProvider's useIsMobile() reads window.matchMedia in a useEffect,
    // and the footer's real <BuildBadge/> fetches /api/health in another —
    // both run on every render below, so both need a working stub or the
    // mount throws/hits the network. Re-installed every test: this block's
    // own afterEach(vi.resetAllMocks) strips whatever vi.fn() implementation
    // was installed here (see app-header.test.tsx's identical note on
    // matchMedia; build-badge.test.tsx is the equivalent note for fetch).
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: false, media: query, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
  })
  afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks() })

  for (const { title, gate } of NAV_CASES) {
    describe(title, () => {
      for (const { name, flags } of ROLES) {
        const expected = gateAllowed(gate, flags)
        it(`is ${expected ? 'shown' : 'hidden'} for a ${name}`, () => {
          renderSidebar(flags)

          if (expected) {
            expect(screen.getByText(title)).toBeInTheDocument()
          } else {
            expect(screen.queryByText(title)).not.toBeInTheDocument()
          }
        })
      }
    })
  }
})

describe('AppSidebar — the Test Runs quick-access group list', () => {
  beforeEach(() => {
    window.matchMedia = vi.fn().mockImplementation((query: string) => ({
      matches: false, media: query, onchange: null,
      addEventListener: vi.fn(), removeEventListener: vi.fn(),
      addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
    }))
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false }))
  })
  afterEach(() => { vi.unstubAllGlobals(); vi.resetAllMocks() })

  const openList = () => fireEvent.click(screen.getByTitle('Show groups'))

  it('stays collapsed until the chevron is clicked', () => {
    renderSidebar(PLAIN, { loaded: false })

    expect(screen.queryByText(/Loading groups/)).not.toBeInTheDocument()

    openList()

    expect(screen.getByText(/Loading groups/)).toBeInTheDocument()
  })

  it('lists each group by name, and clicking one sets the filter to ITS id', () => {
    const setGroupFilter = vi.fn()
    renderSidebar(PLAIN, {
      loaded: true, error: '',
      groups: [
        { group_id: 'g-1', name: 'Checkout', created_by: 'u1', run_count: 2 },
        { group_id: 'g-2', name: 'Regression', created_by: 'u1', run_count: 5 },
      ],
      setGroupFilter,
    })
    openList()

    fireEvent.click(screen.getByText('Checkout'))

    expect(setGroupFilter).toHaveBeenCalledWith('g-1')
    expect(setGroupFilter).not.toHaveBeenCalledWith('g-2')
  })

  it('shows the load error instead of a false "no groups" empty state', () => {
    renderSidebar(PLAIN, { loaded: true, error: 'network down', groups: [] })
    openList()

    expect(screen.getByText(/Couldn.t load groups — network down/)).toBeInTheDocument()
    expect(screen.queryByText(/No groups yet/)).not.toBeInTheDocument()
  })

  it('says "No groups yet" only once loading finished AND the list is truly empty', () => {
    renderSidebar(PLAIN, { loaded: true, error: '', groups: [] })
    openList()

    expect(screen.getByText(/No groups yet — create one on Test Runs/)).toBeInTheDocument()
  })
})
