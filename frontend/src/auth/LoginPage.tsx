import { useState, type FormEvent, type ReactNode } from 'react'
import { supabase } from '../lib/supabase'
import { Button, ErrorNote, Field, InfoNote, Input, Spinner } from '../components/ui'

/**
 * One login. NO persona picker — and its absence is the security fix.
 *
 * This form used to offer a persona dropdown and write it into
 * `raw_user_meta_data`, which `handle_new_user` read. That object is the
 * client-supplied `data` field of a public signup, so the persona was chosen by
 * whoever was signing up. Combined with migration 1100 auto-confirming email,
 * and three policies that carried the commercials wall but no reach conjunct,
 * ONE unauthenticated request reached 1,026 trainer identities and 1,025
 * writable payment rails. Migration 2100 closed it: every signup now lands on
 * the `trainer` sentinel, which since 1800 matches no policy anywhere.
 *
 * The comment that used to sit here argued the picker was safe because "a fresh
 * Manager signup sees an empty console until an admin assigns them colleges".
 * That was true of every reach-scoped table and false for exactly those three.
 * It is a good example of a rationale that is locally correct and globally
 * wrong — do not reintroduce the field on the strength of a similar argument.
 *
 * `is_admin` was never taken from metadata, and that defence was right; it is
 * why this stopped short of handing out admin.
 *
 * More importantly: a new internal account has NO assignments, and internal
 * reach comes from `user_college_assignments` / `user_cluster_assignments`, not
 * from the persona. So a fresh Manager signup sees an empty console until an
 * admin assigns them colleges on Users & roles. That is least privilege
 * working, not a bug to route around.
 */
export function LoginPage() {
  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [fullName, setFullName] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function onSubmit(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)

    try {
      if (mode === 'signin') {
        const { error } = await supabase.auth.signInWithPassword({ email, password })
        if (error) throw error
        return
      }

      const { data, error } = await supabase.auth.signUp({
        email,
        password,
        // `role` is deliberately NOT sent. `handle_new_user` ignores it since
        // migration 2100, and sending a field the server discards invites the
        // next reader to 'fix' the server to honour it.
        options: { data: { full_name: fullName || null } },
      })
      if (error) throw error

      // Signup normally returns a session and AuthProvider takes over from here.
      //
      // It returns `{ user, session: null }` when the project still has "Confirm
      // email" switched on — a GoTrue setting this form cannot override. That
      // used to be a dead end shown as an error. It no longer is: migration
      // 1100 confirms every auth.users row on INSERT, so the account is usable
      // the moment it exists and a plain password sign-in gets the session GoTrue
      // withheld. No link, no OTP, no waiting on mail.
      if (data.user && !data.session) {
        const { error: signInError } = await supabase.auth.signInWithPassword({ email, password })
        if (signInError) throw signInError
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="min-h-dvh grid md:grid-cols-2">
      {/* THE BRAND PANEL. This screen is the only one anybody sees before they
          are anybody, and it carried no brand at all - a white column and a
          form. `navy` is byteXL's deepest published ground, so the console
          announces itself in the palette it then uses throughout rather than
          introducing a colour that appears nowhere else.

          It is content, not decoration: a person landing here has usually been
          sent a link and does not necessarily know what this system is. The
          sentence and the four bullets answer that in the time it takes to type
          an email address. Hidden below md, where it would push the form off
          the fold to say something nobody is reading on a phone. */}
      <div className="hidden md:flex flex-col justify-between p-10 lg:p-12 bg-navy text-navy-ink">
        <div className="flex items-center gap-2.5">
          <Logo />
          <span className="font-semibold tracking-tight">Ops Console</span>
        </div>

        <div className="max-w-md">
          <h1 className="text-3xl lg:text-4xl font-semibold tracking-tight leading-tight">
            Every college engagement, from MoU to final payout.
          </h1>
          <p className="mt-4 text-navy-ink/70 leading-relaxed">
            byteXL's operations record: one pipeline per program, one checklist per
            stage, and one place where what was agreed, what was delivered and what
            is owed are the same set of facts.
          </p>

          <ul className="mt-8 space-y-3.5">
            <Point>Track a program through every stage, with the tasks each stage owes.</Point>
            <Point>Mark trainer attendance per day, per deployment - never a wide sheet.</Point>
            <Point>
              Compute payouts in Python against the signed work order, and check them
              before anybody approves.
            </Point>
            <Point>Publish reports to a college only once a human has approved them.</Point>
          </ul>
        </div>

        <p className="text-xs text-navy-ink/55 max-w-md leading-relaxed">
          Access is enforced by Postgres row-level security, not by this UI. What you
          can see is decided by your persona and your assignments, in the database.
        </p>
      </div>

      {/* The form. `canvas`, so it reads as the app rather than as the panel. */}
      <div className="flex items-center justify-center p-6 bg-canvas">
        <div className="w-full max-w-sm">
          <div className="md:hidden flex items-center gap-2.5 mb-8">
            <Logo />
            <span className="font-semibold tracking-tight">Ops Console</span>
          </div>

          <h2 className="text-xl font-semibold tracking-tight text-ink">
            {mode === 'signin' ? 'Sign in' : 'Create an account'}
          </h2>
          <p className="text-sm text-ink-2 mt-1 mb-6">
            {mode === 'signin'
              ? 'Your persona decides which console you land on.'
              : 'Pick the persona for this account.'}
          </p>

          <div className="glass-strong rounded-card border border-line shadow-raised p-5">
            <form onSubmit={onSubmit} className="space-y-4">
              {mode === 'signup' && (
                <>
                  <Field label="Full name">
                    <Input
                      value={fullName}
                      onChange={(e) => setFullName(e.target.value)}
                      placeholder="Priya Nair"
                      autoComplete="name"
                    />
                  </Field>
                  {/* There is deliberately NO persona picker here. See the
                      module docstring: a signup that chose its own persona was
                      a critical vulnerability, closed by migration 2100. Since
                      the server now ignores any role sent at signup, offering
                      the choice would be worse than not offering it — the
                      account would land on the deny-all sentinel while the
                      person believed they had signed up as a Manager, and the
                      empty console that followed would look like a bug. */}
                  <InfoNote>
                    New accounts start with no access. An admin assigns your
                    persona and your colleges on <strong>Users &amp; roles</strong>,
                    and the console fills in once they have. Educators do not sign
                    in at all — they are managed as records by internal staff.
                  </InfoNote>
                </>
              )}

              <Field label="Email">
                <Input
                  type="email"
                  required
                  value={email}
                  onChange={(e) => setEmail(e.target.value)}
                  placeholder="you@bytexl.in"
                  autoComplete="email"
                />
              </Field>

              <Field label="Password">
                <Input
                  type="password"
                  required
                  minLength={6}
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  placeholder="••••••••"
                  autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
                />
              </Field>

              {error && <ErrorNote>{error}</ErrorNote>}

              <Button type="submit" variant="primary" disabled={busy} className="w-full">
                {busy && <Spinner />}
                {mode === 'signin' ? 'Sign in' : 'Create account'}
              </Button>
            </form>
          </div>

          <p className="text-sm text-ink-2 mt-4 text-center">
            {mode === 'signin' ? "Don't have an account? " : 'Already have one? '}
            <button
              type="button"
              className="font-medium text-accent hover:underline"
              onClick={() => {
                setMode(mode === 'signin' ? 'signup' : 'signin')
                setError(null)
              }}
            >
              {mode === 'signin' ? 'Sign up' : 'Sign in'}
            </button>
          </p>
        </div>
      </div>
    </div>
  )
}

/** One capability line in the brand panel. Tick plus a sentence, nothing more. */
function Point({ children }: { children: ReactNode }) {
  return (
    <li className="flex gap-3 items-start text-sm text-navy-ink/80 leading-relaxed">
      <svg
        width="16"
        height="16"
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden
        className="mt-0.5 shrink-0 text-accent"
      >
        <path
          d="M5 12.5l4.5 4.5L19 7.5"
          stroke="currentColor"
          strokeWidth="2.4"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span>{children}</span>
    </li>
  )
}

function Logo() {
  return (
    <span className="inline-flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-on-accent">
      <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden>
        <path
          d="M5 12.5l4.5 4.5L19 7.5"
          stroke="currentColor"
          strokeWidth="2.6"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </span>
  )
}
