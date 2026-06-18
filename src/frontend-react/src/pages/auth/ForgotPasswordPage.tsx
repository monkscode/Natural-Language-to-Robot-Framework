import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@/components/ui/card'
import { ArrowLeft, CheckCircle2 } from 'lucide-react'
import { api } from '@/lib/api'

function LogoMark({ size = 40 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" fill="none" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="#18181b" />
      <path d="M7 10 L7 22 L19 16 Z" fill="white" />
      <path d="M22 10 Q29.5 16 22 22" stroke="#F97316" strokeWidth="2.5" strokeLinecap="round" fill="none" />
      <circle cx="28.5" cy="16" r="1.8" fill="#F97316" />
    </svg>
  )
}

/** Success state — shown after email is sent */
function CheckEmailState({ email, onResend, resent }: { email: string; onResend: () => void; resent: boolean }) {
  return (
    <Card>
      <CardContent className="flex flex-col items-center text-center pt-8 pb-6 gap-4">
        <div className="flex h-14 w-14 items-center justify-center rounded-full bg-green-50 border-2 border-green-200">
          <CheckCircle2 className="h-7 w-7 text-green-600" />
        </div>
        <div>
          <h2 className="text-xl font-bold tracking-tight mb-1">Check your email</h2>
          <p className="text-sm text-muted-foreground leading-relaxed">
            We sent a password reset link to{' '}
            <span className="font-semibold text-foreground">{email}</span>.
            <br />It expires in 15 minutes.
          </p>
        </div>
        <Button asChild className="w-full mt-1">
          <Link to="/login">Back to sign in</Link>
        </Button>
        <p className="text-xs text-muted-foreground">
          Didn&apos;t receive it?{' '}
          <button
            onClick={onResend}
            className="font-semibold text-foreground underline-offset-4 hover:underline"
          >
            {resent ? '✓ Sent again!' : 'Resend email'}
          </button>
        </p>
      </CardContent>
    </Card>
  )
}

export default function ForgotPasswordPage() {
  const [email,   setEmail]   = useState('')
  const [loading, setLoading] = useState(false)
  const [sent,    setSent]    = useState(false)
  const [resent,  setResent]  = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!email) return
    setLoading(true)
    // Stub endpoint: always succeeds and never reveals whether the email exists.
    try {
      await api('/auth/forgot-password', {
        method: 'POST', auth: false, body: JSON.stringify({ email }),
      })
    } catch {
      /* ignore — privacy: response is identical regardless */
    }
    setLoading(false)
    setSent(true)
  }

  async function handleResend() {
    try {
      await api('/auth/forgot-password', {
        method: 'POST', auth: false, body: JSON.stringify({ email }),
      })
    } catch {
      /* ignore */
    }
    setResent(true)
    setTimeout(() => setResent(false), 3000)
  }

  return (
    <div className="flex min-h-svh flex-col items-center justify-center bg-muted p-6 md:p-10">
      <div className="w-full max-w-sm">
        {sent ? (
          <CheckEmailState email={email} onResend={handleResend} resent={resent} />
        ) : (
          <div className="flex flex-col gap-6">
            <Card>
              <CardHeader className="text-center pb-2">
                <div className="flex justify-center mb-3">
                  <LogoMark size={44} />
                </div>
                <CardTitle className="text-xl">Reset your password</CardTitle>
                <CardDescription>
                  Enter your email and we&apos;ll send you a reset link
                </CardDescription>
              </CardHeader>

              <CardContent>
                <form onSubmit={handleSubmit}>
                  <div className="grid gap-5">
                    <div className="grid gap-2">
                      <Label htmlFor="email">Email address</Label>
                      <Input
                        id="email"
                        type="email"
                        placeholder="you@company.com"
                        value={email}
                        onChange={e => setEmail(e.target.value)}
                        autoFocus
                        autoComplete="email"
                        required
                      />
                    </div>

                    <Button type="submit" className="w-full gap-2" disabled={loading || !email}>
                      {loading && (
                        <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />
                      )}
                      {loading ? 'Sending…' : 'Send reset link'}
                    </Button>
                  </div>
                </form>
              </CardContent>
            </Card>

            <Link
              to="/login"
              className="flex items-center justify-center gap-1.5 text-sm text-muted-foreground hover:text-foreground transition-colors"
            >
              <ArrowLeft className="h-3.5 w-3.5" /> Back to sign in
            </Link>
          </div>
        )}
      </div>
    </div>
  )
}
