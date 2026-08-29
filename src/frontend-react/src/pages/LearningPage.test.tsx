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
import { reviewPageLabel } from './LearningPage'

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
