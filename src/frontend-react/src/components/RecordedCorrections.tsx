/**
 * Read-only list of the corrections a run has already contributed to the
 * learning store — GET /api/feedback/{run_id}'s `corrections` array,
 * rendered. Two consumers: GeneratePage's FeedbackPanel (while the run is
 * still on screen) and HistoryPage's drawer (the same run, read days
 * later). Presentational only — no fetch, no Retract control, no error
 * slot, no props to configure a control — because only the panel offers
 * Retract, and only for the caller's own hint on the live run.
 *
 * The panel does NOT render through this component: it also draws the
 * Retract button, a per-row retract error and the client-only `retracted`
 * marker (this session's own click, which this component must never know
 * about). What this module owns instead is the two strings that must not
 * drift between the panel's markup and this one — the heading and the
 * switched-off marker — exported so GeneratePage.tsx imports them rather
 * than keeping its own copy.
 */

/** Heading above the list. Exported so GeneratePage's FeedbackPanel renders
    the identical string instead of a second hand-typed copy. */
export const ALREADY_RECORDED_HEADING = 'Already recorded for this run'

/** `is_active` going to 0 has four possible causes — a user's own retract,
    an org admin's, auto-disable, or an LLM review — and the server does not
    say which, so the marker names the STATE, never an actor. Exported for
    the same reason as the heading above. */
export const SWITCHED_OFF_MARKER = '— switched off'

/** The subset of a correction row this read-only view needs. A structural
    subtype of GeneratePage's PanelCorrection: this component takes whatever
    shape supplies these three fields, whether that is the panel's
    session-augmented rows or the drawer's plain server response. `active` is
    optional for the same reason it is on the server response — an older
    backend, or the learning-disabled shape, never sends it, and absence
    must render exactly like `active: true` (no marker), never like
    switched off. */
export interface RecordedCorrectionItem {
  hint_id: number
  feedback_text: string
  active?: boolean
}

/** Renders nothing for an empty list — silence claims nothing, the same
    degrade-to-silence the panel already uses for a run that contributed
    nothing (or a corrections read that failed). */
export function RecordedCorrections({ corrections }: { corrections: RecordedCorrectionItem[] }) {
  if (corrections.length === 0) return null
  return (
    <div className="rounded-md border bg-muted/40 px-3 py-2 text-xs">
      <div className="font-medium">{ALREADY_RECORDED_HEADING}</div>
      <ul className="mt-1 space-y-0.5 text-muted-foreground">
        {corrections.map(c => (
          <li key={c.hint_id}>
            “{c.feedback_text}”
            {c.active === false && (
              <span className="ml-1.5 italic">{SWITCHED_OFF_MARKER}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  )
}
