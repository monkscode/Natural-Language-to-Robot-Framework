import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select'
import { Separator } from '@/components/ui/separator'

interface Field {
  id: string
  label: string
  type: 'text' | 'password' | 'select'
  placeholder?: string
  defaultValue: string
  options?: string[]
}

interface Section {
  title: string
  description: string
  fields: Field[]
}

const SECTIONS: Section[] = [
  {
    title: 'AI Provider',
    description: 'Configure which AI model generates your test code.',
    fields: [
      {
        id: 'provider', label: 'Model Provider', type: 'select', defaultValue: 'gemini',
        options: ['Google Gemini', 'OpenAI', 'Anthropic (Claude)'],
      },
      {
        id: 'model', label: 'Model', type: 'select', defaultValue: 'gemini-2.5-flash',
        options: ['gemini-2.5-flash', 'gemini-2.0-pro', 'gpt-4o', 'claude-3-5-sonnet'],
      },
      {
        id: 'apiKey', label: 'API Key', type: 'password',
        placeholder: 'Enter your Gemini API key…',
        defaultValue: '',
      },
    ],
  },
  {
    title: 'Robot Framework',
    description: 'Choose which browser automation library to use for test generation.',
    fields: [
      {
        id: 'library', label: 'Library', type: 'select', defaultValue: 'browser',
        options: ['Browser (Playwright) — Recommended', 'SeleniumLibrary — Legacy'],
      },
      {
        id: 'browser', label: 'Browser', type: 'select', defaultValue: 'chromium',
        options: ['chromium', 'firefox', 'webkit'],
      },
      {
        id: 'headless', label: 'Headless Mode', type: 'select', defaultValue: 'true',
        options: ['Enabled (headless=True)', 'Disabled (headless=False)'],
      },
    ],
  },
  {
    title: 'Server & Execution',
    description: 'Ports, timeouts and service URLs for your local environment.',
    fields: [
      { id: 'port',        label: 'App Port',                  type: 'text',     placeholder: '5000',                      defaultValue: '5000' },
      { id: 'serviceUrl',  label: 'Browser Service URL',       type: 'text',     placeholder: 'http://localhost:4999',      defaultValue: 'http://localhost:4999' },
      { id: 'timeout',     label: 'Execution Timeout (s)',     type: 'text',     placeholder: '900',                        defaultValue: '900' },
    ],
  },
]

function SettingsSection({ section }: { section: Section }) {
  return (
    <Card>
      <CardHeader className="pb-4">
        <CardTitle className="text-sm">{section.title}</CardTitle>
        <CardDescription className="text-xs">{section.description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        {section.fields.map(field => (
          <div key={field.id} className="grid gap-1.5">
            <Label htmlFor={field.id} className="text-sm">{field.label}</Label>
            {field.type === 'select' ? (
              <Select defaultValue={field.defaultValue}>
                <SelectTrigger id={field.id} className="h-9 text-sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {field.options?.map(opt => (
                    <SelectItem key={opt} value={opt.split(' ')[0].toLowerCase()} className="text-sm">
                      {opt}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            ) : (
              <Input
                id={field.id}
                type={field.type}
                placeholder={field.placeholder}
                defaultValue={field.defaultValue}
                className="h-9 text-sm"
              />
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  )
}

export default function SettingsPage() {
  return (
    <div className="mx-auto max-w-2xl">
      <div className="mb-5">
        <h1 className="text-xl font-bold tracking-tight">Settings</h1>
        <p className="mt-0.5 text-sm text-muted-foreground">
          Configure your AI provider, automation library and execution environment
        </p>
      </div>

      <div className="space-y-4">
        {SECTIONS.map(section => (
          <SettingsSection key={section.title} section={section} />
        ))}

        <Separator />

        <div className="flex justify-end gap-3 pb-6">
          <Button variant="outline">Discard changes</Button>
          <Button>Save settings</Button>
        </div>
      </div>
    </div>
  )
}
