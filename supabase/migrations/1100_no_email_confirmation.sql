-- =============================================================================
-- byteXL Ops Intelligence Platform — 1100 — password-only auth, no email step
-- =============================================================================
-- Signup and sign-in are email + password. No confirmation link, no OTP, no
-- magic link. Verification is deferred to a later phase; when it comes back it
-- comes back as a deliberate migration that drops the trigger below, not as a
-- dashboard toggle someone flips without a record.
--
-- WHY THIS IS IN THE DATABASE AND NOT JUST A DASHBOARD SETTING.
-- "Confirm email" lives in GoTrue's config (Dashboard -> Authentication ->
-- Sign In / Providers -> Email), which is project state with no representation
-- in this repo. Two problems with leaving it there alone:
--
--   1. It is invisible. Nothing in a clone of this repo tells you the auth
--      posture, and nothing breaks loudly if the toggle drifts back on.
--   2. It strands accounts. With the toggle on, GoTrue inserts the user, returns
--      NO session, and every later signInWithPassword fails "Email not
--      confirmed" until someone clicks a link that may never arrive. At the time
--      this migration was written the project had exactly one user and they were
--      in precisely that state.
--
-- So the rule is written where the repo can see it. Confirming the row on INSERT
-- means the account is usable the instant it exists, whatever GoTrue's config
-- says — the dashboard toggle then only controls whether a pointless email goes
-- out, not whether anyone can get in.
--
-- SCOPE, deliberately narrow:
--   * BEFORE INSERT only. An email CHANGE on an existing account still goes
--     through GoTrue's normal flow — this migration is about the signup gate,
--     not a blanket "trust every address forever".
--   * email_confirmed_at only. `confirmed_at` is a GENERATED column in current
--     Supabase (least of email/phone) and assigning to it raises. Phone is
--     untouched; this project does not use it.
--
-- Trigger on an auth schema table, same as `on_auth_user_created` in 0200. That
-- is an established pattern here, not a new liberty.
-- =============================================================================

-- --- New signups: confirmed on arrival ---------------------------------------

create or replace function public.auto_confirm_email()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- coalesce, not an unconditional now(): an insert that already carries a
  -- confirmation timestamp (a restore, an admin-created user) keeps its own.
  if new.email_confirmed_at is null then
    new.email_confirmed_at := coalesce(new.created_at, now());
  end if;

  -- Leave no pending confirmation state behind. A stored token on an
  -- already-confirmed row is a live credential nobody is going to use, and it
  -- makes the row read as "awaiting confirmation" to anyone inspecting it.
  new.confirmation_token  := '';
  new.confirmation_sent_at := null;

  return new;
end;
$$;

comment on function public.auto_confirm_email() is
  'Email confirmation is off for this project (deferred feature). Confirms auth.users rows on INSERT so signup yields an immediately usable password account. Drop this trigger to re-enable verification.';

-- Idempotent: this file is applied once by run_tests.py, but a trigger on an
-- auth-schema table is the kind of thing that gets re-run by hand during setup.
drop trigger if exists auto_confirm_email_on_signup on auth.users;

create trigger auto_confirm_email_on_signup
  before insert on auth.users
  for each row execute function public.auto_confirm_email();

-- --- Existing accounts: unstick anyone already stranded -----------------------
-- One-time backfill for users created while confirmation was on. Without it the
-- trigger above helps only future signups and the existing account stays locked
-- out with no way in short of a dashboard visit.

update auth.users
set email_confirmed_at   = coalesce(email_confirmed_at, created_at, now()),
    confirmation_token   = '',
    confirmation_sent_at = null
where email_confirmed_at is null;
