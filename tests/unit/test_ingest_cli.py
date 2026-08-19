"""Tests for tools/ingest_docs.py — the corpus ingestion CLI.

Everything here is offline. The parts of the CLI worth testing are the parts
that REFUSE, and all of them were deliberately written as pure functions so they
can be tested without a database, an API key or a corpus on disk:

  * §9's citation rule — a document whose chunks cannot name a section is not
    ingestible, because "Every answer cites source document and section" is not
    satisfiable by a chunk whose section is the `Document` fallback.
  * §4's commercials wall — a money-quoting document may not land in a corpus an
    LDE Executive can read unless a human says which wall they meant.
  * The insert ordering that made ingestion possible at all (F-A), and the
    embedding-model naming that keeps re-ingest idempotent (F-B).

The gates are the product here. A CLI that ingests is easy; a CLI that declines
to quietly index a rate card into the corpus an LDE Executive reads on campus is
the reason this file exists.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.llm import EMBEDDING_DIM, EMBEDDING_MODEL_ENV
from app.db.models import RagChunk, RagDocument, RagEmbedding
from app.domain.enums import Corpus, Persona
from app.rag.chunking import ROOT_SECTION, chunk_document, normalise
from tools.ingest_docs import (
    Plan,
    Refusal,
    _OrderedFlush,
    markdown_files,
    open_roles_for,
    plan_document,
    resolve_embedder,
    source_ref_for,
    title_for,
)

#: The real `rag_corpus_access` grant for `sop`, `college_dossier`, `curriculum`
#: and `reports` — the corpora an LDE Executive may read.
OPEN_ACL = frozenset({Persona.SENIOR_MANAGER, Persona.MANAGER, Persona.LDE_EXECUTIVE})

#: The real grant for `contracts` and `educator`. Commercials personas only.
WALLED_ACL = frozenset({Persona.SENIOR_MANAGER, Persona.MANAGER})

CLEAN_SOP = """# Trainer Onboarding SOP

The onboarding checklist runs from work order to platform access.

## Work order

A signed work order must be on file before a deployment starts.

## Platform access
Raise a ticket with the Platform team and record the confirmation.
"""

COMMERCIAL_SOP = """# Trainer Payout Cycle SOP

## Payable days

Payable days are counted down from the length of the period for a bCAP
engagement, and up from P marks for CRT.

## Deductions

TDS is deducted at 10% of earned, and never of gross, so a reimbursement of
INR 100 does not change the tax.
"""

PREAMBLE_DOC = """This paragraph sits before any heading at all, which is where a
definitions clause usually lives.

# Governance Report

The delivery narrative for the quarter.
"""


def plan(text: str, corpus: Corpus, acl: frozenset[Persona], mode: str | None = None) -> object:
    return plan_document(Path("docs/corpus/x.md"), text, corpus, acl, mode)


# --- identity: source_ref and title ------------------------------------------


def test_source_ref_is_repo_relative_posix() -> None:
    """`source_ref` is the identity ACROSS versions, so it must not carry a drive letter."""
    ref = source_ref_for(Path("tools/ingest_docs.py"))
    assert ref == "tools/ingest_docs.py"
    assert "\\" not in ref


def test_source_ref_outside_repo_is_absolute(tmp_path: Path) -> None:
    stray = tmp_path / "loose.md"
    stray.write_text("# x\n", encoding="utf-8")
    assert source_ref_for(stray) == stray.resolve().as_posix()


def test_title_comes_from_the_first_h1() -> None:
    """The document half of a §9 citation. A real heading beats a filename."""
    assert title_for(CLEAN_SOP, Path("whatever.md")) == "Trainer Onboarding SOP"


def test_title_falls_back_to_the_filename_stem() -> None:
    assert title_for("Just prose.\n", Path("docs/college-dossier.md")) == "college-dossier"


# --- file discovery -----------------------------------------------------------


def test_markdown_files_recurses_a_directory_in_sorted_order(tmp_path: Path) -> None:
    (tmp_path / "nested").mkdir()
    for name in ("b.md", "a.md", "nested/c.markdown", "notes.txt"):
        (tmp_path / name).write_text("# h\n\ntext\n", encoding="utf-8")
    found = [p.name for p in markdown_files(tmp_path)]
    assert found == ["a.md", "b.md", "c.markdown"]


def test_markdown_files_refuses_a_non_markdown_file(tmp_path: Path) -> None:
    other = tmp_path / "sheet.xlsx"
    other.write_text("x", encoding="utf-8")
    with pytest.raises(SystemExit):
        markdown_files(other)


# --- §9: a chunk that cannot name a section cannot be cited -------------------


def test_preflight_chunks_are_exactly_what_will_be_stored() -> None:
    """The reported section map must be the stored one, or the report is decoration.

    `chunk_document` is a pure function and `RagIngestor` calls it with the same
    defaults, so planning and writing agree by construction. This asserts that
    they still do.
    """
    result = plan(CLEAN_SOP, Corpus.SOP, OPEN_ACL)
    assert isinstance(result, Plan)
    assert result.chunks == chunk_document(normalise(CLEAN_SOP))
    assert result.sections == ("Trainer Onboarding SOP", "Work order", "Platform access")


def test_document_with_no_headings_is_refused() -> None:
    """§9: "No citation -> no answer."

    Every chunk would carry the `Document` fallback, which names a file and no
    findable place inside it. `rag_chunks.section` is NOT NULL, so the database
    would accept it happily — the refusal has to live here.
    """
    result = plan(
        "A policy with no headings whatsoever.\n\nSecond paragraph.\n", Corpus.SOP, OPEN_ACL
    )
    assert isinstance(result, Refusal)
    assert ROOT_SECTION in result.reason
    assert "headings" in result.remedy


def test_empty_document_is_refused() -> None:
    result = plan("   \n\n  \n", Corpus.SOP, OPEN_ACL)
    assert isinstance(result, Refusal)
    assert "empty" in result.reason


def test_preamble_before_the_first_heading_is_kept_and_warned_about() -> None:
    """Retained, not dropped — chunking.py keeps it because it is often definitions.

    But it is reported, because a citation reading "Governance Report — Document"
    is weaker than the rest and the operator should know which chunks are like
    that.
    """
    result = plan(PREAMBLE_DOC, Corpus.REPORTS, OPEN_ACL)
    assert isinstance(result, Plan)
    assert len(result.unsectioned) == 1
    assert any(ROOT_SECTION in w for w in result.warnings)


# --- §4: the commercials wall -------------------------------------------------


def test_open_roles_are_the_personas_without_commercials() -> None:
    assert open_roles_for(OPEN_ACL) == frozenset({Persona.LDE_EXECUTIVE})
    assert open_roles_for(WALLED_ACL) == frozenset()


def test_commercial_document_into_an_lde_readable_corpus_is_refused() -> None:
    """The case this gate exists for.

    `looks_commercial()` would flag the money chunks and the chunk policy would
    wall them — but that regex is, in `app/rag/ingest.py`'s own words, "the
    cheapest one is also the least reliable". Filing a rate card into `sop` must
    be a decision somebody made, not a default.
    """
    result = plan(COMMERCIAL_SOP, Corpus.SOP, OPEN_ACL)
    assert isinstance(result, Refusal)
    assert "lde_executive" in result.reason
    assert "--commercial document" in result.remedy


def test_commercial_mode_document_walls_the_whole_document() -> None:
    result = plan(COMMERCIAL_SOP, Corpus.SOP, OPEN_ACL, "document")
    assert isinstance(result, Plan)
    assert result.is_commercial is True


def test_commercial_mode_chunks_walls_only_the_flagged_chunks() -> None:
    """The `reports` case the ACL describes: narrative theirs, margin not."""
    result = plan(COMMERCIAL_SOP, Corpus.REPORTS, OPEN_ACL, "chunks")
    assert isinstance(result, Plan)
    assert result.is_commercial is False
    assert result.commercial_chunks
    assert any("walled individually" in w for w in result.warnings)


def test_clean_document_needs_no_commercial_decision() -> None:
    result = plan(CLEAN_SOP, Corpus.SOP, OPEN_ACL)
    assert isinstance(result, Plan)
    assert result.commercial_chunks == ()
    assert result.is_commercial is False


def test_commercials_only_corpus_never_asks() -> None:
    """`educator` is granted to Senior Manager and Manager only.

    Nobody who may read it lacks commercials rights, so the corpus ACL is
    already the wall and there is no decision left to make.
    """
    result = plan(COMMERCIAL_SOP, Corpus.EDUCATOR, WALLED_ACL)
    assert isinstance(result, Plan)
    assert result.is_commercial is False


def test_contracts_are_flagged_commercial_without_being_asked() -> None:
    """Belt to the corpus ACL's braces, per the `is_commercial` comment in 1600."""
    result = plan(CLEAN_SOP, Corpus.CONTRACTS, WALLED_ACL)
    assert isinstance(result, Plan)
    assert result.is_commercial is True


# --- F-A: insert ordering -----------------------------------------------------


class _RecordingSession:
    """Enough of `AsyncSession` to watch the order things are released in."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def add_all(self, objects: list[object]) -> None:
        self.events.append(type(objects[0]).__name__)

    async def flush(self) -> None:
        self.events.append("flush")


async def test_ordered_flush_releases_parents_before_children() -> None:
    """Without this the child INSERT goes first and Postgres rejects it.

    Reproduced against the live database: `RagIngestor` on a bare session raises
    `ForeignKeyViolation ... rag_chunks_document_id_fkey`, which is why the six
    corpora were empty. Order is the fix, so order is the assertion.
    """
    session = _RecordingSession()
    staged = _OrderedFlush(session)  # type: ignore[arg-type]
    # Added in a deliberately unhelpful order.
    staged.add(RagEmbedding())
    staged.add(RagChunk())
    staged.add(RagDocument())
    await staged.sequence()
    assert [e for e in session.events if e != "flush"] == [
        "RagDocument",
        "RagChunk",
        "RagEmbedding",
    ]


async def test_ordered_flush_flushes_before_releasing_anything() -> None:
    """The leading flush is what lets a contract supersede itself.

    Stamping `superseded_at` on the old row must reach the database before the
    new version's INSERT, or the partial unique index `rag_documents_one_current`
    rejects the pair.
    """
    session = _RecordingSession()
    staged = _OrderedFlush(session)  # type: ignore[arg-type]
    staged.add(RagDocument())
    await staged.sequence()
    assert session.events[0] == "flush"


# --- F-B: the embedding model name --------------------------------------------


class _FakeResponse:
    def __init__(self, vectors: tuple[tuple[float, ...], ...], model: str) -> None:
        self.vectors = vectors
        self.model = model


class _FakeLLM:
    """Stands in for `LLMClient`. Reports a model name of its own choosing."""

    def __init__(self, reports: str, dim: int = EMBEDDING_DIM) -> None:
        self._reports = reports
        self._dim = dim
        self.requested: list[str | None] = []

    async def embed(self, texts: list[str], *, model: str | None = None) -> _FakeResponse:
        self.requested.append(model)
        return _FakeResponse(
            tuple(tuple(0.0 for _ in range(self._dim)) for _ in texts), self._reports
        )


async def test_resolve_embedder_adopts_the_name_the_stack_reports() -> None:
    """Whatever name comes back is the name rows are written and queried under.

    `RagIngestor._embed_missing()` looks up `RagEmbedding.model == embedder.model`
    BEFORE embedding, and `ingest()` stores `embedder.model` AFTER. The probe
    makes both the same string, so re-ingesting an unchanged file finds its
    vectors instead of re-buying them and colliding on `(chunk_id, model)`.
    """
    llm = _FakeLLM(reports="text-embedding-3-small")
    embedder, check = await resolve_embedder("openai/text-embedding-3-small", client=llm)
    assert check.requested == "openai/text-embedding-3-small"
    assert check.canonical == "text-embedding-3-small"
    assert check.drifted is True
    assert embedder.model == "text-embedding-3-small"


async def test_resolve_embedder_reports_no_drift_on_the_shipped_path() -> None:
    """`app/core/llm.py` returns the REQUESTED name, so today the two agree."""
    llm = _FakeLLM(reports="openai/text-embedding-3-small")
    _, check = await resolve_embedder("openai/text-embedding-3-small", client=llm)
    assert check.drifted is False


async def test_resolve_embedder_refuses_a_wrong_width_model() -> None:
    """`rag_embeddings.embedding` is vector(1536); a width change is a migration."""
    llm = _FakeLLM(reports="openai/text-embedding-3-large", dim=3072)
    with pytest.raises(SystemExit):
        await resolve_embedder("openai/text-embedding-3-large", client=llm)


async def test_resolve_embedder_refuses_when_no_model_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is deliberately no default embedding model, and there must not be one."""
    monkeypatch.delenv(EMBEDDING_MODEL_ENV, raising=False)
    with pytest.raises(SystemExit) as exit_info:
        await resolve_embedder(None, client=_FakeLLM(reports="x"))
    assert EMBEDDING_MODEL_ENV in str(exit_info.value)
