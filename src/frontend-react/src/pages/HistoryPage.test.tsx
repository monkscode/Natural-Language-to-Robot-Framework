/**
 * HistoryPage — the drawer's corrections fetch (Task 2 of
 * feedback-visibility). One thin wiring test, not page coverage: this file
 * had no test before this task. The fixture is the minimum needed to render
 * the page — MemoryRouter (HistoryPage calls useNavigate for Regenerate),
 * plus useAuth/useRunGroups/useFetch/api mocked so nothing here makes a real
 * network call — mirroring the mock shape LearningPage.test.tsx and
 * HintDrawer.test.tsx already use for the same three hooks.
 *
 * What this pins: opening a run in the drawer fires
 * GET /api/feedback/{run_id}, and a refused (or still-pending) read renders
 * no error text — the two behaviours HistoryPage.tsx's corrections block
 * promises and GeneratePage's FeedbackPanel already proved once for its own
 * surface. It does not re-test RecordedCorrections' own rendering rules
 * (RecordedCorrections.test.tsx already does that) or the rest of the page.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))
vi.mock('@/components/history/RunGroupsContext', () => ({ useRunGroups: vi.fn() }))
vi.mock('@/lib/useFetch', () => ({ useFetch: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))

import { useAuth } from '@/auth/AuthContext'
import { useRunGroups } from '@/components/history/RunGroupsContext'
import { useFetch } from '@/lib/useFetch'
import { api } from '@/lib/api'
import HistoryPage from './HistoryPage'

const mockUseAuth = vi.mocked(useAuth)
const mockUseRunGroups = vi.mocked(useRunGroups)
const mockUseFetch = vi.mocked(useFetch)
const mockApi = vi.mocked(api)

afterEach(() => vi.resetAllMocks())

const RUN = {
  run_id: 'run-1', status: 'passed' as const, user_query: 'search flipkart for shoes',
  created_at: '2026-09-01T00:00:00Z', updated_at: '2026-09-01T00:00:00Z',
  has_report: false, can_move: false,
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
function setup(feedbackFetch: { data: any; error: string; loading?: boolean }) {
  mockUseAuth.mockReturnValue({
    user: { id: 'u1', email: 'a@b.com', display_name: 'A', role: 'user', status: 'active' },
    loading: false, isAuthenticated: true, isAdmin: false, status: 'active',
    isOrgAdmin: false, canViewLearning: false,
    login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  })
  mockUseRunGroups.mockReturnValue({
    groups: [], ungroupedCount: 0, error: '', loaded: true,
    refresh: vi.fn(), createGroup: vi.fn(), renameGroup: vi.fn(),
    deleteGroup: vi.fn(), assignRuns: vi.fn(),
    groupFilter: null, setGroupFilter: vi.fn(),
  })
  mockApi.mockResolvedValue({ runs: [RUN], total: 1, scope: 'own' })
  // Keyed by path: the drawer fires useFetch twice (run detail + this
  // task's corrections fetch), and only the second is this test's subject.
  mockUseFetch.mockImplementation((path: string | null) => {
    if (path?.startsWith('/api/feedback/')) {
      return { data: feedbackFetch.data, loading: feedbackFetch.loading ?? false, error: feedbackFetch.error, reload: vi.fn() }
    }
    return { data: null, loading: false, error: '', reload: vi.fn() }
  })
}

async function openDrawer() {
  render(<MemoryRouter><HistoryPage /></MemoryRouter>)
  fireEvent.click(await screen.findByText('search flipkart for shoes'))
  await waitFor(() => expect(mockUseFetch).toHaveBeenCalledWith('/api/feedback/run-1'))
}

describe('HistoryPage drawer — the corrections fetch', () => {
  it('fires GET /api/feedback/{run_id} when a row is opened', async () => {
    setup({ data: null, error: '' })

    await openDrawer()

    // openDrawer's own waitFor is the assertion; this is just its
    // documented restatement.
    expect(mockUseFetch).toHaveBeenCalledWith('/api/feedback/run-1')
  })

  it('renders no error text when the corrections read is refused (403 — a published run whose corrections stay private)', async () => {
    setup({ data: null, error: 'You cannot read feedback for this run' })

    await openDrawer()

    // The page has an unrelated "Error" status filter button, so this checks
    // the specific refusal text rather than a bare /error/i (which would
    // false-positive on that button).
    expect(screen.queryByText(/cannot read feedback/i)).toBeNull()
  })

  it('does not render a previous row’s corrections while this row’s fetch is still in flight', async () => {
    // The corrections payload carries no run_id of its own (only
    // `applied_to`, the resolved ORIGINAL run — see HistoryPage.tsx), so a
    // just-left row's stale `data` can only be caught via `loading`, not by
    // comparing an echoed id the way the run-detail fetch does. loading:
    // true is useFetch's real state for exactly this window: the effect for
    // the newly-selected row's path has fired, but that fetch has not
    // resolved yet.
    setup({
      data: { applied_to: 'run-1', corrections: [{ hint_id: 1, feedback_text: 'stale from the last row' }] },
      error: '', loading: true,
    })

    await openDrawer()

    expect(screen.queryByText(/stale from the last row/)).toBeNull()
  })
})
