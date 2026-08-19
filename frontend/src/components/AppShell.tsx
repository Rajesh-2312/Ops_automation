import { Link, NavLink } from 'react-router-dom'
import type { ReactNode } from 'react'
import { useAuth } from '../auth/AuthProvider'
import { Badge, Button } from './ui'
import { ROLE_SHORT } from '../lib/types'
import { useResolvedTheme, useTheme, type Theme } from '../lib/theme'

/**
 * One link in the sidebar.
 *
 * `group` and `badge` are BOTH optional and both are additive on purpose. Two
 * roots and twenty page files already pass this shape, and a required field
 * here would be a twenty-file edit to render the same links. An array with no
 * groups at all still renders exactly as it did - as a single unheaded list -
 * which is what `CollegeRoot` relies on.
 */
export interface NavItem {
  to: string
  label: string
  icon: ReactNode
  end?: boolean
  /** Section heading this link sits under. Ungrouped links lead the list. */
  group?: string
  /** A pending count. Zero and undefined both render nothing - see `NavBadge`. */
  badge?: number
}

/**
 * Bucket the flat array into sections, in first-appearance order.
 *
 * Collected BY NAME rather than by adjacency, so a group can never be drawn
 * twice: two "Money" headers separated by an unrelated link reads as a
 * rendering bug, and the caller's array order is the wrong place to enforce
 * that. Ungrouped links collect into a single leading, headless bucket.
 */
function groupNav(nav: NavItem[]): { name: string | null; items: NavItem[] }[] {
  const groups: { name: string | null; items: NavItem[] }[] = []
  for (const item of nav) {
    const name = item.group ?? null
    const bucket = groups.find((g) => g.name === name)
    if (bucket) bucket.items.push(item)
    else groups.push({ name, items: [item] })
  }
  return groups
}

export function AppShell({ nav, children }: { nav: NavItem[]; children: ReactNode }) {
  const { profile, session, signOut } = useAuth()
  const role = profile?.role

  return (
    <div className="min-h-dvh flex">
      {/* Collapses to an icon rail below md so the dense tables still get the
          full width on a laptop.

          `.glass` per DESIGN.md 3: the sidebar is chrome, and chrome is where
          glacier belongs. It is also `sticky h-dvh self-start` so it stays put
          while a 200-row payout table scrolls past it - which is what gives the
          frost something to actually frost, and what stops the reader losing
          their place in the nav halfway down a long screen. */}
      <aside className="glass w-14 md:w-60 shrink-0 border-r border-line flex flex-col sticky top-0 h-dvh self-start">
        <div className="h-14 flex items-center gap-2.5 px-3 md:px-4 border-b border-line shrink-0">
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

        {/* Grouped, in the order the work actually flows: what is on me, what
            we sell, what we deliver, what it costs, what helps, who administers
            it. A flat list of fifteen links makes a newcomer read all fifteen
            to find out which two matter to them this morning. */}
        <nav aria-label="Primary" className="flex-1 p-2 overflow-y-auto scroll-slim">
          {groupNav(nav).map((group, i) => (
            <div
              key={group.name ?? '__ungrouped__'}
              role={group.name ? 'group' : undefined}
              aria-label={group.name ?? undefined}
            >
              {group.name && (
                <>
                  {/* At rail width the label is 60px of uppercase 10px type in
                      a 56px column, which is a stub, not a heading. A rule says
                      the same thing - these belong together - in the space
                      there is. The heading survives for screen readers via the
                      group's aria-label either way. */}
                  {i > 0 && <div aria-hidden className="md:hidden mx-2 my-2 h-px bg-line" />}
                  <p
                    className={`hidden md:block px-2.5 pb-1 text-[10px] font-semibold uppercase
                      tracking-wider text-ink-3 ${i > 0 ? 'pt-4' : 'pt-2'}`}
                  >
                    {group.name}
                  </p>
                </>
              )}

              <div className="space-y-0.5">
                {group.items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    end={item.end}
                    className={({ isActive }) =>
                      `relative flex items-center gap-2.5 rounded-lg px-2.5 h-9 text-sm transition
                       justify-center md:justify-start ${
                         isActive
                           ? 'bg-accent-soft text-accent-ink font-medium'
                           : 'text-ink-2 hover:bg-surface-2 hover:text-ink'
                       }`
                    }
                    title={item.label}
                  >
                    {({ isActive }) => (
                      <>
                        {/* Colour is never the only channel (DESIGN.md 5). The
                            2px rail states "you are here" in shape, so the
                            active row survives a reader who cannot separate
                            accent-soft from surface-2. */}
                        {isActive && (
                          <span
                            aria-hidden
                            className="absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full bg-accent"
                          />
                        )}
                        <span className="shrink-0 relative">
                          {item.icon}
                          {/* The rail has no room for the count, and a count
                              nobody can see is worse than a dot that says
                              "there is something here". */}
                          {hasBadge(item) && (
                            <span
                              aria-hidden
                              className="md:hidden absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-accent"
                            />
                          )}
                        </span>
                        <span className="hidden md:block truncate">{item.label}</span>
                        {/* Always in the accessibility tree: `hidden` at rail
                            width removes the label from it entirely, and a nav
                            of fifteen unlabelled links is unusable by screen
                            reader. */}
                        <span className="sr-only md:hidden">{item.label}</span>
                        <NavBadge count={item.badge} />
                      </>
                    )}
                  </NavLink>
                ))}
              </div>
            </div>
          ))}
        </nav>

        <div className="border-t border-line p-2 md:p-3 shrink-0 space-y-2.5">
          <div className="hidden md:block">
            <div className="flex items-center gap-2 mb-2">
              {role && <Badge tone="accent">{ROLE_SHORT[role]}</Badge>}
              {profile?.is_admin && <Badge>Admin</Badge>}
            </div>
            <p className="text-xs font-medium text-ink truncate">
              {profile?.full_name || session?.user.email}
            </p>
            <p className="text-[11px] text-ink-3 truncate">{session?.user.email}</p>
          </div>

          <ThemeToggle />

          <div className="hidden md:block">
            <Button size="sm" variant="secondary" className="w-full" onClick={signOut}>
              Sign out
            </Button>
          </div>
          <div className="md:hidden">
            <Button
              size="sm"
              variant="ghost"
              className="w-full"
              onClick={signOut}
              title="Sign out"
              aria-label="Sign out"
            >
              <svg {...ico} aria-hidden>
                <path d="M15 8V6a1 1 0 00-1-1H6a1 1 0 00-1 1v12a1 1 0 001 1h8a1 1 0 001-1v-2" {...stroke} />
                <path d="M19 12H9m10 0l-3-3m3 3l-3 3" {...stroke} />
              </svg>
            </Button>
          </div>
        </div>
      </aside>

      <main className="flex-1 min-w-0 flex flex-col">{children}</main>
    </div>
  )
}

/**
 * Sticky page header used by every route, so headings never shift position.
 *
 * `subtitle` and `purpose` are not two names for the same line.
 *
 *   subtitle  a tight qualifier on the title - a period, a scope, a count. It
 *             sits on one line and TRUNCATES, because it is trailing detail.
 *   purpose   one plain-English sentence saying what the screen is FOR, aimed
 *             at somebody who has never opened it (DESIGN.md 4.1). It wraps to
 *             `max-w-prose` and is never cut, because a purpose truncated at
 *             the viewport edge is a purpose nobody reads.
 *
 * Both may appear together. `purpose` is `ink-2` rather than `ink-3` on
 * purpose: ink-3 lands near 2.7:1 on canvas, which is fine for a timestamp
 * you glance at and not for a sentence somebody is meant to read (DESIGN.md 5).
 */
export function PageHeader({
  title,
  subtitle,
  purpose,
  actions,
}: {
  title: string
  subtitle?: string
  purpose?: string
  actions?: ReactNode
}) {
  return (
    // `.glass` replaces the old bg-canvas/85 + backdrop-blur pair: same idea,
    // but with the opaque fallback the utility declares for browsers without
    // backdrop-filter, where a translucent bar over a dense table is unreadable.
    // z-20 stays - the only thing above it in the app is the modal at z-50.
    <header className="glass sticky top-0 z-20 border-b border-line px-5 md:px-7 py-3.5">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-base font-semibold tracking-tight text-ink truncate">{title}</h1>
          {subtitle && <p className="text-xs text-ink-2 mt-0.5 truncate">{subtitle}</p>}
          {purpose && (
            <p className="text-xs text-ink-2 mt-1 max-w-prose leading-relaxed">{purpose}</p>
          )}
        </div>
        {actions && <div className="flex items-center gap-2 shrink-0">{actions}</div>}
      </div>
    </header>
  )
}

/**
 * The content column under a `PageHeader`.
 *
 * FULL BLEED IS THE DEFAULT AND STAYS THE DEFAULT. Most screens here are dense
 * tables - attendance grids, payout runs, deployment lists - and a measure cap
 * on those buys a tidy line length by pushing columns off the right edge.
 * `maxWidth` exists for the screens that are prose or a single form, where a
 * 2000px line genuinely is harder to read.
 */
const PAGE_WIDTHS = {
  full: '',
  wide: 'max-w-7xl mx-auto',
  content: 'max-w-5xl mx-auto',
  prose: 'max-w-3xl mx-auto',
} as const

export function Page({
  children,
  maxWidth = 'full',
}: {
  children: ReactNode
  maxWidth?: keyof typeof PAGE_WIDTHS
}) {
  return (
    <div className={`flex-1 px-5 md:px-7 py-6 min-w-0 w-full ${PAGE_WIDTHS[maxWidth]}`}>
      {children}
    </div>
  )
}

// --- Icons (inline so there is no icon-font dependency) ---------------------

const ico = { width: 17, height: 17, viewBox: '0 0 24 24', fill: 'none' } as const
const stroke = {
  stroke: 'currentColor',
  strokeWidth: 1.8,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
}

export const Icons = {
  home: (
    <svg {...ico} aria-hidden>
      <path d="M3.5 10.5L12 4l8.5 6.5" {...stroke} />
      <path d="M5.5 9.5V19a1 1 0 001 1h11a1 1 0 001-1V9.5" {...stroke} />
      <path d="M10 20v-5.5h4V20" {...stroke} />
    </svg>
  ),
  board: (
    <svg {...ico} aria-hidden>
      <rect x="3" y="4" width="5" height="16" rx="1.5" {...stroke} />
      <rect x="10" y="4" width="5" height="10" rx="1.5" {...stroke} />
      <rect x="17" y="4" width="4" height="14" rx="1.5" {...stroke} />
    </svg>
  ),
  building: (
    <svg {...ico} aria-hidden>
      <path d="M4 21V6a1 1 0 011-1h8a1 1 0 011 1v15" {...stroke} />
      <path d="M14 21V10h5a1 1 0 011 1v10" {...stroke} />
      <path d="M7 9h3M7 13h3M7 17h3M17 14h1M17 17h1M2 21h20" {...stroke} />
    </svg>
  ),
  check: (
    <svg {...ico} aria-hidden>
      <path d="M9 11l2.5 2.5L16 8" {...stroke} />
      <rect x="3.5" y="3.5" width="17" height="17" rx="3.5" {...stroke} />
    </svg>
  ),
  users: (
    <svg {...ico} aria-hidden>
      <circle cx="9" cy="8" r="3.2" {...stroke} />
      <path d="M3.5 20a5.5 5.5 0 0111 0" {...stroke} />
      <path d="M16 5.5a3 3 0 010 5.6M17.5 20a5.5 5.5 0 00-2-4.3" {...stroke} />
    </svg>
  ),
  chart: (
    <svg {...ico} aria-hidden>
      <path d="M4 20V10M10 20V4M16 20v-7M22 20H2" {...stroke} />
    </svg>
  ),
  doc: (
    <svg {...ico} aria-hidden>
      <path d="M14 3v5h5" {...stroke} />
      <path d="M19 8.5V20a1 1 0 01-1 1H6a1 1 0 01-1-1V4a1 1 0 011-1h7.5L19 8.5z" {...stroke} />
      <path d="M8.5 13h7M8.5 16.5h5" {...stroke} />
    </svg>
  ),
  calendar: (
    <svg {...ico} aria-hidden>
      <rect x="3.5" y="5" width="17" height="15.5" rx="2.5" {...stroke} />
      <path d="M3.5 10h17M8 3v4M16 3v4" {...stroke} />
    </svg>
  ),
  badge: (
    <svg {...ico} aria-hidden>
      <rect x="3.5" y="6" width="17" height="13.5" rx="2.5" {...stroke} />
      <path d="M9 6V4.5a1 1 0 011-1h4a1 1 0 011 1V6" {...stroke} />
      <path d="M7.5 12.5h9M7.5 16h5" {...stroke} />
    </svg>
  ),
  rupee: (
    <svg {...ico} aria-hidden>
      <path d="M7 4h10M7 8.5h10M7 4c5.5 0 5.5 8 0 8h2l7 8" {...stroke} />
    </svg>
  ),
  // A speech bubble with a spark — the Copilot answers in prose, and the spark
  // marks it as the one generated surface in the console.
  chat: (
    <svg {...ico} aria-hidden>
      <path d="M20.5 12.5a7.5 7.5 0 01-10.9 6.7L4.5 20.5l1.3-5.1A7.5 7.5 0 1120.5 12.5z" {...stroke} />
      <path d="M12 8.5l.9 2.1 2.1.9-2.1.9-.9 2.1-.9-2.1-2.1-.9 2.1-.9.9-2.1z" {...stroke} />
    </svg>
  ),
  // A bell. Alerts are internal-only and never leave the building (§8), so this
  // is deliberately not a megaphone or a send glyph.
  bell: (
    <svg {...ico} aria-hidden>
      <path d="M18 16V11a6 6 0 10-12 0v5l-1.5 2.5h15L18 16z" {...stroke} />
      <path d="M10 20a2 2 0 004 0" {...stroke} />
    </svg>
  ),
  // A sealed envelope, closed rather than open-and-flying: the comms queue
  // drafts and holds. Nothing on that screen transmits (R3/R4).
  mail: (
    <svg {...ico} aria-hidden>
      <rect x="3" y="5.5" width="18" height="13" rx="2.5" {...stroke} />
      <path d="M3.5 7.5l8.5 6 8.5-6" {...stroke} />
    </svg>
  ),
  // Two arrows round a gap — ERM is a MANUAL round trip (§10): the system
  // generates a pack, a human pastes it, a human confirms. The gap is the point;
  // there is no API on the other side.
  sync: (
    <svg {...ico} aria-hidden>
      <path d="M20 11.5A8 8 0 006.3 6.3L4 8.5" {...stroke} />
      <path d="M4 12.5a8 8 0 0013.7 5.2L20 15.5" {...stroke} />
      <path d="M4 4.5v4h4M20 19.5v-4h-4" {...stroke} />
    </svg>
  ),
}
// --- Nav pieces -------------------------------------------------------------

/** Zero and undefined are the same answer here: nothing to show. */
function hasBadge(item: NavItem): boolean {
  return typeof item.badge === 'number' && item.badge > 0
}

/**
 * A pending count on a nav link.
 *
 * Filled `accent` rather than `accent-soft`, because the active row is already
 * `accent-soft` and a badge that disappears on the screen you are standing on
 * is a badge that teaches the reader to stop looking for it. Hidden at rail
 * width, where the dot on the icon carries the same signal in the space there
 * is. `aria-label` spells the number out, since a bare "3" beside a link name
 * is read as part of the label.
 */
function NavBadge({ count }: { count?: number }) {
  if (typeof count !== 'number' || count <= 0) return null
  return (
    <span
      aria-label={`${count} pending`}
      className="hidden md:inline-flex ml-auto shrink-0 items-center justify-center
        h-[18px] min-w-[18px] px-1 rounded-full bg-accent text-on-accent
        text-[10px] font-semibold tabular-nums"
    >
      {count > 99 ? '99+' : count}
    </span>
  )
}

// --- Theme ------------------------------------------------------------------

/**
 * Three-way theme control: Light / System / Dark.
 *
 * THREE, not a two-state switch, because the underlying model has three states
 * (see lib/theme.ts). A binary toggle has to pick a side the first time it is
 * touched, which permanently opts the reader OUT of following their device -
 * and there is no way back to "follow the device" once the switch only has two
 * positions. So "System" is a first-class choice and it is the default.
 *
 * Radio semantics rather than three buttons: exactly one is selected, and the
 * arrow keys behave the way a reader who navigates by keyboard expects. Every
 * control here is icon-only, so every control carries an `aria-label`.
 *
 * The caption exists because "System" alone is not an answer to "what am I
 * looking at". It reads the live OS preference, so flipping the laptop to dark
 * with the app open updates it (that is what the matchMedia subscription in
 * lib/theme.ts is for).
 */
const THEME_OPTIONS: { value: Theme; label: string; icon: ReactNode }[] = [
  {
    value: 'light',
    label: 'Light',
    icon: (
      <svg {...ico} width={15} height={15} aria-hidden>
        <circle cx="12" cy="12" r="4" {...stroke} />
        <path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4" {...stroke} />
      </svg>
    ),
  },
  {
    value: 'system',
    label: 'System',
    icon: (
      <svg {...ico} width={15} height={15} aria-hidden>
        <rect x="3" y="4.5" width="18" height="12" rx="2" {...stroke} />
        <path d="M9 20h6M12 16.5V20" {...stroke} />
      </svg>
    ),
  },
  {
    value: 'dark',
    label: 'Dark',
    icon: (
      <svg {...ico} width={15} height={15} aria-hidden>
        <path d="M20 14.5A8.5 8.5 0 019.5 4a8.5 8.5 0 1010.5 10.5z" {...stroke} />
      </svg>
    ),
  },
]

function ThemeToggle() {
  const [theme, setTheme] = useTheme()
  const resolved = useResolvedTheme()

  return (
    <div>
      <div
        role="radiogroup"
        aria-label="Colour theme"
        // Stacks vertically on the icon rail, where three 28px controls do not
        // fit across 56px but do fit down it.
        className="flex flex-col md:flex-row gap-0.5 rounded-control border border-line bg-surface-2 p-0.5"
      >
        {THEME_OPTIONS.map((opt) => {
          const selected = theme === opt.value
          return (
            <button
              key={opt.value}
              type="button"
              role="radio"
              aria-checked={selected}
              aria-label={opt.label}
              title={opt.label}
              onClick={() => setTheme(opt.value)}
              className={`flex-1 inline-flex items-center justify-center h-7 rounded-[0.35rem]
                transition ${
                  selected
                    ? 'bg-surface text-accent-ink shadow-card'
                    : 'text-ink-3 hover:text-ink'
                }`}
            >
              {opt.icon}
            </button>
          )
        })}
      </div>
      {theme === 'system' && (
        <p className="hidden md:block text-[10px] text-ink-3 mt-1.5 text-center">
          Following this device · {resolved}
        </p>
      )}
    </div>
  )
}

// --- Terminal route states --------------------------------------------------

/**
 * The catch-all route, in the shell rather than in either root, because both
 * roots need the same one and a second copy is a second thing to keep true.
 *
 * It replaces a silent `<Navigate to="/" />`. A redirect and a working link are
 * indistinguishable to somebody who followed a stale bookmark or mistyped a
 * program id: they land on Home having been told nothing, and reasonably
 * conclude the record was deleted. Saying "this URL does not exist here" and
 * handing back the path is the difference between a bug report and a typo.
 */
export function NotFound() {
  return (
    <>
      <PageHeader
        title="Page not found"
        purpose="This address does not match any screen in the console. It is usually a stale link or a mistyped id, not missing data."
      />
      <Page maxWidth="prose">
        <div className="rounded-card border border-line bg-surface p-6">
          <p className="text-sm text-ink-2 leading-relaxed">
            Nothing is registered at{' '}
            <code className="font-mono text-xs text-ink bg-surface-2 rounded px-1.5 py-0.5">
              {typeof window === 'undefined' ? '' : window.location.pathname}
            </code>
            .
          </p>
          <p className="text-sm text-ink-2 leading-relaxed mt-3">
            If you expected a record here, it may exist but be outside your reach — an
            LDE Executive sees their own campus, a Manager their assigned colleges. That
            boundary is enforced in Postgres, so an out-of-reach row is indistinguishable
            from one that was never there.
          </p>
          <Link
            to="/"
            className="inline-flex items-center gap-1.5 mt-5 h-9 px-3.5 rounded-control
              bg-accent text-on-accent text-sm font-medium hover:bg-accent-hover transition"
          >
            Back to home
          </Link>
        </div>
      </Page>
    </>
  )
}

/**
 * A wait shaped like the screen that is arriving, for route-chunk and boot
 * loads. A centred spinner on an otherwise blank page says "something is
 * happening somewhere"; a header bar with rows under it says "the screen you
 * asked for is coming, and this is roughly what it will look like", so nothing
 * jumps when the real one lands.
 *
 * `role="status"` because it appears and disappears without the reader doing
 * anything (DESIGN.md 5), and the label is the only part a screen reader gets -
 * the bars themselves are decoration.
 */
export function PageSkeleton({ label = 'Loading' }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="animate-in">
      <span className="sr-only">{label}</span>
      <div className="glass sticky top-0 z-20 border-b border-line px-5 md:px-7 py-3.5">
        <div className="skeleton h-5 w-48" />
        <div className="skeleton h-3 w-72 mt-2" />
      </div>
      <div className="flex-1 px-5 md:px-7 py-6 min-w-0 space-y-3" aria-hidden>
        <div className="skeleton h-9 w-full max-w-md" />
        <div className="rounded-card border border-line bg-surface p-4 space-y-3">
          {[0, 1, 2, 3, 4, 5].map((i) => (
            <div key={i} className="flex items-center gap-4">
              <div className="skeleton h-4 flex-1" />
              <div className="skeleton h-4 w-24 shrink-0" />
              <div className="skeleton h-4 w-16 shrink-0" />
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
