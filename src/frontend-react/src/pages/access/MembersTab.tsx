import { useEffect, useState } from 'react'
import { api, ApiError } from '@/lib/api'
import { useAuth } from '@/auth/AuthContext'
import { Badge } from '@/components/ui/badge'

interface Member {
  id: string
  email: string
  display_name: string
  role: 'user' | 'admin'
  status: 'pending' | 'active' | 'suspended' | 'rejected'
}

function statusVariant(s: Member['status']): 'default' | 'secondary' | 'destructive' | 'outline' {
  if (s === 'active') return 'default'
  if (s === 'suspended' || s === 'rejected') return 'destructive'
  return 'secondary'
}

export default function MembersTab() {
  const { user } = useAuth()
  const [users, setUsers] = useState<Member[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // See PendingTab: the list starts empty, so "No members yet." would otherwise
  // render during the request and after a failed one — an empty roster is a
  // claim, and it may only be made once a request has returned one.
  const [loaded, setLoaded] = useState(false)

  async function load() {
    setError(null)
    try {
      const all = await api<Member[]>('/auth/admin/users')
      // Pending users live in the Pending tab.
      setUsers(all.filter((u) => u.status !== 'pending'))
      setLoaded(true)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load users')
    }
  }
  useEffect(() => { load() }, [])

  async function mutate(id: string, path: string, body?: unknown) {
    setBusy(id)
    setError(null)
    try {
      await api(path, { method: 'POST', ...(body ? { body: JSON.stringify(body) } : {}) })
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {error && <p className="text-xs text-destructive">{error}</p>}
      {loaded && users.length === 0 && (
        <p className="text-sm text-muted-foreground">No members yet.</p>
      )}
      <ul className="flex flex-col gap-2">
        {users.map((u) => (
          <li key={u.id} className="flex items-center justify-between rounded-md border p-3">
            <div className="flex flex-col gap-1">
              <div className="text-sm font-medium">{u.email}</div>
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">{u.display_name || '—'}</span>
                <Badge variant={statusVariant(u.status)}>{u.status}</Badge>
                <Badge variant={u.role === 'admin' ? 'default' : 'outline'}>{u.role}</Badge>
              </div>
            </div>
            <div className="flex gap-2">
              {u.status === 'active' && (
                <button disabled={busy === u.id || u.id === user?.id}
                  title={u.id === user?.id ? 'You cannot suspend your own account' : undefined}
                  onClick={() => mutate(u.id, `/auth/admin/users/${u.id}/suspend`)}
                  className="rounded-md border px-3 py-1 text-sm disabled:opacity-50">
                  Suspend
                </button>
              )}
              {(u.status === 'suspended' || u.status === 'rejected') && (
                <button disabled={busy === u.id}
                  onClick={() => mutate(u.id, `/auth/admin/users/${u.id}/reactivate`)}
                  className="rounded-md bg-primary px-3 py-1 text-sm text-primary-foreground disabled:opacity-50">
                  Reactivate
                </button>
              )}
              {u.role === 'user' ? (
                <button disabled={busy === u.id}
                  onClick={() => mutate(u.id, `/auth/admin/users/${u.id}/role`, { role: 'admin' })}
                  className="rounded-md border px-3 py-1 text-sm disabled:opacity-50">
                  Make admin
                </button>
              ) : (
                <button disabled={busy === u.id || u.id === user?.id}
                  title={u.id === user?.id ? 'You cannot remove your own admin role' : undefined}
                  onClick={() => mutate(u.id, `/auth/admin/users/${u.id}/role`, { role: 'user' })}
                  className="rounded-md border px-3 py-1 text-sm disabled:opacity-50">
                  Remove admin
                </button>
              )}
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
