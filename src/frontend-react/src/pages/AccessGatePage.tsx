import { useAuth } from '@/auth/AuthContext'

const COPY: Record<string, { title: string; body: string }> = {
  pending: {
    // Approval bumps token_version, which revokes this pending token, and this
    // page never refreshes auth state — so access is NOT granted in place.
    // Telling the user it is would leave them waiting on a screen that can
    // never change. Sign out and back in is the actual path to an active session.
    title: 'Your access is pending approval',
    body: 'An administrator needs to approve your account before you can use the framework. Once approved, sign out and sign in again to start an active session.',
  },
  suspended: {
    title: 'Your access has been suspended',
    body: 'Your account is currently suspended. Contact an administrator if you believe this is a mistake.',
  },
  rejected: {
    title: 'Your access request was declined',
    body: 'Your account was not approved. Contact an administrator for more information.',
  },
}

export default function AccessGatePage() {
  const { status, logout } = useAuth()
  const copy = COPY[status ?? 'pending'] ?? COPY.pending
  return (
    <div className="flex min-h-svh flex-col items-center justify-center gap-4 bg-muted p-6 text-center">
      <h1 className="text-xl font-semibold">{copy.title}</h1>
      <p className="max-w-md text-sm text-muted-foreground">{copy.body}</p>
      <button
        onClick={logout}
        className="rounded-md border px-4 py-2 text-sm hover:bg-accent"
      >
        Sign out
      </button>
    </div>
  )
}
