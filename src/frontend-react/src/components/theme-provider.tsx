import React, { createContext, useContext, useEffect, useState } from 'react'

type ColorMode = 'light' | 'dark' | 'system'
type VisualTheme = 'professional' | 'neo'

interface ThemeContextValue {
  mode:     ColorMode
  setMode:  (m: ColorMode) => void
  theme:    VisualTheme
  setTheme: (t: VisualTheme) => void
}

const ThemeContext = createContext<ThemeContextValue>({
  mode:     'system',
  setMode:  () => {},
  theme:    'professional',
  setTheme: () => {},
})

export function ThemeProvider({ children }: { children: React.ReactNode }) {
  const [mode, setModeState] = useState<ColorMode>(
    () => (localStorage.getItem('m1-mode') as ColorMode) ?? 'system',
  )
  const [theme, setThemeState] = useState<VisualTheme>(
    () => (localStorage.getItem('m1-theme') as VisualTheme) ?? 'professional',
  )

  /** Apply / remove the `dark` class and respond to system preference changes */
  useEffect(() => {
    const root = document.documentElement
    const applyDark = (dark: boolean) => root.classList.toggle('dark', dark)

    if (mode === 'dark')  { applyDark(true);  return }
    if (mode === 'light') { applyDark(false); return }

    // system — listen to OS preference
    const mq = window.matchMedia('(prefers-color-scheme: dark)')
    applyDark(mq.matches)
    const handler = (e: MediaQueryListEvent) => applyDark(e.matches)
    mq.addEventListener('change', handler)
    return () => mq.removeEventListener('change', handler)
  }, [mode])

  /** Apply / remove the `neo` class for the neobrutalism override theme */
  useEffect(() => {
    document.documentElement.classList.toggle('neo', theme === 'neo')
  }, [theme])

  function setMode(m: ColorMode) {
    setModeState(m)
    localStorage.setItem('m1-mode', m)
  }

  function setTheme(t: VisualTheme) {
    setThemeState(t)
    localStorage.setItem('m1-theme', t)
  }

  return (
    <ThemeContext.Provider value={{ mode, setMode, theme, setTheme }}>
      {children}
    </ThemeContext.Provider>
  )
}

export const useTheme = () => useContext(ThemeContext)
