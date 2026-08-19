# Roles, reach and the commercials wall

## What this document covers and who it is for

This document explains who can see what in the platform, and why. It separates
two things that are constantly confused: which persona somebody holds, and which
colleges that person can reach. It is written for anyone who has asked "why can't
I see this college" or "why can my colleague see something I cannot", and for
whoever runs an access review.

The short version is that the persona decides what kind of thing you may see, the
assignment tables decide which colleges you may see it for, and both are enforced
in the database rather than in the interface.

## The three internal personas, and the two external ones

There are three internal personas and they form a line: Senior Manager, then
Manager, then LDE Executive on campus.

A Senior Manager covers all programs in their cluster and holds escalation and
approval authority. A Manager covers their own colleges and works with the full
program state for them. An LDE Executive covers their own college only and owns
the daily campus work — attendance, batches, task lists.

Two personas sit outside that line. A college has a read-only view of published
artifacts and nothing else. Trainers are records rather than users and do not sign
in at all, which the next-but-one section covers.

byteXL's internal teams — TA and Sourcing, HR, Finance and Accounts, Tech and
Assessment, Platform, Learning and Science, Central OPS, Placements — are modelled
as actors in the process rather than as personas with logins. A task can be
assigned to one of them; none of them is a permission level.

## Persona is not reach, and reach comes from an assignment

A persona says what kind of thing you may work with. It says nothing about which
colleges. Reach comes from two assignment tables and only from them.

Managers and LDE Executives are assigned to colleges directly. Senior Managers are
assigned to clusters, and a cluster expands to its colleges through the cluster
reference each college carries. That is why one Manager can hold several colleges
and a Senior Manager a whole cluster without either of them having a different
persona from their peers.

Every policy in the database resolves reach through a small set of shared helper
functions that read those assignment tables — is this caller internal, is this
caller a Senior Manager, which colleges do they hold, can they reach this college
or program or batch or deployment. Policies never re-derive reach for themselves,
which is what keeps one change to the rules from applying to some tables and not
others.

There is a college reference on a user's profile as well, and it is used only by
the College persona for a college's own login. Internal staff never take their
scope from it, and a change to it does not widen anyone's reach.

## Why an assignment with no rows means no access at all

Reach is deny-by-default. A Senior Manager with no cluster assignment reaches
nothing, sees no programs and no P&L, and that is the correct behaviour rather
than a bug: the persona is scoped to all programs in a cluster, not to all
programs.

Being an administrator is not reach either. Administrative rights grant the
ability to hand out assignments; they are not themselves an assignment, and there
is no administrative override on any of the commercial policies. Someone who needs
to see a college's numbers gets assigned to it, on the record, rather than
stepping around the rule.

A new internal account therefore looks empty until an administrator assigns it.
That emptiness is the system working.

## The commercials wall: what an LDE Executive cannot see

An LDE Executive gets zero rows from the commercial tables — P&L, remuneration
sheets, invoices, and the rates on work orders. Not a filtered view, not a masked
column, not a hidden menu item. Zero rows, from the database, on any connection.

The wall is a single reusable predicate that is true for a Senior Manager and a
Manager and false for everyone else. Every policy on a commercial table is written
in the same shape: the wall, and then the reach test, joined together. Both halves
carry weight. Drop the wall and every campus executive can read every trainer's
rate; drop the reach test and a Manager can read another cluster's P&L. Neither
failure raises an error. Both simply return rows.

Each of those boundaries has a test that asserts a forbidden read returns nothing.
The rule is that row-level security is tested rather than assumed, and a boundary
without a test does not count as a boundary.

## Why a persona check alone is not enough

A wall that checks only the persona has checked half the question. This was a real
defect on the trainer-scoped commercial tables: the policies carried the
commercials wall but no reach test, so a legitimately provisioned Manager could
read and write every trainer's bank details in the country regardless of which
colleges they covered. Migration 2200 added the missing conjunct.

The correct habit when writing or reviewing a policy on anything commercial is to
read it as two questions. May this persona see this class of thing at all, and may
this person see it for this college? An answer to only the first is an insider
exposure waiting to be found.

One policy is deliberately exempt. The sourcing roster of trainers is reachable
without a college test, because sourcing precedes deployment: a trainer who has
not been engaged anywhere yet reaches no college by construction, and a reach test
there would make the roster invisible to the people whose job is to build it.

## Why the engagement rate lives on its own table

A trainer's rate is on the work order, not on the deployment. That is a
consequence of the wall rather than a modelling preference.

An LDE Executive has to be able to read deployments — attendance, tracksheets and
the campus dashboard all walk through them. An LDE Executive must not be able to
read rates. A commercial column on an operational table forces a choice between
those two requirements, and either answer is wrong. Splitting the tables costs one
join and buys a clean boundary.

The same reasoning explains why the ERM sync field packs carry no rate and no bank
detail. Those task rows are readable by campus staff on the same terms the trainer
record is, so a pack that carried a per day rate would walk a commercial value
straight around the wall through a helper nobody thought of as commercial.

## Trainers are records, not users

An educator never signs in. Work orders, deployments, attendance and payouts are
managed for them by internal staff. This was the owner's decision of 18 August
2026, and the migration that implemented it removed every policy the trainer
persona held, including four writes.

Two of those writes are the reason the change was worth making carefully. They let
the person being paid mark the attendance that decided their own pay. The rest let
a trainer read their own record, deployment, tasks and remuneration sheet.

If an educator-facing portal is ever wanted, it is a new build against the
existing trainer and deployment tables, with its own decisions about what an
educator may see. It is not a revert of that migration.

## Why the trainer label still exists, and why removing it would be a defect

The trainer label survives in the role enum even though no trainer signs in, and
deleting it would break something important.

It is the deny-by-default sentinel. Every new signup is created with that role and
no persona is read from the signup request at all. Because the label carries no
policy on any table, it grants precisely nothing — which makes it a better
sentinel now than when it was a real persona. The profile's trainer reference and
the constraint that links it to the role remain for the same reason, and the helper
that resolved a signed-in trainer's identity is kept but can no longer be executed
by an ordinary authenticated session.

Dropping a label from an enum that is already in use is also close to
irreversible. There is no upside and a permanent downside.

## A signup cannot choose its own persona

Persona is assigned by an administrator, through a guarded update path, and never
by the account being created.

It used to be read from the signup request's own metadata, which is chosen by
whoever is signing up. Combined with email confirmation being disabled, a single
unauthenticated request could produce a Manager session, and the three policies
that carried the commercials wall without a reach test handed that account a large
number of trainer identities and writable payment rails. The path was reproduced
against the database and closed.

The practical rule that follows: do not reintroduce a persona field at signup. The
picker was removed from the interface for this reason and not by oversight, and
anything that lets a caller state its own role at account creation is the same
defect wearing different clothes.

## What a college can see

A college persona sees published artifacts, read-only, through curated views
rather than through row-level access to the underlying tables. Trainer attendance
is never exposed to a college in any form: it is a payroll record about a byteXL
contractor, not a delivery metric.

That distinction is worth holding on to when someone asks for "the attendance
data" for a college. Student attendance summaries are theirs. The trainer's day
record is not.

## Retrieval is scoped before it runs, not after

The same wall applies to anything the Ops Copilot retrieves. The persona filter is
applied before retrieval rather than after generation, and it is part of the same
statement that ranks and truncates the results.

The ordering matters more than it looks. Filtering a result set that has already
been cut to the top few would silently discard the permitted material that the
forbidden material outranked, and the answer would look thin rather than wrong.

A retrieval scope also cannot claim reach that its persona does not have. Building
one that asserts an LDE Executive may see commercials raises an error rather than
retrieving, so the wall cannot be widened by a caller with good intentions and a
failing test to fix. Which corpora each persona may read at all is held as data in
an access table, so an access review is a query rather than an exercise in reading
policy source.
