/**
 * HintDrawer — audit-timeline rendering for large before/after payloads.
 *
 * M7: ACTION_LABELS had 'reinforce' but not the two migration-21 verbs a
 * merge writes: 'merge' (the survivor absorbing a duplicate) and
 * 'merge_recommendation_dropped' (a duplicate review recommendation dropped
 * in favour of the survivor's). Both must render as real copy, not the bare
 * action string.
 *
 * Routed item A: beforeAfter() used to flatten EVERY key of before_value/
 * after_value onto one inline "k=v, k=v, ..." line. That was fine for a
 * 1-3 key row (reinforce, unflag, patch). Since Task 5, a 'merge' row's
 * before_value is `to_jsonb(row) - 'survivor_id'` — all 23 columns of
 * nl_feedback_corrections (pg_schema.py), including the full feedback_text —
 * and its after_value adds 4 more (survivor_id, used_src, failure_src,
 * unused_src) for a union of 27 fields. A 'merge_recommendation_dropped'
 * row's before_value is the full 11-column hint_review_recommendations row.
 * Rendered inline that is an unreadable wall of text, so large payloads now
 * go through diffFields()/isCompactChange() into an expandable detail view
 * instead of the compact inline string.
 *
 * The timeline half needs no DOM, so it imports the pure functions only. The
 * Metadata half at the bottom renders the drawer — it is a Sheet, not a
 * route, so no Router is involved.
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: vi.fn() }))

import { useAuth } from '@/auth/AuthContext'
import { useFetch } from '@/lib/useFetch'
import HintDrawer, { ACTION_LABELS, diffFields, isCompactChange, COMPACT_FIELD_LIMIT } from './HintDrawer'
import type { TimelineEntry } from './types'

const mockUseAuth = vi.mocked(useAuth)
const mockUseFetch = vi.mocked(useFetch)

afterEach(() => vi.resetAllMocks())

function entry(overrides: Partial<TimelineEntry> = {}): TimelineEntry {
  return {
    source: 'hint_audit', id: 1, action: 'patch', actor: 'admin@e.com',
    reason: null, created_at: '2026-08-29T00:00:00Z',
    ...overrides,
  }
}

describe('ACTION_LABELS: the two verbs Task 5 added', () => {
  it('merge has real copy, not the bare action string', () => {
    expect(ACTION_LABELS.merge).toBeTruthy()
    expect(ACTION_LABELS.merge).not.toBe('merge')
  })

  it('merge_recommendation_dropped has real copy, not the bare action string', () => {
    expect(ACTION_LABELS.merge_recommendation_dropped).toBeTruthy()
    expect(ACTION_LABELS.merge_recommendation_dropped).not.toBe('merge_recommendation_dropped')
  })
})

describe('diffFields: parity with the old before/after gate', () => {
  it('returns [] when before_value is missing (matches the old beforeAfter() gate)', () => {
    expect(diffFields(entry({ before_value: null, after_value: '{"a":1}' }))).toEqual([])
  })

  it('returns [] when after_value is missing', () => {
    expect(diffFields(entry({ before_value: '{"a":1}', after_value: null }))).toEqual([])
  })

  it('returns [] when both are missing (a bare create/reinforce/unflag with no diff)', () => {
    expect(diffFields(entry({ before_value: null, after_value: null }))).toEqual([])
  })
})

describe('diffFields: union of before/after keys', () => {
  it('a key present on both sides with equal values is unchanged', () => {
    const fields = diffFields(entry({
      before_value: '{"is_active":1}', after_value: '{"is_active":1}',
    }))
    expect(fields).toEqual([{ key: 'is_active', before: '1', after: '1', changed: false }])
  })

  it('a key present on both sides with different values is changed', () => {
    const fields = diffFields(entry({
      before_value: '{"evidence_count":3}', after_value: '{"evidence_count":4}',
    }))
    expect(fields).toEqual([
      { key: 'evidence_count', before: '3', after: '4', changed: true },
    ])
  })

  it('a key present ONLY in before_value shows as removed, not dropped', () => {
    // merge_recommendation_dropped: 'reason' lives only on the dropped
    // recommendation (before), never on the small after summary.
    const fields = diffFields(entry({
      before_value: '{"reason":"stale advice"}', after_value: '{"session_id":9}',
    }))
    const reason = fields.find(f => f.key === 'reason')
    expect(reason).toEqual({ key: 'reason', before: 'stale advice', after: '—', changed: true })
  })

  it('a key present ONLY in after_value shows as added, not dropped', () => {
    // merge: used_src/failure_src/unused_src exist only in the survivor's
    // post-merge summary (after), never on the absorbed row (before).
    const fields = diffFields(entry({
      before_value: '{"id":42}', after_value: '{"used_src":2}',
    }))
    const usedSrc = fields.find(f => f.key === 'used_src')
    expect(usedSrc).toEqual({ key: 'used_src', before: '—', after: '2', changed: true })
  })
})

describe('diffFields: no truncation of any recorded value', () => {
  it('a workflow UUID passes through byte-identical, never sliced', () => {
    const uuid = 'wf-3f9a1c2e-8b7d-4a6f-9c1e-2d5b7a8f0c11-extra-long-suffix-that-must-survive'
    const fields = diffFields(entry({
      before_value: JSON.stringify({ source_workflow_id: uuid }),
      after_value: JSON.stringify({ source_workflow_id: null }),
    }))
    const row = fields.find(f => f.key === 'source_workflow_id')
    expect(row?.before).toBe(uuid)
    expect(row?.before.length).toBe(uuid.length)
  })

  it('a long feedback_text passes through in full, never shortened with an ellipsis', () => {
    const longText = 'x'.repeat(500)
    const fields = diffFields(entry({
      before_value: JSON.stringify({ feedback_text: longText }),
      after_value: JSON.stringify({ session_id: 1 }),
    }))
    const row = fields.find(f => f.key === 'feedback_text')
    expect(row?.before).toBe(longText)
    expect(row?.before).not.toContain('…')
  })
})

describe('isCompactChange: real merge-row shapes from pg_schema.py', () => {
  it('a small patch-style row (<=6 fields) stays compact', () => {
    // patch_hint touches at most scope/url/domain/category/
    // original_failure_category — 5 fields, well under the limit.
    const fields = {
      scope: 'domain', domain: 'shop.test', category: 'locator',
    }
    const e = entry({
      action: 'patch',
      before_value: JSON.stringify(fields),
      after_value: JSON.stringify(fields),
    })
    expect(isCompactChange(e)).toBe(true)
  })

  it("a 'merge' row (23-column absorbed hint + 11-key summary) is NOT compact", () => {
    // before_value: to_jsonb(row) - 'survivor_id' — the 23 columns of
    // nl_feedback_corrections (pg_schema.py ~line 205-229).
    const before = {
      id: 99, feedback_text: 'wait for the spinner', category: 'timing',
      scope: 'domain', domain: 'shop.test', url: null,
      original_failure_category: 'B1', evidence_count: 2, applied_count: 5,
      success_count: 4, failure_count: 1, is_active: 1,
      source_workflow_id: 'wf-abc', created_at: '2026-01-01T00:00:00Z',
      last_seen: '2026-01-02T00:00:00Z', conflict_flagged: 0,
      conflict_flagged_at: null, conflict_flag_reason: null,
      created_via: 'workflow', disabled_at: null, anchor_query: 'q',
      unused_count: 0, org_id: 'org-a',
    }
    // after_value: jsonb_build_object(survivor_id, evidence_count,
    // applied_count, success_count, failure_count, unused_count, is_active,
    // conflict_flagged, used_src, failure_src, unused_src) — 11 keys.
    const after = {
      survivor_id: 12, evidence_count: 8, applied_count: 20, success_count: 15,
      failure_count: 3, unused_count: 2, is_active: 1, conflict_flagged: 0,
      used_src: 4, failure_src: 1, unused_src: 1,
    }
    const e = entry({
      action: 'merge',
      before_value: JSON.stringify(before), after_value: JSON.stringify(after),
    })
    const fields = diffFields(e)
    expect(fields.length).toBeGreaterThan(COMPACT_FIELD_LIMIT)
    expect(isCompactChange(e)).toBe(false)
  })

  it("a 'merge_recommendation_dropped' row (11-column recommendation) is NOT compact", () => {
    // before_value: to_jsonb(d) — the 11 columns of
    // hint_review_recommendations (pg_schema.py ~line 348-360).
    const before = {
      id: 7, session_id: 3, hint_id: 55, recommendation: 'disable',
      reason: 'never succeeded', exoneration_count: 0, admin_decision: null,
      admin_notes: null, decided_at: null, applied: 0,
      created_at: '2026-01-01T00:00:00Z',
    }
    const after = { session_id: 3, survivor_id: 12, surviving_recommendation_id: 4 }
    const e = entry({
      action: 'merge_recommendation_dropped',
      before_value: JSON.stringify(before), after_value: JSON.stringify(after),
    })
    expect(isCompactChange(e)).toBe(false)
  })
})

/**
 * I3, the detail half: the drawer an admin opens to decide which tenant's
 * hint they are about to change had no org and no author in its Metadata
 * block either — change 4 of this branch captured authorship and the SPA
 * rendered it nowhere.
 *
 * Rendering the drawer needs no Router (it is a Sheet, not a route) and no
 * second directory fetch: the full org_id is appropriate detail-view content,
 * and /auth/admin/orgs would be a request this view otherwise never makes.
 */
describe('Metadata names the org and the author', () => {
  const ORG = 'aaaaaaaa-0000-0000-0000-000000000001'

  function renderDrawer(hint: Record<string, unknown>) {
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'admin@test.local', display_name: 'A', role: 'admin', status: 'active' },
      isAdmin: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockReturnValue({
      data: { hint: { id: 7, feedback_text: 'wait for the spinner', is_active: 1, conflict_flagged: 0, ...hint }, timeline: [] },
      loading: false,
      error: '',
      reload: vi.fn(),
    } as unknown as ReturnType<typeof useFetch>)
    render(<HintDrawer id={7} onChanged={() => {}} onClose={() => {}} />)
  }

  /** The <dd> that follows a given <dt> label in the Metadata list. */
  const valueFor = (label: string) =>
    screen.getByText(label).nextElementSibling?.textContent

  it('shows the owning org as its whole id, not a prefix of it', () => {
    renderDrawer({ org_id: ORG })

    expect(valueFor('Org')).toBe(ORG)
  })

  it('shows the author’s email when the hint carries one', () => {
    renderDrawer({ org_id: ORG, created_by_email: 'writer@tenant-one.test' })

    expect(valueFor('Author')).toBe('writer@tenant-one.test')
  })

  it('says "unknown" for a hint written before authorship was captured', () => {
    // Pre-v23 rows carry NULL created_by_* and cannot be attributed after the
    // fact. "unknown" is the true answer — not blank, and certainly not the
    // string "undefined".
    renderDrawer({ org_id: ORG, created_by_email: null })

    expect(valueFor('Author')).toBe('unknown')
  })

  it('renders a dash rather than nothing when the org is missing too', () => {
    renderDrawer({ org_id: null, created_by_email: null })

    expect(valueFor('Org')).toBe('—')
    expect(valueFor('Author')).toBe('unknown')
  })
})
