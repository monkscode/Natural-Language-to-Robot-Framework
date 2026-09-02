/**
 * AppSidebar nav gating — the Learning nav item must never disagree with the
 * page it links to (App.test.tsx covers the page half; see its header for
 * why /learning moved off `admin: true`). gateAllowed (auth/pageGates.ts) is
 * NavGroup's filter predicate — the SAME function App.test.tsx exercises
 * against PAGES, here exercised against NAV_PLATFORM instead, so this is
 * testable without rendering the sidebar — this package tests no page
 * components (see FeedbackPanel.test.tsx / LearningPage.test.tsx).
 */
import { describe, expect, it } from 'vitest'
import { NAV_PLATFORM } from './app-sidebar'
import { gateAllowed } from '@/auth/pageGates'

const learningItem = NAV_PLATFORM.find(i => i.url === '/learning')!

describe('the Learning NAV_PLATFORM entry', () => {
  it('gates on viewLearning, not admin', () => {
    expect(learningItem.viewLearning).toBe(true)
    expect(learningItem.admin).toBe(false)
  })
})

describe('gateAllowed for the Learning nav item', () => {
  it('is shown for an org admin: can_view_learning true, role user', () => {
    expect(gateAllowed(learningItem, { isAdmin: false, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })

  it('is hidden when can_view_learning is false, role user', () => {
    expect(gateAllowed(learningItem, { isAdmin: false, isOrgAdmin: false, canViewLearning: false })).toBe(false)
  })

  it('is still shown for a platform admin (no regression)', () => {
    expect(gateAllowed(learningItem, { isAdmin: true, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })
})
