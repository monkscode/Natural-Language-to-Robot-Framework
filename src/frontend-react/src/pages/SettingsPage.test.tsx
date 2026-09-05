/**
 * SettingsPage — a form entirely driven by the SECTIONS data table: every
 * section title/description and every field's label, type, placeholder and
 * default value comes from that one array. This file pins SECTIONS' wiring
 * into the DOM (label -> input association via htmlFor/id, password masking,
 * pre-filled defaults) rather than Radix Select's own open/close behaviour,
 * which belongs to vendored ui/select.tsx (excluded from coverage, and not
 * exercised here to avoid the pointer-capture/scrollIntoView polyfills a real
 * open would need in jsdom).
 *
 * The page is a mockup: nothing here persists. There is no settings route and
 * no settings table in src/backend/ — MODEL_PROVIDER, GEMINI_API_KEY and the
 * rest are process-level .env values read once at startup — and the two action
 * buttons never had an onClick. Wiring it up is a feature (persistence, auth
 * scoping, and a hot-reload story for values read at boot), not a fix, so the
 * controls are disabled and the page says so. The tests below pin that: a
 * platform admin must not be able to type a real API key into a dead field.
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import SettingsPage from './SettingsPage'

describe('SettingsPage — section structure', () => {
  it('renders all three section titles and descriptions', () => {
    render(<SettingsPage />)

    expect(screen.getByText('AI Provider')).toBeInTheDocument()
    expect(screen.getByText('Configure which AI model generates your test code.')).toBeInTheDocument()
    expect(screen.getByText('Robot Framework')).toBeInTheDocument()
    expect(screen.getByText('Choose which browser automation library to use for test generation.')).toBeInTheDocument()
    expect(screen.getByText('Server & Execution')).toBeInTheDocument()
    expect(screen.getByText('Ports, timeouts and service URLs for your local environment.')).toBeInTheDocument()
  })

  it('renders the page heading and its description', () => {
    render(<SettingsPage />)

    expect(screen.getByRole('heading', { name: 'Settings' })).toBeInTheDocument()
    expect(screen.getByText(/Configure your AI provider, automation library and execution environment/))
      .toBeInTheDocument()
  })
})

describe('SettingsPage — field wiring', () => {
  it('gives the API Key field a password input with its placeholder and an empty default', () => {
    render(<SettingsPage />)

    const input = screen.getByLabelText('API Key') as HTMLInputElement
    expect(input.type).toBe('password')
    expect(input.placeholder).toBe('Enter your Gemini API key…')
    expect(input.value).toBe('')
  })

  it('pre-fills the Server & Execution text fields with their configured defaults', () => {
    render(<SettingsPage />)

    expect((screen.getByLabelText('App Port') as HTMLInputElement).value).toBe('5000')
    expect((screen.getByLabelText('Browser Service URL') as HTMLInputElement).value).toBe('http://localhost:4999')
    expect((screen.getByLabelText('Execution Timeout (s)') as HTMLInputElement).value).toBe('900')
  })

  it('associates every select field label with its trigger via htmlFor/id', () => {
    render(<SettingsPage />)

    // getByLabelText only succeeds if <Label htmlFor> actually resolves to an
    // element with that id (here, the Select's trigger button) — it does not
    // exercise Radix's open/close behaviour.
    expect(screen.getByLabelText('Model Provider')).toBeInTheDocument()
    expect(screen.getByLabelText('Model')).toBeInTheDocument()
    expect(screen.getByLabelText('Library')).toBeInTheDocument()
    expect(screen.getByLabelText('Browser')).toBeInTheDocument()
    expect(screen.getByLabelText('Headless Mode')).toBeInTheDocument()
  })
})

describe('SettingsPage — actions', () => {
  it('renders Discard changes and Save settings buttons', () => {
    render(<SettingsPage />)

    expect(screen.getByRole('button', { name: 'Discard changes' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Save settings' })).toBeInTheDocument()
  })

  it('disables both action buttons, because neither one saves or discards anything', () => {
    render(<SettingsPage />)

    expect(screen.getByRole('button', { name: 'Discard changes' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Save settings' })).toBeDisabled()
  })
})

describe('SettingsPage — inactive until a backend exists', () => {
  it('tells the reader the page is not active yet and where the settings really live', () => {
    render(<SettingsPage />)

    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent(/not active yet/i)
    expect(notice).toHaveTextContent(/src\/backend\/\.env/)
  })

  /* The API Key field is the one with real damage attached: it carries the
   * placeholder "Enter your Gemini API key…", so a platform admin can paste a
   * live secret into it and get total silence. It must not accept input. */
  it('disables the API Key field so a real secret cannot be typed into a dead form', () => {
    render(<SettingsPage />)

    expect(screen.getByLabelText('API Key')).toBeDisabled()
  })

  it('disables every text field', () => {
    render(<SettingsPage />)

    for (const label of ['App Port', 'Browser Service URL', 'Execution Timeout (s)']) {
      expect(screen.getByLabelText(label), `"${label}" is still editable`).toBeDisabled()
    }
  })

  it('disables every select trigger', () => {
    render(<SettingsPage />)

    for (const label of ['Model Provider', 'Model', 'Library', 'Browser', 'Headless Mode']) {
      expect(screen.getByLabelText(label), `"${label}" is still selectable`).toBeDisabled()
    }
  })
})
