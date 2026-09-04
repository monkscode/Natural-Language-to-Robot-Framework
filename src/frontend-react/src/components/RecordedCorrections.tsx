/**
 * Read-only list of the corrections a run has already contributed to the
 * learning store — GET /api/feedback/{run_id}'s `corrections` array,
 * rendered. Its one render consumer is HistoryPage's drawer, reading a run
 * days after the run itself (and GeneratePage's panel with it) is gone from
 * screen. Presentational only — no fetch, no Retract control, no error
 * slot, no props to configure a control — because the drawer offers no
 * action on these rows.
 *
 * GeneratePage's FeedbackPanel does NOT render through this component — it
 * keeps its own list markup, because it also draws the Retract button, a
 * per-row retract error and the client-only `retracted` marker (this
 * session's own click, which this component must never know about). What
 * the panel imports from here instead is the two strings that must not
 * drift between its markup and this one — the heading and the switched-off
 * marker, both exported below — so it keeps its own render but not a
 * second hand-typed copy of either string.
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
    supertype of GeneratePage's PanelCorrection: this component takes whatever
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
