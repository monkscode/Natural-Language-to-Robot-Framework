import { useEffect, useState } from 'react'
import { api, ApiError } from '@/lib/api'

interface Member { user_id: string; email: string; org_role: string; status: string }
interface Invite { id: string; email: string; status: string }

export default function TeamPage() {
  const [members, setMembers] = useState<Member[]>([])
  const [invites, setInvites] = useState<Invite[]>([])
  const [email, setEmail] = useState('')
  const [error, setError] = useState<string | null>(null)

  async function load() {
    setError(null)
    try {
      setMembers(await api<Member[]>('/auth/org/members'))
      setInvites(await api<Invite[]>('/auth/org/invitations'))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load team')
    }
  }
  useEffect(() => { load() }, [])

  async function invite() {
    setError(null)
    try {
      await api('/auth/org/invitations', { method: 'POST', body: JSON.stringify({ email }) })
      setEmail('')
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to invite')
    }
  }

  async function revoke(id: string) {
    try {
      await api(`/auth/org/invitations/${id}`, { method: 'DELETE' })
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to revoke invite')
    }
    await load()
  }

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-lg font-semibold">Team members</h1>
        <ul className="flex flex-col gap-1">
          {members.map((m) => (
            <li key={m.user_id} className="flex justify-between rounded-md border p-2 text-sm">
              <span>{m.email}</span>
              <span className="text-muted-foreground">{m.org_role} · {m.status}</span>
            </li>
          ))}
        </ul>
      </div>
      <div className="flex flex-col gap-2">
        <h2 className="text-sm font-semibold">Invite a member</h2>
        <div className="flex gap-2">
          <input value={email} onChange={(e) => setEmail(e.target.value)}
            placeholder="name@company.com"
            className="flex-1 rounded-md border px-3 py-1 text-sm" />
          <button onClick={invite}
            className="rounded-md bg-primary px-3 py-1 text-sm text-primary-foreground">
            Invite
          </button>
        </div>
        {error && <p className="text-xs text-destructive">{error}</p>}
        <p className="text-xs text-muted-foreground">
          Invited people still require administrator approval before they can sign in.
        </p>
        <ul className="flex flex-col gap-1">
          {invites.filter((i) => i.status === 'open').map((i) => (
            <li key={i.id} className="flex justify-between rounded-md border p-2 text-sm">
              <span>{i.email}</span>
              <button onClick={() => revoke(i.id)} className="text-xs underline">revoke</button>
            </li>
          ))}
        </ul>
      </div>
    </div>
  )
}
