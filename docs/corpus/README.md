# The SOP corpus

This directory holds the starter corpus for the Ops Copilot: written policy and
procedure that the Copilot retrieves, quotes and cites when somebody asks how
something is supposed to work.

These documents are meant to be read by people as well as by a retriever. They are
written as prose, with section headings specific enough that a citation naming one
tells the reader where to look and what they will find there.

The documents live in `sop/`, one directory per corpus, so that indexing a corpus
is a matter of pointing at its directory. This README sits outside those
directories on purpose: it is documentation *about* the corpus, it is not policy,
and it should not be indexed.

## What this corpus is not

**It is not a contract.** Nothing here creates an obligation to a trainer, a
college or anyone else. Contracts are documents in their own right and live in
their own versioned corpus.

**It is not a system of record.** No document here is the authority for any value.
The database is. If a document and the database disagree about what happened, the
database is right and the document is out of date.

**It contains no figures.** There is no rate, no payout amount, no trainer, no
college and no date about any real engagement anywhere in these files. The only
numbers present are rules — a field length, a deviation threshold, a default
withholding rate, a count of states — and a rule is not a fact about anybody.

## Structured facts never come from here

The governing rule for retrieval is that structured facts — dates, amounts and
counts — are never answered from documents. They are read from the database, with
a query, against the live record. Documents supply policy and context only, and a
hybrid answer keeps the two visibly apart.

This is why the corpus is written the way it is. A sentence like "a trainer was
paid for six days in July" would be true when written, indexed forever, and
retrieved with confidence long after it stopped being true. So the corpus explains
how days are counted and leaves the counting to the system that holds the days.

The Copilot enforces this in code, not in a prompt: a question that asks for a
fact is refused before anything is retrieved, and any number in a generated answer
that was not in the retrieved passages, the question, or the structured values the
caller supplied causes the answer to be discarded.

## The documents

All six are SOP-corpus documents and live in `sop/`.

| File | Subject | Commercial |
|---|---|---|
| `sop/attendance-and-payable-days.md` | How trainer attendance is marked and how payable days are counted on each program type | None |
| `sop/payout-computation-and-validation-gates.md` | How a payout is computed, what blocks a cycle and what merely warns | **Whole document** |
| `sop/roles-reach-and-the-commercials-wall.md` | Persona versus reach, and what an LDE Executive cannot see | Some passages |
| `sop/erm-sync-and-the-field-pack.md` | The manual ERM sync task, the field pack and drift detection | None |
| `sop/approval-and-release.md` | The draft-to-release lifecycle, and why agents cannot send | Some passages |
| `sop/using-the-ops-copilot.md` | What the Copilot answers, what it refuses, and how to read a citation | Some passages |

The commercial column is a statement of intent for whoever indexes these files,
not a machine-readable interface. Indexing also classifies each passage on its own
text, and that classification can only ever add restriction, never remove it.

The intent behind the three values is worth stating, because the SOP corpus is
readable by an LDE Executive and the choice is therefore a real one. The payout
document should be walled whole: every passage in it quotes money, so there is
nothing in it a campus reader loses by being excluded. The two documents marked
"none" quote no money anywhere and need no decision at all. The three marked "some
passages" are mostly operational text with a few paragraphs that name walled
categories, and those should be walled passage by passage rather than wholesale —
the same treatment governance reports get, and for the same reason: walling the
whole document would cost the LDE Executive the part that is theirs.

## A note on how commercial content is written

The commercials wall applies to retrieval, so a passage that quotes money is
walled from an LDE Executive even inside a corpus they may otherwise read. That
made the writing of these documents a real choice rather than an afterthought.

The rule followed here is to let the subject decide the vocabulary. The payout
document is about rates, remuneration and invoices, and it says so in those words
throughout; it is meant to be flagged, and campus staff are meant not to retrieve
it. The attendance document is about counting days, quotes no amount, and is
written in the vocabulary of marks and day counts so that the people who do the
marking can actually read it. That is not an attempt to slip commercial content
past the classifier — there is no commercial content in it to slip. The arithmetic
that turns a day count into money is in the document that carries the money
vocabulary, and the attendance document says so and points at it.

Some sections of the roles, approval and Copilot documents name the walled
categories by name and will be flagged as a result. That is the classifier being
conservative, which is the behaviour it was built for.

## Where these documents come from

Everything in these files was read out of something already in this repository.
Nothing was supplied from outside it and nothing was inferred to fill a gap.

The rules and their reasoning come from `CLAUDE.md`, principally its hard rules and
sections four through eleven. The observed behaviour of the legacy manual process
comes from `docs/legacy-sheet-findings.md`. The implemented arithmetic, the
counting rules and the validation gates come from `app/domain/attendance.py`,
`app/services/remuneration/engine.py`, `app/services/remuneration/validators.py`
and `app/services/remuneration/invoice_no.py`. The lifecycle and its enforcement
come from `app/services/approval/`, and the retrieval rules from `app/rag/`. The
access rules come from the migrations under `supabase/migrations/`, where a
constraint or a policy is itself the policy — most directly `0600` for attendance,
`0700` for the commercials wall, `1300` for the audit trail and artifact versions,
`1600` for the corpora and their access table, `1800` for trainers being records
rather than users, `1900` for ERM sync, and `2100` through `2500` for how persona
and reach are bound.

## Open questions carried, not answered

Several things an operations document would normally state are genuinely
undecided. They are carried openly rather than filled in with something plausible,
because a plausible answer in a corpus the Copilot quotes becomes a confidently
cited wrong answer later.

**CLAUDE.md §14 question 1, the terms of a CRT engagement.** Whether TA and DA on a
per-day engagement is paid per travel day is unknown, and whether a day rate can be
set per program rather than per work order is unknown. Carried in the payout
document, which also records what the evidence on file does establish.

**CLAUDE.md §14 question 2, the "Expense for the month" field.** Its meaning is
unknown and the cell is left empty rather than filled. Carried in the payout
document.

**CLAUDE.md §14 question 3, approval authority for college-facing communications.**
Undecided, which is why outbound messages can be drafted and submitted but cannot
be approved by anybody today. Carried in the approval document, including the
instruction not to unblock the queue by picking the permissive option.

**CLAUDE.md §14 question 4, whether Finance accepts a generated remuneration
sheet.** Unanswered, and it decides whether that phase can be automated end to end.
Carried in the payout document.

**CLAUDE.md §14 question 6, access to the EdTech platform.** Whether it is a
database connection, an API, or neither is unknown. Carried in the ERM document,
which is where the general pattern for systems without an interface is set out.

**Release authority.** No persona is named for release anywhere; it is currently
read as approval authority as a conservative placeholder. Carried in the approval
document.

**ERM's own field order.** Nobody on this side has seen the ERM form. The order is
a declared, versioned guess that announces itself as unverified. Carried in the ERM
document.

**Who "AM" is** in the invoice sheet's recipient fields. Unidentified, so the field
is left empty and no template is wired to it. Carried in the payout document.

## Adding to or changing this corpus

Write for somebody asking how something is supposed to work, in prose, with each
section answering one question completely. Make headings specific: a citation
reading "Why TDS is charged on earned pay and never on gross" is usable and one
reading "Details" is not.

Assert nothing that is not already written down in this repository. If a topic
matters and the answer is not on file, write the section as an open question and
name the source that records it as open. That is more useful than a guess and far
safer than one.

Keep figures out. If a document seems to need one, what it actually needs is a
sentence explaining where that figure is read from.

Changing a document here replaces it: procedure documents are not versioned in the
index, so the old passages are removed and the new ones take their place. Only
contracts retain their superseded versions. Re-ingesting an unchanged file does
nothing, so re-indexing the whole directory is safe and cheap.
