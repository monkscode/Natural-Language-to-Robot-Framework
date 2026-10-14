/**
 * MembersTab — the org roster and the two ways to change someone's standing.
 *
 * Two properties are safety rails rather than features, and both are easy to
 * lose in a refactor: an admin may not suspend themselves, and may not strip
 * their own admin role. Either one, if dropped, lets the last administrator
 * lock themselves out of the console that would let them undo it. They are
 * enforced by a `u.id === user?.id` term inside a `disabled` expression, which
 * is precisely the kind of clause that gets tidied away.
 *
 * The `loaded` flag is the same claim-only-once-you-know rule PendingTab
 * documents.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))
vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))

import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/auth/AuthContext'
import MembersTab from './MembersTab'

const mockApi = vi.mocked(api)
const mockUseAuth = vi.mocked(useAuth)
afterEach(() => vi.resetAllMocks())

const ME = { id: 'me', email: 'me@corp.com', display_name: 'Me', role: 'admin' as const, status: 'active' as const }
const ALICE = { id: 'u1', email: 'alice@corp.com', display_name: 'Alice', role: 'user' as const, status: 'active' as const }
const BOB = { id: 'u2', email: 'bob@corp.com', display_name: '', role: 'user' as const, status: 'suspended' as const }
const PENDING = { id: 'u3', email: 'pend@corp.com', display_name: 'P', role: 'user' as const, status: 'pending' as const }

function setup() {
  mockUseAuth.mockReturnValue({
    user: { id: 'me', email: 'me@corp.com', display_name: 'Me', role: 'admin', status: 'active' },
    loading: false, isAuthenticated: true, isAdmin: true, status: 'active',
    isOrgAdmin: true, canViewLearning: true,
    login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
  render(<MembersTab />)
}

describe('MembersTab — the roster', () => {
  it('lists members with their status and role', async () => {
    mockApi.mockResolvedValue([ALICE, BOB])
    setup()

    expect(await screen.findByText('alice@corp.com')).toBeInTheDocument()
    expect(screen.getByText('active')).toBeInTheDocument()
    expect(screen.getByText('suspended')).toBeInTheDocument()
  })

  it('leaves pending signups to the Pending tab', async () => {
    // Showing them here would offer Suspend/Make-admin on an account that has
    // not been approved yet.
    mockApi.mockResolvedValue([ALICE, PENDING])
    setup()

    await screen.findByText('alice@corp.com')
    expect(screen.queryByText('pend@corp.com')).toBeNull()
  })

  it('falls back to a dash for a member with no display name', async () => {
    mockApi.mockResolvedValue([BOB])
    setup()

    expect(await screen.findByText('—')).toBeInTheDocument()
  })

  it('does not claim an empty roster while loading or after a failure', async () => {
    mockApi.mockRejectedValue(new ApiError(500, 'boom'))
    setup()

    expect(await screen.findByText('boom')).toBeInTheDocument()
    expect(screen.queryByText('No members yet.')).toBeNull()
  })

  it('claims an empty roster once a request has returned one', async () => {
    mockApi.mockResolvedValue([])
    setup()

    expect(await screen.findByText('No members yet.')).toBeInTheDocument()
  })
})

describe('MembersTab — the self-lockout rails', () => {
  it('will not let an admin suspend their own account', async () => {
    mockApi.mockResolvedValue([ME])
    setup()
    await screen.findByText('me@corp.com')

    const btn = screen.getByText('Suspend')
    expect(btn).toBeDisabled()
    expect(btn.getAttribute('title')).toMatch(/cannot suspend your own account/i)
  })

  it('will not let an admin remove their own admin role', async () => {
    mockApi.mockResolvedValue([ME])
    setup()
    await screen.findByText('me@corp.com')

    const btn = screen.getByText('Remove admin')
    expect(btn).toBeDisabled()
    expect(btn.getAttribute('title')).toMatch(/cannot remove your own admin role/i)
  })

  it('still allows both actions on OTHER people', async () => {
    // The rail must be about identity, not about the action.
    mockApi.mockResolvedValue([ALICE])
    setup()
    await screen.findByText('alice@corp.com')

    expect(screen.getByText('Suspend')).toBeEnabled()
    expect(screen.getByText('Make admin')).toBeEnabled()
  })
})

describe('MembersTab — the actions', () => {
  it('suspends an active member, then refetches', async () => {
    mockApi.mockResolvedValueOnce([ALICE]).mockResolvedValueOnce(undefined).mockResolvedValueOnce([])
    setup()
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Suspend'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/users/u1/suspend', { method: 'POST' }))
    expect(await screen.findByText('No members yet.')).toBeInTheDocument()
  })

  it('offers Reactivate — not Suspend — for a suspended member', async () => {
    mockApi.mockResolvedValueOnce([BOB]).mockResolvedValueOnce(undefined).mockResolvedValueOnce([])
    setup()
    await screen.findByText('bob@corp.com')
    expect(screen.queryByText('Suspend')).toBeNull()

    fireEvent.click(screen.getByText('Reactivate'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/users/u2/reactivate', { method: 'POST' }))
  })

  it('promotes a user to admin with the role in the body', async () => {
    mockApi.mockResolvedValueOnce([ALICE]).mockResolvedValueOnce(undefined).mockResolvedValueOnce([])
    setup()
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Make admin'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/users/u1/role', {
      method: 'POST', body: JSON.stringify({ role: 'admin' }),
    }))
  })

  it('demotes another admin back to user', async () => {
    const otherAdmin = { ...ALICE, role: 'admin' as const }
    mockApi.mockResolvedValueOnce([otherAdmin]).mockResolvedValueOnce(undefined).mockResolvedValueOnce([])
    setup()
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Remove admin'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/users/u1/role', {
      method: 'POST', body: JSON.stringify({ role: 'user' }),
    }))
  })

  it('sends no body for an action that takes none', async () => {
    // suspend/reactivate carry no payload; sending `body: undefined` would
    // still set a Content-Length and change the request shape.
    mockApi.mockResolvedValueOnce([ALICE]).mockResolvedValueOnce(undefined).mockResolvedValueOnce([])
    setup()
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Suspend'))

    await waitFor(() => {
      const call = mockApi.mock.calls.find(c => String(c[0]).includes('/suspend'))!
      expect(call[1]).not.toHaveProperty('body')
    })
  })

  it('surfaces the server’s refusal and re-enables the row', async () => {
    mockApi.mockResolvedValueOnce([ALICE]).mockRejectedValueOnce(new ApiError(403, 'Not your org'))
    setup()
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Suspend'))

    expect(await screen.findByText('Not your org')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Suspend')).toBeEnabled())
  })

  it('falls back to a generic message for a non-ApiError failure', async () => {
    mockApi.mockResolvedValueOnce([ALICE]).mockRejectedValueOnce(new Error('socket hang up'))
    setup()
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Suspend'))

    expect(await screen.findByText('Action failed')).toBeInTheDocument()
  })
})
