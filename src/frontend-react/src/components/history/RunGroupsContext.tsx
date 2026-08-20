/**
 * Run groups — ONE source of truth shared by the sidebar and the Test Runs page.
 *
 * Both surfaces can change the active group (the sidebar's quick-access list,
 * the page's chip row and browse dialog) and both must reflect what the other
 * did. Two independent copies of that state would drift the moment a user
 * picked a group in one place and a different one in the other, so the filter
 * and the group list live here instead — mounted above both consumers, so
 * there is exactly one value and no synchronisation to get wrong.
 *
 * It also owns the single /api/groups fetch: one list means a rename made on
 * the page relabels the sidebar entry immediately, with no second request and
 * no stale copy.
 *
 * Deliberately NOT in the URL. This app keeps every page mounted (see the
 * keep-alive note in App.tsx) so page state survives navigation; a ?group=
 * param would be dropped the moment the user visited another page, silently
 * clearing their filter and firing a refetch on a hidden page.
 *
 * Referenced by: App.tsx (provider), app-sidebar.tsx, pages/HistoryPage.tsx.
 * Depends on: ./useGroups.
 */
import {
  createContext, useContext, useEffect, useMemo, useState, type ReactNode,
} from 'react'
import { useGroups, type GroupChanges, type GroupVisibility, type RunGroup } from './useGroups'

/** null = no group filter; 'ungrouped' = runs in no group; otherwise a group_id. */
export type GroupFilter = string | null

interface RunGroupsValue {
  groups: RunGroup[]
  ungroupedCount: number
  error: string
  loaded: boolean
  refresh: () => Promise<void>
  createGroup: (name: string, visibility?: GroupVisibility) => Promise<RunGroup>
  updateGroup: (groupId: string, changes: GroupChanges) => Promise<void>
  deleteGroup: (groupId: string) => Promise<void>
  assignRuns: (runIds: string[], groupId: string | null) => Promise<void>
  groupFilter: GroupFilter
  setGroupFilter: (value: GroupFilter) => void
}

const RunGroupsContext = createContext<RunGroupsValue | null>(null)

export function RunGroupsProvider({ children }: { children: ReactNode }) {
  const {
    groups, ungroupedCount, error, loaded,
    refresh, createGroup, updateGroup, deleteGroup, assignRuns,
  } = useGroups()
  const [groupFilter, setGroupFilter] = useState<GroupFilter>(null)

  // A filter can outlive its group — deleted in a second tab, or by another
  // session. Left alone it would show an empty table under a group chip that
  // no longer resolves to a name. Wait for the list to actually load (an empty
  // list mid-fetch is not proof of absence), then drop the dangling id.
  useEffect(() => {
    if (!loaded || !groupFilter || groupFilter === 'ungrouped') return
    if (!groups.some(g => g.group_id === groupFilter)) setGroupFilter(null)
  }, [loaded, groups, groupFilter])

  const value = useMemo<RunGroupsValue>(() => ({
    groups, ungroupedCount, error, loaded,
    refresh, createGroup, updateGroup, deleteGroup, assignRuns,
    groupFilter, setGroupFilter,
  }), [
    groups, ungroupedCount, error, loaded,
    refresh, createGroup, updateGroup, deleteGroup, assignRuns,
    groupFilter,
  ])

  return <RunGroupsContext.Provider value={value}>{children}</RunGroupsContext.Provider>
}

export function useRunGroups(): RunGroupsValue {
  const ctx = useContext(RunGroupsContext)
  if (!ctx) throw new Error('useRunGroups must be used inside <RunGroupsProvider>')
  return ctx
}
