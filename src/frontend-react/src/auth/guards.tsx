/**
 * Route guards.
 *
 * - RequireAuth: redirects unauthenticated users to /login. Waits for the
 *   initial /auth/me hydration so a reload doesn't flicker to /login before
 *   the session is restored.
 *
 * Admin gating is no longer a route guard: KeepAlivePages (App.tsx) owns it,
 * mounting admin-only pages only for admins and bouncing non-admins to
 * /generate. Regular users get Generate + their own History.
 */

import type { ReactElement } from 'react'
import { Navigate, useLocation } from 'react-router-dom'
import { useAuth } from './AuthContext'

function FullScreenSpinner() {
  return (
    <div className="flex min-h-svh items-center justify-center bg-muted">
      <span className="h-6 w-6 animate-spin rounded-full border-2 border-foreground border-t-transparent" />
    </div>
  )
}

export function RequireAuth({ children }: { children: ReactElement }) {
  const { isAuthenticated, loading } = useAuth()
  const location = useLocation()
  if (loading) return <FullScreenSpinner />
  if (!isAuthenticated) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  return children
}
