"""Canonical content hashing — the freeze half of CLAUDE.md R4.

R4: "Every artifact moves DRAFT -> PENDING_APPROVAL -> APPROVED -> RELEASED.
**Approval freezes and hashes the version.** Editing an approved artifact creates
a new version in DRAFT requiring fresh approval."

A freeze is only worth something if the hash is reproducible. The question this
module has to answer, months later and in a different process, is: *is the thing
in front of me byte-for-byte the thing the Senior Manager approved?* That answer
must not depend on which machine recomputes it, which Python built it, what order
a dict happened to be populated in, or whether a rupee value arrived as
`Decimal("15484")` or `Decimal("15484.00")`.

So the payload is canonicalised into one deterministic ASCII byte string, and
sha256 is taken over that. The canonicalisation is a **wire format**: a change to
it changes every hash ever produced and invalidates every stored freeze. It is
versioned (`CANONICALISATION_VERSION`) and pinned by a golden-digest test.

CANONICALISATION DECISIONS, AND WHAT EACH ONE PREVENTS
=====================================================

1. **Scalars are type-tagged strings, never bare JSON values.**
   `"d:6"` (Decimal), `"i:6"` (int) and `"s:6"` (str) are three different
   preimages. Without tags, a payload that stores payable days as the string
   `"6"` after a round trip through a form, and as `Decimal("6")` before it,
   would hash identically to one that stores the number — and a freeze that
   cannot tell 6 from "6" cannot tell a corrected sheet from an original.

2. **`Decimal` is rendered losslessly and precision-independently.**
   `Decimal.normalize()` is NOT used: it silently rounds to the active context
   precision, so `Decimal("123456789012345678901234567890").normalize()` renders
   as `...567900`. A hash helper that alters the value it is fingerprinting is
   worse than no hash. The digits and exponent are taken from `as_tuple()` and
   recombined by hand, so the rendering never touches the arithmetic context.
   Trailing *fractional* zeros are stripped, so `Decimal("15484.00")` and
   `Decimal("15484")` — the same rupees, per R6/R7 — produce the same digest,
   while `Decimal("15484.01")` does not. `-0` renders as `0`.

3. **`float` is refused outright (R7).** Not converted — refused. A conversion
   would launder the defect into a freeze that looks authoritative; the same
   stance `app.domain.money.money()` takes at the other end of the pipeline.
   `NaN` and `Infinity` `Decimal`s are refused for the same reason: a payout
   whose canonical form is "NaN" is not a payout.

4. **An enum is its stored value.** §11 makes the `StrEnum` member and the SQL
   token the same vocabulary, so a payload rebuilt from a database row (plain
   strings) must hash identically to one built from domain objects (enum
   members). `Enum` is checked *before* `str`, which also side-steps the
   `(str, Enum)` formatting trap `app.domain.enums` warns about.

5. **Datetimes must be timezone-aware, and are normalised to UTC.**
   §11: "All timestamps UTC in the DB, IST at the presentation layer." A naive
   datetime is refused because it is two different instants depending on who
   reads it, and a freeze cannot be built on a value with two readings.

6. **Mapping keys are sorted, and a duplicate after coercion is an error.**
   Insertion order is not content. Two keys that coerce to the same string (the
   `Persona.MANAGER` member and the `"manager"` string) would make the digest
   depend on iteration order, so that raises rather than silently keeping one.

7. **Sets are refused.** A `set` has no order, so hashing one means inventing a
   sort. The caller knows whether their collection is ordered; a sorted `tuple`
   states it, and this module does not guess on their behalf.

8. **`ensure_ascii=True`.** The preimage is pure ASCII, so no locale, console
   codepage or Unicode normalisation setting can perturb the bytes being hashed.
   Non-ASCII text (a college name in Telugu, a trainer's name) survives as
   `\\uXXXX` escapes, which `json` specifies exactly.

9. **A domain-separation prefix.** The preimage begins with
   `bytexl.approval.canonical.v1\\n`, so a digest produced here can never collide
   with a sha256 taken over the same JSON by some other part of the system, and
   a future v2 canonicalisation is distinguishable rather than merely different.

Nothing here does I/O, reads config, or imports the state machine — the state
machine imports this. It is pure in the same sense
`app.services.remuneration.engine` is pure: data in, value out.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from enum import Enum
from typing import Final, TypeAlias
from uuid import UUID

__all__ = [
    "CANONICALISATION_VERSION",
    "HASH_ALGORITHM",
    "CanonicalisationError",
    "canonical_json",
    "canonical_preimage",
    "content_hash",
]

#: Bump ONLY with a migration plan: every stored `content_hash` becomes
#: unverifiable at the moment this changes, which means every APPROVED and
#: RELEASED artifact needs re-freezing or an explicit "frozen under v1" marker.
CANONICALISATION_VERSION: Final[str] = "1"

HASH_ALGORITHM: Final[str] = "sha256"

#: Domain separation. Prepended to the canonical JSON before hashing.
_PREFIX: Final[bytes] = f"bytexl.approval.canonical.v{CANONICALISATION_VERSION}\n".encode("ascii")

# Scalar type tags. Single characters, chosen so the tag is unmistakable in a
# canonical string a human is reading during a dispute.
_TAG_STRING: Final[str] = "s"
_TAG_INT: Final[str] = "i"
_TAG_DECIMAL: Final[str] = "d"
_TAG_BOOL: Final[str] = "b"
_TAG_NULL: Final[str] = "z"
_TAG_DATE: Final[str] = "D"
_TAG_DATETIME: Final[str] = "T"
_TAG_UUID: Final[str] = "u"

#: The canonical tree: tagged scalar strings, arrays, and string-keyed objects.
#: Nothing else reaches `json.dumps`, which is why `allow_nan` can never fire.
JsonNode: TypeAlias = "str | list[JsonNode] | dict[str, JsonNode]"

#: Recursion guard for the canonicaliser. Deep enough for any sheet payload,
#: shallow enough to fail before CPython's own limit turns into a RecursionError
#: with no path in the message.
_MAX_DEPTH: Final[int] = 64

#: Empty cycle-guard set for the top-level call.
_NO_CONTAINERS: Final[frozenset[int]] = frozenset()


class CanonicalisationError(Exception):
    """A payload that cannot be canonicalised, and therefore cannot be frozen.

    Deliberately not a `TypeError` or `ValueError` subclass. Every rejection here
    is a governance failure — an unhashable payload means an artifact that cannot
    be proven unchanged after approval — and it must not be swallowed by a broad
    `except (TypeError, ValueError)` written for input parsing somewhere upstairs.

    `path` locates the offending value inside the payload (`$.rows[2].rate`),
    because a 21-column remuneration sheet with one bad cell is otherwise a
    needle-in-haystack at exactly the moment somebody is trying to approve it.
    """

    def __init__(self, message: str, path: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.raw_message = message


def content_hash(payload: Mapping[str, object]) -> str:
    """Freeze `payload` to a stable sha256 hex digest.

    The same logical content hashes identically across processes, machines and
    Python versions. See the module docstring for what "the same logical content"
    means precisely — notably `Decimal("15484.00") == Decimal("15484")`, an enum
    member equals its stored value, and key order is irrelevant.

    Raises `CanonicalisationError` for anything that would make that guarantee a
    lie: a `float`, a naive datetime, a `set`, a non-finite `Decimal`, or a type
    with no documented rendering.
    """
    return hashlib.sha256(canonical_preimage(payload)).hexdigest()


def canonical_preimage(payload: Mapping[str, object]) -> bytes:
    """The exact bytes `content_hash` digests, prefix included.

    Exposed so a dispute can be settled by comparing preimages rather than by
    staring at two hex strings that differ.
    """
    return _PREFIX + canonical_json(payload).encode("ascii")


def canonical_json(payload: Mapping[str, object]) -> str:
    """The canonical JSON form of `payload`: ASCII, sorted keys, no whitespace.

    Useful beyond hashing — the Comms service shows a diff-from-template at
    approval (§8), and diffing two canonical forms shows *what* changed where two
    digests only show *that* something did.
    """
    tree = _encode(payload, path="$", depth=0)
    return json.dumps(
        tree,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        check_circular=False,  # `_encode` has already walked the structure
    )


# --- the canonicaliser -------------------------------------------------------


def _encode(
    value: object, path: str, depth: int, seen: frozenset[int] = _NO_CONTAINERS
) -> JsonNode:
    """Render one value into the canonical tree.

    `seen` carries the ids of the containers on the current path, so a payload
    that references itself fails with a path instead of a bare `RecursionError`
    a thousand frames from the cause.
    """
    if depth > _MAX_DEPTH:
        raise CanonicalisationError(f"payload nests deeper than {_MAX_DEPTH} levels", path)

    # Enum FIRST: a `StrEnum` is a `str` and an `IntEnum` is an `int`, and §11
    # makes the member and its stored value the same fact.
    if isinstance(value, Enum):
        return _encode(value.value, path, depth, seen)

    if value is None:
        return f"{_TAG_NULL}:"

    # bool BEFORE int — `bool` is an `int` subclass, and True hashing as 1 would
    # make "attendance complete" indistinguishable from a day count of one.
    if isinstance(value, bool):
        return f"{_TAG_BOOL}:{'true' if value else 'false'}"

    if isinstance(value, float):
        raise CanonicalisationError(
            f"float {value!r} cannot be frozen — CLAUDE.md R7: never use float for money. "
            "Pass a Decimal (or a str) so the frozen value is the value.",
            path,
        )

    if isinstance(value, Decimal):
        return f"{_TAG_DECIMAL}:{_render_decimal(value, path)}"

    if isinstance(value, int):
        return f"{_TAG_INT}:{value}"

    if isinstance(value, str):
        return f"{_TAG_STRING}:{value}"

    # datetime BEFORE date — `datetime` is a `date` subclass.
    if isinstance(value, dt.datetime):
        return f"{_TAG_DATETIME}:{_render_datetime(value, path)}"

    if isinstance(value, dt.date):
        return f"{_TAG_DATE}:{value.isoformat()}"

    if isinstance(value, UUID):
        return f"{_TAG_UUID}:{value}"

    if isinstance(value, frozenset | set):
        raise CanonicalisationError(
            "a set has no order, so hashing one means inventing a sort. Pass a sorted "
            "tuple: the caller knows whether the collection is ordered, this module does not.",
            path,
        )

    if isinstance(value, Mapping):
        return _encode_mapping(value, path, depth, seen)

    # str/bytes are Sequences; str is handled above, bytes falls through to the
    # unsupported-type error below, which is correct — a payload carrying raw
    # bytes wants an explicit encoding decision, not a guessed one.
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return _encode_sequence(value, path, depth, seen)

    raise CanonicalisationError(
        f"{type(value).__name__} has no canonical rendering, so it cannot be frozen. "
        "Convert it to a documented type (str, int, bool, None, Decimal, date, "
        "aware datetime, UUID, Enum, Mapping, Sequence) at the boundary.",
        path,
    )


def _encode_mapping(
    value: Mapping[object, object], path: str, depth: int, seen: frozenset[int]
) -> dict[str, JsonNode]:
    _guard_cycle(value, path, seen)
    nested = seen | {id(value)}
    out: dict[str, JsonNode] = {}
    for raw_key, item in value.items():
        key = _render_key(raw_key, path)
        if key in out:
            raise CanonicalisationError(
                f"two keys collapse to {key!r} after coercion — the digest would depend on "
                "iteration order. Give them distinct keys.",
                path,
            )
        out[key] = _encode(item, f"{path}.{key}", depth + 1, nested)
    return out


def _encode_sequence(
    value: Sequence[object], path: str, depth: int, seen: frozenset[int]
) -> list[JsonNode]:
    _guard_cycle(value, path, seen)
    nested = seen | {id(value)}
    return [_encode(item, f"{path}[{i}]", depth + 1, nested) for i, item in enumerate(value)]


def _guard_cycle(container: object, path: str, seen: frozenset[int]) -> None:
    if id(container) in seen:
        raise CanonicalisationError("payload contains a cycle and cannot be frozen", path)


def _render_key(key: object, path: str) -> str:
    """Mapping keys are strings. Enum keys become their value; nothing else passes.

    An int-keyed mapping is refused rather than stringified: `{1: ...}` and
    `{"1": ...}` would become the same object, and a payload that needs integer
    keys is a list wearing a disguise.
    """
    if isinstance(key, Enum):
        return _render_key(key.value, path)
    if isinstance(key, str):
        return key
    raise CanonicalisationError(
        f"mapping key {key!r} is a {type(key).__name__}; canonical keys must be str "
        "(or an Enum whose value is a str).",
        path,
    )


def _render_decimal(value: Decimal, path: str) -> str:
    """Fixed-point text for a `Decimal`, independent of the arithmetic context.

    `normalize()` is not used — see decision 2 in the module docstring; it rounds
    to context precision and would corrupt a value with more than 28 significant
    digits. This walks `as_tuple()` instead, which is exact by construction:

        Decimal("15484.00")  -> "15484"     (fractional zeros are not content)
        Decimal("1E+2")      -> "100"       (exponent expanded, never "1E+2")
        Decimal("-0.0")      -> "0"         (there is no negative zero rupee)
        Decimal("0.10")      -> "0.1"
    """
    if not value.is_finite():
        raise CanonicalisationError(
            f"Decimal {value!r} is not finite, so it has no canonical value. A NaN or "
            "Infinity in a payload is a computation that went wrong upstream.",
            path,
        )

    sign, digit_tuple, exponent = value.as_tuple()
    if not isinstance(exponent, int):  # pragma: no cover - guarded by is_finite()
        raise CanonicalisationError(f"Decimal {value!r} has a non-numeric exponent", path)

    digits = "".join(str(d) for d in digit_tuple)
    if not digits.strip("0"):
        # Every zero is the same zero: 0, 0.00, -0, 0E-10.
        return "0"

    # Fractional trailing zeros carry no value (R6/R7: 15484.00 is 15484 rupees).
    # Integer trailing zeros obviously do, so the stripping stops at the point.
    while exponent < 0 and digits.endswith("0"):
        digits = digits[:-1]
        exponent += 1

    if exponent > 0:
        digits += "0" * exponent
        exponent = 0

    if exponent < 0:
        places = -exponent
        digits = digits.rjust(places + 1, "0")
        text = f"{digits[:-places]}.{digits[-places:]}"
    else:
        text = digits

    return f"-{text}" if sign else text


def _render_datetime(value: dt.datetime, path: str) -> str:
    """ISO-8601 in UTC. Naive datetimes are refused.

    §11: "All timestamps UTC in the DB, IST at the presentation layer." A naive
    datetime is IST to the person who typed it and UTC to the person who stored
    it — two instants five and a half hours apart wearing one string. Freezing
    that would mean two honest readers disagreeing about what was approved.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise CanonicalisationError(
            f"naive datetime {value.isoformat()} cannot be frozen — CLAUDE.md §11 stores "
            "timestamps in UTC. Attach a timezone (dt.UTC) at the boundary.",
            path,
        )
    return value.astimezone(dt.UTC).isoformat()
