import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardHeader } from '@/components/ui/card'
import { Eye, RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'

type Status = 'pass' | 'fail' | 'running'

interface Run {
  id: string
  status: Status
  query: string
  duration: string
  tokens: string | number
  date: string
}

const RUNS: Run[] = [
  { id: 'wf-001', status: 'pass',    query: 'Open Flipkart, search for shoes, get first product name', duration: '18.3s', tokens: 4820, date: '2026-05-28 14:02' },
  { id: 'wf-002', status: 'fail',    query: 'Navigate to GitHub monkscode and get pinned project name', duration: '22.1s', tokens: 6340, date: '2026-05-28 13:45' },
  { id: 'wf-003', status: 'pass',    query: 'Go to Amazon, search wireless headphones, add top result to cart', duration: '31.5s', tokens: 7100, date: '2026-05-27 16:20' },
  { id: 'wf-004', status: 'pass',    query: 'Open YouTube, search Python automation, play first video', duration: '14.8s', tokens: 3920, date: '2026-05-27 11:05' },
  { id: 'wf-005', status: 'running', query: 'Visit Twitter, search for Robot Framework, like first tweet', duration: '—', tokens: '—', date: '2026-05-28 14:18' },
]

const STATUS_BADGE: Record<Status, JSX.Element> = {
  pass:    <Badge className="bg-green-100 text-green-700 border-green-200 hover:bg-green-100 text-xs">Passed</Badge>,
  fail:    <Badge className="bg-red-100 text-red-700 border-red-200 hover:bg-red-100 text-xs">Failed</Badge>,
  running: (
    <Badge variant="secondary" className="gap-1.5 text-xs">
      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-500" />
      Running
    </Badge>
  ),
}

type Filter = 'all' | Status

export default function HistoryPage() {
  const [filter, setFilter] = useState<Filter>('all')
  const visible = filter === 'all' ? RUNS : RUNS.filter(r => r.status === filter)

  return (
    <div className="mx-auto max-w-5xl">
      <div className="mb-5">
        <h1 className="text-xl font-bold tracking-tight">Test History</h1>
        <p className="mt-0.5 text-sm text-muted-foreground">
          Browse and re-run your previous test executions
        </p>
      </div>

      <Card>
        <CardHeader className="flex-row items-center justify-between space-y-0 pb-3 px-5">
          <div className="flex gap-1.5">
            {(['all', 'pass', 'fail'] as const).map(f => (
              <Button
                key={f}
                size="sm"
                variant={filter === f ? 'default' : 'outline'}
                className="h-7 text-xs"
                onClick={() => setFilter(f)}
              >
                {f === 'all' ? 'All Runs' : f === 'pass' ? '✓ Passed' : '✗ Failed'}
              </Button>
            ))}
          </div>
          <span className="text-xs text-muted-foreground">{visible.length} result{visible.length !== 1 ? 's' : ''}</span>
        </CardHeader>

        <CardContent className="p-0">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/40">
                  <th className="py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Status</th>
                  <th className="py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground">Description</th>
                  <th className="py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground hidden md:table-cell">ID</th>
                  <th className="py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground hidden sm:table-cell">Duration</th>
                  <th className="py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground hidden lg:table-cell">Tokens</th>
                  <th className="py-2.5 px-4 text-left text-xs font-semibold uppercase tracking-wider text-muted-foreground hidden sm:table-cell">Date</th>
                  <th className="py-2.5 px-4"></th>
                </tr>
              </thead>
              <tbody>
                {visible.map(row => (
                  <tr key={row.id} className="border-b last:border-0 hover:bg-muted/30 transition-colors">
                    <td className="py-3 px-4">{STATUS_BADGE[row.status]}</td>
                    <td className="py-3 px-4 max-w-xs">
                      <span className="line-clamp-1 text-sm">{row.query}</span>
                    </td>
                    <td className="py-3 px-4 hidden md:table-cell">
                      <code className="text-xs text-muted-foreground font-mono">{row.id}</code>
                    </td>
                    <td className="py-3 px-4 text-xs text-muted-foreground hidden sm:table-cell">{row.duration}</td>
                    <td className="py-3 px-4 text-xs text-muted-foreground hidden lg:table-cell">{row.tokens.toLocaleString()}</td>
                    <td className="py-3 px-4 text-xs text-muted-foreground hidden sm:table-cell whitespace-nowrap">{row.date}</td>
                    <td className="py-3 px-4">
                      <div className="flex gap-1">
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="View details">
                          <Eye className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className={cn('h-7 w-7', row.status === 'running' && 'opacity-40 pointer-events-none')} title="Re-run">
                          <RefreshCw className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
