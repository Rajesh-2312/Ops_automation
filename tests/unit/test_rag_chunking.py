"""Chunking: determinism, section carriage, idempotency inputs.

Determinism is not a nice property here, it is what makes ingestion re-runnable
(`app/rag/ingest.py` decides "nothing changed, skip" by comparing hashes). If
these tests fail, every nightly re-ingest re-embeds the whole corpus and churns
the ids that citations resolve against.
"""

from __future__ import annotations

import pytest

from app.rag.chunking import (
    ROOT_SECTION,
    chunk_document,
    normalise,
    sha256_text,
    split_sections,
)

SOP = """\
# Trainer Onboarding

Every trainer must have a signed work order on file before deployment.

## Work orders

The work order names the engagement rate and the validity window. A payout
period outside that window is a blocking validation failure.

## ERM

ERM has no API. The system generates a field pack; a named person pastes it.
"""


# --- determinism -------------------------------------------------------------


def test_chunking_is_byte_identical_across_runs():
    """Same text in, same chunks out — the property ingestion idempotency rests on."""
    first = chunk_document(SOP)
    second = chunk_document(SOP)
    assert first == second
    assert [c.content_hash for c in first] == [c.content_hash for c in second]


def test_chunk_hash_is_sha256_of_content():
    for chunk in chunk_document(SOP):
        assert chunk.content_hash == sha256_text(chunk.content)


def test_line_endings_do_not_change_the_output():
    """A Drive export and a Windows checkout must not re-ingest as a change."""
    assert chunk_document(SOP) == chunk_document(SOP.replace("\n", "\r\n"))


def test_trailing_whitespace_does_not_change_the_output():
    noisy = "\n".join(line + "   " for line in SOP.split("\n"))
    assert chunk_document(SOP) == chunk_document(noisy)


def test_ordinals_are_dense_and_ascending():
    chunks = chunk_document(SOP)
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_appending_a_paragraph_leaves_earlier_hashes_untouched():
    """Greedy packing, so an edit at the end does not re-embed the whole document."""
    before = chunk_document(SOP)
    after = chunk_document(SOP + "\n\n## Travel\n\nReturn legs are booked with the onward leg.\n")
    assert [c.content_hash for c in after[: len(before)]] == [c.content_hash for c in before]


# --- sections ----------------------------------------------------------------


def test_every_chunk_carries_a_non_empty_section():
    """§9 requires a citation to name document AND section; the column is NOT NULL."""
    for chunk in chunk_document(SOP):
        assert chunk.section.strip()


def test_markdown_headings_become_sections():
    sections = {c.section for c in chunk_document(SOP)}
    assert {"Trainer Onboarding", "Work orders", "ERM"} <= sections


def test_numbered_headings_become_sections():
    contract = "4.2 Payment Terms\n\nThe fee is payable within thirty days of invoice.\n"
    assert split_sections(normalise(contract))[0].title == "4.2 Payment Terms"


def test_a_numbered_clause_ending_in_a_full_stop_is_not_a_heading():
    """`1. The trainer shall attend.` is prose. Headings do not end in a full stop."""
    clause = "1. The trainer shall attend every scheduled session."
    assert split_sections(normalise(clause))[0].title == ROOT_SECTION


def test_preamble_before_the_first_heading_is_kept():
    """A preamble is frequently the definitions clause; dropping it loses the terms."""
    text = (
        'In this agreement "Trainer" means the individual named overleaf.\n\n# Scope\n\nDelivery.'
    )
    sections = split_sections(normalise(text))
    assert sections[0].title == ROOT_SECTION
    assert "Trainer" in sections[0].body


def test_overlap_never_crosses_a_section_boundary():
    """Carrying §3's tail into §4 would attach §3's text to §4's citation."""
    text = "# A\n\n" + ("alpha " * 400) + "\n\n# B\n\nbeta text here.\n"
    chunks = chunk_document(text, max_chars=300, overlap_chars=50)
    for chunk in chunks:
        if chunk.section == "B":
            assert "alpha" not in chunk.content


# --- edges -------------------------------------------------------------------


def test_empty_document_yields_no_chunks():
    assert chunk_document("   \n\n  ") == ()


def test_oversized_paragraph_is_split_rather_than_dropped():
    text = "word " * 2000
    chunks = chunk_document(text, max_chars=500, overlap_chars=0)
    assert len(chunks) > 1
    assert all(len(c.content) <= 500 for c in chunks)


def test_unpunctuated_run_is_hard_split_rather_than_raising():
    """Tables and legal recitals arrive without sentence boundaries; ingest them anyway."""
    chunks = chunk_document("x" * 5000, max_chars=400, overlap_chars=0)
    assert len(chunks) == 13


@pytest.mark.parametrize(("max_chars", "overlap"), [(0, 0), (100, 100), (100, -1)])
def test_nonsense_parameters_are_refused(max_chars, overlap):
    with pytest.raises(ValueError):
        chunk_document(SOP, max_chars=max_chars, overlap_chars=overlap)
