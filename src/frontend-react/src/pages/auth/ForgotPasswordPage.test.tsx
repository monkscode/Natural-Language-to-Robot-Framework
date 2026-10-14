/**
 * ForgotPasswordPage — the reset-link request.
 *
 * The load-bearing property here is a PRIVACY one, not a UX one: the page must
 * behave identically whether or not the email exists. The component gets that
 * by swallowing the request's rejection and showing the confirmation either
 * way. A well-meaning "show the error" change would turn this page into an
 * account-enumeration oracle, so the swallow is pinned deliberately — twice,
 * once for send and once for resend.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', () => ({ api: vi.fn() }))

import { api } from '@/lib/api'
import ForgotPasswordPage from './ForgotPasswordPage'

const mockApi = vi.mocked(api)
afterEach(() => { vi.resetAllMocks(); vi.useRealTimers() })

function setup() {
  render(<MemoryRouter><ForgotPasswordPage /></MemoryRouter>)
}
const emailBox = () => screen.getByPlaceholderText('you@company.com')
const submit = () => screen.getByRole('button', { name: /send reset link/i })

describe('ForgotPasswordPage — requesting the link', () => {
  it('disables the button until an email is typed', () => {
    setup()
    expect(submit()).toBeDisabled()

    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })

    expect(submit()).toBeEnabled()
  })

  it('posts to the stub endpoint without auth, and shows the confirmation', async () => {
    // auth:false matters — attaching a bearer token to a
    // forgot-password call would be pointless at best, and on a 401 the api
    // wrapper would sign the visitor out of a session they do not have.
    mockApi.mockResolvedValue(undefined)
    setup()
    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })

    fireEvent.click(submit())

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/forgot-password', {
      method: 'POST', auth: false, body: JSON.stringify({ email: 'a@b.com' }),
    }))
    expect(await screen.findByText('Check your email')).toBeInTheDocument()
  })

  it('names the address it sent to, so a typo is visible', async () => {
    mockApi.mockResolvedValue(undefined)
    setup()
    fireEvent.change(emailBox(), { target: { value: 'typo@exampel.com' } })

    fireEvent.click(submit())

    expect(await screen.findByText('typo@exampel.com')).toBeInTheDocument()
  })

  it('shows the SAME confirmation when the request is rejected', async () => {
    // The privacy property. An unknown address must be indistinguishable from
    // a known one; surfacing this error would leak which emails have accounts.
    mockApi.mockRejectedValue(new Error('404 no such user'))
    setup()
    fireEvent.change(emailBox(), { target: { value: 'nobody@nowhere.com' } })

    fireEvent.click(submit())

    expect(await screen.findByText('Check your email')).toBeInTheDocument()
    expect(screen.queryByText(/no such user/i)).toBeNull()
  })

  it('leaves the form alone when submitted empty', () => {
    setup()

    fireEvent.submit(emailBox().closest('form')!)

    expect(mockApi).not.toHaveBeenCalled()
    expect(screen.queryByText('Check your email')).toBeNull()
  })
})

describe('ForgotPasswordPage — resend', () => {
  async function reachConfirmation() {
    mockApi.mockResolvedValue(undefined)
    setup()
    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })
    fireEvent.click(submit())
    await screen.findByText('Check your email')
    mockApi.mockClear()
  }

  it('re-posts to the same endpoint with the same address', async () => {
    await reachConfirmation()

    fireEvent.click(screen.getByText('Resend email'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/forgot-password', {
      method: 'POST', auth: false, body: JSON.stringify({ email: 'a@b.com' }),
    }))
  })

  it('acknowledges the resend, then returns the button to its normal label', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    await reachConfirmation()

    fireEvent.click(screen.getByText('Resend email'))
    expect(await screen.findByText('✓ Sent again!')).toBeInTheDocument()

    await vi.advanceTimersByTimeAsync(3100)

    expect(await screen.findByText('Resend email')).toBeInTheDocument()
  })

  it('acknowledges the resend even when THAT request fails', async () => {
    // Same privacy rule as the first send.
    await reachConfirmation()
    mockApi.mockRejectedValue(new Error('boom'))

    fireEvent.click(screen.getByText('Resend email'))

    expect(await screen.findByText('✓ Sent again!')).toBeInTheDocument()
  })

  it('offers a way back to sign in from the confirmation', async () => {
    await reachConfirmation()

    expect(screen.getByRole('link', { name: /back to sign in/i })).toHaveAttribute('href', '/login')
  })
})
