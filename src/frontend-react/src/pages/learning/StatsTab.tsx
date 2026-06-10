/**
 * Stats tab — hint inventory, learning effectiveness (Cat A/B/C lift),
 * usage-attribution health, trigger activity, and conflict-detection cost.
 * GET /api/learning/stats
 */

import { Card, CardContent } from '@/components/ui/card'
import { useFetch } from '@/lib/useFetch'
import { cn } from '@/lib/utils'
import { TRIGGER_TYPE_LABELS, pctOrDash } from './types'
import type { Stats, CatStats } from './types'

function Num({ label, value, sub }: { label: string; value: number | string; sub?: string }) {
  return (
    <div>
      <p className="text-xs font-medium text-muted-foreground">{label}</p>
      <p className="mt-1 text-2xl font-bold tracking-tight tabular-nums">{value}</p>
      {sub && <p className="mt-0.5 text-xs text-muted-foreground">{sub}</p>}
    </div>
  )
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <Card>
      <CardContent className="p-5">
        <h2 className="text-sm font-semibold">{title}</h2>
        {hint && <p className="mt-0.5 text-xs text-muted-foreground">{hint}</p>}
        <div className="mt-4">{children}</div>
      </CardContent>
    </Card>
  )
}

function CatBlock({ label, cat }: { label: string; cat?: CatStats }) {
  return (
    <Num
      label={label}
      value={cat?.total ?? 0}
      sub={cat && cat.total > 0 ? `${pctOrDash(cat.pass_rate)} pass rate` : 'no runs yet'}
    />
  )
}

export default function StatsTab() {
  const { data, loading, error } = useFetch<Stats>('/api/learning/stats')
  if (loading) return <p className="text-sm text-muted-foreground">Loading…</p>
  if (error) return <p className="rounded-md border border-destructive/30 bg-destructive/5 px-4 py-2 text-sm text-destructive">{error}</p>
  if (!data) return null

  const inv = data.hint_inventory
  const nc = data.learning_effectiveness?.natural_comparison
  const att = data.attribution_health
  const cost = data.llm_cost

  return (
    <div className="space-y-3">
      <Section title="Hint inventory" hint="Point-in-time counts across all learned hints">
        <div className="grid grid-cols-3 gap-4 md:grid-cols-7">
          <Num label="Active" value={inv?.active ?? 0} />
          <Num label="Flagged" value={inv?.flagged ?? 0} />
          <Num label="Auto-disabled" value={inv?.auto_disabled ?? 0} />
          <Num label="LLM-disabled" value={inv?.llm_review_disabled ?? 0} />
          <Num label="Retracted" value={inv?.retracted ?? 0} />
          <Num label="Admin-created" value={inv?.admin_created ?? 0} />
          <Num label="Workflow-created" value={inv?.workflow_created ?? 0} />
        </div>
      </Section>

      <Section
        title="Learning effectiveness — is it helping?"
        hint="Compares pass rates of runs without hints (A), with hints injected (B), and the unbiased holdout control (C)"
      >
        <div className="grid grid-cols-3 gap-4">
          <CatBlock label="Cat A — no hints" cat={nc?.no_hints_available} />
          <CatBlock label="Cat B — hints injected" cat={nc?.hints_injected} />
          <CatBlock label="Cat C — holdout" cat={nc?.holdout_suppressed} />
        </div>
        <div className="mt-4 space-y-1.5 border-l-2 pl-3 text-sm">
          <p>
            <span className="font-medium">Biased lift (B − A): </span>
            <span className="tabular-nums">{nc ? pctOrDash(nc.lift) : '—'}</span>
            <span className="text-xs text-muted-foreground"> — Cat B resembles past successes; flattering, not trusted</span>
          </p>
          <p>
            <span className="font-medium">Honest lift (B − C): </span>
            <span className="tabular-nums">{nc?.honest_lift != null ? pctOrDash(nc.honest_lift) : 'insufficient data'}</span>
            <span className="text-xs text-muted-foreground"> — the unbiased control; the trustworthy number</span>
          </p>
          <p>
            <span className="font-medium">Sufficient data: </span>
            {nc?.sufficient_data ? 'yes' : 'no'}
          </p>
        </div>
      </Section>

      <Section
        title="Usage attribution — is crediting trustworthy?"
        hint="Whether passing runs credit the hints that actually helped"
      >
        <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
          <Num
            label="Attribution events"
            value={att?.events_total ?? 0}
            sub={`${att?.credited_events ?? 0} credited ≥1 hint`}
          />
          <Num
            label="Retirement reversal"
            value={att?.retirement_reversal_rate != null ? pctOrDash(att.retirement_reversal_rate) : 'n/a'}
            sub={`${att?.retired_never_used_total ?? 0} retired (never-used)`}
          />
          <Num
            label="Holdout lift (B − C)"
            value={att?.holdout_lift != null ? pctOrDash(att.holdout_lift) : 'n/a'}
          />
          <Num
            label="Cost / successful test"
            value={`$${(data.learning_effectiveness?.cost_per_successful_test ?? 0).toFixed(4)}`}
            sub={`${data.learning_effectiveness?.total_executions ?? 0} executions`}
          />
        </div>
        {att?.credited_nothing_recently && (
          <p className="mt-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
            Attribution ran in the last 30 days but credited nothing — worth a look.
          </p>
        )}
      </Section>

      <Section title="Trigger activity (30d)" hint="How often each learning trigger fired and flagged hints">
        {(data.trigger_activity?.length ?? 0) === 0 ? (
          <p className="text-sm text-muted-foreground">No trigger activity in the last 30 days.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="py-2 pr-4">Trigger</th>
                <th className="py-2 pr-4">Fired</th>
                <th className="py-2 pr-4">Succeeded</th>
                <th className="py-2">Flagged events</th>
              </tr>
            </thead>
            <tbody>
              {data.trigger_activity!.map(t => (
                <tr key={t.trigger_type} className="border-b last:border-0">
                  <td className="py-2 pr-4">{TRIGGER_TYPE_LABELS[t.trigger_type] ?? t.trigger_type}</td>
                  <td className="py-2 pr-4 tabular-nums">{t.fired}</td>
                  <td className="py-2 pr-4 tabular-nums">{t.succeeded}</td>
                  <td className="py-2 tabular-nums">{t.flagged_events}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section
        title="Conflict-detection cost (30d)"
        hint={`Total estimated: $${(cost?.total_estimated_usd ?? 0).toFixed(4)}`}
      >
        {(cost?.by_model?.length ?? 0) === 0 ? (
          <p className="text-sm text-muted-foreground">No LLM trigger calls in the last 30 days.</p>
        ) : (
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b text-left text-xs uppercase tracking-wider text-muted-foreground">
                <th className="py-2 pr-4">Model</th>
                <th className="py-2 pr-4">Tokens in</th>
                <th className="py-2 pr-4">Tokens out</th>
                <th className="py-2 pr-4">Avg latency</th>
                <th className="py-2">Est. cost</th>
              </tr>
            </thead>
            <tbody>
              {cost!.by_model.map(m => (
                <tr key={m.llm_model} className="border-b last:border-0">
                  <td className="py-2 pr-4">{m.llm_model}</td>
                  <td className="py-2 pr-4 tabular-nums">{(m.input_tokens ?? 0).toLocaleString()}</td>
                  <td className="py-2 pr-4 tabular-nums">{(m.output_tokens ?? 0).toLocaleString()}</td>
                  <td className="py-2 pr-4 tabular-nums">{m.avg_latency_ms != null ? `${Math.round(m.avg_latency_ms)} ms` : '—'}</td>
                  <td className="py-2 tabular-nums">${(m.estimated_cost_usd ?? 0).toFixed(4)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      {(data.review_candidates?.length ?? 0) > 0 && (
        <Section title="Review candidates" hint="Active hints that never succeeded and associate with related failures — worth a manual look">
          <ul className="space-y-1 text-sm">
            {data.review_candidates!.map(c => (
              <li key={c.hint_id} className="flex items-center gap-2">
                <span className={cn('font-medium tabular-nums')}>Hint #{c.hint_id}</span>
                <span className="text-xs text-muted-foreground">{c.failure_associations} related failure{c.failure_associations !== 1 ? 's' : ''}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      {(data.never_attributed?.length ?? 0) > 0 && (
        <Section title="Never attributed" hint="Hints injected repeatedly but never once credited on a pass — candidates for retirement">
          <ul className="space-y-1.5 text-sm">
            {data.never_attributed!.map(h => (
              <li key={h.id} className="rounded-md border px-3 py-2 text-xs">
                <span className="font-semibold text-muted-foreground">#{h.id}</span>{' '}
                <span className="text-muted-foreground">({h.injections} injections)</span>
                <span className="mt-0.5 block line-clamp-2">{h.feedback_text}</span>
              </li>
            ))}
          </ul>
        </Section>
      )}
    </div>
  )
}
