import { useEffect, useState } from 'react'
import { api, ApiError } from '@/lib/api'

interface Pending {
  id: string
  email: string
  display_name: string
  invited_org: string | null
}

export default function PendingTab() {
  const [pending, setPending] = useState<Pending[]>([])
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // "No one is waiting for approval" is a definite claim about the queue, so it
  // may only be made once a request has actually returned one. The list starts
  // empty, so without this it renders during the request AND after a failed one
  // — telling an admin the queue is clear when nobody has looked.
  const [loaded, setLoaded] = useState(false)

  async function load() {
    setError(null)
    try {
      setPending(await api<Pending[]>('/auth/admin/pending'))
      setLoaded(true)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load pending users')
    }
  }
  useEffect(() => { load() }, [])

  async function act(id: string, action: 'approve' | 'reject') {
    setBusy(id)
    try {
      await api(`/auth/admin/users/${id}/${action}`, { method: 'POST' })
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Action failed')
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      {error && <p className="text-xs text-destructive">{error}</p>}
      {loaded && pending.length === 0 && (
        <p className="text-sm text-muted-foreground">No one is waiting for approval.</p>
      )}
      <ul className="flex flex-col gap-2">
        {pending.map((p) => (
          <li key={p.id} className="flex items-center justify-between rounded-md border p-3">
            <div>
              <div className="text-sm font-medium">{p.email}</div>
              <div className="text-xs text-muted-foreground">
                {p.display_name || '—'}{p.invited_org ? ` → joining ${p.invited_org}` : ' → personal'}
              </div>
            </div>
            <div className="flex gap-2">
              <button disabled={busy === p.id} onClick={() => act(p.id, 'approve')}
                className="rounded-md bg-primary px-3 py-1 text-sm text-primary-foreground disabled:opacity-50">
                Approve
              </button>
              <button disabled={busy === p.id} onClick={() => act(p.id, 'reject')}
                className="rounded-md border px-3 py-1 text-sm disabled:opacity-50">
                Reject
              </button>
            </div>
          </li>
        ))}
      </ul>
    </div>
  )
}
