/**
 * RunGroupsContext — the dangling-filter rule, which is the only logic the
 * provider owns beyond passing useGroups' state through.
 *
 * A filter can outlive its folder (deleted in another tab, or by a
 * colleague). Left alone the page renders an empty table under a chip that
 * resolves to no name, so the provider drops it — but only once it actually
 * knows the folder is gone.
 */
import { act, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { RunGroupsProvider, useRunGroups } from './RunGroupsContext'
import type { GroupFilter } from './RunGroupsContext'
import type { RunGroup } from './useGroups'

vi.mock('@/lib/api', () => ({ api: vi.fn() }))
const { api } = await import('@/lib/api')
const apiMock = vi.mocked(api)

const group = (name: string): RunGroup => ({
  group_id: `id-${name}`, name, created_by: 'u1', run_count: 0,
})

/** Renders the live filter and hands the test a way to set it. */
let setFilter: (value: GroupFilter) => void
function Probe() {
  const { groupFilter, setGroupFilter, loaded, error } = useRunGroups()
  setFilter = setGroupFilter
  return (
    <div>
      <span data-testid="filter">{groupFilter ?? 'none'}</span>
      <span data-testid="loaded">{String(loaded)}</span>
      <span data-testid="error">{error || 'none'}</span>
    </div>
  )
}

const renderProvider = () =>
  render(<RunGroupsProvider><Probe /></RunGroupsProvider>)

beforeEach(() => {
  apiMock.mockReset()
})

describe('RunGroupsContext', () => {
  it('drops a filter naming a folder the loaded list does not have', async () => {
    let land: (value: unknown) => void = () => {}
    apiMock.mockImplementation(() => new Promise(resolve => { land = resolve }))
    renderProvider()

    // Set while the list is still in flight: an empty list mid-fetch is not
    // proof the folder is gone, so nothing may be dropped yet.
    await act(async () => { setFilter('id-Deleted') })
    expect(screen.getByTestId('filter').textContent).toBe('id-Deleted')

    await act(async () => { land({ groups: [group('Checkout')], ungrouped_count: 0 }) })

    await waitFor(() => expect(screen.getByTestId('filter').textContent).toBe('none'))
  })

  it('keeps a dangling filter while the list FAILED to load', async () => {
    // loaded flips true on failure too, and `groups` keeps its previous value
    // — so an empty list here says nothing about whether the folder exists.
    // Clearing on a transient 503 would throw away the user's view.
    apiMock.mockRejectedValue(new Error('Service unavailable'))
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('loaded').textContent).toBe('true'))
    expect(screen.getByTestId('error').textContent).toBe('Service unavailable')

    await act(async () => { setFilter('id-Deleted') })

    expect(screen.getByTestId('filter').textContent).toBe('id-Deleted')
  })

  it('never treats "ungrouped" as dangling', async () => {
    // It is a pseudo-filter, not a group_id — no folder will ever match it,
    // so an id-equality test would clear it the instant the list arrived.
    apiMock.mockResolvedValue({ groups: [group('Checkout')], ungrouped_count: 3 })
    renderProvider()
    await waitFor(() => expect(screen.getByTestId('loaded').textContent).toBe('true'))

    await act(async () => { setFilter('ungrouped') })

    expect(screen.getByTestId('filter').textContent).toBe('ungrouped')
  })
})
