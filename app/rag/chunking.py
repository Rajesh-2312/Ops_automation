"""Deterministic, section-aware chunking.

CLAUDE.md §9 requires every answer to cite "source document and section". That
sentence is the whole design brief for this module: a chunker that loses the
heading it came from makes the citation rule unenforceable downstream, because
the only thing left to cite with is the model's memory of what it just read —
and a model asked to cite will invent "SOP-14, §3.2" without hesitating.

So sections are carried, not inferred, and `rag_chunks.section` is NOT NULL in
the schema to make that structural.

DETERMINISM IS A REQUIREMENT, NOT A NICETY
------------------------------------------
`chunk_document()` is a pure function: same text in, byte-identical chunks out,
in the same order, with the same hashes. Ingestion depends on it —
`app/rag/ingest.py` decides "nothing changed, skip" by comparing hashes, and a
chunker with any nondeterminism (dict iteration over a mutable set, a regex over
a set literal, a timestamp in the hash) would make every scheduled re-ingest
re-chunk and re-embed the entire corpus. That is not just wasteful; it churns the
ids that citations resolve against.

Nothing here does any I/O, imports no model, and knows nothing about embeddings.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

#: Target chunk size in characters, not tokens. Characters because the boundary
#: has to be computable without a tokenizer — a tokenizer is a model-specific
#: dependency, and a chunker that changes its output when the model changes stops
#: being idempotent across an embedding upgrade. ~1200 chars is roughly 300
#: tokens, which keeps eight retrieved chunks comfortably inside a volume-tier
#: context window with room for the question and the citation instructions.
DEFAULT_MAX_CHARS: Final[int] = 1200

#: Trailing characters of the previous chunk repeated at the head of the next.
#: Overlap exists because the sentence that answers a question is frequently the
#: one that straddles a boundary. Kept small: overlap is duplicated text in the
#: index, and duplicated text retrieves twice and reads as corroboration when it
#: is actually one source said once.
DEFAULT_OVERLAP_CHARS: Final[int] = 150

#: The section label used when a document has no headings at all. Not NULL and
#: not empty — see the module docstring and the `rag_chunks_section_ck` CHECK.
ROOT_SECTION: Final[str] = "Document"

#: Markdown ATX headings: `# Title`, `### Title`.
_MD_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^(#{1,6})\s+(?P<title>\S.*?)\s*#*$")

#: Numbered legal/SOP headings: `3. Scope`, `4.2 Payment terms`, `12.3.1 Notice`.
#: Anchored and requiring a title after the number so a list item ("1. buy milk")
#: inside a paragraph does not silently become a section — the number must be
#: followed by a capitalised or titled fragment and the line must be short.
_NUM_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<number>\d+(?:\.\d+){0,3})\.?\s+(?P<title>\S.{0,78})$"
)

#: Paragraph break: one or more blank lines.
_PARA_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"\n\s*\n")

#: Sentence boundary, used only when a single paragraph exceeds the chunk size.
_SENTENCE_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True, slots=True)
class Chunk:
    """One retrievable unit, exactly as `rag_chunks` stores it.

    Frozen: a chunk is evidence for a citation, and evidence that can be edited
    between chunking and storage is not evidence.
    """

    ordinal: int
    section: str
    content: str
    content_hash: str


def normalise(text: str) -> str:
    """Canonical form of a source document.

    Applied before hashing and before chunking, so a document that differs only
    in line endings or trailing whitespace hashes identically and re-ingests as a
    no-op. This is the function that makes "idempotent on re-ingest of the same
    document version" true in practice rather than in principle — Windows and
    Drive exports disagree about `\\r\\n` constantly.
    """
    unified = text.replace("\r\n", "\n").replace("\r", "\n")
    stripped_lines = [line.rstrip() for line in unified.split("\n")]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(stripped_lines))
    return collapsed.strip()


def sha256_text(text: str) -> str:
    """Hex sha256 of `text` as UTF-8. One definition, used for both hash columns."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _heading_of(line: str) -> str | None:
    """The section title this line declares, or None if it is body text."""
    md = _MD_HEADING_RE.match(line)
    if md is not None:
        return md.group("title").strip()
    num = _NUM_HEADING_RE.match(line)
    if num is not None:
        title = num.group("title").strip()
        # A numbered line whose remainder ends in a sentence terminator is prose
        # ("1. The trainer shall attend."), not a heading. Headings do not end in
        # a full stop; clauses do. Cheap, and it removes the common false
        # positive that would otherwise scatter one-line sections through a
        # contract.
        if title.endswith((".", ";", ",", ":")):
            return None
        return f"{num.group('number')} {title}"
    return None


@dataclass(frozen=True, slots=True)
class _Section:
    title: str
    body: str


def split_sections(text: str) -> tuple[_Section, ...]:
    """Split normalised text into (heading, body) pairs, in document order.

    Text appearing before the first heading is kept under `ROOT_SECTION` rather
    than discarded — a preamble is frequently the definitions clause, and
    dropping it would make the defined terms uncitable.
    """
    lines = text.split("\n")
    sections: list[_Section] = []
    title = ROOT_SECTION
    buffer: list[str] = []

    for line in lines:
        heading = _heading_of(line)
        if heading is None:
            buffer.append(line)
            continue
        body = "\n".join(buffer).strip()
        if body:
            sections.append(_Section(title=title, body=body))
        buffer = []
        title = heading

    body = "\n".join(buffer).strip()
    if body:
        sections.append(_Section(title=title, body=body))
    return tuple(sections)


def _hard_split(paragraph: str, max_chars: int) -> list[str]:
    """Break one oversized paragraph, preferring sentence boundaries.

    Falls through to a character split only when a single sentence is longer than
    the limit — which happens, in tables and in un-punctuated legal recitals, and
    a chunker that raises on it would be unable to ingest a real contract.
    """
    pieces: list[str] = []
    current = ""
    for sentence in _SENTENCE_SPLIT_RE.split(paragraph):
        candidate = f"{current} {sentence}".strip() if current else sentence
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            pieces.append(current)
        while len(sentence) > max_chars:
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        current = sentence
    if current:
        pieces.append(current)
    return pieces


def _pack(paragraphs: list[str], max_chars: int) -> list[str]:
    """Greedily pack paragraphs into chunks of at most `max_chars`.

    Greedy rather than balanced on purpose: greedy is stable under an edit at the
    end of a document, so appending a paragraph re-chunks the tail and leaves
    every earlier chunk hash untouched. A balanced packer redistributes
    everything and would re-embed the whole document for a one-line addition.
    """
    packed: list[str] = []
    current = ""
    for paragraph in paragraphs:
        for piece in (
            _hard_split(paragraph, max_chars) if len(paragraph) > max_chars else [paragraph]
        ):
            candidate = f"{current}\n\n{piece}" if current else piece
            if len(candidate) <= max_chars:
                current = candidate
            else:
                if current:
                    packed.append(current)
                current = piece
    if current:
        packed.append(current)
    return packed


def chunk_document(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap_chars: int = DEFAULT_OVERLAP_CHARS,
) -> tuple[Chunk, ...]:
    """Split a document into citable chunks. Pure, deterministic, re-runnable.

    Ordinals are assigned across the whole document rather than per section, so
    `(document_id, ordinal)` is a stable address — that pair is the unique index
    in `rag_chunks` and the thing a citation ultimately resolves to.

    Overlap is applied only WITHIN a section. Carrying the tail of §3 into §4
    would attach §3's text to §4's citation, which is precisely the kind of quiet
    mis-attribution §9's citation rule exists to prevent.

    An empty or whitespace-only document yields no chunks — deliberately, rather
    than one empty chunk. A document with nothing in it must not be retrievable,
    and `rag_chunks_content_ck` would reject it anyway.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("overlap_chars must be >= 0 and < max_chars")

    normalised = normalise(text)
    if not normalised:
        return ()

    chunks: list[Chunk] = []
    ordinal = 0
    for section in split_sections(normalised):
        paragraphs = [p.strip() for p in _PARA_SPLIT_RE.split(section.body) if p.strip()]
        pieces = _pack(paragraphs, max_chars)
        previous: str | None = None
        for piece in pieces:
            content = piece
            if previous is not None and overlap_chars:
                content = f"{previous[-overlap_chars:].lstrip()}\n\n{piece}"
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    section=section.title,
                    content=content,
                    content_hash=sha256_text(content),
                )
            )
            ordinal += 1
            previous = piece
    return tuple(chunks)
