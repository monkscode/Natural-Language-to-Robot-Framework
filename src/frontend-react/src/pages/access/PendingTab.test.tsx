/**
 * PendingTab — the approval queue.
 *
 * The `loaded` flag is the reason this file exists. "No one is waiting for
 * approval" is a definite claim about the queue, and the list starts empty, so
 * without that flag the page makes the claim while the request is still in
 * flight AND after one that failed — telling an admin the queue is clear when
 * nobody has actually looked. Three tests hold it in place, because a state
 * variable whose only job is to suppress a message is exactly what gets
 * "simplified" away.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))

import { api, ApiError } from '@/lib/api'
import PendingTab from './PendingTab'

const mockApi = vi.mocked(api)
afterEach(() => vi.resetAllMocks())

const ALICE = { id: 'u1', email: 'alice@corp.com', display_name: 'Alice', invited_org: null }
const BOB = { id: 'u2', email: 'bob@corp.com', display_name: '', invited_org: 'Acme' }

const CLEAR = 'No one is waiting for approval.'

describe('PendingTab — the empty claim', () => {
  it('does NOT claim the queue is clear while the request is in flight', () => {
    mockApi.mockImplementation(() => new Promise(() => {}))   // never settles

    render(<PendingTab />)

    expect(screen.queryByText(CLEAR)).toBeNull()
  })

  it('does NOT claim the queue is clear after the request FAILS', async () => {
    mockApi.mockRejectedValue(new ApiError(500, 'server exploded'))

    render(<PendingTab />)

    expect(await screen.findByText('server exploded')).toBeInTheDocument()
    expect(screen.queryByText(CLEAR)).toBeNull()
  })

  it('claims it only once a request has actually returned an empty queue', async () => {
    mockApi.mockResolvedValue([])

    render(<PendingTab />)

    expect(await screen.findByText(CLEAR)).toBeInTheDocument()
  })
})

describe('PendingTab — the list', () => {
  it('lists each pending user by email', async () => {
    mockApi.mockResolvedValue([ALICE, BOB])

    render(<PendingTab />)

    expect(await screen.findByText('alice@corp.com')).toBeInTheDocument()
    expect(screen.getByText('bob@corp.com')).toBeInTheDocument()
    expect(screen.queryByText(CLEAR)).toBeNull()
  })

  it('says where each signup is headed — a named org, or a personal one', async () => {
    // The destination decides what approving actually does, so it belongs on
    // the row rather than being discovered afterwards.
    mockApi.mockResolvedValue([ALICE, BOB])

    render(<PendingTab />)

    expect(await screen.findByText(/Alice → personal/)).toBeInTheDocument()
    expect(screen.getByText(/→ joining Acme/)).toBeInTheDocument()
  })

  it('falls back to a dash when a signup carries no display name', async () => {
    mockApi.mockResolvedValue([BOB])

    render(<PendingTab />)

    expect(await screen.findByText(/—\s*→ joining Acme/)).toBeInTheDocument()
  })
})

describe('PendingTab — approve and reject', () => {
  it('approves through the admin route, then refetches the queue', async () => {
    // The refetch is the point: without it the approved row stays on screen
    // and invites a second click on a user who is already in.
    mockApi.mockResolvedValueOnce([ALICE]).mockResolvedValueOnce(undefined).mockResolvedValueOnce([])
    render(<PendingTab />)
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Approve'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/users/u1/approve', { method: 'POST' }))
    expect(await screen.findByText(CLEAR)).toBeInTheDocument()
  })

  it('rejects through the admin route', async () => {
    mockApi.mockResolvedValueOnce([ALICE]).mockResolvedValueOnce(undefined).mockResolvedValueOnce([])
    render(<PendingTab />)
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Reject'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/users/u1/reject', { method: 'POST' }))
  })

  it('disables BOTH buttons on the row being acted on, and only that row', async () => {
    mockApi.mockResolvedValueOnce([ALICE, BOB]).mockImplementationOnce(() => new Promise(() => {}))
    render(<PendingTab />)
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getAllByText('Approve')[0])

    await waitFor(() => expect(screen.getAllByText('Approve')[0]).toBeDisabled())
    expect(screen.getAllByText('Reject')[0]).toBeDisabled()
    expect(screen.getAllByText('Approve')[1]).toBeEnabled()
  })

  it('surfaces a failed action and re-enables the row', async () => {
    mockApi.mockResolvedValueOnce([ALICE]).mockRejectedValueOnce(new ApiError(403, 'Not permitted'))
    render(<PendingTab />)
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Approve'))

    expect(await screen.findByText('Not permitted')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByText('Approve')).toBeEnabled())
  })

  it('falls back to a generic message when the failure is not an ApiError', async () => {
    mockApi.mockResolvedValueOnce([ALICE]).mockRejectedValueOnce(new Error('socket hang up'))
    render(<PendingTab />)
    await screen.findByText('alice@corp.com')

    fireEvent.click(screen.getByText('Approve'))

    expect(await screen.findByText('Action failed')).toBeInTheDocument()
  })

  it('clears a previous error once a later reload succeeds', async () => {
    // load() sets error null before it fetches. Without that, an action that
    // failed once leaves its message under a list that has since refreshed
    // correctly.
    mockApi
      .mockResolvedValueOnce([ALICE])                              // initial load
      .mockRejectedValueOnce(new ApiError(403, 'Not permitted'))   // approve fails
      .mockResolvedValueOnce(undefined)                            // approve succeeds
      .mockResolvedValueOnce([])                                   // reload: empty
    render(<PendingTab />)
    await screen.findByText('alice@corp.com')
    fireEvent.click(screen.getByText('Approve'))
    await screen.findByText('Not permitted')

    fireEvent.click(screen.getByText('Approve'))

    await waitFor(() => expect(screen.queryByText('Not permitted')).toBeNull())
    expect(await screen.findByText(CLEAR)).toBeInTheDocument()
  })
})
