/**
 * SignupPage — client-side validation and the password strength meter.
 *
 * Unlike LoginPage, these inputs carry no HTML `required`, so validate() is
 * genuinely the gate: every rule in it is reachable and each one is the only
 * thing standing between a malformed signup and a request the server has to
 * reject. The meter is tested through its label rather than its internals,
 * because the label is what a user acts on.
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
import SignupPage from './SignupPage'

const mockUseAuth = vi.mocked(useAuth)
afterEach(() => vi.resetAllMocks())

function setup(signup = vi.fn().mockResolvedValue(undefined)) {
  mockUseAuth.mockReturnValue({
    user: null, loading: false, isAuthenticated: false, isAdmin: false, status: null,
    isOrgAdmin: false, canViewLearning: false,
    login: vi.fn(), signup, loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
  render(<MemoryRouter><SignupPage /></MemoryRouter>)
  return signup
}

const first = () => screen.getByPlaceholderText('Alex')
const last = () => screen.getByPlaceholderText('Johnson')
const email = () => screen.getByPlaceholderText('you@company.com')
const pw = () => screen.getByPlaceholderText('Min. 8 characters')
const confirm = () => screen.getByPlaceholderText('Re-enter password')
const terms = () => screen.getByRole('checkbox')
const submit = () => screen.getByRole('button', { name: /create account/i })

/** Fill a valid form, then let each test break exactly one thing. */
function fillValid() {
  fireEvent.change(first(), { target: { value: 'Alex' } })
  fireEvent.change(last(), { target: { value: 'Johnson' } })
  fireEvent.change(email(), { target: { value: 'alex@company.com' } })
  fireEvent.change(pw(), { target: { value: 'Password1' } })
  fireEvent.change(confirm(), { target: { value: 'Password1' } })
  fireEvent.click(terms())
}

describe('SignupPage — the happy path', () => {
  it('signs up with the joined display name and navigates away', async () => {
    const signup = setup()
    fillValid()

    fireEvent.click(submit())

    await waitFor(() => {
      expect(signup).toHaveBeenCalledWith('alex@company.com', 'Password1', 'Alex Johnson')
      expect(mockNavigate).toHaveBeenCalledWith('/generate', { replace: true })
    })
  })

  it('trims the display name when the last name is left blank', async () => {
    // `${first} ${last}`.trim() — without the trim the account is created with
    // a trailing space in its display name, which then shows up everywhere.
    const signup = setup()
    fillValid()
    fireEvent.change(last(), { target: { value: '' } })

    fireEvent.click(submit())

    await waitFor(() => expect(signup).toHaveBeenCalledWith('alex@company.com', 'Password1', 'Alex'))
  })
})

describe('SignupPage — validate() blocks each bad field', () => {
  it('requires a first name, and does not treat spaces as one', async () => {
    const signup = setup()
    fillValid()
    fireEvent.change(first(), { target: { value: '   ' } })

    fireEvent.click(submit())

    expect(await screen.findByText('Required')).toBeInTheDocument()
    expect(signup).not.toHaveBeenCalled()
  })

  it('blocks a malformed email — the browser refuses the submit before validate() runs', () => {
    // The field is type="email" with no `required` and the form has no
    // noValidate, so a NON-EMPTY malformed value fails the browser's own
    // constraint validation and handleSubmit never runs. That is why the
    // component's own `!email.includes('@')` rule reaches the user only when
    // the field is EMPTY, and why its message names that case and not this one.
    const signup = setup()
    fillValid()
    fireEvent.change(email(), { target: { value: 'not-an-email' } })

    fireEvent.click(submit())

    expect(signup).not.toHaveBeenCalled()
    expect(screen.queryByText('Enter your work email')).toBeNull()
  })

  it('asks for the email, in the words of the only case that reaches the user — an empty field', async () => {
    const signup = setup()
    fillValid()
    fireEvent.change(email(), { target: { value: '' } })

    fireEvent.click(submit())

    expect(await screen.findByText('Enter your work email')).toBeInTheDocument()
    // The old wording, "Enter a valid email", described the malformed-input
    // case the browser had already taken over - it accused a visitor who had
    // typed nothing of typing something wrong.
    expect(screen.queryByText('Enter a valid email')).toBeNull()
    expect(signup).not.toHaveBeenCalled()
  })

  it('rejects a password under 8 characters', async () => {
    const signup = setup()
    fillValid()
    fireEvent.change(pw(), { target: { value: 'Pass1' } })
    fireEvent.change(confirm(), { target: { value: 'Pass1' } })

    fireEvent.click(submit())

    expect(await screen.findByText('Minimum 8 characters')).toBeInTheDocument()
    expect(signup).not.toHaveBeenCalled()
  })

  it('rejects a mismatched confirmation', async () => {
    const signup = setup()
    fillValid()
    fireEvent.change(confirm(), { target: { value: 'Password2' } })

    fireEvent.click(submit())

    expect(await screen.findByText('Passwords do not match')).toBeInTheDocument()
    expect(signup).not.toHaveBeenCalled()
  })

  it('requires the terms checkbox', async () => {
    const signup = setup()
    fillValid()
    fireEvent.click(terms())   // untick it again

    fireEvent.click(submit())

    expect(await screen.findByText('Please accept the terms')).toBeInTheDocument()
    expect(signup).not.toHaveBeenCalled()
  })

  it('reports every bad field at once, not just the first', async () => {
    // validate() accumulates into one object. Reporting one at a time turns a
    // single fix-up into five round trips.
    const signup = setup()

    fireEvent.click(submit())

    expect(await screen.findByText('Required')).toBeInTheDocument()
    expect(screen.getByText('Enter your work email')).toBeInTheDocument()
    expect(screen.getByText('Minimum 8 characters')).toBeInTheDocument()
    expect(screen.getByText('Please accept the terms')).toBeInTheDocument()
    expect(signup).not.toHaveBeenCalled()
  })
})

describe('SignupPage — the password strength meter', () => {
  it('shows nothing until something is typed', () => {
    setup()

    // The whole meter is behind `password.length > 0`, so index 0 of
    // STRENGTH_LABEL is unreachable while the field is empty - which is what
    // makes it free to carry a real word for the case below.
    expect(screen.queryByText('Very weak')).toBeNull()
    expect(screen.queryByText('Weak')).toBeNull()
    expect(screen.queryByText('Fair')).toBeNull()
    expect(screen.queryByText('Strong')).toBeNull()
  })

  it('calls a password that scores zero Very weak, rather than saying nothing', () => {
    // 'abc' earns nothing: under 8 characters, not mixed case, no digit. Its
    // score indexes STRENGTH_LABEL[0], which used to be the empty string - so
    // the weakest password on the scale was the one the meter refused to
    // describe, and the bars stayed grey exactly as they do for a field
    // nobody has touched.
    setup()
    fireEvent.change(pw(), { target: { value: 'abc' } })

    expect(screen.getByText('Very weak')).toBeInTheDocument()
    expect(screen.queryByText('Fair')).toBeNull()
    expect(screen.queryByText('Strong')).toBeNull()
  })

  it('calls eight lowercase characters Weak — length alone is not enough', () => {
    setup()
    fireEvent.change(pw(), { target: { value: 'abcdefgh' } })

    expect(screen.getByText('Weak')).toBeInTheDocument()
  })

  it('calls mixed case at length Fair', () => {
    setup()
    fireEvent.change(pw(), { target: { value: 'abcdefgH' } })

    expect(screen.getByText('Fair')).toBeInTheDocument()
  })

  it('calls mixed case with a digit Strong', () => {
    setup()
    fireEvent.change(pw(), { target: { value: 'abcdefG1' } })

    expect(screen.getByText('Strong')).toBeInTheDocument()
  })

  it('does not exceed Strong for a very complex password', () => {
    // The score is clamped to 3; an unclamped one would index past
    // STRENGTH_LABEL and render undefined.
    setup()
    fireEvent.change(pw(), { target: { value: 'aB3!aB3!aB3!aB3!' } })

    expect(screen.getByText('Strong')).toBeInTheDocument()
  })
})

describe('SignupPage — server rejection and password visibility', () => {
  it('shows the server’s message and re-enables the button', async () => {
    setup(vi.fn().mockRejectedValue(new Error('Email already registered')))
    fillValid()

    fireEvent.click(submit())

    expect(await screen.findByText('Email already registered')).toBeInTheDocument()
    expect(submit()).toBeEnabled()
  })

  it('falls back to a generic message for a non-Error rejection', async () => {
    setup(vi.fn().mockRejectedValue({ weird: true }))
    fillValid()

    fireEvent.click(submit())

    expect(await screen.findByText('Sign up failed')).toBeInTheDocument()
  })

  it('toggles the password field between masked and visible', () => {
    setup()
    expect(pw()).toHaveAttribute('type', 'password')

    fireEvent.click(screen.getByLabelText('Toggle password visibility'))

    expect(pw()).toHaveAttribute('type', 'text')
  })

  it('links back to sign-in', () => {
    setup()

    expect(screen.getByRole('link', { name: /sign in/i })).toHaveAttribute('href', '/login')
  })
})
