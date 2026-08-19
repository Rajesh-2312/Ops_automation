import { useCallback, useSyncExternalStore } from 'react'

/* ===========================================================================
   Theme - three states, one source of truth.
   ===========================================================================

   The theme is NOT React state. It is an attribute on <html> that index.html
   stamps in a blocking inline script before first paint, because a theme
   applied after hydration gives every dark-mode user a white flash on every
   reload - and on this app that flash lands on a screen full of payout
   figures, which reads as a failed load rather than a style blink.

   So this module is a deliberate mirror of that script: same key, same two
   stored values, same stamping rules. If the two ever disagree the flash comes
   back, so the contract is written down rather than inferred:

     localStorage['bytexl-theme'] === 'dark'   -> data-theme="dark",  colorScheme dark
     localStorage['bytexl-theme'] === 'light'  -> data-theme="light", colorScheme light
     anything else (absent, corrupt, 'system') -> NO attribute, NO colorScheme

   THAT THIRD ROW IS THE POINT. 'system' is the ABSENCE of a stamp, never the
   string "system" written into it. index.css defines its dark ramp twice - once
   under `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])`
   and once under `:root[data-theme="dark"]` - and the media branch only wins
   while no attribute is present. Stamping data-theme="system" would match
   neither selector and leave the app pinned to light for an OS-dark reader.
   =========================================================================== */

export type Theme = 'light' | 'dark' | 'system'

/** Shared verbatim with the inline script in index.html. Changing one is a bug. */
const STORAGE_KEY = 'bytexl-theme'
const DARK_QUERY = '(prefers-color-scheme: dark)'

/* The stored choice is cached in the module rather than re-read per render.
   `useSyncExternalStore` calls its snapshot function on every render and again
   on every notification, and a synchronous localStorage read does not belong on
   that path - least of all in a blocked-storage context, where every call is a
   thrown-and-caught exception. */
let cached: Theme | undefined
const listeners = new Set<() => void>()

function readStored(): Theme {
  try {
    const raw = localStorage.getItem(STORAGE_KEY)
    return raw === 'dark' || raw === 'light' ? raw : 'system'
  } catch {
    // localStorage THROWS - it does not return null - when cookies are blocked
    // or the browser is in a locked-down private mode. A theme preference is
    // not worth taking the app down for; fall through to the OS preference.
    return 'system'
  }
}

function darkQuery(): MediaQueryList | null {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return null
  return window.matchMedia(DARK_QUERY)
}

/** The reader's stored choice. `'system'` when they have not made one. */
export function getTheme(): Theme {
  if (cached === undefined) cached = readStored()
  return cached
}

/**
 * What is actually on screen - the stored choice, or the OS preference resolved
 * through it when that choice is `'system'`.
 *
 * The distinction is what makes the toggle honest: a control reading "System"
 * and nothing else reports what it was told, not what the reader is looking at.
 */
export function getResolvedTheme(): 'light' | 'dark' {
  const chosen = getTheme()
  if (chosen !== 'system') return chosen
  return darkQuery()?.matches ? 'dark' : 'light'
}

/** Apply a choice to <html>. The only place either DOM property is written. */
function stamp(theme: Theme): void {
  if (typeof document === 'undefined') return
  const html = document.documentElement
  if (theme === 'system') {
    html.removeAttribute('data-theme')
    // Cleared rather than set to 'light dark': index.css already declares that
    // on the base rule, and an inline style would shadow it permanently.
    html.style.colorScheme = ''
  } else {
    html.setAttribute('data-theme', theme)
    // Set alongside the attribute so native chrome - scrollbars, form controls,
    // the caret - follows the app rather than the OS.
    html.style.colorScheme = theme
  }
}

export function setTheme(theme: Theme): void {
  cached = theme
  try {
    // 'system' REMOVES the key rather than storing the word. Storing it would
    // make the inline script's "anything else" branch depend on a value it does
    // not know about, and the next reader would reasonably assume the stamp
    // mirrors the storage.
    if (theme === 'system') localStorage.removeItem(STORAGE_KEY)
    else localStorage.setItem(STORAGE_KEY, theme)
  } catch {
    // Blocked storage: the choice still applies to this tab, it just will not
    // survive a reload. Quietly losing the preference is a smaller failure than
    // refusing to change the theme at all.
  }
  stamp(theme)
  for (const notify of listeners) notify()
}

/**
 * Store subscription for `useSyncExternalStore`.
 *
 * Three things move the answer and all three are wired here:
 *   - this tab calling `setTheme`
 *   - the OS flipping while the app is open. That only changes the RESOLVED
 *     theme, and only while the choice is 'system' - but a control claiming to
 *     follow the device has to keep telling the truth about what it is following.
 *   - another tab writing the key, which the browser has already applied to that
 *     document and not to ours
 */
function subscribe(onChange: () => void): () => void {
  listeners.add(onChange)

  const mql = darkQuery()
  mql?.addEventListener('change', onChange)

  const onStorage = (e: StorageEvent) => {
    // `key === null` is a storage.clear(), which takes our key with it.
    if (e.key !== null && e.key !== STORAGE_KEY) return
    cached = readStored()
    stamp(cached)
    onChange()
  }
  window.addEventListener('storage', onStorage)

  return () => {
    listeners.delete(onChange)
    mql?.removeEventListener('change', onChange)
    window.removeEventListener('storage', onStorage)
  }
}

/** `[stored choice, setter]`. Re-renders when any of the three sources moves. */
export function useTheme(): [Theme, (t: Theme) => void] {
  const theme = useSyncExternalStore(subscribe, getTheme, getTheme)
  const set = useCallback((t: Theme) => setTheme(t), [])
  return [theme, set]
}

/** What the reader is actually looking at, tracked live. See `getResolvedTheme`. */
export function useResolvedTheme(): 'light' | 'dark' {
  return useSyncExternalStore(subscribe, getResolvedTheme, getResolvedTheme)
}
