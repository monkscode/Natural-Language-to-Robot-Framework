/**
 * Group filter row — the Test Runs page's folder bar.
 *
 * Deliberately fixed-width: the row never grows with the number of groups.
 * It carries at most three controls — Ungrouped, the groups control, and
 * "＋ New" — so creating twenty groups cannot push the page's toolbar down.
 *
 * The groups control is the whole taxonomy behind one button: it reads
 * "All Groups" while no group is filtered, and becomes the selected group's
 * own chip (with an ✕ to clear) once one is. Clicking it either way opens the
 * browse dialog, which lists every group with its run count and is also where
 * rename/delete live — so group management has one obvious home instead of
 * icons that only appear beside an active chip.
 */
import { useState } from 'react'
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
}

/** One overlay at a time. `from: 'browse'` returns there after a successful
 *  rename/delete, so managing several groups doesn't mean reopening the list. */
type Overlay =
  | { kind: 'browse' }
  | { kind: 'create' }
  | { kind: 'rename'; group: RunGroup; from?: 'browse' }
  | { kind: 'delete'; group: RunGroup; from?: 'browse' }
  | null

export function GroupChipsRow({
  groups, ungroupedCount, active, onSelect, onCreate, onRename, onDelete,
}: Props) {
  const [overlay, setOverlay] = useState<Overlay>(null)
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const activeGroup = groups.find(g => g.group_id === active) ?? null

  const open = (next: Overlay, initialName = '') => {
    setName(initialName)
    setError('')
    setBusy(false)
    setOverlay(next)
  }

  /** Close, or fall back to the browse list the action was launched from. */
  const dismiss = () => {
    setBusy(false)
    setOverlay(prev =>
      prev && (prev.kind === 'rename' || prev.kind === 'delete') && prev.from === 'browse'
        ? { kind: 'browse' }
        : null,
    )
  }

  const submit = async () => {
    if (!overlay) return
    setBusy(true)
    setError('')
    try {
      if (overlay.kind === 'create') await onCreate(name.trim())
      else if (overlay.kind === 'rename') await onRename(overlay.group.group_id, name.trim())
      else if (overlay.kind === 'delete') await onDelete(overlay.group.group_id)
      dismiss()
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong')
      setBusy(false)
    }
  }

  const pickGroup = (groupId: string) => {
    onSelect(groupId)
    setOverlay(null)
  }

  const nameDialogOpen = overlay?.kind === 'create' || overlay?.kind === 'rename'

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
            title="Browse and manage your groups"
            onClick={() => open({ kind: 'browse' })}
          >
            <FolderOpen className="h-3 w-3" />
            All Groups
            <span className="text-muted-foreground">· {groups.length}</span>
          </Button>
        )
      )}

      <Button
        size="sm"
        variant="outline"
        className="h-7 gap-1 rounded-full border-dashed text-xs text-muted-foreground"
        onClick={() => open({ kind: 'create' })}
      >
        <Plus className="h-3 w-3" /> New
      </Button>

      {/* Browse: pick a group to filter, or rename/delete it in place. */}
      <Dialog open={overlay?.kind === 'browse'} onOpenChange={o => { if (!o) setOverlay(null) }}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>All Groups</DialogTitle>
            <DialogDescription>
              Pick a group to filter the runs, or rename and delete them here.
            </DialogDescription>
          </DialogHeader>

          <div className="-mx-1 max-h-[320px] overflow-y-auto">
            {groups.length === 0 ? (
              <p className="px-1 py-6 text-center text-xs text-muted-foreground">
                No groups yet — close this and use ＋ New to create one.
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
                <Button
                  variant="ghost" size="icon" className="h-7 w-7 shrink-0"
                  title="Rename group" aria-label={`Rename ${g.name}`}
                  onClick={() => open({ kind: 'rename', group: g, from: 'browse' }, g.name)}
                >
                  <Pencil className="h-3.5 w-3.5" />
                </Button>
                <Button
                  variant="ghost" size="icon" className="h-7 w-7 shrink-0"
                  title="Delete group (runs are kept)" aria-label={`Delete ${g.name}`}
                  onClick={() => open({ kind: 'delete', group: g, from: 'browse' })}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </Button>
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

      {/* Create / Rename share one name form. */}
      <Dialog open={nameDialogOpen} onOpenChange={o => { if (!o) dismiss() }}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>{overlay?.kind === 'rename' ? 'Rename group' : 'New group'}</DialogTitle>
            <DialogDescription>
              Groups are personal folders for your runs — only you see them.
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
              <Button type="submit" size="sm" disabled={busy || !name.trim()}>
                {overlay?.kind === 'rename' ? 'Rename' : 'Create'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      {/* Delete confirmation — runs survive, they just return to Ungrouped. */}
      <Dialog open={overlay?.kind === 'delete'} onOpenChange={o => { if (!o) dismiss() }}>
        <DialogContent className="sm:max-w-sm">
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
