/**
 * AppHeader — the breadcrumb page label and the two theme controls
 * (light/dark/system mode, and the professional/neo visual toggle). Both
 * controls are built from a .map() over three/two near-identical buttons, so
 * the failure mode worth guarding is a button calling back with the WRONG
 * id/theme — a neighbour's instead of its own. PAGE_LABELS is a hand
 * -maintained path->label table; its fallback branch, and a gap in the table
 * that predates this test file, are pinned below.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/components/theme-provider', () => ({ useTheme: vi.fn() }))

import { useTheme } from '@/components/theme-provider'
import { SidebarProvider } from '@/components/ui/sidebar'
import { AppHeader } from './app-header'

const mockUseTheme = vi.mocked(useTheme)

// SidebarProvider's useIsMobile() calls window.matchMedia via a plain
// useEffect. setup.ts installs a working stub once per FILE, but this file's
// own `afterEach(vi.resetAllMocks)` strips that stub's implementation after
// test 1 (resetAllMocks resets every vi.fn(), including that one), so every
// test after the first would otherwise call matchMedia() and get back
// undefined. theme-provider.test.tsx hits the same interaction and re-installs
// its own stub per test for the same reason — same fix here.
beforeEach(() => {
  window.matchMedia = vi.fn().mockImplementation((query: string) => ({
    matches: false, media: query, onchange: null,
    addEventListener: vi.fn(), removeEventListener: vi.fn(),
    addListener: vi.fn(), removeListener: vi.fn(), dispatchEvent: vi.fn(),
  }))
})
afterEach(() => vi.resetAllMocks())

function renderAt(path: string, mode: 'light' | 'dark' | 'system', theme: 'professional' | 'neo') {
  const setMode = vi.fn()
  const setTheme = vi.fn()
  mockUseTheme.mockReturnValue({ mode, setMode, theme, setTheme })
  render(
    <MemoryRouter initialEntries={[path]}>
      {/* AppHeader renders <SidebarTrigger>, which reads useSidebar() —
          a real (not mocked) SidebarProvider is the cheapest way to satisfy
          that without re-implementing the sidebar's own context. */}
      <SidebarProvider>
        <AppHeader />
      </SidebarProvider>
    </MemoryRouter>,
  )
  return { setMode, setTheme }
}

describe('AppHeader — breadcrumb page label', () => {
  it.each([
    ['/generate', 'Generate'],
    ['/history', 'Test Runs'],
    ['/metrics', 'Metrics'],
    ['/learning', 'Learning'],
    ['/settings', 'Settings'],
    ['/docs', 'Documentation'],
  ])('labels %s as "%s"', (path, label) => {
    renderAt(path, 'system', 'professional')
    expect(screen.getByText('Platform')).toBeInTheDocument()
    expect(screen.getByText(label)).toBeInTheDocument()
  })

  it('falls back to Overview for a path with no PAGE_LABELS entry', () => {
    renderAt('/some-unmapped-path', 'system', 'professional')
    expect(screen.getByText('Overview')).toBeInTheDocument()
  })

  // PAGE_LABELS was never extended when /access and /team shipped — App.tsx's
  // PAGES table gates both (see App.test.tsx) but neither path is a key in
  // this component's PAGE_LABELS map. Pinning what actually happens today,
  // not endorsing it: reported in the task write-up, not fixed here (surgical
  // -diff / no-source-edits rule — this file only adds tests).
  it('SURPRISING: /team has no PAGE_LABELS entry either, so it also falls back to Overview', () => {
    renderAt('/team', 'system', 'professional')
    expect(screen.getByText('Overview')).toBeInTheDocument()
  })
})

describe('AppHeader — light/dark/system toggle', () => {
  it('calls setMode with the id of the button that was clicked, not a neighbour', () => {
    const { setMode } = renderAt('/generate', 'system', 'professional')

    fireEvent.click(screen.getByLabelText('Dark mode'))

    expect(setMode).toHaveBeenCalledWith('dark')
    expect(setMode).not.toHaveBeenCalledWith('light')
    expect(setMode).not.toHaveBeenCalledWith('system')
  })

  it('marks only the current mode button as active', () => {
    renderAt('/generate', 'dark', 'professional')

    expect(screen.getByLabelText('Dark mode').className).toContain('bg-background')
    expect(screen.getByLabelText('Light mode').className).not.toContain('bg-background')
    expect(screen.getByLabelText('System preference').className).not.toContain('bg-background')
  })
})

describe('AppHeader — professional/neo toggle', () => {
  it('offers Neo (the OTHER theme) while professional, and switches to it on click', () => {
    const { setTheme } = renderAt('/generate', 'system', 'professional')
    const btn = screen.getByLabelText('Toggle neobrutalism theme')

    expect(btn.textContent).toContain('Neo')
    fireEvent.click(btn)

    expect(setTheme).toHaveBeenCalledWith('neo')
  })

  it('offers Pro (the OTHER theme) while neo, and switches back to professional on click', () => {
    const { setTheme } = renderAt('/generate', 'system', 'neo')
    const btn = screen.getByLabelText('Toggle neobrutalism theme')

    expect(btn.textContent).toContain('Pro')
    fireEvent.click(btn)

    expect(setTheme).toHaveBeenCalledWith('professional')
  })
})
