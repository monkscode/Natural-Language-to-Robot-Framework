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
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))
vi.mock('@/lib/api', () => ({ api: vi.fn() }))

import { useAuth } from '@/auth/AuthContext'
import { useFetch } from '@/lib/useFetch'
import { api } from '@/lib/api'
import HintDrawer, { ACTION_LABELS, diffFields, isCompactChange, COMPACT_FIELD_LIMIT } from './HintDrawer'
import type { TimelineEntry } from './types'

const mockUseAuth = vi.mocked(useAuth)
const mockUseFetch = vi.mocked(useFetch)
const mockApi = vi.mocked(api)

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

/**
 * The edit form (saveEdit) and the lifecycle actions (unflag/retract/
 * reactivate, via lifecycle()) — both mutate the learning store through
 * call(), which PATCHes/POSTs then reload()s the drawer AND calls onChanged()
 * (the caller's own refetch). A wrong hint id or a dropped refetch here is
 * silent, so every write below pins the exact path/body sent and that both
 * callbacks fired. The timeline-render and status-branch describes below
 * cover the audit-history and badge code that only the "Metadata" tests
 * above exercised a slice of (a hint that is Active, with no timeline rows).
 */

describe('the audit timeline renders what diffFields computes', () => {
  function renderWithTimeline(hint: Record<string, unknown>, timeline: TimelineEntry[]) {
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'admin@test.local', display_name: 'A', role: 'admin', status: 'active' },
      isAdmin: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockReturnValue({
      data: { hint: { id: 7, feedback_text: 'wait for the spinner', is_active: 1, conflict_flagged: 0, ...hint }, timeline },
      loading: false, error: '', reload: vi.fn(),
    } as unknown as ReturnType<typeof useFetch>)
    render(<HintDrawer id={7} onChanged={() => {}} onClose={() => {}} />)
  }

  it('renders a compact entry inline: actor, action label, before→after, reason, and workflow id', () => {
    renderWithTimeline({}, [entry({
      actor: 'writer@test.local', action: 'patch', reason: 'category was wrong', workflow_id: 'wf-123',
      before_value: '{"category":"structural"}', after_value: '{"category":"B1"}',
    })])
    expect(screen.getByText('writer@test.local')).toBeInTheDocument()
    expect(screen.getByText(/edited/)).toBeInTheDocument()
    expect(screen.getByText(/category=structural → category=B1/)).toBeInTheDocument()
    expect(screen.getByText('category was wrong')).toBeInTheDocument()
    expect(screen.getByText('wf-123')).toBeInTheDocument()
  })

  it('falls back to the trigger type when a trigger_events row has no actor', () => {
    renderWithTimeline({}, [entry({ source: 'trigger_events', actor: null, trigger_type: 'trigger_1', action: 'flagged' })])
    expect(screen.getByText('trigger_1')).toBeInTheDocument()
    expect(screen.getByText(/flagged this hint/)).toBeInTheDocument()
  })

  it('renders no before→after suffix for a bare create with no diff', () => {
    renderWithTimeline({}, [entry({ action: 'create', before_value: null, after_value: null })])
    expect(screen.getByText(/created/)).toBeInTheDocument()
    expect(screen.queryByText(/→/)).toBeNull()
  })

  it('switches a large before/after payload to the expandable detail view instead of the inline string', () => {
    const before = { id: 99, feedback_text: 'x', category: 'timing', scope: 'domain', domain: 'shop.test', url: null, evidence_count: 2, applied_count: 5 }
    const after = { survivor_id: 12, evidence_count: 8, applied_count: 20, used_src: 4, failure_src: 1, unused_src: 1 }
    renderWithTimeline({}, [entry({ action: 'merge', before_value: JSON.stringify(before), after_value: JSON.stringify(after) })])
    expect(screen.getByText(/fields changed — view detail/)).toBeInTheDocument()
    // The field union is real DOM content (not just a collapsed summary) —
    // used_src exists only in after_value, so its row is real evidence the
    // union (not just the changed keys) rendered.
    expect(screen.getByText('used_src:')).toBeInTheDocument()
  })
})

describe('status badge covers every active/disabled combination', () => {
  function renderStatus(hint: Record<string, unknown>) {
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'admin@test.local', display_name: 'A', role: 'admin', status: 'active' },
      isAdmin: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockReturnValue({
      data: { hint: { id: 7, feedback_text: 'wait for the spinner', is_active: 1, conflict_flagged: 0, ...hint }, timeline: [] },
      loading: false, error: '', reload: vi.fn(),
    } as unknown as ReturnType<typeof useFetch>)
    render(<HintDrawer id={7} onChanged={() => {}} onClose={() => {}} />)
  }

  it('shows Flagged with its reason when active and conflict-flagged', () => {
    renderStatus({ is_active: 1, conflict_flagged: 1, conflict_flag_reason: 'contradicts hint 9' })
    expect(screen.getByText('Flagged')).toBeInTheDocument()
    expect(screen.getByText('contradicts hint 9')).toBeInTheDocument()
  })

  it('shows LLM-disabled, not the generic Disabled label, when llm_review_disabled is set', () => {
    renderStatus({ is_active: 0, llm_review_disabled: 1 })
    expect(screen.getByText('LLM-disabled')).toBeInTheDocument()
  })

  it('shows the generic Disabled label when inactive for any other reason', () => {
    renderStatus({ is_active: 0, llm_review_disabled: 0 })
    expect(screen.getByText('Disabled')).toBeInTheDocument()
  })

  it('renders the anchor-query section only when the hint carries one', () => {
    renderStatus({ anchor_query: 'find the checkout button' })
    expect(screen.getByText('find the checkout button')).toBeInTheDocument()
  })
})

describe('the edit form (saveEdit)', () => {
  function renderForEdit(hint: Record<string, unknown> = {}, opts: { reload?: ReturnType<typeof vi.fn>; onChanged?: ReturnType<typeof vi.fn>; id?: number } = {}) {
    const reload = opts.reload ?? vi.fn()
    const onChanged = opts.onChanged ?? vi.fn()
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'editor@test.local', display_name: 'E', role: 'admin', status: 'active' },
      isAdmin: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockReturnValue({
      data: { hint: { id: opts.id ?? 55, feedback_text: 'wait for the spinner', is_active: 1, conflict_flagged: 0, scope: 'domain', domain: 'old.test', category: 'uncategorized', ...hint }, timeline: [] },
      loading: false, error: '', reload,
    } as unknown as ReturnType<typeof useFetch>)
    render(<HintDrawer id={opts.id ?? 55} onChanged={onChanged} onClose={() => {}} />)
    return { reload, onChanged }
  }

  it('saves scope=domain with the typed domain, the RIGHT hint id, and refetches both the drawer and the caller', async () => {
    const { reload, onChanged } = renderForEdit({}, { id: 55 })
    mockApi.mockResolvedValueOnce({})

    fireEvent.change(screen.getByPlaceholderText('example.com'), { target: { value: 'new.test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    const [path, options] = mockApi.mock.calls[0]
    expect(path).toBe('/api/learning/hints/55')
    expect((options as { method: string }).method).toBe('PATCH')
    expect(JSON.parse((options as { body: string }).body)).toEqual({
      scope: 'domain', domain: 'new.test', url: null, category: 'uncategorized',
      original_failure_category: null, actor: 'editor@test.local', reason: 'edited via admin dashboard',
    })
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('switches to the URL field when scope is changed to url, and saves url instead of domain', async () => {
    renderForEdit()
    mockApi.mockResolvedValueOnce({})

    fireEvent.change(screen.getByDisplayValue('domain'), { target: { value: 'url' } })
    fireEvent.change(screen.getByPlaceholderText('https://example.com/page'), { target: { value: 'https://shop.test/checkout' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    const body = JSON.parse((mockApi.mock.calls[0][1] as { body: string }).body)
    expect(body.scope).toBe('url')
    expect(body.url).toBe('https://shop.test/checkout')
    expect(body.domain).toBeNull()
  })

  it('surfaces the error and leaves Save usable again when the PATCH fails, without refetching', async () => {
    const { reload, onChanged } = renderForEdit()
    mockApi.mockRejectedValueOnce(new Error('Request failed (409)'))

    const saveBtn = screen.getByRole('button', { name: 'Save' })
    fireEvent.click(saveBtn)

    expect(await screen.findByText('Request failed (409)')).toBeInTheDocument()
    await waitFor(() => expect(saveBtn).not.toBeDisabled())
    expect(reload).not.toHaveBeenCalled()
    expect(onChanged).not.toHaveBeenCalled()
  })
})

describe('lifecycle actions (unflag / retract / reactivate)', () => {
  function renderLifecycle(hint: Record<string, unknown>, opts: { reload?: ReturnType<typeof vi.fn>; onChanged?: ReturnType<typeof vi.fn>; id?: number } = {}) {
    const reload = opts.reload ?? vi.fn()
    const onChanged = opts.onChanged ?? vi.fn()
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'admin@test.local', display_name: 'A', role: 'admin', status: 'active' },
      isAdmin: true,
    } as unknown as ReturnType<typeof useAuth>)
    mockUseFetch.mockReturnValue({
      data: { hint: { id: opts.id ?? 7, feedback_text: 'wait for the spinner', is_active: 1, conflict_flagged: 0, ...hint }, timeline: [] },
      loading: false, error: '', reload,
    } as unknown as ReturnType<typeof useFetch>)
    render(<HintDrawer id={opts.id ?? 7} onChanged={onChanged} onClose={() => {}} />)
    return { reload, onChanged }
  }

  it('unflags a flagged hint and refetches both the drawer and the caller’s list', async () => {
    const { reload, onChanged } = renderLifecycle({ is_active: 1, conflict_flagged: 1 }, { id: 42 })
    mockApi.mockResolvedValueOnce({})

    fireEvent.click(screen.getByRole('button', { name: 'Unflag' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    const [path, options] = mockApi.mock.calls[0]
    expect(path).toBe('/api/learning/hints/42/unflag')
    expect(JSON.parse((options as { body: string }).body)).toEqual({ actor: 'admin@test.local', reason: 'via admin dashboard' })
    await waitFor(() => expect(reload).toHaveBeenCalledTimes(1))
    expect(onChanged).toHaveBeenCalledTimes(1)
  })

  it('asks for confirmation before retracting; a cancelled confirm sends nothing', () => {
    renderLifecycle({ is_active: 1, conflict_flagged: 0 }, { id: 42 })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)

    fireEvent.click(screen.getByRole('button', { name: 'Retract' }))

    expect(confirmSpy).toHaveBeenCalledWith('Retract this hint? It will stop injecting into agent prompts.')
    expect(mockApi).not.toHaveBeenCalled()
    confirmSpy.mockRestore()
  })

  it('retracts THIS drawer’s hint id once confirmed — a distinctive id, not a hardcoded default', async () => {
    mockApi.mockResolvedValueOnce({})
    renderLifecycle({ is_active: 1, conflict_flagged: 0 }, { id: 999 })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    fireEvent.click(screen.getByRole('button', { name: 'Retract' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
    expect(mockApi.mock.calls[0][0]).toBe('/api/learning/hints/999/retract')
    confirmSpy.mockRestore()
  })

  it('reactivates a disabled hint', async () => {
    mockApi.mockResolvedValueOnce({})
    renderLifecycle({ is_active: 0, llm_review_disabled: 0 }, { id: 7 })

    fireEvent.click(screen.getByRole('button', { name: 'Reactivate' }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/api/learning/hints/7/reactivate', expect.anything()))
  })

  it('shows the failure and does not refetch when a lifecycle action rejects', async () => {
    const { reload, onChanged } = renderLifecycle({ is_active: 1, conflict_flagged: 1 }, { id: 7 })
    mockApi.mockRejectedValueOnce(new Error('Request failed (409)'))

    fireEvent.click(screen.getByRole('button', { name: 'Unflag' }))

    expect(await screen.findByText('Request failed (409)')).toBeInTheDocument()
    expect(reload).not.toHaveBeenCalled()
    expect(onChanged).not.toHaveBeenCalled()
  })
})
