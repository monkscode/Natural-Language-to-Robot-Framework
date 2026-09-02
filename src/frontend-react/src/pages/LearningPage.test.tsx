/**
 * LearningPage — review-session page labels must distinguish orgs (Task 3, F2).
 *
 * hint_review_pages.org_id (added by Task 2) now flags which org an LLM
 * hint-review page belongs to. Before this test, the page label was
 * `p.scope_type === 'global' ? 'Global hints' : (p.scope_value || 'No domain')`
 * — with K orgs sharing the same scope/domain shape, every org's page
 * rendered an IDENTICAL label ("Global hints", or the same domain string
 * twice), so an admin reviewing a session with rows from two orgs could not
 * tell which page belonged to which tenant.
 *
 * reviewPageLabel is a pure function (no render, no fetch) so it is testable
 * without violating this package's "no page components" test policy
 * (vite.config.ts) — it never touches the DOM or ReviewSessionDetail's data
 * fetching.
 *
 * Only reviewPageLabel is imported. Importing it does not render
 * LearningPage's default-exported page component.
 */
import { describe, expect, it } from 'vitest'
import { defaultView, reviewPageLabel, visibleViews } from './LearningPage'

describe('reviewPageLabel', () => {
  it('two orgs sharing scope_type=global produce distinguishable labels', () => {
    const a = reviewPageLabel({ id: 1, scope_type: 'global', scope_value: null, status: 'succeeded', org_id: 'org-a' })
    const b = reviewPageLabel({ id: 2, scope_type: 'global', scope_value: null, status: 'succeeded', org_id: 'org-b' })
    expect(a).not.toBe(b)
    expect(a).toContain('org-a')
    expect(b).toContain('org-b')
  })

  it('two orgs sharing the same domain produce distinguishable labels', () => {
    const a = reviewPageLabel({ id: 3, scope_type: 'domain', scope_value: 'shop.example.com', status: 'succeeded', org_id: 'org-a' })
    const b = reviewPageLabel({ id: 4, scope_type: 'domain', scope_value: 'shop.example.com', status: 'succeeded', org_id: 'org-b' })
    expect(a).not.toBe(b)
    expect(a).toContain('shop.example.com')
    expect(b).toContain('shop.example.com')
  })

  it('a null org_id (legacy pre-partition page) still renders a usable label', () => {
    const label = reviewPageLabel({ id: 5, scope_type: 'global', scope_value: null, status: 'succeeded', org_id: null })
    expect(label).toContain('Global hints')
  })

  it('a domain page with no domain value still falls back to "No domain"', () => {
    const label = reviewPageLabel({ id: 6, scope_type: 'domain', scope_value: null, status: 'succeeded', org_id: 'org-a' })
    expect(label).toContain('No domain')
    expect(label).toContain('org-a')
  })
})

/**
 * Which tabs the page offers, and which one it opens on.
 *
 * /learning now admits an org admin (can_view_learning), but four of this
 * page's six tabs are backed by routes that are still Depends(require_admin)
 * and answer an org admin 403: Overview and Stats (GET /learning/stats),
 * Triggers (GET /learning/triggers) and LLM Review
 * (GET /learning/review-hints/sessions). Only Hints and Runs are
 * is_dashboard_viewer. The page opened on 'overview', so an org admin
 * following the new nav item landed on an error box on arrival.
 *
 * Those four routes stay platform-only on purpose —
 * test_learning_dashboards_org.py::test_stats_remains_platform_admin_only
 * asserts the 403 — so the fix is fewer controls, not a wider API. Both
 * functions are pure, so this needs no render (vite.config.ts: no page
 * components).
 */
describe('visibleViews', () => {
  const keys = (isAdmin: boolean) => visibleViews(isAdmin).map(v => v.key)

  it('a non-platform-admin is offered only the tabs whose routes admit them', () => {
    expect(keys(false)).toEqual(['hints', 'runs'])
  })

  it('none of the require_admin tabs survive for a non-platform-admin', () => {
    for (const platformOnly of ['overview', 'triggers', 'stats', 'review']) {
      expect(keys(false)).not.toContain(platformOnly)
    }
  })

  it('a platform admin still sees every tab', () => {
    expect(keys(true)).toEqual(['overview', 'hints', 'triggers', 'runs', 'stats', 'review'])
  })
})

describe('defaultView', () => {
  it('opens an org admin on a tab they can actually load, not on Overview', () => {
    expect(defaultView(false)).not.toBe('overview')
    expect(visibleViews(false).map(v => v.key)).toContain(defaultView(false))
  })

  it('still opens a platform admin on Overview (no regression)', () => {
    expect(defaultView(true)).toBe('overview')
  })
})
