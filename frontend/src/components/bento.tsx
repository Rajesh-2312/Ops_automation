import { Link } from 'react-router-dom'
import type { ReactNode } from 'react'

/* --------------------------------------------------------------------------
   Bento primitives — for the home screens ONLY.

   The working screens (Payouts, Attendance, Program Detail, Users & roles) are
   dense and table-shaped: the eye tracks a column down a list of trainers and
   compares values in it. Tiling those would replace a scan with a hunt. So
   nothing in this file is imported by any of them, on purpose.

   What bento buys on a HOME screen is hierarchy. A grid of equal cards is a
   card list wearing a grid's clothes — it says every number matters the same,
   which is the one thing a morning screen must never say. So `span` and `rows`
   below are not layout convenience, they are the editorial decision: the tile
   holding the thing the org is BLOCKED on is 2x2 and the supporting counts are
   1x1, and a reviewer can see which is which from the JSX alone.

   Visual language is borrowed wholesale from ui.tsx's Card — same radius, same
   border token, same shadow — because a home that looked like a different
   product from the screen it links to would be a worse home.
-------------------------------------------------------------------------- */

type Span = 1 | 2 | 3 | 4
type Rows = 1 | 2

/**
 * Four columns on a desktop, two on a tablet, one on a phone.
 *
 * `lg:auto-rows-*` is what makes a 2-row tile actually twice the height rather
 * than merely twice whatever its content happened to need. Below `lg` the rows
 * go back to auto and every tile is full width, so a tall tile does not become
 * a phone-length column of whitespace — bento is a wide-screen idea and it
 * should degrade to a stack, not shrink.
 *
 * `grid-flow-dense` lets a 1x1 backfill a hole a 2x2 left beside it. Reading
 * order can therefore differ from DOM order for sighted users, which is
 * acceptable for a dashboard of independent tiles and would not be for a form.
 */
export function BentoGrid({ children }: { children: ReactNode }) {
  return (
    <div
      className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3
        lg:auto-rows-[8.75rem] lg:grid-flow-dense"
    >
      {children}
    </div>
  )
}

const COL_SPAN: Record<Span, string> = {
  1: 'sm:col-span-1',
  2: 'sm:col-span-2',
  3: 'sm:col-span-2 lg:col-span-3',
  4: 'sm:col-span-2 lg:col-span-4',
}

const ROW_SPAN: Record<Rows, string> = {
  1: '',
  2: 'lg:row-span-2',
}

/** Left edge accent. Reserved for tiles that mean "something is wrong". */
export type TileTone = 'neutral' | 'alert' | 'accent'

const TONE_EDGE: Record<TileTone, string> = {
  neutral: 'border-line',
  alert: 'border-line border-l-2 border-l-bad',
  accent: 'border-line border-l-2 border-l-accent',
}

export function Tile({
  title,
  hint,
  span = 1,
  rows = 1,
  tone = 'neutral',
  count,
  to,
  toLabel = 'Open',
  action,
  children,
}: {
  title: string
  /** One line under the title saying what the tile is counting. */
  hint?: string
  span?: Span
  rows?: Rows
  tone?: TileTone
  /** Shown beside the title. A string so "3 of 5" is as easy as "3". */
  count?: string | number
  /** Where the tile's work gets done. Every tile that shows a problem has one. */
  to?: string
  toLabel?: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section
      className={`flex flex-col overflow-hidden rounded-card border bg-surface
        shadow-card ${TONE_EDGE[tone]} ${COL_SPAN[span]} ${ROW_SPAN[rows]}`}
    >
      <header className="flex items-start justify-between gap-3 px-4 pt-3.5 pb-2 shrink-0">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-ink leading-snug flex items-center gap-2">
            <span className="truncate">{title}</span>
            {count !== undefined && (
              <span className="shrink-0 text-xs font-medium tabular-nums text-ink-3">
                {count}
              </span>
            )}
          </h2>
          {hint && <p className="text-[11px] text-ink-3 mt-0.5 leading-snug">{hint}</p>}
        </div>
        {action ??
          (to && (
            <Link
              to={to}
              className="shrink-0 text-xs font-medium text-accent hover:underline pt-0.5"
            >
              {toLabel} →
            </Link>
          ))}
      </header>

      {/* Tiles have a fixed height on a desktop, so a long list scrolls INSIDE
          the tile. That keeps the grid intact — a dashboard where one busy tile
          pushes the rest off-screen stops being a dashboard. */}
      <div className="flex-1 min-h-0 overflow-y-auto scroll-slim px-4 pb-3.5 max-h-72 lg:max-h-none">
        {children}
      </div>
    </section>
  )
}

/**
 * The empty state, and the reason this file exists at all.
 *
 * Most of these tiles will be empty for months — Phase 1 has one Manager and a
 * demo college in it. "No data" in that situation is not neutral, it reads as
 * broken. So the shape is forced: `whatFills` says what would put something
 * here, and `action` says who does it. There is no variant of this component
 * that renders a bare message.
 */
export function TileEmpty({
  whatFills,
  action,
}: {
  whatFills: string
  action?: ReactNode
}) {
  return (
    <div className="h-full flex flex-col justify-center py-3">
      <p className="text-xs text-ink-3 leading-relaxed">{whatFills}</p>
      {action && <div className="mt-2.5">{action}</div>}
    </div>
  )
}

// Named utilities rather than `text-[var(--color-good)]`. The arbitrary-value
// form worked, but it reads as an escape hatch and invites the next person to
// reach for a raw literal inside the brackets — which is how a tile ends up a
// different green from the pill sitting next to it in the same row.
const METRIC_TONE: Record<'normal' | 'good' | 'warn' | 'bad', string> = {
  normal: 'text-ink',
  good: 'text-good',
  warn: 'text-warn',
  bad: 'text-bad',
}

/**
 * One number, at the size its importance earns.
 *
 * `value` is a string or number and is rendered verbatim. Nothing here parses
 * or combines anything: rupee figures reach this component as the strings the
 * server sent (CLAUDE.md R2/R7) and are formatted by `fmtAmount`, which groups
 * digits without arithmetic.
 */
export function Metric({
  value,
  label,
  caption,
  tone = 'normal',
  size = 'md',
}: {
  value: string | number
  label?: string
  caption?: string
  tone?: 'normal' | 'good' | 'warn' | 'bad'
  size?: 'md' | 'lg'
}) {
  return (
    <div>
      <p
        className={`font-semibold tabular-nums tracking-tight leading-none ${
          size === 'lg' ? 'text-4xl' : 'text-2xl'
        } ${METRIC_TONE[tone]}`}
      >
        {value}
      </p>
      {label && <p className="text-xs text-ink-2 mt-1.5">{label}</p>}
      {caption && <p className="text-[11px] text-ink-3 mt-1 leading-snug">{caption}</p>}
    </div>
  )
}

/** A row inside a tile's list. Optionally a link — a tile row that goes
 *  nowhere is a dead end on the one screen meant to launch the day. */
export function TileRow({
  to,
  primary,
  secondary,
  trailing,
}: {
  to?: string
  primary: ReactNode
  secondary?: ReactNode
  trailing?: ReactNode
}) {
  const body = (
    <>
      <span className="min-w-0 flex-1">
        <span className="block text-sm text-ink leading-snug truncate">{primary}</span>
        {secondary && (
          <span className="block text-[11px] text-ink-3 mt-0.5 truncate">{secondary}</span>
        )}
      </span>
      {trailing && <span className="shrink-0 flex items-center gap-1.5">{trailing}</span>}
    </>
  )

  const shell = 'flex items-center gap-3 py-2 border-b border-line-soft last:border-0'
  return to ? (
    <Link to={to} className={`${shell} -mx-1 px-1 rounded-control hover:bg-surface-2/70 transition`}>
      {body}
    </Link>
  ) : (
    <div className={shell}>{body}</div>
  )
}

/**
 * A proportion bar with named parts — programs across six stages, attendance
 * across five marks.
 *
 * Segments carry an explicit colour rather than picking one from an index,
 * because in both uses the colour MEANS something (a red segment is unmarked
 * days, not "the fourth series"). Zero-width segments are dropped so a stage
 * with no programs does not draw a 1px sliver that reads as one.
 */
export function SegmentBar({
  segments,
  className = '',
}: {
  segments: { key: string; value: number; color: string; label: string }[]
  className?: string
}) {
  const total = segments.reduce((n, s) => n + s.value, 0)
  if (total === 0) return null
  return (
    <div
      className={`flex h-2 w-full overflow-hidden rounded-full bg-surface-2 ${className}`}
      role="img"
      aria-label={segments
        .filter((s) => s.value > 0)
        .map((s) => `${s.label}: ${s.value}`)
        .join(', ')}
    >
      {segments
        .filter((s) => s.value > 0)
        .map((s) => (
          <span
            key={s.key}
            title={`${s.label}: ${s.value}`}
            style={{ width: `${(100 * s.value) / total}%`, background: s.color }}
          />
        ))}
    </div>
  )
}

/** The legend for a SegmentBar. Wraps; zero rows are hidden. */
export function SegmentLegend({
  segments,
}: {
  segments: { key: string; value: number; color: string; label: string }[]
}) {
  return (
    <ul className="flex flex-wrap gap-x-3 gap-y-1 mt-2.5">
      {segments
        .filter((s) => s.value > 0)
        .map((s) => (
          <li key={s.key} className="flex items-center gap-1.5 text-[11px] text-ink-2">
            <span
              className="h-2 w-2 rounded-full shrink-0"
              style={{ background: s.color }}
              aria-hidden
            />
            {s.label}
            <span className="tabular-nums text-ink-3">{s.value}</span>
          </li>
        ))}
    </ul>
  )
}
