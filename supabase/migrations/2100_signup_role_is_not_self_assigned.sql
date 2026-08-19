-- =============================================================================
-- 2100 — a signup may not choose its own persona
-- =============================================================================
-- SEVERITY: critical. Closes a live path from one unauthenticated HTTP request
-- to 1,026 trainer PII rows and 1,025 rewritable bank rails.
--
-- THE CHAIN, AS REPRODUCED AGAINST THIS DATABASE
-- ----------------------------------------------
-- 1. `handle_new_user` (0200) read the persona from
--    `new.raw_user_meta_data ->> 'role'`. That object is the `data` field of a
--    public `POST /auth/v1/signup` body — it is chosen by whoever is signing
--    up, not by us.
-- 2. `disable_signup` is false, and 1100 auto-confirms every `auth.users` row
--    on INSERT, so no mailbox is required. Signup to session is one request.
-- 3. Three policies carry the commercials wall but NO reach conjunct, so they
--    grant on persona alone:
--       trainers_sourcing_all                  (0400)
--       trainer_bank_accounts_commercials_all  (1400)
--       erm_sync_tasks_sourcing_all            (1900)
--    All three are `for all`, so the gap is a write, not just a read.
--
-- Measured end to end, in a rolled-back transaction, from a row identical to
-- the one a public signup creates with {"role":"manager"}:
--
--     is_internal, can_see_commercials, reach = (true, true, 0)
--     trainers visible                        = 1026   (PAN, email, phone)
--     bank accounts visible                   = 1025   (account no., IFSC)
--     UPDATE all rails rowcount               = 1025
--
-- WHY EACH PIECE LOOKED SAFE ON ITS OWN
-- -------------------------------------
-- 0200's header says `raw_user_meta_data` is attacker-controlled — and then
-- defends `is_admin` against it, four lines above the code that trusts `role`.
-- `LoginPage.tsx` argues open signup is safe because "a fresh Manager signup
-- sees an empty console until an admin assigns them colleges". That is true for
-- every reach-scoped table and false for exactly those three. Each decision was
-- individually reasonable; together they cancelled each other's rationale.
--
-- WHAT THIS MIGRATION DOES
-- ------------------------
-- `handle_new_user` stops reading the requested role. Every new account lands
-- on 'trainer', which since 1800 matches no policy on any table — the
-- deny-by-default sentinel CLAUDE.md §4 already describes. An admin then sets
-- the real persona through the guarded UPDATE path in 0200, which is the only
-- place §4 ever said persona should come from.
--
-- This is the smaller of the two available fixes and it restores the mitigation
-- the frontend already claims. It is NOT a substitute for adding reach
-- conjuncts where they belong — but note `trainers_sourcing_all` is
-- deliberately reach-free (0400:320-334: sourcing precedes deployment, so a
-- reach conjunct would break the sourcing workflow). That policy's correct fix
-- is this migration, not a conjunct. `trainer_bank_accounts` and
-- `erm_sync_tasks` should still gain one; that is a separate change with its
-- own testing, deliberately not bundled here.
--
-- `full_name` is still taken from metadata. It is a display string that grants
-- nothing, and refusing it would make signup worse for no security gain.
-- =============================================================================

create or replace function public.handle_new_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  -- No `requested_role` variable any more, on purpose. A reader looking for
  -- "where does the persona come from" should find nothing here to read, rather
  -- than a line that looks like it validates something.
  --
  -- 'trainer' is not a fallback for a malformed value — it is the ONLY value
  -- this function assigns. Since 1800 dropped all eighteen trainer policies it
  -- grants precisely nothing, which is what a brand-new unvetted account should
  -- have. Promotion is an admin action, audited, through
  -- profiles_guard_privileged_columns.
  insert into public.profiles (id, role, full_name)
  values (
    new.id,
    'trainer',
    nullif(new.raw_user_meta_data ->> 'full_name', '')
  )
  on conflict (id) do nothing;

  return new;
end;
$$;

comment on function public.handle_new_user() is
  'Creates the profile for a new auth.users row. Assigns the deny-by-default '
  '''trainer'' sentinel ALWAYS — the persona is never read from '
  'raw_user_meta_data, which is chosen by whoever is signing up. See migration '
  '2100 for the reproduction this closes. Persona is set only by an admin, '
  'through the guarded UPDATE path in 0200.';

-- --- Remediate accounts created through the hole ------------------------------
-- Any internal persona that was self-assigned and never given reach is
-- indistinguishable from an exploit account, and harmless to demote: with zero
-- assignments it can already reach no college. Genuinely provisioned staff have
-- at least one assignment row and are left alone. is_admin is never touched —
-- 2000's guard owns that, and this must not fight it.
--
-- Reported so the run is not silent about what it changed.
do $$
declare
  demoted integer;
begin
  with unprovisioned as (
    select p.id
    from public.profiles p
    where p.role in ('senior_manager', 'manager', 'lde_executive')
      and p.is_admin is not true
      and not exists (select 1 from public.user_college_assignments a where a.user_id = p.id)
      and not exists (select 1 from public.user_cluster_assignments c where c.user_id = p.id)
  )
  update public.profiles p
     set role = 'trainer'
    from unprovisioned u
   where p.id = u.id;

  get diagnostics demoted = row_count;
  raise notice '2100: demoted % unprovisioned internal profile(s) to the trainer sentinel', demoted;
end;
$$;
