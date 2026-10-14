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
 * The sheet is a drawer, not a route: rendering it needs no Router, and
 * nothing here renders LearningPage either.
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

/* M4: a transient failure on ONE directory fetch used to end the sheet.
   orgErr was never cleared and nothing re-ran the call, so a 500 left a
   platform admin with an empty picker and a permanently disabled Submit until
   they closed and reopened the drawer — and the error REPLACED the sentence
   explaining what the field does, rather than sitting beside it. */
describe('when the org directory fails to load', () => {
  const EXPLANATION = /This hint reaches only this org/
  const retry = () => screen.getByRole('button', { name: /Retry/ })

  it('keeps the explanation visible beside the error, not replaced by it', async () => {
    asCaller(true)
    mockApi.mockRejectedValue(new Error('Request failed (500)'))
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)

    await screen.findByText(/Couldn.t load the org list/)
    expect(screen.getByText(EXPLANATION)).toBeInTheDocument()
  })

  it('offers a retry that refills the picker and clears the error', async () => {
    asCaller(true)
    mockApi.mockRejectedValueOnce(new Error('Request failed (500)'))
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    await screen.findByText(/Couldn.t load the org list/)

    mockApi.mockResolvedValueOnce([{ id: ORG, name: 'Org A', kind: 'team' }])
    fireEvent.click(retry())

    expect(await screen.findByText('Org A')).toBeInTheDocument()
    // The message must go with the failure it described — leaving it up
    // beside a picker that has just filled is its own small lie.
    await waitFor(() => expect(screen.queryByText(/Couldn.t load the org list/)).toBeNull())
    expect(adminOrgsCalls()).toHaveLength(2)
  })

  it('lets the admin finish the form once the retry succeeds', async () => {
    asCaller(true)
    mockApi.mockRejectedValueOnce(new Error('Request failed (500)'))
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    await screen.findByText(/Couldn.t load the org list/)
    fillTheRest()
    expect(submitButton()).toBeDisabled()   // no org to name yet

    mockApi.mockResolvedValueOnce([{ id: ORG, name: 'Org A', kind: 'team' }])
    fireEvent.click(retry())
    await screen.findByText('Org A')
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: ORG } })

    expect(submitButton()).not.toBeDisabled()
  })
})

/* M5: `valid` requires a non-empty org id, so a non-platform admin whose
   claim carries none gets a fillable form and a permanently grey Submit —
   with nothing on screen saying why. Unreachable today (is_dashboard_viewer
   requires a non-null org_id, and both flags derive from the same claim), but
   that coupling is invisible from this component and would break silently. */
describe('a caller whose token carries no org', () => {
  function asOrglessCaller() {
    mockUseAuth.mockReturnValue({
      user: { id: 'u1', email: 'writer@test.local', display_name: 'W', role: 'user', status: 'active' },
      isAdmin: false,
      canViewLearning: true,
    } as unknown as ReturnType<typeof useAuth>)
  }

  it('says what is wrong instead of greying out in silence', () => {
    asOrglessCaller()
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)

    expect(screen.getByText(/isn.t in an organisation/)).toBeInTheDocument()
    // And it does not claim an org it does not have.
    expect(screen.queryByText('Your organisation')).toBeNull()
  })

  it('still refuses to post a blank org id', () => {
    asOrglessCaller()
    render(<AddFeedbackSheet onCreated={() => {}} onClose={() => {}} />)
    fillTheRest()

    expect(submitButton()).toBeDisabled()
  })
})
