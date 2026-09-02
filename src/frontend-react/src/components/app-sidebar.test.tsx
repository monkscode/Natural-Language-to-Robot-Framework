/**
 * AppSidebar nav gating — the Learning nav item must never disagree with the
 * page it links to (App.test.tsx covers the page half; see its header for
 * why /learning moved off `admin: true`). navItemAllowed is NavGroup's
 * filter predicate, exported here so this is testable without rendering the
 * sidebar — this package tests no page components (see
 * FeedbackPanel.test.tsx / LearningPage.test.tsx).
 */
import { describe, expect, it } from 'vitest'
import { NAV_PLATFORM, navItemAllowed } from './app-sidebar'

const learningItem = NAV_PLATFORM.find(i => i.url === '/learning')!

describe('the Learning NAV_PLATFORM entry', () => {
  it('gates on viewLearning, not admin', () => {
    expect(learningItem.viewLearning).toBe(true)
    expect(learningItem.admin).toBe(false)
  })
})

describe('navItemAllowed for the Learning item', () => {
  it('is shown for an org admin: can_view_learning true, role user', () => {
    expect(navItemAllowed(learningItem, { isAdmin: false, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })

  it('is hidden when can_view_learning is false, role user', () => {
    expect(navItemAllowed(learningItem, { isAdmin: false, isOrgAdmin: false, canViewLearning: false })).toBe(false)
  })

  it('is still shown for a platform admin (no regression)', () => {
    expect(navItemAllowed(learningItem, { isAdmin: true, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })
})
