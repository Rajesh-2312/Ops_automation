"""Tests for `app.services.approval.hashing` — the R4 freeze.

CLAUDE.md R4: "Approval freezes and hashes the version." The property under test
throughout is the one that makes a freeze meaningful: **the same logical content
hashes identically, anywhere, and different content never does.**

`test_golden_digest_is_pinned` is the load-bearing one. The canonicalisation is a
wire format — change it and every stored `content_hash` in the database becomes
unverifiable — so it is pinned to a literal. If that test fails, the question is
not "what is the new digest" but "who is re-freezing every approved artifact".
"""

from __future__ import annotations

import datetime as dt
import os
import subprocess
import sys
from decimal import Decimal, localcontext
from enum import Enum
from pathlib import Path
from uuid import UUID

import pytest

from app.domain.enums import ArtifactState, Persona, ProgramType
from app.services.approval.hashing import (
    CANONICALISATION_VERSION,
    CanonicalisationError,
    canonical_json,
    canonical_preimage,
    content_hash,
)

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))

#: A payload shaped like a real remuneration artifact: money as Decimal, dates,
#: an enum, a nested row list, a UUID, an aware timestamp.
GOLDEN_PAYLOAD: dict[str, object] = {
    "program_type": ProgramType.BCAP,
    "period_start": dt.date(2026, 7, 26),
    "period_end": dt.date(2026, 7, 31),
    "days_in_month": 31,
    "generated_at": dt.datetime(2026, 8, 1, 6, 30, tzinfo=dt.UTC),
    "cycle_id": UUID("11111111-2222-3333-4444-555555555555"),
    "rows": [
        {
            "pan": "BCDPS1234K",
            "invoice_number": "BCDP/26-27/JUL1",
            "earned": Decimal("15483.87"),
            "tds": Decimal("1548.39"),
            "net": Decimal("14035"),
            "attendance_complete": True,
            "variance_reason": None,
        }
    ],
}

#: sha256 over the canonical preimage of GOLDEN_PAYLOAD, canonicalisation v1.
GOLDEN_DIGEST = "7205f51735f583d106c6f0ff3cce83727e6d996ffa0f4470c03dcc03edefd7e2"


def test_golden_digest_is_pinned() -> None:
    """The canonical form is a wire format, not an implementation detail.

    A change here invalidates every `content_hash` already written against an
    APPROVED or RELEASED artifact. That is a migration with a governance
    decision attached (re-freeze, or mark the old rows "frozen under v1"), so it
    must never happen as a side effect of a refactor.
    """
    assert content_hash(GOLDEN_PAYLOAD) == GOLDEN_DIGEST
    assert CANONICALISATION_VERSION == "1"


def test_preimage_is_domain_separated_and_ascii() -> None:
    preimage = canonical_preimage(GOLDEN_PAYLOAD)
    assert preimage.startswith(b"bytexl.approval.canonical.v1\n")
    preimage.decode("ascii")  # raises if any byte escaped the ASCII guarantee


# --- the same logical content hashes identically -----------------------------


def test_stable_across_two_independent_constructions() -> None:
    """Key order, trailing rupee zeros and enum-vs-stored-value must not matter.

    This is the freeze's core promise: a payload rebuilt from a database row
    (plain strings, `Decimal("14035.00")` out of a NUMERIC column) is the same
    artifact as the one built from domain objects.
    """
    from_domain = {
        "program_type": ProgramType.BCAP,
        "state": ArtifactState.APPROVED,
        "net": Decimal("14035"),
        "approver": Persona.SENIOR_MANAGER,
    }
    from_database = {
        "approver": "senior_manager",
        "net": Decimal("14035.00"),
        "state": "APPROVED",
        "program_type": "bCAP",
    }
    assert content_hash(from_domain) == content_hash(from_database)


def test_nested_key_order_is_irrelevant() -> None:
    first = {"rows": [{"a": 1, "b": 2}], "meta": {"x": "1", "y": "2"}}
    second = {"meta": {"y": "2", "x": "1"}, "rows": [{"b": 2, "a": 1}]}
    assert content_hash(first) == content_hash(second)


def test_sequence_order_is_content() -> None:
    """Rows are ordered. Two trainers swapping places is a different sheet."""
    assert content_hash({"rows": [1, 2]}) != content_hash({"rows": [2, 1]})


def test_equal_instants_in_different_zones_hash_alike() -> None:
    """§11 stores UTC; a value read back in IST is the same instant."""
    utc = {"at": dt.datetime(2026, 8, 1, 6, 30, tzinfo=dt.UTC)}
    ist = {"at": dt.datetime(2026, 8, 1, 12, 0, tzinfo=IST)}
    assert content_hash(utc) == content_hash(ist)


def test_list_and_tuple_are_the_same_sequence() -> None:
    assert content_hash({"rows": [1, 2]}) == content_hash({"rows": (1, 2)})


# --- different content never collides ----------------------------------------


@pytest.mark.parametrize(
    ("changed", "why"),
    [
        ({"net": Decimal("14035.01")}, "one paise"),
        ({"net": Decimal("-14035")}, "sign"),
        ({"net": 14035}, "int is not Decimal"),
        ({"net": "14035"}, "str is not Decimal"),
        ({"net": True}, "bool is not the number one"),
        ({"net": None}, "absent is not zero"),
    ],
)
def test_digest_changes_with_content(changed: dict[str, object], why: str) -> None:
    """A type change alone must move the digest — see decision 1 in the module.

    Without type tags, a payable-day count of `6` and the string `"6"` that a
    form round trip produces would freeze identically, and a corrected sheet
    would be indistinguishable from the original.
    """
    base = content_hash({"net": Decimal("14035")})
    assert content_hash(changed) != base, why


def test_added_key_changes_digest() -> None:
    assert content_hash({"a": "1"}) != content_hash({"a": "1", "b": "2"})


def test_empty_containers_are_distinguishable() -> None:
    assert content_hash({"x": {}}) != content_hash({"x": []})


# --- Decimal, losslessly and independent of context precision ----------------


def test_decimal_keeps_every_significant_digit() -> None:
    """`Decimal.normalize()` would corrupt this; the canonicaliser must not.

    `Decimal("123456789012345678901234567890").normalize()` renders as
    `...567900` under the default 28-digit context. A hash helper that alters the
    value it fingerprints is worse than no hash at all.
    """
    big = Decimal("123456789012345678901234567890")
    assert "123456789012345678901234567890" in canonical_json({"n": big})


def test_decimal_rendering_ignores_context_precision() -> None:
    """A caller's `localcontext` must not change what a freeze means."""
    payload = {"n": Decimal("123456789012345678901234567890.5")}
    baseline = content_hash(payload)
    with localcontext() as ctx:
        ctx.prec = 5
        assert content_hash(payload) == baseline


@pytest.mark.parametrize(
    ("value", "rendered"),
    [
        (Decimal("15484.00"), "d:15484"),
        (Decimal("15484"), "d:15484"),
        (Decimal("1E+2"), "d:100"),
        (Decimal("0.10"), "d:0.1"),
        (Decimal("-0.0"), "d:0"),
        (Decimal("0E-10"), "d:0"),
        (Decimal("-1.500"), "d:-1.5"),
    ],
)
def test_decimal_canonical_forms(value: Decimal, rendered: str) -> None:
    """Trailing *fractional* zeros are not content (R6/R7); exponents expand."""
    assert canonical_json({"n": value}) == f'{{"n":"{rendered}"}}'


# --- refusals: anything that would make the guarantee a lie ------------------


def test_float_is_refused_not_converted() -> None:
    """R7: never use float for money. Converting would launder the defect."""
    with pytest.raises(CanonicalisationError) as exc:
        content_hash({"net": 14035.0})
    assert "R7" in str(exc.value)
    assert "$.net" in str(exc.value)


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
def test_non_finite_decimal_is_refused(value: Decimal) -> None:
    with pytest.raises(CanonicalisationError):
        content_hash({"net": value})


def test_naive_datetime_is_refused() -> None:
    """Two readings five and a half hours apart wearing one string (§11)."""
    with pytest.raises(CanonicalisationError) as exc:
        content_hash({"at": dt.datetime(2026, 8, 1, 6, 30)})  # noqa: DTZ001
    assert "UTC" in str(exc.value)


def test_set_is_refused_with_a_remedy() -> None:
    with pytest.raises(CanonicalisationError) as exc:
        content_hash({"pans": {"BCDPS1234K", "AAAPZ1234C"}})
    assert "sorted tuple" in str(exc.value)


def test_unsupported_type_is_refused() -> None:
    class Opaque:
        pass

    with pytest.raises(CanonicalisationError) as exc:
        content_hash({"thing": Opaque()})
    assert "Opaque" in str(exc.value)


def test_bytes_are_refused() -> None:
    """A payload carrying raw bytes wants an explicit encoding decision."""
    with pytest.raises(CanonicalisationError):
        content_hash({"blob": b"\x00\x01"})


def test_non_string_key_is_refused() -> None:
    with pytest.raises(CanonicalisationError):
        content_hash({"rows": {1: "first"}})


def test_enum_key_becomes_its_value() -> None:
    assert content_hash({"by": {Persona.MANAGER: 1}}) == content_hash({"by": {"manager": 1}})


def test_colliding_keys_are_refused() -> None:
    """Two keys that coerce alike would make the digest order-dependent.

    A `StrEnum` member cannot produce this — it hashes as its own value, so
    Python's dict collapses the pair before we ever see it, which is exactly
    §11's point about `StrEnum` over `(str, Enum)`. A plain `Enum` with a string
    value does not, and that is the case this guard is for.
    """

    class Label(Enum):
        MANAGER = "manager"

    with pytest.raises(CanonicalisationError) as exc:
        content_hash({"by": {Label.MANAGER: 1, "manager": 2}})
    assert "iteration order" in str(exc.value)


def test_cycle_is_refused_with_a_path() -> None:
    payload: dict[str, object] = {"a": {}}
    inner = payload["a"]
    assert isinstance(inner, dict)
    inner["self"] = payload
    with pytest.raises(CanonicalisationError) as exc:
        content_hash(payload)
    assert "cycle" in str(exc.value)


def test_excessive_nesting_is_refused() -> None:
    deep: dict[str, object] = {"leaf": 1}
    for _ in range(80):
        deep = {"down": deep}
    with pytest.raises(CanonicalisationError) as exc:
        content_hash(deep)
    assert "nests deeper" in str(exc.value)


def test_error_path_locates_the_bad_cell() -> None:
    """A 21-column sheet with one bad cell must not be a needle in a haystack."""
    with pytest.raises(CanonicalisationError) as exc:
        content_hash({"rows": [{"pan": "X"}, {"net": 1.5}]})
    assert exc.value.path == "$.rows[1].net"


# --- across processes --------------------------------------------------------


def test_digest_is_identical_in_a_fresh_process_with_a_different_hash_seed(
    repo_root: Path,
) -> None:
    """The freeze must survive the process that made it.

    `PYTHONHASHSEED` randomises string hashing, so this is the cheap proof that
    nothing in the canonical form depends on a hash-ordered iteration. Two child
    processes with different seeds must agree with each other and with us.
    """
    code = (
        "from decimal import Decimal;"
        "from app.services.approval.hashing import content_hash;"
        "print(content_hash({'net': Decimal('14035.00'), 'pan': 'BCDPS1234K',"
        " 'rows': [{'b': 2, 'a': 1}], 'flag': True}))"
    )
    digests = []
    for seed in ("0", "1", "12345"):
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(repo_root)}
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            cwd=repo_root,
            env=env,
            check=True,
        )
        digests.append(proc.stdout.strip())

    local = content_hash(
        {"net": Decimal("14035.00"), "pan": "BCDPS1234K", "rows": [{"b": 2, "a": 1}], "flag": True}
    )
    assert digests == [local, local, local]
