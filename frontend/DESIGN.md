# Ops Console — design system

The contract every screen in this console is built against. If you are changing
UI here, this file is the spec; `src/index.css` is its implementation.

---

## 1. Where the colours come from

They are **byteXL's published brand tokens**, read off `bytexl.ai`'s own
stylesheet. They are not approximations and they are not to be "tuned".

| byteXL token | Value | Our token |
|---|---|---|
| `--brand` / `--primary` / `--ring` | `#0060c2` | `accent` |
| `--primary-foreground` | `#f8fafd` | `on-accent` |
| `--foreground` / `--card-foreground` | `#102034` | `ink` |
| `--muted-foreground` | `#5e6a7a` | `ink-2` |
| `--muted` | `#eff3f8` | `surface-2` |
| `--secondary` / `--secondary-foreground` | `#eaf1f8` / `#183356` | `brand-wash` / `brand-wash-ink` |
| `--accent` / `--accent-foreground` | `#e3eefa` / `#0d2e55` | `accent-soft` / `accent-ink` |
| `--border` / `--input` | `#dde4eb` | `line` |
| `--card` / `--background` | `#ffffff` | `surface` |
| `--sidebar` | `#f8fafd` | `canvas` |
| `--navy` / `--navy-foreground` | `#0b1e36` / `#f1f6fa` | `navy` / `navy-ink` |
| `--flame` | `#f96428` | `flame` |
| `--destructive` | `#e40014` | `bad` |
| `--chart-1..5` | `#0060c2 #fd6e2d #f05100 #c53c00 #9f2d00` | `chart-1..5` |
| font (sans / mono) | Inter / Geist Mono | `font-sans` / `font-mono` |

**We kept our own token names.** `canvas / surface / ink / accent / line` are
referenced by ~17,000 lines of existing markup. Renaming them to byteXL's names
would be a 26-file rewrite that changes nothing on screen. The rebrand is the
values, not the names.

byteXL ships **no dark palette**. Ours is derived: their `--navy` taken as the
deepest ground and stepped up from there, hues preserved, only lightness and
chroma moved — so dark mode reads as the same brand, not a grey inversion.

---

## 2. The token vocabulary

Use tokens. Never write a raw Tailwind colour literal (`bg-emerald-500/12`,
`text-amber-700`, `border-red-500/30`) in a component again — that is exactly
how 120 of them accumulated across the pages and why nothing matched.

### Ground and surface — three depths, no more

| Token | Use |
|---|---|
| `canvas` | the page behind everything |
| `surface` | a card, a panel, a row |
| `surface-2` | a well *inside* a surface: table header, inert chip, code block |
| `line` | every border |
| `line-soft` | a divider inside a card, where `line` is too loud |

A fourth depth is how a dense screen stops reading as a hierarchy and starts
reading as noise. Do not add one.

### Ink — three weights

| Token | Use |
|---|---|
| `ink` | the thing you are meant to read |
| `ink-2` | labels, secondary copy |
| `ink-3` | metadata, placeholders, timestamps |

### Accent — one, carrying every interactive meaning

`accent` (fill, active border) · `accent-hover` · `on-accent` (text on a filled
accent) · `accent-soft` (selected / active background) · `accent-ink` (text on
`accent-soft`) · `brand-wash` + `brand-wash-ink` (a quieter chrome wash for
chips and rails that should not read as a selection).

### Semantic tones — four meanings, each a triple

`good` · `warn` · `bad` · `info`, each with a `-wash` (background) and an `-ink`
(text on that wash).

```
good   something completed, present, signed, reconciled
warn   something that needs a human to look, but is not yet wrong
bad    something failed, blocked, expired, or is refusing to proceed
info   neutral machine state — sent, scheduled, queued
```

A pill is now **one lookup**: `bg-good-wash text-good-ink border-good/25`.

### `flame` is reserved

`flame` marks **generated / AI-drafted surfaces**, so a reader can always tell a
drafted artefact from a queried fact — CLAUDE.md R1, "the database owns truth,
the LLM owns language". Do not spend it on decoration. The moment `flame` means
two things it means nothing.

### Radius, elevation, type

`rounded-control` (0.5rem — input, button, chip) · `rounded-card` (0.75rem) ·
`rounded-panel` (1rem — modal, drawer).

`shadow-card` · `shadow-card-hover` · `shadow-raised` · `shadow-glass`. Shadows
are navy-tinted, not black: a grey shadow on a blue-tinted ground reads as dirt.

`font-sans` (Inter) · `font-mono` (Geist Mono — PANs, IFSC codes, invoice
numbers, hashes). Body sets Inter's `cv05`/`cv08` alternates so `l` and `1` are
distinguishable, and `tnum`/`zero` so rupee columns align. **Never** put an
identifier in a proportional face.

---

## 3. Glacier

Frosted, cool, translucent chrome. Two utilities: `.glass` and `.glass-strong`.

**Where glacier goes:** sidebar, sticky page header, sticky table head, modal
shell and its scrim, floating toolbars.

**Where it must never go:** cards, table rows, form fields, anything carrying a
number. Frosting a surface a reader parses figures on is decoration bought with
legibility, and the figures on this product decide trainer payouts. *Glacier is
chrome, never content.*

The `.glass` utilities declare an **opaque** background as the base and apply
translucency only inside `@supports (backdrop-filter: ...)`. Support is the
exception, not the assumption — where `backdrop-filter` is missing, a
translucent panel over dense text is unreadable, so those browsers get a solid
panel instead.

---

## 4. Clarity rules — a fresher must be able to read this app

This is the goal that outranks the aesthetics. Someone opening a screen for the
first time should be able to say what it is for and what to do next.

1. **Every screen states its purpose.** `PageHeader` takes a `purpose` — one
   plain sentence, no jargon: *"Trainer pay for one month, checked against the
   signed work order before anyone approves it."* Not *"Payout cycle
   management."*

2. **Every domain term is explained where it is used**, not in a wiki. `CRT`,
   `bCAP`, `LOP`, `ERM`, `WO`, `TDS`, `P/A/H/UNMARKED`, `DRAFT → PENDING →
   APPROVED → RELEASED`. Use `<HelpTip>` inline and `<Legend>` above any grid
   whose colours mean something.

3. **Empty states teach.** Never just "No results." Say what would appear here,
   why it is empty, and the one action that fills it.

4. **Search is one component, everywhere.** `<SearchInput>`: a magnifier, a
   clear button, `/` to focus, `Esc` to clear, and a live result count
   (`12 of 240`). A filter that silently narrows a list is how someone concludes
   a trainer does not exist.

5. **Numbers say what they are.** Money is `fmtAmount` and `font-mono`. A count
   derived from a truncated list is either suppressed or labelled — see
   `BoundNote`, which already does this and is the pattern.

6. **Colour always carries the same meaning.** If it is green it completed. A
   green used decoratively anywhere teaches the reader to stop trusting green
   everywhere.

7. **Destructive and irreversible actions look different from ordinary ones**,
   and say what will happen before they happen. Approval and release are
   separate actions with separate audit rows (R4) — the UI must never blur them
   into one button.

---

## 5. Accessibility floor

- Contrast: 4.5:1 body, 3:1 large text and UI borders — in **both** themes.
- Colour is never the only channel. Every status pill carries a text label; the
  attendance grid carries a letter as well as a fill.
- `:focus-visible` is a 2px accent outline with offset. Never remove it.
- Every icon-only control has an `aria-label`. Every live region that updates
  without user action has `role="status"`.
- `prefers-reduced-motion` is honoured globally in `index.css`; do not add an
  animation that opts back out of it.

---

## 6. Working in here

- Compose from `src/components/ui.tsx`. If you need a new primitive, add it
  there — do not build a one-off in a page. A pattern that exists twice is a
  component.
- `npm run typecheck` and `npm run test` must pass before you are done.
- No new dependencies for styling. The kit is a few hundred lines we can read;
  trading that for a component library buys a theme system we would spend longer
  fighting than writing.
