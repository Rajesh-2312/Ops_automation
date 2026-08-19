"""Embedders. One protocol, two implementations, no direct SDK calls.

CLAUDE.md §2 makes OpenRouter the sole gateway and `app/core/llm.py` the only
module that talks to it. This module therefore contains no HTTP client of its
own: `OpenRouterEmbedder` delegates to `LLMClient.embed()`, which is where the
§11 logging of tokens and latency lives.

WHY THERE IS A DETERMINISTIC OFFLINE EMBEDDER
---------------------------------------------
Not as a toy. It exists so the whole retrieval path — chunking, ingestion, the
persona filter, citation enforcement, the structured-fact refusal — can be
exercised end to end in a unit test with no network, no API key and no cost, and
so the tests assert the *filtering*, which is the part that must never regress.
It is a hashing embedder: it captures lexical overlap and nothing else, so it is
useless for semantic retrieval and is never a fallback in production. Selecting
an embedder is an explicit argument at every call site for exactly that reason —
there is no "default embedder" that could quietly become the production one.

Vectors are L2-normalised on both paths, which is what lets the `vector_cosine_ops`
HNSW index in migration 1600 and the `1 - (a <=> b)` similarity it reports agree
with each other and read as "1.0 identical, 0.0 unrelated".
"""

from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Sequence
from typing import Final, Protocol, runtime_checkable

from app.core.llm import EMBEDDING_DIM, LLMClient

#: Word tokens for the deterministic embedder. Unicode-aware, digits included.
_TOKEN_RE: Final[re.Pattern[str]] = re.compile(r"\w+", re.UNICODE)


@runtime_checkable
class Embedder(Protocol):
    """Anything that turns text into vectors of `EMBEDDING_DIM` floats.

    `model` is not decoration: it is written to `rag_embeddings.model`, which is
    part of that table's primary key, and it is passed back into
    `public.rag_search()` so a query is only ever compared against vectors from
    the same model.
    """

    @property
    def model(self) -> str: ...

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]: ...


def _normalise(values: list[float]) -> tuple[float, ...]:
    """L2-normalise, mapping the zero vector to itself.

    The zero vector arises from text with no word characters at all. Returning it
    unchanged rather than raising keeps the embedder total; such a chunk cannot
    reach here anyway, because `rag_chunks_content_ck` rejects empty content.
    """
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return tuple(values)
    return tuple(v / norm for v in values)


class DeterministicEmbedder:
    """Offline hashing embedder. Same text in, identical vector out, forever.

    Each token is hashed with sha256 — NOT with `hash()`, whose seed is
    randomised per process, which would make the "deterministic" in the class
    name false across restarts and make an ingest run produce different vectors
    from the run that verified it.

    Signed contributions: the top bit of the digest flips the sign, so two
    different tokens landing in the same dimension are as likely to cancel as to
    reinforce. Unsigned hashing makes every vector point into the positive
    orthant and pushes every pairwise cosine towards 1, which would make the
    persona-filter tests pass for the wrong reason (everything matches
    everything).
    """

    def __init__(self, dim: int = EMBEDDING_DIM) -> None:
        self._dim = dim
        self._model = f"deterministic-sha256-{dim}"

    @property
    def model(self) -> str:
        return self._model

    def embed_one(self, text: str) -> tuple[float, ...]:
        values = [0.0] * self._dim
        for token in _TOKEN_RE.findall(text.lower()):
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:4], "big") % self._dim
            sign = 1.0 if digest[4] & 0x80 else -1.0
            values[index] += sign
        return _normalise(values)

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        return tuple(self.embed_one(text) for text in texts)


class OpenRouterEmbedder:
    """Production embedder. Delegates to the one gateway (§2).

    The model name is resolved once at construction and then fixed, so a batch
    cannot be half-embedded under one model and half under another if the
    environment changes mid-run.
    """

    def __init__(self, client: LLMClient, *, model: str | None = None) -> None:
        self._client = client
        self._model = model

    @property
    def model(self) -> str:
        if self._model is None:
            raise RuntimeError(
                "OpenRouterEmbedder was constructed without an explicit model and "
                "has not embedded anything yet, so the model name is not known. "
                "Pass model= at construction when the name is needed up front."
            )
        return self._model

    async def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        response = await self._client.embed(texts, model=self._model)
        # Pin the resolved name so `model` is answerable afterwards and so the
        # rest of the batch cannot drift.
        self._model = response.model
        return response.vectors
