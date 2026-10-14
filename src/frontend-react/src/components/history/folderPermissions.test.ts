/**
 * folderPermissions — which folder controls a caller is offered.
 *
 * Hints only: the server refuses every folder mutation it does not allow. But
 * a hint that is wrong in the refusing direction hides a control that would
 * have worked — the defect that once drew no Delete for any solo user while
 * DELETE /api/groups/{id} answered 204 for them. Two pages now draw the same
 * controls, so the rule lives here once.
 */
import { describe, expect, it } from 'vitest'

import type { User } from '@/auth/AuthContext'
import { canDeleteFolder, canRenameFolder } from './folderPermissions'
import type { RunGroup } from './useGroups'

const FOLDER: RunGroup = {
  group_id: 'g-1', name: 'Checkout', created_by: 'u-creator', run_count: 0, test_count: 0,
}

const user = (over: Partial<User> = {}): User => ({
  id: 'u-someone', email: 's@x.com', display_name: 'S', role: 'user', status: 'active',
  ...over,
})

describe('canRenameFolder', () => {
  it('lets the folder’s creator rename it', () => {
    expect(canRenameFolder(user({ id: 'u-creator' }), FOLDER)).toBe(true)
  })

  it('lets a folder authority rename a folder someone else made', () => {
    expect(canRenameFolder(user({ can_manage_org_folders: true }), FOLDER)).toBe(true)
  })

  it('refuses anyone else, and the caller with no identity', () => {
    expect(canRenameFolder(user(), FOLDER)).toBe(false)
    expect(canRenameFolder(null, FOLDER)).toBe(false)
  })
})

describe('canDeleteFolder', () => {
  it('is the folder authority alone — not the creator', () => {
    expect(canDeleteFolder(user({ can_manage_org_folders: true }))).toBe(true)
    expect(canDeleteFolder(user({ id: 'u-creator' }))).toBe(false)
  })

  it('reads can_manage_org_folders, never is_org_admin', () => {
    // A solo user is org_admin of their personal org: can_manage_org_folders
    // true, is_org_admin (a TEAM-org signal) false. Reading the wrong one is
    // exactly the "no Delete for any new signup" defect.
    expect(canDeleteFolder(user({ can_manage_org_folders: true, is_org_admin: false }))).toBe(true)
    expect(canDeleteFolder(user({ can_manage_org_folders: false, is_org_admin: true }))).toBe(false)
    expect(canDeleteFolder(null)).toBe(false)
  })
})
