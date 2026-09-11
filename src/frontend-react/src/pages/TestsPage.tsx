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
 * Update (spec 7.4) regenerates the test from an editable copy of its
 * description through POST /api/tests/{test_id}/versions, either in place or
 * as a new test. Opening a row opens the drawer,
 * which makes its own read of GET /api/tests/{test_id} (spec 7.3): the
 * results this viewer may open, newest first and ruled where the code
 * changed, the current version's Robot code, and the version history.
 *
 * Referenced by: App.tsx (PAGES).
 * Depends on: lib/api, lib/sse, lib/time, auth/AuthContext,
 * components/history (the shared folder state, chip row, move menu and
 * folder permissions).
 */
import { Fragment, useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Separator } from '@/components/ui/separator'
import {
  Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle,
} from '@/components/ui/sheet'
import { Textarea } from '@/components/ui/textarea'
import { Check, ChevronRight, Copy, FileTerminal, Folder, FolderInput, Play, RefreshCw, Search, Zap } from 'lucide-react'
import { api, isAccessLoss } from '@/lib/api'
import { streamSSE } from '@/lib/sse'
import { formatDate, timeAgo } from '@/lib/time'
import { labelFrom, versionLabel } from '@/lib/testLabels'
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

/** One result under a test, as GET /api/tests/{test_id} returns it (spec
 *  6.2). `n` is the version it ran; null for a result that names none — a
 *  regeneration that failed before a version landed, or a row migrated from
 *  before versions existed. failure_class/failure_locator are P3 columns and
 *  are always null today. */
interface TestResult {
  run_id: string
  status: string
  n: number | null
  created_at: string
  has_report: boolean
  failure_class: string | null
  failure_locator: string | null
}

interface TestVersion {
  n: number
  user_query: string | null
  robot_code: string | null
  created_by_email: string | null
  reason: string | null
  created_at: string
}

interface TestDetail {
  test: {
    test_id: string
    name: string | null
    user_query: string | null
    user_email: string | null
    group_id: string | null
    group_name: string | null
    current_version: number | null
    created_at: string
    updated_at: string
    health: Health
  }
  versions: TestVersion[]
  results: TestResult[]
  /** The whole caller-scoped set, which is what the timeline pages over —
   *  never the version count, and never the row's own result_count. */
  results_total: number
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
/** The drawer's page over ONE test's results. The endpoint's own default,
 *  and its ceiling is 200. */
const RESULTS_PAGE = 50

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

function labelOf(t: TestRow): string {
  return labelFrom(t.name, t.user_query)
}

/** Activity's result palette (passed / failed / error / generated / running),
 *  dark variants included — Task 11 retrofitted the same values onto that
 *  page's STATUS_BADGE, so the two agree. A status with no entry keeps its
 *  own word in a plain outline badge rather than borrowing another status's
 *  colour. */
const RESULT_BADGE: Record<string, string> = {
  passed: 'bg-green-100 text-green-700 border-green-200 hover:bg-green-100 dark:bg-green-950 dark:text-green-300 dark:border-green-900 dark:hover:bg-green-950',
  failed: 'bg-red-100 text-red-700 border-red-200 hover:bg-red-100 dark:bg-red-950 dark:text-red-300 dark:border-red-900 dark:hover:bg-red-950',
  error: 'bg-amber-100 text-amber-700 border-amber-200 hover:bg-amber-100 dark:bg-amber-950 dark:text-amber-300 dark:border-amber-900 dark:hover:bg-amber-950',
}

function ResultBadge({ status }: Readonly<{ status: string }>) {
  if (status === 'running') return <RunningBadge />
  const tone = RESULT_BADGE[status]
  const label = status.charAt(0).toUpperCase() + status.slice(1)
  return tone
    ? <Badge className={`shrink-0 text-xs ${tone}`}>{label}</Badge>
    : <Badge variant="outline" className="shrink-0 text-xs">{label}</Badge>
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

function TestRowView({ row, groupFilter, groups, running, runDisabled, onOpen, onRun, onUpdate, onMove, onCreateGroup }: Readonly<{
  row: TestRow
  groupFilter: GroupFilter
  groups: RunGroup[]
  running: boolean
  runDisabled: boolean
  onOpen: (testId: string) => void
  onRun: (row: TestRow) => void
  onUpdate: (row: TestRow) => void
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
          {/* Offered on the same term as Move, because the route asks the
              same question of an update (_may_move_test). Offered even where
              can_run is false: a test whose current version has no code is
              exactly one you regenerate — which is what NO_CODE says to do. */}
          {row.can_move && (
            <Button
              variant="ghost" size="icon" className="h-7 w-7"
              title="Update — regenerate this test from its description"
              aria-label={`Update ${label}`}
              onClick={() => onUpdate(row)}
            >
              <Zap className="h-3.5 w-3.5" />
            </Button>
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

/** The version each result RAN, in the timeline's own order (newest first).
 *
 * A result that names no version — a regeneration that failed before one
 * landed, or a row migrated from before versions existed — inherits the
 * version of the next OLDER result that names one, because that is the code
 * the test held at the time. It therefore never reads as a code change of
 * its own. A version-less result with nothing older to inherit from keeps
 * null and stands in its own block.
 */
function effectiveVersions(results: TestResult[]): Array<number | null> {
  const eff: Array<number | null> = new Array<number | null>(results.length).fill(null)
  let carry: number | null = null
  for (let i = results.length - 1; i >= 0; i--) {
    if (results[i].n != null) carry = results[i].n
    eff[i] = results[i].n ?? carry
  }
  return eff
}

/* ── The rule drawn where the code changed. A streak across it means
   nothing (spec section 8), which is the whole reason it is drawn ── */
function VersionRule({ version }: Readonly<{ version: number | null }>) {
  const label = version == null ? 'No version recorded' : `Version ${version}`
  return (
    <li className="flex items-center gap-2 py-2" title="The code changed here — a streak across this line means nothing">
      <span className="h-px flex-1 bg-border" />
      <span className="text-[10px] uppercase tracking-wider text-muted-foreground">{label}</span>
      <span className="h-px flex-1 bg-border" />
    </li>
  )
}

/* ── One result in the drawer's timeline: what it did, which version it ran,
   when, its report, and its full id ── */
function TimelineResult({ result, copied, onCopy }: Readonly<{
  result: TestResult
  copied: string | null
  onCopy: (text: string, key: string) => void
}>) {
  return (
    <li className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-xs">
      <ResultBadge status={result.status} />
      {/* Short here, spelled out in the header and on the version rule: a
          row repeats this once per result. */}
      <span className="text-muted-foreground" title={versionLabel(result.n).title}>
        {versionLabel(result.n).text}
      </span>
      <span className="text-muted-foreground" title={formatDate(result.created_at)}>
        {timeAgo(result.created_at)}
      </span>
      {result.has_report && (
        <a
          className="inline-flex items-center gap-1 underline-offset-4 hover:underline"
          href={`/reports/${result.run_id}/log.html`}
          target="_blank"
          rel="noreferrer"
        >
          <FileTerminal className="h-3 w-3" /> Report
        </a>
      )}
      {/* The full id, never a prefix — a standing rule, and the only thing
          you can do with a result you cannot open is hand its id over. */}
      <button
        type="button"
        className="inline-flex basis-full items-center gap-1 font-mono text-[11px] text-muted-foreground hover:text-foreground"
        title="Copy run id"
        onClick={() => onCopy(result.run_id, result.run_id)}
      >
        {result.run_id}
        {copied === result.run_id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
      </button>
    </li>
  )
}

/** Why a version was written, in words. `null` for every version minted
 *  before the Update dialog existed, and for the first version of any test —
 *  nothing is claimed about those. */
const REASON_WORDS: Record<string, string> = {
  edited: 'description edited',
  regenerated: 'regenerated',
}

/* ── The current version's code: what this test runs today. Older versions'
   code is not shown — the list below says what they were, not what they
   said ── */
function DrawerCode({ version, copied, onCopy }: Readonly<{
  version: TestVersion | null
  copied: string | null
  onCopy: (text: string, key: string) => void
}>) {
  const code = version?.robot_code ?? null
  return (
    <section className="flex flex-col gap-2" aria-label="Robot code">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Robot code
        </span>
        {code && (
          <Button
            variant="ghost" size="icon" className="h-7 w-7"
            title="Copy code"
            aria-label="Copy code"
            onClick={() => onCopy(code, 'code')}
          >
            {copied === 'code' ? <Check className="h-3.5 w-3.5" /> : <Copy className="h-3.5 w-3.5" />}
          </Button>
        )}
      </div>
      {code ? (
        <pre className="max-h-64 overflow-auto rounded-md border bg-muted/40 p-3 font-mono text-xs leading-relaxed">
          {code}
        </pre>
      ) : (
        // The server's own sentence, the one the disabled Run gives.
        <p className="rounded-md border border-dashed px-3 py-6 text-center text-xs italic text-muted-foreground">
          {NO_CODE}
        </p>
      )}
    </section>
  )
}

/* ── Every version this test has had, newest first ── */
function DrawerVersions({ versions, current }: Readonly<{
  versions: TestVersion[]
  current: number | null
}>) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Versions
      </span>
      {versions.length === 0 ? (
        <p className="py-2 text-xs text-muted-foreground">No version recorded for this test.</p>
      ) : (
        <ul className="divide-y" aria-label="Versions, newest first">
          {versions.map(v => (
            <li key={v.n} className="flex flex-wrap items-center gap-x-3 gap-y-1 py-2 text-xs">
              <span className="font-medium">Version {v.n}</span>
              {v.n === current && (
                <Badge variant="secondary" className="text-[10px]">current</Badge>
              )}
              <span className="text-muted-foreground" title={formatDate(v.created_at)}>
                {timeAgo(v.created_at)}
              </span>
              {/* An appender the server could not name stays unnamed: naming
                  the test's author beside someone else's version is worse. */}
              <span className="text-muted-foreground">{v.created_by_email ?? 'author unknown'}</span>
              {v.reason && (
                <span className="text-muted-foreground">{REASON_WORDS[v.reason] ?? v.reason}</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** One "just copied" key at a time, cleared after a moment. Shared by the
 *  drawer and the Update dialog, which both render DrawerCode. */
function useCopiedKey(): [string | null, (text: string, key: string) => Promise<void>, () => void] {
  const [copied, setCopied] = useState<string | null>(null)
  const copy = useCallback(async (text: string, key: string) => {
    try {
      await navigator.clipboard.writeText(text)
      setCopied(key)
      setTimeout(() => setCopied(null), 1500)
    } catch { /* clipboard unavailable — non-fatal */ }
  }, [])
  const clear = useCallback(() => setCopied(null), [])
  return [copied, copy, clear]
}

type UpdateMode = 'update' | 'new_test'

/** What the Update stream actually landed, read off its terminal event.
 *  `testId` is that event's own test_id: for mode 'new_test' it names the
 *  NEW test, not the one the dialog was opened on. */
interface SavedVersion {
  mode: UpdateMode
  testId: string
  /** What to call the test this version belongs to — the row's label for an
   *  update, the submitted description for a test that did not exist yet. */
  label: string
  n: number
  runId: string | null
  robotCode: string | null
  userQuery: string
  /** Whether the reader asked for this version to be run straight away. */
  run: boolean
}

/** The confirm step for a regeneration (spec 7.4). Not a navigation: the
 *  Regenerate it replaces left the page and lost every bit of context.
 *
 * Offered exactly where `can_move` is true, which is the authority the route
 * itself applies to `mode: "update"` (_may_move_test, plus visibility) — so
 * "may move" and "may update" are one answer on every row.
 *
 * It makes its own read rather than taking the drawer's, because it opens
 * from a row too, and a row carries no code and no version list.
 */
function UpdateDialog({ target, onClose, onFinished }: Readonly<{
  target: { testId: string; label: string } | null
  onClose: () => void
  onFinished: (saved: SavedVersion | null) => void
}>) {
  const [detail, setDetail] = useState<TestDetail | null>(null)
  const [query, setQuery] = useState('')
  const [runAfter, setRunAfter] = useState(true)
  const [busy, setBusy] = useState(false)
  const [stage, setStage] = useState('')
  const [error, setError] = useState('')
  const [copied, copy, clearCopied] = useCopiedKey()

  const testId = target?.testId ?? null

  useEffect(() => {
    setDetail(null); setQuery(''); setRunAfter(true); setBusy(false); setStage(''); setError(''); clearCopied()
    if (!testId) return
    let live = true
    // limit=1 is the route's own floor — it clamps limit to [1, 200] — and
    // the newest result is the one a P3 classifier would explain, which is
    // where spec 7.4's failure-reason section will read from. Versions are
    // never paged, so one request carries the whole version list.
    api<TestDetail>(`/api/tests/${testId}?limit=1&offset=0`)
      .then(d => { if (live) { setDetail(d); setQuery(d.test.user_query ?? '') } })
      .catch(e => { if (live) setError(e instanceof Error ? e.message : 'Failed to load this test') })
    return () => { live = false }
  }, [testId, clearCopied])

  const submit = async (mode: UpdateMode) => {
    if (!testId || busy || !query.trim()) return
    setBusy(true); setError(''); setStage('Starting the regeneration…')
    // Held on one object because these are written from inside the event
    // callback and read after the stream ends.
    const out: { saved: SavedVersion | null; failed: boolean; touched: boolean
                 code: string | null; runId: string | null } =
      { saved: null, failed: false, touched: false, code: null, runId: null }
    try {
      // The description goes UNTRIMMED: the route trims it, and decides from
      // that trimmed comparison whether the version reads 'regenerated' or
      // 'edited'. Trimming here as well would be a second copy of a rule
      // that belongs to the server.
      await streamSSE(`/api/tests/${testId}/versions`, { user_query: query, mode }, ev => {
        out.touched = true
        // Read FIRST: the terminal version event carries status 'complete'
        // too, so a read-back failure would otherwise land in the branch
        // below and be mistaken for the generation finishing.
        if (ev.stage === 'version') {
          if (ev.status === 'complete' && typeof ev.test_id === 'string' && typeof ev.n === 'number') {
            out.saved = {
              mode, testId: ev.test_id, n: ev.n,
              // 'new_test' minted a test nobody has named yet; the
              // description they submitted is what labelFrom would call it.
              label: mode === 'new_test' ? query.trim() : (target?.label ?? ''),
              runId: typeof ev.run_id === 'string' ? ev.run_id : out.runId,
              robotCode: out.code, userQuery: query, run: runAfter,
            }
          } else {
            out.failed = true
            setError(ev.message || 'The new version could not be confirmed.')
          }
          return
        }
        if (ev.status === 'running') {
          if (ev.message) setStage(String(ev.message))
        } else if (ev.status === 'complete' && ev.robot_code) {
          // Generation is done; the version has not landed yet.
          out.code = String(ev.robot_code)
          out.runId = typeof ev.workflow_id === 'string' ? ev.workflow_id : null
          setStage('Saving the new version…')
        } else if (ev.status === 'error') {
          out.failed = true
          setError(ev.message || 'Generation failed')
        }
      })
      if (!out.saved && !out.failed) {
        setError('The server ended the stream without saying whether a version was written. Reload the list to see whether it is there.')
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'The update request failed')
    } finally {
      setBusy(false)
      // Any event at all means record_start wrote a run against this test —
      // a regeneration that FAILS attaches to it with no version (spec case
      // 10), so the list is told either way. A refusal answered before the
      // stream began wrote nothing, and says nothing.
      if (out.touched) onFinished(out.saved)
    }
  }

  const current = detail?.versions.find(v => v.n === detail.test.current_version) ?? null
  const canWrite = !!detail && !!query.trim() && !busy

  return (
    <Dialog open={!!target} onOpenChange={o => { if (!o && !busy) onClose() }}>
      {/* Every dismissal route Radix has — the X, Escape, a click outside —
          ends in onOpenChange, so one guard closes all three while a
          generation is in flight. The server keeps generating whatever the
          client does, and a version landing with the page told nothing is
          exactly the dishonesty this prevents. */}
      <DialogContent className="max-h-[85vh] gap-3 overflow-y-auto sm:max-w-xl" showClose={!busy}>
        <DialogHeader className="pr-6 text-left">
          <DialogTitle>Update test</DialogTitle>
          <DialogDescription className="text-xs">
            Regenerate <span className="font-medium text-foreground">{target?.label}</span> from
            the description below. Its results and its earlier versions are kept.
          </DialogDescription>
        </DialogHeader>

        {/* Spec 7.4 item 1 — the classified failure reason — renders nothing
            in P2, and this is not a placeholder for content that exists:
            test_runs has no failure_class column and get_test_detail hands
            back a literal None for every result. The seam is the read above,
            which fetches the newest result for a classifier to explain. */}

        <div className="flex flex-col gap-1.5">
          <label
            htmlFor="update-description"
            className="text-xs font-semibold uppercase tracking-wider text-muted-foreground"
          >
            Description
          </label>
          <Textarea
            id="update-description"
            value={query}
            disabled={busy || !detail}
            onChange={e => setQuery(e.target.value)}
            placeholder="What should this test do?"
            className="min-h-[90px] text-sm"
          />
          <p className="text-[11px] text-muted-foreground">
            Leave it as it is to regenerate the same test; change it to change what the test does.
          </p>
        </div>

        <DrawerCode version={current} copied={copied} onCopy={copy} />

        {/* The first run of a version is the only one that records anything:
            learning is written during EXECUTION and only when a user_query
            comes with it, and the row's own Run sends {test_id}, which
            _run_current_version deliberately executes with none. Unticking
            this is a real choice — a version with no result at all is spec
            case 11 — and it is the reason this is not done automatically. */}
        <label className="flex items-start gap-2 text-xs text-muted-foreground">
          <input
            type="checkbox"
            className="mt-0.5 h-3.5 w-3.5 cursor-pointer accent-primary"
            checked={runAfter}
            disabled={busy}
            onChange={e => setRunAfter(e.target.checked)}
          />
          <span>
            Run the new version once it is saved. Only this first run records what
            the generation learned; running it later re-executes the stored code
            and records nothing.
          </span>
        </label>

        {busy && <p className="text-xs text-muted-foreground" aria-live="polite">{stage}</p>}
        {error && <p className="text-xs text-destructive">{error}</p>}

        {!busy && (
          <DialogFooter className="gap-2 sm:gap-2">
            <Button type="button" size="sm" variant="outline" onClick={onClose}>
              Cancel
            </Button>
            <Button
              type="button" size="sm" variant="outline"
              disabled={!canWrite}
              onClick={() => void submit('new_test')}
            >
              Save as a new test
            </Button>
            <Button type="button" size="sm" disabled={!canWrite} onClick={() => void submit('update')}>
              Update this test
            </Button>
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  )
}

/** The open test: its own read of GET /api/tests/{test_id} (spec 7.3), keyed
 *  on the test id rather than on the row it was opened from — a row that
 *  leaves the list (a folder filter, a refusal) no longer closes the drawer,
 *  and the detail is the later of the two reads, so it wins wherever they
 *  disagree. The row still fills the header for the moment before the read
 *  lands; both carry ONE definition of health (run_registry's single
 *  lateral), so the swap changes a value only when a result landed between
 *  the two requests.
 *
 * `refreshTick` is the page's own list refresh. Without it a run that ends
 * while the drawer is open leaves the timeline saying "Running" under a row
 * that already says Passed. */
function TestDrawer({ testId, row, refreshTick, onUpdate, onClose }: Readonly<{
  testId: string | null
  row: TestRow | null
  refreshTick: number
  onUpdate: (testId: string, label: string) => void
  onClose: () => void
}>) {
  const [detail, setDetail] = useState<TestDetail | null>(null)
  const [results, setResults] = useState<TestResult[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [copied, copy, clearCopied] = useCopiedKey()
  // One ticket per read, as the list does: only the newest may write, so the
  // answer for a test the user already closed cannot repaint this one.
  const readSeq = useRef(0)

  /** replace = this test, freshly opened; append = the next page the reader
   *  asked for; merge = page zero over what is already loaded, which is how a
   *  refresh keeps the pages they asked for (the list does the same). */
  const read = useCallback(async (offset: number, mode: 'replace' | 'append' | 'merge') => {
    if (!testId) return
    const seq = ++readSeq.current
    setLoading(true)
    setError('')
    try {
      const page = await api<TestDetail>(`/api/tests/${testId}?limit=${RESULTS_PAGE}&offset=${offset}`)
      if (seq !== readSeq.current) return
      setDetail(page)
      setTotal(page.results_total)
      setResults(prev => {
        if (mode === 'replace') return page.results
        // Results written between two reads shift every offset after them,
        // so the same run can arrive twice — keep the first copy.
        const seen = new Set(page.results.map(r => r.run_id))
        const rest = prev.filter(r => !seen.has(r.run_id))
        return mode === 'append' ? [...rest, ...page.results] : [...page.results, ...rest]
      })
    } catch (e) {
      if (seq !== readSeq.current) return
      setError(e instanceof Error ? e.message : 'Failed to load this test')
      if (mode !== 'append') {
        setDetail(null)
        setResults([])
        setTotal(0)
      }
    } finally {
      if (seq === readSeq.current) setLoading(false)
    }
  }, [testId])

  // Opening a different test must not show the previous one's timeline for
  // the length of a request.
  useEffect(() => { setDetail(null); setResults([]); setTotal(0); setError(''); clearCopied() }, [testId, clearCopied])
  useEffect(() => { void read(0, 'replace') }, [read])
  // The page's refresh, and only that: `read` changing here as well would
  // fire a second request for a test that was merely opened.
  const seenTick = useRef(refreshTick)
  useEffect(() => {
    if (seenTick.current === refreshTick) return
    seenTick.current = refreshTick
    void read(0, 'merge')
  }, [refreshTick, read])

  const eff = effectiveVersions(results)
  // What this test RUNS today — which is `current_version`, not whichever
  // version happens to be newest in the list.
  const currentVersion = detail?.versions.find(v => v.n === detail.test.current_version) ?? null
  const t = detail?.test
  const health = t?.health ?? row?.health ?? 'not_run'
  const h = HEALTH[health]
  const version = t ? t.current_version : row?.current_version ?? null
  const groupName = t ? t.group_name : row?.group_name ?? null
  const rowLabel = row ? labelOf(row) : ''
  const label = t ? labelFrom(t.name, t.user_query) : rowLabel

  return (
    <Sheet open={!!testId} onOpenChange={open => { if (!open) onClose() }}>
      <SheetContent className="flex w-full flex-col gap-4 overflow-y-auto sm:max-w-2xl">
        {testId && (
          <SheetHeader className="space-y-2 pr-6 text-left">
            <div className="flex items-center gap-2">
              <Badge variant={health === 'not_run' ? 'outline' : 'default'} className={`text-xs ${h.badge}`}>
                {h.label}
              </Badge>
              {version != null && (
                <span className="text-xs text-muted-foreground">Current version {version}</span>
              )}
              {/* Gated on the ROW, because GET /api/tests/{test_id} answers
                  no can_move and the dialog's write asks the same question
                  Move does. A drawer whose row has left the list therefore
                  offers nothing here rather than offering a refusal. */}
              {row?.can_move && (
                <Button
                  variant="outline" size="sm" className="ml-auto h-7 gap-1.5 text-xs"
                  title="Update — regenerate this test from its description"
                  onClick={() => onUpdate(testId, label)}
                >
                  <Zap className="h-3.5 w-3.5" /> Update
                </Button>
              )}
            </div>
            <SheetTitle className="text-base leading-snug">{label}</SheetTitle>
            <SheetDescription className="text-xs">
              {groupName ? `In ${groupName}` : 'Not in a group'}
            </SheetDescription>
          </SheetHeader>
        )}

        {testId && (
          <div className="flex flex-col gap-1">
            <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Results
            </span>
            {error && <p className="py-4 text-xs text-destructive">{error}</p>}
            {!error && loading && results.length === 0 && (
              <p className="py-4 text-xs text-muted-foreground">Loading results…</p>
            )}
            {!error && !loading && results.length === 0 && (
              <p className="py-4 text-xs text-muted-foreground">
                No result of this test that you can open.
              </p>
            )}
            <ul className="divide-y" aria-label="Results, newest first">
              {results.map((r, i) => (
                <Fragment key={r.run_id}>
                  {i > 0 && eff[i] !== eff[i - 1] && <VersionRule version={eff[i]} />}
                  <TimelineResult result={r} copied={copied} onCopy={copy} />
                </Fragment>
              ))}
            </ul>
            {results.length < total && (
              <div className="pt-2">
                <Button
                  size="sm" variant="outline" className="h-7 text-xs"
                  disabled={loading}
                  onClick={() => void read(results.length, 'append')}
                >
                  Load more ({total - results.length} more)
                </Button>
              </div>
            )}
          </div>
        )}

        {detail && (
          <>
            <Separator />
            <DrawerCode version={currentVersion} copied={copied} onCopy={copy} />
            <Separator />
            <DrawerVersions versions={detail.versions} current={detail.test.current_version} />
          </>
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
  // The test the Update dialog is open on, with the name to call it by
  // while its own read is still in flight.
  const [updating, setUpdating] = useState<{ testId: string; label: string } | null>(null)
  // Bumped every time the list is re-read, so the open drawer re-reads its
  // own test with it rather than going stale behind a finished run.
  const [refreshTick, setRefreshTick] = useState(0)

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
      setRefreshTick(n => n + 1)
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

  // The first run of a freshly written version, and the ONLY form of run
  // that records learning: _process_learning_record is called from the
  // execution stream and returns early on an empty user_query, while the
  // row's Run sends {test_id}, which _run_current_version runs with none.
  // Re-using the generation's own workflow_id reuses that run row rather
  // than minting a second one, and record_start COALESCEs test_id and
  // test_version_id, so the result stays attached to the version just
  // written.
  const runNewVersion = useCallback(async (saved: SavedVersion) => {
    const id = saved.testId
    const what = `Version ${saved.n} of “${saved.label}”`
    setInFlight(prev => new Set(prev).add(id))
    setRunError('')
    let refreshed = false
    try {
      await streamSSE('/execute-test', {
        robot_code: saved.robotCode,
        workflow_id: saved.runId,
        user_query: saved.userQuery,
      }, ev => {
        if (!refreshed && typeof ev.run_id === 'string') {
          refreshed = true
          void reloadAll()
        }
        if (ev.status === 'error') {
          const which = typeof ev.run_id === 'string' ? ` (run ${ev.run_id})` : ''
          // Two facts, kept apart: the version IS written whatever the run
          // does, and saying otherwise would send someone regenerating a
          // test that is already up to date.
          setRunError(`${what} was saved. Running it failed${which}: ${ev.message || 'execution failed'}`)
        }
      })
    } catch (e) {
      setRunError(`${what} was saved, but the run could not start — ${e instanceof Error ? e.message : 'the request failed'}`)
    } finally {
      setInFlight(prev => { const next = new Set(prev); next.delete(id); return next })
      void reloadAll()
    }
  }, [reloadAll])

  // Called once per Update stream that produced any event at all. A version
  // that landed closes the dialog; one that did not leaves it open with the
  // server's words in it, so a retry costs no retyping. Either way the list
  // is re-read, because a failed regeneration still attaches its run to the
  // test (spec case 10) — and refreshLoaded bumps the tick the open drawer
  // re-reads on.
  const updateFinished = useCallback((saved: SavedVersion | null) => {
    if (!saved) { void reloadAll(); return }
    setUpdating(null)
    // The terminal event names the test the version landed on, which for
    // mode 'new_test' is a test that did not exist a moment ago. Opening it
    // is the only landing that reliably shows it: list_tests orders on
    // last_run_at DESC NULLS LAST, so a test with no result sorts LAST and
    // a refresh alone can leave it off the loaded page entirely.
    if (saved.mode === 'new_test') setSelected(saved.testId)
    // No code means there is nothing to post — /execute-test is given the
    // code itself, and the {test_id} form it would otherwise take is the
    // re-run that records nothing.
    if (saved.run && saved.robotCode && saved.runId) void runNewVersion(saved)
    else void reloadAll()
  }, [reloadAll, runNewVersion])

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
                          onUpdate={r => setUpdating({ testId: r.test_id, label: labelOf(r) })}
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

      <TestDrawer
        testId={selected}
        row={selectedRow}
        refreshTick={refreshTick}
        onUpdate={(testId, label) => setUpdating({ testId, label })}
        onClose={() => setSelected(null)}
      />

      {/* One instance for the whole page, not one per row: a dialog rendered
          inside a row bubbles its React synthetic events to that row's
          onClick and opens the drawer behind itself (the defect Task 11
          fixes for MoveToGroupMenu). Out here there is no row to bubble to. */}
      <UpdateDialog
        target={updating}
        onClose={() => setUpdating(null)}
        onFinished={updateFinished}
      />
    </div>
  )
}
