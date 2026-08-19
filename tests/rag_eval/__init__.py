"""RAG evaluation harness — CLAUDE.md §9 as an executable specification.

This package measures the RAG layer rather than unit-testing it. The difference
matters: `tests/unit/test_rag_*.py` proves each module behaves as written, with
fakes on every seam. Nothing there touches Postgres, pgvector, a real embedding
model or a real generation. So the questions those tests cannot answer are
exactly the ones §9 is about — does the persona filter hold in the *plan* the
planner actually chooses, does the citation gate survive an adversarial prompt,
is a policy question refused because it contains the word "how many".

Layout
------
    eval_set.py                the reusable question set — question, expected
                               behaviour, why. Import it; do not fork it.
    corpus.py                  the namespaced test documents, including a
                               prompt-injection document and a superseded
                               contract clause.
    conftest.py                DB / embedder / namespace fixtures.
    test_guards_evalset.py     offline: the eval set against the refusal gates.
    test_citation_gate.py      offline: adversarial attempts at an uncited answer.
    test_figure_gate.py        offline: adversarial attempts at a fabricated figure.
    test_chunk_citations.py    offline: can a chunk actually be cited to a section.
    test_sql_persona_filter.py DB: the §9 wall, proven in SQL and in EXPLAIN.
    test_versioning.py         DB: superseded clauses carry their flag.
    test_injection.py          DB: an ingested document that tries to give orders.
    test_logging.py            offline: §11 agent-I/O logging actually happens.

Running
-------
    python -m pytest -q tests/rag_eval

DB-backed tests skip themselves when `DATABASE_URL` is absent. Live-model tests
(real OpenRouter calls, real money) run only with `RAG_EVAL_LIVE=1`.

Write discipline
----------------
Every row this package creates is namespaced by `corpus.NAMESPACE` on
`rag_documents.source_ref` and removed in fixture teardown. Nothing here deletes
a row it did not create; the DELETE is `where source_ref like 'rag-eval/%'` and
nothing else.
"""
