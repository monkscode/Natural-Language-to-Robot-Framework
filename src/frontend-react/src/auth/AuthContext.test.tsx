/**
 * AuthContext — the wiring from the server's flags to the ones the gates read.
 *
 * gateAllowed and the two gate tables are already pinned (pageGates.test.ts,
 * App.test.tsx, app-sidebar.test.tsx) — but all three take GateFlags as an
 * ARGUMENT. Nothing connected /auth/me's `can_view_learning` to the
 * `canViewLearning` those tests feed in, and an audit measured what that gap
 * costs: rewriting the provider's `canViewLearning: user?.can_view_learning ??
 * false` to a bare `canViewLearning: false` left all 89 tests green and
 * `tsc -b` at exit 0, while no user of any shape could reach /learning again —
 * the nav item gone and the URL bouncing to /generate. It is a plausible
 * single-token typo: the field's own camelCase name sits one line below
 * `is_org_admin`, which uses the identical pattern.
 *
 * So this renders the provider and reads the values back out. Three inputs per
 * flag — true, false, and ABSENT — because absent is its own case: it is what
 * a backend older than the flag actually sends, and `?? false` is the only
 * thing standing between that and `undefined` reaching a gate.
 *
 * AuthContext imports api, clearToken, getToken and setToken from @/lib/api,
 * and hydrate() returns early unless getToken() is truthy, so the mock factory
 * supplies all four or the provider never asks for a user at all.
 *
 * A provider is not a page component (vite.config.ts: "no page components") —
 * nothing here renders App, and no Router is involved.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', () => ({
  api: vi.fn(),
  getToken: vi.fn(),
  setToken: vi.fn(),
  clearToken: vi.fn(),
}))

import { api, getToken } from '@/lib/api'
import { AuthProvider, useAuth, type User } from './AuthContext'

const mockApi = vi.mocked(api)
const mockGetToken = vi.mocked(getToken)

// resetAllMocks, not clearAllMocks: getToken's return value is part of the
// fixture, and a value left armed by one test would sign the next one in.
afterEach(() => vi.resetAllMocks())

/** Prints exactly what the gates consume, so a mis-wiring shows up as text.
 *  `loading` is printed too — it is how a test knows hydration has finished,
 *  which matters because every flag reads false BEFORE it does. */
function Flags() {
  const { isAdmin, isOrgAdmin, canViewLearning, loading, isAuthenticated } = useAuth()
  return (
    <ul>
      <li data-testid="loading">{String(loading)}</li>
      <li data-testid="isAuthenticated">{String(isAuthenticated)}</li>
      <li data-testid="isAdmin">{String(isAdmin)}</li>
      <li data-testid="isOrgAdmin">{String(isOrgAdmin)}</li>
      <li data-testid="canViewLearning">{String(canViewLearning)}</li>
    </ul>
  )
}

/** A stored token plus the /auth/me body it hydrates from, then a wait for
 *  hydration to land. The body is a Partial on purpose: which keys are
 *  PRESENT is the whole subject of these tests. */
async function signedInAs(me: Partial<User>) {
  mockGetToken.mockReturnValue('a.jwt.token')
  mockApi.mockResolvedValue({
    id: 'u1', email: 'someone@test.local', display_name: 'Someone',
    role: 'user', status: 'active', ...me,
  })
  render(<AuthProvider><Flags /></AuthProvider>)
  await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'))
}

const flag = (name: string) => screen.getByTestId(name).textContent

describe('canViewLearning — the flag the whole Learning page hangs on', () => {
  it('is true when /auth/me says can_view_learning: true', async () => {
    await signedInAs({ can_view_learning: true })
    expect(flag('canViewLearning')).toBe('true')
  })

  it('is false when /auth/me says can_view_learning: false', async () => {
    await signedInAs({ can_view_learning: false })
    expect(flag('canViewLearning')).toBe('false')
  })

  it('is false when the key is absent — a backend older than the flag', async () => {
    await signedInAs({})
    // Not undefined: gateAllowed reads `!gate.viewLearning || auth.canViewLearning`,
    // so undefined would still deny — but the `?? false` is what keeps the
    // type honest for every other reader of this context.
    expect(flag('canViewLearning')).toBe('false')
  })
})

describe('the sibling flags read from the same body', () => {
  it('isOrgAdmin follows is_org_admin, absent included', async () => {
    await signedInAs({ is_org_admin: true })
    expect(flag('isOrgAdmin')).toBe('true')
  })

  it('isOrgAdmin is false when is_org_admin is absent', async () => {
    await signedInAs({})
    expect(flag('isOrgAdmin')).toBe('false')
  })

  it('isAdmin follows the platform role, not either org flag', async () => {
    await signedInAs({ role: 'admin' })
    expect(flag('isAdmin')).toBe('true')
  })

  it('isAdmin is false for role user even when both org flags are true', async () => {
    await signedInAs({ role: 'user', is_org_admin: true, can_view_learning: true })
    expect(flag('isAdmin')).toBe('false')
  })
})

/* The three flags are near-identical one-liners sitting on consecutive lines,
   so reading the wrong field is as cheap a mistake as returning a constant.
   Two bodies where the flags DISAGREE catch that; a body where they all agree
   cannot. */
describe('the flags are not cross-wired', () => {
  it('an org admin of a personal org: viewLearning yes, isOrgAdmin no', async () => {
    // The solo-user shape exactly: org_admin of their own personal org, which
    // is_org_admin deliberately does NOT report (it means a TEAM org).
    await signedInAs({ is_org_admin: false, can_view_learning: true })
    expect(flag('isOrgAdmin')).toBe('false')
    expect(flag('canViewLearning')).toBe('true')
  })

  it('the mirror shape: isOrgAdmin yes, viewLearning no', async () => {
    await signedInAs({ is_org_admin: true, can_view_learning: false })
    expect(flag('isOrgAdmin')).toBe('true')
    expect(flag('canViewLearning')).toBe('false')
  })
})

describe('no token at all', () => {
  it('never asks /auth/me, and every flag reads false', async () => {
    mockGetToken.mockReturnValue(null)
    render(<AuthProvider><Flags /></AuthProvider>)
    await waitFor(() => expect(screen.getByTestId('loading')).toHaveTextContent('false'))

    expect(mockApi).not.toHaveBeenCalled()
    expect(flag('isAuthenticated')).toBe('false')
    expect(flag('isAdmin')).toBe('false')
    expect(flag('isOrgAdmin')).toBe('false')
    expect(flag('canViewLearning')).toBe('false')
  })
})
