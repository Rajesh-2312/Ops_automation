"""`RetrievalScope` — who is asking, as a value that cannot be forged casually.

CLAUDE.md §9: "Persona filter applies **before** retrieval, not after
generation." That is a statement about *structure*, and this module is where the
structure lives.

THE PROBLEM THIS SOLVES
-----------------------
The FastAPI service connects with the service-role key, which carries BYPASSRLS
(`app/db/session.py`). Every policy in migration 1600 steps aside for it. So the
database cannot be the thing that stops an LDE Executive retrieving a contract
chunk on this connection — the code has to, and code is easy to forget.

The defence is to make the forgetful version impossible to write rather than
merely discouraged:

  * `RagRetriever.retrieve()` takes a `RetrievalScope` as its FIRST, REQUIRED
    argument. There is no `retrieve_all`, no `scope=None` default, and no other
    function in `app/rag/` that issues a similarity query.
  * `public.rag_search()` likewise has no defaults for its persona parameters —
    a query missing them does not run, it fails to typecheck in Postgres.
  * The filter and the `ORDER BY ... LIMIT` are in the SAME SQL statement, which
    is the part that makes late filtering not merely unsafe but *wrong*: filter a
    top-k that has already been truncated and you have silently discarded the
    permitted chunks that the forbidden ones outranked.
  * A scope cannot claim a reach its persona does not have. `__post_init__`
    re-derives `is_internal` and `can_see_commercials` from the persona and
    rejects any disagreement, so `RetrievalScope(persona=LDE_EXECUTIVE,
    can_see_commercials=True)` raises rather than retrieving.

`for_principal()` is the intended constructor and mirrors `app/core/security.py`
field for field, so the copilot's wall and the rest of the API's wall are the
same wall.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from app.core.security import Principal
from app.domain.enums import COMMERCIALS_PERSONAS, INTERNAL_PERSONAS, Corpus, Persona

#: Every corpus, in the enum's declared order. The default breadth of a search:
#: the ACL in `rag_corpus_access` narrows it per persona, so asking for all six
#: is not asking for more than you hold — it is asking for everything you hold.
ALL_CORPORA: tuple[Corpus, ...] = tuple(Corpus)


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """The persona filter, as a value. Frozen, self-consistent, required.

    Frozen so a handler cannot widen its own caller's scope halfway through —
    the same reasoning as `Principal`, which it is derived from.
    """

    persona: Persona
    #: The app-side value of `public.my_college_ids()`: direct assignments UNION
    #: the colleges of assigned clusters. Empty is deny-by-default, exactly as in
    #: SQL — a Senior Manager with no cluster assignment reaches nothing.
    college_ids: frozenset[UUID] = field(default_factory=frozenset)
    is_internal: bool = False
    can_see_commercials: bool = False

    def __post_init__(self) -> None:
        """Refuse a scope that claims more than its persona holds.

        Both flags are DERIVED, and this check is what makes them derived rather
        than merely conventional. Without it the two booleans are an open door: a
        future caller constructs `RetrievalScope(persona=..., can_see_commercials=True)`
        to make a test pass, and the wall is gone with no diff that looks like a
        security change.

        Raising `ValueError` rather than silently correcting is deliberate. A
        silent correction turns a programming error into a behaviour nobody
        notices; the loud version turns it into a stack trace at the call site.
        """
        expected_internal = self.persona in INTERNAL_PERSONAS
        expected_commercials = self.persona in COMMERCIALS_PERSONAS
        if self.is_internal != expected_internal:
            raise ValueError(
                f"RetrievalScope for persona {self.persona.value!r} claims "
                f"is_internal={self.is_internal}; INTERNAL_PERSONAS says "
                f"{expected_internal}. Reach is derived from persona, never asserted."
            )
        if self.can_see_commercials != expected_commercials:
            raise ValueError(
                f"RetrievalScope for persona {self.persona.value!r} claims "
                f"can_see_commercials={self.can_see_commercials}; CLAUDE.md §4 says "
                f"{expected_commercials}. An LDE Executive has NO commercials."
            )

    @classmethod
    def for_principal(cls, principal: Principal) -> RetrievalScope:
        """The intended constructor: a verified caller becomes a retrieval scope.

        Mirrors `app/core/security.py` one for one — `Principal.is_internal` and
        `Principal.can_see_commercials` are the app-side echoes of
        `public.is_internal()` and `public.can_see_commercials()`, and nothing
        here re-derives them a second way.
        """
        return cls(
            persona=principal.persona,
            college_ids=principal.college_ids,
            is_internal=principal.is_internal,
            can_see_commercials=principal.can_see_commercials,
        )

    def permits_commercial(self) -> bool:
        return self.can_see_commercials

    def permits_college(self, college_id: UUID | None) -> bool:
        """NULL scope is organisation-wide; anything else must be in reach."""
        return college_id is None or college_id in self.college_ids
