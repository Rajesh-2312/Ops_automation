"""Measure the RAG layer: latency, index usage, recall under the persona filter.

    python tools/bench_rag.py all                 # everything that costs nothing
    python tools/bench_rag.py all --live          # + real embedding and generation
    python tools/bench_rag.py load --chunks 4000
    python tools/bench_rag.py retrieval --runs 200
    python tools/bench_rag.py explain
    python tools/bench_rag.py recall
    python tools/bench_rag.py chunks
    python tools/bench_rag.py embed --live
    python tools/bench_rag.py generate --live
    python tools/bench_rag.py clean

WHY THE THREE LATENCIES ARE REPORTED SEPARATELY
-----------------------------------------------
A single "Copilot took 4.2 seconds" number hides which of three unrelated
systems is slow, and each has a different owner and a different fix:

    sql        `public.rag_search()` alone, measured server-side. A database
               problem — index, plan, row count.
    embedding  one query embedded through OpenRouter. A vendor round trip on
               the critical path of every question.
    generation the completion. A vendor problem, and the only one a model
               choice changes.

Averaging them produces a number nobody can act on. p50/p95/p99 are reported per
stage; the mean is deliberately not.

WRITE DISCIPLINE
----------------
Every row created here has `source_ref` under `rag-eval/bench/`, and `clean`
deletes exactly that prefix. `load` refuses to run against a corpus that already
holds non-bench documents unless `--force` is given, because a load that doubles
someone's real index while measuring it is not a benchmark, it is an incident.
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

BENCH_PREFIX = "rag-eval/bench/"
EVAL_PREFIX = "rag-eval/"
BENCH_MODEL = "deterministic-sha256-1536"

#: The queries the retrieval benchmark runs, in rotation. Real questions from
#: `tests/rag_eval/eval_set.py` rather than lorem ipsum: a query vector's
#: neighbourhood density affects HNSW traversal cost, and random vectors have a
#: uniform neighbourhood that real questions do not.
BENCH_QUERIES = (
    "How are payable days counted for a bCAP trainer?",
    "What is the escalation procedure for a trainer no-show?",
    "How often must attendance be marked?",
    "What are the blocking validation gates before approval?",
    "How is a superseded contract clause handled?",
)


def load_dotenv() -> None:
    path = REPO_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def connect():  # noqa: ANN201 - psycopg.Connection, imported lazily
    import psycopg

    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        raise SystemExit("DATABASE_URL is not set (put it in .env)")
    return psycopg.connect(url, connect_timeout=30, autocommit=True)


def percentiles(samples: list[float]) -> dict[str, float]:
    """p50/p95/p99 by nearest rank. No mean — see the module docstring."""
    if not samples:
        return {}
    ordered = sorted(samples)

    def at(p: float) -> float:
        index = max(0, min(len(ordered) - 1, int(round(p * len(ordered))) - 1))
        return ordered[index]

    return {
        "n": len(ordered),
        "min": ordered[0],
        "p50": at(0.50),
        "p95": at(0.95),
        "p99": at(0.99),
        "max": ordered[-1],
    }


def show(label: str, stats: dict[str, float]) -> None:
    if not stats:
        print(f"{label:<28} (no samples)")
        return
    print(
        f"{label:<28} n={int(stats['n']):<5} "
        f"min={stats['min']:7.1f}  p50={stats['p50']:7.1f}  "
        f"p95={stats['p95']:7.1f}  p99={stats['p99']:7.1f}  max={stats['max']:7.1f}   (ms)"
    )


# --- embedding ----------------------------------------------------------------


def deterministic_vector(text: str) -> str:
    """The offline embedder's vector, in pgvector text form.

    Used so the benchmark can issue thousands of queries without buying an
    embedding for each. The vector's *distribution* differs from a real model's,
    which is why `embed --live` measures the real thing separately rather than
    this standing in for it.
    """
    from app.db.models import format_vector
    from app.rag.embeddings import DeterministicEmbedder

    return format_vector(DeterministicEmbedder().embed_one(text))


# --- load ---------------------------------------------------------------------

_LOAD_DOCS = """
insert into public.rag_documents
  (id, corpus, source_ref, title, version, is_commercial, content_hash, ingested_at, updated_at)
select gen_random_uuid(),
       'contracts'::public.rag_corpus,
       %(prefix)s || 'doc-' || i,
       '[RAG-EVAL] Bench Contract ' || i,
       1, true, md5('bench' || i), now(), now()
from generate_series(1, %(docs)s) i
returning id
"""

#: Chunk text is synthetic but shaped like the corpus: clause-length paragraphs
#: with contract vocabulary, so token counts and row widths are representative.
_LOAD_CHUNKS = """
insert into public.rag_chunks (id, document_id, ordinal, section, content, content_hash,
                               is_commercial, created_at)
select gen_random_uuid(), d.id, o.ordinal,
       'Clause ' || (o.ordinal + 1),
       'Clause ' || (o.ordinal + 1) || ' of the agreement. The Provider shall deliver the '
         || 'programme in accordance with the schedule, and the Institution shall pay the '
         || 'fees set out in the commercial annexure within the agreed period. '
         || 'Reference ' || d.source_ref || ' segment ' || o.ordinal || '.',
       md5(d.id::text || o.ordinal::text),
       true, now()
from public.rag_documents d
cross join generate_series(0, %(per_doc)s - 1) as o(ordinal)
where d.source_ref like %(like)s
"""

#: Random unit vectors, generated server-side. Pushing 4,000 x 1536 floats over
#: the wire from a laptop in India to Supabase takes minutes and measures the
#: network rather than the index.
_LOAD_EMBEDDINGS = """
insert into public.rag_embeddings (chunk_id, model, dim, embedding, created_at)
select c.id, %(model)s, 1536,
       l2_normalize((
         select ('[' || string_agg((random() - 0.5)::real::text, ',') || ']')::vector(1536)
         from generate_series(1, 1536)
       )),
       now()
from public.rag_chunks c
join public.rag_documents d on d.id = c.document_id
where d.source_ref like %(like)s
"""


def cmd_load(args: argparse.Namespace) -> None:
    per_doc = 8
    docs = max(1, args.chunks // per_doc)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select count(*) from public.rag_documents where source_ref not like %s",
            (f"{EVAL_PREFIX}%",),
        )
        foreign = cur.fetchone()[0]
        if foreign and not args.force:
            raise SystemExit(
                f"{foreign} non-eval documents are already in the corpora. Loading "
                "benchmark rows alongside a real index changes what you are measuring. "
                "Re-run with --force if that is genuinely what you want."
            )
        # Idempotent: `source_ref` is unique per (corpus, version), so a second
        # load without this fails on `rag_documents_version_uq` rather than
        # replacing the previous run's rows.
        cur.execute(
            "delete from public.rag_documents where source_ref like %s", (f"{BENCH_PREFIX}%",)
        )
        if cur.rowcount:
            print(f"removed {cur.rowcount} documents from a previous benchmark run")
        print(f"loading {docs} documents x {per_doc} chunks = {docs * per_doc} chunks ...")
        started = time.perf_counter()
        cur.execute(_LOAD_DOCS, {"prefix": BENCH_PREFIX, "docs": docs})
        cur.execute(_LOAD_CHUNKS, {"per_doc": per_doc, "like": f"{BENCH_PREFIX}%"})
        chunks = cur.rowcount
        cur.execute(_LOAD_EMBEDDINGS, {"model": BENCH_MODEL, "like": f"{BENCH_PREFIX}%"})
        vectors = cur.rowcount
        cur.execute("analyze public.rag_embeddings")
        cur.execute("analyze public.rag_chunks")
        elapsed = time.perf_counter() - started
        print(f"  {chunks} chunks, {vectors} vectors in {elapsed:.1f}s")
        _print_sizes(cur)


def _print_sizes(cur: Any) -> None:  # noqa: ANN401 - psycopg cursor
    for table in ("rag_documents", "rag_chunks", "rag_embeddings"):
        cur.execute(f"select count(*) from public.{table}")
        rows = cur.fetchone()[0]
        cur.execute(f"select pg_size_pretty(pg_total_relation_size('public.{table}'))")
        print(f"  {table:<16} {rows:>7} rows   {cur.fetchone()[0]}")
    cur.execute("select pg_size_pretty(pg_relation_size('public.rag_embeddings_hnsw'))")
    print(f"  {'hnsw index':<16} {'':>7}        {cur.fetchone()[0]}")


def cmd_clean(_: argparse.Namespace) -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "delete from public.rag_documents where source_ref like %s", (f"{BENCH_PREFIX}%",)
        )
        print(f"deleted {cur.rowcount} benchmark documents (chunks and vectors cascade)")


# --- retrieval latency --------------------------------------------------------

_SEARCH = """
select chunk_id, similarity
from public.rag_search(%(v)s, %(model)s,
  cast(%(corpora)s as public.rag_corpus[]), %(internal)s, %(commercials)s,
  cast(%(role)s as public.app_role), cast(%(colleges)s as uuid[]), %(limit)s, false)
"""

ALL_CORPORA = ["sop", "contracts", "college_dossier", "educator", "curriculum", "reports"]


def _search_params(role: str, vector: str, *, limit: int = 8) -> dict[str, object]:
    return {
        "v": vector,
        "model": BENCH_MODEL,
        "corpora": ALL_CORPORA,
        "internal": True,
        "commercials": role in ("manager", "senior_manager"),
        "role": role,
        "colleges": [],
        "limit": limit,
    }


def cmd_retrieval(args: argparse.Namespace) -> None:
    vectors = [deterministic_vector(q) for q in BENCH_QUERIES]
    with connect() as conn, conn.cursor() as cur:
        _print_sizes(cur)
        # Baseline: the round trip with no work in it. Everything below includes
        # this, and on a laptop talking to a hosted Postgres it is most of the
        # number — reporting the search timings without it would attribute a
        # network cost to the index.
        rtt: list[float] = []
        for _ in range(args.runs):
            started = time.perf_counter()
            cur.execute("select 1")
            cur.fetchall()
            rtt.append((time.perf_counter() - started) * 1000)
        show("network round trip", percentiles(rtt))
        for role in ("manager", "lde_executive"):
            samples: list[float] = []
            returned: list[int] = []
            for run in range(args.runs):
                params = _search_params(role, vectors[run % len(vectors)])
                started = time.perf_counter()
                cur.execute(_SEARCH, params)
                rows = cur.fetchall()
                samples.append((time.perf_counter() - started) * 1000)
                returned.append(len(rows))
            show(f"sql rag_search [{role}]", percentiles(samples))
            server: list[float] = []
            params = _search_params(role, vectors[0])
            params.pop("limit")
            for _ in range(min(args.runs, 20)):
                cur.execute(f"explain (analyze, costs off, timing off) {_BODY}", params)
                line = cur.fetchall()[-1][0]
                server.append(float(line.split(":")[1].strip().split(" ")[0]))
            show(f"  server-side execution [{role}]", percentiles(server))
            print(
                f"{'  rows returned':<28} min={min(returned)} max={max(returned)} "
                f"(limit was 8) — fewer than 8 means the filter ate the index candidates"
            )


# --- explain ------------------------------------------------------------------

#: `public.rag_search()` carries `SET search_path`, which makes it non-inlinable,
#: so `explain select * from rag_search(...)` shows a bare Function Scan and
#: nothing about the index. The body is therefore reproduced here VERBATIM from
#: migration 1600 in order to plan it. If the migration changes, this must too —
#: `test_sql_persona_filter.py` asserts the two have not drifted.
_BODY = """
  select
    c.id, d.id, d.corpus, d.title, c.section, c.content, d.version,
    d.superseded_at is not null,
    c.is_commercial or d.is_commercial,
    (1 - (e.embedding <=> %(v)s::vector(1536)))::double precision
  from public.rag_chunks c
  join public.rag_documents  d on d.id = c.document_id
  join public.rag_embeddings e on e.chunk_id = c.id and e.model = %(model)s
  where
    %(internal)s
    and exists (
      select 1 from public.rag_corpus_access a
      where a.corpus = d.corpus and a.role = cast(%(role)s as public.app_role)
    )
    and d.corpus = any (cast(%(corpora)s as public.rag_corpus[]))
    and (%(commercials)s or not (c.is_commercial or d.is_commercial))
    and (d.college_id is null or d.college_id = any (cast(%(colleges)s as uuid[])))
    and (
      d.program_id is null
      or exists (
        select 1 from public.programs p
        where p.id = d.program_id and p.college_id = any (cast(%(colleges)s as uuid[]))
      )
    )
    and (true or d.superseded_at is null)
  order by e.embedding <=> %(v)s::vector(1536)
  limit 8
"""


def _trim(line: str, width: int = 150) -> str:
    """Fold the 1536-element vector literal out of a plan line.

    An EXPLAIN of a vector query prints the whole query vector in every Sort Key
    and Index Cond. Unreadable, and it hides the row counts that matter.
    """
    folded = re.sub(r"'\[[-0-9.,e ]{80,}\]'", "'[...1536 floats...]'", line)
    return folded if len(folded) <= width else folded[:width] + " ..."


def cmd_explain(args: argparse.Namespace) -> None:
    vector = deterministic_vector(BENCH_QUERIES[0])
    with connect() as conn, conn.cursor() as cur:
        # pgvector's GUCs are registered when its library loads, which happens on
        # first use of the type in the session — not at connect. Without this the
        # `show` below fails with "unrecognized configuration parameter".
        cur.execute("select '[1,2]'::extensions.vector(2) <=> '[2,1]'::extensions.vector(2)")
        cur.execute("show hnsw.ef_search")
        ef = cur.fetchone()[0]
        cur.execute("show hnsw.iterative_scan")
        iterative = cur.fetchone()[0]
        print(f"hnsw.ef_search={ef}  hnsw.iterative_scan={iterative}")
        for role in ("manager", "lde_executive"):
            params = _search_params(role, vector)
            params.pop("limit")
            print(f"\n--- EXPLAIN (ANALYZE, BUFFERS) as {role} ---")
            cur.execute(f"explain (analyze, buffers, costs off) {_BODY}", params)
            for (line,) in cur.fetchall():
                print("  " + _trim(line))
        if args.seqscan:
            print("\n--- same query, index disabled (ground truth) ---")
            cur.execute("set local enable_indexscan = off")
            cur.execute("set local enable_bitmapscan = off")
            params = _search_params("lde_executive", vector)
            params.pop("limit")
            cur.execute(f"explain (analyze, buffers, costs off) {_BODY}", params)
            for (line,) in cur.fetchall():
                print("  " + _trim(line))


# --- recall under the persona filter ------------------------------------------


#: Move every benchmark vector to `query + small noise`, so the whole forbidden
#: set outranks everything the caller is allowed to read. Bench rows only.
_CLUSTER = """
update public.rag_embeddings e
set embedding = l2_normalize(
      %(qv)s::vector(1536)
      + (select ('[' || string_agg(((random() - 0.5) * 0.02)::real::text, ',') || ']')
           ::vector(1536)
         from generate_series(1, 1536))
    )
from public.rag_chunks c
join public.rag_documents d on d.id = c.document_id
where c.id = e.chunk_id and d.source_ref like %(like)s
"""


def cmd_recall(_: argparse.Namespace) -> None:
    """Does the persona filter cost an LDE Executive results the index hid?

    The construction: every benchmark document is a commercial contract, so an
    LDE Executive may read none of them. The permitted rows are the handful of
    SOP chunks from `tests/rag_eval/corpus.py`. If HNSW returns its `ef_search`
    nearest neighbours and the filter is applied to THOSE, an LDE Executive gets
    nothing — not because nothing is permitted, but because nothing permitted was
    in the candidate set. Turning the index off answers the same query correctly,
    which is what makes the difference an index artifact rather than a corpus gap.
    """
    vector = deterministic_vector(BENCH_QUERIES[0])
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            "select count(*) from public.rag_chunks c join public.rag_documents d "
            "on d.id = c.document_id where d.source_ref like %s",
            (f"{BENCH_PREFIX}%",),
        )
        bench_rows = cur.fetchone()[0]
        cur.execute(
            "select count(*) from public.rag_chunks c join public.rag_documents d "
            "on d.id = c.document_id where d.source_ref like %s and d.source_ref not like %s "
            "and not (c.is_commercial or d.is_commercial) and d.corpus = 'sop'",
            (f"{EVAL_PREFIX}%", f"{BENCH_PREFIX}%"),
        )
        permitted = cur.fetchone()[0]
        print(f"benchmark chunks (all commercial contracts): {bench_rows}")
        print(f"chunks an LDE Executive may read:            {permitted}")
        if bench_rows == 0:
            raise SystemExit("run `python tools/bench_rag.py load` first")

        # Pull every forbidden chunk to within a hair of the query, so the
        # nearest neighbours are ALL rows this persona may not read. That is the
        # adversarial-but-realistic case: a large contracts corpus and a small
        # SOP corpus, with a question whose vocabulary is contractual. Only bench
        # rows are touched.
        print("\nclustering the 4,000 forbidden vectors around the query ...")
        cur.execute(_CLUSTER, {"qv": vector, "like": f"{BENCH_PREFIX}%"})
        cur.execute("analyze public.rag_embeddings")

        plans = (
            ("planner's choice", []),
            (
                "index off (exact scan)",
                ["set local enable_indexscan = off", "set local enable_bitmapscan = off"],
            ),
            ("HNSW forced", ["set local enable_seqscan = off", "set local enable_sort = off"]),
        )
        # Run the BODY rather than `rag_search()`: the function's `SET
        # search_path` makes it non-inlinable, so a plan setting on the session
        # cannot be seen in — or reliably reach — the plan it uses internally.
        params = _search_params("lde_executive", vector)
        params.pop("limit")
        for label, settings in plans:
            cur.execute("begin")
            for statement in settings:
                cur.execute(statement)
            cur.execute(_BODY, params)
            got = cur.fetchall()
            cur.execute(f"explain (analyze, costs off) {_BODY}", params)
            nodes = [row[0].strip() for row in cur.fetchall()]
            access = next(
                (n for n in nodes if "on rag_embeddings" in n or "on rag_chunks c" in n),
                "?",
            )
            cur.execute("commit")
            print(f"  {label:<24} rows returned to lde_executive: {len(got)} / 8 requested")
            print(f"  {'':24} {_trim(access, 110)}")
        print(
            "\n  'HNSW forced' is the case that matters: it is the plan Postgres will\n"
            "  choose once the corpus is large enough for a sequential scan to look\n"
            "  expensive, and it is where hnsw.iterative_scan=off starves the filter."
        )


# --- chunk quality ------------------------------------------------------------


def cmd_chunks(_: argparse.Namespace) -> None:
    """Are chunks citable to a section, and are they cut mid-sentence?

    §9 requires a citation to name document AND section. A chunk whose text
    begins mid-clause is still labelled with a section, so the citation LOOKS
    sound while the quoted sentence is a fragment of a sentence that started in
    the previous chunk.
    """
    from app.rag.chunking import ROOT_SECTION, chunk_document
    from tests.rag_eval import corpus as eval_corpus

    def report(label: str, texts: list[str]) -> None:
        total = root = mid_start = mid_end = numbered_section = 0
        lengths: list[int] = []
        for body in texts:
            for chunk in chunk_document(body):
                total += 1
                lengths.append(len(chunk.content))
                root += chunk.section == ROOT_SECTION
                numbered_section += chunk.section[:1].isdigit()
                head = chunk.content.lstrip()
                if head and not head[0].isupper() and not head[0].isdigit():
                    mid_start += 1
                tail = chunk.content.rstrip()
                if tail and tail[-1] not in ".!?:;":
                    mid_end += 1
        print(f"\n{label}")
        print(f"  chunks                       {total}")
        print(f"  section == {ROOT_SECTION!r:<12}      {root}")
        print(f"  section starts with a digit  {numbered_section}  (contract clause OR list item)")
        print(f"  opens mid-clause             {mid_start}")
        print(f"  closes mid-clause            {mid_end}")
        print(
            f"  chars  min={min(lengths)} p50={statistics.median(lengths):.0f} "
            f"max={max(lengths)}  (target 1200, overlap 150)"
        )

    corpus_texts = [spec.text for spec in eval_corpus.documents()]
    corpus_texts += [eval_corpus.CONTRACT_V1_TEXT, eval_corpus.CONTRACT_V2_TEXT]
    report("evaluation corpus (clean shapes)", corpus_texts)
    report(
        "torture document (list + oversized paragraph + table)", [eval_corpus.CHUNKING_PROBE_TEXT]
    )


# --- live: embedding and generation -------------------------------------------


@dataclass(frozen=True)
class LiveResult:
    latency_ms: float
    tokens: int
    cost: float


def _openrouter_post(path: str, payload: dict[str, object]) -> tuple[dict[str, Any], float]:
    import httpx

    key = os.environ.get("OPENROUTER_API_KEY", "")
    base = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    if not key:
        raise SystemExit("OPENROUTER_API_KEY is not set")
    started = time.perf_counter()
    response = httpx.post(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=120,
    )
    elapsed = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return response.json(), elapsed


def cmd_embed(args: argparse.Namespace) -> None:
    """Real embedding latency and cost, at the batch sizes ingestion uses."""
    model = os.environ.get("OPENROUTER_EMBEDDING_MODEL") or args.model
    print(f"embedding model: {model}")
    if not os.environ.get("OPENROUTER_EMBEDDING_MODEL"):
        print("  WARNING: OPENROUTER_EMBEDDING_MODEL is unset in the environment.")
        print("  app/core/llm.py raises RuntimeError without it, so the live path is dead.")
    sentence = (
        "For a bCAP engagement the rate is a monthly retainer and payable days are counted "
        "down from the length of the period, including weekends and college holidays."
    )
    for batch in (1, 8, 32):
        samples: list[float] = []
        tokens = 0
        cost = 0.0
        for _ in range(args.runs):
            body, elapsed = _openrouter_post(
                "/embeddings", {"model": model, "input": [sentence] * batch}
            )
            samples.append(elapsed)
            usage = body.get("usage") or {}
            tokens = usage.get("prompt_tokens", 0)
            cost = usage.get("cost", 0.0)
        show(f"embed batch={batch}", percentiles(samples))
        print(f"{'  tokens/cost':<28} {tokens} tokens, ${cost:.8f} per call")


GEN_SYSTEM = "Answer using ONLY the numbered sources. Cite like [1]. Three sentences maximum."


def cmd_generate(args: argparse.Namespace) -> None:
    """Real generation latency and token usage on the volume tier."""
    model = os.environ.get("OPENROUTER_MODEL_VOLUME", "")
    if not model:
        raise SystemExit("OPENROUTER_MODEL_VOLUME is not set")
    from tests.rag_eval import corpus as eval_corpus

    sources = "\n\n".join(
        f"[{i}] Trainer Payout Cycle SOP — Payable days (v1)\n{block}"
        for i, block in enumerate(eval_corpus.SOP_PAYOUT_TEXT.split("\n\n")[:8], start=1)
    )
    user = f"SOURCES:\n{sources}\n\nQUESTION: How are payable days counted for a bCAP trainer?"
    latencies: list[float] = []
    prompt_tokens = completion_tokens = 0
    cost = 0.0
    for _ in range(args.runs):
        body, elapsed = _openrouter_post(
            "/chat/completions",
            {
                "model": model,
                "messages": [
                    {"role": "system", "content": GEN_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "temperature": 0.2,
                # Supplied here and NOT by app/rag/copilot.py — see finding F18.
                # Without it OpenRouter reserves the model's full 64,000-token
                # completion budget and rejects the request on a low balance.
                "max_tokens": 500,
            },
        )
        latencies.append(elapsed)
        usage = body.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        cost = usage.get("cost", 0.0)
    print(f"generation model: {model}")
    show("generation", percentiles(latencies))
    print(
        f"{'  tokens':<28} prompt={prompt_tokens} completion={completion_tokens} "
        f"total={prompt_tokens + completion_tokens}  cost=${cost:.6f}/answer"
    )


# --- all ----------------------------------------------------------------------


def cmd_all(args: argparse.Namespace) -> None:
    sections: list[tuple[str, object]] = [
        ("CHUNK QUALITY", cmd_chunks),
        ("LOAD", cmd_load),
        ("RETRIEVAL LATENCY", cmd_retrieval),
        ("PLAN", cmd_explain),
        ("RECALL UNDER THE PERSONA FILTER", cmd_recall),
    ]
    if args.live:
        sections += [("EMBEDDING (live)", cmd_embed), ("GENERATION (live)", cmd_generate)]
    for title, fn in sections:
        print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")
        fn(args)  # type: ignore[operator]


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    for name, fn, helptext in (
        ("load", cmd_load, "insert namespaced benchmark rows"),
        ("clean", cmd_clean, "delete namespaced benchmark rows"),
        ("retrieval", cmd_retrieval, "rag_search() latency percentiles"),
        ("explain", cmd_explain, "EXPLAIN ANALYZE the retrieval plan"),
        ("recall", cmd_recall, "does the HNSW candidate set starve the filter"),
        ("chunks", cmd_chunks, "chunk size and citability statistics"),
        ("embed", cmd_embed, "live embedding latency and cost"),
        ("generate", cmd_generate, "live generation latency and tokens"),
        ("all", cmd_all, "everything"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.set_defaults(func=fn)
        p.add_argument("--chunks", type=int, default=4000)
        p.add_argument("--runs", type=int, default=50)
        p.add_argument("--force", action="store_true")
        p.add_argument("--live", action="store_true")
        p.add_argument("--seqscan", action="store_true", help="also plan with the index off")
        p.add_argument("--model", default="openai/text-embedding-3-small")
        p.add_argument("--json", action="store_true")

    args = parser.parse_args()
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
