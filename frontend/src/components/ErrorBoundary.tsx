import { Component, type ErrorInfo, type ReactNode } from 'react'

/**
 * Catches render errors so one broken screen does not blank the whole console.
 *
 * WHY THIS EXISTS
 * ---------------
 * React unmounts the entire tree when a render throws and nothing catches it.
 * The result is a completely empty page: no message, no stack, no clue which
 * component failed — the app simply disappears. That is what happened the first
 * time anyone saved a work order, because `fmtAmount` was typed for a string and
 * PostgREST handed it a number.
 *
 * A blank screen is the most expensive failure mode this app can have. It looks
 * identical to "no data", "not logged in", "server down" and "you lack
 * permission" — four problems with four different fixes — so whoever hits it
 * cannot even report it usefully. An error boundary converts all of that into
 * one sentence and a stack.
 *
 * Deliberately NOT a silent fallback. It shows the real message, because the
 * people using this are internal staff who will paste it into a bug report, and
 * because an ops tool that hides its failures gets trusted less than one that
 * admits them.
 *
 * WHO IS ACTUALLY READING THIS. Not an engineer. It is an LDE Executive on a
 * campus, mid-task, and the first thing they need is permission to stop
 * worrying: nothing they typed broke, nothing they saved is lost, the rest of
 * the console still works, and here is the button that usually fixes it. The
 * exception text is still on screen and still complete — it has just stopped
 * being the headline, because a stack trace as the opening line reads as "you
 * have broken something expensive" to everyone who cannot parse it. It lives
 * behind a <details> that a support thread can ask them to open.
 */

interface Props {
  children: ReactNode
  /** Shown above the error. Defaults to a generic line. */
  label?: string
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Keep the component stack in the console for whoever is debugging; the
    // rendered panel stays short enough to screenshot.
    console.error('Render error caught by ErrorBoundary:', error, info.componentStack)
  }

  private reset = (): void => this.setState({ error: null })

  render(): ReactNode {
    const { error } = this.state
    if (!error) return this.props.children

    return (
      <div className="p-6">
        {/* `border-bad/40 bg-bad-wash`, and this is a fix rather than a
            restyle: the previous markup asked for `border-danger/40
            bg-danger/5`, and there has never been a `danger` colour token in
            index.css. Tailwind generates nothing for a token it does not know,
            so the panel that announces a crash was rendering with no tint and a
            default border — the one surface in the app that most needed to look
            different from an ordinary card looked exactly like one. */}
        <div className="max-w-2xl rounded-card border border-bad/40 bg-bad-wash p-5 shadow-card">
          <h2 className="text-base font-semibold text-ink">
            {this.props.label ?? 'This part of the screen could not be displayed'}
          </h2>

          <p className="mt-2 text-sm text-ink-2 leading-relaxed">
            Something went wrong while drawing this page. Nothing you entered has been sent
            and nothing already saved has been changed — this is a display problem, not a
            data one.
          </p>
          <p className="mt-2 text-sm text-ink-2 leading-relaxed">
            Reloading fixes it most of the time. If it keeps happening, open the technical
            detail below and paste it into your message to the ops tech team — it says
            exactly what failed.
          </p>

          <div className="mt-4 flex flex-wrap gap-2">
            <button
              onClick={() => window.location.reload()}
              className="inline-flex h-9 items-center rounded-control bg-accent px-3.5 text-sm
                font-medium text-on-accent shadow-card transition hover:bg-accent-hover"
            >
              Reload the page
            </button>
            <button
              onClick={this.reset}
              className="inline-flex h-9 items-center rounded-control border border-line
                bg-surface px-3.5 text-sm font-medium text-ink transition hover:bg-surface-2"
            >
              Try again without reloading
            </button>
          </div>

          <p className="mt-3 text-xs text-ink-3">
            The rest of the console is unaffected — the navigation still works.
          </p>

          {/* Collapsed, not hidden. The message is the one thing that makes a
              bug report actionable, so it stays on the page and one click from
              a screenshot; it just no longer greets a non-engineer with a stack
              frame. */}
          <details className="mt-4 group">
            <summary
              className="cursor-pointer text-xs font-medium text-ink-2 hover:text-ink
                marker:text-ink-3"
            >
              Technical detail (for a bug report)
            </summary>
            <pre
              className="mt-2 overflow-x-auto rounded-control border border-line bg-surface-2
                p-3 text-xs leading-relaxed text-ink-2 whitespace-pre-wrap break-words"
            >
              {error.message}
            </pre>
            <p className="mt-2 text-xs text-ink-3">
              The full stack and component trace are in the browser console.
            </p>
          </details>
        </div>
      </div>
    )
  }
}
