"""SEC-02 / SEC-03 / F1 — commercial policies that carry the wall but not the scope.

`app/core/security.py` states the rule these policies break, in its own words:

    Every money policy in `0700_finance.sql` has the shape::

        using (public.can_see_commercials() and public.can_reach_<scope>(...))
               ^ the wall                        ^ the scope

    Both conjuncts are load-bearing. `require_commercials()` alone lets a Manager
    read another cluster's P&L.

Three policies shipped with the wall alone:

  * `trainer_bank_accounts_commercials_all`  (1400) — `for all`, account number + IFSC
  * `trainers_sourcing_all`                  (0400) — `for all`, PAN + email + phone
  * `documents_commercials_trainer_rw`       (0900) — `for all`, signed work-order PDFs

1400 argues the missing conjunct deliberately, and the argument is real: rails
are filed during onboarding, before any deployment exists, so plain reachability
is false of everybody and a reach-gated policy could never file the first account
number. But that argument is about INSERT, and 1400's own cost statement is about
SELECT — "a Manager READS the rails of a trainer deployed only at colleges they do
not cover". The policies are `for all`, so they also grant UPDATE, and nothing in
either migration argues for cross-tenant UPDATE of a payment rail.

WHERE EACH ONE STANDS
---------------------
* **`trainer_bank_accounts`** — CLOSED by 2200, applied. Its policy now carries
  `can_reach_trainer(trainer_id)`.
* **`trainers`** — still persona-only, deliberately: 2200 argues sourcing precedes
  deployment, so a reach conjunct would hide the roster from the people whose job
  is to build it. Still xfail below, because the cost is real even if the trade is
  accepted.
* **`documents_commercials_trainer_rw`** — fix written in
  `supabase/migrations/2400_truncate_grant_and_storage_reach.sql` and verified
  against this database in a rolled-back transaction, but **not applied** (that is
  the orchestrator's move, with the owner present). Its probes are therefore
  `xfail(strict=True)`: they describe the live database today, and the moment 2400
  lands they XPASS, strict turns that into a hard failure, and the failure message
  is the instruction to delete the marker.

THE CARVE-OUT, AND WHY ONE PROBE BELOW LOOKS LIKE A HOLE
--------------------------------------------------------
2200's `can_reach_trainer()` is not plain reachability. It is "reachable, **or**
not yet deployed anywhere", because onboarding collects bank details before the
first deployment exists. That carve-out is load-bearing for real work and it has
a real cost: an undeployed trainer's rails are readable and writable by any
Manager or Senior Manager nationally. `test_the_undeployed_carve_out_is_the_price
_of_the_onboarding_path` pins that cost so it stays visible rather than becoming
folklore. It is an open question for the owner, not a defect to patch quietly.

The probes use `mgr_no_reach` / `sm_no_reach`: real seeded accounts with ZERO rows
in `user_college_assignments` and ZERO in `user_cluster_assignments`. They reach
no college at all, which is the cleanest possible statement of the finding — there
is no tenant these rows could legitimately belong to.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.security.conftest import (
    COLLEGE_DEMO_INSTITUTE,
    COLLEGE_MALINENI,
    TRAINER_ANITHA,
    TRAINER_VEMA,
    USERS,
    scalar,
)

psycopg = pytest.importorskip("psycopg", reason="psycopg is needed for RLS probes")

pytestmark = [pytest.mark.rls, pytest.mark.integration]


@pytest.fixture
def _assert_no_reach(as_user):
    """Guard the premise: these accounts really do reach nothing."""

    #: The persona each no-reach subject must hold for these probes to mean
    #: anything. Provisioned per-transaction rather than assumed to be sitting in
    #: the database — see `ensure_persona` in conftest for why migration 2100
    #: made that assumption false.
    PERSONA = {"mgr_no_reach": "manager", "sm_no_reach": "senior_manager"}

    def _check(key: str) -> None:
        with as_user(USERS[key], ensure_persona=PERSONA[key]) as cur:
            reach = scalar(cur, "select count(*) from public.my_college_ids()")
            assert reach == 0, f"fixture drift: {key} now reaches {reach} colleges"
            assert scalar(cur, "select public.can_see_commercials()") is True

    return _check


# --- in-transaction fixtures the probes build for themselves ------------------
#
# Two of the probes below need subjects the seeded demo estate does not contain:
# a trainer who is genuinely DEPLOYED somewhere and has no bank row yet, and an
# object inside the `documents` bucket (which is empty in this environment). Both
# are created here, as the superuser connection, inside the transaction the
# `as_user` fixture rolls back. Nothing persists — see the safety contract at the
# top of conftest.
#
# Seeding rather than re-pointing at demo data is the honest choice for the rails
# probe in particular. Every deployed trainer in the demo estate already HAS a
# `trainer_bank_accounts` row, so an INSERT probe against one of them would be
# swallowed by the primary key and pass for a reason that has nothing to do with
# the policy under test.


def _seed_deployed_trainer(cur: Any, *, pan: str) -> str:
    """A trainer deployed at Demo Institute, with no payment rail yet.

    Deployed, so 2200's "not yet deployed anywhere" carve-out does NOT apply to
    them — which is the whole point: this is the subject that separates "the
    policy denies" from "the carve-out permits".

    Drops to the session role to write, then restores `authenticated`. The
    `request.jwt.claims` GUC is transaction-local and survives the role change,
    so impersonation resumes exactly as it was.
    """
    cur.execute("reset role")
    cur.execute(
        """
        insert into public.trainers (pan, full_name, type)
        values (%s, 'Security Probe Educator', 'freelancer')
        returning id
        """,
        (pan,),
    )
    trainer_id = str(cur.fetchone()[0])
    cur.execute(
        """
        insert into public.deployments (trainer_id, batch_id)
        select %s, b.id
          from public.batches b
          join public.programs p on p.id = b.program_id
         where p.college_id = %s
         limit 1
        """,
        (trainer_id, COLLEGE_DEMO_INSTITUTE),
    )
    assert cur.rowcount == 1, "fixture drift: no batch at Demo Institute to deploy onto"
    cur.execute("set local role authenticated")
    return trainer_id


def _seed_document(cur: Any, path: str) -> str:
    """Put one object in the `documents` bucket, inside the rolled-back transaction.

    F1 could not be demonstrated during the security review because the bucket is
    empty here, which made it a policy-shape finding that would start leaking on
    the first upload. Seeding the object makes the proof about the policy and
    stops it waiting on the bucket ever being used.
    """
    cur.execute("reset role")
    cur.execute(
        "insert into storage.objects (bucket_id, name, metadata) "
        "values ('documents', %s, '{}'::jsonb)",
        (path,),
    )
    cur.execute("set local role authenticated")
    return path


# --- SEC-02: trainer_bank_accounts -------------------------------------------


# SEC-02 CLOSED by migration 2200: the policy now carries
# `can_reach_trainer(trainer_id)` alongside `can_see_commercials()`. Verified
# live before applying — a Senior Manager scoped to Malineni went from 5 rails
# to 0, which is the cross-tenant case.
@pytest.mark.parametrize("who", ["mgr_no_reach", "sm_no_reach"])
def test_unassigned_commercials_persona_reads_no_payment_rails(
    as_user, _assert_no_reach, who: str
) -> None:
    """A commercials persona reaching no college must read no bank account."""
    _assert_no_reach(who)
    with as_user(USERS[who]) as cur:
        visible = scalar(cur, "select count(*) from public.trainer_bank_accounts")
        assert visible == 0, (
            f"{who} reaches 0 colleges but sees {visible} payment rails "
            "(account number + IFSC + beneficiary name) across every tenant."
        )


# SEC-03 CLOSED by 2200. The `with check` clause carries the same conjunct as
# `using`, so the write half is closed too — that mattered more than the read:
# a rewritable rail is a redirected payment.
def test_unassigned_manager_cannot_rewrite_a_payment_rail(as_user, _assert_no_reach) -> None:
    """Repointing a trainer's bank account is the highest-value write in the schema."""
    _assert_no_reach("mgr_no_reach")
    with as_user(USERS["mgr_no_reach"]) as cur:
        cur.execute(
            """
            update public.trainer_bank_accounts
               set bank_account_number = '99999999999',
                   ifsc                = 'ATTK0000001',
                   account_name        = 'Attacker'
             where trainer_id = %s
            """,
            (TRAINER_ANITHA,),
        )
        assert cur.rowcount == 0, (
            f"an unassigned Manager rewrote {cur.rowcount} payment rail(s). "
            "Between approval and release this silently redirects a payout."
        )


# SEC-03 CLOSED by 2200 — `with check` covers INSERT as well as UPDATE.
def test_unassigned_manager_cannot_file_a_rail_for_an_unreachable_trainer(
    as_user, _assert_no_reach
) -> None:
    """The subject has to be a trainer who is genuinely DEPLOYED somewhere.

    This probe previously used `TRAINER_VEMA`, and it failed — honestly. Vema has
    no deployment row in the current dataset, so 2200's "reachable, **or** not yet
    deployed anywhere" carve-out legitimately permits the insert. The probe was
    not detecting a hole in the fix; it was pointed at the documented carve-out
    and reporting it as one. That cost is pinned on its own terms in
    `test_the_undeployed_carve_out_is_the_price_of_the_onboarding_path` below.

    Every deployed trainer in the demo estate already has a rail, so the subject
    is seeded here rather than borrowed: without that, `on conflict do nothing`
    or a primary-key violation would decide the outcome instead of the policy.

    A denied INSERT *raises* (RLS `with check`, SQLSTATE 42501) rather than
    returning rowcount 0 the way a filtered UPDATE does, so the refusal is
    asserted by SQLSTATE and not by rowcount.
    """
    _assert_no_reach("mgr_no_reach")
    with as_user(USERS["mgr_no_reach"]) as cur:
        trainer_id = _seed_deployed_trainer(cur, pan="PROBEA111A")

        assert scalar(cur, "select public.can_reach_trainer(%s)", (trainer_id,)) is False, (
            "fixture drift: the seeded trainer is reachable by an account with zero "
            "assignments, so this probe would prove nothing"
        )

        cur.execute("savepoint rail_insert_probe")
        refused: str | None = None
        try:
            cur.execute(
                """
                insert into public.trainer_bank_accounts
                    (trainer_id, bank_account_number, ifsc, bank_name, account_name)
                values (%s, '11112222333', 'ATTK0000001', 'Attacker Bank', 'Attacker')
                """,
                (trainer_id,),
            )
        except psycopg.errors.InsufficientPrivilege as exc:
            refused = exc.sqlstate
            cur.execute("rollback to savepoint rail_insert_probe")

        assert refused == "42501", (
            "an unassigned Manager filed rails for a trainer deployed only at a "
            "college they do not cover. The `with check` conjunct is missing."
        )


@pytest.mark.xfail(
    strict=True,
    reason="ACCEPTED COST, NOT A REGRESSION: 2200's can_reach_trainer() returns TRUE "
    "for a trainer deployed nowhere, so any Manager or Senior Manager nationally "
    "can read and write an undeployed trainer's payment rails. The carve-out exists "
    "because onboarding files bank details BEFORE the first deployment. Narrowing it "
    "needs a schema decision (owning college / creator scoping / time-box) and is the "
    "owner's call — see the SEC-04/F1 report. This XPASSes if the predicate is ever "
    "narrowed, which is the signal to update it.",
)
def test_the_undeployed_carve_out_is_the_price_of_the_onboarding_path(
    as_user, _assert_no_reach
) -> None:
    """Pin the residual so it stays a decision rather than becoming folklore.

    `TRAINER_VEMA` is engaged at no college, so `can_reach_trainer()` is TRUE of
    him for everybody — including an account with zero assignments, which is
    exactly the shape SEC-02 was about. The insert below succeeds today.

    That is not 2200 failing. It is 2200 doing what it says: the alternative
    breaks onboarding, which is real work that happens every week. What this test
    refuses to let happen is the cost being forgotten — the rails of every trainer
    between "sourced" and "deployed" are national, and the window is exactly the
    one in which the account number is fresh and unverified.
    """
    _assert_no_reach("mgr_no_reach")
    with as_user(USERS["mgr_no_reach"]) as cur:
        assert (
            scalar(
                cur,
                "select count(*) from public.deployments where trainer_id = %s",
                (TRAINER_VEMA,),
            )
            == 0
        ), "fixture drift: TRAINER_VEMA is now deployed, so this no longer probes the carve-out"

        cur.execute("savepoint carve_out_probe")
        refused: str | None = None
        try:
            cur.execute(
                """
                insert into public.trainer_bank_accounts
                    (trainer_id, bank_account_number, ifsc, bank_name, account_name)
                values (%s, '11112222333', 'ATTK0000001', 'Attacker Bank', 'Attacker')
                """,
                (TRAINER_VEMA,),
            )
        except psycopg.errors.InsufficientPrivilege as exc:
            refused = exc.sqlstate
            cur.execute("rollback to savepoint carve_out_probe")

        assert refused == "42501", (
            "an unassigned Manager filed payment rails for an undeployed trainer. "
            "Permitted today by the onboarding carve-out in can_reach_trainer()."
        )


# --- F1: the storage `trainers/` folder ---------------------------------------
#
# `documents_commercials_trainer_rw` (0900:124) is the third policy in this
# family and the one 2200 deliberately left: object policies key off a PATH
# PREFIX rather than a column, so the predicate is a different shape. 2400 is
# that change. It holds signed work orders, and a signed work order states the
# rate — which is why 0900 put the folder behind the commercials wall to begin
# with. What it never had was the scope.


# F1 CLOSED by migration 2400 (applied 2026-08-19): the trainers/ storage folder
# now carries a reach conjunct alongside can_see_commercials().
def test_unassigned_manager_cannot_read_an_unreachable_trainers_work_order(
    as_user, _assert_no_reach
) -> None:
    """A signed work order states the rate. Reach has to gate it, not persona alone."""
    _assert_no_reach("mgr_no_reach")
    with as_user(USERS["mgr_no_reach"]) as cur:
        trainer_id = _seed_deployed_trainer(cur, pan="PROBEB222B")
        path = _seed_document(cur, f"trainers/{trainer_id}/signed_work_order.pdf")

        visible = scalar(cur, "select count(*) from storage.objects where name = %s", (path,))
        assert visible == 0, (
            "a Manager reaching zero colleges read the signed work order of a trainer "
            "deployed at a college they do not cover."
        )


# F1 CLOSED by migration 2400 (applied 2026-08-19): the trainers/ storage
# folder now carries a reach conjunct alongside can_see_commercials().
def test_unassigned_manager_cannot_write_into_an_unreachable_trainers_folder(
    as_user, _assert_no_reach
) -> None:
    """§7 blocks a payout unless the engagement rate matches "the rate in the signed WO".

    If the signed WO can be replaced by someone outside the trainer's tenant, that
    gate is comparing the rate against a document the comparison's adversary wrote.
    """
    _assert_no_reach("mgr_no_reach")
    with as_user(USERS["mgr_no_reach"]) as cur:
        trainer_id = _seed_deployed_trainer(cur, pan="PROBEC333C")

        cur.execute("savepoint object_insert_probe")
        refused: str | None = None
        try:
            cur.execute(
                "insert into storage.objects (bucket_id, name, metadata) "
                "values ('documents', %s, '{}'::jsonb)",
                (f"trainers/{trainer_id}/substituted_work_order.pdf",),
            )
        except psycopg.errors.InsufficientPrivilege as exc:
            refused = exc.sqlstate
            cur.execute("rollback to savepoint object_insert_probe")

        assert refused == "42501", (
            "a Manager reaching zero colleges filed a document into another tenant's "
            "trainer folder."
        )


# F1 CLOSED by migration 2400 (applied 2026-08-19), and the NULL trap named
# here is closed twice over: 2400 guards the policy with an explicit
# `try_uuid(...) is not null`, and migration 2500 made can_reach_trainer(NULL)
# return FALSE at the source. Two checks on a path that turns a filename into
# a permission is the right number, not one too many.
@pytest.mark.parametrize(
    "path",
    [
        "trainers/not-a-uuid/signed_work_order.pdf",  # segment 2 is not a UUID
        "trainers/signed_work_order.pdf",  # there is no segment 2 at all
        "trainers/../signed_work_order.pdf",  # traversal-shaped, still not a UUID
    ],
)
def test_a_malformed_trainer_folder_path_denies_rather_than_allows(as_user, path: str) -> None:
    """A path that is not where the convention says it should be must DENY.

    0900 chose `try_uuid()` returning NULL over a raw `::uuid` cast so that one
    misfiled object cannot raise 22P02 and fail the entire listing query for
    everyone. The other two 0900 policies can rely on that NULL denying, because
    `can_reach_college()` / `can_reach_program()` are a bare
    `exists (... where id = p_id)` and are FALSE for NULL.

    `can_reach_trainer()` is not of that shape, and this is the probe that says
    so. `mgr_demo_inst` genuinely covers Demo Institute, so they are past the
    commercials wall and past any reach test — the only thing that can stop them
    reading a folder named after nothing is the NULL guard.
    """
    with as_user(USERS["mgr_demo_inst"]) as cur:
        seeded = _seed_document(cur, path)
        visible = scalar(cur, "select count(*) from storage.objects where name = %s", (seeded,))
        assert visible == 0, (
            f"an object at {seeded!r} is readable. A path segment that is not a UUID "
            "must deny — try_uuid() gives NULL and can_reach_trainer(NULL) is TRUE, so "
            "without an explicit `is not null` the reach conjunct is bypassed by a "
            "filename."
        )


def test_a_reachable_trainers_work_order_is_still_readable(as_user) -> None:
    """The control that must survive the fix.

    `mgr_demo_inst` covers Demo Institute and the seeded trainer is deployed
    there, so the work order is theirs to read — before 2400 and after it. This is
    the test that catches a fix which denies everything and calls it secure.
    """
    with as_user(USERS["mgr_demo_inst"]) as cur:
        trainer_id = _seed_deployed_trainer(cur, pan="PROBED444D")
        path = _seed_document(cur, f"trainers/{trainer_id}/signed_work_order.pdf")

        assert scalar(cur, "select public.can_reach_trainer(%s)", (trainer_id,)) is True
        visible = scalar(cur, "select count(*) from storage.objects where name = %s", (path,))
        assert visible == 1, (
            "the Manager who covers this trainer's college cannot read their signed "
            "work order. The reach conjunct has been written too tightly."
        )


def test_lde_executive_reads_no_trainer_folder_at_all(as_user) -> None:
    """R5 boundary, unchanged by 2400: the wall is around the NUMBER, not the person.

    §4 gives the LDE Executive no commercials, and a signed work order states the
    rate — so they get nothing under `trainers/`, even for a trainer standing in
    their own classroom whose attendance they mark daily. 0900's header calls that
    asymmetry the point. 2400 adds a conjunct to the same policy and must not
    disturb it.
    """
    with as_user(USERS["lde_demo_inst"]) as cur:
        trainer_id = _seed_deployed_trainer(cur, pan="PROBEE555E")
        path = _seed_document(cur, f"trainers/{trainer_id}/signed_work_order.pdf")

        assert scalar(cur, "select public.can_see_commercials()") is False
        assert (
            scalar(cur, "select count(*) from storage.objects where name = %s", (path,)) == 0
        ), "an LDE Executive read a signed work order, which states the rate."


def test_the_other_storage_folders_are_untouched(as_user) -> None:
    """`colleges/` still resolves by reach for all three internal personas.

    2400 rewrites one of the five policies on `storage.objects`. This pins that it
    rewrote only that one: the MOU under `colleges/<demo institute>/` stays visible
    to the campus executive who chases the signature, and invisible to a Manager
    with no assignments.
    """
    path = f"colleges/{COLLEGE_DEMO_INSTITUTE}/mou.pdf"
    with as_user(USERS["lde_demo_inst"]) as cur:
        _seed_document(cur, path)
        assert (
            scalar(cur, "select count(*) from storage.objects where name = %s", (path,)) == 1
        ), "an LDE Executive lost their own college's MOU"
    with as_user(USERS["mgr_no_reach"], ensure_persona="manager") as cur:
        _seed_document(cur, path)
        assert (
            scalar(cur, "select count(*) from storage.objects where name = %s", (path,)) == 0
        ), "a Manager with zero assignments read a college MOU"


# --- SEC-02: trainers (PAN) ---------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="SEC-02 OPEN: trainers_sourcing_all is keyed on persona alone.",
)
@pytest.mark.parametrize("who", ["mgr_no_reach", "sm_no_reach"])
def test_unassigned_commercials_persona_reads_no_trainer_roster(
    as_user, _assert_no_reach, who: str
) -> None:
    """PAN, email and phone for every trainer in the estate, to an account with no reach."""
    _assert_no_reach(who)
    with as_user(USERS[who]) as cur:
        visible = scalar(cur, "select count(*) from public.trainers")
        assert visible == 0, f"{who} reaches 0 colleges but sees {visible} trainer records"


@pytest.mark.xfail(
    strict=True,
    reason="SEC-03 OPEN: trainers_sourcing_all is `for all`, so PAN is writable "
    "cross-tenant. §6: PAN is the trainer identity and seeds the invoice number.",
)
def test_unassigned_manager_cannot_rewrite_a_pan(as_user, _assert_no_reach) -> None:
    _assert_no_reach("mgr_no_reach")
    with as_user(USERS["mgr_no_reach"]) as cur:
        cur.execute("update public.trainers set pan = 'ZZZZZ9999Z' where id = %s", (TRAINER_VEMA,))
        assert cur.rowcount == 0, (
            f"an unassigned Manager rewrote {cur.rowcount} PAN(s). §6 makes PAN the "
            "only stable trainer key and the seed of every invoice number."
        )


# --- cross-CLUSTER, not merely cross-college ---------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="SEC-02 OPEN: cluster isolation does not cover the roster or the rails.",
)
def test_senior_manager_cluster_does_not_leak_another_clusters_trainer(as_user) -> None:
    """A Senior Manager's reach stops at their cluster — for trainers too, not just programs."""
    with as_user(USERS["sm_cluster_south"]) as cur:
        assert scalar(cur, "select public.can_reach_college(%s)", (COLLEGE_MALINENI,)) is False
        cur.execute(
            "select full_name, pan, email, phone from public.trainers where id = %s",
            (TRAINER_VEMA,),
        )
        rows = cur.fetchall()
        assert rows == [], f"cluster South read a Malineni trainer's identity: {rows}"


# --- F2 (already known, still live): colleges has no column guard -------------


@pytest.mark.xfail(
    strict=True,
    reason="F2 OPEN (supabase/tests/02_rls_matrix_test.sql:48): colleges_internal_update "
    "has no column guard, so cluster_id is writable by any internal persona that "
    "reaches the college.",
)
def test_lde_executive_cannot_detach_a_college_from_its_cluster(as_user) -> None:
    """Not privilege escalation — oversight removal, by the least-authorised persona.

    `colleges.cluster_id` is how a Senior Manager's cluster assignment expands to
    colleges (`my_college_ids()`). Nulling it removes the campus from their view
    without touching their assignment row, so nothing looks revoked. `profiles`,
    `tasks` and `deployments` all narrow columns with a BEFORE UPDATE trigger;
    `colleges` does not.
    """
    with as_user(USERS["lde_demo_inst"]) as cur:
        cur.execute(
            "update public.colleges set cluster_id = null where id = %s",
            (COLLEGE_DEMO_INSTITUTE,),
        )
        assert cur.rowcount == 0, (
            "an LDE Executive detached their college from its cluster, removing it "
            "from the overseeing Senior Manager's reach"
        )


def test_program_scoped_commercial_tables_do_hold_the_line(as_user, _assert_no_reach) -> None:
    """The control that WORKS, locked in.

    `pnl`, `remuneration_sheets` and `work_orders` all carry
    `can_see_commercials() and can_reach_program(...)`. An unassigned Manager gets
    zero rows from all three. This is what the policies above should look like, and
    this test exists so a "simplifying" refactor cannot quietly drop the second
    conjunct here too.
    """
    _assert_no_reach("mgr_no_reach")
    with as_user(USERS["mgr_no_reach"]) as cur:
        for table in ("pnl", "remuneration_sheets", "work_orders"):
            assert scalar(cur, f"select count(*) from public.{table}") == 0, table
