/**
 * Add feedback — admin creates a hint directly (POST /api/learning/hints).
 * Mirrors the legacy modal: text, anchor query, scope, category,
 * original failure category, optional auto-triage.
 */

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Textarea } from '@/components/ui/textarea'
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet'
import { api } from '@/lib/api'
import { useAuth } from '@/auth/AuthContext'
import { HINT_CATEGORIES, FAILURE_CATEGORIES } from './types'

export default function AddFeedbackSheet({ onCreated, onClose }: {
  onCreated: () => void
  onClose: () => void
}) {
  const { user } = useAuth()
  const [text, setText] = useState('')
  const [anchor, setAnchor] = useState('')
  const [scope, setScope] = useState('domain')
  const [domain, setDomain] = useState('')
  const [url, setUrl] = useState('')
  const [category, setCategory] = useState('uncategorized')
  const [failureCat, setFailureCat] = useState('')
  const [triage, setTriage] = useState(false)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const valid =
    text.trim().length > 0 &&
    anchor.trim().length >= 3 &&
    (scope !== 'domain' || domain.trim().length > 0) &&
    (scope !== 'url' || url.trim().length > 0)

  async function submit() {
    if (!valid || busy) return
    setBusy(true); setErr('')
    try {
      await api('/api/learning/hints', {
        method: 'POST',
        body: JSON.stringify({
          feedback_text: text.trim().slice(0, 500),
          anchor_query: anchor.trim().slice(0, 500),
          scope,
          domain: scope === 'domain' ? domain.trim() : null,
          url: scope === 'url' ? url.trim() : null,
          category,
          original_failure_category: failureCat || null,
          run_triage: triage,
          actor: user?.email || 'admin',
        }),
      })
      onCreated()
      onClose()
    } catch (e) {
      setErr(e instanceof Error ? e.message : 'Failed to add feedback')
    } finally {
      setBusy(false)
    }
  }

  const selectCls = 'h-8 w-full rounded-md border border-input bg-background px-2 text-sm'

  return (
    <Sheet open onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-lg">
        <SheetHeader>
          <SheetTitle>Add feedback</SheetTitle>
          <SheetDescription>Teach the generator a rule or correction directly</SheetDescription>
        </SheetHeader>
        <div className="mt-4 space-y-4 text-sm">
          <div>
            <label className="mb-1 block text-xs font-medium">Feedback text *</label>
            <Textarea
              value={text}
              maxLength={500}
              onChange={e => setText(e.target.value)}
              placeholder="Describe the rule or correction…"
              className="min-h-[90px] text-sm"
            />
            <p className="mt-1 text-right text-xs text-muted-foreground">{text.length} / 500</p>
          </div>

          <div>
            <label className="mb-1 block text-xs font-medium">Example user request this hint applies to *</label>
            <Textarea
              value={anchor}
              maxLength={500}
              onChange={e => setAnchor(e.target.value)}
              placeholder="e.g. verify the product list loads after applying a filter"
              className="min-h-[70px] text-sm"
            />
            <p className="mt-1 text-xs text-muted-foreground">
              The similarity filter matches future test queries against this. Required, 3–500 characters.
            </p>
          </div>

          <div>
            <label className="mb-1.5 block text-xs font-medium">Scope *</label>
            <div className="space-y-1.5">
              {[
                { v: 'url', label: 'This URL only' },
                { v: 'domain', label: 'This domain' },
                { v: 'global', label: 'Global (all sites)' },
              ].map(o => (
                <label key={o.v} className="flex items-center gap-2 text-sm">
                  <input type="radio" name="add-scope" value={o.v} checked={scope === o.v} onChange={() => setScope(o.v)} />
                  {o.label}
                </label>
              ))}
              {scope === 'domain' && (
                <Input className="ml-6 h-8 w-[calc(100%-1.5rem)] text-sm" value={domain} onChange={e => setDomain(e.target.value)} placeholder="example.com" />
              )}
              {scope === 'url' && (
                <Input className="ml-6 h-8 w-[calc(100%-1.5rem)] text-sm" value={url} onChange={e => setUrl(e.target.value)} placeholder="https://example.com/page" />
              )}
            </div>
          </div>

          <div className="grid grid-cols-2 gap-2">
            <div>
              <label className="mb-1 block text-xs font-medium">Category</label>
              <select className={selectCls} value={category} onChange={e => setCategory(e.target.value)}>
                {HINT_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium">Original failure category</label>
              <select className={selectCls} value={failureCat} onChange={e => setFailureCat(e.target.value)}>
                <option value="">— none —</option>
                {FAILURE_CATEGORIES.map(c => <option key={c} value={c}>{c}</option>)}
              </select>
            </div>
          </div>
          <p className="text-xs text-muted-foreground">Leaving the failure category blank disables failure-based auto-disable for this hint.</p>

          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={triage} onChange={e => setTriage(e.target.checked)} />
            Run automatic triage on this text (override category)
          </label>

          {err && <p className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">{err}</p>}

          <div className="flex justify-end gap-2 pt-1">
            <Button variant="ghost" size="sm" onClick={onClose} disabled={busy}>Cancel</Button>
            <Button size="sm" onClick={submit} disabled={!valid || busy}>
              {busy ? 'Adding…' : 'Add feedback'}
            </Button>
          </div>
        </div>
      </SheetContent>
    </Sheet>
  )
}
