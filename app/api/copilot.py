"""The Ops Copilot HTTP surface — read-only, cited, persona-scoped.

    POST /copilot/ask
    GET  /copilot/corpora

CLAUDE.md §8 gives this agent a ceiling of **Read-only**, and this router is
where that ceiling is visible: there is no POST that writes anything, no draft to
save, no queue to enqueue on. R3's "agent tool sets contain read and `save_draft`
only" is satisfied here by the stronger statement — the Copilot has no
`save_draft` either.

AUTHORISATION — WHY THIS FILE RE-CHECKS EVERYTHING
==================================================
We are on the service-role connection, which carries BYPASSRLS, so every policy
in migration 1600 steps aside (`app/core/security.py`). The persona filter that
the database would apply for a browser session is applied here instead, and it is
applied by *construction* rather than by a check the handler could forget:
`RetrievalScope.for_principal()` is the only way a scope is made, and
`RagRetriever.retrieve()` will not run without one. See `app/rag/scope.py`.

`require_internal()` still runs first, before any of that. Not redundant: it
turns a trainer's request into a 403 at the door rather than into an empty
result, and an empty result is indistinguishable from "the corpus does not cover
this", which would send the wrong person looking for the wrong bug.

NO AUDIT EVENT, DELIBERATELY
============================
§11 requires an `AuditEvent` for every *state transition*. A question is not one:
nothing here writes a row, and the append-only audit table is the record of
decisions, not a query log. What §11 does require for this path is agent I/O
logging — prompt, tools called, tokens, latency — and `app/rag/copilot.py` emits
exactly that on every invocation, refusals included.

Registered in `app/main.py`. This line once read "NOT REGISTERED" and was
left behind when registration happened, which is worse than no note at all:
a reader who trusts it concludes `/copilot/ask` 404s and goes looking for a
bug that is not there. If this router is ever unregistered, delete this
paragraph rather than inverting it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.llm import LLMClient
from app.core.security import CurrentPrincipal, require_internal
from app.db.models import RagCorpusAccess
from app.db.session import get_session
from app.domain.enums import Corpus
from app.rag.copilot import OpsCopilot
from app.rag.embeddings import OpenRouterEmbedder
from app.rag.guards import RefusalReason
from app.rag.retrieval import DEFAULT_LIMIT, RagRetriever
from app.rag.scope import RetrievalScope

router = APIRouter(prefix="/copilot", tags=["copilot"])

Session = Annotated[AsyncSession, Depends(get_session)]

#: Retrieval breadth ceiling. Not a tuning knob for callers to raise: past
#: roughly this many chunks the marginal source mostly dilutes, and the model
#: starts citing the weakest thing it was handed because it is present.
MAX_LIMIT: int = 20


def get_copilot(session: Session) -> OpsCopilot:
    """Build the Copilot for one request. Overridable in tests.

    Constructed per request because the retriever holds the request's session —
    retrieval frequently runs inside a transaction that has already begun, and a
    second connection would read a different snapshot mid-answer.

    A missing OpenRouter configuration surfaces as 503, not 500. Phase 1 is
    AI-free by design (§13) and boots without those variables, so an unconfigured
    deployment is a *deployment state* rather than a bug — 503 says "this
    capability is not turned on here", which is both true and actionable.
    """
    try:
        client = LLMClient()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"The Ops Copilot is not configured on this deployment: {exc}",
        ) from exc
    return OpsCopilot(RagRetriever(session, OpenRouterEmbedder(client)), client)


Copilot = Annotated[OpsCopilot, Depends(get_copilot)]


# --- wire models --------------------------------------------------------------


class AskRequest(BaseModel):
    """A question, and optionally which corpora to search.

    `corpora` narrows; it never widens. The ACL in `rag_corpus_access` is
    intersected with it inside `public.rag_search()`, so naming a corpus the
    caller does not hold yields zero rows from that corpus rather than an error —
    an error would confirm the corpus exists and who holds it.

    There is deliberately no `facts` field on the wire. The structured values in
    a hybrid answer (§9) come from a SQL query this service runs on the caller's
    behalf, never from the client: a client-supplied "fact" is an unverified
    number that the figure check in `app/rag/guards.py` would then bless as
    grounded, which inverts R1 exactly.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=2000)
    corpora: list[Corpus] | None = Field(
        default=None, description="Narrow the search. Never widens beyond your access."
    )
    limit: int = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)
    include_superseded: bool = Field(
        default=False,
        description=(
            "Include superseded contract versions. They are always flagged (§9); "
            "this only controls whether they are searched at all."
        ),
    )


class CitationOut(BaseModel):
    """§9's two required halves, plus the version flag."""

    marker: int
    document: str
    section: str
    corpus: Corpus
    version: int
    is_superseded: bool


class RefusalOut(BaseModel):
    reason: RefusalReason
    message: str


class AskResponse(BaseModel):
    """An answer or a refusal — never a degraded blend of the two.

    `answered` is an explicit field rather than something a client infers from
    `answer` being null, so a refusal renders as a refusal and is countable. The
    proportion refused as `structured_fact` is the platform's running list of the
    SQL views it still owes its users.

    `facts` is a separate block by design. §9: "Hybrid answers must visibly
    separate the two" — policy prose in `answer`, values of record in `facts`,
    never merged by this service.
    """

    answered: bool
    answer: str | None = None
    citations: list[CitationOut] = Field(default_factory=list)
    facts: dict[str, str] = Field(default_factory=dict)
    refusal: RefusalOut | None = None


class CorpusAccessOut(BaseModel):
    corpus: Corpus
    rationale: str


# --- endpoints ----------------------------------------------------------------


@router.post(
    "/ask",
    response_model=AskResponse,
    status_code=status.HTTP_200_OK,
    summary="Ask the Ops Copilot a policy question. Cited, or refused.",
)
async def ask(
    body: AskRequest,
    principal: CurrentPrincipal,
    copilot: Copilot,
) -> AskResponse:
    """Answer from the corpora the caller may read, with citations, or refuse.

    Returns **200 with `answered: false`** on a refusal rather than a 4xx. A
    refusal is a correct, expected outcome of a well-formed request — "that
    number comes from the tracksheet, not from a document" is the system working
    — and mapping it onto an HTTP error would make clients treat the single most
    important behaviour in §9 as a failure to retry around.
    """
    require_internal(principal)

    result = await copilot.answer(
        RetrievalScope.for_principal(principal),
        body.question,
        corpora=body.corpora,
        limit=body.limit,
        include_superseded=body.include_superseded,
    )
    return AskResponse(
        answered=result.answered,
        answer=result.text,
        citations=[
            CitationOut(
                marker=c.marker,
                document=c.document,
                section=c.section,
                corpus=c.corpus,
                version=c.version,
                is_superseded=c.is_superseded,
            )
            for c in result.citations
        ],
        facts=dict(result.facts),
        refusal=(
            RefusalOut(reason=result.refusal.reason, message=result.refusal.message)
            if result.refusal
            else None
        ),
    )


@router.get(
    "/corpora",
    response_model=list[CorpusAccessOut],
    summary="Which corpora this caller may search, and why",
)
async def my_corpora(principal: CurrentPrincipal, session: Session) -> list[CorpusAccessOut]:
    """The caller's own row set from `rag_corpus_access`.

    Exposed because a Copilot that silently searches four of six corpora is
    indistinguishable from one whose index is missing two, and the person who
    cannot tell will file the wrong bug. The `rationale` column is returned
    verbatim: it is written for exactly this reading.

    Scoped to the caller's own persona, not to any persona they name. This
    endpoint answers "what can I see", never "what can they see" — the latter is
    an access review, which belongs to an admin surface with its own audit line.
    """
    require_internal(principal)
    rows = (
        (
            await session.execute(
                select(RagCorpusAccess)
                .where(RagCorpusAccess.role == principal.persona)
                .order_by(RagCorpusAccess.corpus)
            )
        )
        .scalars()
        .all()
    )
    return [CorpusAccessOut(corpus=row.corpus, rationale=row.rationale) for row in rows]
