import { Suspense, lazy } from 'react'
import { BrowserRouter } from 'react-router-dom'
import { AuthProvider, useAuth } from './auth/AuthProvider'
import { LoginPage } from './auth/LoginPage'
import { Button, Card } from './components/ui'
import { PageSkeleton } from './components/AppShell'

/* --------------------------------------------------------------------------
   The two persona roots are split from the entry chunk as well as from each
   other, and this is the split that pays best: OpsRoot pulls in seventeen
   screens' worth of imports and CollegeRoot pulls in three read-only views, and
   NO ACCOUNT IS EVER BOTH. A college login downloading the whole ops console —
   every byte of which returns zero rows to it — is the clearest waste in the
   bundle, and `LoginPage` in front of it needs neither.

   As in OpsRoot: this is load timing, not authorisation. `Gate` below picks a
   root from `profile.role`, and its own comment is explicit that a trainer who
   edited their bundle to render <OpsRoot /> would get empty tables because RLS
   still applies. That remains exactly as true with the roots lazy-loaded.
-------------------------------------------------------------------------- */

const OpsRoot = lazy(() => import('./ops/OpsRoot').then((m) => ({ default: m.OpsRoot })))
const CollegeRoot = lazy(() =>
  import('./college/CollegeRoot').then((m) => ({ default: m.CollegeRoot })),
)

export default function App() {
  return (
    <AuthProvider>
      {/* Paired with `base` in vite.config.ts. BASE_URL is '/' on Vercel and
          '/Ops_automation/' on a GitHub Pages project site; without the
          basename every route would be matched one path segment too deep there
          and the app would render its not-found screen at its own home page.
          The trailing slash is stripped because React Router wants '/repo',
          not '/repo/'. */}
      <BrowserRouter basename={import.meta.env.BASE_URL.replace(/\/$/, '')}>
        {/* The same full-screen treatment `Gate` uses while the session and
            profile resolve, so signing in and loading the console read as one
            continuous wait rather than two different ones. */}
        <Suspense fallback={<FullScreenLoading label="Loading the console" />}>
          <Gate />
        </Suspense>
      </BrowserRouter>
    </AuthProvider>
  )
}

/**
 * Five personas, three front doors.
 *
 * CLAUDE.md §4 defines five personas. Three of them — Senior Manager, Manager,
 * LDE Executive — are byteXL staff running the same pipeline at different
 * reach, so they share the ops root; the difference between them is which
 * COLLEGES they are assigned to, and that is resolved in Postgres by
 * `my_college_ids()`, not here. Trainer and College are outward-facing and get
 * their own roots.
 *
 * THIS SWITCH IS NAVIGATION, NOT SECURITY. A trainer who edited their bundle to
 * render <OpsRoot /> would get a console full of empty tables, because every
 * query underneath still goes through RLS with their JWT. The wall is in
 * Postgres; this is only which door opens by default.
 *
 * The same is true one level down, inside OpsRoot: the LDE Executive is not
 * shown the commercials surfaces (work orders, and later P&L and remuneration).
 * Hiding them is cosmetic — `can_see_commercials()` returns false for that
 * persona, so `work_orders`, `remuneration_sheets` and `pnl` return ZERO ROWS
 * to them whatever the UI renders. Never rely on the UI for that boundary, and
 * never "temporarily" surface a commercials figure because a UI check passed.
 */
function Gate() {
  const { session, profile, loading, profileError, signOut } = useAuth()

  if (loading) return <FullScreenLoading label="Starting up" />

  if (!session) return <LoginPage />

  // Signed in, but no profile row. Should be impossible — handle_new_user
  // creates one on every auth.users insert — so say so plainly rather than
  // rendering a console that will silently show nothing.
  if (!profile) {
    return (
      <div className="min-h-dvh grid place-items-center p-6">
        <Card className="max-w-md p-6 text-center">
          <h1 className="text-base font-semibold text-ink">No profile found</h1>
          <p className="text-sm text-ink-2 mt-2 leading-relaxed">
            This account is authenticated but has no row in <code>profiles</code>, so
            it has no persona and no access. The <code>handle_new_user</code> trigger
            should have created one on signup.
          </p>
          {profileError && (
            <p className="text-xs text-ink-3 mt-3 font-mono break-words">{profileError}</p>
          )}
          <div className="mt-5">
            <Button variant="secondary" onClick={signOut}>
              Sign out
            </Button>
          </div>
        </Card>
      </div>
    )
  }

  switch (profile.role) {
    case 'senior_manager':
    case 'manager':
    case 'lde_executive':
      return <OpsRoot />
    case 'college':
      return <CollegeRoot />

    // Educators are RECORDS, not users (owner's decision, 2026-08-18). Migration
    // 1800 dropped every trainer policy, so this persona reads zero rows from
    // every table and can write nothing.
    //
    // The case still has to exist. `handle_new_user` does
    // `coalesce(requested_role, 'trainer')`, so a signup whose role metadata is
    // absent or malformed still lands here — that fallback is the deny-by-default
    // sentinel and is deliberate. Rendering the ops console would show a
    // convincing but permanently empty app; rendering nothing would look broken.
    // Say plainly that the account has no access.
    case 'trainer':
      return <NoAccess onSignOut={signOut} />
  }
}

/**
 * The app's one full-screen wait, shared by the auth gate and the root split.
 *
 * It is shaped like the console rather than centred on a spinner. Both waits it
 * covers end in the same picture - a sidebar, a sticky header, a dense table -
 * so drawing that picture first means the real console fades in on top of its
 * own outline instead of replacing an empty screen, and the first thing a
 * reader sees is where things will be rather than that something is pending.
 *
 * The rail is drawn but deliberately EMPTY of links. Which persona is signing
 * in is not known yet at this point, and inventing a nav that is about to be
 * replaced by a different one is a worse lie than an unlabelled rail.
 */
function FullScreenLoading({ label }: { label: string }) {
  return (
    <div className="min-h-dvh flex">
      <div className="glass w-14 md:w-60 shrink-0 border-r border-line flex flex-col" aria-hidden>
        <div className="h-14 flex items-center gap-2.5 px-3 md:px-4 border-b border-line">
          <span className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg bg-accent text-on-accent">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden>
              <path
                d="M5 12.5l4.5 4.5L19 7.5"
                stroke="currentColor"
                strokeWidth="2.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </span>
          <span className="hidden md:block font-semibold tracking-tight text-sm">
            Ops Console
          </span>
        </div>
        <div className="p-2 space-y-1.5 hidden md:block">
          {[0, 1, 2, 3, 4, 5, 6].map((i) => (
            <div key={i} className="skeleton h-8 w-full" />
          ))}
        </div>
      </div>
      <main className="flex-1 min-w-0 flex flex-col">
        <PageSkeleton label={label} />
      </main>
    </div>
  )
}

/**
 * Terminal state for an account with a persona that grants nothing.
 *
 * Deliberately not an error screen: nothing has gone wrong, this account simply
 * is not a user of the platform. It names the likely cause and gives the one
 * action available.
 */
function NoAccess({ onSignOut }: { onSignOut: () => void }) {
  return (
    <div className="min-h-dvh grid place-items-center p-6">
      <Card className="max-w-md p-6 text-center">
        <h1 className="text-base font-semibold text-ink">This account has no access</h1>
        <p className="text-sm text-ink-2 mt-2 leading-relaxed">
          Educators are managed as records by byteXL staff and do not sign in to the
          Ops Console. If you should have access, ask an administrator to set your
          persona on Users &amp; roles.
        </p>
        <div className="mt-5">
          <Button variant="secondary" onClick={onSignOut}>
            Sign out
          </Button>
        </div>
      </Card>
    </div>
  )
}
