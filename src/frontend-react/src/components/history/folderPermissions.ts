/**
 * Which folder-management controls to OFFER a caller.
 *
 * These mirror the server's rules, which differ per action. They are hints
 * only — the server 404s any folder the caller may not mutate either way —
 * but a hint wrong in the refusing direction hides a control that works.
 * Moved here from HistoryPage when the Tests page began drawing the same
 * controls, so the two pages cannot offer them by different rules.
 *
 * Referenced by: pages/HistoryPage.tsx, pages/TestsPage.tsx.
 * Depends on: auth/AuthContext (User), ./useGroups (RunGroup).
 */
import type { User } from '@/auth/AuthContext'
import type { RunGroup } from './useGroups'

/** Rename: the folder's creator, or a folder authority. */
export function canRenameFolder(user: User | null, g: RunGroup): boolean {
  return !!user && (g.created_by === user.id || user.can_manage_org_folders === true)
}

/**
 * Delete: a folder authority alone. Narrower on purpose: deleting a folder
 * returns everything inside it to Ungrouped, which un-shares it from the
 * whole org. That consequence is the org's, so the authority is an
 * org-admin's — not the folder creator's.
 *
 * can_manage_org_folders, NOT is_org_admin. The two are different questions:
 * is_org_admin is is_team_admin(), team orgs only, and gates the Team page;
 * folder authority is the org_role claim, which ensure_personal_org grants
 * every user over their own personal org. Reading the wrong one drew no
 * Delete control for any solo user while DELETE /api/groups/{id} answered
 * 204 for them — i.e. for every new signup.
 */
export function canDeleteFolder(user: User | null): boolean {
  return !!user && user.can_manage_org_folders === true
}
