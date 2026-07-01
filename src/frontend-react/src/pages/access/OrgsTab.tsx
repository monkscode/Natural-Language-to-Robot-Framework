import { useEffect, useState } from 'react'
import { api, ApiError } from '@/lib/api'
import { Badge } from '@/components/ui/badge'

interface Org {
  id: string
  name: string
  kind: 'team' | 'personal'
  member_count: number
}
interface UserOption {
  id: string
  email: string
  status: 'pending' | 'active' | 'suspended' | 'rejected'
}
interface OrgMember {
  user_id: string
  email: string
  display_name: string
  org_role: 'org_admin' | 'org_member'
  status: string
}

export default function OrgsTab() {
  const [orgs, setOrgs] = useState<Org[]>([])
  const [users, setUsers] = useState<UserOption[]>([])
  const [name, setName] = useState('')
  const [ownerId, setOwnerId] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<string | null>(null)
  const [members, setMembers] = useState<OrgMember[]>([])

  async function load() {
    setError(null)
    try {
      setOrgs(await api<Org[]>('/auth/admin/orgs'))
      // Only active users can be seated as an org owner (a suspended/rejected
      // or not-yet-approved user shouldn't own an org).
      const candidates = await api<UserOption[]>('/auth/admin/users')
      setUsers(candidates.filter((u) => u.status === 'active'))
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load orgs')
    }
  }
  useEffect(() => { load() }, [])

  async function createOrg() {
    if (!name.trim() || !ownerId) {
      setError('A name and an owner are both required')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api('/auth/admin/orgs', {
        method: 'POST',
        body: JSON.stringify({ name: name.trim(), owner_user_id: ownerId }),
      })
      setName('')
      setOwnerId('')
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to create org')
    } finally {
      setBusy(false)
    }
  }

  async function loadMembers(orgId: string) {
    setMembers(await api<OrgMember[]>(`/auth/admin/orgs/${orgId}/members`))
  }

  async function toggleMembers(orgId: string) {
    if (expanded === orgId) {
      setExpanded(null)
      setMembers([])
      return
    }
    setError(null)
    try {
      await loadMembers(orgId)
      setExpanded(orgId)
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to load members')
    }
  }

  async function setOwner(orgId: string, userId: string, orgRole: 'org_admin' | 'org_member') {
    setError(null)
    try {
      await api(`/auth/admin/orgs/${orgId}/owner`, {
        method: 'POST',
        body: JSON.stringify({ user_id: userId, org_role: orgRole }),
      })
      await loadMembers(orgId)
      await load()
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Failed to update owner')
    }
  }

  return (
    <div className="flex flex-col gap-5">
      {error && <p className="text-xs text-destructive">{error}</p>}

      <div className="flex flex-col gap-2 rounded-md border p-3">
        <h2 className="text-sm font-semibold">Create a team org</h2>
        <div className="flex flex-wrap gap-2">
          <input value={name} onChange={(e) => setName(e.target.value)}
            placeholder="Org name"
            className="flex-1 rounded-md border px-3 py-1 text-sm" />
          <select value={ownerId} onChange={(e) => setOwnerId(e.target.value)}
            className="rounded-md border px-3 py-1 text-sm">
            <option value="">Select owner…</option>
            {users.map((u) => (
              <option key={u.id} value={u.id}>{u.email}</option>
            ))}
          </select>
          <button disabled={busy} onClick={createOrg}
            className="rounded-md bg-primary px-3 py-1 text-sm text-primary-foreground disabled:opacity-50">
            Create
          </button>
        </div>
      </div>

      <ul className="flex flex-col gap-2">
        {orgs.map((o) => (
          <li key={o.id} className="flex flex-col gap-2 rounded-md border p-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">{o.name}</span>
                <Badge variant={o.kind === 'team' ? 'default' : 'secondary'}>{o.kind}</Badge>
                <span className="text-xs text-muted-foreground">
                  {o.member_count} member{o.member_count === 1 ? '' : 's'}
                </span>
              </div>
              {o.kind === 'team' && (
                <button onClick={() => toggleMembers(o.id)}
                  className="rounded-md border px-3 py-1 text-sm">
                  {expanded === o.id ? 'Hide members' : 'Members'}
                </button>
              )}
            </div>
            {o.kind === 'team' && expanded === o.id && (
              <ul className="flex flex-col gap-1 border-t pt-2">
                {members.length === 0 && (
                  <li className="text-xs text-muted-foreground">No members.</li>
                )}
                {members.map((m) => (
                  <li key={m.user_id} className="flex items-center justify-between text-sm">
                    <div className="flex items-center gap-2">
                      <span>{m.email}</span>
                      <Badge variant={m.org_role === 'org_admin' ? 'default' : 'outline'}>
                        {m.org_role}
                      </Badge>
                      <span className="text-xs text-muted-foreground">{m.status}</span>
                    </div>
                    {m.org_role === 'org_member' ? (
                      <button onClick={() => setOwner(o.id, m.user_id, 'org_admin')}
                        className="text-xs underline">Make owner</button>
                    ) : (
                      <button onClick={() => setOwner(o.id, m.user_id, 'org_member')}
                        className="text-xs underline">Make member</button>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
