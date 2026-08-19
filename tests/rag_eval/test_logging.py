"""§11's agent-I/O logging, checked against what a deployment actually emits.

    "Log agent I/O — prompt, tools called, tokens, latency — for every
     invocation."

Four things are named. Three of them are on the `copilot.answer` INFO event and
are genuinely emitted on every path, refusals included. The fourth — the prompt —
is logged with `log.debug`, and every deployment of this system runs at INFO
(`app/core/config.py` defaults `log_level` to "INFO"; `.env` sets it to "INFO";
`configure_logging()` builds `make_filtering_bound_logger(INFO)`, which discards
debug entirely rather than merely not rendering it).

So the prompt is never recorded in practice. That is finding F14, and the second
test below is written so that it fails the day the level or the call changes —
in either direction.

The tests use `structlog.testing.capture_logs`, which bypasses the configured
wrapper class, so the level test reconfigures structlog for real and reads what
comes out of the processor chain instead.
"""

from __future__ import annotations

import logging
from uuid import uuid4

import pytest
import structlog

from app.domain.enums import Corpus, LLMTask, Persona
from app.rag.copilot import COPILOT_TOOLS, OpsCopilot
from app.rag.retrieval import RetrievedChunk
from app.rag.scope import RetrievalScope
from tests.rag_eval.conftest import principal, run_async

CHUNK = RetrievedChunk(
    chunk_id=uuid4(),
    document_id=uuid4(),
    corpus=Corpus.SOP,
    title="Trainer Payout Cycle SOP",
    section="Payable days",
    content="For a bCAP engagement payable days are counted down from the period length.",
    version=1,
    is_superseded=False,
    is_commercial=False,
    similarity=0.82,
)


class _Retriever:
    def __init__(self, hits: tuple[RetrievedChunk, ...] = (CHUNK,)) -> None:
        self._hits = hits

    async def retrieve(self, scope, query, **kwargs):  # noqa: ANN001, ANN003, ANN201
        return self._hits


class _Response:
    text = "Payable days are counted down from the period length [1]."
    total_tokens = 431


class _LLM:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def complete(self, task, *, system, user, **kwargs):  # noqa: ANN001, ANN003, ANN201
        self.calls.append({"task": task, "system": system, "user": user})
        return _Response()


def _answer(question: str = "How are payable days counted for a bCAP trainer?"):  # noqa: ANN202
    copilot = OpsCopilot(_Retriever(), _LLM())  # type: ignore[arg-type]
    scope = RetrievalScope.for_principal(principal(Persona.MANAGER))
    return run_async(copilot.answer(scope, question))


def test_every_invocation_logs_tools_tokens_and_latency() -> None:
    with structlog.testing.capture_logs() as logs:
        result = _answer()
    assert result.answered
    events = [entry for entry in logs if entry["event"] == "copilot.answer"]
    assert len(events) == 1
    entry = events[0]
    assert entry["tools_called"] == list(COPILOT_TOOLS)
    assert entry["tokens"] == 431
    assert "latency_ms" in entry
    assert entry["persona"] == Persona.MANAGER.value
    assert entry["sources"], "the chunk ids must be logged; the index moves"


def test_a_refusal_is_logged_as_loudly_as_an_answer() -> None:
    """The refusal counter is §9's most useful operating metric — it must exist."""
    with structlog.testing.capture_logs() as logs:
        result = _answer("How many payable days does the trainer have?")
    assert not result.answered
    entry = next(e for e in logs if e["event"] == "copilot.answer")
    assert entry["answered"] is False
    assert entry["refusal"] == "structured_fact"


def test_the_prompt_is_emitted_at_debug_level() -> None:
    """Half one of finding F14: the event exists, and it is a DEBUG event."""
    with structlog.testing.capture_logs() as logs:
        _answer()
    io_events = [entry for entry in logs if entry["event"] == "copilot.answer.io"]
    assert io_events, "the question should be logged"
    assert io_events[0]["log_level"] == "debug"


def test_the_deployed_log_level_discards_debug_events() -> None:
    """FINDING F14, half two: at INFO the debug event does not merely go unrendered.

    `configure_logging()` installs `make_filtering_bound_logger(INFO)`, whose
    `debug` is a no-op bound at class-construction time — the call is dropped
    before any processor runs, so no sink, no sampling and no local override
    recovers it. `Settings.log_level` defaults to "INFO" and `.env` sets "INFO",
    so §11's "log the prompt" is not satisfied in any deployment as configured.

    Built in isolation rather than by reconfiguring structlog globally:
    `configure_logging` sets `cache_logger_on_first_use=True`, so a second call
    in the same process does not reach loggers that have already been bound.
    """
    from app.core.config import Settings

    assert Settings.model_fields["log_level"].default == "INFO"

    capture = structlog.testing.LogCapture()
    isolated = structlog.wrap_logger(
        None,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        processors=[capture],
    )
    isolated.info("copilot.answer")
    isolated.debug("copilot.answer.io", question="who asked what")

    assert [entry["event"] for entry in capture.entries] == ["copilot.answer"]


def test_what_is_logged_is_the_question_and_not_the_assembled_prompt() -> None:
    """FINDING F14, continued: even at DEBUG the SOURCES block is not recorded here.

    `copilot.answer.io` logs the question alone. The prompt that the model
    actually saw — the numbered sources, the facts block, the system rules — is
    logged by `app/core/llm.py` instead, also at debug. Reconstructing a disputed
    answer therefore needs two debug events from two modules, and gets neither.
    """
    with structlog.testing.capture_logs() as logs:
        _answer()
    io_events = [entry for entry in logs if entry["event"] == "copilot.answer.io"]
    assert io_events, "the debug event should exist under capture_logs"
    assert set(io_events[0]) <= {"event", "log_level", "question"}
    assert "SOURCES" not in io_events[0]["question"]


def test_generation_routes_to_the_volume_tier() -> None:
    """§2: route by task. Summarisation over supplied context is volume work."""
    llm = _LLM()
    copilot = OpsCopilot(_Retriever(), llm)  # type: ignore[arg-type]
    run_async(
        copilot.answer(
            RetrievalScope.for_principal(principal(Persona.MANAGER)),
            "How are payable days counted for a bCAP trainer?",
        )
    )
    assert llm.calls[0]["task"] is LLMTask.SUMMARY


@pytest.mark.parametrize("question", ["How are payable days counted for a bCAP trainer?"])
def test_the_retrieved_chunk_text_reaches_the_prompt_verbatim(question: str) -> None:
    """Context for the injection findings: chunk text is interpolated as-is.

    No escaping, no delimiter, no instruction/data separation. Whatever a
    document says arrives inside the same message as the rules.
    """
    llm = _LLM()
    copilot = OpsCopilot(_Retriever(), llm)  # type: ignore[arg-type]
    run_async(copilot.answer(RetrievalScope.for_principal(principal(Persona.MANAGER)), question))
    assert CHUNK.content in llm.calls[0]["user"]
