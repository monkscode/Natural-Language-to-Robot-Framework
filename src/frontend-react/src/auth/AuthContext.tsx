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
  org_role?: 'org_admin' | 'org_member' | null
}

interface AuthState {
  user: User | null
  loading: boolean
  isAuthenticated: boolean
  isAdmin: boolean
  status: User['status'] | null
  isOrgAdmin: boolean
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
        isOrgAdmin: user?.org_role === 'org_admin',
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
