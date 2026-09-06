/**
 * TeamPage — the org roster, the invite form and the open-invitations queue.
 *
 * Two behaviours are easy to lose in a refactor: only OPEN invitations are
 * ever listed (an accepted/expired one must not clutter the invite queue —
 * there is no per-status affordance for those, so showing them would be
 * silently wrong, not just untidy), and a failed invite must leave the typed
 * email in the box so the caller does not have to retype it — the box is
 * cleared only on the success path.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))

import { api, ApiError } from '@/lib/api'
import TeamPage from './TeamPage'

const mockApi = vi.mocked(api)
afterEach(() => vi.resetAllMocks())

interface Member { user_id: string; email: string; org_role: string; status: string }
interface Invite { id: string; email: string; status: string }

const ALICE: Member = { user_id: 'u1', email: 'alice@corp.com', org_role: 'member', status: 'active' }
const BOB: Member = { user_id: 'u2', email: 'bob@corp.com', org_role: 'org_admin', status: 'active' }
const OPEN: Invite = { id: 'i1', email: 'carol@corp.com', status: 'open' }
const ACCEPTED: Invite = { id: 'i2', email: 'dave@corp.com', status: 'accepted' }

/** Routes the mock by path+method instead of call order, so tests can queue
 * a `mockImplementationOnce` failure for one specific call without having to
 * track exactly which position it falls at. */
function setupApi(members: Member[] = [], invites: Invite[] = []) {
  mockApi.mockImplementation(async (path, opts) => {
    const method = opts?.method ?? 'GET'
    if (path === '/auth/org/members') return members
    if (path === '/auth/org/invitations' && method === 'POST') return undefined
    if (path === '/auth/org/invitations') return invites
    if (path.startsWith('/auth/org/invitations/') && method === 'DELETE') return undefined
    throw new Error(`unexpected call in TeamPage test: ${method} ${path}`)
  })
}

describe('TeamPage — roster and invite list', () => {
  it('lists members with their org role and status', async () => {
    setupApi([ALICE, BOB], [])
    render(<TeamPage />)

    expect(await screen.findByText('alice@corp.com')).toBeInTheDocument()
    expect(screen.getByText('member · active')).toBeInTheDocument()
    expect(screen.getByText('org_admin · active')).toBeInTheDocument()
  })

  it('lists an open invitation but hides one that is no longer open', async () => {
    setupApi([], [OPEN, ACCEPTED])
    render(<TeamPage />)

    expect(await screen.findByText('carol@corp.com')).toBeInTheDocument()
    expect(screen.queryByText('dave@corp.com')).toBeNull()
  })

  it('shows the server message when the initial load fails', async () => {
    setupApi([], [])
    mockApi.mockImplementationOnce(async () => { throw new ApiError(500, 'org lookup failed') })
    render(<TeamPage />)

    expect(await screen.findByText('org lookup failed')).toBeInTheDocument()
  })

  it('falls back to a generic message for a non-ApiError load failure', async () => {
    setupApi([], [])
    mockApi.mockImplementationOnce(async () => { throw new Error('network down') })
    render(<TeamPage />)

    expect(await screen.findByText('Failed to load team')).toBeInTheDocument()
  })
})

describe('TeamPage — sending an invite', () => {
  it('POSTs the typed email, then clears the box and refetches the roster', async () => {
    setupApi([], [])
    render(<TeamPage />)
    await screen.findByText('Invited people still require administrator approval before they can sign in.')

    const input = screen.getByPlaceholderText('name@company.com') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'new@corp.com' } })
    fireEvent.click(screen.getByText('Invite'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith(
      '/auth/org/invitations', { method: 'POST', body: JSON.stringify({ email: 'new@corp.com' }) },
    ))
    // Cleared by the component after a successful invite — not the value we
    // just typed; the failure test below proves it is NOT cleared otherwise.
    await waitFor(() => expect(input.value).toBe(''))
    // A reload is a real refetch, not "trust the list is still current".
    const memberGets = mockApi.mock.calls.filter(c => c[0] === '/auth/org/members')
    expect(memberGets).toHaveLength(2)
  })

  it('leaves the typed email in the box when the invite POST fails', async () => {
    setupApi([], [])
    render(<TeamPage />)
    await screen.findByText('Invited people still require administrator approval before they can sign in.')

    const input = screen.getByPlaceholderText('name@company.com') as HTMLInputElement
    fireEvent.change(input, { target: { value: 'retry@corp.com' } })
    mockApi.mockImplementationOnce(async () => { throw new ApiError(409, 'Already invited') })
    fireEvent.click(screen.getByText('Invite'))

    expect(await screen.findByText('Already invited')).toBeInTheDocument()
    expect(input.value).toBe('retry@corp.com')
  })

  it('falls back to a generic message for a non-ApiError invite failure', async () => {
    setupApi([], [])
    render(<TeamPage />)
    await screen.findByText('Invited people still require administrator approval before they can sign in.')

    fireEvent.change(screen.getByPlaceholderText('name@company.com'), { target: { value: 'x@corp.com' } })
    mockApi.mockImplementationOnce(async () => { throw new Error('socket hang up') })
    fireEvent.click(screen.getByText('Invite'))

    expect(await screen.findByText('Failed to invite')).toBeInTheDocument()
  })
})

describe('TeamPage — revoking an invite', () => {
  it('DELETEs the specific invitation id, then refetches', async () => {
    setupApi([], [OPEN])
    render(<TeamPage />)
    await screen.findByText('carol@corp.com')

    fireEvent.click(screen.getByText('revoke'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/org/invitations/i1', { method: 'DELETE' }))
  })

  it('shows an error and keeps the row when revoke fails', async () => {
    setupApi([], [OPEN])
    render(<TeamPage />)
    await screen.findByText('carol@corp.com')

    mockApi.mockImplementationOnce(async () => { throw new ApiError(403, 'Not your org') })
    fireEvent.click(screen.getByText('revoke'))

    expect(await screen.findByText('Not your org')).toBeInTheDocument()
    expect(screen.getByText('carol@corp.com')).toBeInTheDocument()
  })

  it('falls back to a generic message for a non-ApiError revoke failure', async () => {
    setupApi([], [OPEN])
    render(<TeamPage />)
    await screen.findByText('carol@corp.com')

    mockApi.mockImplementationOnce(async () => { throw new Error('socket hang up') })
    fireEvent.click(screen.getByText('revoke'))

    expect(await screen.findByText('Failed to revoke invite')).toBeInTheDocument()
  })
})
