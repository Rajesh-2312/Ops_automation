"""Chunk quality, measured against what §9 asks a citation to mean. Offline.

    "Every answer cites source document and section."

`rag_chunks.section` is NOT NULL, so a citation always HAS a section. These tests
ask the harder question: is the section the chunk carries the section the text
actually came from, and is the text a whole thought.

Two failures are demonstrated on shapes that occur in every real SOP and every
real contract:

  F10  a numbered list item is promoted to a heading, so the paragraph after
       the list is attributed to the last bullet and the true section is lost
  F11  overlap is a raw character slice, so a chunk can open mid-word

Neither is caught by `tests/unit/test_rag_chunking.py`, which tests determinism,
hashing and heading recognition on documents that contain no lists and no
oversized paragraphs.
"""

from __future__ import annotations

from app.rag.chunking import ROOT_SECTION, chunk_document, normalise, split_sections
from tests.rag_eval import corpus


def test_a_numbered_list_item_becomes_a_section_and_steals_the_next_paragraph() -> None:
    """FINDING F10. The citation names a section that is not a section.

    `_NUM_HEADING_RE` matches any short line starting with a digit and not ending
    in punctuation — which is exactly the shape of an unpunctuated bullet. The
    guard for prose ("a clause ends in a full stop") does not fire on a list.

    The consequence is worse than a cosmetic label. The paragraph that follows
    the list belongs to "Blocking validations"; it is stored under "3 PAN, bank
    account and IFSC present and well formed", and the true heading disappears
    from the index entirely, so a search for the section by name cannot find it.
    """
    chunks = chunk_document(corpus.CHUNKING_PROBE_TEXT)
    orphan = next(c for c in chunks if c.content.startswith("Each of these is checked"))
    assert orphan.section.startswith("3 PAN, bank account")
    sections = {c.section for c in chunks}
    assert "Blocking validations" in sections  # the heading survives on its own text
    assert orphan.section != "Blocking validations"  # ... but not on this paragraph


def test_a_list_shatters_one_section_into_several() -> None:
    """FINDING F10, the retrieval cost: §7's gate list becomes many sections."""
    text = normalise(corpus.CHUNKING_PROBE_TEXT)
    titles = [section.title for section in split_sections(text)]
    promoted = [t for t in titles if t[0].isdigit()]
    assert promoted, "expected list items to have been promoted to headings"


def test_overlap_can_open_a_chunk_mid_word() -> None:
    """FINDING F11. `previous[-150:].lstrip()` cuts at a character, not a token.

    The opening fragment is both quoted to the user and embedded into the vector
    that decides whether this chunk is retrieved at all.
    """
    chunks = chunk_document(corpus.CHUNKING_PROBE_TEXT)
    continuations = [c for c in chunks if c.section == "Long recital"]
    assert len(continuations) > 1, "the long recital should have been split"
    tail = continuations[1].content
    first_word = tail.split()[0]
    assert first_word == "hedule", f"expected a truncated word, got {first_word!r}"


def test_every_chunk_still_carries_a_section_label() -> None:
    """The schema guarantee holds even where the label is wrong (F10)."""
    for chunk in chunk_document(corpus.CHUNKING_PROBE_TEXT):
        assert chunk.section.strip()


def test_the_evaluation_corpus_itself_chunks_cleanly() -> None:
    """The control: documents without lists or oversized paragraphs are fine.

    Stated so the two findings above are read as "these shapes break it", not as
    "chunking is broken". Every section in the eval corpus fits one chunk, no
    overlap is applied, and every chunk starts on a sentence boundary.
    """
    for spec in corpus.documents():
        for chunk in chunk_document(spec.text):
            assert chunk.section != ROOT_SECTION or spec.text.lstrip().startswith("#") is False
            head = chunk.content.lstrip()
            assert head[0].isupper() or head[0].isdigit(), chunk.content[:60]
