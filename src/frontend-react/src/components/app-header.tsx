import { useLocation } from 'react-router-dom'
import { SidebarTrigger } from '@/components/ui/sidebar'
import { Separator } from '@/components/ui/separator'
import { Button } from '@/components/ui/button'
import {
  Breadcrumb,
  BreadcrumbItem,
  BreadcrumbLink,
  BreadcrumbList,
  BreadcrumbPage,
  BreadcrumbSeparator,
} from '@/components/ui/breadcrumb'
import { Sun, Moon, Monitor, Zap, Bell } from 'lucide-react'
import { useTheme } from '@/components/theme-provider'
import { cn } from '@/lib/utils'

const PAGE_LABELS: Record<string, string> = {
  '/generate':  'Generate',
  '/history':   'History',
  '/metrics':   'Metrics',
  '/templates': 'Templates',
  '/settings':  'Settings',
  '/docs':      'Documentation',
}

export function AppHeader() {
  const { pathname } = useLocation()
  const { mode, setMode, theme, setTheme } = useTheme()
  const pageLabel = PAGE_LABELS[pathname] ?? 'Overview'

  return (
    <header className="flex h-14 shrink-0 items-center justify-between border-b bg-background px-4 transition-[width,height] ease-linear group-has-[[data-collapsible=icon]]/sidebar-wrapper:h-12">

      {/* Left: collapse trigger + breadcrumb */}
      <div className="flex items-center gap-2">
        <SidebarTrigger className="-ml-1" />
        <Separator orientation="vertical" className="mr-1 h-4" />
        <Breadcrumb>
          <BreadcrumbList>
            <BreadcrumbItem className="hidden md:block">
              <BreadcrumbLink className="text-muted-foreground hover:text-foreground">
                Platform
              </BreadcrumbLink>
            </BreadcrumbItem>
            <BreadcrumbSeparator className="hidden md:block" />
            <BreadcrumbItem>
              <BreadcrumbPage>{pageLabel}</BreadcrumbPage>
            </BreadcrumbItem>
          </BreadcrumbList>
        </Breadcrumb>
      </div>

      {/* Right: theme controls + notifications */}
      <div className="flex items-center gap-1.5">

        {/* Light / Dark / System toggle */}
        <div className="flex items-center rounded-md border bg-muted/60 p-0.5 gap-0.5">
          {(
            [
              { id: 'light',  Icon: Sun,     label: 'Light mode'   },
              { id: 'dark',   Icon: Moon,    label: 'Dark mode'    },
              { id: 'system', Icon: Monitor, label: 'System preference' },
            ] as const
          ).map(({ id, Icon, label }) => (
            <button
              key={id}
              title={label}
              aria-label={label}
              onClick={() => setMode(id)}
              className={cn(
                'flex h-7 w-7 items-center justify-center rounded transition-all',
                mode === id
                  ? 'bg-background text-foreground shadow-sm'
                  : 'text-muted-foreground hover:text-foreground',
              )}
            >
              <Icon className="h-3.5 w-3.5" />
            </button>
          ))}
        </div>

        {/* Professional ↔ Neo toggle */}
        <button
          onClick={() => setTheme(theme === 'neo' ? 'professional' : 'neo')}
          aria-label="Toggle neobrutalism theme"
          className={cn(
            'flex h-8 items-center gap-1.5 rounded-md border px-2.5 text-xs font-semibold tracking-wide transition-all',
            theme === 'neo'
              ? 'border-black bg-[#ccff00] text-black'
              : 'bg-transparent text-muted-foreground hover:bg-accent hover:text-accent-foreground',
          )}
        >
          <Zap className="h-3 w-3" />
          {theme === 'neo' ? 'Pro' : 'Neo'}
        </button>

        <Separator orientation="vertical" className="mx-0.5 h-4" />

        <Button variant="ghost" size="icon" className="h-8 w-8" aria-label="Notifications">
          <Bell className="h-4 w-4" />
        </Button>
      </div>
    </header>
  )
}
