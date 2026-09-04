/**
 * FeedbackPanel — what the panel is allowed to tell the user.
 *
 * The panel used to `await` the POST, throw the body away, and render
 * "Thanks — your feedback helps the system learn" unconditionally. Four of the
 * answers it thanked the user for had discarded the correction: no learning
 * record, learning paused behind an open circuit breaker, an internal error,
 * and learning switched off entirely.
 *
 * Two halves are pinned here, and the second is the one that bites: an answer
 * that tells the user to send it again must LEAVE them able to. Replacing the
 * textarea with the notice is advice the UI makes impossible to follow, and
 * that is exactly what the first cut of this change did.
 *
 * Imported from GeneratePage.tsx, which is where the panel is defined. The
 * page itself is not under test — nothing here renders GeneratePage, only the
 * panel, so no Router and no page state are involved.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

// Only `api` is stubbed. ApiError and isAccessLoss stay REAL: the retract
// path now branches on isAccessLoss(e), and a stubbed copy of that rule would
// test the stub instead of the rule lib/api.test.ts pins.
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))

import { api, ApiError } from '@/lib/api'
import { FeedbackPanel } from './GeneratePage'

const THANKS = 'Thanks — your feedback helps the system learn.'
const NO_RECORD =
  'We could not find the learning record this belongs to, so nothing was ' +
  'learned from it. Please send it again in a moment.'
const PAUSED =
  'Learning is paused right now, so this correction was not recorded. ' +
  'Please send it again later.'

const mockApi = vi.mocked(api)

// resetAllMocks, not clearAllMocks: clearAllMocks leaves a queued
// mockResolvedValueOnce behind (verified by execution), and Step 6 adds a
// fetch-on-mount to this panel, so an unconsumed value would start
// answering the NEXT test's first call.
afterEach(() => vi.resetAllMocks())

/** The corrections GET the panel fires on mount, answered per test.
    Routed on the request method rather than queued: the mount fetch is the
    panel's FIRST api call, so a bare mockResolvedValueOnce meant for the POST
    would be eaten by it. */
function onFile(corrections: unknown[] = []) {
  mockApi.mockImplementation(async (_path: string, opts?: { method?: string }) => {
    if (opts?.method === 'POST') throw new Error('no POST answer queued')
    return { status: 'success', applied_to: 'wf-1', corrections }
  })
}

/** POST calls only — the mount GET is not part of any submission count. */
const posts = () =>
  mockApi.mock.calls.filter(([, o]) => (o as { method?: string } | undefined)?.method === 'POST')

/** A failed run: the correction form is open from the start. */
function renderFailPanel() {
  render(<FeedbackPanel outcome="fail" workflowId="wf-1" />)
  return {
    box: screen.getByPlaceholderText(/it clicked the wrong button/),
    submit: screen.getByRole('button', { name: /Submit feedback/ }),
  }
}

/** Let the mount fetch settle before a test queues its POST answer. */
async function mounted() {
  await waitFor(() =>
    expect(mockApi).toHaveBeenCalledWith('/api/feedback/wf-1', { cache: 'no-store' }))
}

async function answerWith(body: unknown) {
  onFile()
  const { box, submit } = renderFailPanel()
  await mounted()
  mockApi.mockResolvedValueOnce(body)
  fireEvent.change(box, { target: { value: 'the search box locator was off' } })
  fireEvent.click(submit)
  await waitFor(() => expect(posts()).toHaveLength(1))
}

describe('a correction that reached the learning store', () => {
  it('retires the form and shows the sentence the backend sent', async () => {
    await answerWith({ status: 'success', outcome: 'processed', message: THANKS })

    await screen.findByText(THANKS)
    expect(screen.queryByPlaceholderText(/it clicked the wrong button/)).toBeNull()
  })
})

describe('an answer that says the correction did not land', () => {
  it.each([
    ['no_record', { status: 'success', outcome: 'no_record', message: NO_RECORD }, NO_RECORD],
    ['learning_paused', { status: 'success', outcome: 'learning_paused', message: PAUSED }, PAUSED],
    ['error', { status: 'error', outcome: 'error', message: 'Something went wrong, so this correction did not reach the learning store — please send it again.' }, /Something went wrong/],
    // Learning switched off answers with a message and no outcome at all. It
    // is not a success either, and it used to render as "Thanks".
    ['learning off', { status: 'disabled', message: 'Learning system is not enabled' }, 'Learning system is not enabled'],
    // An older backend deployed behind a newer SPA: no outcome, no message.
    // Not confirmed is not the same as recorded.
    ['a backend that confirms nothing', { status: 'success', triage: {} }, /did not confirm/],
  ])('%s: never renders as a recorded correction', async (_label, body, expected) => {
    await answerWith(body)

    await screen.findByText(expected as string | RegExp)
    expect(screen.queryByText(THANKS)).toBeNull()
    // The text alone is not the pin. Rendering any of these in the green
    // terminal state would still show the right words while claiming the
    // correction landed, so the surviving form IS the assertion.
    expect(screen.getByPlaceholderText(/it clicked the wrong button/)).toBeInTheDocument()
  })

  it('leaves the form, the typed text and Submit alive so the user can retry', async () => {
    await answerWith({ status: 'success', outcome: 'no_record', message: NO_RECORD })
    await screen.findByText(NO_RECORD)

    const box = screen.getByPlaceholderText(/it clicked the wrong button/)
    expect(box).toHaveValue('the search box locator was off')

    // The advice is "send it again" — so sending it again must reach the API.
    mockApi.mockResolvedValueOnce({ status: 'success', outcome: 'processed', message: THANKS })
    fireEvent.click(screen.getByRole('button', { name: /Submit feedback/ }))

    await waitFor(() => expect(posts()).toHaveLength(2))
    await screen.findByText(THANKS)
  })

  it('drops the stale notice while the retry is in flight', async () => {
    await answerWith({ status: 'success', outcome: 'no_record', message: NO_RECORD })
    await screen.findByText(NO_RECORD)

    let release: (v: unknown) => void = () => {}
    mockApi.mockReturnValueOnce(new Promise(res => { release = res }))
    fireEvent.click(screen.getByRole('button', { name: /Submit feedback/ }))

    await waitFor(() => expect(screen.queryByText(NO_RECORD)).toBeNull())
    release({ status: 'success', outcome: 'processed', message: THANKS })
  })
})

/* ── Task 8: Skip's empty text carries nothing for any engine to learn from.
   `outcome: "no_text"` retires the form (the user already declined to say
   more, so "send it again" is not advice they can follow), but with a
   NEUTRAL presentation — not the green "Thanks" check, not the amber
   warning. ── */
const NO_TEXT =
  'Nothing was learned because no description was given, though this ' +
  'submission was sent to be recorded against the run.'

describe('no_text — Skip carried no words to store', () => {
  async function skipWith(body: unknown) {
    onFile()
    render(<FeedbackPanel outcome="fail" workflowId="wf-1" />)
    await mounted()
    mockApi.mockResolvedValueOnce(body)
    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))
    await waitFor(() => expect(posts()).toHaveLength(1))
  }

  it('retires the form and shows the backend message', async () => {
    await skipWith({ status: 'success', outcome: 'no_text', message: NO_TEXT })

    await screen.findByText(NO_TEXT)
    expect(screen.queryByPlaceholderText(/it clicked the wrong button/)).toBeNull()
  })

  it('does not render the green "Thanks" state', async () => {
    await skipWith({ status: 'success', outcome: 'no_text', message: NO_TEXT })

    await screen.findByText(NO_TEXT)
    expect(screen.queryByText(THANKS)).toBeNull()
  })
})

/* ── Fix wave D: the resubmission the run-level gate refused because the hint
   it matches is switched off. Nothing was stored and nothing broke, so this
   is neither of the two terminal states — and it is the one answer where
   "send it again" is provably useless, so the amber retry notice is wrong
   for it too. ── */
const INACTIVE =
  'This correction is already on file but is currently switched off, so this ' +
  'submission did not change it. An organisation admin can switch it back on.'

describe('hint_inactive — on file, but switched off', () => {
  it('shows the backend sentence and does NOT claim the correction landed', async () => {
    await answerWith({ status: 'success', outcome: 'hint_inactive', message: INACTIVE })

    await screen.findByText(INACTIVE)
    expect(screen.queryByText(THANKS)).toBeNull()
  })

  it('leaves the form usable — the user may have something different to say', async () => {
    await answerWith({ status: 'success', outcome: 'hint_inactive', message: INACTIVE })
    await screen.findByText(INACTIVE)

    // Unlike no_text the user did not decline to speak, so retiring the panel
    // would take the correction form away over an answer about a DIFFERENT
    // correction. Their typed text survives too, because re-sending is only
    // useless for this exact text on this run.
    const box = screen.getByPlaceholderText(/it clicked the wrong button/)
    expect(box).toBeInTheDocument()
    expect(box).toHaveValue('the search box locator was off')
    expect(screen.getByRole('button', { name: /Submit feedback/ })).toBeEnabled()
  })

  it('does not render as a failure — nothing broke', async () => {
    await answerWith({ status: 'success', outcome: 'hint_inactive', message: INACTIVE })

    // The amber AlertTriangle notice is this panel's "something went wrong,
    // try again" register. Every other non-terminal answer uses it; this one
    // must not, or a no-op reads as an error.
    const notice = (await screen.findByText(INACTIVE)).closest('div')
    expect(notice?.className).not.toMatch(/amber/)
  })
})

describe('processed is unchanged', () => {
  it('still retires the form with the green "Thanks" state', async () => {
    await answerWith({ status: 'success', outcome: 'processed', message: THANKS })

    await screen.findByText(THANKS)
    expect(screen.queryByPlaceholderText(/it clicked the wrong button/)).toBeNull()
  })
})

describe('a request that never got an answer', () => {
  it('keeps the existing behaviour: inline error, form untouched', async () => {
    onFile()
    const { box, submit } = renderFailPanel()
    await mounted()
    mockApi.mockRejectedValueOnce(new Error('Network unreachable'))
    fireEvent.change(box, { target: { value: 'anything' } })
    fireEvent.click(submit)

    await screen.findByText('Network unreachable')
    expect(screen.getByPlaceholderText(/it clicked the wrong button/)).toHaveValue('anything')
  })
})

/* ── T8 Step 6: what this run has already contributed ───────────────────────
   T5 made a second submission of the same correction from the same run a
   no-op. The answer is not a warning about the ignored duplicate — nothing is
   lost, and the endpoint cannot know the outcome anyway (the gate runs on the
   writer thread after the response is sent). It is visible memory. */

const RECORDED = [
  { hint_id: 7, feedback_text: 'wait for the spinner', recorded_at: '2026-08-28T10:00:00Z' },
  { hint_id: 9, feedback_text: 'the search box locator was off', recorded_at: '2026-08-28T10:05:00Z' },
]

describe('the corrections this run already contributed', () => {
  it('lists them above the form, without retiring it', async () => {
    onFile(RECORDED)
    renderFailPanel()

    await screen.findByText(/Already recorded for this run/)
    expect(screen.getByText(/wait for the spinner/)).toBeInTheDocument()
    expect(screen.getByText(/the search box locator was off/)).toBeInTheDocument()
    // The form is still the point of the panel — a user may have a second,
    // different correction to make, and T5 counts that one too.
    expect(screen.getByPlaceholderText(/it clicked the wrong button/)).toBeInTheDocument()
  })

  it('says nothing at all when the run contributed nothing', async () => {
    onFile([])
    renderFailPanel()
    await mounted()

    expect(screen.queryByText(/Already recorded for this run/)).toBeNull()
  })

  it('renders exactly today\u2019s form when the read fails', async () => {
    // Learning off, an unreachable backend, a version-skewed body: the panel
    // adds a note when there is something on file and is silent otherwise, so
    // every failure degrades to the form the user had before this feature.
    mockApi.mockImplementation(async (_path: string, opts?: { method?: string }) => {
      if (opts?.method === 'POST') throw new Error('no POST answer queued')
      throw new Error('Request failed (500)')
    })
    renderFailPanel()
    await mounted()

    expect(screen.queryByText(/Already recorded for this run/)).toBeNull()
    expect(screen.getByPlaceholderText(/it clicked the wrong button/)).toBeInTheDocument()
    expect(screen.queryByText(/Request failed/)).toBeNull()
  })

  it('warns before Submit when the typed text is already on file', async () => {
    onFile(RECORDED)
    const { box } = renderFailPanel()
    await screen.findByText(/Already recorded for this run/)

    fireEvent.change(box, { target: { value: '  wait for the spinner  ' } })

    await screen.findByText(/won\u2019t be counted again/)
  })

  it('never blocks the submission it warns about', async () => {
    // The backend gate is the authority. This compare is a courtesy, and a
    // courtesy that refuses to send would be a second, weaker copy of the
    // dedup key deciding the outcome.
    onFile(RECORDED)
    const { box } = renderFailPanel()
    await screen.findByText(/Already recorded for this run/)
    fireEvent.change(box, { target: { value: 'wait for the spinner' } })
    await screen.findByText(/won\u2019t be counted again/)

    const submit = screen.getByRole('button', { name: /Submit feedback/ })
    expect(submit).toBeEnabled()
    mockApi.mockResolvedValueOnce({ status: 'success', outcome: 'processed', message: THANKS })
    fireEvent.click(submit)

    await waitFor(() => expect(posts()).toHaveLength(1))
  })

  it('stays quiet for text that is merely similar', async () => {
    onFile(RECORDED)
    const { box } = renderFailPanel()
    await screen.findByText(/Already recorded for this run/)

    fireEvent.change(box, { target: { value: 'wait for the spinner to go' } })

    expect(screen.queryByText(/won\u2019t be counted again/)).toBeNull()
  })

  it('asks for nothing when there is no run to ask about', () => {
    render(<FeedbackPanel outcome="fail" workflowId={null} />)

    expect(mockApi).not.toHaveBeenCalled()
  })
})

/* ── M9: the mount-only fetch above cannot see a correction filed during
   THIS session, so a second correction typed after the first landed never
   showed up in "Already recorded for this run" until the page was reloaded.
   Re-fetching once the POST confirms `processed` closes that gap. ── */

describe('M9: catching up after a correction lands', () => {
  // The corrections GET carries { cache: 'no-store' } (the response is
  // per-caller and must never sit in a private browser cache); the POST
  // carries a method. Keying on "no method" keeps this filter honest if more
  // fetch options are added later.
  const getCalls = () =>
    mockApi.mock.calls.filter(([path, opts]) =>
      path === '/api/feedback/wf-1' &&
      (opts as { method?: string } | undefined)?.method === undefined)

  it('re-fetches the recorded list once a correction confirms processed', async () => {
    onFile([])
    const { box, submit } = renderFailPanel()
    await mounted()
    expect(getCalls()).toHaveLength(1)

    mockApi.mockResolvedValueOnce({ status: 'success', outcome: 'processed', message: THANKS })
    mockApi.mockResolvedValueOnce({
      status: 'success',
      corrections: [{ hint_id: 21, feedback_text: 'the search box locator was off', recorded_at: '2026-08-29T00:00:00Z' }],
    })
    fireEvent.change(box, { target: { value: 'the search box locator was off' } })
    fireEvent.click(submit)
    await screen.findByText(THANKS)

    await waitFor(() => expect(getCalls()).toHaveLength(2))
  })

  it('does not re-fetch when the correction did not confirm processed', async () => {
    onFile([])
    const { box, submit } = renderFailPanel()
    await mounted()
    expect(getCalls()).toHaveLength(1)

    mockApi.mockResolvedValueOnce({ status: 'success', outcome: 'no_record', message: 'not confirmed' })
    fireEvent.change(box, { target: { value: 'the search box locator was off' } })
    fireEvent.click(submit)
    await screen.findByText('not confirmed')

    expect(getCalls()).toHaveLength(1)
  })
})

/* ── An EMPTY "Submit feedback" is an attempt to correct, not a decline.

   Skip deliberately sends empty text: the user said they have nothing to
   add, and `no_text` retiring the form is the right answer to that. Submit
   with an empty box produced the identical request, so the same retirement
   fired — and because this panel is rendered in exactly one place and never
   remounts for a run, the user lost every route back to giving feedback for
   it. Measured in the real SPA on a passing run: after an empty Submit,
   Skip / Submit / Spot on / Not quite were all gone and no textarea
   remained.

   The guard is on Submit only. Skip keeps sending empty text (it is the
   decline), and the passing path keeps Cancel, so neither is a dead end. ── */
describe('Submit requires words; Skip does not', () => {
  it('disables Submit while the box is empty or whitespace', async () => {
    onFile()
    const { box, submit } = renderFailPanel()
    await mounted()

    expect(submit).toBeDisabled()

    fireEvent.change(box, { target: { value: '   ' } })
    expect(submit).toBeDisabled()

    fireEvent.change(box, { target: { value: 'the locator was wrong' } })
    expect(submit).toBeEnabled()
  })

  it('still lets Skip send the empty decline', async () => {
    onFile()
    render(<FeedbackPanel outcome="fail" workflowId="wf-1" />)
    await mounted()
    mockApi.mockResolvedValueOnce({
      status: 'success', outcome: 'no_text', message: NO_TEXT,
    })

    fireEvent.click(screen.getByRole('button', { name: /Skip/ }))

    await waitFor(() => expect(posts()).toHaveLength(1))
    expect(posts()[0][1]).toMatchObject({ method: 'POST' })
    await screen.findByText(NO_TEXT)
  })

  it('does not strand a passing run — Cancel closes the form instead', async () => {
    onFile()
    render(<FeedbackPanel outcome="pass" workflowId="wf-1" />)
    await mounted()
    fireEvent.click(screen.getByRole('button', { name: /Not quite/ }))

    expect(screen.getByRole('button', { name: /Submit feedback/ })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: /Cancel/ }))

    expect(screen.getByRole('button', { name: /Spot on/ })).toBeInTheDocument()
    expect(posts()).toHaveLength(0)
  })

})


/* ── T7: retracting a hint from the feedback panel ───────────────────────────
   can_retract is server-computed (hint_mutation_verdict, ownership.py) — the
   client never decides this, it only draws what the server already permits.
   So the control's presence is driven entirely by the field on each
   correction, including the two cases where it must stay hidden: an older
   server that predates the field, and the learning-disabled response shape
   (neither ever sends can_retract at all).

   What happens AFTER a retract is the other half, and it is where this panel
   was lying. It used to drop the row, which hid the user's own words (the GET
   behind them is deliberately unfiltered on is_active), disarmed the
   duplicate-submission notice, and so let a re-typed identical correction be
   answered with "Thanks — your feedback helps the system learn" while the
   writer thread deduped it back to the retracted hint and returned without
   reactivating anything. ── */
describe('T7: retracting a hint from the feedback panel', () => {
  const RETRACT_PATH = '/api/learning/hints/7/retract'
  const retractCalls = () =>
    mockApi.mock.calls.filter(([path]) => path === RETRACT_PATH)

  const RETRACTABLE = [
    { hint_id: 7, feedback_text: 'wait for the spinner', recorded_at: '2026-08-28T10:00:00Z', can_retract: true },
  ]
  const TWO_RETRACTABLE = [
    RETRACTABLE[0],
    { hint_id: 9, feedback_text: 'the search box locator was off', recorded_at: '2026-08-28T10:05:00Z', can_retract: true },
  ]

  // jsdom ships a window.confirm that only logs "Not implemented", so every
  // test that reaches the POST has to say what the user answered. Restored
  // rather than reset, so the stub cannot outlive this file.
  // Typed by what this file does with the handle — restore it — rather than
  // by spyOn's return, whose generic parameters cannot be spelled here.
  let confirmSpy: { mockRestore: () => void } | null = null
  const answersConfirm = (agreed: boolean) => {
    confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(agreed)
  }
  afterEach(() => { confirmSpy?.mockRestore(); confirmSpy = null })

  it('renders a Retract control when the server says can_retract: true', async () => {
    onFile(RETRACTABLE)
    renderFailPanel()

    expect(await screen.findByRole('button', { name: /Retract/ })).toBeInTheDocument()
  })

  it('does not render Retract when can_retract is false', async () => {
    onFile([{ ...RETRACTABLE[0], can_retract: false }])
    renderFailPanel()
    await screen.findByText(/Already recorded for this run/)

    expect(screen.queryByRole('button', { name: /Retract/ })).toBeNull()
  })

  it('does not render Retract when the field is absent (older server, or learning disabled)', async () => {
    onFile([{ hint_id: 7, feedback_text: 'wait for the spinner', recorded_at: '2026-08-28T10:00:00Z' }])
    renderFailPanel()
    await screen.findByText(/Already recorded for this run/)

    expect(screen.queryByRole('button', { name: /Retract/ })).toBeNull()
  })

  it('POSTs to the retract endpoint for that hint id when clicked', async () => {
    answersConfirm(true)
    onFile(RETRACTABLE)
    renderFailPanel()
    const retractBtn = await screen.findByRole('button', { name: /Retract/ })
    mockApi.mockResolvedValueOnce({ hint: {}, changed: true })   // the retract POST

    fireEvent.click(retractBtn)

    await waitFor(() => expect(retractCalls()).toHaveLength(1))
    expect(retractCalls()[0][1]).toMatchObject({ method: 'POST' })
  })

  it('does not send the signed-in user email as actor — the server overrides it from the token', async () => {
    answersConfirm(true)
    onFile(RETRACTABLE)
    renderFailPanel()
    const retractBtn = await screen.findByRole('button', { name: /Retract/ })
    mockApi.mockResolvedValueOnce({ hint: {}, changed: true })

    fireEvent.click(retractBtn)
    await waitFor(() => expect(retractCalls()).toHaveLength(1))

    const body = JSON.parse((retractCalls()[0][1] as { body: string }).body)
    expect(typeof body.actor).toBe('string')
    expect(body.actor.length).toBeGreaterThan(0)
    expect(body.actor).not.toMatch(/@/)   // never an email address
  })

  /* Both admin surfaces that offer this action confirm first (LearningPage's
     Hints table and HintDrawer, identical wording). This one did not — and it
     is a small destructive button flush against the user's own text, on the
     surface built for the user with the fewest ways back. */
  describe('the confirmation', () => {
    it('sends nothing when the user cancels', async () => {
      answersConfirm(false)
      onFile(RETRACTABLE)
      renderFailPanel()
      const retractBtn = await screen.findByRole('button', { name: /Retract/ })

      fireEvent.click(retractBtn)

      expect(window.confirm).toHaveBeenCalled()
      expect(retractCalls()).toHaveLength(0)
      // Nothing moved: the control and the words are exactly as they were.
      expect(screen.getByRole('button', { name: /Retract/ })).toBeInTheDocument()
      expect(screen.getByText(/wait for the spinner/)).toBeInTheDocument()
    })

    it('asks in the user’s language, not the admin’s', async () => {
      answersConfirm(false)
      onFile(RETRACTABLE)
      renderFailPanel()
      fireEvent.click(await screen.findByRole('button', { name: /Retract/ }))

      const asked = vi.mocked(window.confirm).mock.calls[0][0] as string
      // "It will stop injecting into agent prompts" is the admin sentence. A
      // plain org member is not the person to reason about that.
      expect(asked).not.toMatch(/inject/i)
      expect(asked).toMatch(/retract this correction/i)
    })
  })

  /* R-B2: the row survives a successful retract.

     The engine's own rule is that these words stay visible —
     get_corrections_for_run is unfiltered on is_active because "these are the
     user's own words, and hiding a hint the LLM later flagged would report
     'nothing on file' … the exact lie this task removes." Dropping the row
     here contradicted that on the very next mount, and disarmed the duplicate
     notice in between. */
  describe('after a successful retract', () => {
    async function retractIt(answer: unknown = { hint: {}, changed: true }) {
      answersConfirm(true)
      onFile(RETRACTABLE)
      const rendered = renderFailPanel()
      const retractBtn = await screen.findByRole('button', { name: /Retract/ })
      mockApi.mockResolvedValueOnce(answer)
      fireEvent.click(retractBtn)
      await waitFor(() => expect(screen.queryByRole('button', { name: /Retract/ })).toBeNull())
      return rendered
    }

    it('keeps the user’s own words on screen, marked retracted', async () => {
      await retractIt()

      expect(screen.getByText(/wait for the spinner/)).toBeInTheDocument()
      expect(screen.getByText(/Already recorded for this run/)).toBeInTheDocument()
      expect(screen.getByText('— retracted')).toBeInTheDocument()
    })

    it('does not re-fetch the list to find that out', async () => {
      // The server would hand back this same row: the GET is unfiltered on
      // is_active, so can_retract and active would both flip. A round trip
      // still buys nothing — the local retracted marker already outranks
      // active === false, so the render is identical either way.
      await retractIt()

      expect(mockApi.mock.calls.filter(([path]) => path === '/api/feedback/wf-1')).toHaveLength(1)
    })

    it('tells the user a re-send will not undo it — scoped to this run', async () => {
      // The warning this arms: retract by mistake, retype the identical
      // text, and the panel warns before the resend instead of staying
      // silent about it. The resend itself is answered honestly too — the
      // writer thread dedupes to the retracted hint and the engine reports
      // outcome "hint_inactive" rather than thanking the user for a no-op —
      // but that response only exists after the click, so this warning is
      // still the only thing that reaches the user BEFORE it.
      const { box } = await retractIt()

      fireEvent.change(box, { target: { value: 'wait for the spinner' } })

      await screen.findByText(/You retracted this correction/)
      expect(screen.queryByText(/won’t be counted again/)).toBeNull()
    })

    it('says a no-op was a no-op when the server reports changed: false', async () => {
      // An org admin retracted it between this panel's GET and this click.
      // The hint IS retracted, so the row is still marked — but this click is
      // not what did it, and claiming otherwise reports a no-op as the user's
      // own action.
      await retractIt({ hint: {}, changed: false, note: 'hint was already retracted' })

      expect(screen.getByText('— already retracted')).toBeInTheDocument()
      expect(screen.queryByText('— retracted')).toBeNull()
      expect(screen.getByText(/wait for the spinner/)).toBeInTheDocument()
    })
  })

  describe('a retract that fails', () => {
    it('surfaces the failure instead of silently looking like success', async () => {
      answersConfirm(true)
      onFile(RETRACTABLE)
      renderFailPanel()
      const retractBtn = await screen.findByRole('button', { name: /Retract/ })
      mockApi.mockRejectedValueOnce(new ApiError(500, 'Failed to retract hint'))

      fireEvent.click(retractBtn)

      await screen.findByText('Failed to retract hint')
      // Unlike the read (which degrades to silence), this is a write the user
      // explicitly asked for — the panel must not blow up, and a 500 is ours
      // and transient, so the control stays for a retry.
      expect(screen.getByRole('button', { name: /Retract/ })).toBeInTheDocument()
      expect(screen.queryByText(/^— (already )?retracted$/)).toBeNull()
    })

    it('clears a control the server has refused, and keeps the words', async () => {
      // M1. 403 and 404 both mean this hint is not the caller's to act on — an
      // org admin got there first, or it is gone. Leaving the button drew a
      // control that could only fail again, forever.
      //
      // The sentence is the server's own, and it is the one wave A split out
      // for this route: the author tier applies to retract and to nothing
      // else, so the other four routes now say "Only an org admin can change
      // this hint" instead.
      answersConfirm(true)
      onFile(RETRACTABLE)
      renderFailPanel()
      const retractBtn = await screen.findByRole('button', { name: /Retract/ })
      mockApi.mockRejectedValueOnce(
        new ApiError(403, "Only the hint's author or an org admin can retract it"))

      fireEvent.click(retractBtn)

      await screen.findByText("Only the hint's author or an org admin can retract it")
      await waitFor(() => expect(screen.queryByRole('button', { name: /Retract/ })).toBeNull())
      expect(screen.getByText(/wait for the spinner/)).toBeInTheDocument()
      // A refusal is not a retraction, so no marker.
      expect(screen.queryByText(/^— (already )?retracted$/)).toBeNull()
    })
  })

  /* M3: one in-flight id and one error string, for a list of N items. */
  describe('two corrections, two independent controls', () => {
    it('does not re-enable one item’s Retract while it is still in flight', async () => {
      answersConfirm(true)
      onFile(TWO_RETRACTABLE)
      renderFailPanel()
      const [a, b] = await screen.findAllByRole('button', { name: /Retract/ })

      mockApi.mockReturnValueOnce(new Promise(() => {}))   // A never answers
      fireEvent.click(a)
      await waitFor(() => expect(a).toBeDisabled())

      mockApi.mockReturnValueOnce(new Promise(() => {}))
      fireEvent.click(b)
      await waitFor(() => expect(b).toBeDisabled())

      expect(a).toBeDisabled()
    })

    it('does not wipe one item’s error the instant the other is attempted', async () => {
      answersConfirm(true)
      onFile(TWO_RETRACTABLE)
      renderFailPanel()
      const [a, b] = await screen.findAllByRole('button', { name: /Retract/ })

      mockApi.mockRejectedValueOnce(new ApiError(500, 'Failed to retract hint'))
      fireEvent.click(a)
      await screen.findByText('Failed to retract hint')

      mockApi.mockReturnValueOnce(new Promise(() => {}))
      fireEvent.click(b)
      await waitFor(() => expect(b).toBeDisabled())

      expect(screen.getByText('Failed to retract hint')).toBeInTheDocument()
    })
  })

  /* `active` is the server's own signal (is_active, forwarded by
     get_run_corrections) — orthogonal to `retracted`, which is client-only
     and knows only about THIS session's own click. Global Constraint 1 is
     why the marker for it says "switched off", never "retracted": is_active
     going to 0 has four possible causes and the client cannot tell them
     apart. */
  describe('active: false — switched off by something other than this session', () => {
    it('renders "— switched off" and no Retract control', async () => {
      onFile([{ ...RETRACTABLE[0], can_retract: false, active: false }])
      renderFailPanel()

      await screen.findByText('— switched off')
      expect(screen.queryByRole('button', { name: /Retract/ })).toBeNull()
    })

    it('renders no marker at all when active is true', async () => {
      onFile([{ ...RETRACTABLE[0], active: true }])
      renderFailPanel()
      await screen.findByText(/wait for the spinner/)

      expect(screen.queryByText('— switched off')).toBeNull()
      expect(screen.queryByText(/^— (already )?retracted$/)).toBeNull()
    })

    it('renders no marker when the field is absent (older backend)', async () => {
      onFile(RETRACTABLE)
      renderFailPanel()
      await screen.findByText(/wait for the spinner/)

      expect(screen.queryByText('— switched off')).toBeNull()
      expect(screen.queryByText(/^— (already )?retracted$/)).toBeNull()
    })

    it('lets the in-session retracted marker win over active: false', async () => {
      answersConfirm(true)
      onFile([{ ...RETRACTABLE[0], active: false }])
      renderFailPanel()
      const retractBtn = await screen.findByRole('button', { name: /Retract/ })
      mockApi.mockResolvedValueOnce({ hint: {}, changed: true })

      fireEvent.click(retractBtn)

      await waitFor(() => expect(screen.queryByRole('button', { name: /Retract/ })).toBeNull())
      expect(screen.getByText('— retracted')).toBeInTheDocument()
      expect(screen.queryByText('— switched off')).toBeNull()
    })
  })

  describe('the duplicate notice, once active is known', () => {
    it('shows the switched-off sentence — not "already sent" — when active is false', async () => {
      onFile([{ ...RETRACTABLE[0], can_retract: false, active: false }])
      const { box } = renderFailPanel()
      await screen.findByText(/Already recorded for this run/)

      fireEvent.change(box, { target: { value: 'wait for the spinner' } })

      await screen.findByText(/turn it back on/)
      expect(screen.queryByText(/already sent this/)).toBeNull()
    })

    it('still shows "already sent" when active is true', async () => {
      onFile([{ ...RETRACTABLE[0], can_retract: false, active: true }])
      const { box } = renderFailPanel()
      await screen.findByText(/Already recorded for this run/)

      fireEvent.change(box, { target: { value: 'wait for the spinner' } })

      await screen.findByText(/won’t be counted again/)
    })
  })
})
