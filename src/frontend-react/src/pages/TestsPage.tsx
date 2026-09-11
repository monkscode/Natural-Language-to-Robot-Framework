/**
 * Tests — the landing page: every test the caller can see, and how each one
 * is doing (spec section 7.2). A test is a description plus the code
 * generated from it; its individual runs stay on Activity.
 *
 * List: GET /api/tests, caller-scoped server-side exactly as /api/history
 * is — the caller's own tests plus every test their org has filed into a
 * folder. Every run-derived number on a row (health, sparkline, run count,
 * last run) counts only the results THIS viewer may open, so two people can
 * see the same test with different numbers, and in a different order
 * (RunRegistry.list_tests' docstring). A test whose only results the viewer
 * cannot open reads as never run: that is a normal row, not an error. The
 * tab counts come off the same response (`counts`), so they are per-viewer
 * too.
 *
 * Run re-executes the test's CURRENT version as a new run via
 * POST /execute-test {test_id} — no LLM, no regeneration, learning skipped.
 * Move files the test via PUT /api/tests/assignments; its results follow it.
 * Update (spec 7.4) is not on the row yet — its dialog is not built — and the
 * actions column is already sized for it. The drawer shows only what the row
 * already knows; its timeline, code and versions (spec 7.3) are not built.
 *
 * Referenced by: App.tsx (PAGES).
 * Depends on: lib/api, lib/sse, lib/time, auth/AuthContext,
 * components/history (the shared folder state, chip row, move menu and
 * folder permissions).
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from '@/components/ui/sheet'
import { ChevronRight, Folder, FolderInput, Play, RefreshCw, Search, Zap } from 'lucide-react'
import { api, isAccessLoss } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import { formatDate, timeAgo } from '@/lib/time'
import { canDeleteFolder, canRenameFolder } from '@/components/history/folderPermissions'
import { GroupChipsRow } from '@/components/history/GroupChipsRow'
import { MoveToGroupMenu } from '@/components/history/MoveToGroupMenu'
import { useRunGroups, type GroupFilter } from '@/components/history/RunGroupsContext'
import type { RunGroup } from '@/components/history/useGroups'
import { useAuth } from '@/auth/AuthContext'

type Health = 'passing' | 'failing' | 'not_run'

interface TestRow {
  test_id: string
  name: string | null
  user_query: string | null
  user_email?: string | null
  group_id: string | null
  group_name: string | null
  current_version: number | null
  version_count: number
  result_count: number
  pass_count: number
  last_status: string | null
  last_run_at: string | null
  last_run_id: string | null
  health: Health
  running: boolean
  spark: Array<'pass' | 'fail'>
  can_run: boolean
  can_move: boolean
}

type Tab = 'all' | 'failing' | 'passing'
type Counts = Record<Tab, number>

interface TestsResponse {
  tests: TestRow[]
  total: number
  counts: Counts
  scope: 'own' | 'all'
}

const PAGE = 100

const TABS: ReadonlyArray<{ key: Tab; label: string }> = [
  { key: 'all', label: 'All' },
  { key: 'failing', label: 'Failing' },
  { key: 'passing', label: 'Passing' },
]

// The server's own 409 sentence, so the reason a disabled Run gives is the
// one the server would give if it were clicked.
const NO_CODE = 'This test has no runnable code in its current version — regenerate it first.'

/** The health word, the dot, and what it means. The palette is History's
 *  passed / failed badge palette, with the dark variants History lacks. */
const HEALTH: Record<Health, { label: string; dot: string; badge: string; title: string }> = {
  passing: {
    label: 'Passing',
    dot: 'bg-green-500 dark:bg-green-400',
    badge: 'bg-green-100 text-green-700 border-green-200 hover:bg-green-100 dark:bg-green-950 dark:text-green-300 dark:border-green-900 dark:hover:bg-green-950',
    title: 'Passing — the latest completed run of the current version that you can see passed',
  },
  failing: {
    label: 'Failing',
    dot: 'bg-red-500 dark:bg-red-400',
    badge: 'bg-red-100 text-red-700 border-red-200 hover:bg-red-100 dark:bg-red-950 dark:text-red-300 dark:border-red-900 dark:hover:bg-red-950',
    title: 'Failing — the latest completed run of the current version that you can see failed or errored',
  },
  not_run: {
    label: 'Not run',
    dot: 'border border-muted-foreground/60',
    badge: '',
    title: 'Not run — no completed run of the current version that you can see',
  },
}

const SPARK_MARK: Record<'pass' | 'fail', string> = {
  pass: 'bg-green-500 dark:bg-green-400',
  fail: 'bg-red-500 dark:bg-red-400',
}

/** What a row is called: its name, else its description. */
function labelOf(t: TestRow): string {
  return t.name?.trim() || t.user_query?.trim() || 'Untitled test'
}

/** Same three cases historySubtitle separates, for the same reason:
 *  scope='all' is ANY org_admin — every solo signup included — so only
 *  role='admin' may be read as "everyone's". */
function testsSubtitle(isAdminScope: boolean, isAdmin: boolean): string {
  if (!isAdminScope) return 'Your tests, plus every test your team has filed into a group'
  if (isAdmin) return 'All users’ tests (admin view)'
  return 'Every test in your organization'
}

/** Why a page that is not the first-run page has no rows, in the same
 *  precedence History uses: search, then folder, then tab. */
function noTestsMessage(search: string, groupFilter: GroupFilter, tab: Tab): string {
  if (search) return 'No tests match your search.'
  if (groupFilter === 'ungrouped') return 'No ungrouped tests — everything is filed.'
  if (groupFilter) return 'No tests in this group yet — move tests here with the folder button on any row.'
  if (tab === 'failing') return 'No failing tests.'
  return 'No passing tests yet.'
}

function HealthDot({ health }: Readonly<{ health: Health }>) {
  const h = HEALTH[health]
  return (
    <span className="flex h-4 w-4 items-center justify-center" title={h.title}>
      <span className={`h-2.5 w-2.5 rounded-full ${h.dot}`} />
      <span className="sr-only">{h.label}</span>
    </span>
  )
}

function RunningBadge() {
  return (
    <Badge variant="secondary" className="shrink-0 gap-1.5 text-xs">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500 dark:bg-blue-400" />
      <span>Running</span>
    </Badge>
  )
}

/** The current version's last ten completed runs, oldest on the left.
 *  Padded on the LEFT, so the newest run always sits at the right edge and
 *  a short history does not read as a gap in a long one. */
function Spark({ spark }: Readonly<{ spark: TestRow['spark'] }>) {
  const passed = spark.filter(s => s === 'pass').length
  const label = spark.length
    ? `Last ${spark.length} completed run${spark.length === 1 ? '' : 's'} of the current version you can see: ${passed} passed, ${spark.length - passed} failed`
    : 'No completed runs of the current version that you can see'
  const slots: Array<'pass' | 'fail' | null> = [
    ...Array<null>(Math.max(0, 10 - spark.length)).fill(null), ...spark.slice(-10),
  ]
  return (
    <div role="img" aria-label={label} title={label} className="flex h-4 items-stretch gap-0.5">
      {slots.map((s, i) => (
        // Keyed by position on purpose: slot i is always the i-th of ten.
        <span key={i} className={`w-1.5 rounded-sm ${s ? SPARK_MARK[s] : 'bg-muted'}`} />
      ))}
    </div>
  )
}

/* ── The three health tabs, each with its own count, plus the search box and
   the "N of M" counter. Every number here is the server's, for THIS viewer ── */
function TestsFilterBar({ tab, onTab, counts, search, onSearch, shown, total }: Readonly<{
  tab: Tab
  onTab: (next: Tab) => void
  counts: Counts | null
  search: string
  onSearch: (next: string) => void
  shown: number
  total: number
}>) {
  return (
    <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
      <div className="flex flex-wrap items-center gap-1.5">
        {TABS.map(t => (
          <Button
            key={t.key}
            size="sm"
            variant={tab === t.key ? 'default' : 'outline'}
            className="h-7 w-28 gap-1.5 text-xs"
            aria-pressed={tab === t.key}
            onClick={() => onTab(t.key)}
          >
            {t.label}
            {/* The space is load-bearing: without it the button's name
                reads "Failing3" to a screen reader. */}
            {counts && <>{' '}<span className="tabular-nums opacity-70">{counts[t.key]}</span></>}
          </Button>
        ))}
      </div>
      <div className="flex items-center gap-3">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={e => onSearch(e.target.value)}
            placeholder="Search descriptions…"
            aria-label="Search tests"
            className="h-7 w-56 pl-8 text-xs"
          />
        </div>
        <span className="whitespace-nowrap text-xs text-muted-foreground">
          {shown} of {total} test{total === 1 ? '' : 's'}
        </span>
      </div>
    </div>
  )
}

/* ── What the table area says when it has no rows: loading, failed, the
   first-run prompt, or why a filter emptied it ── */
function TestsEmptyState({ loading, error, isEmpty, firstRun, message, onGenerate }: Readonly<{
  loading: boolean
  error: string
  isEmpty: boolean
  firstRun: boolean
  message: string
  onGenerate: () => void
}>) {
  if (loading && isEmpty) {
    return <p className="flex min-h-[420px] items-center justify-center text-sm text-muted-foreground">Loading tests…</p>
  }
  if (!loading && error) {
    return <p className="flex min-h-[420px] items-center justify-center text-sm text-destructive">{error}</p>
  }
  if (loading || !isEmpty) return null
  if (firstRun) {
    // The landing page's first screen for a brand-new user (spec 7.1):
    // what a test is here, and the one way to make one.
    return (
      <div className="flex min-h-[420px] flex-col items-center justify-center gap-4 px-6 text-center">
        <div className="space-y-1.5">
          <h2 className="text-base font-semibold">No tests yet</h2>
          <p className="max-w-md text-sm text-muted-foreground">
            A test is a plain-English description plus the Robot Framework
            script Mark 1 generates from it — once generated, it lives here,
            ready to run again.
          </p>
        </div>
        <Button size="sm" className="gap-1.5" onClick={onGenerate}>
          <Zap className="h-3.5 w-3.5" /> Generate your first test
        </Button>
      </div>
    )
  }
  return <p className="flex min-h-[420px] items-center justify-center text-sm text-muted-foreground">{message}</p>
}

function TestsTableHead() {
  const th = 'py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground'
  return (
    <thead className="sticky top-0 z-10 bg-background shadow-[inset_0_-1px_0_hsl(var(--border))]">
      <tr className="bg-muted/40">
        <th className="w-12 py-2.5 pl-4"><span className="sr-only">Health</span></th>
        <th className={th}>Test</th>
        <th className={`w-32 hidden md:table-cell ${th}`}>Last 10</th>
        <th className={`w-20 hidden sm:table-cell ${th}`}>Runs</th>
        <th className={`w-28 hidden sm:table-cell ${th}`}>Last run</th>
        {/* Three h-7 w-7 buttons and their gaps (92px) inside px-2 — sized
            for Run, Update and Move, so Update lands without a reflow. */}
        <th className="w-28 py-2.5 px-2"><span className="sr-only">Actions</span></th>
        <th className="w-9 py-2.5 pr-3"></th>
      </tr>
    </thead>
  )
}

function TestRowView({ row, groupFilter, groups, running, runDisabled, onOpen, onRun, onMove, onCreateGroup }: Readonly<{
  row: TestRow
  groupFilter: GroupFilter
  groups: RunGroup[]
  running: boolean
  runDisabled: boolean
  onOpen: (testId: string) => void
  onRun: (row: TestRow) => void
  onMove: (testIds: string[], groupId: string | null) => void
  onCreateGroup: (name: string) => Promise<RunGroup>
}>) {
  const label = labelOf(row)
  return (
    <tr
      className="group cursor-pointer border-b last:border-0 hover:bg-muted/30 transition-colors"
      onClick={() => onOpen(row.test_id)}
    >
      <td className="py-3 pl-4"><HealthDot health={row.health} /></td>
      <td className="py-3 px-4">
        <div className="flex min-w-0 items-center gap-2">
          {/* A real button, so Tab reaches the row and Enter or Space opens
              it — the row's own click is a mouse convenience on top. */}
          <button
            type="button"
            className="min-w-0 truncate rounded-sm text-left text-sm hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            title={label}
            onClick={e => { e.stopPropagation(); onOpen(row.test_id) }}
          >
            {label}
          </button>
          {running && <RunningBadge />}
          {!groupFilter && row.group_name && (
            <span
              className="hidden shrink-0 items-center gap-1 rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground sm:inline-flex"
              title={`In group ${row.group_name}`}
            >
              <Folder className="h-3 w-3" />
              {row.group_name}
            </span>
          )}
        </div>
      </td>
      <td className="py-3 px-4 hidden md:table-cell"><Spark spark={row.spark} /></td>
      <td className="py-3 px-4 hidden sm:table-cell text-xs tabular-nums text-muted-foreground">
        {row.result_count}
      </td>
      <td
        className="py-3 px-4 hidden sm:table-cell whitespace-nowrap text-xs text-muted-foreground"
        title={row.last_run_at ? formatDate(row.last_run_at) : undefined}
      >
        {row.last_run_at ? timeAgo(row.last_run_at) : 'Never'}
      </td>
      {/* Every click in here stays here. The move menu's "New group" dialog
          is portalled to <body>, but React events bubble the REACT tree, so
          without this a click anywhere in that dialog opens this row's
          drawer behind it. */}
      <td className="py-3 px-2" onClick={e => e.stopPropagation()}>
        <div className="flex justify-end gap-1">
          {row.can_run ? (
            <Button
              variant="ghost" size="icon" className="h-7 w-7"
              title="Run — execute the current version as-is (no regeneration)"
              aria-label={`Run ${label}`}
              disabled={runDisabled}
              onClick={() => onRun(row)}
            >
              <Play className="h-3.5 w-3.5" />
            </Button>
          ) : (
            // A disabled button receives no pointer events, so its own title
            // would never show; the wrapper carries the reason instead.
            <span title={NO_CODE}>
              <Button
                variant="ghost" size="icon" className="h-7 w-7"
                aria-label={`Run ${label} — ${NO_CODE}`}
                disabled
              >
                <Play className="h-3.5 w-3.5" />
              </Button>
            </span>
          )}
          {row.can_move && (
            <MoveToGroupMenu
              groups={groups}
              currentGroupId={row.group_id}
              noun="tests"
              onMove={gid => onMove([row.test_id], gid)}
              onCreateGroup={onCreateGroup}
              trigger={
                <Button
                  variant="ghost" size="icon" className="h-7 w-7"
                  title="Move to group…"
                  aria-label={`Move ${label} to a group`}
                >
                  <FolderInput className="h-3.5 w-3.5" />
                </Button>
              }
            />
          )}
        </div>
      </td>
      <td className="py-3 pr-3">
        <ChevronRight className="h-4 w-4 text-muted-foreground/40 transition-all group-hover:translate-x-0.5 group-hover:text-foreground" />
      </td>
    </tr>
  )
}

/** Header only: every field here is already on the list row, so opening it
 *  costs no request. The timeline, code and version list (spec 7.3) are not
 *  built yet. */
function TestDrawer({ row, onClose }: Readonly<{ row: TestRow | null; onClose: () => void }>) {
  const h = row ? HEALTH[row.health] : null
  return (
    <Sheet open={!!row} onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent className="flex w-full flex-col gap-4 overflow-y-auto sm:max-w-2xl">
        {row && h && (
          <SheetHeader className="space-y-2 pr-6 text-left">
            <div className="flex items-center gap-2">
              <Badge variant={row.health === 'not_run' ? 'outline' : 'default'} className={`text-xs ${h.badge}`}>
                {h.label}
              </Badge>
              {row.current_version != null && (
                <span className="text-xs text-muted-foreground">Version {row.current_version}</span>
              )}
            </div>
            <SheetTitle className="text-base leading-snug">{labelOf(row)}</SheetTitle>
            <SheetDescription className="text-xs">
              {row.group_name ? `In ${row.group_name}` : 'Not in a group'}
            </SheetDescription>
          </SheetHeader>
        )}
      </SheetContent>
    </Sheet>
  )
}

export default function TestsPage() {
  const navigate = useNavigate()

  const [tests, setTests] = useState<TestRow[]>([])
  const [total, setTotal] = useState(0)
  const [counts, setCounts] = useState<Counts | null>(null)
  const [scope, setScope] = useState<'own' | 'all'>('own')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')

  const [tab, setTab] = useState<Tab>('all')
  // search is the raw input; debouncedSearch is what the request keys off.
  const [search, setSearch] = useState('')
  const [debouncedSearch, setDebouncedSearch] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  // Runs this page started, by test_id — each disables its own Run button
  // until its stream ends. A run someone else started shows through the
  // row's `running` flag instead, and does not disable Run: two runs of one
  // test may be in flight at once (spec case 25).
  const [inFlight, setInFlight] = useState<Set<string>>(new Set())
  const [runError, setRunError] = useState('')
  const [moveError, setMoveError] = useState('')

  // The same folder state, filter included, that Activity and the sidebar
  // read — a folder picked on any of them scopes all of them.
  const {
    groups, ungroupedTestCount, error: groupsError, refresh: refreshGroups,
    createGroup, renameGroup, deleteGroup, assignTests, groupFilter, setGroupFilter,
  } = useRunGroups()
  const { user, isAdmin } = useAuth()
  const canRename = useCallback((g: RunGroup) => canRenameFolder(user, g), [user])
  const canDelete = useCallback((_g: RunGroup) => canDeleteFolder(user), [user])

  const queryString = useCallback((offset: number) => {
    const healthParam = tab === 'all' ? '' : `&health=${tab}`
    const qParam = debouncedSearch ? `&q=${encodeURIComponent(debouncedSearch)}` : ''
    const groupParam = groupFilter ? `&group=${encodeURIComponent(groupFilter)}` : ''
    return `/api/tests?limit=${PAGE}&offset=${offset}${healthParam}${qParam}${groupParam}`
  }, [tab, debouncedSearch, groupFilter])

  // One ticket per list request; only the newest may write, so a slow
  // response for a filter the user already left cannot repaint the table.
  const listSeq = useRef(0)

  const apply = useCallback((page: TestsResponse) => {
    setScope(page.scope)
    setTotal(page.total)
    setCounts(page.counts)
  }, [])

  const loadTests = useCallback(async (offset: number, append: boolean) => {
    const seq = ++listSeq.current
    if (!append) setLoading(true)
    setError('')
    try {
      const page = await api<TestsResponse>(queryString(offset))
      if (seq !== listSeq.current) return
      apply(page)
      setTests(prev => {
        if (!append) return page.tests
        // Rows written between page loads shift offsets — dedupe on append.
        const seen = new Set(prev.map(t => t.test_id))
        return [...prev, ...page.tests.filter(t => !seen.has(t.test_id))]
      })
    } catch (e) {
      if (seq !== listSeq.current) return
      setError(e instanceof Error ? e.message : 'Failed to load tests')
    } finally {
      if (seq === listSeq.current) setLoading(false)
    }
  }, [queryString, apply])

  // Refresh after a run or a move WITHOUT collapsing pagination: page zero
  // merged over the loaded rows, keeping older pages the user loaded.
  // dropIds: rows that must not survive in the tail — a test that left the
  // folder being filtered, or one the caller can no longer see.
  const refreshLoaded = useCallback(async (dropIds?: Set<string>) => {
    const seq = ++listSeq.current
    try {
      const page = await api<TestsResponse>(queryString(0))
      if (seq !== listSeq.current) return
      apply(page)
      setTests(prev => {
        const fresh = new Set(page.tests.map(t => t.test_id))
        const tail = prev.filter(t => !fresh.has(t.test_id) && !dropIds?.has(t.test_id))
        return [...page.tests, ...tail]
      })
    } catch { /* a transient refresh failure leaves the rows in place */ } finally {
      if (seq === listSeq.current) setLoading(false)
    }
  }, [queryString, apply])

  // A run moves the row's numbers and the folder's run count together.
  const reloadAll = useCallback(async (dropIds?: Set<string>) => {
    await Promise.all([refreshLoaded(dropIds), refreshGroups()])
  }, [refreshLoaded, refreshGroups])

  useEffect(() => {
    const t = setTimeout(() => setDebouncedSearch(search.trim()), 300)
    return () => clearTimeout(t)
  }, [search])

  useEffect(() => { void loadTests(0, false) }, [loadTests])

  const runTest = useCallback(async (row: TestRow) => {
    const id = row.test_id
    const label = labelOf(row)
    setInFlight(prev => new Set(prev).add(id))
    setRunError('')
    let refreshed = false
    let refused = false
    try {
      // {test_id} and nothing else: the server answers 400 to any other
      // field beside it, and mints the run id itself.
      await streamSSE('/execute-test', { test_id: id }, ev => {
        // run_id rides the FIRST execution event: the run's row exists from
        // then on, so the list can show the test as running.
        if (!refreshed && typeof ev.run_id === 'string') {
          refreshed = true
          void reloadAll()
        }
        if (ev.status === 'error') {
          const which = typeof ev.run_id === 'string' ? ` (run ${ev.run_id})` : ''
          setRunError(`Run of “${label}” failed${which}: ${ev.message || 'execution failed'}`)
        }
      })
    } catch (e) {
      setRunError(`Couldn’t run “${label}” — ${e instanceof Error ? e.message : 'the request failed'}`)
      // Only a test the caller can no longer see may leave the list — see
      // isAccessLoss for why a 409 is not that.
      refused = isAccessLoss(e)
    } finally {
      setInFlight(prev => { const next = new Set(prev); next.delete(id); return next })
      void reloadAll(refused ? new Set([id]) : undefined)
    }
  }, [reloadAll])

  const moveTests = useCallback(async (testIds: string[], groupId: string | null) => {
    setMoveError('')
    try {
      await assignTests(testIds, groupId)
      // Under a folder filter a test that left it must leave the view — but
      // only if it actually left ('ungrouped' is a pseudo-filter, so there
      // "still in view" means the new folder is none).
      const staysInView = groupFilter === 'ungrouped' ? groupId === null : groupId === groupFilter
      void refreshLoaded(groupFilter && !staysInView ? new Set(testIds) : undefined)
    } catch (e) {
      setMoveError(e instanceof Error ? e.message : 'Failed to move the test')
    }
  }, [assignTests, groupFilter, refreshLoaded])

  const isAdminScope = scope === 'all'
  // Only the caller with no tests at all gets the first-run prompt. counts
  // honour the search and the folder, so "no search, no folder, zero on All"
  // is exactly "this caller has no tests".
  const firstRun = counts?.all === 0 && !debouncedSearch && !groupFilter
  const selectedRow = tests.find(t => t.test_id === selected) ?? null

  return (
    <div className="mx-auto max-w-6xl">
      <div className="mb-5 flex items-end justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Tests</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">{testsSubtitle(isAdminScope, isAdmin)}</p>
        </div>
        <Button
          size="sm" variant="outline" className="h-7 text-xs gap-1.5"
          onClick={() => { void loadTests(0, false); void refreshGroups() }}
        >
          <RefreshCw className="h-3 w-3" /> Refresh
        </Button>
      </div>

      <Card>
        <CardHeader className="gap-3 space-y-0 pb-3 px-5">
          <TestsFilterBar
            tab={tab}
            onTab={setTab}
            counts={counts}
            search={search}
            onSearch={setSearch}
            shown={tests.length}
            total={total}
          />
          <div className="border-t pt-2.5">
            <GroupChipsRow
              unit="test"
              groups={groups}
              ungroupedCount={ungroupedTestCount}
              active={groupFilter}
              onSelect={setGroupFilter}
              onCreate={async name => { await createGroup(name) }}
              onRename={async (groupId, name) => { await renameGroup(groupId, name); void refreshLoaded() }}
              onDelete={async groupId => {
                await deleteGroup(groupId)
                // Clearing the filter refetches on its own; a second request
                // here would race it with the deleted folder's query.
                if (groupFilter === groupId) setGroupFilter(null)
                else void refreshLoaded()
              }}
              canRename={canRename}
              canDelete={canDelete}
              canCreate={!!user}
            />
          </div>
          {groupsError && <p className="text-xs text-destructive">Couldn’t load groups — {groupsError}</p>}
          {moveError && <p className="text-xs text-destructive">{moveError}</p>}
          {runError && <p className="text-xs text-destructive">{runError}</p>}
        </CardHeader>

        <CardContent className="p-0">
          <div className="min-h-[420px]">
            <TestsEmptyState
              loading={loading}
              error={error}
              isEmpty={tests.length === 0}
              firstRun={firstRun}
              message={noTestsMessage(debouncedSearch, groupFilter, tab)}
              onGenerate={() => navigate('/generate')}
            />
            {!error && tests.length > 0 && (
              <div className={`transition-opacity duration-200 ${loading ? 'pointer-events-none opacity-50' : 'opacity-100'}`}>
                <div className="overflow-x-auto">
                  <table className="w-full table-fixed text-sm">
                    <TestsTableHead />
                    <tbody>
                      {tests.map(row => (
                        <TestRowView
                          key={row.test_id}
                          row={row}
                          groupFilter={groupFilter}
                          groups={groups}
                          running={row.running || inFlight.has(row.test_id)}
                          runDisabled={inFlight.has(row.test_id)}
                          onOpen={setSelected}
                          onRun={r => void runTest(r)}
                          onMove={(ids, gid) => void moveTests(ids, gid)}
                          onCreateGroup={createGroup}
                        />
                      ))}
                    </tbody>
                  </table>
                </div>
                {tests.length < total && (
                  <div className="border-t px-4 py-3 text-center">
                    <Button size="sm" variant="outline" className="h-7 text-xs" onClick={() => void loadTests(tests.length, true)}>
                      Load more ({total - tests.length} more)
                    </Button>
                  </div>
                )}
              </div>
            )}
          </div>
        </CardContent>
      </Card>

      <TestDrawer row={selectedRow} onClose={() => setSelected(null)} />
    </div>
  )
}
