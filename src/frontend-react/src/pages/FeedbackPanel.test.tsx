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
 * page itself is deliberately not under test (vite.config.ts: "no page
 * components") — nothing here renders GeneratePage, only the panel, so no
 * Router and no page state are involved.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/api', () => ({ api: vi.fn() }))

import { api } from '@/lib/api'
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
  await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/api/feedback/wf-1'))
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
    ['error', { status: 'error', outcome: 'error', message: 'Something went wrong, so this correction was not recorded. Please send it again.' }, /Something went wrong/],
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
  'submission is recorded against the run — a description can still be sent.'

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
  const getCalls = () =>
    mockApi.mock.calls.filter(([path, opts]) => path === '/api/feedback/wf-1' && opts === undefined)

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
