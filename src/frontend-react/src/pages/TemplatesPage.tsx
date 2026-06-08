import { Button } from '@/components/ui/button'
import { Plus } from 'lucide-react'

export default function TemplatesPage() {
  return (
    <div className="mx-auto max-w-4xl">
      <div className="mb-5 flex items-center justify-between">
        <div>
          <h1 className="text-xl font-bold tracking-tight">Templates</h1>
          <p className="mt-0.5 text-sm text-muted-foreground">
            Save and reuse common test patterns across projects
          </p>
        </div>
        <Button size="sm" className="gap-1.5">
          <Plus className="h-3.5 w-3.5" /> New Template
        </Button>
      </div>

      <div className="flex flex-col items-center justify-center py-24 text-center">
        <span className="text-5xl mb-4 opacity-60">🗂️</span>
        <h2 className="text-base font-semibold mb-1">No templates yet</h2>
        <p className="text-sm text-muted-foreground max-w-[280px] mb-5">
          Generate a test and save it as a template to quickly reuse it for similar scenarios.
        </p>
        <Button variant="outline" size="sm">Browse example templates</Button>
      </div>
    </div>
  )
}
