/**
 * RequireAuth — the route guard between an unauthenticated visitor and every
 * page behind it.
 *
 * The `loading` branch is the one worth pinning. Without it a reload bounces
 * to /login before /auth/me has answered, and the user loses whatever page
 * they had open even though their session is perfectly valid. Two of these
 * tests exist purely to hold that branch in place, because "if (loading)
 * return spinner" reads like a cosmetic nicety and is not one.
 */
import { render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('./AuthContext', () => ({ useAuth: vi.fn() }))

import { useAuth } from './AuthContext'
import { RequireAuth } from './guards'

const mockUseAuth = vi.mocked(useAuth)
afterEach(() => vi.resetAllMocks())

type AuthShape = { isAuthenticated: boolean; loading: boolean }

function setAuth({ isAuthenticated, loading }: AuthShape) {
  mockUseAuth.mockReturnValue({
    user: isAuthenticated ? { id: 'u', email: 'a@b.com', display_name: 'A', role: 'user', status: 'active' } : null,
    loading, isAuthenticated, isAdmin: false, status: isAuthenticated ? 'active' : null,
    isOrgAdmin: false, canViewLearning: false,
    login: vi.fn(), signup: vi.fn(), loginWithToken: vi.fn(), logout: vi.fn(), logoutAll: vi.fn(),
  } as unknown as ReturnType<typeof useAuth>)
}

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<p>LOGIN PAGE</p>} />
        <Route path="/secret" element={<RequireAuth><p>SECRET CONTENT</p></RequireAuth>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('RequireAuth', () => {
  it('renders the guarded page for an authenticated caller', () => {
    setAuth({ isAuthenticated: true, loading: false })

    renderAt('/secret')

    expect(screen.getByText('SECRET CONTENT')).toBeInTheDocument()
  })

  it('redirects an unauthenticated caller to /login', () => {
    setAuth({ isAuthenticated: false, loading: false })

    renderAt('/secret')

    expect(screen.getByText('LOGIN PAGE')).toBeInTheDocument()
    expect(screen.queryByText('SECRET CONTENT')).toBeNull()
  })

  it('shows a spinner while the session is still hydrating — it does NOT bounce to login', () => {
    // The reload case. isAuthenticated is false here only because /auth/me has
    // not answered yet; redirecting on it would sign out every user who
    // pressed F5.
    setAuth({ isAuthenticated: false, loading: true })

    renderAt('/secret')

    expect(screen.queryByText('LOGIN PAGE')).toBeNull()
    expect(screen.queryByText('SECRET CONTENT')).toBeNull()
    expect(document.querySelector('.animate-spin')).not.toBeNull()
  })

  it('waits even when the session will turn out to be valid', () => {
    setAuth({ isAuthenticated: true, loading: true })

    renderAt('/secret')

    // loading wins over isAuthenticated: the guard commits to nothing until
    // hydration settles, so there is no flash of content either way.
    expect(screen.queryByText('SECRET CONTENT')).toBeNull()
    expect(document.querySelector('.animate-spin')).not.toBeNull()
  })
})
