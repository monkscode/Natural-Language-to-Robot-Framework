/**
 * LoginPage — the form, its client-side validation, and the OAuth error codes
 * the backend redirects back with.
 *
 * Two things here are easy to get wrong and invisible to a type check. First,
 * `loading` is set true before login() and reset ONLY in the catch: on success
 * the page navigates away, so a reset there would flash an enabled button
 * mid-navigation — but it also means a failure that forgets to reset leaves
 * the form permanently disabled. Second, OAUTH_ERRORS maps codes the backend
 * chooses; an unknown code must still say something, never render blank.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/auth/AuthContext', () => ({ useAuth: vi.fn() }))
const mockNavigate = vi.fn()
vi.mock('react-router-dom', async importOriginal => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => mockNavigate,
}))

import { useAuth } from '@/auth/AuthContext'
import LoginPage from './LoginPage'

const mockUseAuth = vi.mocked(useAuth)
afterEach(() => vi.resetAllMocks())

function setup(login = vi.fn().mockResolvedValue(undefined), search = '') {
  mockUseAuth.mockReturnValue({
    user: null, loading: false, isAuthenticated: false, isAdmin: false, status: null,
    isOrgAdmin: false, canViewLearning: false,
    login, signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
  render(<MemoryRouter initialEntries={[`/login${search}`]}><LoginPage /></MemoryRouter>)
  return login
}

const emailBox = () => screen.getByPlaceholderText('you@company.com')
const pwBox = () => screen.getByPlaceholderText('••••••••')
const submit = () => screen.getByRole('button', { name: /sign in$/i })

describe('LoginPage — the form', () => {
  it('renders the email and password fields and a submit button', () => {
    setup()

    expect(emailBox()).toBeInTheDocument()
    expect(pwBox()).toBeInTheDocument()
    expect(submit()).toBeEnabled()
  })

  it('signs in with what was typed, then navigates to /generate replacing history', () => {
    // `replace: true` matters: without it Back returns to the login page of a
    // session that is now signed in.
    const login = setup()

    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })
    fireEvent.change(pwBox(), { target: { value: 'hunter2' } })
    fireEvent.click(submit())

    return waitFor(() => {
      expect(login).toHaveBeenCalledWith('a@b.com', 'hunter2')
      expect(mockNavigate).toHaveBeenCalledWith('/generate', { replace: true })
    })
  })

  // Both inputs carry the HTML `required` attribute (LoginPage.tsx:117,141),
  // so the browser's own constraint validation refuses the submit and
  // handleSubmit never runs. That makes the component's `if (!email)` /
  // `if (!password)` guards unreachable through the UI in any browser that
  // implements constraint validation - i.e. all of them. Left in place
  // (pre-existing, and harmless as a belt-and-braces guard); these tests pin
  // the behaviour that IS reachable, which is that nothing is submitted.
  it('does not call login when the email is empty - the browser blocks the submit', () => {
    const login = setup()

    fireEvent.change(pwBox(), { target: { value: 'hunter2' } })
    fireEvent.click(submit())

    expect(login).not.toHaveBeenCalled()
    expect(mockNavigate).not.toHaveBeenCalled()
    expect(emailBox()).toBeRequired()
  })

  it('does not call login when the password is empty - the browser blocks the submit', () => {
    const login = setup()

    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })
    fireEvent.click(submit())

    expect(login).not.toHaveBeenCalled()
    expect(mockNavigate).not.toHaveBeenCalled()
    expect(pwBox()).toBeRequired()
  })

  it('shows the server’s own message when sign-in is rejected', async () => {
    const login = setup(vi.fn().mockRejectedValue(new Error('Invalid email or password')))

    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })
    fireEvent.change(pwBox(), { target: { value: 'wrong' } })
    fireEvent.click(submit())

    expect(await screen.findByText('Invalid email or password')).toBeInTheDocument()
    expect(login).toHaveBeenCalled()
  })

  it('re-enables the button after a failure so the user can retry', async () => {
    // The form is disabled during the attempt. If the catch forgot to reset
    // `loading`, a mistyped password would lock the page until a reload.
    setup(vi.fn().mockRejectedValue(new Error('nope')))

    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })
    fireEvent.change(pwBox(), { target: { value: 'wrong' } })
    fireEvent.click(submit())

    await screen.findByText('nope')
    expect(submit()).toBeEnabled()
  })

  it('falls back to a generic message when the rejection is not an Error', async () => {
    setup(vi.fn().mockRejectedValue('a bare string'))

    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })
    fireEvent.change(pwBox(), { target: { value: 'x' } })
    fireEvent.click(submit())

    expect(await screen.findByText('Sign in failed')).toBeInTheDocument()
  })

  it('clears the previous error when the form is resubmitted', async () => {
    // setError('') runs first in handleSubmit. Without it a retry that is
    // still in flight shows the OLD failure, which reads as a second failure.
    const login = vi.fn()
      .mockRejectedValueOnce(new Error('first failure'))
      .mockResolvedValueOnce(undefined)
    setup(login)
    fireEvent.change(emailBox(), { target: { value: 'a@b.com' } })
    fireEvent.change(pwBox(), { target: { value: 'x' } })
    fireEvent.click(submit())
    expect(await screen.findByText('first failure')).toBeInTheDocument()

    fireEvent.click(submit())

    await waitFor(() => expect(screen.queryByText('first failure')).toBeNull())
    expect(login).toHaveBeenCalledTimes(2)
  })
})

describe('LoginPage — password visibility', () => {
  it('starts masked', () => {
    setup()
    expect(pwBox()).toHaveAttribute('type', 'password')
  })

  it('reveals and re-masks, and its label names the NEXT action each time', () => {
    setup()

    fireEvent.click(screen.getByLabelText('Show password'))
    expect(pwBox()).toHaveAttribute('type', 'text')

    fireEvent.click(screen.getByLabelText('Hide password'))
    expect(pwBox()).toHaveAttribute('type', 'password')
  })
})

describe('LoginPage — OAuth error codes from the backend redirect', () => {
  it.each([
    ['email_exists', /already exists/i],
    ['account_disabled', /has been disabled/i],
    ['email_unverified', /not verified/i],
    ['invalid_state', /expired/i],
    ['google_failed', /failed/i],
  ])('renders a specific message for ?error=%s', (code, expected) => {
    setup(vi.fn(), `?error=${code}`)

    expect(screen.getByText(expected)).toBeInTheDocument()
  })

  it('falls back to the generic Google message for an unrecognised code', () => {
    // The backend owns these codes. A new one must never render an empty
    // error area that silently swallows a failed sign-in.
    setup(vi.fn(), '?error=some_new_code_we_do_not_know')

    expect(screen.getByText(/Google sign-in failed/i)).toBeInTheDocument()
  })

  it('shows no error at all on a clean load', () => {
    setup()

    expect(screen.queryByText(/Google sign-in failed/i)).toBeNull()
  })
})

describe('LoginPage — the other ways out', () => {
  it('sends the Google button to the backend’s own OAuth entry point', () => {
    setup()
    const original = window.location
    // jsdom forbids assigning window.location.href; swap in a plain object.
    Object.defineProperty(window, 'location', { writable: true, value: { href: '' } })

    fireEvent.click(screen.getByRole('button', { name: /google/i }))

    expect((window.location as unknown as { href: string }).href).toBe('/auth/google/login')
    Object.defineProperty(window, 'location', { writable: true, value: original })
  })

  it('links to signup and to password recovery', () => {
    setup()

    expect(screen.getByRole('link', { name: /sign up/i })).toHaveAttribute('href', '/signup')
    expect(screen.getByRole('link', { name: /forgot/i })).toHaveAttribute('href', '/forgot-password')
  })
})
