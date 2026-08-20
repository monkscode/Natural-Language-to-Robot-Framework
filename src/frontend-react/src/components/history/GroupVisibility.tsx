/**
 * Shared visibility affordances for run-group folders.
 *
 * Folders are org-scoped with a per-folder visibility: 'org' (everyone in the
 * organization) or 'private' (only its creator). Two folders may legitimately
 * carry the SAME name — one private, one shared — so the lock beside a name is
 * the only thing telling them apart. It therefore renders everywhere a folder
 * name appears: the chip row, the browse dialog, the Move menu, the sidebar
 * sub-tree, the row tag and the detail drawer.
 *
 * Referenced by: GroupChipsRow, MoveToGroupMenu, app-sidebar, pages/HistoryPage.
 * Depends on: ./useGroups (the GroupVisibility union).
 */
import { Lock, Users } from 'lucide-react'
import type { GroupVisibility } from './useGroups'

/** Lock marker — never a bare glyph: it carries its own title and a11y name. */
export function PrivateLock({ name, className = 'h-3 w-3' }: {
  name?: string
  className?: string
}) {
  const label = name
    ? `${name} is private — only you can see it`
    : 'Private folder — only you can see it'
  return (
    <span role="img" aria-label={label} title={label} className="inline-flex shrink-0">
      <Lock aria-hidden="true" className={className} />
    </span>
  )
}

/**
 * The private/org chooser. Used by BOTH create dialogs (the chip row's ＋ New
 * and the Move menu's "New group…") and by the edit dialog, so the same words
 * describe the same choice wherever it is made.
 */
export function VisibilityField({ id, value, onChange, disabled }: {
  id: string
  value: GroupVisibility
  onChange: (next: GroupVisibility) => void
  disabled?: boolean
}) {
  return (
    <fieldset className="flex flex-col gap-1.5 pt-1" disabled={disabled}>
      <legend className="pb-1 text-xs font-medium text-muted-foreground">
        Who can see this folder
      </legend>
      <label htmlFor={`${id}-org`} className="flex items-center gap-2 text-xs">
        <input
          id={`${id}-org`}
          type="radio"
          name={id}
          value="org"
          className="h-3.5 w-3.5 accent-primary"
          checked={value === 'org'}
          onChange={() => onChange('org')}
        />
        <Users aria-hidden="true" className="h-3.5 w-3.5 text-muted-foreground" />
        <span>Everyone in my organization</span>
      </label>
      <label htmlFor={`${id}-private`} className="flex items-center gap-2 text-xs">
        <input
          id={`${id}-private`}
          type="radio"
          name={id}
          value="private"
          className="h-3.5 w-3.5 accent-primary"
          checked={value === 'private'}
          onChange={() => onChange('private')}
        />
        <Lock aria-hidden="true" className="h-3.5 w-3.5 text-muted-foreground" />
        <span>Only me</span>
      </label>
    </fieldset>
  )
}
