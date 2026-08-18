/**
 * Personal run-groups state for the History page.
 *
 * Owns the /api/groups list (with live run counts) and every group mutation.
 * Each mutation re-fetches the list so chip counts stay honest without the
 * callers having to know what changed. Server errors (409 duplicate name,
 * 404 foreign group/run) surface as thrown ApiError with the server's
 * human-readable detail — dialogs display e.message directly.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/api'

export interface RunGroup {
  group_id: string
  name: string
  run_count: number
}

export function useGroups() {
  const [groups, setGroups] = useState<RunGroup[]>([])
  const [ungroupedCount, setUngroupedCount] = useState(0)
  const [error, setError] = useState('')
  // First fetch settled (success OR failure). Consumers need this to tell
  // "no groups exist" apart from "the list hasn't arrived yet" — without it
  // they would act on an empty list during the initial render.
  const [loaded, setLoaded] = useState(false)

  const refresh = useCallback(async () => {
    try {
      const data = await api<{ groups: RunGroup[]; ungrouped_count: number }>('/api/groups')
      setGroups(data.groups)
      setUngroupedCount(data.ungrouped_count)
      setError('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load groups')
    } finally {
      setLoaded(true)
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

  return {
    groups, ungroupedCount, error, loaded,
    refresh, createGroup, renameGroup, deleteGroup, assignRuns,
  }
}
