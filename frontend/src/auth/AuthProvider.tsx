import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import type { Session } from '@supabase/supabase-js'
import { useQueryClient } from '@tanstack/react-query'
import { supabase } from '../lib/supabase'
import { isInternalRole, roleSeesCommercials, type Profile } from '../lib/types'

interface AuthState {
  session: Session | null
  profile: Profile | null
  loading: boolean
  /** Non-fatal: signed in, but the profile row could not be read. */
  profileError: string | null

  /**
   * Convenience mirrors of the SQL helpers, for LAYOUT DECISIONS ONLY.
   *
   * `isInternal` mirrors `public.is_internal()`, `canSeeCommercials` mirrors
   * `public.can_see_commercials()`, `isAdmin` mirrors `public.is_admin()`.
   * They decide which nav items and panels get drawn. They decide nothing about
   * what data is reachable — that is RLS, in Postgres, per CLAUDE.md R5. Never
   * treat a `true` from one of these as authorisation.
   */
  isInternal: boolean
  canSeeCommercials: boolean
  isAdmin: boolean

  refreshProfile: () => Promise<void>
  signOut: () => Promise<void>
}

const Ctx = createContext<AuthState | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<Session | null>(null)
  const [profile, setProfile] = useState<Profile | null>(null)
  const [loading, setLoading] = useState(true)
  const [profileError, setProfileError] = useState<string | null>(null)
  const queryClient = useQueryClient()

  /**
   * The profile row carries the persona. It is created by the `handle_new_user`
   * trigger on signup and is readable under the `profiles_self_select` policy,
   * so this select returns exactly one row — or none, which is itself
   * meaningful and surfaced rather than swallowed.
   */
  const loadProfile = useCallback(async (userId: string) => {
    const { data, error } = await supabase
      .from('profiles')
      .select('*')
      .eq('id', userId)
      .maybeSingle()

    if (error) {
      setProfile(null)
      setProfileError(error.message)
      return
    }
    setProfile((data as Profile | null) ?? null)
    setProfileError(data ? null : 'No profile row found for this account.')
  }, [])

  useEffect(() => {
    let active = true

    void supabase.auth.getSession().then(async ({ data }) => {
      if (!active) return
      setSession(data.session)
      if (data.session?.user) await loadProfile(data.session.user.id)
      if (active) setLoading(false)
    })

    const { data: sub } = supabase.auth.onAuthStateChange((_event, next) => {
      if (!active) return
      setSession(next)
      if (next?.user) {
        void loadProfile(next.user.id)
      } else {
        setProfile(null)
        setProfileError(null)
        // Every cached query was fetched under the previous user's JWT and
        // scoped by their assignments. Keeping any of it after a sign-out would
        // show one persona another's rows on the next sign-in.
        queryClient.clear()
      }
    })

    return () => {
      active = false
      sub.subscription.unsubscribe()
    }
  }, [loadProfile, queryClient])

  const refreshProfile = useCallback(async () => {
    if (session?.user) await loadProfile(session.user.id)
  }, [session, loadProfile])

  const signOut = useCallback(async () => {
    await supabase.auth.signOut()
  }, [])

  const value = useMemo<AuthState>(
    () => ({
      session,
      profile,
      loading,
      profileError,
      isInternal: isInternalRole(profile?.role),
      canSeeCommercials: roleSeesCommercials(profile?.role),
      isAdmin: profile?.is_admin ?? false,
      refreshProfile,
      signOut,
    }),
    [session, profile, loading, profileError, refreshProfile, signOut],
  )

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useAuth(): AuthState {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>')
  return ctx
}
