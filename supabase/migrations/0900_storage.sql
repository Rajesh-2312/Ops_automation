-- =============================================================================
-- byteXL Ops Intelligence Platform — 0900 — document storage
-- =============================================================================
-- MOUs, POs, signed work orders and report packs live in Supabase Storage. One
-- private bucket, never public, with access derived from the object PATH.
--
-- PATH CONVENTION — the policies below depend on it, so it is not optional:
--
--     colleges/<college_id>/<filename>     MOU, PO
--     trainers/<trainer_id>/<filename>     work order, signed work order
--     programs/<program_id>/<filename>     governance reports, misc
--
-- storage.foldername(name) returns the path segments before the filename, so
-- segment 1 is the entity type and segment 2 is the entity UUID. Every policy
-- pins BOTH: matching only the UUID would let a file dropped at
-- trainers/<college_id>/ be read by whoever reaches that college.
--
-- WHY THE TRAINER FOLDER IS BEHIND THE COMMERCIALS WALL
-- -----------------------------------------------------
-- It holds signed work orders, and a signed work order states the rate. §4 gives
-- the LDE Executive no commercials, so they get no access to trainers/ — even
-- for a trainer standing in their own classroom, whose deployment and attendance
-- they administer daily. That asymmetry is the point: the wall is around the
-- NUMBER, not around the person.
-- =============================================================================

insert into storage.buckets (id, name, public)
values ('documents', 'documents', false)
on conflict (id) do nothing;

-- --- Path parsing helper ------------------------------------------------------
-- The reach helpers take uuid; path segments are text. A direct `::uuid` cast
-- is not safe here: one hand-uploaded object at `colleges/draft/mou.pdf` would
-- raise 22P02 during policy evaluation and fail the ENTIRE listing query for
-- everyone, turning a filename typo into an outage. This returns NULL instead,
-- and NULL flows into can_reach_*() as "no", which is the correct answer for an
-- object that is not where the convention says it should be.

create or replace function public.try_uuid(p_text text)
returns uuid
language plpgsql
immutable
set search_path = ''
as $$
begin
  return p_text::uuid;
exception when others then
  return null;
end;
$$;

comment on function public.try_uuid(text) is
  'Cast text to uuid, or NULL if it is not one. Used by the storage path policies so a malformed object path denies access instead of erroring the query.';

revoke execute on function public.try_uuid(text) from public, anon;
grant  execute on function public.try_uuid(text) to authenticated;

-- storage.objects is a shared table that other features may also have policies
-- on, so drop ours by name before creating them rather than colliding.
drop policy if exists documents_admin_all              on storage.objects;
drop policy if exists documents_internal_college_rw    on storage.objects;
drop policy if exists documents_internal_program_rw    on storage.objects;
drop policy if exists documents_commercials_trainer_rw on storage.objects;
drop policy if exists documents_trainer_select_own     on storage.objects;

-- --- ADMIN --------------------------------------------------------------------
-- Full control of every document in the bucket. Unlike the money tables in 0700,
-- storage DOES get an admin override: someone has to be able to fix a
-- misfiled object, and a file at the wrong path is unreachable by every
-- scoped policy below, including the one that should own it.

create policy documents_admin_all on storage.objects
  for all to authenticated
  using (bucket_id = 'documents' and public.is_admin())
  with check (bucket_id = 'documents' and public.is_admin());

-- --- INTERNAL: colleges/<college_id>/ ----------------------------------------
-- Contract documents for a college the caller reaches. All three internal
-- personas: an MOU and a PO are contract state, not commercials, and the campus
-- executive chasing a signature needs the document.

create policy documents_internal_college_rw on storage.objects
  for all to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'colleges'
    and public.is_internal()
    and public.can_reach_college(public.try_uuid((storage.foldername(name))[2]))
  )
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'colleges'
    and public.is_internal()
    and public.can_reach_college(public.try_uuid((storage.foldername(name))[2]))
  );

-- --- INTERNAL: programs/<program_id>/ ----------------------------------------
-- Delivery paperwork: governance packs, timetables, assessment reports.

create policy documents_internal_program_rw on storage.objects
  for all to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'programs'
    and public.is_internal()
    and public.can_reach_program(public.try_uuid((storage.foldername(name))[2]))
  )
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'programs'
    and public.is_internal()
    and public.can_reach_program(public.try_uuid((storage.foldername(name))[2]))
  );

-- --- COMMERCIALS: trainers/<trainer_id>/ -------------------------------------
-- Senior Manager and Manager only — see the header. Scoped by persona rather
-- than by can_reach_trainer(), and that is a considered trade: reach to a
-- trainer is derived from their deployments, so a trainer who has been sourced
-- and contracted but not yet deployed is reachable by NOBODY, and the work order
-- could never be filed in the first place. Since contracting is exactly the
-- window in which this folder is written, gating it on deployment would make the
-- feature unusable at the only moment it is needed.

create policy documents_commercials_trainer_rw on storage.objects
  for all to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'trainers'
    and public.can_see_commercials()
  )
  with check (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'trainers'
    and public.can_see_commercials()
  );

-- --- TRAINER ------------------------------------------------------------------
-- §4 grants trainers their own work order and ERM/ZOHO status. Read-only, and
-- only inside their own folder — a trainer must never be able to list another
-- trainer's work order, nor reach college contract documents.
--
-- No INSERT policy: work orders are uploaded by HR / the contracting manager. A
-- trainer who could write into their own folder could replace a signed document
-- with a differently-worded one, and the §7 gate that compares the engagement
-- rate against "the rate in the signed WO" would then be comparing it against
-- the trainer's own file.

create policy documents_trainer_select_own on storage.objects
  for select to authenticated
  using (
    bucket_id = 'documents'
    and (storage.foldername(name))[1] = 'trainers'
    and public.my_trainer_id() is not null
    and (storage.foldername(name))[2] = public.my_trainer_id()::text
  );

-- --- COLLEGE ------------------------------------------------------------------
-- No policy. §4 gives the college published artifacts — served as URLs from
-- governance_reports — not raw access to the contract document store. Deny by
-- default; revisit only if OPS asks for colleges to self-serve their MOU/PO
-- copies, which would be one path-scoped SELECT policy on colleges/<my id>/.
