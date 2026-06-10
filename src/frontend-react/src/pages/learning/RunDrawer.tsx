/**
 * Run detail drawer — one test-case run's full learning journey:
 * the execution record, hint selection→injection→attribution funnel,
 * trigger events, and the generated Robot code.
 * GET /api/learning/runs/{workflow_id}
 */

import { Badge } from '@/components/ui/badge'
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from '@/components/ui/sheet'
import { useFetch } from '@/lib/useFetch'
import { cn } from '@/lib/utils'
import { TRIGGER_TYPE_LABELS, fmtWhen } from './types'

interface FunnelRow {
  hint_id: number
  scope?: string | null
  source?: string | null
  priority?: string | null
  similarity_score?: number | null
  available: number
  injected: number
  drop_reason?: string | null
  attribution_bucket?: string | null
  attribution_reason?: string | null
  feedback_text?: string | null
  is_active?: number | null
  conflict_flagged?: number | null
}

interface RunDetail {
  run: {
    workflow_id: string
    timestamp?: string
    user_query?: string
    url?: string | null
    domain?: string | null
    test_status?: string
    failure_category?: string | null
    robot_code?: string | null
    working_code?: string | null
  } | null
  metrics: {
    hints_injected?: number
    test_passed?: number
    is_first_attempt?: number
    was_holdout?: number
  } | null
  trace: FunnelRow[]
  triggers: { id: number; trigger_type: string; status: string; created_at: string; reason?: string | null }[]
}

const BUCKET_BADGES: Record<string, string> = {
  success: 'bg-green-100 text-green-700 border-green-200',
  failure: 'bg-red-100 text-red-700 border-red-200',
  unused: 'bg-muted text-muted-foreground',
}

export default function RunDrawer({ workflowId, onClose }: { workflowId: string; onClose: () => void }) {
  const { data, loading, error } = useFetch<RunDetail>(`/api/learning/runs/${workflowId}`)
  const run = data?.run

  return (
    <Sheet open onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent className="w-full overflow-y-auto sm:max-w-2xl">
        <SheetHeader>
          <SheetTitle>Run detail</SheetTitle>
          <SheetDescription><code className="text-xs">{workflowId}</code></SheetDescription>
        </SheetHeader>
        {loading && <p className="mt-4 text-sm text-muted-foreground">Loading…</p>}
        {error && <p className="mt-4 text-sm text-destructive">{error}</p>}
        {data && (
          <div className="mt-4 space-y-5 text-sm">
            {run && (
              <section className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge className={cn('text-xs',
                    run.test_status === 'passed' ? 'bg-green-100 text-green-700 border-green-200'
                      : run.test_status === 'failed' ? 'bg-red-100 text-red-700 border-red-200'
                        : 'bg-muted text-muted-foreground')}>
                    {run.test_status || 'unknown'}
                  </Badge>
                  {run.failure_category && <Badge className="bg-amber-100 text-amber-700 border-amber-200 text-xs">{run.failure_category}</Badge>}
                  {data.metrics?.was_holdout === 1 && <Badge className="bg-blue-100 text-blue-700 border-blue-200 text-xs">holdout</Badge>}
                  <span className="text-xs text-muted-foreground">{fmtWhen(run.timestamp)}</span>
                </div>
                <p className="rounded-md bg-muted/40 px-3 py-2 text-xs leading-relaxed">{run.user_query || '(paste-and-execute — no query)'}</p>
                {(run.domain || run.url) && (
                  <p className="text-xs text-muted-foreground">{run.url || run.domain}</p>
                )}
              </section>
            )}

            <section>
              <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                Hint funnel ({data.trace.length})
              </h3>
              {data.trace.length === 0 ? (
                <p className="text-xs text-muted-foreground">No hints were considered for this run.</p>
              ) : (
                <ul className="space-y-1.5">
                  {data.trace.map(t => (
                    <li key={t.hint_id} className="rounded-md border px-3 py-2 text-xs">
                      <div className="mb-1 flex flex-wrap items-center gap-1.5">
                        <span className="font-semibold text-muted-foreground">#{t.hint_id}</span>
                        {t.scope && <span className="text-muted-foreground">{t.scope}</span>}
                        {t.similarity_score != null && (
                          <span className="text-muted-foreground">sim {(t.similarity_score * 100).toFixed(0)}%</span>
                        )}
                        <Badge className={cn('text-[10px]', t.injected === 1
                          ? 'bg-green-100 text-green-700 border-green-200'
                          : 'bg-muted text-muted-foreground')}>
                          {t.injected === 1 ? 'injected' : 'dropped'}
                        </Badge>
                        {t.drop_reason && <span className="text-muted-foreground">({t.drop_reason})</span>}
                        {t.attribution_bucket && (
                          <Badge className={cn('text-[10px]', BUCKET_BADGES[t.attribution_bucket] ?? 'bg-muted text-muted-foreground')}>
                            {t.attribution_bucket}
                          </Badge>
                        )}
                      </div>
                      <span className="line-clamp-2">{t.feedback_text ?? '(hint deleted)'}</span>
                      {t.attribution_reason && (
                        <p className="mt-1 italic text-muted-foreground">{t.attribution_reason}</p>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>

            {data.triggers.length > 0 && (
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  Trigger events ({data.triggers.length})
                </h3>
                <ul className="space-y-1.5">
                  {data.triggers.map(t => (
                    <li key={t.id} className="rounded-md border px-3 py-2 text-xs">
                      <span className="font-medium">{TRIGGER_TYPE_LABELS[t.trigger_type] ?? t.trigger_type}</span>
                      <span className="text-muted-foreground"> · {t.status} · {fmtWhen(t.created_at)}</span>
                      {t.reason && <p className="mt-1 line-clamp-3 text-muted-foreground">{t.reason}</p>}
                    </li>
                  ))}
                </ul>
              </section>
            )}

            {(run?.working_code || run?.robot_code) && (
              <section>
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
                  {run.working_code ? 'Working code' : 'Generated code'}
                </h3>
                <pre className="max-h-72 overflow-auto rounded-md bg-[#0d1117] p-3 text-[11px] leading-relaxed text-[#e6edf3]">
                  {run.working_code || run.robot_code}
                </pre>
              </section>
            )}
          </div>
        )}
      </SheetContent>
    </Sheet>
  )
}
