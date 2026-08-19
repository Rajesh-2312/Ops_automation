/**
 * The Delivery Monitor / Escalation Engine client. CLAUDE.md §8.
 *
 * Unlike almost every other screen in this app, the alerts feed does NOT come
 * from PostgREST. There is no `alerts` table and no `escalations` table to
 * select from: both services are pure functions over attendance, tasks and
 * documents that are already stored, and `app/api/monitoring.py` runs them on
 * request. So this file talks to FastAPI through `apiGet`, and the user's
 * Supabase JWT is what the backend re-checks persona and reach against.
 *
 * WHAT THIS SCREEN MAY DO
 * -----------------------
 * Read. That is the whole list. §8 puts the Delivery Monitor at autonomy level 1
 * — "Observe: read, report, alert internally" — so there is no acknowledge, no
 * dismiss, no snooze and no notify here, and no mutation helper below for one to
 * be built on. An escalation is worked by doing the underlying job (marking the
 * attendance, closing the task), not by clearing it from a list.
 *
 * NUMBERS ARRIVE AS STRINGS
 * -------------------------
 * `score`, `measured` and `threshold` are `Decimal` on the server and are
 * serialised as strings so JSON's float cannot round them. Compare them with
 * `Number(...)` for sorting only, never recompute a score from them — the
 * banding is the engine's (R1, and the same reasoning as `fmtAmount`).
 *
 * QUERY KEYS ARE LOCAL, ON PURPOSE
 * --------------------------------
 * `lib/queryKeys.ts` exists because invalidation is a cross-screen concern:
 * ticking a task off the work queue has to invalidate the board's roll-up. This
 * feed is derived and read-only, so nothing in the app invalidates it and
 * nothing it does invalidates anything else. Its keys therefore live with the
 * only file that uses them.
 */

import { apiGet } from './api'

/** Banded reading of a program's risk score. `app/domain/risk.py:RiskBand`. */
export type RiskBand = 'low' | 'medium' | 'high' | 'critical'

/** `app/domain/risk.py:AnomalySeverity`. Three levels, not five. */
export type AnomalySeverity = 'info' | 'warning' | 'critical'

/** A rung of the §4 chain. There is no college or trainer tier — internal only. */
export type EscalationTier = 'lde_executive' | 'manager' | 'senior_manager'

export interface Anomaly {
  code: string
  severity: AnomalySeverity
  message: string
  /** The operative figures, already stringified by the detector that raised it. */
  detail: Record<string, string>
}

export interface DeploymentRisk {
  deployment_id: string
  trainer_name: string
  batch_name: string
  period_start: string
  period_end: string
  /** The last day a mark could reasonably exist for. Days after it are not late. */
  elapsed_through: string
  score: string
  band: RiskBand
  headline: string
  lines: string[]
  anomalies: Anomaly[]
}

export interface Escalation {
  code: string
  metric: string
  comparison: string
  measured: string
  threshold: string
  severity: AnomalySeverity
  reason: string
  requested_tier: EscalationTier
  resolved_tier: EscalationTier
  recipient_persona: string
  recipient_count: number
  /** True when an unstaffed rung was climbed. Worth showing: it explains why a
   *  Senior Manager is looking at a campus-level SLA. */
  climbed: boolean
  /** True when nobody at any rung reaches this college — an ops gap in the
   *  assignment tables, not a program problem. */
  unrouted: boolean
}

export interface ProgramRisk {
  program_id: string
  program_name: string
  program_type: string
  college_id: string
  college_name: string
  stage: string
  score: string
  band: RiskBand
  deployments: DeploymentRisk[]
  escalations: Escalation[]
  unmeasured_metrics: string[]
}

export interface AlertFeed {
  as_of: string
  period_start: string
  period_end: string
  audience: string
  programs: ProgramRisk[]
  program_count: number
  deployment_count: number
  escalation_count: number
  band_counts: Record<string, number>
}

export interface SlaRule {
  code: string
  metric: string
  comparison: string
  threshold: string
  tier: EscalationTier | null
  severity: AnomalySeverity
  program_type: string | null
  rationale: string
  explanation: string
  is_measured: boolean
}

export interface SlaRules {
  rules: SlaRule[]
  measured_metrics: string[]
  unmeasured_metrics: string[]
}

export interface AlertFeedParams {
  programId?: string
  collegeId?: string
  periodStart?: string
  periodEnd?: string
}

export const alertKeys = {
  all: ['monitoring'] as const,
  feed: (params: AlertFeedParams = {}) => ['monitoring', 'alerts', params] as const,
  rules: () => ['monitoring', 'rules'] as const,
}

/**
 * The internal alert feed for everything the signed-in user reaches.
 *
 * `as_of` is deliberately NOT sent. Leaving it unset means the server stamps the
 * observation instant, which is what a live dashboard wants; passing a client
 * clock would make two people looking at the same screen disagree about which
 * days had elapsed. The endpoint accepts it for replaying a past run.
 */
export function fetchAlertFeed(params: AlertFeedParams = {}): Promise<AlertFeed> {
  const query = new URLSearchParams()
  if (params.programId) query.set('program_id', params.programId)
  if (params.collegeId) query.set('college_id', params.collegeId)
  if (params.periodStart) query.set('period_start', params.periodStart)
  if (params.periodEnd) query.set('period_end', params.periodEnd)
  const qs = query.toString()
  return apiGet<AlertFeed>(`/monitoring/alerts${qs ? `?${qs}` : ''}`)
}

/** The shipped SLA rule table — the escalation policy, as data. */
export function fetchSlaRules(): Promise<SlaRules> {
  return apiGet<SlaRules>('/monitoring/rules')
}

/** Highest first, which is the order the feed is worked in. */
export const BANDS: readonly RiskBand[] = ['critical', 'high', 'medium', 'low']

export const BAND_LABEL: Record<RiskBand, string> = {
  critical: 'Critical',
  high: 'High',
  medium: 'Medium',
  low: 'Low',
}

export const SEVERITY_LABEL: Record<AnomalySeverity, string> = {
  critical: 'Critical',
  warning: 'Warning',
  info: 'Note',
}

export const TIER_LABEL: Record<EscalationTier, string> = {
  lde_executive: 'LDE Executive',
  manager: 'Manager',
  senior_manager: 'Senior Manager',
}

/**
 * A code like `attendance_unmarked_days` as a sentence-cased label.
 *
 * Derived rather than tabulated: `AnomalyCode` and `SlaCode` are stable
 * identifiers that a migration adds to, and a hand-written map would render a
 * new one as blank rather than as an ugly-but-readable label.
 */
export function codeLabel(code: string): string {
  const words = code.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** Sortable numeric view of a `Decimal` string. For ordering only — never maths. */
export function scoreOf(value: string): number {
  const n = Number(value)
  return Number.isFinite(n) ? n : 0
}
