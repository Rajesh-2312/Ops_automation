"""The RAG layer behind the Ops Copilot (CLAUDE.md §9, Phase 3).

Six corpora, separately indexed and separately permissioned, in the same Postgres
as everything else — §2 is explicit that this is not a separate vector store,
"because permission filtering must live in one place".

Read `app/rag/scope.py` before adding anything to this package. The one rule that
shapes every module here is §9's "persona filter applies **before** retrieval,
not after generation", and it is enforced structurally: `RagRetriever.retrieve()`
is the only function that returns chunks, it requires a `RetrievalScope` as its
first argument, and the SQL it issues carries the persona in the same statement
as the `ORDER BY ... LIMIT`. Adding a second query path that reads `rag_chunks`
would defeat that, and `tests/unit/test_rag_retrieval.py` fails if one appears.
"""

from __future__ import annotations

from app.rag.chunking import Chunk, chunk_document, normalise, sha256_text
from app.rag.copilot import Citation, CopilotAnswer, OpsCopilot
from app.rag.embeddings import DeterministicEmbedder, Embedder, OpenRouterEmbedder
from app.rag.guards import Refusal, RefusalReason
from app.rag.ingest import DocumentSpec, IngestResult, RagIngestor
from app.rag.retrieval import RagRetriever, RetrievalWallBreach, RetrievedChunk
from app.rag.scope import ALL_CORPORA, RetrievalScope

__all__ = [
    "ALL_CORPORA",
    "Chunk",
    "Citation",
    "CopilotAnswer",
    "DeterministicEmbedder",
    "DocumentSpec",
    "Embedder",
    "IngestResult",
    "OpenRouterEmbedder",
    "OpsCopilot",
    "RagIngestor",
    "RagRetriever",
    "Refusal",
    "RefusalReason",
    "RetrievalScope",
    "RetrievalWallBreach",
    "RetrievedChunk",
    "chunk_document",
    "normalise",
    "sha256_text",
]
