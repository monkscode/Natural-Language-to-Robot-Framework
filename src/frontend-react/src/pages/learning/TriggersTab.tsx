/**
 * Triggers tab — learning trigger events (fix-after-fail, feedback conflict,
 * usage attribution) with a detail drawer per event.
 * GET /api/learning/triggers, GET /api/learning/triggers/{id}
 */

import { useState } from 'react'
import { Card, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet'
import { useFetch } from '@/lib/useFetch'
import { cn } from '@/lib/utils'
import { TRIGGER_TYPE_LABELS, fmtWhen } from './types'
import type { TriggersResp, TriggerDetailResp } from './types'

const TYPE_FILTERS = [
  { key: '', label: 'All types' },
  { key: 'trigger_1', label: 'Fix-after-fail' },
  { key: 'trigger_2', label: 'Feedback conflict' },
  { key: 'usage_attribution', label: 'Usage attribution' },
] as const

function statusBadge(s: string): string {
  if (s === 'succeeded') return 'bg-green-100 text-green-700 border-green-200'
  if (s === 'failed' || s === 'error') return 'bg-red-100 text-red-700 border-red-200'
  return 'bg-muted text-muted-foreground'
}

function TriggerDrawer({ id, onClose }: { id: number; onClose: () => void }) {
  const { data, loading, error } = useFetch<TriggerDetailResp>(`/api/learning/triggers/${id}`)
  const t = data?.trigger
  return (
    <Sheet open onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-xl">
        <SheetHeader>
          <SheetTitle>Trigger event #{id}</SheetTitle>
          <SheetDescription>
            {t ? `${TRIGGER_TYPE_LABELS[t.trigger_type] ?? t.trigger_type} · ${fmtWhen(t.created_at)}` : ''}
          </SheetDescription>
        </SheetHeader>
        {loading && <p className="mt-4 text-sm text-muted-foreground">Loading…</p>}
        {error && <p className="mt-4 text-sm text-destructive">{error}</p>}
        {t && (
          <div className="mt-4 space-y-4 text-sm">
            <div className="flex flex-wrap items-center gap-2">
              <Badge className={cn('text-xs', statusBadge(t.status))}>{t.status}</Badge>
              {t.domain && <span className="text-xs text-muted-foreground">{t.domain}</span>}
              {t.workflow_id && <code className="text-xs text-muted-foreground">{t.workflow_id}</code>}
            </div>

            {t.reason && (
              <section>
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">LLM reasoning</h3>
                <p className="rounded-md bg-muted/40 px-3 py-2 text-xs leading-relaxed">{t.reason}</p>
              </section>
            )}
            {t.error_message && (
              <section>
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Error</h3>
                <p className="rounded-md border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive">{t.error_message}</p>
              </section>
            )}

            {data && Object.keys(data.hint_texts).length > 0 && (
              <section>
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Hints in play ({Object.keys(data.hint_texts).length})
                </h3>
                <ul className="space-y-1.5">
                  {Object.entries(data.hint_texts).map(([hid, text]) => {
                    const flagged = (t.actually_flagged_hint_ids ?? t.flagged_hint_ids ?? []).includes(Number(hid))
                    const used = (t.used_hint_ids ?? []).includes(Number(hid))
                    return (
                      <li key={hid} className="rounded-md border px-3 py-2 text-xs">
                        <div className="mb-1 flex items-center gap-1.5">
                          <span className="font-semibold text-muted-foreground">#{hid}</span>
                          {flagged && <Badge className="bg-amber-100 text-amber-700 border-amber-200 text-[10px]">flagged</Badge>}
                          {used && <Badge className="bg-green-100 text-green-700 border-green-200 text-[10px]">credited</Badge>}
                        </div>
                        <span className="line-clamp-3">{text}</span>
                      </li>
                    )
                  })}
                </ul>
              </section>
            )}

            {data?.execution && (
              <section>
                <h3 className="mb-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">Workflow</h3>
                <div className="space-y-1 rounded-md border px-3 py-2 text-xs">
                  <p><span className="text-muted-foreground">Query: </span>{data.execution.user_query || '—'}</p>
                  <p><span className="text-muted-foreground">Result: </span>{data.execution.test_status || '—'}</p>
                  <p><span className="text-muted-foreground">When: </span>{fmtWhen(data.execution.timestamp)}</p>
                </div>
              </section>
            )}

            <section className="grid grid-cols-2 gap-2 text-xs">
              <div className="rounded-md border px-3 py-2">
                <p className="text-muted-foreground">Model</p>
                <p className="mt-0.5 font-medium">{t.llm_model || '—'}</p>
              </div>
              <div className="rounded-md border px-3 py-2">
                <p className="text-muted-foreground">Latency</p>
                <p className="mt-0.5 font-medium">{t.llm_latency_ms != null ? `${t.llm_latency_ms} ms` : '—'}</p>
              </div>
              <div className="rounded-md border px-3 py-2">
                <p className="text-muted-foreground">Tokens in → out</p>
                <p className="mt-0.5 font-medium tabular-nums">
                  {t.input_tokens != null ? t.input_tokens.toLocaleString() : '—'} → {t.output_tokens != null ? t.output_tokens.toLocaleString() : '—'}
                </p>
              </div>
              <div className="rounded-md border px-3 py-2">
                <p className="text-muted-foreground">When</p>
                <p className="mt-0.5 font-medium">{fmtWhen(t.created_at)}</p>
              </div>
            </section>
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}

export default function TriggersTab() {
  const [type, setType] = useState('')
  const [since, setSince] = useState('30d')
  const [openId, setOpenId] = useState<number | null>(null)
  const path = `/api/learning/triggers?limit=50${type ? `&trigger_type=${type}` : ''}${since ? `&since=${since}` : ''}`
  const { data, loading, error } = useFetch<TriggersResp>(path)

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5">
        {TYPE_FILTERS.map(f => (
          <Button key={f.key} size="sm" variant={type === f.key ? 'default' : 'outline'} className="h-7 text-xs" onClick={() => setType(f.key)}>
            {f.label}
          </Button>
        ))}
        <select
          className="h-7 rounded-md border border-input bg-background px-1.5 text-xs"
          value={since}
          onChange={e => setSince(e.target.value)}
        >
          <option value="7d">Last 7 days</option>
          <option value="30d">Last 30 days</option>
          <option value="90d">Last 90 days</option>
          <option value="">All time</option>
        </select>
        <span className="ml-auto self-center text-xs text-muted-foreground">
          {data ? `${data.total} event${data.total !== 1 ? 's' : ''}` : ''}
        </span>
      </div>
      {error && <p className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">{error}</p>}
      <Card>
        <CardContent className="p-0">
          {loading && !data && <p className="px-4 py-6 text-sm text-muted-foreground">Loading…</p>}
          {data && data.triggers.length === 0 && (
            <p className="px-4 py-6 text-sm text-muted-foreground">No trigger events recorded yet — they appear when the learning system reacts to runs.</p>
          )}
          {data && data.triggers.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b bg-muted/40 text-left text-xs uppercase tracking-wider text-muted-foreground">
                    <th className="px-4 py-2.5">When</th>
                    <th className="px-4 py-2.5">Type</th>
                    <th className="px-4 py-2.5 hidden md:table-cell">Domain</th>
                    <th className="px-4 py-2.5">Status</th>
                    <th className="px-4 py-2.5 hidden lg:table-cell">Tokens in → out</th>
                    <th className="px-4 py-2.5 hidden sm:table-cell">Latency</th>
                  </tr>
                </thead>
                <tbody>
                  {data.triggers.map(t => (
                    <tr
                      key={t.id}
                      className="cursor-pointer border-b last:border-0 hover:bg-muted/30"
                      onClick={() => setOpenId(t.id)}
                    >
                      <td className="px-4 py-2.5 whitespace-nowrap text-xs text-muted-foreground">{fmtWhen(t.created_at)}</td>
                      <td className="px-4 py-2.5">{TRIGGER_TYPE_LABELS[t.trigger_type] ?? t.trigger_type}</td>
                      <td className="px-4 py-2.5 hidden md:table-cell text-xs text-muted-foreground">{t.domain || '—'}</td>
                      <td className="px-4 py-2.5"><Badge className={cn('text-xs', statusBadge(t.status))}>{t.status}</Badge></td>
                      <td className="px-4 py-2.5 hidden lg:table-cell text-xs tabular-nums text-muted-foreground">
                        {t.input_tokens != null ? `${t.input_tokens.toLocaleString()} → ${(t.output_tokens ?? 0).toLocaleString()}` : '—'}
                      </td>
                      <td className="px-4 py-2.5 hidden sm:table-cell text-xs tabular-nums text-muted-foreground">
                        {t.llm_latency_ms != null ? `${t.llm_latency_ms} ms` : '—'}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
      {openId != null && <TriggerDrawer id={openId} onClose={() => setOpenId(null)} />}
    </div>
  )
}
