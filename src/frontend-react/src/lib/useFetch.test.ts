/**
 * useFetch — the hook's own tests. It had none before this file, while being
 * called from 16 places, which is why the staleness defect below survived.
 *
 * What this pins, in the order it matters:
 *
 *  - The defect: a fetch that FAILS for a path the data on screen does not
 *    belong to must not leave that data on screen. Three call sites build
 *    their path from a filter that changes while mounted and render the error
 *    banner and the rows as SIBLINGS rather than as an early return
 *    (LearningPage.tsx's hints and runs tables, TriggersTab.tsx's list), so
 *    before this the banner appeared above the PREVIOUS filter's rows and its
 *    previous total.
 *
 *  - The behaviour that must survive the fix: on the SUCCESS path a path
 *    change must not blank the data first. Two of those three sites write
 *    `{loading && !data && <Loading…/>}` precisely so the old rows stay put
 *    while the new ones load, and clearing data on every path change defeats
 *    that guard — measured, it commits BLANK then LOADING then the rows,
 *    three states where there was one, on every filter click and every
 *    debounced keystroke.
 *
 *  - The other behaviour that must survive it: a SAME-path reload() that
 *    fails keeps its data, under the banner. That is not staleness, it is the
 *    last good view of the same screen, and six call sites render it
 *    deliberately. LearningPage.tsx's act() awaits reload() after every
 *    unflag/retract/reactivate, so this is the live path, not a hypothetical.
 *
 * Together those three are why the fix is scoped to "the data belongs to a
 * different path", and is neither "clear on every path change" nor "clear on
 * every error" — each of which fixes the defect but breaks one of the other
 * two.
 */
import { renderHook, waitFor, act } from '@testing-library/react'
import { StrictMode, createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./api', () => ({ api: vi.fn() }))

import { api } from './api'
import { useFetch } from './useFetch'

const mockApi = vi.mocked(api)

beforeEach(() => mockApi.mockReset())
afterEach(() => vi.resetAllMocks())

const ROWS_A = { total: 122, rows: ['a'] }
const ROWS_B = { total: 48, rows: ['b'] }

describe('useFetch — the basics it never had a test for', () => {
  it('fetches on mount and returns the payload', async () => {
    mockApi.mockResolvedValue(ROWS_A)

    const { result } = renderHook(() => useFetch<typeof ROWS_A>('/runs'))

    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))
    expect(mockApi).toHaveBeenCalledWith('/runs')
    expect(result.current.loading).toBe(false)
    expect(result.current.error).toBe('')
  })

  it('does not fetch at all when the path is null, and settles loading to false', async () => {
    const { result } = renderHook(() => useFetch<typeof ROWS_A>(null))

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(mockApi).not.toHaveBeenCalled()
    expect(result.current.data).toBeNull()
  })

  it('surfaces the error message and leaves data null when the FIRST fetch fails', async () => {
    mockApi.mockRejectedValue(new Error('nope'))

    const { result } = renderHook(() => useFetch<typeof ROWS_A>('/runs'))

    await waitFor(() => expect(result.current.error).toBe('nope'))
    expect(result.current.data).toBeNull()
  })

  it('ignores a response that lands after the path has already moved on', async () => {
    // The monotonic seq guard. A resolves LAST but is no longer the newest
    // request, so its payload must not overwrite B's.
    let resolveA: (v: unknown) => void = () => {}
    mockApi.mockImplementationOnce(() => new Promise(r => { resolveA = r }))
    mockApi.mockResolvedValueOnce(ROWS_B)

    const { result, rerender } = renderHook(({ p }) => useFetch<typeof ROWS_A>(p), {
      initialProps: { p: '/runs' },
    })
    rerender({ p: '/runs?status=failed' })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_B))

    await act(async () => { resolveA(ROWS_A) })

    expect(result.current.data).toEqual(ROWS_B)
  })
})

describe('useFetch — data must not outlive the path it answered', () => {
  it('clears data when a fetch FAILS for a path the data does not belong to', async () => {
    // The defect. Filter A loaded 122 rows; the user clicks a filter whose
    // read the server refuses. Before the fix, `data` stayed — so the caller
    // rendered its error banner above filter A's rows and its "122 runs".
    mockApi.mockResolvedValueOnce(ROWS_A)
    mockApi.mockRejectedValueOnce(new Error('403 refused'))

    const { result, rerender } = renderHook(({ p }) => useFetch<typeof ROWS_A>(p), {
      initialProps: { p: '/runs' },
    })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))

    rerender({ p: '/runs?status=failed' })

    await waitFor(() => expect(result.current.error).toBe('403 refused'))
    expect(result.current.data).toBeNull()
  })

  it('keeps data through a SUCCESSFUL path change, never nulling it in between', async () => {
    // Keep-previous-data. If this regresses, `{loading && !data && …}` at
    // LearningPage.tsx and TriggersTab.tsx starts flashing a blank table and
    // then "Loading…" on every filter click.
    mockApi.mockResolvedValueOnce(ROWS_A)
    mockApi.mockResolvedValueOnce(ROWS_B)

    const seen: (typeof ROWS_A | null)[] = []
    const { result, rerender } = renderHook(({ p }) => {
      const r = useFetch<typeof ROWS_A>(p)
      seen.push(r.data)
      return r
    }, { initialProps: { p: '/runs' } })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))
    const from = seen.length

    rerender({ p: '/runs?status=failed' })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_B))

    // Every render between the two payloads held one of them — never null.
    expect(seen.slice(from)).not.toContain(null)
  })

  it('keeps data when a SAME-path reload() fails', async () => {
    // Not staleness: the last good view of the same screen, under a banner.
    // LearningPage.tsx's act() awaits reload() after every hint mutation.
    mockApi.mockResolvedValueOnce(ROWS_A)
    mockApi.mockRejectedValueOnce(new Error('500 on refresh'))

    const { result } = renderHook(() => useFetch<typeof ROWS_A>('/runs'))
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))

    await act(async () => { await result.current.reload() })

    expect(result.current.error).toBe('500 on refresh')
    expect(result.current.data).toEqual(ROWS_A)
  })

  it('clears data when returning to a path whose refetch fails after another path succeeded', async () => {
    // A succeeds, B succeeds (so the data on screen is B's), then back to A
    // and A fails. B's rows must not sit under an A-shaped banner.
    mockApi.mockResolvedValueOnce(ROWS_A)
    mockApi.mockResolvedValueOnce(ROWS_B)
    mockApi.mockRejectedValueOnce(new Error('A refused'))

    const { result, rerender } = renderHook(({ p }) => useFetch<typeof ROWS_A>(p), {
      initialProps: { p: '/runs' },
    })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))
    rerender({ p: '/runs?status=failed' })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_B))

    rerender({ p: '/runs' })

    await waitFor(() => expect(result.current.error).toBe('A refused'))
    expect(result.current.data).toBeNull()
  })

  it('a failed path change followed by a successful one recovers, with no residue', async () => {
    mockApi.mockResolvedValueOnce(ROWS_A)
    mockApi.mockRejectedValueOnce(new Error('transient'))
    mockApi.mockResolvedValueOnce(ROWS_B)

    const { result, rerender } = renderHook(({ p }) => useFetch<typeof ROWS_A>(p), {
      initialProps: { p: '/runs' },
    })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))
    rerender({ p: '/runs?status=passed' })
    await waitFor(() => expect(result.current.data).toBeNull())

    rerender({ p: '/runs?status=failed' })

    await waitFor(() => expect(result.current.data).toEqual(ROWS_B))
    expect(result.current.error).toBe('')
  })
})

// StrictMode is ON in this app (main.tsx wraps <App/> in it), so in dev every
// effect mounts, unmounts and mounts again — reload() fires twice per mount and
// the FIRST request is superseded before it resolves. These re-run the two
// behaviours that matter under that double-invocation, because the rest of this
// file renders without it and would not catch a regression that only appears
// there.
const strict = {
  wrapper: ({ children }: { children: ReactNode }) => createElement(StrictMode, null, children),
}

describe('useFetch — under StrictMode double-invocation', () => {
  it('still clears data on a cross-path failure', async () => {
    mockApi.mockResolvedValue(ROWS_A)
    const { result, rerender } = renderHook(({ p }) => useFetch<typeof ROWS_A>(p), {
      initialProps: { p: '/runs' }, ...strict,
    })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))

    mockApi.mockRejectedValue(new Error('403 refused'))
    rerender({ p: '/runs?status=failed' })

    await waitFor(() => expect(result.current.error).toBe('403 refused'))
    expect(result.current.data).toBeNull()
  })

  it('still keeps data when a same-path reload() fails', async () => {
    mockApi.mockResolvedValue(ROWS_A)
    const { result } = renderHook(() => useFetch<typeof ROWS_A>('/runs'), strict)
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))

    mockApi.mockRejectedValue(new Error('500 on refresh'))
    await act(async () => { await result.current.reload() })

    expect(result.current.data).toEqual(ROWS_A)
  })
})

describe('useFetch — paths that pass through null', () => {
  // Two call sites do this: HistoryPage's detail and corrections fetches are
  // `selected ? \`/api/.../${selected}\` : null`, so closing the drawer sends
  // the path to null and reload() early-returns without touching anything.
  it('clears data when the path goes A -> null -> B and B fails', async () => {
    mockApi.mockResolvedValueOnce(ROWS_A)
    const { result, rerender } = renderHook(({ p }) => useFetch<typeof ROWS_A>(p), {
      initialProps: { p: '/runs' as string | null },
    })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))

    rerender({ p: null })
    expect(result.current.data).toEqual(ROWS_A)   // nothing runs on a null path

    mockApi.mockRejectedValueOnce(new Error('B refused'))
    rerender({ p: '/runs?status=failed' })

    await waitFor(() => expect(result.current.error).toBe('B refused'))
    expect(result.current.data).toBeNull()
  })

  it('KEEPS data when the path goes A -> null -> A and A fails', async () => {
    // Reopening the SAME row. What is on screen is still that path's own
    // answer, so this is the keep case, not the drop case.
    mockApi.mockResolvedValueOnce(ROWS_A)
    const { result, rerender } = renderHook(({ p }) => useFetch<typeof ROWS_A>(p), {
      initialProps: { p: '/runs' as string | null },
    })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))

    rerender({ p: null })
    mockApi.mockRejectedValueOnce(new Error('A refused'))
    rerender({ p: '/runs' })

    await waitFor(() => expect(result.current.error).toBe('A refused'))
    expect(result.current.data).toEqual(ROWS_A)
  })
})

describe('useFetch — teardown and pile-ups', () => {
  it('does not throw when the component unmounts with a request still in flight', async () => {
    let settle: (v: unknown) => void = () => {}
    mockApi.mockImplementationOnce(() => new Promise(r => { settle = r }))
    const { unmount } = renderHook(() => useFetch<typeof ROWS_A>('/runs'))

    unmount()

    await act(async () => { settle(ROWS_A) })
    // Reaching here without an unhandled rejection or a React warning IS the
    // assertion; the explicit one keeps the test from being vacuous.
    expect(mockApi).toHaveBeenCalledTimes(1)
  })

  it('does not throw when the component unmounts with a request about to FAIL', async () => {
    let boom: (e: unknown) => void = () => {}
    mockApi.mockImplementationOnce(() => new Promise((_, rej) => { boom = rej }))
    const { unmount } = renderHook(() => useFetch<typeof ROWS_A>('/runs'))

    unmount()

    await act(async () => { boom(new Error('late failure')) })
    expect(mockApi).toHaveBeenCalledTimes(1)
  })

  it('a superseded request that FAILS late never clears the current path’s data', async () => {
    // A -> B -> C where B rejects after C has already landed. B is not the
    // newest request, so its catch must not run at all - if it did, its clear
    // would wipe C's perfectly good data.
    let failB: (e: unknown) => void = () => {}
    mockApi.mockResolvedValueOnce(ROWS_A)
    mockApi.mockImplementationOnce(() => new Promise((_, rej) => { failB = rej }))
    mockApi.mockResolvedValueOnce(ROWS_B)

    const { result, rerender } = renderHook(({ p }) => useFetch<typeof ROWS_A>(p), {
      initialProps: { p: '/runs' },
    })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_A))
    rerender({ p: '/runs?status=passed' })
    rerender({ p: '/runs?status=failed' })
    await waitFor(() => expect(result.current.data).toEqual(ROWS_B))

    await act(async () => { failB(new Error('late B failure')) })

    expect(result.current.data).toEqual(ROWS_B)
    expect(result.current.error).toBe('')
  })
})
