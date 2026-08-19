"""OpenRouter client with task-based model routing.

CLAUDE.md §2: "route by task, not by default." One gateway, two tiers — a cheap
fast model for the volume work (drafting, chase, summaries) and a frontier model
where accuracy is load-bearing (document extraction, governance reports).

Three rules from CLAUDE.md constrain everything in this module:

  R1  The database owns truth; the LLM owns language. Nothing here fetches
      facts. Callers pass structured input in and get prose out.
  R2  No money is computed by an LLM. `app/services/remuneration/` is
      forbidden from importing this module, and the rule linter (L2) enforces
      that at CI time.
  §11 "Log agent I/O — prompt, tools called, tokens, latency — for every
      invocation." `complete()` does this unconditionally.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

import structlog
from openai import AsyncOpenAI

from app.core.config import get_settings
from app.domain.enums import LLMTask, ModelTier

log = structlog.get_logger(__name__)


#: Which tier each task routes to. This is the whole of the routing policy —
#: deliberately a flat table rather than logic, so it can be read at a glance
#: and argued about without reading code.
TASK_TIER: dict[LLMTask, ModelTier] = {
    LLMTask.DRAFTING: ModelTier.VOLUME,
    LLMTask.CHASE: ModelTier.VOLUME,
    LLMTask.SUMMARY: ModelTier.VOLUME,
    LLMTask.EXTRACTION: ModelTier.FRONTIER,
    LLMTask.GOVERNANCE_REPORT: ModelTier.FRONTIER,
}


#: The embedding model name, read from the environment.
#:
#: Read from `os.environ` rather than from `Settings` for exactly the reason
#: `app/core/security.py` gives about `SUPABASE_JWT_SECRET`: that module is owned
#: elsewhere and does not carry the field yet. It belongs on `Settings` beside
#: `openrouter_model_volume`; move it there and delete this constant when config
#: gains the field.
#:
#: There is no default. An embedding model silently defaulting is the one
#: misconfiguration that produces a working system with a corrupted index: the
#: vectors land, the search returns neighbours, and the neighbours are wrong
#: because they were computed under a different model than the corpus. Naming the
#: model is therefore mandatory, and `rag_embeddings.model` is part of that
#: table's primary key so the mistake cannot be made quietly at query time
#: either.
EMBEDDING_MODEL_ENV: Final[str] = "OPENROUTER_EMBEDDING_MODEL"

#: Width of every vector in `rag_embeddings`. Mirrors `app.db.models.EMBEDDING_DIM`
#: and the `vector(1536)` column; a model of a different width needs a migration.
EMBEDDING_DIM: Final[int] = 1536


@dataclass(frozen=True, slots=True)
class EmbeddingResponse:
    """Vectors for a batch of texts, plus what it cost to get them.

    `model` travels with the vectors because it is part of `rag_embeddings`'s
    primary key: two models' vectors are not comparable, and mixing them in one
    nearest-neighbour scan produces plausible, wrong neighbours.
    """

    vectors: tuple[tuple[float, ...], ...]
    model: str
    prompt_tokens: int
    latency_ms: int


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """What a completion returned, plus what it cost to get it."""

    text: str
    model: str
    tier: ModelTier
    task: LLMTask
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMClient:
    """Thin wrapper over the OpenAI-compatible OpenRouter endpoint.

    Deliberately thin: no retry policy, no caching, no prompt templating. Those
    belong to the caller, which knows whether a failed chase message should be
    retried and a failed contract extraction should not.
    """

    def __init__(self, client: AsyncOpenAI | None = None) -> None:
        settings = get_settings()

        # The OpenRouter settings are optional so that Phase 1 — which has no AI
        # in it by design (CLAUDE.md §13) — boots without them. Constructing this
        # class is the moment they stop being optional, so check here and name
        # every missing variable rather than failing later inside an HTTP call.
        missing = [
            name
            for name, value in (
                ("OPENROUTER_API_KEY", settings.openrouter_api_key),
                ("OPENROUTER_MODEL_VOLUME", settings.openrouter_model_volume),
                ("OPENROUTER_MODEL_FRONTIER", settings.openrouter_model_frontier),
            )
            if not value
        ]
        if missing:
            raise RuntimeError(
                "LLMClient requires " + ", ".join(missing) + " in the environment. "
                "Phase 1 is AI-free (CLAUDE.md §13), so these are unset by default; "
                "populate them in .env before using any agent or drafting feature."
            )

        self._models: dict[ModelTier, str] = {
            ModelTier.VOLUME: settings.openrouter_model_volume,
            ModelTier.FRONTIER: settings.openrouter_model_frontier,
        }
        # The `missing` guard above has already raised if this is None. Repeating
        # the check gives the type checker the same guarantee the reader has, and
        # costs nothing — an `assert` would express it too but vanishes under -O,
        # which is exactly when a silently-empty API key would be worst.
        api_key = settings.openrouter_api_key
        if api_key is None:  # pragma: no cover — unreachable via the guard above
            raise RuntimeError("OPENROUTER_API_KEY is unset")

        self._client = client or AsyncOpenAI(
            api_key=api_key.get_secret_value(),
            base_url=settings.openrouter_base_url,
            default_headers={
                # OpenRouter attribution headers; both are optional upstream.
                "HTTP-Referer": settings.openrouter_app_url,
                "X-Title": settings.openrouter_app_title,
            },
        )

    def model_for(self, task: LLMTask) -> str:
        return self._models[TASK_TIER[task]]

    async def complete(
        self,
        task: LLMTask,
        *,
        system: str,
        user: str,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        **kwargs: Any,  # noqa: ANN401  # passthrough to the OpenAI SDK, which types these itself
    ) -> LLMResponse:
        """Run one completion, routed by task, fully logged.

        `task` is required and positional-ish by design. There is no default
        model: every caller must state what kind of work it is doing, which is
        what makes §2's routing rule enforceable rather than aspirational.
        """
        tier = TASK_TIER[task]
        model = self._models[tier]
        started = time.perf_counter()

        response = await self._client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=temperature,
            max_tokens=max_tokens,
            **kwargs,
        )

        latency_ms = int((time.perf_counter() - started) * 1000)
        usage = response.usage
        result = LLMResponse(
            text=response.choices[0].message.content or "",
            model=model,
            tier=tier,
            task=task,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            latency_ms=latency_ms,
        )

        # §11: log every invocation. Prompts are logged at debug because they
        # can contain trainer PII; token counts and latency are always on.
        log.info(
            "llm.completion",
            task=task.value,
            tier=tier.value,
            model=model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            latency_ms=latency_ms,
        )
        log.debug(
            "llm.completion.io",
            task=task.value,
            system=system,
            user=user,
            output=result.text,
        )

        return result

    async def embed(self, texts: Sequence[str], *, model: str | None = None) -> EmbeddingResponse:
        """Embed a batch of texts through the same gateway (CLAUDE.md §2).

        Retrieval is not a task on the `TASK_TIER` table and deliberately does
        not become one. That table routes *generation* between a volume and a
        frontier model, and its whole value is that it is a flat, readable
        policy; an embedding model is not a tier of anything, it is a property of
        the index. Putting it there would mean a corpus's vectors could change
        model as a side effect of a routing edit, and every vector in
        `rag_embeddings` would silently stop being comparable with every query.

        So the model is named explicitly — by the caller, or by
        `OPENROUTER_EMBEDDING_MODEL` — and it is returned on the response so the
        writer stores it alongside the vector.

        Batching is the caller's business. This sends one request per call and
        logs it, because §11 wants token counts and latency for every invocation
        and a hidden internal batcher would report neither honestly.
        """
        resolved = model or os.environ.get(EMBEDDING_MODEL_ENV, "")
        if not resolved:
            raise RuntimeError(
                f"No embedding model configured. Set {EMBEDDING_MODEL_ENV} in the "
                "environment or pass model= explicitly. There is deliberately no "
                "default: a defaulted embedding model produces a corpus whose "
                "vectors were computed under one model and queried under another, "
                "which returns confident, wrong neighbours."
            )
        if not texts:
            return EmbeddingResponse(vectors=(), model=resolved, prompt_tokens=0, latency_ms=0)

        started = time.perf_counter()
        response = await self._client.embeddings.create(model=resolved, input=list(texts))
        latency_ms = int((time.perf_counter() - started) * 1000)

        vectors = tuple(tuple(float(v) for v in item.embedding) for item in response.data)
        for vector in vectors:
            if len(vector) != EMBEDDING_DIM:
                raise RuntimeError(
                    f"Model {resolved!r} returned a {len(vector)}-dimension vector; "
                    f"rag_embeddings.embedding is vector({EMBEDDING_DIM}). Changing "
                    "the width is a migration, not a config change."
                )

        usage = response.usage
        result = EmbeddingResponse(
            vectors=vectors,
            model=resolved,
            prompt_tokens=usage.prompt_tokens if usage else 0,
            latency_ms=latency_ms,
        )
        # §11: log every invocation. Texts themselves are never logged, not even
        # at debug — a batch of chunks is the corpus, and the corpus includes
        # contracts.
        log.info(
            "llm.embedding",
            model=resolved,
            count=len(vectors),
            prompt_tokens=result.prompt_tokens,
            latency_ms=latency_ms,
        )
        return result
