/**
 * RecordedCorrections — the read-only list shared by the Generate panel and
 * the History drawer (Task 2 of feedback-visibility). Presentational only:
 * no fetch, no mocks needed, so this is cheap to pin directly.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ALREADY_RECORDED_HEADING, RecordedCorrections, SWITCHED_OFF_MARKER } from './RecordedCorrections'

describe('RecordedCorrections', () => {
  it('renders nothing for an empty array', () => {
    const { container } = render(<RecordedCorrections corrections={[]} />)

    expect(container).toBeEmptyDOMElement()
  })

  it('renders the heading and every correction’s text', () => {
    render(
      <RecordedCorrections
        corrections={[
          { hint_id: 7, feedback_text: 'wait for the spinner' },
          { hint_id: 9, feedback_text: 'the search box locator was off' },
        ]}
      />,
    )

    expect(screen.getByText(ALREADY_RECORDED_HEADING)).toBeInTheDocument()
    expect(screen.getByText(/wait for the spinner/)).toBeInTheDocument()
    expect(screen.getByText(/the search box locator was off/)).toBeInTheDocument()
  })

  it('renders the switched-off marker only for a correction with active: false', () => {
    render(
      <RecordedCorrections
        corrections={[
          { hint_id: 1, feedback_text: 'switched off one', active: false },
          { hint_id: 2, feedback_text: 'still active one', active: true },
          { hint_id: 3, feedback_text: 'unknown-state one' },
        ]}
      />,
    )

    // Exactly one row carries the marker.
    expect(screen.getAllByText(SWITCHED_OFF_MARKER)).toHaveLength(1)
    expect(screen.getByText(/switched off one/).textContent).toContain(SWITCHED_OFF_MARKER)
    expect(screen.getByText(/still active one/).textContent).not.toContain(SWITCHED_OFF_MARKER)
    expect(screen.getByText(/unknown-state one/).textContent).not.toContain(SWITCHED_OFF_MARKER)
  })
})
