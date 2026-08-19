"""The Ops Copilot. CLAUDE.md §8: "RAG Q&A", ceiling **Read-only**.

The ladder in §8 gives every other agent a ceiling of Draft or Alert. This one is
lower still: it has no `save_draft`, no queue, no state to write. Its entire
capability is *retrieve and explain*, and R3's rule about tool binding is honoured
here by there being no tools at all — this module imports a retriever and an LLM
client and nothing that can emit anything anywhere.

WHAT AN ANSWER FROM THIS MODULE IS
----------------------------------
Prose that (a) is grounded in chunks the asker was permitted to retrieve, (b)
cites the document and section of each claim, and (c) contains no figure that was
not handed to it. Anything failing any of the three is not returned in a degraded
form — it is replaced by a `Refusal` (see `app/rag/guards.py`). There is no
partial-credit path, because a partially-grounded answer is indistinguishable
from a grounded one to the person reading it.

THE HYBRID CASE, §9
-------------------
    "Hybrid answers must visibly separate the two."

`answer(..., facts=...)` takes structured values the CALLER read from SQL. They
are returned in their own `facts` block, never merged into the prose by this
module, and the response model keeps them a separate field all the way to the
API. The prose may reference them — that is "explaining a number you were given",
which R2 permits — and the figure check in `guards.py` is what proves the
reference is to a given number rather than a produced one.

§11: every invocation is logged with prompt, tools called, tokens and latency.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import structlog

from app.core.llm import LLMClient
from app.domain.enums import Corpus, LLMTask
from app.rag.guards import (
    Refusal,
    RefusalReason,
    check_citations,
    check_figures,
    is_structured_fact_question,
    structured_fact_refusal,
)
from app.rag.retrieval import DEFAULT_LIMIT, RagRetriever, RetrievedChunk
from app.rag.scope import RetrievalScope

log = structlog.get_logger(__name__)

#: The tools this agent may call, named so the §11 log line and the R3 audit both
#: have something concrete to point at. Retrieval is read-only and there is
#: nothing else — no `save_draft`, no comms queue, no release path. Kept as data
#: rather than as a sentence in the docstring so a future addition is a visible
#: diff on a line that says what it is.
COPILOT_TOOLS: tuple[str, ...] = ("rag.retrieve",)

#: §9's rules, restated to the model. Note carefully what this prompt is and is
#: not: it is an attempt to make the model produce a compliant answer on the
#: first try, so the gates below rarely have to fire. It is NOT the enforcement.
#: Every rule stated here is independently checked in code afterwards, and the
#: answer is discarded if the check fails. If this prompt were deleted the system
#: would get worse answers and would remain exactly as safe.
SYSTEM_PROMPT = """\
You are the byteXL Ops Copilot. You answer questions about internal policy, \
contracts, college history and curriculum, using ONLY the numbered sources given \
to you.

Rules, all of which are checked after you answer:

1. CITE. Every claim ends with the marker of the source it came from, like [1] or \
[3]. An answer with no marker is discarded.
2. NEVER use a source that was not given to you, and never cite a number higher \
than the highest source number.
3. NEVER state a figure — an amount, a count, a date, a percentage — unless that \
exact figure appears in a source or in the question. Do not add, subtract, \
average, convert or round. If a figure is needed and absent, say which system \
holds it instead.
4. If the sources do not answer the question, say so plainly. A short honest \
"the sources do not cover this" is correct and expected; a plausible answer \
without a source is a defect.
5. If a source is marked SUPERSEDED, say so in the sentence that uses it.

Answer in three sentences or fewer unless the question needs a list.\
"""


@dataclass(frozen=True, slots=True)
class Citation:
    """One numbered source, as it is shown to the user.

    `marker` is the `[n]` the prose refers to, one-based, matching the order the
    sources were presented to the model. `document` and `section` are §9's two
    required halves.
    """

    marker: int
    document: str
    section: str
    corpus: Corpus
    version: int
    is_superseded: bool


@dataclass(frozen=True, slots=True)
class CopilotAnswer:
    """What the Copilot returns. Either an answer or a refusal, never both.

    `answered` is not derived from `text` being non-empty: a refusal must be a
    distinct, countable outcome rather than an empty string that a caller might
    render as a blank answer.
    """

    answered: bool
    text: str | None = None
    citations: tuple[Citation, ...] = ()
    #: Structured values supplied by the CALLER from SQL, kept in their own block
    #: so a hybrid answer visibly separates policy from fact (§9).
    facts: Mapping[str, str] = field(default_factory=dict)
    refusal: Refusal | None = None


def _render_sources(hits: Sequence[RetrievedChunk]) -> str:
    """The numbered source block. Numbering is 1-based and is the citation key."""
    blocks = []
    for index, hit in enumerate(hits, start=1):
        flag = " [SUPERSEDED]" if hit.is_superseded else ""
        blocks.append(
            f"[{index}] {hit.title} — {hit.section} (v{hit.version}){flag}\n{hit.content}"
        )
    return "\n\n".join(blocks)


def _render_facts(facts: Mapping[str, str] | None) -> str:
    """Caller-supplied database values, labelled as such.

    Labelled loudly and separately in the prompt as well as in the response,
    because the model must understand these are the ONLY figures it may state,
    and the reader must be able to see which part of the answer came from a query
    rather than from a document (§9).
    """
    if not facts:
        return ""
    lines = "\n".join(f"- {key}: {value}" for key, value in facts.items())
    return (
        "\n\nVALUES FROM THE DATABASE (these are facts of record; you may state "
        f"these figures and no others):\n{lines}"
    )


class OpsCopilot:
    """Read-only RAG Q&A. No write path exists on this class, by construction."""

    def __init__(self, retriever: RagRetriever, llm: LLMClient) -> None:
        self._retriever = retriever
        self._llm = llm

    async def answer(
        self,
        scope: RetrievalScope,
        question: str,
        *,
        corpora: Sequence[Corpus] | None = None,
        limit: int = DEFAULT_LIMIT,
        include_superseded: bool = False,
        facts: Mapping[str, str] | None = None,
    ) -> CopilotAnswer:
        """Answer a question, or refuse. The gates run in this order for a reason.

        1. **Structured fact** — before retrieval and before the model sees
           anything. Refusing here costs nothing, and it means a numeric question
           never even reaches a context window where a plausible number is
           sitting in a retrieved chunk waiting to be read out (R1, §9).
        2. **Retrieve** — persona-scoped; see `app/rag/scope.py`. No sources is a
           refusal, not an unsourced answer.
        3. **Generate** — volume tier. §2 routes by task; this is summarisation
           over supplied context, not extraction from a document, so it does not
           need the frontier model.
        4. **Citations** — §9, no citation → no answer.
        5. **Figures** — R1/R2, no figure the model was not given.

        4 and 5 both run on the finished text, and both DISCARD rather than edit.
        """
        started = time.perf_counter()

        if is_structured_fact_question(question):
            refusal = structured_fact_refusal(question)
            self._log(question, scope, refusal=refusal, hits=(), started=started)
            return CopilotAnswer(answered=False, refusal=refusal)

        hits = await self._retriever.retrieve(
            scope,
            question,
            corpora=corpora,
            limit=limit,
            include_superseded=include_superseded,
        )
        if not hits:
            refusal = Refusal(
                reason=(
                    RefusalReason.NO_CORPUS_ACCESS
                    if not scope.is_internal
                    else RefusalReason.NO_SOURCES
                ),
                message=(
                    "The Copilot is available to byteXL staff only."
                    if not scope.is_internal
                    else (
                        "Nothing in the documents you have access to covers that. The "
                        "Copilot answers only from cited sources (CLAUDE.md §9), so it "
                        "will not guess. If the policy exists but is not indexed, ask "
                        "for the document to be added to the corpus."
                    )
                ),
            )
            self._log(question, scope, refusal=refusal, hits=hits, started=started)
            return CopilotAnswer(answered=False, refusal=refusal)

        user_prompt = (
            f"SOURCES:\n{_render_sources(hits)}"
            f"{_render_facts(facts)}"
            f"\n\nQUESTION: {question}"
        )
        response = await self._llm.complete(
            LLMTask.SUMMARY,
            system=SYSTEM_PROMPT,
            user=user_prompt,
        )

        # Rebound rather than reusing the name above: the two earlier refusals are
        # certain, this one is a maybe, and letting one variable be both types is
        # how a `None` eventually reaches `CopilotAnswer(answered=False)`.
        gate: Refusal | None = check_citations(response.text, len(hits)) or check_figures(
            response.text,
            question=question,
            contexts=[hit.content for hit in hits],
            facts=facts,
        )
        if gate is not None:
            self._log(
                question,
                scope,
                refusal=gate,
                hits=hits,
                started=started,
                tokens=response.total_tokens,
            )
            return CopilotAnswer(answered=False, refusal=gate)

        self._log(
            question, scope, refusal=None, hits=hits, started=started, tokens=response.total_tokens
        )
        return CopilotAnswer(
            answered=True,
            text=response.text,
            citations=tuple(
                Citation(
                    marker=index,
                    document=hit.title,
                    section=hit.section,
                    corpus=hit.corpus,
                    version=hit.version,
                    is_superseded=hit.is_superseded,
                )
                for index, hit in enumerate(hits, start=1)
            ),
            facts=dict(facts or {}),
        )

    @staticmethod
    def _log(
        question: str,
        scope: RetrievalScope,
        *,
        refusal: Refusal | None,
        hits: Sequence[RetrievedChunk],
        started: float,
        tokens: int = 0,
    ) -> None:
        """§11: "Log agent I/O — prompt, tools called, tokens, latency."

        The question goes in at debug and the metadata at info, matching
        `app/core/llm.py`: a question can name a trainer or a college and is
        therefore PII-adjacent, while the refusal reason and the source ids are
        the operational signal and must always be present. The chunk ids are
        logged because "which sources did it answer from" is the first question
        asked when an answer is disputed, and reconstructing it later from a
        similarity search is not possible — the index moves.
        """
        log.info(
            "copilot.answer",
            persona=scope.persona.value,
            tools_called=list(COPILOT_TOOLS),
            answered=refusal is None,
            refusal=refusal.reason.value if refusal else None,
            sources=[str(hit.chunk_id) for hit in hits],
            tokens=tokens,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        log.debug("copilot.answer.io", question=question)
