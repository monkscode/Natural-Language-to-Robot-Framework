/**
 * Group filter row — the Test Runs page's folder bar.
 *
 * Deliberately fixed-width: the row never grows with the number of groups.
 * It carries at most three controls — Ungrouped, the groups control, and
 * "＋ New" — so creating twenty groups cannot push the page's toolbar down.
 * That matters more now that a group is the org's and the list gets longer.
 *
 * The groups control is the whole taxonomy behind one button: it reads
 * "All Groups" while no group is filtered, and becomes the selected group's
 * own chip (with an ✕ to clear) once one is. Clicking it either way opens the
 * browse dialog, which lists every group with its run count and is also where
 * edit/delete live — so group management has one obvious home instead of
 * icons that only appear beside an active chip.
 *
 * Management affordances are offered only where the caller may actually use
 * them (canRename / canDelete / canCreate); the server refuses the rest
 * regardless. Rename and delete are separate permissions: an org-admin alone
 * may delete, because that un-shares every run the folder held.
 */
import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Folder, FolderOpen, Pencil, Plus, Trash2, X } from 'lucide-react'
import type { RunGroup } from './useGroups'
// The filter type lives with the shared state it describes — the sidebar's
// quick-access list writes the same value this row does.
import type { GroupFilter } from './RunGroupsContext'

interface Props {
  groups: RunGroup[]
  ungroupedCount: number
  active: GroupFilter
  onSelect: (value: GroupFilter) => void
  onCreate: (name: string) => Promise<unknown>
  onRename: (groupId: string, name: string) => Promise<unknown>
  onDelete: (groupId: string) => Promise<unknown>
  /** May this caller RENAME this group? (its creator, or an org-admin).
   *  A hint only — the server 404s either way. */
  canRename: (group: RunGroup) => boolean
  /** May this caller DELETE it? Narrower than canRename: an org-admin only.
   *  Deleting returns every run inside to Ungrouped, which un-shares them
   *  from the whole org, so it is not the folder creator's call to make. */
  canDelete: (group: RunGroup) => boolean
  /** False without an identity: the server refuses every mutation with 403,
   *  so offering the control would only produce a dead end. */
  canCreate: boolean
}

/** One overlay at a time. `from: 'browse'` returns there after a successful
 *  edit/delete, so managing several groups doesn't mean reopening the list. */
type Overlay =
  | { kind: 'browse' }
  | { kind: 'create' }
  | { kind: 'edit'; group: RunGroup; from?: 'browse' }
  | { kind: 'delete'; group: RunGroup; from?: 'browse' }
  | null

/** Folder names are unique within the org, so the name alone identifies the
 *  row an action belongs to. */
const actionLabel = (verb: string, group: RunGroup) => `${verb} ${group.name}`

export function GroupChipsRow({
  groups, ungroupedCount, active, onSelect,
  onCreate, onRename, onDelete, canRename, canDelete, canCreate,
}: Props) {
  const [overlay, setOverlay] = useState<Overlay>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const returnFocusRef = useRef<HTMLElement | null>(null)

  const activeGroup = groups.find(g => g.group_id === active) ?? null

  const open = (next: Overlay, initialName = '') => {
    // These dialogs are controlled and render no <DialogTrigger>, so Radix has
    // nothing to restore focus to on close and it drops to <body>. Remember the
    // control that opened the chain — only at its START, so browse → edit keeps
    // the original opener rather than an Edit button that is about to unmount.
    if (!overlay) returnFocusRef.current = document.activeElement as HTMLElement | null
    setName(initialName)
    setError('')
    setBusy(false)
    setOverlay(next)
  }

  /** Close, or fall back to the browse list the action was launched from. */
  const dismiss = () => {
    setBusy(false)
    setOverlay(prev =>
      prev && (prev.kind === 'edit' || prev.kind === 'delete') && prev.from === 'browse'
        ? { kind: 'browse' }
        : null,
    )
  }

  /** Hand focus back to whatever opened the chain, but only once the chain has
   *  actually ENDED. An intermediate transition (browse → edit, or edit →
   *  Cancel → browse) must leave focus to the overlay taking over. */
  const restoreFocus = (event: Event) => {
    if (overlay) return
    const opener = returnFocusRef.current
    returnFocusRef.current = null
    if (!opener || !document.body.contains(opener)) return
    event.preventDefault()
    opener.focus()
  }

  const submit = async () => {
    if (!overlay) return
    setBusy(true)
    setError('')
    try {
      if (overlay.kind === 'create') await onCreate(name.trim())
      else if (overlay.kind === 'edit') {
        if (name.trim() !== overlay.group.name) {
          await onRename(overlay.group.group_id, name.trim())
        }
      } else if (overlay.kind === 'delete') await onDelete(overlay.group.group_id)
      dismiss()
    } catch (e) {
      // Server refusals are actionable — a name the org already uses answers
      // 409 naming it. Show the server's own words and leave the dialog open
      // so the user can act on them.
      setError(e instanceof Error ? e.message : 'Something went wrong')
      setBusy(false)
    }
  }

  const pickGroup = (groupId: string) => {
    onSelect(groupId)
    setOverlay(null)
  }

  const nameDialogOpen = overlay?.kind === 'create' || overlay?.kind === 'edit'
  const submitDisabled = busy || !name.trim() || (
    overlay?.kind === 'edit' && name.trim() === overlay.group.name
  )

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <Button
        size="sm"
        variant={active === 'ungrouped' ? 'secondary' : 'ghost'}
        className={`h-7 gap-1.5 rounded-full text-xs ${active === 'ungrouped' ? '' : 'text-muted-foreground'}`}
        title={active === 'ungrouped' ? 'Clear the filter' : 'Show only runs that are in no group'}
        onClick={() => onSelect(active === 'ungrouped' ? null : 'ungrouped')}
      >
        Ungrouped
        <span className="text-muted-foreground">· {ungroupedCount}</span>
      </Button>

      {/* The whole taxonomy behind one control — "All Groups" until a group is
          filtered, then that group's own chip. Either way it opens the list. */}
      {groups.length > 0 && (
        activeGroup ? (
          <span className="inline-flex items-center">
            <Button
              size="sm"
              variant="secondary"
              className="h-7 gap-1.5 rounded-full rounded-r-none pr-2 text-xs"
              title="Switch or manage groups"
              onClick={() => open({ kind: 'browse' })}
            >
              <Folder className="h-3 w-3" />
              {activeGroup.name}
              <span className="text-muted-foreground">· {activeGroup.run_count}</span>
            </Button>
            <Button
              size="sm"
              variant="secondary"
              className="h-7 rounded-full rounded-l-none border-l border-background px-1.5"
              title="Clear the group filter"
              aria-label="Clear the group filter"
              onClick={() => onSelect(null)}
            >
              <X className="h-3 w-3" />
            </Button>
          </span>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            className="h-7 gap-1.5 rounded-full text-xs text-muted-foreground"
            title="Browse and manage groups"
            onClick={() => open({ kind: 'browse' })}
          >
            <FolderOpen className="h-3 w-3" />
            All Groups
            {/* Labelled on purpose: the chips either side of this one count
                RUNS, so a bare "· 3" here would read as three runs. */}
            <span className="text-muted-foreground">
              · {groups.length} group{groups.length === 1 ? '' : 's'}
            </span>
          </Button>
        )
      )}

      {canCreate && (
        <Button
          size="sm"
          variant="outline"
          className="h-7 gap-1 rounded-full border-dashed text-xs text-muted-foreground"
          onClick={() => open({ kind: 'create' })}
        >
          <Plus className="h-3 w-3" /> New
        </Button>
      )}

      {/* Browse: pick a group to filter, or edit/delete the ones you manage. */}
      <Dialog open={overlay?.kind === 'browse'} onOpenChange={o => { if (!o) setOverlay(null) }}>
        <DialogContent className="sm:max-w-md" onCloseAutoFocus={restoreFocus}>
          <DialogHeader>
            <DialogTitle>All Groups</DialogTitle>
            <DialogDescription>
              Pick a group to filter the runs. Everyone in your organization
              sees the same groups and the tests inside them.
            </DialogDescription>
          </DialogHeader>

          <div className="-mx-1 max-h-[320px] overflow-y-auto">
            {groups.length === 0 ? (
              <p className="px-1 py-6 text-center text-xs text-muted-foreground">
                {canCreate
                  ? 'No groups yet — close this and use ＋ New to create one.'
                  : 'No groups yet.'}
              </p>
            ) : groups.map(g => (
              <div
                key={g.group_id}
                className={`group flex items-center gap-2 rounded-md px-2 py-2 text-sm hover:bg-muted/60 ${
                  g.group_id === active ? 'bg-muted' : ''
                }`}
              >
                <button
                  type="button"
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  title={`Show only ${g.name}`}
                  onClick={() => pickGroup(g.group_id)}
                >
                  <Folder className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
                  <span className="truncate">{g.name}</span>
                  <span className="shrink-0 text-xs text-muted-foreground">
                    · {g.run_count} run{g.run_count === 1 ? '' : 's'}
                  </span>
                </button>
                {canRename(g) && (
                  <Button
                    variant="ghost" size="icon" className="h-7 w-7 shrink-0"
                    title="Rename group"
                    aria-label={actionLabel('Rename', g)}
                    onClick={() => open({ kind: 'edit', group: g, from: 'browse' }, g.name)}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                )}
                {canDelete(g) && (
                  <Button
                    variant="ghost" size="icon" className="h-7 w-7 shrink-0"
                    title="Delete group (runs are kept, but stop being shared)"
                    aria-label={actionLabel('Delete', g)}
                    onClick={() => open({ kind: 'delete', group: g, from: 'browse' })}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                )}
              </div>
            ))}
          </div>

          <DialogFooter className="sm:justify-between">
            {active && activeGroup ? (
              <Button
                size="sm" variant="outline"
                onClick={() => { onSelect(null); setOverlay(null) }}
              >
                Clear filter
              </Button>
            ) : <span />}
            <Button size="sm" variant="outline" onClick={() => setOverlay(null)}>Close</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Create / Rename share one form — a name, and nothing else to decide:
          a group is the org's, so there is no visibility to choose. */}
      <Dialog open={nameDialogOpen} onOpenChange={o => { if (!o) dismiss() }}>
        <DialogContent className="sm:max-w-sm" onCloseAutoFocus={restoreFocus}>
          <DialogHeader>
            <DialogTitle>{overlay?.kind === 'edit' ? 'Rename group' : 'New group'}</DialogTitle>
            <DialogDescription>
              A group holds the tests your team has finished. Everyone in the
              organization can see a group and the tests inside it, so moving
              a test here is how you share it.
            </DialogDescription>
          </DialogHeader>
          <form onSubmit={e => { e.preventDefault(); void submit() }} className="flex flex-col gap-2">
            <Input
              autoFocus
              value={name}
              maxLength={60}
              onChange={e => setName(e.target.value)}
              placeholder="e.g. Checkout flows"
            />
            {error && <p className="text-xs text-destructive">{error}</p>}
            <DialogFooter className="mt-2">
              <Button type="button" size="sm" variant="outline" onClick={dismiss}>Cancel</Button>
              <Button type="submit" size="sm" disabled={submitDisabled}>
                {overlay?.kind === 'edit' ? 'Rename' : 'Create'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation. Runs survive, but they return to Ungrouped —
          which now means only their own author sees them again, so the copy
          has to say that and not just "the runs are safe". */}
      <Dialog open={overlay?.kind === 'delete'} onOpenChange={o => { if (!o) dismiss() }}>
        <DialogContent className="sm:max-w-sm" onCloseAutoFocus={restoreFocus}>
          <DialogHeader>
            <DialogTitle>
              Delete “{overlay?.kind === 'delete' ? overlay.group.name : ''}”?
            </DialogTitle>
            <DialogDescription>
              The runs are NOT deleted — they return to Ungrouped, where only
              the person who ran each one can see it. Move them into another
              group to share them again.
            </DialogDescription>
          </DialogHeader>
          {error && <p className="text-xs text-destructive">{error}</p>}
          <DialogFooter>
            <Button size="sm" variant="outline" onClick={dismiss}>Cancel</Button>
            <Button size="sm" variant="destructive" disabled={busy} onClick={() => void submit()}>
              Delete group
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
