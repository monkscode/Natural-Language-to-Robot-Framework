/**
 * GeneratePage — the page shell around the generate/execute SSE flow.
 *
 * FeedbackPanel.test.tsx already renders FeedbackPanel in isolation and pins
 * its behaviour (outcome handling, retract, duplicate warnings, ...) — none
 * of that is repeated here. This file covers everything else: the query and
 * code inputs, the generate and execute SSE calls (lib/sse.ts is mocked —
 * it has its own test file), the pipeline progress rail, the post-run result
 * card, New Test's confirm guard (both the confirmed and cancelled path),
 * copy/export, and — the failure class a type checker cannot catch because
 * both sides of a mix-up are plain strings — which run id a correction or a
 * download actually gets attributed to.
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/sse', () => ({ streamSSE: vi.fn() }))
vi.mock('@/lib/api', async importOriginal => ({
  ...(await importOriginal<typeof import('@/lib/api')>()),
  api: vi.fn(),
}))

import { streamSSE } from '@/lib/sse'
import { api } from '@/lib/api'
import GeneratePage from './GeneratePage'

const mockStreamSSE = vi.mocked(streamSSE)
const mockApi = vi.mocked(api)

// jsdom implements neither scrollIntoView nor real navigation. GeneratePage
// calls scrollIntoView from two effects (entering "executing", and once an
// outcome lands); without a stub those effects throw inside a setTimeout and
// the failure surfaces on a completely unrelated later test.
HTMLElement.prototype.scrollIntoView = vi.fn()

afterEach(() => { vi.resetAllMocks(); vi.unstubAllGlobals() })

const QUERY = 'open flipkart and search for shoes'
// No trailing newline: split('\n').length below is then exactly the number
// of lines a human would count, so the "(N lines)" assertion isn't derived
// from the same formula the source uses to compute it.
const CODE = '*** Settings ***\nLibrary    Browser\n\n*** Test Cases ***\nSearch\n    New Page    https://flipkart.com'

function stubClipboard() {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true, writable: true })
  return writeText
}

/** URL.createObjectURL/revokeObjectURL don't exist in jsdom, jsdom's own
 *  Blob has no .text()/.arrayBuffer() to read a captured one back with, and
 *  a real anchor .click() logs a "Not implemented: navigation" jsdom error.
 *  Replace Blob with a constructor that just records what it was built
 *  from — nothing downstream needs a real blob, since createObjectURL is
 *  stubbed too. */
function stubDownload() {
  let parts: unknown[] | null = null
  let anchor: HTMLAnchorElement | null = null
  vi.stubGlobal('Blob', vi.fn().mockImplementation((p: unknown[]) => { parts = p; return {} }))
  URL.createObjectURL = vi.fn(() => 'blob:mock-url') as typeof URL.createObjectURL
  URL.revokeObjectURL = vi.fn()
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (this: HTMLAnchorElement) { anchor = this })
  return { getParts: () => parts, getAnchor: () => anchor }
}

/** Answers FeedbackPanel's mount GET so a test whose flow reaches "outcome"
 *  doesn't leave that fetch unanswered. */
function allowFeedbackMount() {
  mockApi.mockImplementation(async (path: string) => {
    if (path.startsWith('/api/feedback/')) return { corrections: [] }
    throw new Error(`allowFeedbackMount: unexpected api(${path})`)
  })
}

function renderPage(initialState: { prefillQuery?: string } = {}) {
  return render(
    <MemoryRouter initialEntries={[{ pathname: '/generate', state: initialState }]}>
      <GeneratePage />
    </MemoryRouter>,
  )
}

function typeQuery(text = QUERY) {
  fireEvent.change(screen.getByPlaceholderText(/Open Google, search for/), { target: { value: text } })
}

function pasteCode(text = CODE) {
  fireEvent.change(screen.getByPlaceholderText(/Generated code appears here/), { target: { value: text } })
}

describe('GeneratePage — idle state', () => {
  it('starts empty: no code, a disabled action button, no New Test control', () => {
    renderPage()

    expect(screen.getByPlaceholderText(/Open Google, search for/)).toHaveValue('')
    expect(screen.getByRole('button', { name: /Enter a query or paste code/ })).toBeDisabled()
    expect(screen.queryByRole('button', { name: /New Test/ })).toBeNull()
  })
})

describe('GeneratePage — the run button’s icon and label', () => {
  it('shows the Zap icon for "Generate Test", and swaps to Play once code exists', () => {
    renderPage()
    typeQuery()
    expect(screen.getByRole('button', { name: /Generate Test/ }).querySelector('svg')).toHaveClass('lucide-zap')

    pasteCode()
    expect(screen.getByRole('button', { name: /^Run Test/ }).querySelector('svg')).toHaveClass('lucide-play')
  })

  it('shows a spinner (no icon glyph) and "Generating… N%" while a generation is in flight', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', progress: 42, message: 'working…' })
      return new Promise(() => {})
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    const busyBtn = await screen.findByRole('button', { name: /Generating… 42%/ })
    expect(busyBtn).toBeDisabled()
    expect(busyBtn.querySelector('svg')).toBeNull()
  })
})

describe('GeneratePage — starting a generation', () => {
  it('enables "Generate Test" once a query is typed, and sends it verbatim', async () => {
    mockStreamSSE.mockImplementation(() => new Promise(() => {}))
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    await waitFor(() => expect(mockStreamSSE).toHaveBeenCalledWith(
      '/generate-test', { query: QUERY }, expect.any(Function),
    ))
  })

  it('Ctrl+Enter in the query box also starts generation while no code exists yet', async () => {
    mockStreamSSE.mockImplementation(() => new Promise(() => {}))
    renderPage()
    typeQuery()

    fireEvent.keyDown(screen.getByPlaceholderText(/Open Google, search for/), { key: 'Enter', ctrlKey: true })

    await waitFor(() => expect(mockStreamSSE).toHaveBeenCalledWith(
      '/generate-test', { query: QUERY }, expect.any(Function),
    ))
  })
})

describe('GeneratePage — the generation pipeline rail', () => {
  it.each([
    [10, 'Breaking your description into test steps'],
    [30, 'Visiting the page and pinning down each element'],
    [70, 'Writing test.robot from the plan and locators'],
    [90, 'Compiling the test in a clean Docker container — auto-fixing anything that fails'],
  ])('shows the right stage as active at %i%% progress', async (progress, expectedTitle) => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', progress, message: 'working…' })
      return new Promise(() => {}) // stays "generating" so the pipeline stays on screen
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    expect(await screen.findByText(expectedTitle)).toBeInTheDocument()
  })

  it('surfaces the element count once the locate stage reports one', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', progress: 30, message: 'Found 12 elements on the page' })
      return new Promise(() => {})
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    expect(await screen.findByText('12 elements found on the page')).toBeInTheDocument()
  })

  it('marks earlier stages done, with their own summaries, once progress passes them', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', progress: 90, message: 'Dry-run verification' })
      return new Promise(() => {})
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    expect(await screen.findByText('Test plan ready')).toBeInTheDocument()
    expect(screen.getByText('Element locators captured')).toBeInTheDocument()
    expect(screen.getByText('test.robot assembled')).toBeInTheDocument()
    expect(screen.getByText('90%')).toBeInTheDocument()
    // A finished stage (Plan) and the current one (Verify, the last stage —
    // there is no later `at` to pass, so it can only ever be active or done)
    // must read as visually distinct, not just as different text.
    const planIcon = screen.getByText('Plan').parentElement!.querySelector('span')!
    const verifyIcon = screen.getByText('Verify').parentElement!.querySelector('span')!
    expect(planIcon).toHaveClass('border-emerald-500/40')
    expect(verifyIcon).toHaveClass('stage-active')
  })

  it('leaves a not-yet-reached stage in its pending style while an earlier one is active', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', progress: 10, message: 'working…' })
      return new Promise(() => {})
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    await screen.findByText('Plan')
    const planIcon = screen.getByText('Plan').parentElement!.querySelector('span')!
    const locateIcon = screen.getByText('Locate').parentElement!.querySelector('span')!
    const locateLabel = screen.getByText('Locate')
    expect(planIcon).toHaveClass('stage-active')
    expect(locateIcon).toHaveClass('border-[#30363d]')
    expect(locateLabel).toHaveClass('text-[#484f58]')
  })
})

describe('GeneratePage — a completed generation', () => {
  it('shows the generated code and a success log line once delivery is clean', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'running', message: 'Doing work…' })
      onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    await screen.findByRole('button', { name: /^Run Test/ })
    expect(screen.getByText('Generated test.robot (6 lines)')).toBeInTheDocument()
    // Info and success log lines get their own left-border color, not just
    // their own text.
    expect(screen.getByText('Doing work…').parentElement).toHaveClass('border-l-blue-400')
    expect(screen.getByText('Generated test.robot (6 lines)').parentElement).toHaveClass('border-l-green-500')
  })

  it('flags a delivered-but-unverified dryrun without inventing a cause', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({
        status: 'complete', robot_code: CODE, workflow_id: 'wf-1',
        dryrun_status: 'unverified', dryrun_message: 'Docker health check timed out',
      })
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    expect(await screen.findByText('Delivered — this test was NOT verified, because the dryrun could not run')).toBeInTheDocument()
    expect(screen.getByText('Docker health check timed out')).toBeInTheDocument()
  })

  it('flags a failed dryrun with the backend’s own message', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({
        status: 'complete', robot_code: CODE, workflow_id: 'wf-1',
        dryrun_status: 'failed', dryrun_message: 'Keyword not found: Click Buttonn',
      })
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    expect(await screen.findByText('Delivered — dryrun found issues you may want to review')).toBeInTheDocument()
    expect(screen.getByText('Keyword not found: Click Buttonn')).toBeInTheDocument()
    // Error-kind log lines get their own left-border color too.
    expect(screen.getByText('Keyword not found: Click Buttonn').parentElement).toHaveClass('border-l-destructive')
  })

  it('shows the SSE error message and re-enables the form on status: error', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'error', message: 'The planner produced no usable steps' })
    })
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    // Written to BOTH the error banner and the (now-visible) generation
    // log — asserting the count pins that double-write rather than just
    // picking whichever element happens to match first.
    await waitFor(() => expect(screen.getAllByText('The planner produced no usable steps')).toHaveLength(2))
    expect(screen.getByRole('button', { name: /Generate Test/ })).not.toBeDisabled()
  })

  it('shows a fallback error and recovers when streamSSE itself rejects (network failure)', async () => {
    mockStreamSSE.mockRejectedValue(new Error('Failed to fetch'))
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))

    await waitFor(() => expect(screen.getAllByText('Failed to fetch')).toHaveLength(2))
    expect(screen.getByRole('button', { name: /Generate Test/ })).not.toBeDisabled()
  })
})

describe('GeneratePage — running pasted code (no generation)', () => {
  it('sends user_query: null and mounts no FeedbackPanel, even with unrelated text in the query box', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({
        status: 'complete',
        result: { report_html: '/reports/RUN-PASTE/log.html', logs: 'Results: 1 passed, 0 failed' },
        test_status: 'passed',
      })
    })
    renderPage()
    typeQuery('this text was never used to generate anything')
    pasteCode()

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    await waitFor(() => expect(mockStreamSSE).toHaveBeenCalledWith(
      '/execute-test',
      { robot_code: CODE, user_query: null, workflow_id: null },
      expect.any(Function),
    ))
    await screen.findByText('Test passed! 🎉')
    // Paste-and-execute has no query, so there is nothing to attribute a
    // correction to — the panel must not mount (and so must never fetch).
    expect(mockApi).not.toHaveBeenCalled()
  })

  it.each([
    ['Ctrl', { ctrlKey: true }],
    ['Cmd', { metaKey: true }],
  ])('%s+Enter inside the code editor runs the pasted code too, from the keyboard alone', async (_label, modifier) => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({
        status: 'complete',
        result: { report_html: '/reports/RUN-PASTE-KBD/log.html', logs: 'Results: 1 passed, 0 failed' },
        test_status: 'passed',
      })
    })
    renderPage()
    pasteCode()

    // The Run button's own click is covered by the test above; this fires
    // the same handleRun through the code editor's own Ctrl/Cmd+Enter
    // shortcut instead — the handler now lives directly on the editor's
    // textarea (RobotCodeEditor forwards onKeyDown to it), not a wrapping
    // div, so this exercises the real, current wiring rather than bubbling.
    fireEvent.keyDown(screen.getByPlaceholderText(/Generated code appears here/), { key: 'Enter', ...modifier })

    await waitFor(() => expect(mockStreamSSE).toHaveBeenCalledWith(
      '/execute-test',
      { robot_code: CODE, user_query: null, workflow_id: null },
      expect.any(Function),
    ))
    await screen.findByText('Test passed! 🎉')
  })
})

describe('GeneratePage — a completed execution', () => {
  it('shows a per-test breakdown and the failing step’s message', async () => {
    allowFeedbackMount()
    const logs = [
      'Test: Login flow - PASS',
      'Test: Checkout flow - FAIL',
      'Error: Element not found: #submit',
      'Results: 1 passed, 1 failed',
    ].join('\n')
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
      else if (path === '/execute-test') {
        onEvent({ status: 'complete', result: { report_html: '/reports/RUN-1/log.html', logs }, test_status: 'failed' })
      }
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    expect(await screen.findByText('1 of 2 tests failed')).toBeInTheDocument()
    expect(screen.getByText('Login flow')).toBeInTheDocument()
    expect(screen.getByText('Checkout flow')).toBeInTheDocument()
    expect(screen.getByText(/Element not found: #submit/)).toBeInTheDocument()
  })

  it('derives pass/fail counts by counting per-test lines when the summary has no Results: line', async () => {
    // Every other fixture in this file includes a "Results: N passed, M
    // failed" line, so the count always comes from that regex match. This
    // one omits it on purpose, forcing the fallback that counts parsed
    // per-test lines instead — 2 passes and 1 fail, deliberately unequal so
    // a mixed-up PASS/FAIL count would show up as a wrong total.
    allowFeedbackMount()
    const logs = [
      'Test: Login flow - PASS',
      'Test: Search flow - PASS',
      'Test: Checkout flow - FAIL',
      'Error: Element not found: #submit',
    ].join('\n')
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
      else if (path === '/execute-test') {
        onEvent({ status: 'complete', result: { report_html: '/reports/RUN-2/log.html', logs }, test_status: 'failed' })
      }
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    expect(await screen.findByText('1 of 3 tests failed')).toBeInTheDocument()
  })

  it('says "Test failed" (not "N of M") for a single failing test', async () => {
    allowFeedbackMount()
    const logs = [
      'Test: Checkout flow - FAIL',
      'Error: Element not found: #submit',
      'Results: 0 passed, 1 failed',
    ].join('\n')
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
      else if (path === '/execute-test') {
        onEvent({ status: 'complete', result: { report_html: '/reports/RUN-5/log.html', logs }, test_status: 'failed' })
      }
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    expect(await screen.findByText('Test failed')).toBeInTheDocument()
  })

  it('says "All tests passed!" (not the single-test wording) for a multi-test run with no failures', async () => {
    allowFeedbackMount()
    const logs = [
      'Test: Login flow - PASS',
      'Test: Search flow - PASS',
      'Results: 2 passed, 0 failed',
    ].join('\n')
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
      else if (path === '/execute-test') {
        onEvent({ status: 'complete', result: { report_html: '/reports/RUN-3/log.html', logs }, test_status: 'passed' })
      }
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    expect(await screen.findByText('All tests passed! 🎉')).toBeInTheDocument()
  })

  it('falls back to "Test execution failed" when a failing run reports no parseable summary', async () => {
    allowFeedbackMount()
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
      else if (path === '/execute-test') {
        onEvent({ status: 'complete', result: { report_html: '/reports/RUN-4/log.html' }, test_status: 'failed' })
      }
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    expect(await screen.findByText('Test execution failed')).toBeInTheDocument()
  })

  it('shows the SSE error message and no result card on status: error', async () => {
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
      else if (path === '/execute-test') onEvent({ status: 'error', message: 'Docker daemon unreachable' })
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    await waitFor(() => expect(screen.getAllByText('Docker daemon unreachable')).toHaveLength(2))
    expect(screen.queryByText(/Test passed|tests failed|Test failed/)).toBeNull()
  })
})

describe('GeneratePage — which run a correction or download is attributed to', () => {
  it('attributes feedback to the EXECUTED run’s id from the report URL, not the generation’s internal workflow id', async () => {
    // The two ids are different on purpose: workflow_id is CrewAI's
    // internal handle from the generation call; the report URL carries the
    // DB run_id the execute call actually produced. /api/feedback/{id}
    // must be the second one.
    allowFeedbackMount()
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'WF-INTERNAL-999' })
      else if (path === '/execute-test') {
        onEvent({
          status: 'complete',
          result: { report_html: '/reports/RUN-REAL-123/log.html', logs: 'Results: 1 passed, 0 failed' },
          test_status: 'passed',
        })
      }
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    await waitFor(() => expect(mockApi).toHaveBeenCalledWith('/api/feedback/RUN-REAL-123', { cache: 'no-store' }))
    expect(mockApi).not.toHaveBeenCalledWith('/api/feedback/WF-INTERNAL-999', expect.anything())
  })

  it('severs attribution once the editor is cleared — a run pasted afterwards is sent with no workflow id or query', async () => {
    // "Clearing the editor severs the link to the last generation" per the
    // RobotCodeEditor onChange handler's own comment. Generate once, wipe
    // the box, paste different code, and the execute call for THAT code
    // must not carry the old generation's workflow_id — otherwise a
    // correction typed against the NEW code would land on the OLD run.
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'WF-OLD' })
      else if (path === '/execute-test') return new Promise(() => {})
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    pasteCode('')
    pasteCode('*** Settings ***\nLibrary    Browser\n')

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    await waitFor(() => expect(mockStreamSSE).toHaveBeenCalledWith(
      '/execute-test',
      expect.objectContaining({ workflow_id: null, user_query: null }),
      expect.any(Function),
    ))
  })
})

describe('GeneratePage — New Test', () => {
  it('clears immediately, with no confirm prompt, when only a query is typed (no code yet)', () => {
    const confirmSpy = vi.spyOn(window, 'confirm')
    renderPage()
    typeQuery()

    fireEvent.click(screen.getByRole('button', { name: /New Test/ }))

    expect(confirmSpy).not.toHaveBeenCalled()
    expect(screen.getByPlaceholderText(/Open Google, search for/)).toHaveValue('')
    confirmSpy.mockRestore()
  })

  it('asks for confirmation once code exists, and clears nothing when the user cancels', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false)

    fireEvent.click(screen.getByRole('button', { name: /New Test/ }))

    expect(confirmSpy).toHaveBeenCalledWith('This will clear your current test. Start a new test?')
    expect(screen.getByPlaceholderText(/Generated code appears here/)).toHaveValue(CODE)
    expect(screen.getByPlaceholderText(/Open Google, search for/)).toHaveValue(QUERY)
    confirmSpy.mockRestore()
  })

  it('clears query, code and logs once the user confirms', async () => {
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true)

    fireEvent.click(screen.getByRole('button', { name: /New Test/ }))

    expect(screen.getByPlaceholderText(/Open Google, search for/)).toHaveValue('')
    expect(screen.getByPlaceholderText(/Generated code appears here/)).toHaveValue('')
    expect(screen.queryByRole('button', { name: /New Test/ })).toBeNull()
    confirmSpy.mockRestore()
  })
})

describe('GeneratePage — copy and export', () => {
  it('copies the exact generated code, not the query text', async () => {
    const writeText = stubClipboard()
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Copy/ }))

    expect(writeText).toHaveBeenCalledWith(CODE)
    expect(writeText).not.toHaveBeenCalledWith(QUERY)
  })

  it('downloads a .robot file whose content is the exact code on screen', async () => {
    const { getParts, getAnchor } = stubDownload()
    mockStreamSSE.mockImplementation(async (_path, _body, onEvent) => {
      onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Export/ }))

    expect(getAnchor()?.download).toBe('test.robot')
    expect(getParts()).toEqual([CODE])
  })
})

describe('GeneratePage — prefilled from History’s Regenerate', () => {
  it('fills the query from router state, then clears that state so a refresh starts clean', () => {
    const replaceStateSpy = vi.spyOn(window.history, 'replaceState')

    renderPage({ prefillQuery: 'search flipkart for shoes' })

    expect(screen.getByPlaceholderText(/Open Google, search for/)).toHaveValue('search flipkart for shoes')
    // Verify the state is actually cleared: replaceState called with empty state and empty title.
    expect(replaceStateSpy).toHaveBeenCalledWith({}, '')
    replaceStateSpy.mockRestore()
  })
})

describe('GeneratePage — while executing', () => {
  it('shows a live "running" strip and disables the editor', async () => {
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') { onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' }); return }
      return new Promise(() => {}) // execute-test hangs — stay "executing"
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })

    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))

    expect(await screen.findByText('Running your scenario')).toBeInTheDocument()
    expect(screen.getByPlaceholderText(/Generated code appears here/)).toBeDisabled()
    expect(screen.getByRole('button', { name: /Executing…/ })).toBeDisabled()
  })
})

describe('GeneratePage — logs after a run', () => {
  it('auto-collapses both log sections once the run completes, and Expand reveals one again', async () => {
    allowFeedbackMount()
    mockStreamSSE.mockImplementation(async (path, _body, onEvent) => {
      if (path === '/generate-test') onEvent({ status: 'complete', robot_code: CODE, workflow_id: 'wf-1' })
      else if (path === '/execute-test') {
        onEvent({
          status: 'complete',
          result: { report_html: '/reports/RUN-1/log.html', logs: 'Results: 1 passed, 0 failed' },
          test_status: 'passed',
        })
      }
    })
    renderPage()
    typeQuery()
    fireEvent.click(screen.getByRole('button', { name: /Generate Test/ }))
    await screen.findByRole('button', { name: /^Run Test/ })
    fireEvent.click(screen.getByRole('button', { name: /^Run Test/ }))
    await screen.findByText('Test passed! 🎉')

    const expandButtons = screen.getAllByRole('button', { name: 'Expand' })
    expect(expandButtons).toHaveLength(2) // Generation Logs + Execution Logs

    fireEvent.click(expandButtons[0])

    expect(screen.getAllByRole('button', { name: 'Expand' })).toHaveLength(1)
    expect(screen.getByRole('button', { name: 'Collapse' })).toBeInTheDocument()
  })
})
