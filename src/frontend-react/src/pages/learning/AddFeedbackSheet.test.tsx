/**
 * AddFeedbackSheet — an org admin must be able to finish the form.
 *
 * The backend was widened so an org admin may create a hint in their OWN org
 * (learning_endpoints.create_hint), and a test asserts the 201. No client
 * could produce that request: the sheet filled its org picker from
 * GET /auth/admin/orgs, which is Depends(require_admin). An org admin got 403,
 * `orgs` stayed [], `orgId` stayed '', `valid` stayed false, and the submit
 * button never enabled — so the only visible effect of the widening was a red
 * "403 Forbidden" printed under the org field.
 *
 * The fix draws fewer controls rather than opening that route: no fetch, no
 * picker, org pinned to the caller's own org_id claim (the only value the
 * server would accept from them anyway). A platform admin keeps the picker.
 *
 * The sheet is a drawer, not a route, so rendering it does not break this
 * package's "no page components" policy (vite.config.ts) — nothing here
 * renders LearningPage, and no Router is involved.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', () => ({ api: vi.fn() }))
vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))

import { api } from '@/lib/api'
import { useAuth } from '@/auth/AuthContext'
import AddFeedbackSheet from './AddFeedbackSheet'

const mockApi = vi.mocked(api)
const mockUseAuth = vi.mocked(useAuth)

const ORG = '3f2b1c00-0000-0000-0000-00000000000a'

/** Only the three fields the component reads; the rest of AuthState is not
 *  touched by this sheet, so a cast keeps the fixture honest about that. */
function asCaller(isAdmin: boolean) {
  mockUseAuth.mockReturnValue({
    user: { id: 'u1', email: 'writer@test.local', display_name: 'W', role: isAdmin ? 'admin' : 'user', status: 'active', org_id: ORG },
    isAdmin,
    canViewLearning: true,
  } as unknown as ReturnType<typeof useAuth>)
}

const adminOrgsCalls = () =>
  mockApi.mock.calls.filter(([path]) => path === '/auth/admin/orgs')

const hintPosts = () =>
  mockApi.mock.calls.filter(
    ([path, o]) => path === '/api/learning/hints' &&
      (o as { method?: string } | undefined)?.method === 'POST')

/** Fill every required field so `valid` can only be false because of the org. */
function fillTheRest() {
  fireEvent.change(screen.getByPlaceholderText(/Describe the rule or correction/), {
    target: { value: 'wait for the grid before reading a row' },
  })
  fireEvent.change(screen.getByPlaceholderText(/verify the product list loads/), {
    target: { value: 'check the first result after filtering' },
  })
  fireEvent.change(screen.getByPlaceholderText('example.com'), {
    target: { value: 'shop.example.com' },
  })
}

const submitButton = () => screen.getByRole('button', { name: /Add feedback/ })

afterEach(() => vi.resetAllMocks())

describe('an org admin (not a platform admin)', () => {
  it('never calls the admin-only orgs route', async () => {
    asCaller(false)
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    await waitFor(() => expect(screen.getByText('Your organisation')).toBeInTheDocument())
    expect(adminOrgsCalls()).toHaveLength(0)
  })

  it('draws no org picker — there is nothing for them to choose', () => {
    asCaller(false)
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    expect(screen.queryByText('— choose an org —')).toBeNull()
    expect(screen.getByText('Your organisation')).toBeInTheDocument()
    // The two unrelated selects (category, original failure category) are
    // untouched — this removed one control, not the form's selects.
    expect(screen.getAllByRole('combobox')).toHaveLength(2)
  })

  it('can reach submit, and sends its own org', async () => {
    asCaller(false)
    mockApi.mockResolvedValue({})
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    fillTheRest()
    expect(submitButton()).not.toBeDisabled()

    fireEvent.click(submitButton())
    await waitFor(() => expect(hintPosts()).toHaveLength(1))
    const body = JSON.parse((hintPosts()[0][1] as { body: string }).body)
    expect(body.org_id).toBe(ORG)
  })

  it('stays blocked when the token carries no org, rather than posting a blank one', () => {
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'writer@test.local', display_name: 'W', role: 'user', status: 'active' },
      isAdmin: false,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    fillTheRest()
    // org_id is required by create_hint (400 without it); an empty string must
    // fail the client's own `valid` check rather than travel.
    expect(submitButton()).toBeDisabled()
  })
})

describe('a platform admin', () => {
  it('still gets the picker, filled from the admin orgs route', async () => {
    asCaller(true)
    mockApi.mockResolvedValue([
      { id: ORG, name: 'Org A', kind: 'team' },
      { id: 'other', name: 'Org B', kind: 'team' },
    ])
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    await waitFor(() => expect(adminOrgsCalls()).toHaveLength(1))
    expect(await screen.findByText('Org A')).toBeInTheDocument()
    expect(screen.getByText('— choose an org —')).toBeInTheDocument()
  })

  it('is not pre-pinned to their own org — they must choose one', () => {
    asCaller(true)
    mockApi.mockResolvedValue([])
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    fillTheRest()
    expect(submitButton()).toBeDisabled()
  })
})
