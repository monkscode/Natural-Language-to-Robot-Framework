import { BrowserRouter, Routes, Route, Navigate, Outlet, useLocation } from 'react-router-dom'
import { useRef } from 'react'
import { ThemeProvider } from '@/components/theme-provider'
import { AuthProvider, useAuth } from '@/auth/AuthContext'
import { RequireAuth } from '@/auth/guards'
import { SidebarProvider, SidebarInset } from '@/components/ui/sidebar'
import { AppSidebar } from '@/components/app-sidebar'
import { AppHeader } from '@/components/app-header'
import GeneratePage from '@/pages/GeneratePage'
import HistoryPage from '@/pages/HistoryPage'
import MetricsPage from '@/pages/MetricsPage'
import TemplatesPage from '@/pages/TemplatesPage'
import SettingsPage from '@/pages/SettingsPage'
import LearningPage from '@/pages/LearningPage'
import LoginPage from '@/pages/auth/LoginPage'
import SignupPage from '@/pages/auth/SignupPage'
import ForgotPasswordPage from '@/pages/auth/ForgotPasswordPage'
import OAuthCallback from '@/auth/OAuthCallback'
import AccessGatePage from '@/pages/AccessGatePage'
import AccessConsolePage from '@/pages/AccessConsolePage'
import TeamPage from '@/pages/TeamPage'

/**
 * Keep-alive page cache.
 *
 * Every page is mounted ONCE on first visit and then kept alive (hidden, not
 * destroyed) for the rest of the tab session, so ALL page state — typed
 * queries, generated code, fetched lists, filters, open drawers, and even
 * in-flight generation/execution SSE streams — survives any page switch
 * exactly as the user left it. No serialization, no per-page persistence
 * code: the component instances simply never unmount. A hard refresh (F5)
 * starts clean, and logout unmounts the whole layout (state never leaks
 * across sessions).
 *
 * Role gating: each page can require admin (`admin: true`) and/or org-admin
 * (`orgAdmin: true`) access; a page is only ever mounted when
 * `(!p.admin || isAdmin) && (!p.orgAdmin || isOrgAdmin)` holds, via the
 * `allowed(p)` helper below. A user lacking the required role who navigates
 * to a gated path (e.g. an admin-only page, or the org-admin-only `/team`)
 * is bounced to /generate — the exact behaviour RequireAdmin had when each
 * route owned its element. The redirect renders only for the ACTIVE path,
 * so a cached page can never hijack navigation.
 */
const PAGES: Array<{ path: string; admin?: boolean; orgAdmin?: boolean; node: JSX.Element }> = [
  { path: '/generate', node: <GeneratePage /> },
  { path: '/history', node: <HistoryPage /> },
  { path: '/metrics', admin: true, node: <MetricsPage /> },
  { path: '/learning', admin: true, node: <LearningPage /> },
  { path: '/templates', admin: true, node: <TemplatesPage /> },
  { path: '/access', admin: true, node: <AccessConsolePage /> },
  { path: '/settings', admin: true, node: <SettingsPage /> },
  { path: '/team', orgAdmin: true, node: <TeamPage /> },
]

function KeepAlivePages() {
  const { pathname } = useLocation()
  const { isAdmin, isOrgAdmin } = useAuth()
  const allowed = (p: { admin?: boolean; orgAdmin?: boolean }) =>
    (!p.admin || isAdmin) && (!p.orgAdmin || isOrgAdmin)
  const visited = useRef(new Set<string>())

  const active = PAGES.find(p => p.path === pathname)
  if (active && allowed(active)) visited.current.add(active.path)

  if (active && !allowed(active)) return <Navigate to="/generate" replace />

  // The admin predicate is re-checked on every render, so if a session is
  // demoted mid-flight (isAdmin flips false), any already-mounted admin page
  // unmounts immediately instead of lingering hidden and firing now-forbidden
  // background requests.
  return (
    <>
      {PAGES.filter(p => visited.current.has(p.path) && allowed(p)).map(p => (
        <div
          key={p.path}
          className={p.path === pathname ? 'flex flex-1 flex-col' : 'hidden'}
        >
          {p.node}
        </div>
      ))}
    </>
  )
}

/** Full app layout: sidebar + topbar + keep-alive page content */
function AppLayout() {
  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
        <AppHeader />
        {/* Page content — SidebarInset provides the responsive padding offset.
            KeepAlivePages renders the actual pages; the Outlet only carries
            the index / catch-all redirects (page routes are element={null}). */}
        <div className="flex flex-1 flex-col p-6 pt-4 overflow-auto">
          <KeepAlivePages />
          <Outlet />
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}

function GatedLayout() {
  const { status } = useAuth()
  if (status !== 'active') return <AccessGatePage />
  return <AppLayout />
}

export default function App() {
  return (
    <ThemeProvider>
      <AuthProvider>
        <BrowserRouter>
          <Routes>
            {/* Auth routes — no sidebar, public */}
            <Route path="/login"            element={<LoginPage />} />
            <Route path="/signup"           element={<SignupPage />} />
            <Route path="/forgot-password"  element={<ForgotPasswordPage />} />
            <Route path="/oauth/callback"   element={<OAuthCallback />} />

            {/* Protected app routes — require a valid session. The page
                routes render null: KeepAlivePages (in AppLayout) owns the
                page elements so they persist across navigation. */}
            <Route element={<RequireAuth><GatedLayout /></RequireAuth>}>
              <Route index            element={<Navigate to="/generate" replace />} />
              {/* Every authenticated user gets Generate + their own History */}
              <Route path="/generate" element={null} />
              <Route path="/history"  element={null} />
              {/* Admin-only pages (gated inside KeepAlivePages) */}
              <Route path="/metrics"   element={null} />
              <Route path="/learning"  element={null} />
              <Route path="/templates" element={null} />
              <Route path="/settings"  element={null} />
              <Route path="/access" element={null} />
              <Route path="/team"   element={null} />
              {/* catch-all */}
              <Route path="*"          element={<Navigate to="/generate" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  )
}
