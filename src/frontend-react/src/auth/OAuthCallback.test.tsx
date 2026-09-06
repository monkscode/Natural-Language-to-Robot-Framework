/**
 * OAuthCallback — the Google OAuth landing route. The token lives only in the
 * URL fragment (#token=...), is read once, exchanged via loginWithToken, and
 * the fragment is stripped immediately so it never lingers in history. Its
 * own comment says StrictMode double-invokes the effect in dev, and that the
 * SECOND invocation would see the already-stripped hash and flash a spurious
 * "token missing" error — the `ranOnce` ref guard exists so that second
 * invocation is a no-op instead. A guard that only works when nobody looks is
 * not a guard, so both the StrictMode path and a plain single invocation are
 * pinned here.
 */
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { StrictMode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async importOriginal => ({
  ...(await importOriginal<typeof import('react-router-dom')>()),
  useNavigate: () => mockNavigate,
}))
vi.mock('./AuthContext', () => ({ useAuth: vi.fn() }))

import { useAuth } from './AuthContext'
import OAuthCallback from './OAuthCallback'

const mockUseAuth = vi.mocked(useAuth)

beforeEach(() => {
  // Clean baseline before every test: no leftover hash from a previous case.
  window.history.pushState({}, '', '/oauth/callback')
})
afterEach(() => vi.resetAllMocks())

function setAuth(loginWithToken: (token: string) => Promise<void>) {
  mockUseAuth.mockReturnValue({
    user: null, loading: false, isAuthenticated: false, isAdmin: false, status: null,
    isOrgAdmin: false, canViewLearning: false,
    login: vi.fn(), signup: vi.fn(), loginWithToken,
    logout: vi.fn(), logoutAll: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
}

function renderCallback(strict: boolean) {
  const tree = (
    <MemoryRouter>
      <OAuthCallback />
    </MemoryRouter>
  )
  return render(strict ? <StrictMode>{tree}</StrictMode> : tree)
}

describe('OAuthCallback — the StrictMode double-invocation guard', () => {
  it('never flashes "token missing" from a second StrictMode invocation reading the already-stripped hash', async () => {
    window.history.pushState({}, '', '/oauth/callback#token=abc123')
    const loginWithToken = vi.fn().mockResolvedValue(undefined)
    setAuth(loginWithToken)

    renderCallback(true)

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/generate', { replace: true }))
    expect(loginWithToken).toHaveBeenCalledTimes(1)
    expect(loginWithToken).toHaveBeenCalledWith('abc123')
    // Without the ranOnce guard, the second invocation re-reads the hash
    // (already stripped by the first) and sets this error — pin that it never
    // does, which is the actual bug the guard prevents (NOT a double call to
    // loginWithToken: the second invocation takes the "no token" branch, it
    // does not re-invoke login — verified empirically, see the task report).
    expect(screen.queryByText('Sign-in token missing from the callback.')).toBeNull()
  })

  it('still exchanges the token correctly on a single (non-StrictMode) invocation', async () => {
    window.history.pushState({}, '', '/oauth/callback#token=xyz789')
    const loginWithToken = vi.fn().mockResolvedValue(undefined)
    setAuth(loginWithToken)

    renderCallback(false)

    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/generate', { replace: true }))
    expect(loginWithToken).toHaveBeenCalledTimes(1)
    expect(loginWithToken).toHaveBeenCalledWith('xyz789')
  })
})

describe('OAuthCallback — reading the token', () => {
  it('strips the fragment from the address bar immediately, before the async exchange resolves', async () => {
    window.history.pushState({}, '', '/oauth/callback#token=stripme')
    let resolveLogin!: () => void
    const loginWithToken = vi.fn(() => new Promise<void>(resolve => { resolveLogin = resolve }))
    setAuth(loginWithToken)

    renderCallback(false)

    // Assert BEFORE resolving: stripping happens synchronously in the
    // effect, not after the network call settles.
    expect(window.location.hash).toBe('')

    resolveLogin()
    await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/generate', { replace: true }))
  })

  it('shows an error and never calls loginWithToken when the fragment has no token', async () => {
    // beforeEach already set a bare pathname with no hash.
    const loginWithToken = vi.fn()
    setAuth(loginWithToken)

    renderCallback(false)

    expect(await screen.findByText('Sign-in token missing from the callback.')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Back to sign in' })).toBeInTheDocument()
    expect(loginWithToken).not.toHaveBeenCalled()
  })

  it('shows a generic error when the exchange rejects', async () => {
    window.history.pushState({}, '', '/oauth/callback#token=bad')
    setAuth(() => Promise.reject(new Error('network down')))

    renderCallback(false)

    expect(await screen.findByText('Could not complete sign-in. Please try again.')).toBeInTheDocument()
    expect(mockNavigate).not.toHaveBeenCalled()
  })
})
