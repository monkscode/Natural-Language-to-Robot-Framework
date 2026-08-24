/**
 * useGroups — the three behaviours a type check cannot see.
 *
 * Every test drives the hook through a MOCKED lib/api, so nothing here
 * touches the network. The mock hands back deferred promises where the test
 * needs to control which response lands first.
 */
import { act, renderHook, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useGroups, type RunGroup } from './useGroups'

vi.mock('@/lib/api', () => ({ api: vi.fn() }))
const { api } = await import('@/lib/api')
const apiMock = vi.mocked(api)

const group = (name: string): RunGroup => ({
  group_id: `id-${name}`, name, created_by: 'u1', run_count: 0,
})

/** One controllable promise per api() call, in call order. */
function deferQueue() {
  const pending: Array<(value: unknown) => void> = []
  apiMock.mockImplementation(() => new Promise(resolve => { pending.push(resolve) }))
  return pending
}

beforeEach(() => {
  apiMock.mockReset()
})

describe('useGroups', () => {
  it('ignores a stale refresh response that lands after a newer one', async () => {
    const pending = deferQueue()
    const { result } = renderHook(() => useGroups())
    await waitFor(() => expect(pending).toHaveLength(1))   // the mount fetch

    // Two overlapping refreshes — a mutation's, then the user's Refresh.
    await act(async () => { void result.current.refresh(); void result.current.refresh() })
    expect(pending).toHaveLength(3)

    // The NEWEST request answers first, the older ones after it. Without the
    // sequence guard the last RESPONSE would win and repaint the chips with
    // a list the user already moved on from.
    await act(async () => { pending[2]({ groups: [group('Newest')], ungrouped_count: 7 }) })
    await act(async () => { pending[1]({ groups: [group('Older')], ungrouped_count: 1 }) })
    await act(async () => { pending[0]({ groups: [group('Oldest')], ungrouped_count: 2 }) })

    expect(result.current.groups.map(g => g.name)).toEqual(['Newest'])
    expect(result.current.ungroupedCount).toBe(7)
  })

  it('flips loaded true on a FAILED fetch, and keeps the reason', async () => {
    // loaded is "the first fetch settled", not "the first fetch succeeded" —
    // consumers wait on it to tell an empty list from an unloaded one, and a
    // failure that never flipped it would hang them there forever.
    apiMock.mockRejectedValue(new Error('Service unavailable'))
    const { result } = renderHook(() => useGroups())

    await waitFor(() => expect(result.current.loaded).toBe(true))
    expect(result.current.error).toBe('Service unavailable')
    expect(result.current.groups).toEqual([])
  })

  it('refetches the list after a mutation', async () => {
    apiMock.mockResolvedValue({ groups: [], ungrouped_count: 0 })
    const { result } = renderHook(() => useGroups())
    await waitFor(() => expect(result.current.loaded).toBe(true))
    apiMock.mockClear()

    apiMock
      .mockResolvedValueOnce(group('Checkout'))
      .mockResolvedValueOnce({ groups: [group('Checkout')], ungrouped_count: 0 })
    await act(async () => { await result.current.createGroup('Checkout') })

    // The mutation, then the re-read — chip counts stay honest without the
    // caller having to know what changed.
    expect(apiMock).toHaveBeenCalledTimes(2)
    expect(apiMock.mock.calls[0][0]).toBe('/api/groups')
    expect(apiMock.mock.calls[0][1]).toMatchObject({ method: 'POST' })
    expect(apiMock.mock.calls[1]).toEqual(['/api/groups'])
    expect(result.current.groups.map(g => g.name)).toEqual(['Checkout'])
  })
})
