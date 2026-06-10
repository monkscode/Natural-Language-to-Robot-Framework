/**
 * Shared shapes for the Learning admin pages (only the fields we render).
 * Backed by /api/learning/* (src/backend/api/learning_endpoints.py).
 */

export interface Hint {
  id: number
  feedback_text: string
  anchor_query?: string | null
  scope?: string
  domain?: string | null
  url?: string | null
  category?: string | null
  original_failure_category?: string | null
  created_via?: string
  created_at?: string
  last_seen?: string
  evidence_count?: number
  is_active: number
  conflict_flagged: number
  conflict_flag_reason?: string | null
  llm_review_disabled?: number
  success_count?: number
  failure_count?: number
  applied_count?: number
}

export interface HintsResp { total: number; hints: Hint[] }

export interface TimelineEntry {
  source: 'hint_audit' | 'trigger_events'
  id: number
  action: string
  actor: string | null
  reason: string | null
  before_value?: string | null
  after_value?: string | null
  created_at: string
  trigger_type?: string | null
  workflow_id?: string | null
}

export interface HintDetailResp { hint: Hint; timeline: TimelineEntry[] }

export interface TriggerEvent {
  id: number
  trigger_type: string
  workflow_id?: string | null
  domain?: string | null
  url?: string | null
  feedback_text?: string | null
  active_hint_ids?: number[] | null
  flagged_hint_ids?: number[] | null
  actually_flagged_hint_ids?: number[] | null
  used_hint_ids?: number[] | null
  unused_hint_ids?: number[] | null
  reason?: string | null
  llm_model?: string | null
  input_tokens?: number | null
  output_tokens?: number | null
  llm_latency_ms?: number | null
  status: string
  error_message?: string | null
  created_at: string
}

export interface TriggersResp { total: number; triggers: TriggerEvent[] }

export interface TriggerDetailResp {
  trigger: TriggerEvent
  execution: {
    robot_code?: string | null
    working_code?: string | null
    user_query?: string | null
    test_status?: string | null
    timestamp?: string | null
  } | null
  hint_texts: Record<string, string>
}

export interface CatStats { total: number; passed: number; pass_rate: number }

export interface Stats {
  hint_inventory?: {
    active: number; flagged: number; retracted: number
    llm_review_disabled: number; auto_disabled: number
    admin_created: number; workflow_created: number
  }
  trigger_activity?: { trigger_type: string; fired: number; succeeded: number; flagged_events: number }[]
  llm_accuracy?: {
    flagged_events: number; reviewed_events: number; reversed_events: number
    pending_review: number; engagement_rate: number | null
    reversal_rate: number | null; threshold_applicable: boolean
  }
  manual_actions?: { unflags_30d: number; retracts_30d: number }
  llm_cost?: {
    by_model: { llm_model: string; input_tokens: number; output_tokens: number; avg_latency_ms: number; estimated_cost_usd: number }[]
    total_estimated_usd: number
  }
  learning_effectiveness?: {
    natural_comparison?: {
      no_hints_available: CatStats
      hints_injected: CatStats
      holdout_suppressed: CatStats
      lift: number
      lift_is_biased: boolean
      honest_lift: number | null
      sufficient_data: boolean
    }
    retry_after_feedback?: CatStats
    hint_accuracy?: number
    cost_per_successful_test?: number
    total_executions?: number
  }
  attribution_health?: {
    events_by_status: Record<string, number>
    events_total: number
    credited_events: number
    credited_nothing_recently: boolean
    retirement_reversal_rate: number | null
    retired_never_used_total: number
    holdout_lift: number | null
  }
  review_candidates?: { hint_id: number; failure_associations: number }[]
  never_attributed?: { id: number; feedback_text: string; scope: string; domain: string | null; injections: number }[]
}

export const TRIGGER_TYPE_LABELS: Record<string, string> = {
  trigger_1: 'Fix-after-fail (Case B)',
  trigger_2: 'Feedback conflict',
  usage_attribution: 'Usage attribution',
}

export const HINT_CATEGORIES = ['structural', 'B1', 'B2', 'C1', 'D1', 'uncategorized'] as const
export const FAILURE_CATEGORIES = ['B1', 'B2', 'C1', 'D1'] as const

export const pctOrDash = (n: number | null | undefined, digits = 1) =>
  n == null ? '—' : `${(n * 100).toFixed(digits)}%`

export const fmtWhen = (iso: string | null | undefined) =>
  iso ? new Date(iso).toLocaleString() : '—'
