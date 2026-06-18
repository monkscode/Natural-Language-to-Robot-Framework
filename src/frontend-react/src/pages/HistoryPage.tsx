/**
 * Test History — the framework's reuse surface, not just a log.
 *
 * List: GET /api/history (role-scoped server-side: users see own runs,
 * admins see everyone's). Clicking a row opens a detail drawer
 * (GET /api/history/{id}) with the original query and the stored Robot code.
 *
 * "Run again" re-executes that stored code verbatim via
 * POST /execute-test {rerun_of} — a fresh run attributed to the clicker,
 * with NO LLM cost and learning deliberately skipped (the first execution
 * already recorded the query→code evidence). "Regenerate" prefills Generate
 * instead, for when the site changed and the stored locators went stale.
 */
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Separator } from '@/components/ui/separator'
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from '@/components/ui/sheet'
import {
  Check, ChevronRight, Copy, Download, Play, RefreshCw, Repeat2, RotateCw, FileTerminal, Search,
} from 'lucide-react'
import { api } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import { useFetch } from '@/lib/useFetch'

type RunStatus = 'generated' | 'running' | 'passed' | 'failed' | 'error'

interface Run {
  run_id: string
  status: RunStatus
  user_query: string | null
  user_email?: string | null
  // Original run this row was re-run from (root-flattened server-side).
  // Feedback on a re-run is applied to that original run's learning record.
  rerun_of?: string | null
  created_at: string
  updated_at: string
  has_report: boolean
}

interface RunDetail extends Run {
  robot_code: string | null
}

interface HistoryResponse {
  runs: Run[]
  total: number
  scope: 'own' | 'all'
}

const PAGE = 100

const STATUS_BADGE: Record<RunStatus, JSX.Element> = {
  passed: <Badge className="bg-green-100 text-green-700 border-green-200 hover:bg-green-100 text-xs">Passed</Badge>,
  failed: <Badge className="bg-red-100 text-red-700 border-red-200 hover:bg-red-100 text-xs">Failed</Badge>,
  error:  <Badge className="bg-amber-100 text-amber-700 border-amber-200 hover:bg-amber-100 text-xs">Error</Badge>,
  generated: <Badge variant="outline" className="text-xs">Generated</Badge>,
  running: (
    <Badge variant="secondary" className="gap-1.5 text-xs">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
      <span>Running</span>
    </Badge>
  ),
}

const FILTERS = ['all', 'passed', 'failed', 'generated', 'error'] as const
type Filter = (typeof FILTERS)[number]

function formatDate(iso: string): string {
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString([], {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  })
}

/** "2h ago" for the table; the exact stamp lives in the cell tooltip. */
function timeAgo(iso: string): string {
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

export default function HistoryPage() {
  const navigate = useNavigate()

  const [runs, setRuns] = useState<Run[]>([])
  const [total, setTotal] = useState(0)
  const [scope, setScope] = useState<'own' | 'all'>('own')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [filter, setFilter] = useState<Filter>('all')
  // search is the raw input; debouncedSearch is what the server query keys off,
  // so each keystroke doesn't fire a request.
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  // Re-run bookkeeping, keyed by the SOURCE run id: which reruns are in
  // flight (disables their Play buttons) and the latest live status line
  // (shown in the drawer).
  const [inFlight, setInFlight] = useState<Set<string>>(new Set())
  const [rerunNote, setRerunNote] = useState<Record<string, string>>({})
  const [copied, setCopied] = useState<string | null>(null)

  // Status AND text search are both SERVER-side: each tab fetches, counts and
  // paginates only its matching rows, so "Load more (N older)" and the "N of M"
  // counter are that filter+search's truth — search spans ALL of the user's
  // runs, not just the page already loaded.
  const queryString = useCallback((offset: number) => {
    const statusParam = filter === 'all' ? '' : `&status=${filter}`
    const qParam = debouncedSearch ? `&q=${encodeURIComponent(debouncedSearch)}` : ''
    return `/api/history?limit=${PAGE}&offset=${offset}${statusParam}${qParam}`
  }, [filter, debouncedSearch])

  // silent: refresh the rows in place without the "Loading runs…" placeholder
  // (background refetches while the table is already populated).
  const loadRuns = useCallback(async (offset: number, append: boolean, silent = false) => {
    if (!append && !silent) setLoading(true)
    setError('')
    try {
      const page = await api<HistoryResponse>(queryString(offset))
      setScope(page.scope)
      setTotal(page.total)
      setRuns(prev => {
        if (!append) return page.runs
        // Rows created between page loads shift offsets — dedupe on append.
        const seen = new Set(prev.map(r => r.run_id))
        return [...prev, ...page.runs.filter(r => !seen.has(r.run_id))]
      })
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load history')
    } finally {
      setLoading(false)
    }
  }, [queryString])

  // Refresh after a re-run WITHOUT collapsing pagination: re-fetch only page
  // zero, merge fresh rows over the loaded set (updating statuses, surfacing
  // the new run at the top) and keep any older pages the user had loaded.
  const refreshLoaded = useCallback(async () => {
    try {
      const page = await api<HistoryResponse>(queryString(0))
      setScope(page.scope)
      setTotal(page.total)
      setRuns(prev => {
        const freshIds = new Set(page.runs.map(r => r.run_id))
        const tail = prev.filter(r => !freshIds.has(r.run_id))
        return [...page.runs, ...tail]
      })
    } catch { /* a transient refresh failure leaves the existing rows in place */ }
  }, [queryString])

  // Debounce the search box into the server query (and reset to page zero).
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(t)
  }, [search])

  // Initial load + refetch from page zero whenever the tab or search changes.
  useEffect(() => { void loadRuns(0, false) }, [loadRuns])

  const detailPath = selected ? `/api/history/${selected}` : null
  const { data: detail, error: detailError } = useFetch<RunDetail>(detailPath)
  // useFetch keeps stale data during a refetch, so a just-clicked row would
  // briefly render the PREVIOUS run's code/query. Only trust detail once it
  // matches the open row.
  const d = detail && detail.run_id === selected ? detail : null

  const isAdminScope = scope === 'all'
  const visible = runs  // filtering is server-side now

  const runAgain = useCallback(async (sourceId: string) => {
    setSelected(sourceId) // feedback lives in the drawer
    setInFlight(prev => new Set(prev).add(sourceId))
    const note = (text: string) => setRerunNote(prev => ({ ...prev, [sourceId]: text }))
    note('Starting re-run of the stored code…')
    let listRefreshed = false
    try {
      await streamSSE('/execute-test', { rerun_of: sourceId }, ev => {
        // First event = the new run row exists; surface it at the top
        // (the table is populated and the stream is still going).
        if (!listRefreshed) { listRefreshed = true; void refreshLoaded() }
        if (ev.test_status === 'passed') note('✓ Re-run passed')
        else if (ev.test_status === 'failed') note('✗ Re-run failed — open its report from the new row on top')
        else if (ev.status === 'error') note(`Error: ${ev.message || 'execution failed'}`)
        else if (typeof ev.message === 'string' && ev.message) note(ev.message)
      })
    } catch (e) {
      note(e instanceof Error ? e.message : 'Failed to start the re-run')
    } finally {
      setInFlight(prev => { const next = new Set(prev); next.delete(sourceId); return next })
      void refreshLoaded()
    }
  }, [refreshLoaded])

  const copyText = useCallback(async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      setTimeout(() => setCopied(null), 1500)
    } catch { /* clipboard unavailable — non-fatal */ }
  }, [])

  const downloadCode = useCallback((code: string, runId: string) => {
    const url = URL.createObjectURL(new Blob([code], { type: 'text/plain' }))
    const a = document.createElement('a')
    a.href = url
    a.download = `test-${runId.slice(0, 8)}.robot`
    a.click()
    URL.revokeObjectURL(url)
  }, [])

  const selectedNote = selected ? rerunNote[selected] : undefined
  const rerunDisabled = !d?.robot_code || !selected || inFlight.has(selected) || d?.status === 'running'

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-5 flex items-end justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Test History</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {isAdminScope
              ? 'All users’ test runs (admin view) — click any run to view its script and details'
              : 'Click any run to view its script and re-run it as-is — no regeneration time, no LLM cost'}
          </p>
        </div>
        <Button size="sm" variant="outline" className="h-7 text-xs gap-1.5" onClick={() => loadRuns(0, false)}>
          <RefreshCw className="h-3 w-3" /> Refresh
        </Button>
      </div>

      <Card>
        <CardHeader className="gap-3 space-y-0 pb-3 px-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-1.5">
            {FILTERS.map(f => (
              <Button
                key={f}
                size="sm"
                variant={filter === f ? 'default' : 'outline'}
                className="h-7 w-24 text-xs capitalize"
                onClick={() => setFilter(f)}
              >
                {f === 'all' ? 'All Runs' : f}
              </Button>
            ))}
          </div>
          <div className="flex items-center gap-3">
            <div className="relative">
              <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                value={search}
                onChange={e => setSearch(e.target.value)}
                placeholder={isAdminScope ? 'Search description, user or id…' : 'Search description or id…'}
                className="h-7 w-56 pl-8 text-xs"
              />
            </div>
            <span className="whitespace-nowrap text-xs text-muted-foreground">
              {visible.length} of {total} run{total === 1 ? '' : 's'}
            </span>
          </div>
        </CardHeader>

        <CardContent className="p-0">
          {/* Full-page scroll (industry-standard runs list): the table grows
              with content and the PAGE scrolls; the header row stays pinned
              via sticky. min-h keeps short/empty filter results from
              collapsing the card. Column geometry is constant (table-fixed),
              so switching filters only changes the values. */}
          <div className="min-h-[420px]">
          {loading && (
            <p className="flex min-h-[420px] items-center justify-center text-sm text-muted-foreground">Loading runs…</p>
          )}
          {!loading && error && (
            <p className="flex min-h-[420px] items-center justify-center text-sm text-destructive">{error}</p>
          )}
          {!loading && !error && visible.length === 0 && (
            <p className="flex min-h-[420px] items-center justify-center text-sm text-muted-foreground">
              {debouncedSearch
                ? 'No runs match your search.'
                : filter === 'all'
                  ? 'No test runs yet — generate your first test from the Generate page.'
                  : `No ${filter} runs yet.`}
            </p>
          )}

          {!loading && !error && visible.length > 0 && (
            <>
              {/* table-fixed: column widths are set here once and never
                  recomputed from row content, so switching status filters
                  keeps the exact same grid — only the values change. */}
              <table className="w-full table-fixed text-sm">
                <thead className="sticky top-0 z-10 bg-background shadow-[inset_0_-1px_0_hsl(var(--border))]">
                  <tr className="bg-muted/40">
                    <th className="w-28 py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Status</th>
                    <th className="py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Description</th>
                    {isAdminScope && (
                      <th className="w-44 py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground hidden lg:table-cell">User</th>
                    )}
                    <th className="w-[320px] py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground hidden md:table-cell">ID</th>
                    <th className="w-24 py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground hidden sm:table-cell">When</th>
                    <th className="w-20 py-2.5 px-4"></th>
                    <th className="w-9 py-2.5 pr-3"></th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map(row => (
                    <tr
                      key={row.run_id}
                      className="group cursor-pointer border-b last:border-0 hover:bg-muted/30 transition-colors"
                      onClick={() => setSelected(row.run_id)}
                    >
                      <td className="py-3 px-4">{STATUS_BADGE[row.status] ?? row.status}</td>
                      <td className="py-3 px-4">
                        <div className="flex items-center gap-2">
                          {row.rerun_of && (
                            <button
                              type="button"
                              className="shrink-0"
                              title={`Re-run of ${row.rerun_of} — click to open the original run`}
                              onClick={e => { e.stopPropagation(); setSelected(row.rerun_of!) }}
                            >
                              {/* Same pill family as the status badges so it
                                  reads as a first-class chip, not a footnote. */}
                              <Badge className="gap-1 border-blue-200 bg-blue-100 text-xs text-blue-700 hover:bg-blue-200">
                                <Repeat2 className="h-3 w-3" />
                                <span>Re-run</span>
                              </Badge>
                            </button>
                          )}
                          <span className="line-clamp-1 text-sm" title={row.user_query ?? undefined}>
                            {row.user_query || <span className="text-muted-foreground italic">Pasted code run</span>}
                          </span>
                        </div>
                      </td>
                      {isAdminScope && (
                        <td className="py-3 px-4 text-xs text-muted-foreground hidden lg:table-cell">
                          {row.user_email ? (
                            <button
                              type="button"
                              className="block max-w-full truncate text-left hover:text-foreground hover:underline"
                              title="Filter the list by this user"
                              onClick={e => { e.stopPropagation(); setSearch(row.user_email!) }}
                            >
                              {row.user_email}
                            </button>
                          ) : '—'}
                        </td>
                      )}
                      <td className="py-3 px-4 hidden md:table-cell">
                        <button
                          type="button"
                          className="whitespace-nowrap font-mono text-xs text-muted-foreground hover:text-foreground"
                          title="Click to copy the run id"
                          onClick={e => { e.stopPropagation(); void copyText(row.run_id, `id-${row.run_id}`) }}
                        >
                          {row.run_id}{copied === `id-${row.run_id}` ? ' ✓' : ''}
                        </button>
                      </td>
                      <td
                        className="py-3 px-4 text-xs text-muted-foreground hidden sm:table-cell whitespace-nowrap"
                        title={formatDate(row.created_at)}
                      >
                        {timeAgo(row.created_at)}
                      </td>
                      <td className="py-3 px-4">
                        <div className="flex gap-1 justify-end">
                          <Button
                            variant="ghost"
                            size="icon"
                            className="h-7 w-7"
                            title="Run again — execute the saved code as-is (no regeneration)"
                            disabled={inFlight.has(row.run_id) || row.status === 'running'}
                            onClick={e => { e.stopPropagation(); void runAgain(row.run_id) }}
                          >
                            <Play className="h-3.5 w-3.5" />
                          </Button>
                          {row.has_report ? (
                            <Button asChild variant="ghost" size="icon" className="h-7 w-7" title="View report">
                              <a
                                href={`/reports/${row.run_id}/log.html`}
                                target="_blank"
                                rel="noreferrer"
                                onClick={e => e.stopPropagation()}
                              >
                                <FileTerminal className="h-3.5 w-3.5" />
                              </a>
                            </Button>
                          ) : (
                            <Button variant="ghost" size="icon" className="h-7 w-7 opacity-40 pointer-events-none" title="No report for this run">
                              <FileTerminal className="h-3.5 w-3.5" />
                            </Button>
                          )}
                        </div>
                      </td>
                      <td className="py-3 pr-3">
                        {/* Disclosure affordance: signals the row opens a
                            detail view, brightening + nudging on hover. */}
                        <ChevronRight className="h-4 w-4 text-muted-foreground/40 transition-all group-hover:translate-x-0.5 group-hover:text-foreground" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {runs.length < total && (
                <div className="border-t px-4 py-3 text-center">
                  <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => loadRuns(runs.length, true)}>
                    Load more ({total - runs.length} older)
                  </Button>
                </div>
              )}
            </>
          )}
          </div>
        </CardContent>
      </Card>

      <Sheet open={!!selected} onOpenChange={open => { if (!open) setSelected(null) }}>
        <SheetContent className="flex w-full flex-col gap-4 overflow-y-auto sm:max-w-2xl">
          <SheetHeader className="space-y-2 pr-6 text-left">
            <div className="flex items-center gap-2">
              {d && STATUS_BADGE[d.status]}
              {d?.rerun_of && (
                <Badge className="gap-1 border-blue-200 bg-blue-100 text-xs text-blue-700 hover:bg-blue-100">
                  <Repeat2 className="h-3 w-3" />
                  <span>Re-run</span>
                </Badge>
              )}
              {isAdminScope && d?.user_email && (
                <span className="text-xs text-muted-foreground">{d.user_email}</span>
              )}
            </div>
            <SheetTitle className="text-base leading-snug">
              {d?.user_query || 'Pasted code run'}
            </SheetTitle>
            <SheetDescription className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
              {selected && (
                <button
                  type="button"
                  className="inline-flex items-center gap-1 font-mono hover:text-foreground"
                  title="Copy run id"
                  onClick={() => void copyText(selected, 'drawer-id')}
                >
                  {selected}
                  {copied === 'drawer-id' ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                </button>
              )}
              {d && <span>created {formatDate(d.created_at)}</span>}
              {d && d.updated_at !== d.created_at && (
                <span>last update {formatDate(d.updated_at)}</span>
              )}
              {d?.rerun_of && (
                <span className="basis-full">
                  re-run of{' '}
                  <button
                    type="button"
                    className="font-mono hover:text-foreground hover:underline"
                    title="Open the original run — feedback on this re-run applies to it"
                    onClick={() => setSelected(d.rerun_of!)}
                  >
                    {d.rerun_of}
                  </button>
                </span>
              )}
            </SheetDescription>
          </SheetHeader>

          {selectedNote && (
            <p className="rounded-md border bg-muted/40 px-3 py-2 text-xs">{selectedNote}</p>
          )}

          <div className="flex flex-wrap gap-2">
            <Button
              size="sm"
              className="h-8 gap-1.5 text-xs"
              disabled={rerunDisabled}
              title={d?.robot_code
                ? 'Execute this exact saved code as a new run — no regeneration, no LLM cost'
                : 'No stored code for this run'}
              onClick={() => selected && void runAgain(selected)}
            >
              <Play className="h-3.5 w-3.5" /> Run again
            </Button>
            {d?.has_report && (
              <Button asChild size="sm" variant="outline" className="h-8 gap-1.5 text-xs">
                <a href={`/reports/${d.run_id}/log.html`} target="_blank" rel="noreferrer">
                  <FileTerminal className="h-3.5 w-3.5" /> Open report
                </a>
              </Button>
            )}
            {d?.user_query && (
              <Button
                size="sm"
                variant="outline"
                className="h-8 gap-1.5 text-xs"
                title="Start a fresh generation from this description (for when the site changed)"
                onClick={() => navigate('/generate', { state: { prefillQuery: d.user_query } })}
              >
                <RotateCw className="h-3.5 w-3.5" /> Regenerate
              </Button>
            )}
          </div>

          <Separator />

          <div className="flex min-h-0 flex-1 flex-col gap-2">
            <div className="flex items-center justify-between">
              <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Robot code</span>
              {d?.robot_code && (
                <div className="flex gap-1">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    title="Copy code"
                    onClick={() => void copyText(d.robot_code!, 'drawer-code')}
                  >
                    {copied === 'drawer-code' ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-7 w-7"
                    title="Download .robot file"
                    onClick={() => downloadCode(d.robot_code!, d.run_id)}
                  >
                    <Download className="h-3.5 w-3.5" />
                  </Button>
                </div>
              )}
            </div>

            {/* d is null while the fetch is pending OR still resolving a row
                switch (useFetch keeps stale data), so gate the body on d to
                avoid flashing the previous run's code. */}
            {detailError ? (
              <p className="py-6 text-center text-xs text-destructive">{detailError}</p>
            ) : !d ? (
              <p className="py-6 text-center text-xs text-muted-foreground">Loading…</p>
            ) : d.robot_code ? (
              <pre className="min-h-0 flex-1 overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
                {d.robot_code}
              </pre>
            ) : (
              <p className="rounded-md border border-dashed px-3 py-6 text-center text-xs italic text-muted-foreground">
                No stored code — this run predates code persistence.
                {d.user_query ? ' Use Regenerate to produce it again.' : ''}
              </p>
            )}
          </div>
        </SheetContent>
      </Sheet>
    </div>
  )
}
