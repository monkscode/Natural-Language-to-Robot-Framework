import { BrowserRouter, Routes, Route, Navigate, Outlet } from 'react-router-dom'
import { ThemeProvider } from '@/components/theme-provider'
import { AuthProvider } from '@/auth/AuthContext'
import { RequireAuth, RequireAdmin } from '@/auth/guards'
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

/** Full app layout: sidebar + topbar + page content */
function AppLayout() {
  return (
    <SidebarProvider>
      <AppSidebar />
      <SidebarInset>
        <AppHeader />
        {/* Page content — SidebarInset provides the responsive padding offset */}
        <div className="flex flex-1 flex-col p-6 pt-4 overflow-auto">
          <Outlet />
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
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

            {/* Protected app routes — require a valid session */}
            <Route element={<RequireAuth><AppLayout /></RequireAuth>}>
              <Route index            element={<Navigate to="/generate" replace />} />
              {/* Every authenticated user gets Generate */}
              <Route path="/generate" element={<GeneratePage />} />
              {/* Admin-only pages */}
              <Route path="/history"   element={<RequireAdmin><HistoryPage /></RequireAdmin>} />
              <Route path="/metrics"   element={<RequireAdmin><MetricsPage /></RequireAdmin>} />
              <Route path="/learning"  element={<RequireAdmin><LearningPage /></RequireAdmin>} />
              <Route path="/templates" element={<RequireAdmin><TemplatesPage /></RequireAdmin>} />
              <Route path="/settings"  element={<RequireAdmin><SettingsPage /></RequireAdmin>} />
              {/* catch-all */}
              <Route path="*"          element={<Navigate to="/generate" replace />} />
            </Route>
          </Routes>
        </BrowserRouter>
      </AuthProvider>
    </ThemeProvider>
  )
}
