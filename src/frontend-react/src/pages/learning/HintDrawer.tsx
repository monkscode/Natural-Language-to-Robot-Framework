/**
 * Hint detail drawer — full hint text + anchor, metadata, scope/category
 * editing (PATCH), lifecycle actions, and the audit timeline.
 * GET /api/learning/hints/{id}, PATCH /api/learning/hints/{id}
 */

import { useEffect, useState } from 'react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet'
import { useFetch } from '@/lib/useFetch'
import { api } from '@/lib/api'
import { useAuth } from '@/auth/AuthContext'
import { cn } from '@/lib/utils'
import { HINT_CATEGORIES, FAILURE_CATEGORIES, fmtWhen } from './types'
import type { HintDetailResp, TimelineEntry } from './types'

const ACTION_LABELS: Record<string, string> = {
  create: 'created',
  reinforce: 'reinforced',
  unflag: 'unflagged',
  retract: 'retracted',
  reactivate: 'reactivated',
  auto_disable: 'auto-disabled',
  edit: 'edited',
  patch: 'edited',
  change_scope: 'changed scope',
  change_category: 'changed category',
  flagged: 'flagged this hint',
  flag_recommended_suppressed: 'recommended flag (suppressed by history guard)',
  trigger_1_flag: 'Trigger 1: flagged',
  trigger_2_flag: 'Trigger 2: flagged',
  llm_review_disable: 'disabled via LLM review',
  llm_review_reactivate: 'reactivated via LLM review',
  llm_review_unflag: 'unflagged via LLM review',
  llm_review_keep: 'kept via LLM review',
  llm_review_flagged: 'flagged for review via LLM review',
}

/** "{a:1} → {a:2}" detail for audit rows that carry before/after values. */
function beforeAfter(e: TimelineEntry): string {
  if (!e.before_value || !e.after_value) return ''
  try {
    const parse = (v: unknown) => (typeof v === 'string' ? JSON.parse(v) : v) as Record<string, unknown>
    const fmt = (o: Record<string, unknown>) => Object.entries(o).map(([k, v]) => `${k}=${v}`).join(', ')
    const b = fmt(parse(e.before_value)); const a = fmt(parse(e.after_value))
    return b && a ? ` (${b} → ${a})` : ''
  } catch { return '' }
}

function TimelineRow({ e }: { e: TimelineEntry }) {
  // trigger_events rows have no actor — show the trigger type instead.
  const actor = e.actor || (e.source === 'trigger_events' ? (e.trigger_type || 'trigger') : 'system')
  return (
    <li className="relative border-l-2 pb-4 pl-4 last:pb-0">
      <span className="absolute -left-[5px] top-1 h-2 w-2 rounded-full bg-border" />
      <p className="text-xs text-muted-foreground">{fmtWhen(e.created_at)}</p>
      <p className="mt-0.5 text-sm">
        <span className="font-medium">{actor}</span>{' '}
        {ACTION_LABELS[e.action] ?? e.action}{beforeAfter(e)}
      </p>
      {e.reason && <p className="mt-0.5 text-xs italic text-muted-foreground">{e.reason}</p>}
      {e.workflow_id && <p className="mt-0.5 text-xs text-muted-foreground">workflow: <code>{e.workflow_id}</code></p>}
    </li>
  )
}

export default function HintDrawer({ id, onChanged, onClose }: {
  id: number
  onChanged: () => void
  onClose: () => void
}) {
  const { user } = useAuth()
  const { data, loading, error, reload } = useFetch<HintDetailResp>(`/api/learning/hints/${id}`)
  const [actErr, setActErr] = useState('')
  const [busy, setBusy] = useState(false)

  // Edit form state, seeded from the loaded hint
  const [scope, setScope] = useState('domain')
  const [domain, setDomain] = useState('')
  const [url, setUrl] = useState('')
  const [category, setCategory] = useState('uncategorized')
  const [failureCat, setFailureCat] = useState('')

  const h = data?.hint
  useEffect(() => {
    if (!h) return
    setScope(h.scope || 'domain')
    setDomain(h.domain || '')
    setUrl(h.url || '')
    setCategory(h.category || 'uncategorized')
    setFailureCat(h.original_failure_category || '')
  }, [h])

  const actor = user?.email || 'admin'

  async function call(method: 'POST' | 'PATCH', path: string, body: object) {
    setBusy(true); setActErr('')
    try {
      await api(path, { method, body: JSON.stringify(body) })
      await reload()
      onChanged()
    } catch (e) {
      setActErr(e instanceof Error ? e.message : 'Action failed')
    } finally {
      setBusy(false)
    }
  }

  const saveEdit = () => call('PATCH', `/api/learning/hints/${id}`, {
    scope,
    domain: scope === 'domain' ? domain || null : null,
    url: scope === 'url' ? url || null : null,
    category,
    original_failure_category: failureCat || null,
    actor,
    reason: 'edited via admin dashboard',
  })
  const lifecycle = (action: 'unflag' | 'retract' | 'reactivate') => {
    if (action === 'retract' && !window.confirm('Retract this hint? It will stop injecting into agent prompts.')) return
    call('POST', `/api/learning/hints/${id}/${action}`, { actor, reason: 'via admin dashboard' })
  }

  const status = !h ? null
    : h.is_active === 1 && h.conflict_flagged === 1 ? { label: 'Flagged', cls: 'bg-amber-100 text-amber-700 border-amber-200' }
    : h.is_active === 1 ? { label: 'Active', cls: 'bg-green-100 text-green-700 border-green-200' }
    : h.llm_review_disabled === 1 ? { label: 'LLM-disabled', cls: 'bg-red-100 text-red-700 border-red-200' }
    : { label: 'Disabled', cls: 'bg-muted text-muted-foreground' }

  const selectCls = 'h-8 w-full rounded-md border border-input bg-background px-2 text-sm'

  return (
    <Sheet open onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Hint #{id}</SheetTitle>
          <SheetDescription>Full detail, edit, and audit history</SheetDescription>
        </SheetHeader>
        {loading && <p className="mt-4 text-sm text-muted-foreground">Loading…</p>}
        {error && <p className="mt-4 text-sm text-destructive">{error}</p>}
        {h && status && (
          <div className="mt-4 space-y-5 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge className={cn('text-xs', status.cls)}>{status.label}</Badge>
              {h.conflict_flag_reason && (
                <span className="text-xs text-amber-700">{h.conflict_flag_reason}</span>
              )}
            </div>

            <section>
              <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Hint text</h3>
              <p className="rounded-md bg-muted/40 px-3 py-2 text-sm leading-relaxed">{h.feedback_text}</p>
            </section>

            {h.anchor_query && (
              <section>
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Anchored to (similarity match)</h3>
                <p className="rounded-md bg-muted/40 px-3 py-2 text-xs leading-relaxed">{h.anchor_query}</p>
              </section>
            )}

            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Metadata</h3>
              <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs">
                <dt className="text-muted-foreground">Scope</dt>
                <dd className="text-right font-mono">{[h.scope, h.domain, h.url].filter(Boolean).join(' / ') || '—'}</dd>
                <dt className="text-muted-foreground">Category</dt>
                <dd className="text-right font-mono">{h.category || '—'}</dd>
                <dt className="text-muted-foreground">Failure category</dt>
                <dd className="text-right font-mono">{h.original_failure_category || '—'}</dd>
                <dt className="text-muted-foreground">Created via</dt>
                <dd className="text-right font-mono">{h.created_via || '—'}</dd>
                <dt className="text-muted-foreground">Created</dt>
                <dd className="text-right font-mono">{fmtWhen(h.created_at)}</dd>
                <dt className="text-muted-foreground">Last seen</dt>
                <dd className="text-right font-mono">{fmtWhen(h.last_seen)}</dd>
                <dt className="text-muted-foreground">Applied / Success / Fail</dt>
                <dd className="text-right font-mono">{h.applied_count ?? 0} / {h.success_count ?? 0} / {h.failure_count ?? 0}</dd>
                <dt className="text-muted-foreground">Evidence count</dt>
                <dd className="text-right font-mono">{h.evidence_count ?? 0}</dd>
              </dl>
            </section>

            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Edit</h3>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <label className="mb-1 block text-xs text-muted-foreground">Scope</label>
                  <select className={selectCls} value={scope} onChange={e => setScope(e.target.value)}>
                    <option value="url">url</option>
                    <option value="domain">domain</option>
                    <option value="global">global</option>
                  </select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-muted-foreground">Category</label>
                  <select className={selectCls} value={category} onChange={e => setCategory(e.target.value)}>
                    {HINT_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                {scope === 'domain' && (
                  <div className="col-span-2">
                    <label className="mb-1 block text-xs text-muted-foreground">Domain</label>
                    <Input className="h-8 text-sm" value={domain} onChange={e => setDomain(e.target.value)} placeholder="example.com" />
                  </div>
                )}
                {scope === 'url' && (
                  <div className="col-span-2">
                    <label className="mb-1 block text-xs text-muted-foreground">URL</label>
                    <Input className="h-8 text-sm" value={url} onChange={e => setUrl(e.target.value)} placeholder="https://example.com/page" />
                  </div>
                )}
                <div>
                  <label className="mb-1 block text-xs text-muted-foreground">Original failure category</label>
                  <select className={selectCls} value={failureCat} onChange={e => setFailureCat(e.target.value)}>
                    <option value="">— none —</option>
                    {FAILURE_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
                  </select>
                </div>
                <div className="flex items-end">
                  <Button size="sm" className="h-8 text-xs" disabled={busy} onClick={saveEdit}>Save</Button>
                </div>
              </div>
            </section>

            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Actions</h3>
              <div className="flex gap-2">
                {h.is_active === 1 && h.conflict_flagged === 1 && (
                  <Button size="sm" variant="outline" className="h-8 text-xs" disabled={busy} onClick={() => lifecycle('unflag')}>Unflag</Button>
                )}
                {h.is_active === 1 && (
                  <Button size="sm" variant="outline" className="h-8 text-xs text-destructive" disabled={busy} onClick={() => lifecycle('retract')}>Retract</Button>
                )}
                {h.is_active === 0 && (
                  <Button size="sm" variant="outline" className="h-8 text-xs" disabled={busy} onClick={() => lifecycle('reactivate')}>Reactivate</Button>
                )}
              </div>
            </section>

            {actErr && <p className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">{actErr}</p>}

            <section>
              <h3 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Audit timeline</h3>
              {data!.timeline.length === 0
                ? <p className="text-xs text-muted-foreground">No audit events yet.</p>
                : <ul>{data!.timeline.map(e => <TimelineRow key={`${e.source}-${e.id}-${e.created_at}`} e={e} />)}</ul>}
            </section>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
