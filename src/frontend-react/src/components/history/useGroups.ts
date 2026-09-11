/**
 * Org run-groups state for the Tests and Activity pages and the sidebar.
 *
 * Folders belong to the caller's ORGANISATION — every member sees every
 * folder and every run filed into one, so there is no per-folder visibility
 * to model here. Owns the /api/groups list (with live run and test counts)
 * and every group mutation. Each mutation re-fetches the list so chip counts stay
 * honest without the callers having to know what changed; `refresh` is also
 * exported so the page can re-sync counts after something OTHER than a
 * mutation changed them — a re-run, or the Refresh button. Server errors
 * (409 duplicate name, 404 folder/run outside the caller's reach, 403 no
 * identity or no org) surface as thrown ApiError with the server's
 * human-readable detail — dialogs display e.message directly.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '@/lib/api'

export interface RunGroup {
  group_id: string
  name: string
  /** user_id of the creator. Rename: creator OR can_manage_org_folders.
   *  Delete: can_manage_org_folders alone — this field plays no part. */
  created_by: string
  /** The folder's WHOLE contents, org-wide — not the caller's share of it. */
  run_count: number
  /** How many TESTS are filed there, org-wide — the Tests page's count, as
   *  run_count is Activity's. POST /api/groups answers 0 for a new folder. */
  test_count: number
}

export function useGroups() {
  const [groups, setGroups] = useState<RunGroup[]>([])
  const [ungroupedCount, setUngroupedCount] = useState(0)
  // The same chip on the Tests page counts TESTS filed nowhere, a different
  // population from the results ungroupedCount counts.
  const [ungroupedTestCount, setUngroupedTestCount] = useState(0)
  const [error, setError] = useState('')
  // First fetch settled (success OR failure). Consumers need this to tell
  // "no groups exist" apart from "the list hasn't arrived yet" — without it
  // they would act on an empty list during the initial render.
  const [loaded, setLoaded] = useState(false)
  // Guards against refresh() calls resolving out of order: a mutation's
  // refresh can overlap the mount fetch or another mutation's refresh, and
  // whichever RESPONSE lands last would otherwise win regardless of which
  // REQUEST was issued last. Only the request holding the current sequence
  // number when it settles is allowed to apply its result.
  const refreshSeq = useRef(0)

  const refresh = useCallback(async () => {
    const seq = ++refreshSeq.current
    try {
      const data = await api<{
        groups: RunGroup[]; ungrouped_count: number; ungrouped_test_count: number
      }>('/api/groups')
      if (seq === refreshSeq.current) {
        setGroups(data.groups)
        setUngroupedCount(data.ungrouped_count)
        setUngroupedTestCount(data.ungrouped_test_count)
        setError('')
      }
    } catch (e) {
      if (seq === refreshSeq.current) {
        setError(e instanceof Error ? e.message : 'Failed to load groups')
      }
    } finally {
      if (seq === refreshSeq.current) setLoaded(true)
    }
  }, [])

  useEffect(() => { void refresh() }, [refresh])

  const createGroup = useCallback(async (name: string): Promise<RunGroup> => {
    const g = await api<RunGroup>('/api/groups', {
      method: 'POST', body: JSON.stringify({ name }),
    })
    await refresh()
    return g
  }, [refresh])

  // The response body is deliberately discarded (it carries only what
  // changed) — refresh() is the source of truth.
  const renameGroup = useCallback(async (groupId: string, name: string) => {
    await api(`/api/groups/${groupId}`, {
      method: 'PATCH', body: JSON.stringify({ name }),
    })
    await refresh()
  }, [refresh])

  const deleteGroup = useCallback(async (groupId: string) => {
    await api(`/api/groups/${groupId}`, { method: 'DELETE' })
    await refresh()
  }, [refresh])

  const assignRuns = useCallback(async (runIds: string[], groupId: string | null) => {
    await api('/api/groups/assignments', {
      method: 'PUT', body: JSON.stringify({ run_ids: runIds, group_id: groupId }),
    })
    await refresh()
  }, [refresh])

  // Files TESTS — PUT /api/tests/assignments, the Tests page's Move. A test's
  // results follow it by join, so both chip counts move and the list is
  // re-read exactly as it is after every other mutation here.
  const assignTests = useCallback(async (testIds: string[], groupId: string | null) => {
    await api('/api/tests/assignments', {
      method: 'PUT', body: JSON.stringify({ test_ids: testIds, group_id: groupId }),
    })
    await refresh()
  }, [refresh])

  return {
    groups, ungroupedCount, ungroupedTestCount, error, loaded,
    refresh, createGroup, renameGroup, deleteGroup, assignRuns, assignTests,
  }
}
