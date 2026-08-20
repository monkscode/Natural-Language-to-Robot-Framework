/**
 * Org run-groups state for the History page.
 *
 * Folders are keyed to the caller's ORGANISATION, each with its own
 * visibility: 'org' (every member sees it) or 'private' (only its creator).
 * Owns the /api/groups list (with live run counts) and every group mutation.
 * Each mutation re-fetches the list so chip counts stay honest without the
 * callers having to know what changed. Server errors (409 duplicate name,
 * 409 org->private with other members' runs inside, 404 invisible group/run,
 * 403 no identity or no org) surface as thrown ApiError with the server's
 * human-readable detail — dialogs display e.message directly.
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '@/lib/api'

/** 'org' = everyone in the organisation; 'private' = only the creator. */
export type GroupVisibility = 'private' | 'org'

export interface RunGroup {
  group_id: string
  name: string
  visibility: GroupVisibility
  /** user_id of the creator — with is_org_admin, decides who may manage it. */
  created_by: string
  run_count: number
}

/** PATCH takes either field; at least one, or the server answers 400. */
export interface GroupChanges {
  name?: string
  visibility?: GroupVisibility
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

  const createGroup = useCallback(async (
    name: string, visibility: GroupVisibility = 'org',
  ): Promise<RunGroup> => {
    const g = await api<RunGroup>('/api/groups', {
      method: 'POST', body: JSON.stringify({ name, visibility }),
    })
    await refresh()
    return g
  }, [refresh])

  // One function, one PATCH: name and visibility travel together so flipping a
  // folder to private while renaming it is a single request the server can
  // accept or refuse as a whole. The response body is deliberately discarded
  // (it carries only what changed) — refresh() is the source of truth.
  const updateGroup = useCallback(async (groupId: string, changes: GroupChanges) => {
    await api(`/api/groups/${groupId}`, {
      method: 'PATCH', body: JSON.stringify(changes),
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
    refresh, createGroup, updateGroup, deleteGroup, assignRuns,
  }
}
