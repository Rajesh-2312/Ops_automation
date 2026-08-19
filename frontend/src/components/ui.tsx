import {
  useEffect,
  useId,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type InputHTMLAttributes,
  type ReactNode,
  type KeyboardEvent as ReactKeyboardEvent,
  type SelectHTMLAttributes,
  type TextareaHTMLAttributes,
} from 'react'
import type {
  AttendanceMark,
  DocStatus,
  TaskCadence,
  TaskStatus,
  TaskUrgency,
} from '../lib/types'
import {
  ATTENDANCE_MARK_SHORT,
  CADENCE_LABEL,
  DOC_STATUS_LABEL,
  TASK_STATUS_LABEL,
  URGENCY_LABEL,
} from '../lib/types'

/* --------------------------------------------------------------------------
   A small, deliberately boring UI kit. Every screen composes from these so the
   app reads as one system rather than a pile of one-off markup.

   No component library on purpose: the whole kit is 500 lines of markup we can
   read, and swapping it for a dependency would trade that for a theme system
   we would spend longer fighting than writing.
-------------------------------------------------------------------------- */

/* --------------------------------------------------------------------------
   THE TONE VOCABULARY.

   Four meanings and one quiet default, each a background/text/border TRIPLE
   resolved in `index.css` and therefore already correct in both themes. Reach
   for one of these before writing a colour anywhere in this file.

   This map is the reason no dark-mode variant survives anywhere in the kit.
   Every pill used to name a raw Tailwind literal plus a hand-written dark-mode
   override of its text colour — three guesses per pill, made independently,
   which is exactly how the pages underneath accumulated 120 mismatched greens.
   A `-wash`/`-ink` token already flips with the theme, so a dark-mode override
   layered on top of one is not belt-and-braces; it is a second opinion that
   will eventually disagree with the first.

   `quiet` is not a fifth meaning. It is the ABSENCE of one, and most states in
   this app deserve it: if everything is coloured, colour has stopped saying
   anything (DESIGN.md §4.6).
-------------------------------------------------------------------------- */

export type Tone = 'quiet' | 'good' | 'warn' | 'bad' | 'info'

export const TONE_CLASS: Record<Tone, string> = {
  quiet: 'bg-surface-2 text-ink-2 border-line',
  good: 'bg-good-wash text-good-ink border-good/30',
  warn: 'bg-warn-wash text-warn-ink border-warn/30',
  bad: 'bg-bad-wash text-bad-ink border-bad/30',
  info: 'bg-info-wash text-info-ink border-info/30',
}

// --- Button -----------------------------------------------------------------

type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger'

/**
 * `danger` is deliberately the only filled-red control in the kit, and it is
 * filled rather than outlined so it cannot be mistaken for `secondary` at a
 * glance — DESIGN.md §4.7, destructive actions must not look like ordinary
 * ones. Approval and release are separate actions with separate audit rows
 * (CLAUDE.md R4); if you ever find yourself putting both behind one of these,
 * the button is not the problem.
 */
const BUTTON_STYLES: Record<ButtonVariant, string> = {
  primary:
    'bg-accent text-on-accent hover:bg-accent-hover active:bg-accent-hover shadow-card disabled:opacity-45',
  secondary:
    'bg-surface text-ink border border-line hover:bg-surface-2 disabled:opacity-45',
  ghost: 'text-ink-2 hover:bg-surface-2 hover:text-ink disabled:opacity-45',
  danger:
    'bg-bad text-on-accent hover:bg-bad/90 active:bg-bad/80 shadow-card disabled:opacity-45',
}

export function Button({
  variant = 'secondary',
  size = 'md',
  className = '',
  children,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: ButtonVariant
  size?: 'sm' | 'md'
}) {
  const sizing = size === 'sm' ? 'h-8 px-2.5 text-xs gap-1.5' : 'h-9 px-3.5 text-sm gap-2'
  return (
    <button
      className={`inline-flex items-center justify-center rounded-control font-medium transition
        disabled:cursor-not-allowed ${sizing} ${BUTTON_STYLES[variant]} ${className}`}
      {...rest}
    >
      {children}
    </button>
  )
}

// --- Surfaces ---------------------------------------------------------------

export function Card({
  className = '',
  children,
}: {
  className?: string
  children: ReactNode
}) {
  return (
    <div
      className={`rounded-card border border-line bg-surface shadow-card ${className}`}
    >
      {children}
    </div>
  )
}

export function SectionTitle({
  title,
  subtitle,
  action,
}: {
  title: string
  subtitle?: string
  action?: ReactNode
}) {
  return (
    <div className="flex items-end justify-between gap-4 mb-4">
      <div className="min-w-0">
        <h2 className="text-lg font-semibold tracking-tight text-ink">{title}</h2>
        {subtitle && <p className="text-sm text-ink-2 mt-0.5">{subtitle}</p>}
      </div>
      {action}
    </div>
  )
}

// --- Table primitives -------------------------------------------------------
// Three screens draw the same dense table. Defining the cells once keeps the
// padding and the muted-header treatment identical across all of them.

export function Th({ children, className = '' }: { children?: ReactNode; className?: string }) {
  return (
    <th
      className={`px-4 py-2.5 text-xs font-medium uppercase tracking-wide text-ink-3 text-left ${className}`}
    >
      {children}
    </th>
  )
}

export function Td({ children, className = '' }: { children?: ReactNode; className?: string }) {
  return <td className={`px-4 py-3 align-top ${className}`}>{children}</td>
}

// --- Form controls ----------------------------------------------------------

export function Field({
  label,
  hint,
  children,
}: {
  label: string
  hint?: string
  children: ReactNode
}) {
  return (
    <label className="block">
      <span className="block text-xs font-medium text-ink-2 mb-1.5">{label}</span>
      {children}
      {hint && <span className="block text-xs text-ink-3 mt-1">{hint}</span>}
    </label>
  )
}

const CONTROL =
  'w-full h-9 rounded-control border border-line px-3 text-sm text-ink transition ' +
  'focus:border-accent focus:outline-none disabled:opacity-50'

export function Input(props: InputHTMLAttributes<HTMLInputElement>) {
  const { className = '', ...rest } = props
  return <input className={`${CONTROL} ${className}`} {...rest} />
}

export function Select(props: SelectHTMLAttributes<HTMLSelectElement>) {
  const { className = '', ...rest } = props
  return <select className={`${CONTROL} pr-8 ${className}`} {...rest} />
}

export function Textarea(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const { className = '', ...rest } = props
  return (
    <textarea
      className={`w-full rounded-control border border-line px-3 py-2 text-sm text-ink
        transition focus:border-accent focus:outline-none ${className}`}
      {...rest}
    />
  )
}

// --- Status pills -----------------------------------------------------------

const TASK_TONE: Record<TaskStatus, string> = {
  pending: TONE_CLASS.quiet,
  in_progress: TONE_CLASS.warn,
  done: TONE_CLASS.good,
  blocked: TONE_CLASS.bad,
}

const PILL =
  'inline-flex items-center rounded-control border px-1.5 py-0.5 text-[11px] font-medium whitespace-nowrap'

export function TaskStatusPill({ status }: { status: TaskStatus }) {
  return <span className={`${PILL} ${TASK_TONE[status]}`}>{TASK_STATUS_LABEL[status]}</span>
}

// `sent` is `info` rather than `good` on purpose: a document leaving the
// building is a machine event, not an outcome. Only `signed` is green, because
// only `signed` is the thing anyone downstream is actually waiting for.
const DOC_TONE: Record<DocStatus, string> = {
  not_started: TONE_CLASS.quiet,
  in_progress: TONE_CLASS.warn,
  sent: TONE_CLASS.info,
  signed: TONE_CLASS.good,
  expired: TONE_CLASS.bad,
}

export function DocStatusPill({ status }: { status: DocStatus }) {
  return <span className={`${PILL} ${DOC_TONE[status]}`}>{DOC_STATUS_LABEL[status]}</span>
}

// Cadence describes the SHAPE of work — how often it recurs — not its priority,
// so every cadence is quiet. It used to reach for violet, sky and teal, which
// bought a reader nothing (nobody memorises "teal means monthly") and cost them
// something real: three more colours competing with the urgency pills below,
// which are the ones that decide what to do next. Cadence is already spelled
// out in words beside the chip; the colour was decoration paid for out of the
// urgency signal's budget.
const CADENCE_TONE: Record<TaskCadence, string> = {
  one_time: TONE_CLASS.quiet,
  daily: TONE_CLASS.quiet,
  weekly: TONE_CLASS.quiet,
  monthly: TONE_CLASS.quiet,
  quarterly: TONE_CLASS.quiet,
}

export function CadencePill({ cadence }: { cadence: TaskCadence }) {
  if (cadence === 'one_time') return null // the default; showing it is noise
  return <span className={`${PILL} ${CADENCE_TONE[cadence]}`}>{CADENCE_LABEL[cadence]}</span>
}

/**
 * Urgency IS priority, so this is the one place loud colour is earned.
 *
 * There are four tones and six urgencies, so two pairs share a colour and are
 * separated by BORDER WEIGHT instead: `overdue` and `due_today` are the
 * heavier-bordered members of `bad` and `warn`, the merely-`blocked` and
 * merely-`due_soon` the lighter. Inventing a fifth hue (the old orange) to sit
 * between red and amber is how a palette starts meaning "roughly bad" instead
 * of one specific thing, and every reader has to relearn it. Weight is a
 * secondary channel that costs nothing — and per DESIGN.md §5 the label is
 * carrying the real distinction either way, since colour is never the only
 * channel here.
 */
const URGENCY_TONE: Record<TaskUrgency, string> = {
  overdue: 'bg-bad-wash text-bad-ink border-bad/60',
  due_today: 'bg-warn-wash text-warn-ink border-warn/60',
  due_soon: TONE_CLASS.warn,
  scheduled: TONE_CLASS.quiet,
  unscheduled: TONE_CLASS.quiet,
  blocked: TONE_CLASS.bad,
  done: TONE_CLASS.good,
}

export function UrgencyPill({ urgency }: { urgency: TaskUrgency }) {
  return <span className={`${PILL} ${URGENCY_TONE[urgency]}`}>{URGENCY_LABEL[urgency]}</span>
}

/**
 * Trainer attendance mark.
 *
 * Colour here is doing real work: these five marks are the payout engine's
 * input, and 'UNMARKED' is the dangerous one — it silently pays a bCAP trainer
 * and silently underpays a CRT trainer (CLAUDE.md §5). So it is drawn as an
 * absence (dashed, muted) rather than as a neutral state, and never as a tick.
 */
const MARK_TONE: Record<AttendanceMark, string> = {
  P: TONE_CLASS.good,
  A: TONE_CLASS.bad,
  H: TONE_CLASS.warn,
  HOLIDAY: TONE_CLASS.info,
  // Not `quiet`. A quiet chip is a filled chip, and a filled chip reads as a
  // recorded decision. This one has to read as a HOLE: no wash at all, dashed
  // border, tertiary ink. It is the only mark in the app drawn as an absence,
  // and that is the entire point of it.
  UNMARKED: 'bg-transparent text-ink-3 border-line border-dashed',
}

export function AttendanceMarkPill({ mark }: { mark: AttendanceMark }) {
  return <span className={`${PILL} ${MARK_TONE[mark]}`}>{ATTENDANCE_MARK_SHORT[mark]}</span>
}

/** The same palette as a full-size day cell in the attendance grid. */
export function attendanceCellClass(mark: AttendanceMark, selected: boolean): string {
  const base =
    'h-9 rounded-control border text-xs font-medium tabular-nums transition disabled:opacity-40 ' +
    'disabled:cursor-not-allowed'
  const ring = selected ? 'ring-2 ring-accent ring-offset-1 ring-offset-[var(--color-canvas)]' : ''
  return `${base} ${MARK_TONE[mark]} ${ring}`
}

/** Filter chip with a count. Zero-count chips render disabled rather than
 *  vanishing, so the row does not reflow as you tick things off. */
export function FilterChip({
  label,
  count,
  active,
  tone = 'neutral',
  onClick,
}: {
  label: string
  count: number
  active: boolean
  tone?: 'neutral' | 'alert'
  onClick: () => void
}) {
  const base =
    'inline-flex items-center gap-1.5 rounded-control border px-2.5 h-8 text-xs font-medium transition'
  const state = active
    ? tone === 'alert'
      ? 'bg-bad-wash border-bad/40 text-bad-ink'
      : 'bg-accent-soft border-accent/40 text-accent-ink'
    : count === 0
      ? 'bg-surface border-line text-ink-3 opacity-50'
      : 'bg-surface border-line text-ink-2 hover:border-accent/40 hover:text-ink'

  return (
    <button
      type="button"
      onClick={onClick}
      disabled={count === 0 && !active}
      className={`${base} ${state}`}
    >
      {tone === 'alert' && count > 0 && <span aria-hidden>⚠</span>}
      {label}
      <span className="tabular-nums opacity-70">{count}</span>
    </button>
  )
}

/**
 * A free-form pill for anything the typed status pills above do not cover.
 *
 * The tone list grew rather than changed: `neutral | accent | warn` are the
 * three the pages already pass, and `good | bad | info | flame` were added so a
 * screen no longer has to drop to raw markup the moment it wants a green one.
 *
 * `flame` is not a fifth severity. It is RESERVED for generated or AI-drafted
 * content (DESIGN.md §2, CLAUDE.md R1) — a reader must always be able to tell a
 * drafted artefact from a queried fact, and that only works while flame means
 * exactly one thing. Do not reach for it because a badge needs to stand out.
 */
export function Badge({
  children,
  tone = 'neutral',
}: {
  children: ReactNode
  tone?: 'neutral' | 'accent' | 'warn' | 'good' | 'bad' | 'info' | 'flame'
}) {
  const cls =
    tone === 'accent'
      ? 'bg-accent-soft text-accent-ink border-transparent'
      : tone === 'flame'
        ? 'bg-flame-soft text-flame-ink border-flame/30'
        : tone === 'neutral'
          ? TONE_CLASS.quiet
          : TONE_CLASS[tone]
  return <span className={`${PILL} ${cls}`}>{children}</span>
}

/** A removable chip. Used for the college/cluster assignments in Users. */
export function RemovableChip({
  label,
  onRemove,
  disabled = false,
  title,
}: {
  label: string
  onRemove: () => void
  disabled?: boolean
  title?: string
}) {
  return (
    <span
      className="inline-flex items-center gap-1 rounded-control border border-line bg-surface-2
        pl-2 pr-1 h-7 text-xs text-ink"
      title={title}
    >
      {label}
      <button
        type="button"
        onClick={onRemove}
        disabled={disabled}
        aria-label={`Remove ${label}`}
        className="inline-flex h-5 w-5 items-center justify-center rounded-full text-ink-3
          hover:text-bad-ink hover:bg-bad-wash transition disabled:opacity-40
          disabled:cursor-not-allowed"
      >
        ✕
      </button>
    </span>
  )
}

// --- Feedback ---------------------------------------------------------------

export function Spinner({ className = '' }: { className?: string }) {
  return (
    <svg
      className={`animate-spin ${className}`}
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      aria-hidden
    >
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" opacity="0.2" />
      <path
        d="M21 12a9 9 0 0 0-9-9"
        stroke="currentColor"
        strokeWidth="3"
        strokeLinecap="round"
      />
    </svg>
  )
}

export function Loading({ label = 'Loading' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-ink-3 py-10 justify-center">
      <Spinner />
      {label}…
    </div>
  )
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div
      className={`rounded-control border px-3 py-2 text-sm ${TONE_CLASS.bad}`}
      role="alert"
    >
      {children}
    </div>
  )
}

/**
 * "You are not looking at all of it."
 *
 * Rendered by every screen that reads a bounded list (lib/bounds.ts). It draws
 * NOTHING when the list is complete, so the common case costs a reader nothing
 * and the note's presence always means something.
 *
 * WHY THIS IS A COMPONENT AND NOT A COMMENT IN THE QUERY. A limit that
 * truncates in silence is worse than no limit at all: the reader has no way to
 * tell a short list from a complete one, and on this product the conclusions
 * drawn from a list are attendance and payout decisions. `role="status"` so a
 * screen reader is told as well — the sighted failure mode (a table that simply
 * stops) is at least visible; the unsighted one is not.
 *
 * `derived` is the important half. A count computed FROM a truncated list is
 * wrong rather than short, so a screen either suppresses that figure or names
 * it here. "12 overdue" is a different kind of lie from "200 of 640 rows".
 */
export function BoundNote({
  bound,
  noun,
  derived,
  onMore,
  step,
  atServerCap = false,
}: {
  bound: { limit: number; truncated: boolean } | undefined
  /** Plural, lowercase: "tasks", "trainers", "attendance rows". */
  noun: string
  /** What else on this screen is computed from the truncated list. */
  derived?: string
  onMore?: () => void
  step?: number
  /** The FastAPI path, where the bound is the server's cap — see boundedFromServer. */
  atServerCap?: boolean
}) {
  if (!bound?.truncated) return null
  return (
    <div
      role="status"
      className={`flex flex-wrap items-center gap-x-3 gap-y-2 rounded-control border
        px-3 py-2 text-xs leading-relaxed ${TONE_CLASS.warn}`}
    >
      <span className="min-w-0">
        Showing the first <span className="tabular-nums font-medium">{bound.limit}</span> {noun}
        {atServerCap ? ' — the server cap for this queue.' : '. There are more.'}
        {derived ? ` ${derived}` : ''}
      </span>
      {onMore && (
        <Button size="sm" variant="secondary" onClick={onMore}>
          Load {step ?? bound.limit} more
        </Button>
      )}
    </div>
  )
}

/** A quieter note for "this is deliberately not here", not for failures. */
export function InfoNote({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-control border border-line bg-surface-2 px-3 py-2 text-xs text-ink-2 leading-relaxed">
      {children}
    </div>
  )
}

/**
 * An empty state that TEACHES rather than reports (DESIGN.md §4.3).
 *
 * "No results." is the wrong answer to give anyone here, because in Phase 1 a
 * screen is empty for two completely different reasons — nothing has happened
 * yet, or a filter above is hiding what has — and those need opposite actions.
 * So the shape is: `title` names the absence, `body` describes what would
 * appear, `hint` says WHY it is empty right now, and `action` is the one thing
 * that fills it.
 *
 * `hint` is separate from `body` rather than a longer `body` because it is the
 * half that changes with context: the same list says "no trainers are deployed
 * to this program yet" (body) either because none ever were or because the
 * month filter excludes them (hint). Callers that only know one of those pass
 * one of them, and the old two-prop call sites keep working untouched.
 */
export function EmptyState({
  title,
  body,
  hint,
  action,
}: {
  title: string
  body?: string
  /** Why it is empty right now — the filter, the stage, the missing upstream step. */
  hint?: string
  action?: ReactNode
}) {
  return (
    <div className="text-center py-14 px-6">
      {/* A dashed, half-faded frame: the same visual grammar as an UNMARKED
          attendance cell, so "nothing here" looks the same everywhere. */}
      <svg
        width="40"
        height="40"
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden
        className="mx-auto mb-3 text-ink-3 opacity-45"
      >
        <rect
          x="2.75"
          y="4.75"
          width="18.5"
          height="14.5"
          rx="3"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeDasharray="3 3"
        />
        <path
          d="M7.5 11.5h9M7.5 15h5.5"
          stroke="currentColor"
          strokeWidth="1.4"
          strokeLinecap="round"
        />
      </svg>
      <p className="text-sm font-medium text-ink">{title}</p>
      {body && <p className="text-sm text-ink-3 mt-1 max-w-md mx-auto">{body}</p>}
      {hint && (
        <p className="text-xs text-ink-3 mt-2 max-w-md mx-auto leading-relaxed">{hint}</p>
      )}
      {action && <div className="mt-4 flex justify-center">{action}</div>}
    </div>
  )
}

// --- Modal ------------------------------------------------------------------

export function Modal({
  open,
  onClose,
  title,
  children,
  width = 'max-w-lg',
}: {
  open: boolean
  onClose: () => void
  title: string
  children: ReactNode
  width?: string
}) {
  const panelRef = useRef<HTMLDivElement>(null)
  const restoreRef = useRef<HTMLElement | null>(null)

  useEffect(() => {
    if (!open) return

    // Whatever opened the dialog gets focus back when it closes. Without this
    // the caret lands back at <body> and a keyboard user has to tab from the
    // top of the console to wherever they were — which on the payout screens is
    // twenty-odd stops down a table.
    restoreRef.current = document.activeElement as HTMLElement | null

    const panel = panelRef.current
    const focusable = (): HTMLElement[] =>
      Array.from(
        panel?.querySelectorAll<HTMLElement>(
          'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ) ?? [],
      ).filter((el) => el.offsetParent !== null || el === document.activeElement)

    ;(focusable()[0] ?? panel)?.focus()

    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key !== 'Tab') return

      // The trap. A modal that lets Tab walk out into the page behind it is not
      // a modal for anyone who cannot see which layer they are on — and this
      // dialog is where approvals get confirmed, so "which layer am I on" is
      // not a cosmetic question.
      const list = focusable()
      if (list.length === 0) {
        e.preventDefault()
        panel?.focus()
        return
      }
      const first = list[0]
      const last = list[list.length - 1]
      const active = document.activeElement
      if (e.shiftKey && (active === first || active === panel)) {
        e.preventDefault()
        last.focus()
      } else if (!e.shiftKey && active === last) {
        e.preventDefault()
        first.focus()
      }
    }

    window.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    return () => {
      window.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
      // Guarded: the trigger is often a row action that the mutation this modal
      // just fired has already unmounted.
      const back = restoreRef.current
      if (back && document.contains(back)) back.focus()
    }
  }, [open, onClose])

  if (!open) return null

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4 sm:p-8">
      {/* Navy rather than black. A neutral scrim over a blue-tinted app reads as
          dirt on the screen; the brand's own deep ground reads as depth. */}
      <div
        className="fixed inset-0 bg-navy/40 backdrop-blur-[3px]"
        onClick={onClose}
        aria-hidden
      />
      {/* One of the few places glacier is allowed (DESIGN.md §3): a modal shell
          is chrome, and the content inside it sits on its own opaque ground. */}
      <div
        ref={panelRef}
        tabIndex={-1}
        className={`glass-strong relative w-full ${width} rounded-panel border
          shadow-raised animate-in my-auto focus:outline-none`}
        role="dialog"
        aria-modal="true"
        aria-label={title}
      >
        <div className="flex items-center justify-between border-b border-line px-5 py-3.5">
          <h3 className="text-sm font-semibold text-ink">{title}</h3>
          <Button variant="ghost" size="sm" onClick={onClose} aria-label="Close">
            ✕
          </Button>
        </div>
        <div className="p-5">{children}</div>
      </div>
    </div>
  )
}

// --- Tabs -------------------------------------------------------------------

export function Tabs<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: T; label: string; hint?: string }[]
  active: T
  onChange: (id: T) => void
}) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])

  /**
   * Roving tabindex, which is what `role="tablist"` has been promising since
   * this component was written and not delivering. Only the selected tab is in
   * the tab order; the arrows move between them. Without it a keyboard user
   * pays one Tab stop per tab just to reach the panel, and on Program Detail
   * that is five stops of nothing.
   *
   * Activation follows focus — arrowing to a tab selects it. That is the right
   * call here because every panel is already mounted client-side and switching
   * costs nothing; the manual-activation pattern exists for tabs whose panels
   * are expensive to load, which none of ours are.
   */
  const onKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>, i: number) => {
    const step = e.key === 'ArrowRight' ? 1 : e.key === 'ArrowLeft' ? -1 : 0
    const next =
      step !== 0
        ? (i + step + tabs.length) % tabs.length
        : e.key === 'Home'
          ? 0
          : e.key === 'End'
            ? tabs.length - 1
            : -1
    if (next < 0) return
    e.preventDefault()
    onChange(tabs[next].id)
    refs.current[next]?.focus()
  }

  return (
    <div className="flex items-center gap-1 mb-5 border-b border-line" role="tablist">
      {tabs.map((t, i) => (
        <button
          key={t.id}
          type="button"
          role="tab"
          aria-selected={active === t.id}
          tabIndex={active === t.id ? 0 : -1}
          ref={(el) => {
            refs.current[i] = el
          }}
          onKeyDown={(e) => onKeyDown(e, i)}
          onClick={() => onChange(t.id)}
          className={`px-3 h-9 text-sm font-medium border-b-2 -mb-px transition ${
            active === t.id
              ? 'border-accent text-ink'
              : 'border-transparent text-ink-2 hover:text-ink'
          }`}
        >
          {t.label}
          {t.hint && <span className="ml-1.5 text-xs tabular-nums text-ink-3">{t.hint}</span>}
        </button>
      ))}
    </div>
  )
}

// --- Misc -------------------------------------------------------------------

/**
 * Progress meter used on program cards and the college progress view.
 *
 * A bar with no number beside it is a picture, not a reading — you can see that
 * something is "about two thirds" and not that it is 19 of 29 tasks. So this
 * component now draws the figure itself.
 *
 * It draws it only when the caller passes `label`, and that is a compatibility
 * decision rather than a design one: every one of the six existing call sites
 * already hand-renders its own number in the surrounding markup, so printing
 * one unconditionally would show it twice on every screen in the app. Passing
 * `label` hands the whole row — label, bar, percentage — to this component and
 * lets the call site delete its copy. New code should do that; `<Meter pct>`
 * alone stays exactly as it was.
 *
 * `pct == null` means "not known", which is not the same as zero, so it renders
 * an em dash and an empty track rather than a confident 0%.
 */
export function Meter({
  pct,
  label,
  showValue,
}: {
  pct: number | null
  /** Naming the bar also turns on the printed percentage beside it. */
  label?: string
  /** Override, for the rare bar that wants its number without a label. */
  showValue?: boolean
}) {
  const value = pct ?? 0
  const clamped = Math.max(0, Math.min(100, value))
  const withText = showValue ?? label !== undefined
  const text = pct == null ? '—' : `${Math.round(pct)}%`

  const bar = (
    <div
      className="h-1.5 w-full rounded-full bg-surface-2 overflow-hidden"
      role="progressbar"
      aria-valuenow={value}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuetext={pct == null ? 'Not known' : text}
      aria-label={label}
    >
      <div
        className="h-full rounded-full bg-accent transition-[width] duration-500"
        style={{ width: `${clamped}%` }}
      />
    </div>
  )

  if (!withText) return bar

  // Labelled: the figure sits on the label's baseline, above the bar, so a
  // column of these lines its percentages up. Unlabelled: it sits inline to the
  // right, which is what a bar inside a table cell has room for.
  if (!label) {
    return (
      <div className="flex items-center gap-2">
        {bar}
        <span className="text-xs tabular-nums text-ink-2 shrink-0">{text}</span>
      </div>
    )
  }

  return (
    <div className="w-full">
      <div className="flex items-baseline justify-between gap-3 mb-1.5">
        <span className="text-xs font-medium text-ink-2 min-w-0 truncate">{label}</span>
        <span className="text-xs tabular-nums text-ink-2 shrink-0">{text}</span>
      </div>
      {bar}
    </div>
  )
}

const STAT_TONE: Record<'normal' | 'good' | 'warn' | 'bad', string> = {
  normal: 'text-ink',
  good: 'text-good',
  warn: 'text-warn',
  bad: 'text-bad',
}

/**
 * One figure and what it counts.
 *
 * `hint` is the important addition. A stat card reading "Marked / 24" is
 * ambiguous in exactly the way this domain cannot afford — marked out of what,
 * counted how, over which window — and the answer is usually one short line
 * that nobody has room for in the label. Attendance completeness is a hard
 * block for CRT and a warning for bCAP (CLAUDE.md §5), so "what does this number
 * count" is a question with a payout attached to it.
 *
 * The hint sits under the LABEL rather than under the value, which is the one
 * tradeoff worth flagging: it pushes the number down, so a row mixing hinted
 * and unhinted stats will not have its figures on a common baseline. Hint every
 * stat in a row or none of them.
 *
 * The tone list widened from `normal | warn` and nothing was removed, so the
 * existing call sites are untouched. Tones here are for a number that is ITSELF
 * the bad news (unmarked days, failed validations) — not for decorating a
 * healthy figure green, which would spend the one colour that currently means
 * "this completed" on saying nothing.
 */
export function Stat({
  label,
  value,
  hint,
  tone = 'normal',
}: {
  label: string
  value: number | string
  /** One line saying what the number counts, and over what period. */
  hint?: string
  tone?: 'normal' | 'good' | 'warn' | 'bad'
}) {
  return (
    <Card className="p-4">
      <p className="text-xs text-ink-3">{label}</p>
      {hint && <p className="text-[11px] text-ink-3 mt-0.5 leading-snug">{hint}</p>}
      <p className={`text-2xl font-semibold tabular-nums mt-1 ${STAT_TONE[tone]}`}>{value}</p>
    </Card>
  )
}

export function fmtDate(value: string | null | undefined): string {
  if (!value) return '—'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return '—'
  return d.toLocaleDateString(undefined, { day: '2-digit', month: 'short', year: 'numeric' })
}

/**
 * Render a numeric(14,2) as grouped rupees.
 *
 * Deliberately a STRING formatter: it groups digits and prefixes ₹, and it
 * never parses, adds, or rounds. Money arithmetic lives in
 * `services/remuneration/engine.py` with Decimal (CLAUDE.md R2/R7); a helper
 * here that returned a number would be the first step towards it happening in
 * the browser instead.
 *
 * `number` is accepted only because PostgREST serialises `numeric` as a JSON
 * number, so a plain `select('*')` hands JavaScript a float before any of our
 * code runs — `rate` arrives as `80000.0`, not `"80000.00"`. Prefer selecting
 * `rate::text` (see WorkOrdersPage) so full precision survives the wire; this
 * branch is the safety net for the queries that do not, and for the ones nobody
 * has written yet.
 *
 * It previously accepted `string` alone and called `.split()` unconditionally,
 * which threw `value.split is not a function` on the first work order anyone
 * saved and — with no error boundary above it — blanked the entire console.
 */
export function fmtAmount(value: string | number | null | undefined): string {
  if (value === null || value === undefined || value === '') return '—'
  // String(80000.1) is "80000.1", never exponential, for every rupee value the
  // schema allows (numeric(14,2) tops out well below 1e21).
  const text = typeof value === 'number' ? (Number.isFinite(value) ? String(value) : '') : value
  if (text === '') return '—'
  const [whole, fraction = ''] = text.split('.')
  const negative = whole.startsWith('-')
  const digits = negative ? whole.slice(1) : whole

  // Indian grouping: last three, then pairs (12,34,567).
  const head = digits.slice(0, -3)
  const tail = digits.slice(-3)
  const grouped =
    head.length > 0 ? `${head.replace(/\B(?=(\d{2})+(?!\d))/g, ',')},${tail}` : tail

  const paise = fraction.replace(/0+$/, '')
  return `${negative ? '-' : ''}₹${grouped}${paise ? `.${paise}` : ''}`
}
/* --------------------------------------------------------------------------
   CLARITY PRIMITIVES

   Everything below exists because a first-time reader could not answer "what is
   this screen for, and what do I do next" from the screen alone (DESIGN.md §4).
   They are deliberately in the kit rather than in the pages: a search box that
   behaves differently on two screens is worse than two search boxes that are
   both mediocre, because the reader cannot carry anything they learn.
-------------------------------------------------------------------------- */

/**
 * The search field, and the only one. `/` focuses it from anywhere on the page,
 * Esc clears and lets go, and it prints how many rows survived the filter.
 *
 * THE RESULT COUNT IS THE POINT. A filter that silently narrows a list is how
 * someone concludes a trainer does not exist and re-onboards them — the failure
 * is invisible precisely because the screen looks fine. "12 of 240" makes the
 * narrowing a fact on screen rather than something the reader has to remember
 * they caused. It is `role="status"` so it is announced too; a filtered list is
 * a change nobody asked for out loud.
 *
 * `type="text"` and not `type="search"`, deliberately: WebKit draws its own
 * clear affordance inside a search input, and two ✕ buttons a few pixels apart
 * doing the same thing is worse than one we control the label of. The role is
 * declared explicitly to get the semantics back.
 */
export function SearchInput({
  value,
  onChange,
  placeholder = 'Search',
  count,
  total,
  className = '',
  autoFocus = false,
}: {
  value: string
  onChange: (v: string) => void
  placeholder?: string
  /** Rows surviving the filter. */
  count?: number
  /** Rows before it. */
  total?: number
  className?: string
  autoFocus?: boolean
}) {
  const ref = useRef<HTMLInputElement>(null)

  // `/` from anywhere, unless the reader is already typing somewhere — the
  // shortcut has to lose to a text field, or it eats a slash in every date and
  // every "12/31" note someone writes.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== '/' || e.ctrlKey || e.metaKey || e.altKey) return
      const el = document.activeElement as HTMLElement | null
      const tag = el?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el?.isContentEditable) return
      e.preventDefault()
      ref.current?.focus()
      ref.current?.select()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const showCount = count !== undefined
  const countText =
    count === undefined
      ? ''
      : total === undefined
        ? `${count}`
        : `${count} of ${total}`

  return (
    <div className={`relative ${className || 'w-full max-w-xs'}`}>
      <svg
        width="15"
        height="15"
        viewBox="0 0 24 24"
        fill="none"
        aria-hidden
        className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-3"
      >
        <circle cx="11" cy="11" r="6.5" stroke="currentColor" strokeWidth="1.8" />
        <path d="M16 16l4.5 4.5" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>

      <input
        ref={ref}
        type="text"
        role="searchbox"
        value={value}
        autoFocus={autoFocus}
        placeholder={placeholder}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key !== 'Escape') return
          // Esc clears first and only lets go of focus if there was nothing to
          // clear, so one press never does two things at once.
          if (value) {
            e.stopPropagation()
            onChange('')
          } else {
            e.currentTarget.blur()
          }
        }}
        className={`${CONTROL} pl-9 ${showCount ? 'pr-[7.5rem]' : 'pr-9'}`}
      />

      {/* Fixed reservation rather than a measured one: the count is at most
          "1,240 of 12,405" at 11px, and a ResizeObserver to save 20px of
          padding is not a trade worth making in a field this small. */}
      <div className="absolute right-1.5 top-1/2 -translate-y-1/2 flex items-center gap-1">
        {showCount && (
          <span role="status" className="text-[11px] tabular-nums text-ink-3 whitespace-nowrap">
            {countText}
          </span>
        )}
        {value !== '' && (
          <button
            type="button"
            aria-label="Clear search"
            onClick={() => {
              onChange('')
              ref.current?.focus()
            }}
            className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded-full
              text-ink-3 hover:text-ink hover:bg-surface-2 transition"
          >
            ✕
          </button>
        )}
      </div>
    </div>
  )
}

/**
 * A domain term with its explanation attached to it.
 *
 * This app is thick with vocabulary that is obvious to Rajesh and opaque to a
 * new LDE Executive on their first morning — CRT, bCAP, LOP, ERM, WO, TDS,
 * UNMARKED, the DRAFT → PENDING → APPROVED → RELEASED ladder. DESIGN.md §4.2 is
 * explicit that these get explained where they are USED, not in a wiki nobody
 * opens, because the moment of confusion and the moment of reading are the same
 * moment.
 *
 * Hover AND focus, because keyboard users exist and a hover-only tooltip is
 * simply not there for them. `aria-describedby` wires the popover to the button
 * so a screen reader gets the explanation as the description of the control
 * rather than as loose text that happened to appear nearby.
 */
export function HelpTip({ term, children }: { term: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const id = useId()

  return (
    <span
      className="relative inline-flex items-center gap-1"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      <span>{term}</span>
      <button
        type="button"
        aria-label={`What is ${term}?`}
        aria-expanded={open}
        aria-describedby={open ? id : undefined}
        onFocus={() => setOpen(true)}
        onBlur={() => setOpen(false)}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (e.key !== 'Escape') return
          e.stopPropagation()
          setOpen(false)
        }}
        className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full
          border border-line bg-surface-2 text-[10px] font-semibold leading-none text-ink-3
          hover:border-accent/50 hover:text-accent transition"
      >
        ?
      </button>
      {open && (
        <span
          id={id}
          role="tooltip"
          // `normal-case`/`font-normal` because these get dropped into <Th>,
          // which is uppercase and letter-spaced. A definition rendered in
          // small caps is a definition nobody reads.
          className="absolute left-0 top-full z-30 mt-1.5 w-64 rounded-card border border-line
            bg-surface p-2.5 text-xs font-normal normal-case leading-relaxed tracking-normal
            text-ink-2 shadow-raised"
        >
          {children}
        </span>
      )}
    </span>
  )
}

/**
 * The key for a grid whose colours mean something.
 *
 * Pairs with the attendance grid and the program board. `swatch` is a className
 * rather than a token name so a caller can pass whatever the grid itself uses —
 * `attendanceCellClass`'s dashed UNMARKED treatment included — and the key
 * cannot drift from the thing it is describing by being a second definition of
 * it.
 */
export function Legend({
  items,
  className = '',
}: {
  items: { swatch: string; label: string; hint?: string }[]
  className?: string
}) {
  return (
    <ul className={`flex flex-wrap items-center gap-x-4 gap-y-1.5 ${className}`}>
      {items.map((it) => (
        <li key={it.label} className="flex items-center gap-1.5 text-[11px] text-ink-2">
          <span
            aria-hidden
            className={`inline-block h-3 w-3 shrink-0 rounded-[3px] border ${it.swatch}`}
          />
          <span className="font-medium">{it.label}</span>
          {it.hint && <span className="text-ink-3">{it.hint}</span>}
        </li>
      ))}
    </ul>
  )
}

/**
 * The filter/search/action bar above a table.
 *
 * Wraps rather than scrolls. A toolbar that overflows sideways hides controls
 * with no indication they are there, and the ones that end up off the right
 * edge are reliably the filters — the things whose invisibility causes the
 * silent-narrowing problem `SearchInput` exists to prevent.
 */
export function Toolbar({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`flex flex-wrap items-center gap-2 ${className}`}>{children}</div>
  )
}

/**
 * A loading placeholder shaped like the thing it is standing in for.
 *
 * `Loading`'s centred spinner is the right answer for a whole screen and the
 * wrong one for a table, because it collapses the layout and then springs it
 * back. A skeleton holds the shape, so the page does not jump when the rows
 * land and the reader's eye is already where the data will be.
 *
 * The caller owns the sizing entirely — `className` REPLACES the default rather
 * than merging with it, because Tailwind resolves conflicting utilities by
 * stylesheet order, not by string order, so a merged `h-4` and `h-8` would win
 * unpredictably.
 */
export function Skeleton({ className = '' }: { className?: string }) {
  return <div aria-hidden className={`skeleton ${className || 'h-4 w-full'}`} />
}

/**
 * A table's worth of skeleton rows.
 *
 * `role="status"` with a label, so the wait is announced rather than being a
 * silent stretch of nothing — the unsighted version of a blank screen is
 * indistinguishable from a broken one. The bars are given varied widths because
 * a perfect grid of identical rectangles reads as a UI element rather than as
 * content on its way.
 */
export function TableSkeleton({ rows = 5, cols = 4 }: { rows?: number; cols?: number }) {
  // Varied but deterministic — a random width would reshuffle on every render
  // and turn a calm placeholder into a flicker.
  const width = (c: number) => ['w-3/4', 'w-1/2', 'w-2/3', 'w-5/6', 'w-2/5'][c % 5]

  return (
    <div role="status" aria-label="Loading rows" className="w-full">
      {Array.from({ length: rows }, (_, r) => (
        <div
          key={r}
          className="grid gap-4 border-b border-line-soft px-4 py-3 last:border-0"
          style={{ gridTemplateColumns: `repeat(${Math.max(1, cols)}, minmax(0, 1fr))` }}
        >
          {Array.from({ length: Math.max(1, cols) }, (_, c) => (
            <Skeleton key={c} className={`h-3.5 ${width(r + c)}`} />
          ))}
        </div>
      ))}
    </div>
  )
}

/**
 * The plain-English explainer strip at the top of a screen.
 *
 * This is the single biggest clarity win available and it costs one paragraph.
 * `PageHeader`'s `purpose` says what the screen is FOR; this says how it works
 * and, via `steps`, in what order — "what happens here" as a sequence, which is
 * the thing an ops tool's screens actually are and the thing their navigation
 * never shows. Someone landing on Payouts should not have to infer that
 * attendance is locked before validation runs before anyone can approve.
 *
 * Prose, not jargon. If a step cannot be written without an acronym, wrap the
 * acronym in `HelpTip` rather than assuming it.
 */
export function PageIntro({
  children,
  steps,
  className = '',
}: {
  children: ReactNode
  /** "What happens here, in order." Three to five short phrases. */
  steps?: string[]
  className?: string
}) {
  return (
    <div
      className={`rounded-card border border-line bg-surface-2 px-4 py-3 ${className}`}
    >
      <div className="text-sm text-ink-2 leading-relaxed max-w-3xl">{children}</div>
      {steps && steps.length > 0 && (
        <ol className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-2">
          {steps.map((s, i) => (
            <li key={s} className="flex items-center gap-2 text-xs text-ink-2">
              {i > 0 && (
                <span aria-hidden className="text-ink-3">
                  →
                </span>
              )}
              <span
                aria-hidden
                className="inline-flex h-4 w-4 shrink-0 items-center justify-center rounded-full
                  bg-accent-soft text-[10px] font-semibold tabular-nums text-accent-ink"
              >
                {i + 1}
              </span>
              {s}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}

/**
 * One labelled fact: muted label on the left, value on the right.
 *
 * Roughly eight screens hand-roll this pair today, which is how the label
 * weight and the gap ended up slightly different on each of them. `mono` is
 * separate from the value itself so the caller does not have to remember that
 * identifiers get a monospaced face — see `MonoValue` for why that matters.
 *
 * Rendered as plain elements rather than <dt>/<dd>, despite being a definition
 * list row in spirit: those are only valid inside a <dl>, and this is a leaf
 * component dropped into cards, table cells and modal bodies that are not one.
 * A row that is invalid HTML in most of its call sites is not worth the
 * semantics it would buy in the rest.
 */
export function KeyValue({
  label,
  value,
  children,
  mono = false,
  className = '',
}: {
  label: ReactNode
  /** The value. `children` works too, and wins if both are given. */
  value?: ReactNode
  children?: ReactNode
  mono?: boolean
  className?: string
}) {
  const shown = children ?? value
  return (
    <div className={`flex items-baseline justify-between gap-4 py-1.5 ${className}`}>
      <span className="text-xs text-ink-2 shrink-0">{label}</span>
      <span
        className={`text-sm text-ink text-right min-w-0 break-words ${
          mono ? 'font-mono text-[0.8125rem] tracking-tight' : ''
        }`}
      >
        {shown === null || shown === undefined || shown === '' ? (
          <span className="text-ink-3">—</span>
        ) : (
          shown
        )}
      </span>
    </div>
  )
}

/**
 * An identifier, in the face identifiers belong in.
 *
 * PANs, IFSC codes, invoice numbers, account numbers, version hashes. DESIGN.md
 * §2 is blunt about this and it is not typographic taste: trainer identity IS
 * the PAN (CLAUDE.md §6) and the invoice number is seeded from it, so an `l`
 * that looks like a `1` in a proportional face is a mismatched payee, not a
 * cosmetic complaint. Geist Mono plus the body's `cv05`/`cv08` alternates keeps
 * those apart.
 *
 * `select-all` because every real use of one of these values is copying it into
 * ZOHO, a bank form or a support thread, and a half-selected IFSC is worse than
 * none.
 */
export function MonoValue({
  children,
  className = '',
  title,
}: {
  children: ReactNode
  className?: string
  title?: string
}) {
  if (children === null || children === undefined || children === '') {
    return <span className="text-ink-3">—</span>
  }
  return (
    <span
      title={title}
      className={`font-mono text-[0.8125rem] tracking-tight select-all ${className}`}
    >
      {children}
    </span>
  )
}
