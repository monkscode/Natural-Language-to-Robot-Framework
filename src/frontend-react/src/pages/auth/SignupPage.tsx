import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
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
import { Eye, EyeOff } from 'lucide-react'
import { useAuth } from '@/auth/AuthContext'

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

function GoogleIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" aria-hidden="true">
      <path d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92c-.26 1.37-1.04 2.53-2.21 3.31v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.09z" fill="#4285F4" />
      <path d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z" fill="#34A853" />
      <path d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z" fill="#FBBC05" />
      <path d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z" fill="#EA4335" />
    </svg>
  )
}

function pwStrength(pw: string): 0 | 1 | 2 | 3 {
  if (!pw) return 0
  let s = 0
  if (pw.length >= 8) s++
  if (/[A-Z]/.test(pw) && /[a-z]/.test(pw)) s++
  if (/[0-9]/.test(pw)) s++
  return Math.min(s, 3) as 0 | 1 | 2 | 3
}

const STRENGTH_LABEL = ['', 'Weak', 'Fair', 'Strong'] as const
const STRENGTH_COLOR = ['bg-muted', 'bg-red-500', 'bg-yellow-500', 'bg-green-500'] as const

export default function SignupPage() {
  const navigate = useNavigate()
  const { signup } = useAuth()
  const [firstName, setFirstName] = useState('')
  const [lastName,  setLastName]  = useState('')
  const [email,     setEmail]     = useState('')
  const [password,  setPassword]  = useState('')
  const [confirm,   setConfirm]   = useState('')
  const [showPw,    setShowPw]    = useState(false)
  const [agreed,    setAgreed]    = useState(false)
  const [loading,   setLoading]   = useState(false)
  const [errors,    setErrors]    = useState<Record<string, string>>({})

  const strength = pwStrength(password)

  function validate() {
    const e: Record<string, string> = {}
    if (!firstName.trim())        e.firstName = 'Required'
    if (!email.includes('@'))     e.email     = 'Enter a valid email'
    if (password.length < 8)      e.password  = 'Minimum 8 characters'
    if (password !== confirm)     e.confirm   = 'Passwords do not match'
    if (!agreed)                  e.terms     = 'Please accept the terms'
    return e
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    const errs = validate()
    setErrors(errs)
    if (Object.keys(errs).length > 0) return
    setLoading(true)
    try {
      await signup(email, password, `${firstName} ${lastName}`.trim())
      navigate('/generate', { replace: true })
    } catch (err) {
      setErrors({ form: err instanceof Error ? err.message : 'Sign up failed' })
      setLoading(false)
    }
  }

  const FieldError = ({ field }: { field: string }) =>
    errors[field] ? <p className="text-xs text-destructive mt-1">{errors[field]}</p> : null

  return (
    <div className="flex min-h-svh flex-col items-center justify-center bg-muted p-6 md:p-10">
      <div className="w-full max-w-sm">
        <div className="flex flex-col gap-6">
          <Card>
            <CardHeader className="text-center pb-2">
              <div className="flex justify-center mb-3">
                <LogoMark size={44} />
              </div>
              <CardTitle className="text-xl">Create an account</CardTitle>
              <CardDescription>Start generating tests in seconds — free to try</CardDescription>
            </CardHeader>

            <CardContent>
              <form onSubmit={handleSubmit}>
                <div className="grid gap-4">
                  {/* Google — server-side flow starts at the backend */}
                  <Button
                    type="button"
                    variant="outline"
                    className="w-full gap-2"
                    onClick={() => { window.location.href = '/auth/google/login' }}
                  >
                    <GoogleIcon /> Sign up with Google
                  </Button>

                  {/* Divider */}
                  <div className="relative text-center text-xs text-muted-foreground after:absolute after:inset-0 after:top-1/2 after:z-0 after:flex after:items-center after:border-t after:border-border">
                    <span className="relative z-10 bg-card px-2">or continue with email</span>
                  </div>

                  {/* Name row */}
                  <div className="grid grid-cols-2 gap-3">
                    <div className="grid gap-1.5">
                      <Label htmlFor="firstName">First name</Label>
                      <Input id="firstName" placeholder="Alex" value={firstName} onChange={e => setFirstName(e.target.value)} />
                      <FieldError field="firstName" />
                    </div>
                    <div className="grid gap-1.5">
                      <Label htmlFor="lastName">Last name</Label>
                      <Input id="lastName" placeholder="Johnson" value={lastName} onChange={e => setLastName(e.target.value)} />
                    </div>
                  </div>

                  {/* Email */}
                  <div className="grid gap-1.5">
                    <Label htmlFor="email">Work email</Label>
                    <Input id="email" type="email" placeholder="you@company.com" value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" />
                    <FieldError field="email" />
                  </div>

                  {/* Password */}
                  <div className="grid gap-1.5">
                    <Label htmlFor="password">Password</Label>
                    <div className="relative">
                      <Input
                        id="password"
                        type={showPw ? 'text' : 'password'}
                        placeholder="Min. 8 characters"
                        value={password}
                        onChange={e => setPassword(e.target.value)}
                        autoComplete="new-password"
                        className="pr-10"
                      />
                      <button
                        type="button"
                        onClick={() => setShowPw(s => !s)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                        aria-label="Toggle password visibility"
                      >
                        {showPw ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </button>
                    </div>
                    {password.length > 0 && (
                      <div className="space-y-1">
                        <div className="flex gap-1">
                          {[1, 2, 3].map(n => (
                            <div key={n} className={`h-1 flex-1 rounded-full transition-colors ${n <= strength ? STRENGTH_COLOR[strength] : 'bg-muted'}`} />
                          ))}
                        </div>
                        <p className="text-xs text-muted-foreground">{STRENGTH_LABEL[strength]}</p>
                      </div>
                    )}
                    <FieldError field="password" />
                  </div>

                  {/* Confirm */}
                  <div className="grid gap-1.5">
                    <Label htmlFor="confirm">Confirm password</Label>
                    <Input id="confirm" type="password" placeholder="Re-enter password" value={confirm} onChange={e => setConfirm(e.target.value)} />
                    <FieldError field="confirm" />
                  </div>

                  {/* Terms */}
                  <div className="flex items-start gap-2">
                    <input
                      id="terms"
                      type="checkbox"
                      checked={agreed}
                      onChange={e => setAgreed(e.target.checked)}
                      className="mt-0.5 h-4 w-4 cursor-pointer accent-foreground"
                    />
                    <label htmlFor="terms" className="text-xs text-muted-foreground leading-relaxed cursor-pointer">
                      I agree that my test runs and feedback are recorded to improve generation quality
                    </label>
                  </div>
                  {errors.terms && <p className="text-xs text-destructive -mt-2">{errors.terms}</p>}
                  {errors.form && <p className="text-xs text-destructive">{errors.form}</p>}

                  {/* Submit */}
                  <Button type="submit" className="w-full gap-2" disabled={loading}>
                    {loading && <span className="h-4 w-4 animate-spin rounded-full border-2 border-current border-t-transparent" />}
                    {loading ? 'Creating account…' : 'Create account'}
                  </Button>

                  {/* Login link */}
                  <p className="text-center text-sm text-muted-foreground">
                    Already have an account?{' '}
                    <Link to="/login" className="font-semibold text-foreground underline-offset-4 hover:underline">
                      Sign in
                    </Link>
                  </p>
                </div>
              </form>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  )
}
