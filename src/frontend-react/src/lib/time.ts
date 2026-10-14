/**
 * Timestamp renderings shared by the list pages.
 *
 * Moved here from HistoryPage when the Tests page became their second user,
 * so both lists say "2h ago" by one rule. Both hand an unparseable value
 * back untouched rather than rendering "Invalid Date" or "NaN ago".
 *
 * Referenced by: pages/HistoryPage.tsx, pages/TestsPage.tsx.
 * Depends on: nothing.
 */

export function formatDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

/** "2h ago" for a table; the exact stamp belongs in the cell tooltip. */
export function timeAgo(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  if (Number.isNaN(ms)) return iso
  const mins = Math.floor(ms / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const hours = Math.floor(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.floor(hours / 24)
  if (days < 7) return `${days}d ago`
  return new Date(iso).toLocaleDateString([], { year: 'numeric', month: '2-digit', day: '2-digit' })
}
