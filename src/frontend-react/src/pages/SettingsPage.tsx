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
  options?: { value: string; label: string }[]
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
        options: [
          { value: 'gemini', label: 'Google Gemini' },
          { value: 'openai', label: 'OpenAI' },
          { value: 'anthropic', label: 'Anthropic (Claude)' },
        ],
      },
      {
        id: 'model', label: 'Model', type: 'select', defaultValue: 'gemini-2.5-flash',
        options: [
          { value: 'gemini-2.5-flash', label: 'gemini-2.5-flash' },
          { value: 'gemini-2.0-pro', label: 'gemini-2.0-pro' },
          { value: 'gpt-4o', label: 'gpt-4o' },
          { value: 'claude-3-5-sonnet', label: 'claude-3-5-sonnet' },
        ],
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
        options: [
          { value: 'browser', label: 'Browser (Playwright)' },
        ],
      },
      {
        id: 'browser', label: 'Browser', type: 'select', defaultValue: 'chromium',
        options: [
          { value: 'chromium', label: 'chromium' },
          { value: 'firefox', label: 'firefox' },
          { value: 'webkit', label: 'webkit' },
        ],
      },
      {
        id: 'headless', label: 'Headless Mode', type: 'select', defaultValue: 'true',
        options: [
          { value: 'true', label: 'Enabled (headless=True)' },
          { value: 'false', label: 'Disabled (headless=False)' },
        ],
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
              <Select defaultValue={field.defaultValue} disabled>
                <SelectTrigger id={field.id} className="h-9 text-sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {field.options?.map(opt => (
                    <SelectItem key={opt.value} value={opt.value} className="text-sm">
                      {opt.label}
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
                disabled
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
        {/* This page is a mockup of a feature that does not exist. There is no
            settings route and no settings table in src/backend/: MODEL_PROVIDER,
            GEMINI_API_KEY and the rest are process-level .env values read once
            at startup. Every control below is therefore disabled rather than
            wired — wiring them up means persistence, auth scoping and a
            hot-reload story for boot-time values, which is a feature, not a fix.
            The API Key field is the one that could do real damage: it invites a
            platform admin to paste a live secret and then swallows it. */}
        <div
          role="status"
          className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900"
        >
          <p className="font-medium">This page is not active yet.</p>
          <p className="mt-0.5 text-xs">
            Nothing here is saved and nothing is read back from the server — the values shown are
            placeholders. Change these settings in <code>src/backend/.env</code> and restart the
            backend; they are read once at startup.
          </p>
        </div>

        {SECTIONS.map(section => (
          <SettingsSection key={section.title} section={section} />
        ))}

        <Separator />

        <div className="flex justify-end gap-3 pb-6">
          <Button variant="outline" disabled>Discard changes</Button>
          <Button disabled>Save settings</Button>
        </div>
      </div>
    </div>
  )
}
