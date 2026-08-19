import { describe, expect, it } from 'vitest'
import { errorMessage } from './supabase'

/* =============================================================================
   PostgREST errors, translated for an Ops executive.

   The translations key on CONSTRAINT NAME, not on Postgres' prose, because the
   name is declared in a migration and the prose varies by server version. That
   only holds if the substrings tested here are the ones the migrations actually
   declare — so each case below names a real constraint, spelled the way
   Postgres reports it, including the `_key` / `_ck` suffixes it appends.
   ============================================================================= */

/** A PostgREST error as supabase-js hands it over. */
function pgError(code: string, message: string, details = ''): unknown {
  return { code, message, details, hint: '' }
}

describe('errorMessage — nothing to translate', () => {
  it.each([
    ['undefined', undefined],
    ['null', null],
    ['an empty string', ''],
    ['0', 0],
  ])('says so plainly for %s', (_label, value) => {
    expect(errorMessage(value)).toBe('Unknown error')
  })

  it('falls back to the raw message when the code is not one it knows', () => {
    expect(errorMessage(pgError('08006', 'connection failure'))).toBe('connection failure')
  })

  it('falls back to details when there is no message', () => {
    expect(errorMessage({ details: 'Key (id)=(7) is not present in table.' })).toBe(
      'Key (id)=(7) is not present in table.',
    )
  })

  it('has a last resort for an error object carrying neither', () => {
    expect(errorMessage({})).toBe('Something went wrong')
  })

  it('DROPS a bare string error rather than showing it', () => {
    // Known lossy edge: a thrown string has no `.message`, so it lands on the
    // generic sentence. Pinned rather than fixed — the fix is in the source file,
    // which this suite does not own.
    expect(errorMessage('boom')).toBe('Something went wrong')
  })
})

describe('errorMessage — 42501, the RLS wall (R5)', () => {
  it('reads a 42501 as a role/assignment problem, not as a bug', () => {
    const said = errorMessage(pgError('42501', 'permission denied for table pnl'))
    expect(said).toContain('Your role does not permit that action')
    expect(said).toContain('assignments')
    expect(said).not.toContain('permission denied for table')
  })

  it('catches an RLS refusal reported without the 42501 code', () => {
    const said = errorMessage({
      message: 'new row violates row-level security policy for table "remuneration_sheets"',
    })
    expect(said).toContain('Your role does not permit that action')
  })

  it('does not translate an unrelated error that merely mentions security', () => {
    expect(errorMessage({ message: 'security definer function failed' })).toBe(
      'security definer function failed',
    )
  })
})

describe('errorMessage — 23505, duplicate key', () => {
  it('names the deployment the user was actually trying to create', () => {
    const said = errorMessage(
      pgError(
        '23505',
        'duplicate key value violates unique constraint "deployments_trainer_batch_key"',
      ),
    )
    expect(said).toContain('already deployed to that batch')
    expect(said).toContain('Edit the existing deployment')
  })

  it('reads the constraint out of `details` when the message does not carry it', () => {
    const said = errorMessage(
      pgError('23505', 'duplicate key value violates unique constraint', 'trainers_pan_key'),
    )
    expect(said).toContain('PAN is the identity key')
  })

  it('explains a repeated invoice number as a system-generated value (§6)', () => {
    const said = errorMessage(
      pgError('23505', 'duplicate key value violates unique constraint "invoices_number_key"'),
    )
    expect(said).toContain('already been issued')
    expect(said).toContain('cannot repeat')
  })

  it('has a generic sentence for a constraint it has not been taught', () => {
    const said = errorMessage(
      pgError('23505', 'duplicate key value violates unique constraint "colleges_name_key"'),
    )
    expect(said).toBe(
      'That record already exists. Open the existing one and edit it rather than adding a duplicate.',
    )
  })

  it('prefers the deployment translation when a message names two constraints', () => {
    // First match wins, and the order in the source is deployments → invoice →
    // PAN. Pinned so a reordering is a failing test rather than a silent change
    // of sentence.
    const said = errorMessage(
      pgError('23505', 'deployments_trainer_batch_key', 'invoice_no already present'),
    )
    expect(said).toContain('already deployed to that batch')
  })

  it('never leaks the raw constraint name to the reader', () => {
    for (const constraint of [
      'deployments_trainer_batch_key',
      'invoices_number_key',
      'trainers_pan_key',
      'colleges_name_key',
    ]) {
      const said = errorMessage(pgError('23505', `duplicate key ... "${constraint}"`))
      expect(said).not.toContain(constraint)
      expect(said).not.toContain('duplicate key')
    }
  })
})

describe('errorMessage — 23514, check violation', () => {
  it.each([
    ['ifsc_length', 'IFSC must be exactly 11 characters, e.g. SBIN0001234.'],
    ['ifsc_upper', 'IFSC must be uppercase.'],
    [
      'account_digits',
      'Bank account number must contain digits only — no spaces or dashes.',
    ],
    [
      'passout_year',
      'Passout year looks wrong. Enter the full four-digit graduating year, e.g. 2027.',
    ],
    ['date_order', 'The end date cannot be before the start date.'],
  ])('translates the %s constraint into an instruction', (constraint, expected) => {
    const said = errorMessage(
      pgError('23514', `new row violates check constraint "trainers_${constraint}_ck"`),
    )
    expect(said).toBe(expected)
  })

  it('reads the constraint out of `details` too', () => {
    expect(
      errorMessage(pgError('23514', 'new row violates check constraint', 'ifsc_length_ck')),
    ).toContain('exactly 11 characters')
  })

  it('has a generic sentence for a check it has not been taught', () => {
    expect(
      errorMessage(pgError('23514', 'new row violates check constraint "programs_seats_ck"')),
    ).toBe('That value failed a validation rule in the database. Check the highlighted fields.')
  })

  it('states the §7 IFSC and PAN shapes the same way the gates do', () => {
    // §7 blocks on PAN 10 chars / IFSC 11 chars. The sentence a user reads must
    // agree with the gate that stopped them.
    expect(errorMessage(pgError('23514', 'ifsc_length_ck'))).toContain('11 characters')
  })

  it('does not confuse a 23514 with a 23505', () => {
    const check = errorMessage(pgError('23514', 'trainers_pan_ck'))
    expect(check).not.toContain('already exists')
    expect(check).toContain('validation rule')
  })
})
