/**
 * App — PAGES gating for /learning (Task T6).
 *
 * The API gates the Learning routes on is_dashboard_viewer (org admin or
 * platform admin); the SPA used to gate /learning on the platform `admin`
 * role only, stranding an org admin behind a page the server would happily
 * serve. gateAllowed (auth/pageGates.ts) is the pure predicate
 * KeepAlivePages uses to decide which page mounts (and to redirect to
 * /generate otherwise) — shared with app-sidebar.test.tsx, which exercises
 * the same predicate against NAV_PLATFORM, so this is testable without
 * rendering App. Most of this package still tests predicates and
 * sub-components rather than pages (see FeedbackPanel.test.tsx);
 * LearningPage.test.tsx is the deliberate exception, where mounting the page
 * is the point (vite.config.ts).
 */
import { describe, expect, it } from 'vitest'
import { PAGES } from './App'
import { gateAllowed } from '@/auth/pageGates'

const learningPage = PAGES.find(p => p.path === '/learning')!
const metricsPage = PAGES.find(p => p.path === '/metrics')!

describe('the /learning PAGES entry', () => {
  it('gates on viewLearning, not admin', () => {
    expect(learningPage.viewLearning).toBe(true)
    expect(learningPage.admin).toBeFalsy()
  })
})

describe('gateAllowed for /learning', () => {
  it('renders for an org admin: can_view_learning true, role user', () => {
    expect(gateAllowed(learningPage, { isAdmin: false, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })

  it('does not render when can_view_learning is false, role user', () => {
    expect(gateAllowed(learningPage, { isAdmin: false, isOrgAdmin: false, canViewLearning: false })).toBe(false)
  })

  it('still renders for a platform admin (no regression)', () => {
    // A platform admin's can_view_learning is always true from the server
    // (is_dashboard_viewer short-circuits on is_platform_admin) — the SPA
    // trusts that one flag rather than re-deriving admin-ness itself.
    expect(gateAllowed(learningPage, { isAdmin: true, isOrgAdmin: false, canViewLearning: true })).toBe(true)
  })
})

describe('gateAllowed leaves other pages unchanged', () => {
  it('/metrics is deliberately out of scope: still gates on isAdmin alone', () => {
    expect(gateAllowed(metricsPage, { isAdmin: false, isOrgAdmin: false, canViewLearning: true })).toBe(false)
    expect(gateAllowed(metricsPage, { isAdmin: true, isOrgAdmin: false, canViewLearning: false })).toBe(true)
  })
})
