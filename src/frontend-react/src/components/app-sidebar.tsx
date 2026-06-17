import { Link, useLocation, useNavigate } from 'react-router-dom'
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarRail,
} from '@/components/ui/sidebar'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import {
  Zap,
  History,
  BarChart2,
  Brain,
  LayoutGrid,
  Settings,
  LogOut,
  ChevronsUpDown,
} from 'lucide-react'
import { useAuth } from '@/auth/AuthContext'

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
}

const NAV_PLATFORM: NavItem[] = [
  { title: 'Generate', url: '/generate', icon: Zap, admin: false },
  { title: 'History', url: '/history', icon: History, admin: false },
  { title: 'Metrics', url: '/metrics', icon: BarChart2, admin: true },
  { title: 'Learning', url: '/learning', icon: Brain, admin: true },
  { title: 'Templates', url: '/templates', icon: LayoutGrid, admin: true },
]

const NAV_WORKSPACE: NavItem[] = [
  { title: 'Settings', url: '/settings', icon: Settings, admin: true },
]

/* ── User footer with context menu ── */
function NavUser() {
  const navigate = useNavigate()
  const { user, logout } = useAuth()

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

          <DropdownMenuContent side="top" align="end" sideOffset={4} className="w-56 rounded-lg">
            <div className="px-2 py-1.5 border-b mb-1">
              <p className="text-sm font-semibold">{label}</p>
              <p className="text-xs text-muted-foreground">{user?.email}</p>
              <p className="mt-0.5 text-[10px] font-medium uppercase tracking-wider text-muted-foreground">
                {user?.role}
              </p>
            </div>
            <DropdownMenuItem
              className="text-destructive focus:text-destructive"
              onSelect={handleLogout}
            >
              <LogOut className="mr-2 h-4 w-4" /> Sign out
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}

/* ── Renders one nav group, hiding admin-only items from regular users ── */
function NavGroup({ label, items, isAdmin, pathname }: {
  label: string
  items: NavItem[]
  isAdmin: boolean
  pathname: string
}) {
  const visible = items.filter(item => !item.admin || isAdmin)
  if (visible.length === 0) return null
  return (
    <SidebarGroup>
      <SidebarGroupLabel>{label}</SidebarGroupLabel>
      <SidebarMenu>
        {visible.map(item => (
          <SidebarMenuItem key={item.title}>
            <SidebarMenuButton asChild isActive={pathname === item.url} tooltip={item.title}>
              <Link to={item.url}>
                <item.icon />
                <span>{item.title}</span>
              </Link>
            </SidebarMenuButton>
          </SidebarMenuItem>
        ))}
      </SidebarMenu>
    </SidebarGroup>
  )
}

/* ── Main sidebar component ── */
export function AppSidebar() {
  const { pathname } = useLocation()
  const { isAdmin } = useAuth()

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
        <NavGroup label="Platform" items={NAV_PLATFORM} isAdmin={isAdmin} pathname={pathname} />
        <NavGroup label="Workspace" items={NAV_WORKSPACE} isAdmin={isAdmin} pathname={pathname} />
      </SidebarContent>

      {/* User footer */}
      <SidebarFooter>
        <NavUser />
      </SidebarFooter>

      <SidebarRail />
    </Sidebar>
  )
}
