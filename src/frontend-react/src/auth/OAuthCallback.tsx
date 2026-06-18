/**
 * Google OAuth landing route (/oauth/callback — deliberately NOT under /auth,
 * which the dev proxy and nginx forward to the backend).
 *
 * The backend redirects here with the freshly-minted JWT in the URL *fragment*
 * (#token=...), which never reaches a server or access log. We read it, store it
 * via the auth context, and forward to /generate.
 */

import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from './AuthContext'

export default function OAuthCallback() {
  const navigate = useNavigate()
  const { loginWithToken } = useAuth()
  const [error, setError] = useState('')
  const ranOnce = useRef(false)

  useEffect(() => {
    // StrictMode double-invokes effects in dev; the second run would see the
    // already-stripped hash and flash a spurious "token missing" error.
    if (ranOnce.current) return
    ranOnce.current = true
    const raw = window.location.hash.startsWith('#')
      ? window.location.hash.slice(1)
      : window.location.hash
    const token = new URLSearchParams(raw).get('token')
    // Strip the fragment immediately so the JWT never lingers in the address
    // bar or browser history while (or after) the async login runs.
    window.history.replaceState(null, '', window.location.pathname + window.location.search)
    if (!token) {
      setError('Sign-in token missing from the callback.')
      return
    }
    loginWithToken(token)
      .then(() => navigate('/generate', { replace: true }))
      .catch(() => setError('Could not complete sign-in. Please try again.'))
  }, [])

  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-4 bg-muted p-6 text-center">
      {error ? (
        <>
          <p className="text-sm text-destructive">{error}</p>
          <Link to="/login" className="text-sm font-semibold underline underline-offset-4">
            Back to sign in
          </Link>
        </>
      ) : (
        <>
          <span className="h-6 w-6 animate-spin rounded-full border-2 border-foreground border-t-transparent" />
          <p className="text-sm text-muted-foreground">Completing sign-in…</p>
        </>
      )}
    </div>
  )
}
