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

/** A failed run: the correction form is open from the start. */
function renderFailPanel() {
  render(<FeedbackPanel outcome="fail" workflowId="wf-1" />)
  return {
    box: screen.getByPlaceholderText(/it clicked the wrong button/),
    submit: screen.getByRole('button', { name: /Submit feedback/ }),
  }
}

async function answerWith(body: unknown) {
  mockApi.mockResolvedValueOnce(body)
  const { box, submit } = renderFailPanel()
  fireEvent.change(box, { target: { value: 'the search box locator was off' } })
  fireEvent.click(submit)
  await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(1))
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

    await waitFor(() => expect(mockApi).toHaveBeenCalledTimes(2))
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

describe('a request that never got an answer', () => {
  it('keeps the existing behaviour: inline error, form untouched', async () => {
    mockApi.mockRejectedValueOnce(new Error('Network unreachable'))
    const { box, submit } = renderFailPanel()
    fireEvent.change(box, { target: { value: 'anything' } })
    fireEvent.click(submit)

    await screen.findByText('Network unreachable')
    expect(screen.getByPlaceholderText(/it clicked the wrong button/)).toHaveValue('anything')
  })
})
