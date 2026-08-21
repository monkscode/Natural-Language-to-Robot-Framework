/**
 * Group filter row — the Test Runs page's folder bar.
 *
 * Deliberately fixed-width: the row never grows with the number of groups.
 * It carries at most three controls — Ungrouped, the groups control, and
 * "＋ New" — so creating twenty groups cannot push the page's toolbar down.
 * That matters more now that groups are org-shared and the list gets longer.
 *
 * The groups control is the whole taxonomy behind one button: it reads
 * "All Groups" while no group is filtered, and becomes the selected group's
 * own chip (with an ✕ to clear) once one is. Clicking it either way opens the
 * browse dialog, which lists every group with its run count and is also where
 * edit/delete live — so group management has one obvious home instead of
 * icons that only appear beside an active chip.
 *
 * Management affordances are offered only where the caller may actually use
 * them (canManage / canCreate); the server refuses the rest regardless.
 */
import { useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import { Folder, FolderOpen, Pencil, Plus, Trash2, X } from 'lucide-react'
import type { GroupChanges, GroupVisibility, RunGroup } from './useGroups'
import { PrivateLock, VisibilityField } from './GroupVisibility'
// The filter type lives with the shared state it describes — the sidebar's
// quick-access list writes the same value this row does.
import type { GroupFilter } from './RunGroupsContext'

interface Props {
  groups: RunGroup[]
  ungroupedCount: number
  active: GroupFilter
  onSelect: (value: GroupFilter) => void
  onCreate: (name: string, visibility: GroupVisibility) => Promise<unknown>
  onUpdate: (groupId: string, changes: GroupChanges) => Promise<unknown>
  onDelete: (groupId: string) => Promise<unknown>
  /** May this caller rename/delete this group? (creator, or org-admin on
   *  a shared group). A hint only — the server 404s either way. */
  canManage: (group: RunGroup) => boolean
  /** May this caller change WHO CAN SEE this group? Narrower than canManage:
   *  only the creator, never an org-admin acting on someone else's group — a
   *  hint only, the server 403s either way. */
  canChangeVisibility: (group: RunGroup) => boolean
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

/** A private and a shared group may legitimately carry the SAME name, and the
 *  lock beside it is the only thing separating them — so an action's
 *  accessible name has to carry what the lock carries visually. */
const actionLabel = (verb: string, group: RunGroup) =>
  `${verb} ${group.name}${group.visibility === 'private' ? ' (private)' : ''}`

export function GroupChipsRow({
  groups, ungroupedCount, active, onSelect,
  onCreate, onUpdate, onDelete, canManage, canChangeVisibility, canCreate,
}: Props) {
  const [overlay, setOverlay] = useState<Overlay>(null)
  const [name, setName] = useState('')
  const [visibility, setVisibility] = useState<GroupVisibility>('org')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const returnFocusRef = useRef<HTMLElement | null>(null)

  const activeGroup = groups.find(g => g.group_id === active) ?? null

  const open = (next: Overlay, initialName = '', initialVisibility: GroupVisibility = 'org') => {
    // These dialogs are controlled and render no <DialogTrigger>, so Radix has
    // nothing to restore focus to on close and it drops to <body>. Remember the
    // control that opened the chain — only at its START, so browse → edit keeps
    // the original opener rather than an Edit button that is about to unmount.
    if (!overlay) returnFocusRef.current = document.activeElement as HTMLElement | null
    setName(initialName)
    setVisibility(initialVisibility)
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

  /** Only what actually changed — an empty PATCH body is a 400. */
  const pendingChanges = (group: RunGroup): GroupChanges => {
    const changes: GroupChanges = {}
    if (name.trim() && name.trim() !== group.name) changes.name = name.trim()
    if (visibility !== group.visibility) changes.visibility = visibility
    return changes
  }

  const submit = async () => {
    if (!overlay) return
    setBusy(true)
    setError('')
    try {
      if (overlay.kind === 'create') await onCreate(name.trim(), visibility)
      else if (overlay.kind === 'edit') {
        const changes = pendingChanges(overlay.group)
        if (Object.keys(changes).length) await onUpdate(overlay.group.group_id, changes)
      } else if (overlay.kind === 'delete') await onDelete(overlay.group.group_id)
      dismiss()
    } catch (e) {
      // Server refusals are actionable: a shared group still holding other
      // members' runs cannot go private, and that 409 names the count. Show
      // the server's own words and leave the dialog open so the user can act.
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
    overlay?.kind === 'edit' && Object.keys(pendingChanges(overlay.group)).length === 0
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
              {activeGroup.visibility === 'private' && <PrivateLock name={activeGroup.name} />}
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
              Pick a group to filter the runs. A lock marks a private group,
              visible only to whoever created it.
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
                  {g.visibility === 'private' && (
                    <PrivateLock name={g.name} className="h-3.5 w-3.5 text-muted-foreground" />
                  )}
                  <span className="shrink-0 text-xs text-muted-foreground">
                    · {g.run_count} run{g.run_count === 1 ? '' : 's'}
                  </span>
                </button>
                {canManage(g) && (
                  <>
                    <Button
                      variant="ghost" size="icon" className="h-7 w-7 shrink-0"
                      title={canChangeVisibility(g) ? 'Edit group (name and who can see it)' : 'Edit group (name)'}
                      aria-label={actionLabel('Edit', g)}
                      onClick={() => open({ kind: 'edit', group: g, from: 'browse' }, g.name, g.visibility)}
                    >
                      <Pencil className="h-3.5 w-3.5" />
                    </Button>
                    <Button
                      variant="ghost" size="icon" className="h-7 w-7 shrink-0"
                      title="Delete group (runs are kept)" aria-label={actionLabel('Delete', g)}
                      onClick={() => open({ kind: 'delete', group: g, from: 'browse' })}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </>
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

      {/* Create / Edit share one form: a name plus, on create or an edit by
          the folder's creator, who can see it — canChangeVisibility below
          hides that field on an admin's edit of someone else's folder. PATCH
          takes both in one call, so flipping visibility needs no second dialog. */}
      <Dialog open={nameDialogOpen} onOpenChange={o => { if (!o) dismiss() }}>
        <DialogContent className="sm:max-w-sm" onCloseAutoFocus={restoreFocus}>
          <DialogHeader>
            <DialogTitle>{overlay?.kind === 'edit' ? 'Edit group' : 'New group'}</DialogTitle>
            <DialogDescription>
              Groups keep test runs together. A shared group is visible to
              everyone in the organization; a private one only to whoever
              created it.
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
            {(overlay?.kind !== 'edit' || canChangeVisibility(overlay.group)) && (
              <VisibilityField
                id="group-chips-visibility"
                value={visibility}
                onChange={setVisibility}
                disabled={busy}
              />
            )}
            {error && <p className="text-xs text-destructive">{error}</p>}
            <DialogFooter className="mt-2">
              <Button type="button" size="sm" variant="outline" onClick={dismiss}>Cancel</Button>
              <Button type="submit" size="sm" disabled={submitDisabled}>
                {overlay?.kind === 'edit' ? 'Save' : 'Create'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation — runs survive, they just return to Ungrouped. */}
      <Dialog open={overlay?.kind === 'delete'} onOpenChange={o => { if (!o) dismiss() }}>
        <DialogContent className="sm:max-w-sm" onCloseAutoFocus={restoreFocus}>
          <DialogHeader>
            <DialogTitle>
              Delete “{overlay?.kind === 'delete' ? overlay.group.name : ''}”?
            </DialogTitle>
            <DialogDescription>
              Runs in this group are NOT deleted — they return to Ungrouped.
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
