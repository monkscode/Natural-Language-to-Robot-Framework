/**
 * "Move to group…" dropdown — the single filing control used by the History
 * row actions, the detail drawer, and the bulk-select toolbar. Lists the
 * user's groups (check-marks the run's current one), offers Remove from
 * group, and can create-and-move in one gesture via the New group… item.
 */
import { useState, type ReactNode } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel,
  DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Check, Folder, FolderMinus, Plus } from 'lucide-react'
import type { RunGroup } from './useGroups'

interface Props {
  groups: RunGroup[]
  /** The run's current group — check-marked; pass undefined for bulk moves. */
  currentGroupId?: string | null
  /** Always show "Remove from group" — for bulk moves, where there is no
      single currentGroupId but the selection may contain grouped runs. */
  showRemove?: boolean
  onMove: (groupId: string | null) => void
  onCreateGroup: (name: string) => Promise<RunGroup>
  /** The button that opens the menu (wrapped with asChild). */
  trigger: ReactNode
}

export function MoveToGroupMenu({ groups, currentGroupId, showRemove, onMove, onCreateGroup, trigger }: Props) {
  const [creating, setCreating] = useState(false)
  const [name, setName] = useState('')
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  const createAndMove = async () => {
    setBusy(true)
    setError('')
    try {
      const g = await onCreateGroup(name.trim())
      setCreating(false)
      onMove(g.group_id)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Something went wrong')
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>{trigger}</DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-52">
          <DropdownMenuLabel className="text-xs">Move to group</DropdownMenuLabel>
          {groups.map(g => (
            <DropdownMenuItem
              key={g.group_id}
              className="gap-2 text-xs"
              onClick={() => onMove(g.group_id)}
            >
              <Folder className="h-3.5 w-3.5" />
              <span className="flex-1 truncate">{g.name}</span>
              {currentGroupId === g.group_id && <Check className="h-3.5 w-3.5" />}
            </DropdownMenuItem>
          ))}
          {groups.length === 0 && (
            <p className="px-2 py-1.5 text-xs text-muted-foreground">No groups yet.</p>
          )}
          {(currentGroupId || showRemove) && (
            <DropdownMenuItem className="gap-2 text-xs" onClick={() => onMove(null)}>
              <FolderMinus className="h-3.5 w-3.5" /> Remove from group
            </DropdownMenuItem>
          )}
          <DropdownMenuSeparator />
          <DropdownMenuItem
            className="gap-2 text-xs"
            onClick={() => { setName(''); setError(''); setBusy(false); setCreating(true) }}
          >
            <Plus className="h-3.5 w-3.5" /> New group…
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      <Dialog open={creating} onOpenChange={o => { if (!o) { setCreating(false); setBusy(false) } }}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>New group</DialogTitle>
            <DialogDescription>The selected runs move into it right away.</DialogDescription>
          </DialogHeader>
          <form
            onSubmit={e => { e.preventDefault(); void createAndMove() }}
            className="flex flex-col gap-2"
          >
            <Input
              autoFocus
              value={name}
              maxLength={60}
              onChange={e => setName(e.target.value)}
              placeholder="e.g. Checkout flows"
            />
            {error && <p className="text-xs text-destructive">{error}</p>}
            <DialogFooter className="mt-2">
              <Button type="button" size="sm" variant="outline" onClick={() => setCreating(false)}>
                Cancel
              </Button>
              <Button type="submit" size="sm" disabled={busy || !name.trim()}>
                Create &amp; move
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
