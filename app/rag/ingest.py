"""Ingestion: document → chunks → embeddings. Deterministic and re-runnable.

The property this module exists to guarantee is stated in one sentence and is
harder than it sounds: **re-ingesting the same document version must change
nothing.** Not "must not corrupt anything" — must be a no-op, down to the row
ids, because those ids are what citations resolve to and what an audit of a
disputed answer is reconstructed from.

Three things make that true, and all three are load-bearing:

  * `app/rag/chunking.py` is a pure function. Same text in, byte-identical chunks
    out, in the same order.
  * `rag_documents.content_hash` is checked BEFORE anything is written. An
    unchanged document exits without touching a row and without spending an
    embedding call, so a nightly re-ingest of the whole corpus is free.
  * Embeddings are inserted only for `(chunk, model)` pairs that do not exist, so
    re-embedding under a new model is additive and leaves the live index serving
    while it runs.

VERSIONING (§9)
---------------
    "Contracts corpus is versioned. A superseded clause must not surface without
     a version flag."

A changed CONTRACT is a new version: the old row is stamped `superseded_at`, its
chunks stay indexed and flagged, and a new row is inserted at `version + 1`. A
July payout dispute is argued against the work order as it read in July, and an
index that silently upgraded itself to the November amendment would answer that
dispute wrongly and with total confidence.

A changed SOP is a replacement: the old chunks are deleted and the version is
kept. The asymmetry is deliberate — nobody argues a dispute against last
quarter's procedure document, and retaining every draft of an SOP would fill the
index with near-duplicate text that competes with itself at retrieval time.
Callers may override per document; the default is the corpus's nature, not a
guess.

NOTHING HERE IS AN AGENT. Ingestion is a service invoked by a job or an endpoint;
it has no LLM in it beyond the embedder, and it makes no judgements about
content. The one classification it does perform — the commercial flag — is a
regex over currency tokens, and it only ever *adds* restriction.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass
from typing import Final
from uuid import UUID

import structlog
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import EMBEDDING_DIM, RagChunk, RagDocument, RagEmbedding
from app.domain.enums import Corpus
from app.rag.chunking import (
    DEFAULT_MAX_CHARS,
    DEFAULT_OVERLAP_CHARS,
    chunk_document,
    normalise,
    sha256_text,
)
from app.rag.embeddings import Embedder

log = structlog.get_logger(__name__)

#: Corpora whose documents are versioned rather than replaced. §9 names Contracts
#: explicitly; nothing else has a reason to retain superseded text, and retaining
#: it everywhere would fill the index with near-duplicates that compete at
#: retrieval time.
VERSIONED_CORPORA: frozenset[Corpus] = frozenset({Corpus.CONTRACTS})

#: Currency and rate tokens that make a chunk commercial (§4). Text about money
#: is walled from an LDE Executive even inside a corpus they may read — which is
#: the case that matters for `reports`, where the delivery narrative is theirs
#: and the margin paragraph is not.
#:
#: This classifier only ever ADDS restriction, and that is what makes a regex an
#: acceptable tool for it. A false positive costs a Manager-only paragraph that
#: could have been shared; a false negative is caught by the document-level flag
#: and, for Contracts, by the corpus-wide ACL in migration 1600. Three layers,
#: because the cheapest one is also the least reliable.
_COMMERCIAL_RE: Final[re.Pattern[str]] = re.compile(r"""(?ix)
    (
        ₹ | \bINR\b | \bRs\.? | \blakh\b | \bcrore\b
      | \bper\s+(day|month)\s+rate\b
      | \b(remuneration|payout|invoice|margin|revenue|commercial\s+terms|fee\s+schedule)\b
      | \brate\s+of\s+(pay|₹|rs)
      | \b(tds|gst)\b
    )
    """)


def looks_commercial(text: str) -> bool:
    """True when a chunk quotes money or commercial terms (§4).

    Not a claim that the figure is correct or current — R1 stands, and the
    Copilot refuses numeric questions outright. It is only a claim about who may
    read the sentence.
    """
    return bool(_COMMERCIAL_RE.search(text))


@dataclass(frozen=True, slots=True)
class DocumentSpec:
    """One document to ingest, as the caller has it.

    `source_ref` is the identity ACROSS versions — a Drive path, a storage object
    key, an SOP number. Two ingests sharing a `source_ref` are two versions of one
    document, which is what makes superseding mechanical rather than a judgement.
    """

    corpus: Corpus
    source_ref: str
    title: str
    text: str
    college_id: UUID | None = None
    program_id: UUID | None = None
    #: Document-level §4 flag. Chunk-level detection can only ADD to this.
    is_commercial: bool = False
    ingested_by: UUID | None = None


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What one ingest did. `unchanged=True` means it did nothing, on purpose."""

    document_id: UUID
    version: int
    unchanged: bool
    chunks_written: int
    embeddings_written: int
    superseded_document_id: UUID | None = None


class RagIngestor:
    """Writes documents into the corpora. Service-role only.

    There is no browser write path to any `rag_*` table (migration 1600 grants
    SELECT and nothing else), because a corpus that can be written from the
    browser is a corpus into which anyone can plant an authoritative-looking SOP
    — and §9's answers cite these rows, so a forged row is a forged citation.
    """

    def __init__(self, session: AsyncSession, embedder: Embedder) -> None:
        self._session = session
        self._embedder = embedder

    async def ingest(
        self,
        spec: DocumentSpec,
        *,
        max_chars: int = DEFAULT_MAX_CHARS,
        overlap_chars: int = DEFAULT_OVERLAP_CHARS,
        versioned: bool | None = None,
    ) -> IngestResult:
        """Ingest or re-ingest one document. Idempotent on an unchanged version.

        Does NOT commit. The caller owns the transaction, so a batch ingest of a
        folder either lands whole or not at all — a half-ingested contract whose
        clauses 1-6 are indexed and 7-12 are not is worse than no contract at
        all, because it retrieves confidently and answers incompletely.
        """
        normalised = normalise(spec.text)
        content_hash = sha256_text(normalised)
        now = dt.datetime.now(dt.UTC)

        current = (
            await self._session.execute(
                select(RagDocument).where(
                    RagDocument.corpus == spec.corpus,
                    RagDocument.source_ref == spec.source_ref,
                    RagDocument.superseded_at.is_(None),
                )
            )
        ).scalar_one_or_none()

        # --- unchanged: the whole point of the hash column --------------------
        if current is not None and current.content_hash == content_hash:
            written = await self._embed_missing(current.id)
            log.info(
                "rag.ingest.unchanged",
                corpus=spec.corpus.value,
                source_ref=spec.source_ref,
                version=current.version,
                embeddings_written=written,
            )
            return IngestResult(
                document_id=current.id,
                version=current.version,
                unchanged=written == 0,
                chunks_written=0,
                embeddings_written=written,
            )

        use_versioning = spec.corpus in VERSIONED_CORPORA if versioned is None else versioned
        superseded_id: UUID | None = None
        version = 1

        if current is not None:
            if use_versioning:
                # §9: retain and flag. The old row keeps its chunks and its
                # embeddings; `rag_search()` excludes it unless asked and flags it
                # when returned.
                current.superseded_at = now
                superseded_id = current.id
                version = current.version + 1
            else:
                # Replace in place. Deleting the chunks cascades to their
                # embeddings, so no orphan vector survives to be matched against.
                await self._session.execute(
                    delete(RagChunk).where(RagChunk.document_id == current.id)
                )
                current.content_hash = content_hash
                current.title = spec.title
                current.is_commercial = spec.is_commercial
                current.updated_at = now

        document_id = current.id if (current is not None and not use_versioning) else uuid.uuid4()

        if current is None or use_versioning:
            self._session.add(
                RagDocument(
                    id=document_id,
                    corpus=spec.corpus,
                    source_ref=spec.source_ref,
                    title=spec.title,
                    version=version,
                    superseded_at=None,
                    college_id=spec.college_id,
                    program_id=spec.program_id,
                    is_commercial=spec.is_commercial,
                    content_hash=content_hash,
                    ingested_at=now,
                    ingested_by=spec.ingested_by,
                    updated_at=now,
                )
            )
        else:
            version = current.version

        chunks = chunk_document(normalised, max_chars=max_chars, overlap_chars=overlap_chars)
        chunk_ids: list[UUID] = []
        for chunk in chunks:
            chunk_id = uuid.uuid4()
            chunk_ids.append(chunk_id)
            self._session.add(
                RagChunk(
                    id=chunk_id,
                    document_id=document_id,
                    ordinal=chunk.ordinal,
                    section=chunk.section,
                    content=chunk.content,
                    content_hash=chunk.content_hash,
                    # OR, never override: a document flagged commercial keeps
                    # every chunk commercial regardless of what the regex sees.
                    is_commercial=spec.is_commercial or looks_commercial(chunk.content),
                    created_at=now,
                )
            )

        embeddings_written = 0
        if chunks:
            vectors = await self._embedder.embed([chunk.content for chunk in chunks])
            if len(vectors) != len(chunks):
                raise RuntimeError(
                    f"Embedder returned {len(vectors)} vectors for {len(chunks)} chunks. "
                    "A partial batch would silently mis-pair vectors with text, which "
                    "produces confident wrong retrieval; refusing to write any."
                )
            for chunk_id, vector in zip(chunk_ids, vectors, strict=True):
                self._session.add(
                    RagEmbedding(
                        chunk_id=chunk_id,
                        model=self._embedder.model,
                        dim=EMBEDDING_DIM,
                        embedding=list(vector),
                        created_at=now,
                    )
                )
                embeddings_written += 1

        log.info(
            "rag.ingest",
            corpus=spec.corpus.value,
            source_ref=spec.source_ref,
            version=version,
            versioned=use_versioning,
            superseded=str(superseded_id) if superseded_id else None,
            chunks_written=len(chunks),
            embeddings_written=embeddings_written,
            model=self._embedder.model,
        )
        return IngestResult(
            document_id=document_id,
            version=version,
            unchanged=False,
            chunks_written=len(chunks),
            embeddings_written=embeddings_written,
            superseded_document_id=superseded_id,
        )

    async def _embed_missing(self, document_id: UUID) -> int:
        """Fill in vectors for chunks that have none under the current model.

        This is the backfill path after an embedding-model change: the text is
        unchanged, so the expensive re-chunk is skipped and only the missing
        `(chunk, model)` pairs are computed. It is also why re-ingesting an
        unchanged document is not *quite* a no-op the first time it runs under a
        new model — and `IngestResult.unchanged` reports that honestly rather
        than claiming nothing happened.
        """
        rows = (
            (
                await self._session.execute(
                    select(RagChunk)
                    .outerjoin(
                        RagEmbedding,
                        (RagEmbedding.chunk_id == RagChunk.id)
                        & (RagEmbedding.model == self._embedder.model),
                    )
                    .where(RagChunk.document_id == document_id, RagEmbedding.chunk_id.is_(None))
                    .order_by(RagChunk.ordinal)
                )
            )
            .scalars()
            .all()
        )
        if not rows:
            return 0

        now = dt.datetime.now(dt.UTC)
        vectors = await self._embedder.embed([row.content for row in rows])
        for row, vector in zip(rows, vectors, strict=True):
            self._session.add(
                RagEmbedding(
                    chunk_id=row.id,
                    model=self._embedder.model,
                    dim=EMBEDDING_DIM,
                    embedding=list(vector),
                    created_at=now,
                )
            )
        return len(rows)
