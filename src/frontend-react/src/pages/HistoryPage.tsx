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
import { useCallback, useEffect, useRef, useState } from 'react'
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
  Check, ChevronRight, Copy, Download, Folder, FolderInput, ListChecks,
  Play, RefreshCw, Repeat2, RotateCw, FileTerminal, Search,
} from 'lucide-react'
import { api } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import { useFetch } from '@/lib/useFetch'
import { GroupChipsRow } from '@/components/history/GroupChipsRow'
import { MoveToGroupMenu } from '@/components/history/MoveToGroupMenu'
import { useRunGroups } from '@/components/history/RunGroupsContext'
import type { RunGroup } from '@/components/history/useGroups'
import { useAuth } from '@/auth/AuthContext'

type RunStatus = 'generated' | 'running' | 'passed' | 'failed' | 'error'

interface Run {
  run_id: string
  status: RunStatus
  user_query: string | null
  user_email?: string | null
  // Original run this row was re-run from (root-flattened server-side).
  // Feedback on a re-run is applied to that original run's learning record.
  rerun_of?: string | null
  // Server-computed: could THIS caller open that original right now? It is a
  // live question — a peer re-runs a published run and its owner then unfiles
  // it, which un-publishes it. Never re-derive it here; the row does not carry
  // the org id it would need. False renders the pill as a non-interactive
  // badge instead of a link that answers 404.
  rerun_of_accessible?: boolean
  group_id?: string | null
  group_name?: string | null
  created_at: string
  updated_at: string
  has_report: boolean
  // Server-computed: may THIS caller file THIS run? Seeing a run and being
  // able to move it are different questions — a grouped run is visible to
  // the whole org, but only its owner or an org_admin moves it. Never
  // re-derive it here; the row does not carry the org id it would need.
  can_move: boolean
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

  // Select mode drives the bulk "Move N runs to…" flow (checkbox column +
  // toolbar). The group filter itself is NOT local state — it lives in
  // RunGroupsContext because the sidebar's quick-access list sets the same
  // value, and two copies would drift apart.
  const [selectMode, setSelectMode] = useState(false)
  const [checkedIds, setCheckedIds] = useState<Set<string>>(new Set())
  const [moveError, setMoveError] = useState('')

  // Re-run bookkeeping, keyed by the SOURCE run id: which reruns are in
  // flight (disables their Play buttons) and the latest live status line
  // (shown in the drawer).
  const [inFlight, setInFlight] = useState<Set<string>>(new Set())
  const [rerunNote, setRerunNote] = useState<Record<string, string>>({})
  const [copied, setCopied] = useState<string | null>(null)

  // Shared with the sidebar's group quick-access: whichever surface the user
  // picks a group from, both render the same active group.
  const {
    groups, ungroupedCount, refresh: refreshGroups,
    createGroup, renameGroup, deleteGroup, assignRuns,
    groupFilter, setGroupFilter,
  } = useRunGroups()

  // Who is looking. Without an identity every group mutation answers 403, so
  // the controls that could only produce one are not offered at all — the LIST
  // still loads, because GET /api/groups answers 200 with an empty list.
  const { user, isAdmin } = useAuth()

  // Mirrors the server's rules, which differ per action. A hint only — the
  // server 404s any folder the caller may not mutate either way.
  const canRename = useCallback((g: RunGroup) => (
    !!user && (g.created_by === user.id || user.can_manage_org_folders === true)
  ), [user])

  // Narrower on purpose: deleting a folder returns every run inside it to
  // Ungrouped, which un-shares them from the whole org. That consequence is
  // the org's, so the authority is an org-admin's — not the folder creator's.
  //
  // can_manage_org_folders, NOT is_org_admin. The two are different questions:
  // is_org_admin is is_team_admin(), team orgs only, and gates the Team page;
  // folder authority is the org_role claim, which ensure_personal_org grants
  // every user over their own personal org. Reading the wrong one drew no
  // Delete control for any solo user while DELETE /api/groups/{id} answered
  // 204 for them — i.e. for every new signup.
  const canDelete = useCallback((_g: RunGroup) => (
    !!user && user.can_manage_org_folders === true
  ), [user])

  // Status AND text search are both SERVER-side: each tab fetches, counts and
  // paginates only its matching rows, so "Load more (N older)" and the "N of M"
  // counter are that filter+search's truth — search spans ALL of the user's
  // runs, not just the page already loaded.
  const queryString = useCallback((offset: number) => {
    const statusParam = filter === 'all' ? '' : `&status=${filter}`
    const qParam = debouncedSearch ? `&q=${encodeURIComponent(debouncedSearch)}` : ''
    const groupParam = groupFilter ? `&group=${encodeURIComponent(groupFilter)}` : ''
    return `/api/history?limit=${PAGE}&offset=${offset}${statusParam}${qParam}${groupParam}`
  }, [filter, debouncedSearch, groupFilter])

  // Every list fetch takes a ticket; only the newest one may write to state.
  // Switching groups quickly (sidebar → chip → sidebar) fires overlapping
  // requests, and without this the SLOWER response can land last and repaint
  // the table with the previous group's runs.
  const listSeq = useRef(0)

  // silent: refresh the rows in place without the "Loading runs…" placeholder
  // (background refetches while the table is already populated).
  const loadRuns = useCallback(async (offset: number, append: boolean, silent = false) => {
    const seq = ++listSeq.current
    if (!append && !silent) setLoading(true)
    setError('')
    try {
      const page = await api<HistoryResponse>(queryString(offset))
      if (seq !== listSeq.current) return  // superseded — a newer filter won
      setScope(page.scope)
      setTotal(page.total)
      setRuns(prev => {
        if (!append) return page.runs
        // Rows created between page loads shift offsets — dedupe on append.
        const seen = new Set(prev.map(r => r.run_id))
        return [...prev, ...page.runs.filter(r => !seen.has(r.run_id))]
      })
    } catch (e) {
      if (seq !== listSeq.current) return
      setError(e instanceof Error ? e.message : 'Failed to load history')
    } finally {
      // A superseded request must not clear the spinner the newer one raised.
      if (seq === listSeq.current) setLoading(false)
    }
  }, [queryString])

  // Refresh after a re-run WITHOUT collapsing pagination: re-fetch only page
  // zero, merge fresh rows over the loaded set (updating statuses, surfacing
  // the new run at the top) and keep any older pages the user had loaded.
  // dropIds: rows that must NOT survive in the tail — a run moved out of the
  // group currently being filtered is absent from the fresh page and would
  // otherwise linger as a phantom on any older page the user had loaded.
  const refreshLoaded = useCallback(async (dropIds?: Set<string>) => {
    const seq = ++listSeq.current
    try {
      const page = await api<HistoryResponse>(queryString(0))
      if (seq !== listSeq.current) return  // superseded — a newer filter won
      setScope(page.scope)
      setTotal(page.total)
      setRuns(prev => {
        const freshIds = new Set(page.runs.map(r => r.run_id))
        const tail = prev.filter(r => !freshIds.has(r.run_id) && !dropIds?.has(r.run_id))
        return [...page.runs, ...tail]
      })
    } catch { /* a transient refresh failure leaves the existing rows in place */ } finally {
      // Both loaders share ONE ticket, so whoever holds the newest one clears
      // the flag. Without this a refreshLoaded that overtakes an in-flight
      // loadRuns leaves `loading` true forever: loadRuns sees its ticket is
      // stale and skips the clear, and nothing else ever runs.
      if (seq === listSeq.current) setLoading(false)
    }
  }, [queryString])

  // Rows and folder counts in ONE gesture, fired together rather than in
  // sequence: they are independent requests and the chips must never lag the
  // table. Deriving the counts from the loaded rows instead would be cheaper
  // and wrong — the table is paginated, so anything past the first page would
  // be missing from the arithmetic.
  //
  // Every caller that can change either one goes through here: the Refresh
  // button, a re-run (which adds a row and moves the Ungrouped count), and a
  // move. Before this, /api/groups was refetched ONLY by a group mutation, so
  // a re-run left "Ungrouped · N" one behind and pressing Refresh did not fix
  // it — the same chip-versus-table disagreement, arriving as staleness
  // rather than as a wrong query.
  const reloadAll = useCallback(async (dropIds?: Set<string>) => {
    await Promise.all([refreshLoaded(dropIds), refreshGroups()])
  }, [refreshLoaded, refreshGroups])

  // Deleting the active group falls back to All groups; renames/deletes can
  // change row tags, so refresh the loaded rows in place afterwards.
  // useGroups already refetched the folder list, so this only needs the rows.
  const handleRenameGroup = useCallback(async (groupId: string, name: string) => {
    await renameGroup(groupId, name)
    void refreshLoaded()
  }, [renameGroup, refreshLoaded])

  const handleDeleteGroup = useCallback(async (groupId: string) => {
    await deleteGroup(groupId)
    if (groupFilter === groupId) {
      // The filter reset re-fetches via the queryString effect — an extra
      // refreshLoaded here would race it with the stale deleted-group query.
      setGroupFilter(null)
    } else {
      void refreshLoaded()
    }
  }, [deleteGroup, groupFilter, setGroupFilter, refreshLoaded])

  // Declared above moveRuns because a successful move has to refetch it: the
  // drawer caches the run's group label and check-mark, and without a reload
  // it keeps showing the folder the run just left.
  const detailPath = selected ? `/api/history/${selected}` : null
  const { data: detail, error: detailError, reload: reloadDetail } = useFetch<RunDetail>(detailPath)
  // useFetch keeps stale data during a refetch, so a just-clicked row would
  // briefly render the PREVIOUS run's code/query. Only trust detail once it
  // matches the open row.
  const d = detail && detail.run_id === selected ? detail : null

  // fromBulk: the toolbar's multi-select move — only that path exits select
  // mode, and only on success. A failed move keeps the selection so the user
  // can retry; the error shows above the table.
  const moveRuns = useCallback(async (runIds: string[], groupId: string | null, fromBulk = false) => {
    setMoveError('')
    try {
      await assignRuns(runIds, groupId)
      if (fromBulk) {
        setCheckedIds(new Set())
        setSelectMode(false)
      }
      // The drawer holds its own copy of the run; refetch it so its folder
      // label and check-mark stop describing where the run used to be.
      if (selected && runIds.includes(selected)) void reloadDetail()
      // Under an active group filter a moved run must LEAVE the view, and
      // dropping just those ids from the merge tail does that WITHOUT resetting
      // to page zero — a user who had loaded 150 rows still has 150.
      //
      // But only when the run actually left: filing a row into the group being
      // filtered is a no-op the user can still see, and evicting it would
      // delete a legitimate row while `total` held ("149 of 150"). Note that
      // 'ungrouped' is a pseudo-filter rather than a group_id, so "still in
      // view" there means the new group is null — comparing groupId against
      // the filter STRING would evict exactly the row Remove-from-group was
      // supposed to keep. With no filter at all the run stays visible either
      // way, and dropping it would make an older row vanish.
      const staysInView = groupFilter === 'ungrouped' ? groupId === null : groupId === groupFilter
      // assignRuns already refetched the folder list; reloadAll's second
      // request is what keeps the Ungrouped count right when a run leaves or
      // joins it, and the two fire together so neither lags the other.
      if (groupFilter && !staysInView) void reloadAll(new Set(runIds))
      else void reloadAll()
    } catch (e) {
      setMoveError(e instanceof Error ? e.message : 'Failed to move runs')
    }
  }, [assignRuns, groupFilter, reloadAll, reloadDetail, selected])

  const toggleChecked = useCallback((runId: string) => {
    setCheckedIds(prev => {
      const next = new Set(prev)
      if (next.has(runId)) next.delete(runId)
      else next.add(runId)
      return next
    })
  }, [])

  // Debounce the search box into the server query (and reset to page zero).
  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(t)
  }, [search])

  // Initial load + refetch from page zero whenever the tab or search changes.
  useEffect(() => { void loadRuns(0, false) }, [loadRuns])

  // Any filter change replaces the visible rows, so a selection made against
  // the PREVIOUS list is no longer something the user can see or reason about
  // — keeping it would let a bulk move act on rows that scrolled out of
  // existence when they switched groups from the sidebar. Select mode itself
  // stays on; only the now-invisible picks are dropped, along with a move
  // error that belonged to the old view.
  useEffect(() => {
    setCheckedIds(new Set())
    setMoveError('')
  }, [groupFilter, filter, debouncedSearch])

  const isAdminScope = scope === 'all'
  const visible = runs  // filtering is server-side now

  // scope='all' means "no per-user narrowing", NOT "every user on the
  // platform": the server returns it to any org_admin, and every solo signup
  // is org_admin of their own personal org. Reading it as a platform-admin
  // signal told ordinary users they were looking at everyone's runs. Only
  // role='admin' answers that question, so the three cases are separate.
  const subtitle = !isAdminScope
    ? 'Your work in progress, plus every test your team has filed into a group — click any run to view its script and re-run it as-is'
    : isAdmin
      ? 'All users’ test runs (admin view) — click any run to view its script and details'
      : 'Every test run in your organization — click any run to view its script and re-run it as-is'

  // Who ran each test. A group is shared, so a member's table now contains
  // colleagues' runs, and a row with no author would leave the org unable to
  // say who wrote what — the accountability the whole shared model rests on.
  //
  // Shown when there is actually someone else to distinguish: an admin's
  // table always spans users, and a team's does as soon as a second author's
  // run is loaded. A solo user in their own org sees only their own runs, and
  // repeating their address down every line would be noise.
  const showAuthor = isAdminScope || visible.some(
    r => r.user_email && r.user_email !== user?.email)

  // Header select-all works over the VISIBLE (loaded) rows only, so stray ids
  // checked under a previous filter neither satisfy "all selected" nor get
  // swept along by the header toggle.
  // Only rows this caller may actually file can be selected. A bulk move is
  // all-or-nothing server-side, so one unmovable row in the selection rejects
  // the entire batch — and now that a folder is shared, a member's table is
  // full of colleagues' runs they may read but not move. Select-all over the
  // raw list would have made the bulk move unusable for every team member.
  const movable = visible.filter(r => r.can_move)
  const allVisibleSelected = movable.length > 0 && movable.every(r => checkedIds.has(r.run_id))
  const someVisibleSelected = movable.some(r => checkedIds.has(r.run_id))

  const toggleAllVisible = useCallback(() => {
    setCheckedIds(prev => {
      const next = new Set(prev)
      const all = visible.filter(r => r.can_move)
      if (all.every(r => next.has(r.run_id))) all.forEach(r => next.delete(r.run_id))
      else all.forEach(r => next.add(r.run_id))
      return next
    })
  }, [visible])

  const runAgain = useCallback(async (sourceId: string) => {
    setSelected(sourceId) // feedback lives in the drawer
    setInFlight(prev => new Set(prev).add(sourceId))
    const note = (text: string) => setRerunNote(prev => ({ ...prev, [sourceId]: text }))
    note('Starting re-run of the stored code…')
    let listRefreshed = false
    let failed = false
    try {
      await streamSSE('/execute-test', { rerun_of: sourceId }, ev => {
        // First event = the new run row exists; surface it at the top
        // (the table is populated and the stream is still going).
        if (!listRefreshed) { listRefreshed = true; void reloadAll() }
        if (ev.test_status === 'passed') note('✓ Re-run passed')
        else if (ev.test_status === 'failed') note('✗ Re-run failed — open its report from the new row on top')
        else if (ev.status === 'error') note(`Error: ${ev.message || 'execution failed'}`)
        else if (typeof ev.message === 'string' && ev.message) note(ev.message)
      })
    } catch (e) {
      note(e instanceof Error ? e.message : 'Failed to start the re-run')
      failed = true
    } finally {
      setInFlight(prev => { const next = new Set(prev); next.delete(sourceId); return next })
      // The new run exists and may have landed in its source's folder, so the
      // chip counts moved too — reload both.
      //
      // A REFUSED re-run means the source is no longer ours to run: its owner
      // un-filed it between the page load and the click, so it is gone from
      // page zero. Drop it explicitly, or refreshLoaded's merge keeps it alive
      // in the tail and the user is left staring at a row the server denies.
      // Only this id — blanket-dropping everything missing from page zero is
      // what would delete the older Load-more pages.
      void reloadAll(failed ? new Set([sourceId]) : undefined)
    }
  }, [reloadAll])

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
          <h1 className="text-xl font-bold tracking-tight">Test Runs</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            {subtitle}
          </p>
        </div>
        <Button
          size="sm" variant="outline" className="h-7 text-xs gap-1.5"
          onClick={() => { void loadRuns(0, false); void refreshGroups() }}
        >
          <RefreshCw className="h-3 w-3" /> Refresh
        </Button>
      </div>

      <Card>
        <CardHeader className="gap-3 space-y-0 pb-3 px-5">
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
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
                  placeholder={showAuthor ? 'Search description, user or id…' : 'Search description or id…'}
                  className="h-7 w-56 pl-8 text-xs"
                />
              </div>
              <span className="whitespace-nowrap text-xs text-muted-foreground">
                {visible.length} of {total} run{total === 1 ? '' : 's'}
              </span>
            </div>
          </div>
          {/* Group chips row (design option C) + the bulk-move toolbar. The
              toolbar is pinned top-right: only the CHIPS wrap onto new lines
              (min-w-0 flex-1), so Select/Move never drift down as groups grow. */}
          <div className="flex items-start justify-between gap-3 border-t pt-2.5">
            <div className="min-w-0 flex-1">
              <GroupChipsRow
                groups={groups}
                ungroupedCount={ungroupedCount}
                active={groupFilter}
                onSelect={setGroupFilter}
                onCreate={async name => { await createGroup(name) }}
                onRename={handleRenameGroup}
                onDelete={handleDeleteGroup}
                canRename={canRename}
                canDelete={canDelete}
                canCreate={!!user}
              />
            </div>
            {/* Everything in here is a mutation, and every one of them answers
                403 without an identity — so they are not offered at all. */}
            <div className="flex shrink-0 items-center gap-1.5">
              {user && (selectMode ? (
                <>
                  <MoveToGroupMenu
                    groups={groups}
                    showRemove
                    onMove={gid => { if (checkedIds.size) void moveRuns([...checkedIds], gid, true) }}
                    onCreateGroup={createGroup}
                    trigger={
                      <Button size="sm" className="h-7 text-xs gap-1.5" disabled={checkedIds.size === 0}>
                        <FolderInput className="h-3 w-3" />
                        Move {checkedIds.size || ''} to…
                      </Button>
                    }
                  />
                  <Button
                    size="sm" variant="outline" className="h-7 text-xs"
                    onClick={() => { setSelectMode(false); setCheckedIds(new Set()) }}
                  >
                    Cancel
                  </Button>
                </>
              ) : (
                <Button
                  size="sm" variant="outline" className="h-7 text-xs gap-1.5"
                  title="Select multiple runs to move them into a group"
                  onClick={() => setSelectMode(true)}
                >
                  <ListChecks className="h-3 w-3" /> Select
                </Button>
              ))}
            </div>
          </div>
          {moveError && <p className="text-xs text-destructive">{moveError}</p>}
        </CardHeader>

        <CardContent className="p-0">
          {/* Full-page scroll (industry-standard runs list): the table grows
              with content and the PAGE scrolls; the header row stays pinned
              via sticky. min-h keeps short/empty filter results from
              collapsing the card. Column geometry is constant (table-fixed),
              so switching filters only changes the values. */}
          <div className="min-h-[420px]">
          {loading && visible.length === 0 && (
            <p className="flex min-h-[420px] items-center justify-center text-sm text-muted-foreground">Loading runs…</p>
          )}
          {!loading && error && (
            <p className="flex min-h-[420px] items-center justify-center text-sm text-destructive">{error}</p>
          )}
          {!loading && !error && visible.length === 0 && (
            <p className="flex min-h-[420px] items-center justify-center text-sm text-muted-foreground">
              {debouncedSearch
                ? 'No runs match your search.'
                : groupFilter === 'ungrouped'
                  ? 'No ungrouped runs — everything is filed.'
                  : groupFilter
                    ? 'No runs in this group yet — move runs here with the folder button on any row.'
                    : filter === 'all'
                      ? 'No test runs yet — generate your first test from the Generate page.'
                      : `No ${filter} runs yet.`}
            </p>
          )}

          {/* Filter/search changes keep the PREVIOUS rows on screen and dim
              them while the refetch is in flight, then swap in place — no
              unmount, no "Loading runs…" flash between filters. The full
              placeholder only ever shows on the very first load. */}
          {!error && visible.length > 0 && (
            <div className={`transition-opacity duration-200 ${loading ? 'pointer-events-none opacity-50' : 'opacity-100'}`}>
              {/* table-fixed: column widths are set here once and never
                  recomputed from row content, so switching status filters
                  keeps the exact same grid — only the values change. */}
              <table className="w-full table-fixed text-sm">
                <thead className="sticky top-0 z-10 bg-background shadow-[inset_0_-1px_0_hsl(var(--border))]">
                  <tr className="bg-muted/40">
                    {selectMode && (
                      <th className="w-10 py-2.5 pl-4">
                        <input
                          type="checkbox"
                          className="h-3.5 w-3.5 cursor-pointer accent-primary align-middle"
                          title="Select all loaded runs"
                          aria-label="Select all loaded runs"
                          checked={allVisibleSelected}
                          ref={el => { if (el) el.indeterminate = someVisibleSelected && !allVisibleSelected }}
                          onChange={toggleAllVisible}
                        />
                      </th>
                    )}
                    <th className="w-28 py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Status</th>
                    <th className="py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Description</th>
                    {showAuthor && (
                      <th className="w-44 py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground hidden lg:table-cell">Ran by</th>
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
                      onClick={() => (
                        selectMode
                          ? (row.can_move && toggleChecked(row.run_id))
                          : setSelected(row.run_id)
                      )}
                    >
                      {selectMode && (
                        <td className="py-3 pl-4" onClick={e => e.stopPropagation()}>
                          <input
                            type="checkbox"
                            className="h-3.5 w-3.5 accent-primary disabled:cursor-not-allowed disabled:opacity-40 enabled:cursor-pointer"
                            aria-label={`Select run ${row.user_query || 'Pasted code run'}`}
                            title={row.can_move ? undefined
                              : 'Only the owner of a run, or an org admin, can move it'}
                            disabled={!row.can_move}
                            checked={checkedIds.has(row.run_id)}
                            onChange={() => toggleChecked(row.run_id)}
                          />
                        </td>
                      )}
                      <td className="py-3 px-4">{STATUS_BADGE[row.status] ?? row.status}</td>
                      <td className="py-3 px-4">
                        <div className="flex items-center gap-2">
                          {row.rerun_of && (row.rerun_of_accessible ? (
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
                          ) : (
                            /* Still a re-run — that stays true — but the
                               original is no longer ours to open, so the pill
                               is a statement, not a control. Muted and
                               non-interactive; the full original id lives in
                               the tooltip, never truncated. */
                            <span
                              className="shrink-0"
                              title={`Re-run of ${row.rerun_of} — you no longer have access to the original run`}
                            >
                              <Badge className="gap-1 border-border bg-muted text-xs text-muted-foreground hover:bg-muted">
                                <Repeat2 className="h-3 w-3" />
                                <span>Re-run</span>
                                {/* The title attribute is mouse-only. This
                                    span is the same sentence as real text, so
                                    a keyboard or screen-reader user is told
                                    why the pill does nothing instead of
                                    meeting a badge with no explanation. */}
                                <span className="sr-only">
                                  {` of ${row.rerun_of} — you no longer have access to the original run`}
                                </span>
                              </Badge>
                            </span>
                          ))}
                          <span className="line-clamp-1 text-sm" title={row.user_query ?? undefined}>
                            {row.user_query || <span className="text-muted-foreground italic">Pasted code run</span>}
                          </span>
                          {!groupFilter && row.group_name && (
                            <span
                              className="hidden shrink-0 items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground lg:inline-flex"
                              title={`In group ${row.group_name}`}
                            >
                              <Folder className="h-3 w-3" />
                              {row.group_name}
                            </span>
                          )}
                        </div>
                      </td>
                      {showAuthor && (
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
                          {user && row.can_move && (
                            <MoveToGroupMenu
                              groups={groups}
                              currentGroupId={row.group_id}
                              onMove={gid => void moveRuns([row.run_id], gid)}
                              onCreateGroup={createGroup}
                              trigger={
                                <Button
                                  variant="ghost" size="icon" className="h-7 w-7"
                                  title="Move to group…"
                                  onClick={e => e.stopPropagation()}
                                >
                                  <FolderInput className="h-3.5 w-3.5" />
                                </Button>
                              }
                            />
                          )}
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
            </div>
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
              {d?.user_email && d.user_email !== user?.email && (
                <span className="text-xs text-muted-foreground" title="Who ran this test">
                  {d.user_email}
                </span>
              )}
            </div>
            {/* detailError means d is null because the fetch was REFUSED,
                not because the run has no description — so the "Pasted code
                run" fallback would assert something false about a run we
                could not read at all. The reason itself renders in the body
                (see detailError below); the header only stops lying. */}
            <SheetTitle className="text-base leading-snug">
              {detailError ? 'Run unavailable' : d?.user_query || 'Pasted code run'}
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
                  {d.rerun_of_accessible ? (
                    <button
                      type="button"
                      className="font-mono hover:text-foreground hover:underline"
                      title="Open the original run — feedback on this re-run applies to it"
                      onClick={() => setSelected(d.rerun_of!)}
                    >
                      {d.rerun_of}
                    </button>
                  ) : (
                    /* The same rule as the row pill: the id is still shown in
                       full, but it is text rather than a link that 404s. */
                    <span
                      className="font-mono"
                      title="You no longer have access to the original run"
                    >
                      {d.rerun_of}
                    </span>
                  )}
                  {/* The leading space in the string below is load-bearing.
                      JSX strips the newline between two adjacent elements, so
                      without it textContent, a copy-paste and every screen
                      reader read "…b05c23967b51(no longer available to you)".
                      The ml-2 that used to sit here moved 8px on screen and
                      nothing anywhere else. */}
                  {!d.rerun_of_accessible && (
                    <span className="text-muted-foreground">
                      {' (no longer available to you)'}
                    </span>
                  )}
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
            {/* A run this caller may read but not file still shows WHERE it
                lives — that is the shared folder doing its job — but as a
                label rather than a control that could only 404. */}
            {d && user && (d.can_move ? (
              <MoveToGroupMenu
                groups={groups}
                currentGroupId={d.group_id}
                onMove={gid => void moveRuns([d.run_id], gid)}
                onCreateGroup={createGroup}
                trigger={
                  <Button size="sm" variant="outline" className="h-8 gap-1.5 text-xs">
                    <FolderInput className="h-3.5 w-3.5" />
                    {d.group_name ? `Group: ${d.group_name}` : 'Move to group…'}
                  </Button>
                }
              />
            ) : d.group_name && (
              <span
                className="inline-flex items-center gap-1.5 rounded border px-2 py-1 text-xs text-muted-foreground"
                title="Only the owner of a run, or an org admin, can move it"
              >
                <Folder className="h-3.5 w-3.5" />
                Group: {d.group_name}
              </span>
            ))}
          </div>

          {/* The card header's copy of this sits BEHIND the drawer overlay, so
              a move that failed from in here would otherwise be silent. */}
          {moveError && <p className="text-xs text-destructive">{moveError}</p>}

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
