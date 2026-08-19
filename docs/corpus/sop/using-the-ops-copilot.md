# Using the Ops Copilot

## What this document covers and who it is for

The Ops Copilot answers questions about internal policy, procedure, contracts,
college history and curriculum by retrieving passages from indexed documents and
explaining them with citations. This document explains what it will answer, what
it refuses and why, and how to read what it gives back. It is written for everyone
who uses it, and particularly for anyone who has been refused an answer and
suspects the tool is broken.

The Copilot is deliberately the least capable agent in the platform. It has no
ability to draft anything, no queue, and no state to write. Its entire capability
is retrieve and explain.

## What an answer from the Copilot is

An answer is prose that is grounded in passages the asker was permitted to
retrieve, that cites the document and section behind each claim, and that contains
no number it was not handed.

If any of those three fails, the answer is not returned in a weakened form. It is
replaced by a refusal. There is no partial-credit path, because a partially
grounded answer is indistinguishable from a grounded one to the person reading
it — and the person reading it is about to act on it.

## Why every answer carries a citation

The rule is that every answer cites its source document and section, and that no
citation means no answer.

This is checked rather than requested. The model is asked to cite, and then the
citations in what it produced are resolved against the passages that were actually
retrieved. An answer whose references do not resolve is discarded and a refusal is
returned instead. It is discarded rather than repaired, because an answer with an
invented citation is not an answer with a formatting problem; it is a fabrication
that reads as sourced.

A citation names the document title and the section within it, which is why
section headings in the corpus are written to be specific enough to act on. If you
are checking an answer, go to the cited section and read it. That is the intended
workflow, not a fallback for when you are suspicious.

## Why the Copilot refuses to tell you a number

Ask the Copilot how many days a trainer worked last month, or what a college was
charged, or when a batch started, and it will refuse and tell you where the answer
actually lives. That is the design working.

Structured facts — dates, amounts and counts — are never retrieved from documents.
Documents supply policy and context. Facts come from the database, through a query,
against the live record. The question is recognised and declined before anything is
retrieved and before any model sees it.

The failure this prevents is specific and severe: a dispute settled from a sentence
in a six-week-old status report that happened to mention a number. The report was
not wrong when it was written. It is simply not the system of record, and nothing
about the way it reads makes that obvious.

Every number in a generated answer is also checked against the material the model
was given. A figure that appears in none of its inputs — an arithmetic result, a
rounded restatement, a plausible-looking total — fails the check and the answer is
refused. This is what makes it safe for the Copilot to explain a figure: the check
proves the figure was given to it rather than produced by it.

## What a hybrid answer looks like

Some questions are half policy and half fact — "what is the rule here, and where
does this program stand against it". Those are answered by handing the Copilot the
structured values, read from the database by the caller, alongside the question.

The two halves stay visibly separate. The retrieved policy is explained in prose
with citations; the values from the database are returned in their own block and
are never quietly merged into the sentences. The prose may refer to them, which is
explaining a number you were given, and that is allowed. Producing one is not.

## When the Copilot says it has no sources

A refusal for lack of sources means nothing in the corpora you are permitted to
read was close enough to cite. It does not mean the policy does not exist.

The useful response is to treat it as a gap in the corpus and say so, because the
gap is fixable: a policy that exists only in somebody's head or in a mail thread
cannot be cited, and the Copilot will keep refusing until it is written down and
indexed. The same is true of the refusals that send you to the database. The
proportion of questions refused because they asked for a fact is tracked
deliberately — it is a live list of the queries the platform still owes its users.

## What each persona can retrieve

There are six separately indexed and separately permissioned corpora: standard
operating procedures, contracts, college dossiers, educator material, curriculum,
and reports.

Procedures, college dossiers, curriculum and reports are available to all three
internal personas, with a college dossier further scoped to the colleges a person
actually holds. Contracts and educator material are restricted to Senior Managers
and Managers.

Contracts are walled at the whole corpus rather than passage by passage. An MoU is
a single document whose clauses interleave scope, obligations and commercial terms,
and a chunker cannot be trusted to have caught every clause that quotes a number.
Drawing the line at the corpus makes a mis-flagged passage a redundancy failure
rather than a leak. Reports are treated the other way round, passage by passage,
because a governance report is mostly delivery narrative that campus staff should
be able to read, and walling the whole corpus would cost them the part that is
theirs.

A trainer and a college hold no corpus at all, so the Copilot returns nothing for
them — before issuing any query, and recorded in the log as a denial rather than
appearing as an empty result that looks like a gap in the corpus.

## Superseded contract clauses carry a flag

The contracts corpus is versioned. When a contract changes, the previous version
is retained and marked superseded rather than being overwritten, and a superseded
passage never surfaces without a version flag on it.

This exists for one situation in particular. A dispute about July is argued against
the work order as it read in July, and an index that had silently upgraded itself
to a later amendment would answer that dispute wrongly and with total confidence.

Procedure documents work the other way: a changed procedure replaces the old one,
because nobody argues a dispute against last quarter's procedure, and keeping every
draft would fill the index with near-duplicates that compete with each other at
retrieval time.

## Where the corpus comes from, and who can write to it

Documents are ingested by a service, never from a browser. There is no write path
to the index for an ordinary signed-in session, and that is a security property
rather than an oversight: a corpus that can be written from the browser is a corpus
in which anyone can plant an authoritative-looking procedure document, and since
answers cite these passages, a forged passage is a forged citation.

Re-ingesting an unchanged document does nothing at all, by design, so the corpus
can be re-indexed on a schedule without churning the identifiers that citations
resolve against.

## What is logged

Every invocation is logged with the prompt, the tools called, the tokens and the
latency, along with the persona, which corpora were searched, and how many passages
came back. Refusals are logged with their reason from a fixed vocabulary so they
can be counted.

The point of counting them is improvement rather than surveillance. A rising count
of fact refusals says the platform needs another query surface; a rising count of
no-source refusals says the corpus has a hole in it.
