import { useEffect, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuAction,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarMenuSub,
  SidebarMenuSubButton,
  SidebarMenuSubItem,
  SidebarRail,
} from '@/components/ui/sidebar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  Zap,
  History,
  BarChart2,
  Brain,
  Settings,
  LogOut,
  MonitorSmartphone,
  Loader2,
  ChevronDown,
  ChevronsUpDown,
  Folder,
  ShieldCheck,
  Users,
} from 'lucide-react'
import { useAuth } from '@/auth/AuthContext'
import { useRunGroups } from '@/components/history/RunGroupsContext'

/* ── Logo mark ── */
const LogoMark = () => (
  <svg width="28" height="28" viewBox="0 0 32 32" fill="none" aria-hidden="true">
    <rect width="32" height="32" rx="7" fill="#18181b" />
    <path d="M7 10 L7 22 L19 16 Z" fill="white" />
    <path d="M22 10 Q29.5 16 22 22" stroke="#F97316" strokeWidth="2.5" strokeLinecap="round" fill="none" />
    <circle cx="28.5" cy="16" r="1.8" fill="#F97316" />
  </svg>
)

/* ── Navigation structure (admin: true = hidden from regular users) ── */
interface NavItem {
  title: string
  url: string
  icon: typeof Zap
  admin: boolean
  orgAdmin?: boolean
  /** Renders the expandable run-group quick-access list under this item. */
  groups?: boolean
}

const NAV_PLATFORM: NavItem[] = [
  { title: 'Generate', url: '/generate', icon: Zap, admin: false },
  { title: 'Test Runs', url: '/history', icon: History, admin: false, groups: true },
  { title: 'Metrics', url: '/metrics', icon: BarChart2, admin: true },
  { title: 'Learning', url: '/learning', icon: Brain, admin: true },
  { title: 'Access', url: '/access', icon: ShieldCheck, admin: true },
  { title: 'Team', url: '/team', icon: Users, admin: false, orgAdmin: true },
]

const NAV_WORKSPACE: NavItem[] = [
  { title: 'Settings', url: '/settings', icon: Settings, admin: true },
]

/* ── User footer with context menu ── */
function NavUser() {
  const navigate = useNavigate()
  const { user, logout, logoutAll } = useAuth()
  const [signingOutAll, setSigningOutAll] = useState(false)

  const label = user?.display_name?.trim() || user?.email || 'User'
  const initials = label
    .split(' ')
    .map(n => n[0])
    .join('')
    .slice(0, 2)
    .toUpperCase()

  function handleLogout() {
    logout()
    navigate('/login', { replace: true })
  }

  async function handleLogoutAll() {
    setSigningOutAll(true)
    try {
      await logoutAll()
      navigate('/login', { replace: true })
    } finally {
      setSigningOutAll(false)
    }
  }

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <SidebarMenuButton
              size="lg"
              className="data-[state=open]:bg-sidebar-accent data-[state=open]:text-sidebar-accent-foreground"
            >
              <Avatar className="h-8 w-8 rounded-lg">
                <AvatarFallback className="rounded-lg bg-sidebar-primary text-sidebar-primary-foreground text-xs font-bold">
                  {initials}
                </AvatarFallback>
              </Avatar>
              <div className="grid flex-1 text-left text-sm leading-tight">
                <span className="truncate font-semibold">{label}</span>
                <span className="truncate text-xs text-muted-foreground">{user?.email}</span>
              </div>
              <ChevronsUpDown className="ml-auto size-4 text-muted-foreground" />
            </SidebarMenuButton>
          </DropdownMenuTrigger>

          <DropdownMenuContent side="top" align="end" sideOffset={4} className="w-64 rounded-lg">
            <div className="px-2 py-1.5 border-b mb-1">
              <p className="text-sm font-semibold">{label}</p>
              <p className="text-xs text-muted-foreground">{user?.email}</p>
              <p className="mt-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                {user?.role}
              </p>
            </div>

            <DropdownMenuItem className="gap-2 py-2" onSelect={handleLogout}>
              <LogOut className="h-4 w-4 text-muted-foreground" />
              <div className="grid leading-tight">
                <span className="text-sm">Sign out</span>
                <span className="text-[11px] text-muted-foreground">This device only</span>
              </div>
            </DropdownMenuItem>

            <DropdownMenuSeparator />

            <DropdownMenuItem
              className="gap-2 py-2 text-destructive focus:text-destructive"
              disabled={signingOutAll}
              onSelect={e => {
                // Keep the menu open while the revoke request is in flight.
                e.preventDefault()
                handleLogoutAll()
              }}
            >
              {signingOutAll ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <MonitorSmartphone className="h-4 w-4" />
              )}
              <div className="grid leading-tight">
                <span className="text-sm">
                  {signingOutAll ? 'Signing out…' : 'Sign out of all devices'}
                </span>
                <span className="text-[11px] text-muted-foreground">
                  Ends every active session
                </span>
              </div>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}

/* ── Test Runs + its run-group quick access ──
 *
 * The row keeps its normal behaviour: the label navigates to the page. The
 * chevron beside it is a separate control that expands the user's groups, so
 * jumping straight to one group's runs takes a single click from anywhere in
 * the app.
 *
 * The list is read-only on purpose — creating, renaming and deleting groups
 * all live on the page itself. A nav sidebar answers "where do I go", and
 * mixing management controls into it would give those actions two homes.
 *
 * The active group comes from RunGroupsContext, the same value the page's chip
 * row writes, so picking a group here or there always agrees. */
function GroupsNavItem({ item, pathname }: { item: NavItem; pathname: string }) {
  const navigate = useNavigate()
  const { groups, groupFilter, setGroupFilter } = useRunGroups()
  const [open, setOpen] = useState(false)

  // Reveal the active group the way a file tree opens to the selected file:
  // when a group is chosen on the PAGE, the sidebar expands to show where you
  // are. Keyed on the filter alone so a background refresh of the group list
  // never re-opens a list the user deliberately collapsed.
  useEffect(() => {
    if (groupFilter && groupFilter !== 'ungrouped') setOpen(true)
  }, [groupFilter])

  const subId = 'sidebar-run-groups'

  const pick = (groupId: string) => {
    setGroupFilter(groupId)
    // Already on the page? Only the filter changes — no navigation, so the
    // page keeps its scroll position and open drawer.
    if (pathname !== item.url) navigate(item.url)
  }

  return (
    <SidebarMenuItem>
      <SidebarMenuButton asChild isActive={pathname === item.url} tooltip={item.title}>
        <Link to={item.url}>
          <item.icon />
          <span>{item.title}</span>
        </Link>
      </SidebarMenuButton>

      {/* The chevron shows even with no groups: a user who has never made one
          would otherwise get no hint anywhere in the nav that groups exist,
          and they are exactly who needs to find the feature. */}
      <SidebarMenuAction
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        aria-controls={subId}
        aria-label={open ? 'Hide groups' : 'Show groups'}
        title={open ? 'Hide groups' : 'Show groups'}
      >
        <ChevronDown
          className={`transition-transform duration-200 ${open ? 'rotate-180' : ''}`}
        />
      </SidebarMenuAction>

      {open && (
        <SidebarMenuSub id={subId} className="max-h-[40vh] overflow-y-auto">
          {groups.length === 0 ? (
            // Empty states point somewhere. Naming where groups come from
            // turns this click into how the feature gets discovered — and it
            // stays a signpost, not a second place to create them.
            <li className="px-2 py-1.5 text-xs leading-snug text-sidebar-foreground/60">
              No groups yet — create one on Test Runs.
            </li>
          ) : groups.map(g => (
            <SidebarMenuSubItem key={g.group_id}>
              <SidebarMenuSubButton
                asChild
                size="sm"
                isActive={groupFilter === g.group_id}
              >
                <button
                  type="button"
                  onClick={() => pick(g.group_id)}
                  title={`Show only ${g.name}`}
                >
                  <Folder />
                  <span className="truncate">{g.name}</span>
                  <span className="ml-auto shrink-0 text-xs tabular-nums text-sidebar-foreground/60">
                    {g.run_count}
                  </span>
                </button>
              </SidebarMenuSubButton>
            </SidebarMenuSubItem>
          ))}
        </SidebarMenuSub>
      )}
    </SidebarMenuItem>
  )
}

/* ── Renders one nav group, hiding admin-only items from regular users ── */
function NavGroup({ label, items, isAdmin, isOrgAdmin, pathname }: {
  label: string
  items: NavItem[]
  isAdmin: boolean
  isOrgAdmin: boolean
  pathname: string
}) {
  const visible = items.filter(item => (!item.admin || isAdmin) && (!item.orgAdmin || isOrgAdmin))
  if (visible.length === 0) return null
  return (
    <SidebarGroup>
      <SidebarGroupLabel>{label}</SidebarGroupLabel>
      <SidebarMenu>
        {visible.map(item => (
          item.groups ? (
            <GroupsNavItem key={item.title} item={item} pathname={pathname} />
          ) : (
            <SidebarMenuItem key={item.title}>
              <SidebarMenuButton asChild isActive={pathname === item.url} tooltip={item.title}>
                <Link to={item.url}>
                  <item.icon />
                  <span>{item.title}</span>
                </Link>
              </SidebarMenuButton>
            </SidebarMenuItem>
          )
        ))}
      </SidebarMenu>
    </SidebarGroup>
  )
}

/* ── Main sidebar component ── */
export function AppSidebar() {
  const { pathname } = useLocation()
  const { isAdmin, isOrgAdmin } = useAuth()

  return (
    <Sidebar collapsible="icon">
      {/* Header — logo + app name */}
      <SidebarHeader>
        <SidebarMenu>
          <SidebarMenuItem>
            <SidebarMenuButton size="lg" asChild>
              <Link to="/generate" className="flex items-center gap-3">
                <LogoMark />
                <span className="font-bold text-base tracking-tight">Mark 1</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        </SidebarMenu>
      </SidebarHeader>

      {/* Nav — role-filtered */}
      <SidebarContent>
        <NavGroup label="Platform" items={NAV_PLATFORM} isAdmin={isAdmin} isOrgAdmin={isOrgAdmin} pathname={pathname} />
        <NavGroup label="Workspace" items={NAV_WORKSPACE} isAdmin={isAdmin} isOrgAdmin={isOrgAdmin} pathname={pathname} />
      </SidebarContent>

      {/* User footer */}
      <SidebarFooter>
        <NavUser />
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  )
}
