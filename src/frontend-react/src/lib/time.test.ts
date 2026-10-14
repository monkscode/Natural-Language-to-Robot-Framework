/**
 * lib/time — the two timestamp renderings every list page shares.
 *
 * Moved out of HistoryPage when the Tests page became their second user: two
 * copies of "2h ago" are two rules that drift, and a list whose rows say
 * "3d ago" beside another list saying "72h ago" for the same moment is the
 * disagreement that produces.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { formatDate, timeAgo } from './time'

const NOW = new Date('2026-09-11T12:00:00Z')

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(NOW)
})
afterEach(() => vi.useRealTimers())

const ago = (ms: number) => new Date(NOW.getTime() - ms).toISOString()
const MIN = 60_000

describe('timeAgo', () => {
  it('reads "just now" under a minute', () => {
    expect(timeAgo(ago(30_000))).toBe('just now')
  })

  it('counts minutes, then hours, then days', () => {
    expect(timeAgo(ago(5 * MIN))).toBe('5m ago')
    expect(timeAgo(ago(3 * 60 * MIN))).toBe('3h ago')
    expect(timeAgo(ago(2 * 24 * 60 * MIN))).toBe('2d ago')
  })

  it('falls back to a calendar date from a week out', () => {
    const iso = ago(8 * 24 * 60 * MIN)
    expect(timeAgo(iso)).toBe(new Date(iso).toLocaleDateString([], {
      year: 'numeric', month: '2-digit', day: '2-digit',
    }))
  })

  it('hands back an unparseable value untouched rather than "NaN ago"', () => {
    expect(timeAgo('not a date')).toBe('not a date')
  })
})

describe('formatDate', () => {
  it('renders a parseable stamp in the viewer’s locale', () => {
    const iso = '2026-09-10T08:30:00Z'
    expect(formatDate(iso)).toBe(new Date(iso).toLocaleString([], {
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit',
    }))
  })

  it('hands back an unparseable value untouched', () => {
    expect(formatDate('not a date')).toBe('not a date')
  })
})
