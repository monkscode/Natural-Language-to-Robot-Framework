import { useAuth } from '@/auth/AuthContext'

const COPY: Record<string, { title: string; body: string }> = {
  pending: {
    title: 'Your access is pending approval',
    body: 'An administrator needs to approve your account before you can use the framework. You will gain access as soon as it is approved.',
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
