/**
 * App.tsx — KeepAlivePages is the authorisation boundary between a page and
 * the roles allowed to reach it. Every entry in PAGES carries its own gate
 * (admin / orgAdmin / viewLearning), and KeepAlivePages is the ONLY place that
 * turns a gate into "mount the page" or "bounce to /generate" — there is no
 * per-route guard left (see auth/guards.tsx's own docstring, which documents
 * the move). A caller who fails a page's gate must never see that page by
 * navigating straight to its path, and a page already mounted must unmount
 * the instant its caller's flags stop satisfying the gate (a session demoted
 * mid-flight), rather than linger hidden and keep firing background requests
 * — both are called out explicitly in App.tsx's own comments.
 *
 * The gate values below (admin/orgAdmin/viewLearning per path) are pinned by
 * hand from PAGES, not read from the PAGES export itself — reading them back
 * from the same table the code under test consults would make an
 * authorisation weakening invisible (drop `admin: true` from an entry and a
 * test that re-derives its expectation from that same entry "passes" the
 * weakened version too). gateAllowed is imported for real, though: it is a
 * separately-tested pure predicate shared by design (pageGates.ts), and using
 * it as the oracle here tests whether KeepAlivePages actually WIRES UP the
 * gate table correctly, not whether gateAllowed's own boolean logic is right.
 *
 * Every real page component is replaced by a one-line stub: this file tests
 * ONLY the PAGES table + KeepAlivePages wiring, not GeneratePage, HistoryPage,
 * etc, which have their own test files (and their own API calls, which would
 * otherwise fire for real here).
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { Link } from 'react-router-dom'
import type { ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({
  AuthProvider: ({ children }: { children: ReactNode }) => children,
  useAuth: vi.fn(),
}))
vi.mock('@/components/history/RunGroupsContext', () => ({
  RunGroupsProvider: ({ children }: { children: ReactNode }) => children,
}))
vi.mock('@/components/app-sidebar', () => ({ AppSidebar: () => <span>SIDEBAR_STUB</span> }))
vi.mock('@/components/app-header', () => ({ AppHeader: () => <span>HEADER_STUB</span> }))

vi.mock('@/pages/GeneratePage', () => ({
  default: () => (
    <>
      <span>GENERATE_PAGE</span>
      <Link to="/metrics">nav-metrics</Link>
    </>
  ),
}))
vi.mock('@/pages/HistoryPage', () => ({ default: () => <span>HISTORY_PAGE</span> }))
vi.mock('@/pages/MetricsPage', () => ({
  default: () => (
    <>
      <span>METRICS_PAGE</span>
      <Link to="/generate">nav-generate</Link>
    </>
  ),
}))
vi.mock('@/pages/LearningPage', () => ({ default: () => <span>LEARNING_PAGE</span> }))
vi.mock('@/pages/SettingsPage', () => ({ default: () => <span>SETTINGS_PAGE</span> }))
vi.mock('@/pages/AccessConsolePage', () => ({ default: () => <span>ACCESS_PAGE</span> }))
vi.mock('@/pages/TeamPage', () => ({ default: () => <span>TEAM_PAGE</span> }))
vi.mock('@/pages/AccessGatePage', () => ({ default: () => <span>ACCESS_GATE_PAGE</span> }))
vi.mock('@/pages/auth/LoginPage', () => ({ default: () => <span>LOGIN_PAGE</span> }))
vi.mock('@/pages/auth/SignupPage', () => ({ default: () => <span>SIGNUP_PAGE</span> }))
vi.mock('@/pages/auth/ForgotPasswordPage', () => ({ default: () => <span>FORGOT_PAGE</span> }))
vi.mock('@/auth/OAuthCallback', () => ({ default: () => <span>OAUTH_PAGE</span> }))

import { useAuth } from '@/auth/AuthContext'
import { gateAllowed, type Gate, type GateFlags } from '@/auth/pageGates'
import App, { PAGES } from './App'

const mockUseAuth = vi.mocked(useAuth)

// The REAL ThemeProvider and SidebarProvider both run inside App() (only the
// PAGE components and app-sidebar/app-header are stubbed), and both read
// window.matchMedia via a useEffect. setup.ts installs a working stub once
// per FILE, but this file's own afterEach(vi.resetAllMocks) strips that
// stub's implementation after the first test (resetAllMocks resets every
// vi.fn(), including that one) — so every test after the first would
// otherwise call matchMedia() and get back undefined. Re-install per test.
beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }))
})
afterEach(() => vi.resetAllMocks())

function setAuth(flags: GateFlags) {
  mockUseAuth.mockReturnValue({
    user: { id: 'u1', email: 'a@b.com', display_name: 'A', role: flags.isAdmin ? 'admin' : 'user', status: 'active' },
    loading: false, isAuthenticated: true, status: 'active',
    ...flags,
    login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
}

function renderAt(path: string) {
  window.history.pushState({}, '', path)
  return render(<App />)
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

const PAGE_CASES: Array<{ path: string; marker: string; gate: Gate }> = [
  { path: '/generate', marker: 'GENERATE_PAGE', gate: {} },
  { path: '/history', marker: 'HISTORY_PAGE', gate: {} },
  { path: '/metrics', marker: 'METRICS_PAGE', gate: { admin: true } },
  { path: '/learning', marker: 'LEARNING_PAGE', gate: { viewLearning: true } },
  { path: '/access', marker: 'ACCESS_PAGE', gate: { admin: true } },
  { path: '/settings', marker: 'SETTINGS_PAGE', gate: { admin: true } },
  { path: '/team', marker: 'TEAM_PAGE', gate: { orgAdmin: true } },
]

describe('KeepAlivePages — the gate table, every page x every role', () => {
  for (const { path, marker, gate } of PAGE_CASES) {
    describe(path, () => {
      for (const { name, flags } of ROLES) {
        const expected = gateAllowed(gate, flags)
        it(`${expected ? 'renders the page' : 'redirects to /generate instead of rendering'} for a ${name}`, () => {
          setAuth(flags)
          renderAt(path)

          if (expected) {
            expect(screen.getByText(marker)).toBeInTheDocument()
          } else {
            expect(screen.queryByText(marker)).toBeNull()
            expect(screen.getByText('GENERATE_PAGE')).toBeInTheDocument()
          }
        })
      }
    })
  }
})

describe('KeepAlivePages — keep-alive across navigation', () => {
  it('keeps a visited page mounted (hidden, not destroyed) after navigating away from it', () => {
    setAuth(ADMIN)
    renderAt('/metrics')
    expect(screen.getByText('METRICS_PAGE')).toBeInTheDocument()

    fireEvent.click(screen.getByText('nav-generate'))

    // Both pages are still in the DOM...
    expect(screen.getByText('METRICS_PAGE')).toBeInTheDocument()
    expect(screen.getByText('GENERATE_PAGE')).toBeInTheDocument()
    // ...but only the active one gets the visible className; the visited,
    // no-longer-active one gets the plain 'hidden' branch of the ternary.
    expect(screen.getByText('METRICS_PAGE').parentElement!.className).toBe('hidden')
    expect(screen.getByText('GENERATE_PAGE').parentElement!.className).toBe('flex flex-1 flex-col')
  })
})

describe('GatedLayout — the access-status gate above KeepAlivePages', () => {
  it('shows the access gate instead of any page when the caller is not active', () => {
    // A DIFFERENT gate than KeepAlivePages' role check (pending/suspended/
    // rejected accounts), sitting directly above it — pinned here since it is
    // in the same file and decides whether KeepAlivePages ever runs at all.
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'a@b.com', display_name: 'A', role: 'user', status: 'pending' },
      loading: false, isAuthenticated: true, status: 'pending',
      isAdmin: false, isOrgAdmin: false, canViewLearning: false,
      login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
    } as unknown as ReturnType<typeof useAuth>)

    renderAt('/generate')

    expect(screen.getByText('ACCESS_GATE_PAGE')).toBeInTheDocument()
    expect(screen.queryByText('GENERATE_PAGE')).toBeNull()
  })
})

describe('KeepAlivePages — demotion mid-flight', () => {
  it('unmounts an already-visited admin page the instant the caller stops being admin, even while merely hidden (not the active path)', () => {
    setAuth(ADMIN)
    const { rerender } = renderAt('/metrics')
    expect(screen.getByText('METRICS_PAGE')).toBeInTheDocument()

    fireEvent.click(screen.getByText('nav-generate'))
    expect(screen.getByText('METRICS_PAGE').parentElement!.className).toBe('hidden')

    setAuth(PLAIN)
    rerender(<App />)

    // Re-checked on every render, per App.tsx's own comment: a hidden admin
    // page must disappear entirely once demoted, not linger with
    // now-forbidden background requests still possible.
    expect(screen.queryByText('METRICS_PAGE')).toBeNull()
    expect(screen.getByText('GENERATE_PAGE')).toBeInTheDocument()
  })

  it('redirects away immediately when the currently ACTIVE page itself is demoted', () => {
    setAuth(ADMIN)
    const { rerender } = renderAt('/metrics')
    expect(screen.getByText('METRICS_PAGE')).toBeInTheDocument()

    setAuth(PLAIN)
    rerender(<App />)

    expect(screen.queryByText('METRICS_PAGE')).toBeNull()
    expect(screen.getByText('GENERATE_PAGE')).toBeInTheDocument()
  })
})

// --- Preserved from this file's previous version (Task T6) ---
//
// These inspect the REAL PAGES export directly (not the hand-pinned gate
// literals PAGE_CASES above uses) and pin the exact historical regression:
// the SPA used to gate /learning on the platform `admin` role only, even
// though the API gates the Learning routes on is_dashboard_viewer (org admin
// OR platform admin), stranding an org admin behind a page the server would
// happily serve. Kept verbatim rather than dropped, since the table-driven
// suite above pins the CURRENT contract but does not, by itself, document
// which specific historical bug that contract was fixed for.
const learningPage = PAGES.find(p => p.path === '/learning')!
const metricsPage = PAGES.find(p => p.path === '/metrics')!

describe('the /learning PAGES entry', () => {
  it('gates on viewLearning, not admin', () => {
    expect(learningPage.viewLearning).toBe(true)
    expect(learningPage.admin).toBeFalsy()
  })
})

describe('gateAllowed for /learning', () => {
  it('renders for an org admin: can_view_learning true, role user', () => {
    expect(gateAllowed(learningPage, { isAdmin: false, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })

  it('does not render when can_view_learning is false, role user', () => {
    expect(gateAllowed(learningPage, { isAdmin: false, isOrgAdmin: false, canViewLearning: false })).toBe(false)
  })

  it('still renders for a platform admin (no regression)', () => {
    // A platform admin's can_view_learning is always true from the server
    // (is_dashboard_viewer short-circuits on is_platform_admin) — the SPA
    // trusts that one flag rather than re-deriving admin-ness itself.
    expect(gateAllowed(learningPage, { isAdmin: true, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })
})

describe('gateAllowed leaves other pages unchanged', () => {
  it('/metrics is deliberately out of scope: still gates on isAdmin alone', () => {
    expect(gateAllowed(metricsPage, { isAdmin: false, isOrgAdmin: false, canViewLearning: true })).toBe(false)
    expect(gateAllowed(metricsPage, { isAdmin: true, isOrgAdmin: false, canViewLearning: false })).toBe(true)
  })
})
