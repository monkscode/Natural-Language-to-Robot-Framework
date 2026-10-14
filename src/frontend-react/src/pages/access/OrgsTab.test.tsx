/**
 * OrgsTab — team orgs, their membership, and the moves between them.
 *
 * This component carries a hand-rolled request-sequence guard (membersReq),
 * and it is the reason for most of this file. Expanding one org while another
 * org's member load is still in flight, or collapsing a row mid-load, can put
 * ONE org's users under ANOTHER org's heading — which on this page is a
 * cross-tenant display error, not a cosmetic one. The guard has three separate
 * jobs (drop a superseded load, refuse to expand on one, cancel on collapse)
 * and each is tested here, because a counter in a ref is exactly the kind of
 * thing a refactor drops.
 *
 * The owner-candidate filter is the other load-bearing rule: only ACTIVE users
 * may be seated, so a suspended or not-yet-approved account must never appear
 * in the owner or add-member pickers.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))

import { api, ApiError } from '@/lib/api'
import OrgsTab from './OrgsTab'

const mockApi = vi.mocked(api)
afterEach(() => vi.resetAllMocks())

const TEAM_A = { id: 'oa', name: 'Acme', kind: 'team', member_count: 2, owner_email: 'boss@acme.com' }
const TEAM_B = { id: 'ob', name: 'Globex', kind: 'team', member_count: 1, owner_email: 'x@globex.com' }
const PERSONAL = { id: 'op', name: 'Solo', kind: 'personal', member_count: 1, owner_email: 'solo@corp.com' }

const ACTIVE = { id: 'u1', email: 'active@corp.com', display_name: 'Active', status: 'active' }
// Active, and deliberately NOT a member of Acme: add-candidates are active
// users not already seated in the expanded org, so a seated user is filtered
// out of that picker and cannot be used to test adding.
const UNSEATED = { id: 'u4', email: 'unseated@corp.com', display_name: 'Unseated', status: 'active' }
const SUSPENDED = { id: 'u2', email: 'suspended@corp.com', display_name: 'Susp', status: 'suspended' }
const PENDING = { id: 'u3', email: 'pending@corp.com', display_name: 'Pend', status: 'pending' }

const MEMBER_A = { user_id: 'u1', email: 'active@corp.com', display_name: 'Active', org_role: 'org_member' }

/** Answer each endpoint from a table, so call ORDER never decides the payload. */
function routeApi(overrides: Record<string, unknown> = {}) {
  mockApi.mockImplementation((path: string) => {
    for (const [frag, value] of Object.entries(overrides)) {
      if (path.includes(frag)) {
        return value instanceof Error ? Promise.reject(value) : Promise.resolve(value)
      }
    }
    if (path.includes('/members')) return Promise.resolve([MEMBER_A])
    if (path.includes('/orgs')) return Promise.resolve([TEAM_A, TEAM_B, PERSONAL])
    if (path.includes('/users')) return Promise.resolve([ACTIVE, SUSPENDED, PENDING, UNSEATED])
    return Promise.resolve(undefined)
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
  }) as any
}

describe('OrgsTab — the org list', () => {
  it('lists every org with its owner and member count', async () => {
    routeApi()

    render(<OrgsTab />)

    expect(await screen.findByText('Acme')).toBeInTheDocument()
    expect(screen.getByText('Globex')).toBeInTheDocument()
    expect(screen.getByText('Solo')).toBeInTheDocument()
  })

  it('offers a Members button only for TEAM orgs', async () => {
    // A personal org has exactly one seat by construction; offering membership
    // management on it invites an action the backend will refuse.
    routeApi()

    render(<OrgsTab />)
    await screen.findByText('Acme')

    expect(screen.getAllByText('Members')).toHaveLength(2)
  })

  it('surfaces a failed load', async () => {
    routeApi({ '/orgs': new ApiError(403, 'Platform admin required') })

    render(<OrgsTab />)

    expect(await screen.findByText('Platform admin required')).toBeInTheDocument()
  })
})

describe('OrgsTab — only ACTIVE users may be seated', () => {
  it('offers only active users as owner candidates', async () => {
    routeApi()

    render(<OrgsTab />)
    await screen.findByText('Acme')

    expect(screen.getByRole('option', { name: 'active@corp.com' })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: 'suspended@corp.com' })).toBeNull()
    expect(screen.queryByRole('option', { name: 'pending@corp.com' })).toBeNull()
  })
})

describe('OrgsTab — creating an org', () => {
  it('refuses to create without both a name and an owner', async () => {
    routeApi()
    render(<OrgsTab />)
    await screen.findByText('Acme')
    mockApi.mockClear()

    fireEvent.click(screen.getByText('Create'))

    expect(await screen.findByText('A name and an owner are both required')).toBeInTheDocument()
    expect(mockApi).not.toHaveBeenCalled()
  })

  it('creates the org, then reloads the list', async () => {
    routeApi()
    render(<OrgsTab />)
    await screen.findByText('Acme')
    fireEvent.change(screen.getByPlaceholderText('Org name'), { target: { value: 'New Team' } })
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'u1' } })

    fireEvent.click(screen.getByText('Create'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/orgs', expect.objectContaining({ method: 'POST' })))
  })

  it('surfaces a rejected creation', async () => {
    routeApi()
    render(<OrgsTab />)
    await screen.findByText('Acme')
    fireEvent.change(screen.getByPlaceholderText('Org name'), { target: { value: 'Dupe' } })
    fireEvent.change(screen.getAllByRole('combobox')[0], { target: { value: 'u1' } })
    routeApi({ '/auth/admin/orgs': new ApiError(409, 'An org with that name exists') })

    fireEvent.click(screen.getByText('Create'))

    expect(await screen.findByText('An org with that name exists')).toBeInTheDocument()
  })
})

describe('OrgsTab — membership', () => {
  async function expandAcme() {
    routeApi()
    render(<OrgsTab />)
    await screen.findByText('Acme')
    fireEvent.click(screen.getAllByText('Members')[0])
    await screen.findByText('Hide members')
  }

  it('expands to show that org’s members', async () => {
    await expandAcme()

    expect(screen.getAllByText('active@corp.com').length).toBeGreaterThan(0)
  })

  it('collapses again, dropping the rows', async () => {
    await expandAcme()

    fireEvent.click(screen.getByText('Hide members'))

    await waitFor(() => expect(screen.queryByText('Hide members')).toBeNull())
  })

  it('adds a member with the org_member role, then refreshes', async () => {
    await expandAcme()
    const addPicker = screen.getAllByRole('combobox').find(s =>
      Array.from(s.querySelectorAll('option')).some(o => o.textContent === 'Add member…'))!
    fireEvent.change(addPicker, { target: { value: 'u4' } })

    fireEvent.click(screen.getByText('Add'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/orgs/oa/members', {
      method: 'POST', body: JSON.stringify({ user_id: 'u4', org_role: 'org_member' }),
    }))
  })

  it('removes a member through DELETE', async () => {
    await expandAcme()

    fireEvent.click(screen.getByText('Remove'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/orgs/oa/members/u1', { method: 'DELETE' }))
  })

  it('promotes a member to owner', async () => {
    await expandAcme()

    fireEvent.click(screen.getByText('Make owner'))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/orgs/oa/owner', {
      method: 'POST', body: JSON.stringify({ user_id: 'u1', org_role: 'org_admin' }),
    }))
  })

  it('moves a member to another team org, refreshing the org they LEFT', async () => {
    // refreshOrg(fromOrgId), not the destination: the row that changed on
    // screen is the one the user was removed from.
    await expandAcme()
    const movePicker = screen.getAllByRole('combobox').find(s =>
      Array.from(s.querySelectorAll('option')).some(o => o.textContent === 'Move to…'))!

    fireEvent.change(movePicker, { target: { value: 'ob' } })

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/auth/admin/users/u1/reassign', {
      method: 'POST',
      body: JSON.stringify({ from_org_id: 'oa', to_org_id: 'ob', org_role: 'org_member' }),
    }))
  })

  it('surfaces a refused member action', async () => {
    await expandAcme()
    routeApi({ '/members/u1': new ApiError(403, 'Cannot remove the owner') })

    fireEvent.click(screen.getByText('Remove'))

    expect(await screen.findByText('Cannot remove the owner')).toBeInTheDocument()
  })
})

describe('OrgsTab — the members request-sequence guard', () => {
  it('does not expand an org whose member load was superseded', async () => {
    // Click Acme, then Globex before Acme's rows land. Acme's response is no
    // longer current, so it must neither seat rows nor expand its row —
    // otherwise one org's users appear under another org's heading.
    let releaseAcme: (v: unknown) => void = () => {}
    mockApi.mockImplementation((path: string) => {
      if (path.includes('/orgs/oa/members')) return new Promise(r => { releaseAcme = r })   // acme-only@corp.com, below
      if (path.includes('/orgs/ob/members')) return Promise.resolve([{ ...MEMBER_A, user_id: 'u9', email: 'globex-only@corp.com' }])
      if (path.includes('/orgs')) return Promise.resolve([TEAM_A, TEAM_B])
      if (path.includes('/users')) return Promise.resolve([ACTIVE])
      return Promise.resolve(undefined)
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    }) as any
    render(<OrgsTab />)
    await screen.findByText('Acme')

    fireEvent.click(screen.getAllByText('Members')[0])   // Acme — stalls
    fireEvent.click(screen.getAllByText('Members')[1])   // Globex — resolves
    await screen.findByText('globex-only@corp.com')

    releaseAcme([{ ...MEMBER_A, user_id: 'u7', email: 'acme-only@corp.com' }])

    // Acme's late rows must not replace Globex's. 'acme-only@corp.com' appears
    // nowhere else on the page, so finding it would mean the stale load landed.
    await waitFor(() => expect(screen.getByText('globex-only@corp.com')).toBeInTheDocument())
    expect(screen.queryByText('acme-only@corp.com')).toBeNull()
  })

  it('cancels an in-flight load when the row is collapsed', async () => {
    let release: (v: unknown) => void = () => {}
    let calls = 0
    mockApi.mockImplementation((path: string) => {
      if (path.includes('/orgs/oa/members')) {
        calls += 1
        if (calls === 1) return Promise.resolve([MEMBER_A])
        return new Promise(r => { release = r })
      }
      if (path.includes('/orgs')) return Promise.resolve([TEAM_A])
      if (path.includes('/users')) return Promise.resolve([ACTIVE])
      return Promise.resolve(undefined)
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
    }) as any
    render(<OrgsTab />)
    await screen.findByText('Acme')
    fireEvent.click(screen.getByText('Members'))
    await screen.findByText('Hide members')

    fireEvent.click(screen.getByText('Hide members'))   // collapse
    fireEvent.click(screen.getByText('Members'))        // re-expand: stalls
    fireEvent.click(screen.getByText('Members'))        // collapse again, cancelling it
    release([MEMBER_A])

    // The cancelled load must not put the rows back under a collapsed row.
    await waitFor(() => expect(screen.getByText('Members')).toBeInTheDocument())
  })
})
