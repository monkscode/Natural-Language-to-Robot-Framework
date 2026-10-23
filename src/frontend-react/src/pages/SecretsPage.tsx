import { useEffect, useState } from 'react'
import { api, ApiError } from '@/lib/api'
import { formatDate } from '@/lib/time'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'

interface Secret { name: string; updated_at: string }

// The server's upload cap (bytes). Checked here too so a big file is never sent.
const MAX_UPLOAD_BYTES = 65536

const message = (e: unknown, fallback: string) => (e instanceof ApiError ? e.message : fallback)

export default function SecretsPage() {
  const [secrets, setSecrets] = useState<Secret[]>([])
  const [loaded, setLoaded] = useState(false) // true after a successful GET: "none yet" is then true
  const [name, setName] = useState('')
  const [value, setValue] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  async function load() {
    try {
      setSecrets(await api<Secret[]>('/api/secrets'))
      setLoaded(true)
    } catch (e) {
      setError(message(e, 'Failed to load secrets'))
    }
  }
  useEffect(() => { void load() }, [])

  function start() {
    setError(null)
    setNotice(null)
  }

  async function save() {
    start()
    const typed = name.trim()
    try {
      const r = await api<{ name: string; replaced: boolean }>(
        `/api/secrets/${encodeURIComponent(typed)}`,
        { method: 'PUT', body: JSON.stringify({ value }) },
      )
      setNotice(`${r.replaced ? 'Replaced' : 'Saved'} ${r.name}.`)
      setName('')
      setValue('')
      await load()
    } catch (e) {
      setError(message(e, 'Failed to save the secret'))
    }
  }

  async function upload(file: File | undefined, input: HTMLInputElement) {
    start()
    if (!file) return
    try {
      if (file.size > MAX_UPLOAD_BYTES) {
        setError('That file is over 64 KB. Split it and upload the parts.')
        return
      }
      const r = await api<{ added: string[]; replaced: string[] }>('/api/secrets/upload', {
        method: 'POST',
        body: JSON.stringify({ content: await file.text() }),
      })
      setNotice([
        r.added.length ? `Added: ${r.added.join(', ')}.` : '',
        r.replaced.length ? `Replaced: ${r.replaced.join(', ')}.` : '',
      ].filter(Boolean).join(' ') || 'The file held no secrets.')
      await load()
    } catch (e) {
      setError(message(e, 'Failed to upload the file'))
    } finally {
      input.value = '' // lets the same file be chosen again
    }
  }

  async function remove(secretName: string) {
    if (!window.confirm(`Delete ${secretName}?`)) return
    start()
    try {
      await api(`/api/secrets/${encodeURIComponent(secretName)}`, { method: 'DELETE' })
      await load()
    } catch (e) {
      setError(message(e, 'Failed to delete the secret'))
      void load() // a 404 means another member deleted it: show the list as it is now
    }
  }

  return (
    <div className="flex max-w-2xl flex-col gap-6">
      <Card>
        <CardHeader className="pb-4">
          <CardTitle className="text-sm">Secrets</CardTitle>
          <CardDescription className="text-xs">
            Passwords and keys your tests type. Values are encrypted and never shown again.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-xs text-muted-foreground">
            Write {'{{NAME}}'} in a test request — for example: type {'{{SAUCE_PASSWORD}}'} in the
            password field. The AI never sees the value; the test reads it when it runs.
          </p>
          <p className="text-xs text-muted-foreground">
            Every member of your organization can use, replace and delete these.
          </p>
          {error && <p role="alert" className="text-xs text-destructive">{error}</p>}
          {notice && <p role="status" className="text-xs">{notice}</p>}
          {secrets.length === 0 ? (
            loaded && <p className="text-sm text-muted-foreground">No secrets yet.</p>
          ) : (
            <ul className="flex flex-col gap-1">
              {secrets.map(s => (
                <li key={s.name} className="flex items-center justify-between rounded-md border p-2 text-sm">
                  <span className="font-mono">{s.name}</span>
                  <span className="flex items-center gap-3">
                    <span className="text-xs text-muted-foreground">
                      {formatDate(s.updated_at)}
                    </span>
                    <Button variant="ghost" size="sm" aria-label={`Delete ${s.name}`}
                      onClick={() => void remove(s.name)}>
                      Delete
                    </Button>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-4">
          <CardTitle className="text-sm">Add or replace a secret</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-1.5">
            <Label htmlFor="secret-name" className="text-sm">Name</Label>
            <Input id="secret-name" value={name} onChange={e => setName(e.target.value)}
              placeholder="SAUCE_PASSWORD" autoComplete="off" className="h-9 text-sm" />
          </div>
          <div className="grid gap-1.5">
            <Label htmlFor="secret-value" className="text-sm">Value</Label>
            <Input id="secret-value" type="password" value={value}
              onChange={e => setValue(e.target.value)}
              autoComplete="new-password" className="h-9 text-sm" />
          </div>
          <Button disabled={!name.trim() || !value} onClick={() => void save()}>Save secret</Button>
          <div className="grid gap-1.5 border-t pt-4">
            <Label htmlFor="secret-file" className="text-sm">Upload a .env file</Label>
            <Input id="secret-file" type="file" className="h-9 text-sm"
              onChange={e => void upload(e.target.files?.[0], e.target)} />
            <p className="text-xs text-muted-foreground">
              Upload a .env file — one NAME=value per line. Names you already have are replaced.
            </p>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
