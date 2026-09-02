/**
 * Auth context — holds the current user and exposes login/signup/logout.
 *
 * On mount it hydrates from /auth/me if a token exists (so a page reload keeps
 * the session). `isAdmin` drives role-based routing/nav. Tokens live in
 * localStorage via lib/api.
 */

import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api, clearToken, getToken, setToken } from '@/lib/api'

export interface User {
  id: string
  email: string
  display_name: string
  role: 'user' | 'admin'
  status: 'pending' | 'active' | 'suspended' | 'rejected'
  // True iff the user is org_admin of a TEAM org — the signal that unlocks the
  // org-owner Team page/nav. Personal-org admin (everyone) does NOT set this.
  is_org_admin?: boolean
  // True iff the caller's active-org org_role is org_admin — the server's own
  // folder-authority rule (delete a folder, rename one they did not create,
  // file anyone's run). A solo user IS org_admin of their personal org, so
  // this is true for them while is_org_admin is not. Never swap the two.
  can_manage_org_folders?: boolean
  // True iff the caller may view an org-level aggregate dashboard — today
  // this gates only the Learning page (/metrics and the traces dashboard
  // have the identical divergence and are a deliberate, separate
  // follow-up). Mirrors the server's is_dashboard_viewer rule: true for a
  // platform admin (regardless of their own org_role), or for an org_admin
  // of the caller's own org — so, like can_manage_org_folders and unlike
  // is_org_admin, a solo user's own PERSONAL org counts. It is NOT the same
  // rule as can_manage_org_folders: that one has no platform-admin
  // short-circuit, so a platform admin who is merely an org_member of their
  // current org (e.g. one who joined a team and so lost their personal-org
  // seat) gets can_manage_org_folders=false but can_view_learning=true.
  // Never reuse one for the other.
  can_view_learning?: boolean
}

interface AuthState {
  user: User | null
  loading: boolean
  isAuthenticated: boolean
  isAdmin: boolean
  status: User['status'] | null
  isOrgAdmin: boolean
  canViewLearning: boolean
  login: (email: string, password: string) => Promise<void>
  signup: (email: string, password: string, displayName: string) => Promise<void>
  loginWithToken: (token: string) => Promise<void>
  logout: () => void
  logoutAll: () => Promise<void>
}

const AuthContext = createContext<AuthState | undefined>(undefined)

interface AuthResponse {
  access_token: string
  user: User
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [loading, setLoading] = useState(true)

  async function hydrate() {
    if (!getToken()) {
      setUser(null)
      setLoading(false)
      return
    }
    try {
      setUser(await api<User>('/auth/me'))
    } catch {
      clearToken()
      setUser(null)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    hydrate()
  }, [])

  async function login(email: string, password: string) {
    const data = await api<AuthResponse>('/auth/login', {
      method: 'POST',
      auth: false,
      body: JSON.stringify({ email, password }),
    })
    setToken(data.access_token)
    setUser(data.user)
  }

  async function signup(email: string, password: string, displayName: string) {
    const data = await api<AuthResponse>('/auth/register', {
      method: 'POST',
      auth: false,
      body: JSON.stringify({ email, password, display_name: displayName }),
    })
    setToken(data.access_token)
    setUser(data.user)
  }

  async function loginWithToken(token: string) {
    setToken(token)
    setLoading(true)
    await hydrate()
  }

  function logout() {
    api('/auth/logout', { method: 'POST' }).catch(() => {})
    clearToken()
    setUser(null)
  }

  // Revoke every token for this account (this device and any other) by bumping
  // the server-side token_version, then clear the local session. The POST must
  // run while the token is still present (it identifies whose tokens to revoke),
  // so await it BEFORE clearing.
  async function logoutAll() {
    await api('/auth/logout-all', { method: 'POST' }).catch(() => {})
    clearToken()
    setUser(null)
  }

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        isAuthenticated: !!user,
        isAdmin: user?.role === 'admin',
        status: user?.status ?? null,
        isOrgAdmin: user?.is_org_admin ?? false,
        canViewLearning: user?.can_view_learning ?? false,
        login,
        signup,
        loginWithToken,
        logout,
        logoutAll,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within an AuthProvider')
  return ctx
}
