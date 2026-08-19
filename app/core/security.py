"""Persona and reach checks for the FastAPI service.

WHY THIS MODULE EXISTS — READ BEFORE DELETING ANYTHING AS "REDUNDANT"
====================================================================
The frontend talks to Supabase directly with the *user's* JWT for ordinary CRUD,
and Row Level Security is the permission wall (CLAUDE.md §3, §4). FastAPI exists
only for the logic RLS cannot express — checklist generation, remuneration math,
roll-ups, integrations — and it connects with the **service-role key**.

`service_role` carries `BYPASSRLS`. That has two consequences, both of which look
like the security model still applies and neither of which does:

1. **Every RLS policy steps aside.** `supabase/migrations/0200_identity.sql`
   header, technique 2: tables are `FORCE ROW LEVEL SECURITY` so even the owner
   is subject to policy — but a `BYPASSRLS` role is still exempt. A service-role
   `select * from pnl` returns every row in the database.

2. **Every column-guard trigger steps aside too.** Technique 3: the guards on
   `profiles`, `deployments` and `tasks` all short-circuit on
   `auth.uid() is null`, which is exactly what a service-role connection looks
   like. They do *nothing at all* on our connection. `supabase/migrations/README.md`
   calls this "the sharpest edge in this schema", and it is.

So the persona checks that the database performs for the frontend must be
performed **here, in code**, for every endpoint. The helpers below are the
app-side mirror of the SQL helpers, one for one:

| SQL (`supabase/migrations/`)  | Python (here)                        |
|-------------------------------|--------------------------------------|
| `is_internal()`               | `require_internal()`                 |
| `can_see_commercials()`       | `require_commercials()`              |
| `can_reach_college(uuid)`     | `require_college_reach()`            |
| `my_college_ids()`            | `Principal.college_ids`              |
| `app_role()`                  | `Principal.persona`                  |

**Compose them the way the policies do.** Every money policy in `0700_finance.sql`
has the shape::

    using (public.can_see_commercials() and public.can_reach_<scope>(...))
           ^ the wall                        ^ the scope

Both conjuncts are load-bearing. `require_commercials()` alone lets a Manager
read another cluster's P&L; `require_college_reach()` alone lets an LDE Executive
read a rate. An endpoint touching commercials calls **both**.

**There is deliberately no admin override on reach.** `is_admin()` is the right
to *hand out* reach; it is not itself reach (0700 header). A Senior Manager with
no cluster assignment reaches nothing, and `require_college_reach()` says so.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Final
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models import (
    Cluster,
    College,
    Profile,
    UserClusterAssignment,
    UserCollegeAssignment,
)
from app.db.session import get_session
from app.domain.enums import COMMERCIALS_PERSONAS, INTERNAL_PERSONAS, Persona

#: Supabase's LEGACY symmetric signing secret (HS256).
#:
#: Read from the environment rather than from `app.core.config.Settings` only
#: because that module is owned elsewhere and does not carry the field yet. It
#: belongs on `Settings` next to `supabase_service_role_key`; move it there and
#: delete this constant when config gains the field.
JWT_SECRET_ENV: Final[str] = "SUPABASE_JWT_SECRET"

#: Supabase stamps every user token with this audience.
JWT_AUDIENCE: Final[str] = "authenticated"

#: Both signing schemes this service accepts, and why there are two.
#:
#: Supabase has moved projects to ASYMMETRIC signing keys: tokens now arrive as
#: `ES256` with a `kid` in the header, and the public keys are published at
#: `/auth/v1/.well-known/jwks.json`. The old scheme was `HS256` against the
#: shared project secret.
#:
#: This was not a theoretical migration. Before JWKS verification existed here,
#: every endpoint in the service returned 401 to every real browser session —
#: the frontend signed in fine (that is Supabase's own client talking to
#: Supabase) and then every FastAPI call failed, because this module could only
#: verify a signature nobody was producing any more.
#:
#: HS256 is retained rather than removed: a project that has not been migrated
#: still issues it, and dropping it would swap one silent lockout for another.
#: The algorithm is chosen per token from its own header — see `_decode()` — and
#: an `alg` outside this tuple is refused rather than trusted, which is the
#: classic `alg: none` / algorithm-confusion defence.
JWT_ALGORITHMS: Final[tuple[str, ...]] = ("ES256", "HS256")

#: Where the project publishes its public keys. Derived from `SUPABASE_URL` so
#: there is one source of truth for which project this service trusts.
JWKS_PATH: Final[str] = "/auth/v1/.well-known/jwks.json"

#: How long a fetched key set is reused. Supabase rotates keys rarely, and an
#: unknown `kid` forces an immediate refetch regardless (see `_jwks()`), so this
#: only bounds how long a REVOKED key stays acceptable.
JWKS_TTL_SECONDS: Final[int] = 600


# --- what a verified caller is -----------------------------------------------


class JwtClaims(BaseModel):
    """The subset of a Supabase access token this service trusts.

    Only `sub` is authoritative. Persona and reach are **never** taken from the
    token: a JWT is minted at sign-in and would keep asserting a persona for its
    whole lifetime after an admin revoked it. They are read from `profiles` and
    the assignment tables on every request instead, which is also what the SQL
    helpers do.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)

    sub: UUID
    aud: str | None = None
    exp: int | None = None
    email: str | None = None
    #: Postgres role from the token (`authenticated`), NOT the app persona.
    role: str | None = Field(default=None)


@dataclass(frozen=True, slots=True)
class Principal:
    """A resolved caller: who they are, what persona they hold, what they reach.

    Built fresh per request from the database, never from the JWT body. Frozen so
    an endpoint cannot widen its own caller's scope halfway through a handler.

    `college_ids` is the app-side value of `public.my_college_ids()`: direct
    assignments UNION the colleges of assigned clusters. Empty for trainers,
    colleges and unassigned staff — which is deny-by-default, exactly as in SQL.

    `is_admin` mirrors `public.is_admin()`. It is NOT reach; see the module
    docstring.
    """

    user_id: UUID
    persona: Persona
    college_ids: frozenset[UUID]
    is_admin: bool = False

    @property
    def is_internal(self) -> bool:
        """Mirror of `public.is_internal()` — byteXL staff, persona only."""
        return self.persona in INTERNAL_PERSONAS

    @property
    def can_see_commercials(self) -> bool:
        """Mirror of `public.can_see_commercials()`.

        Senior Manager and Manager only. The set comes from
        `app.domain.enums.COMMERCIALS_PERSONAS` rather than being re-derived
        here: one definition, one place to change, greppable from both sides.
        """
        return self.persona in COMMERCIALS_PERSONAS

    def can_reach_college(self, college_id: UUID) -> bool:
        """Mirror of `public.can_reach_college(uuid)`. No admin override."""
        return college_id in self.college_ids


# --- token verification -------------------------------------------------------


def _jwt_secret() -> str:
    """The HS256 secret Supabase signs access tokens with.

    Read from `Settings` so the value comes from `.env` like every other secret,
    with a bare-environment fallback for contexts that never build Settings
    (a one-off script, a test that sets only this variable).

    Fails closed. A misconfigured verifier must never fall back to "trust the
    token" — a dead endpoint is recoverable, an endpoint that accepts unverified
    JWTs is a breach. 500 rather than 401 is deliberate too: nothing is wrong
    with the caller's request, the server is misconfigured, and reporting it as
    a client error would send whoever is debugging in the wrong direction.
    """
    configured = get_settings().supabase_jwt_secret
    secret = configured.get_secret_value() if configured else os.environ.get(JWT_SECRET_ENV, "")
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"{JWT_SECRET_ENV} is not configured; JWTs cannot be verified",
        )
    return secret


def _unauthorised(exc: Exception | None = None) -> HTTPException:
    """One 401, one message, whatever actually failed.

    Telling a caller *which* check failed — bad signature vs expired vs wrong
    audience vs unknown key — is free reconnaissance.
    """
    error = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if exc is not None:
        error.__cause__ = exc
    return error


#: Cached public keys: `(fetched_at_monotonic, {kid: jwk})`.
_jwks_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


async def _jwks(*, force_refresh: bool = False) -> dict[str, dict[str, Any]]:
    """The project's public signing keys, by `kid`, cached for `JWKS_TTL_SECONDS`.

    `force_refresh` exists for key rotation: when a token arrives with a `kid`
    this process has never seen, the honest reading is "the keys changed", not
    "the token is forged". Refetching once and retrying turns a rotation from an
    outage lasting the cache TTL into a single extra HTTP call. It is bounded to
    one retry per verification so an actually-forged `kid` cannot be used to
    hammer the auth endpoint.
    """
    global _jwks_cache

    if not force_refresh and _jwks_cache is not None:
        fetched_at, keys = _jwks_cache
        if (time.monotonic() - fetched_at) < JWKS_TTL_SECONDS:
            return keys

    base = str(get_settings().supabase_url).rstrip("/")
    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(base + JWKS_PATH)
        response.raise_for_status()
        document = response.json()

    keys = {key["kid"]: key for key in document.get("keys", []) if "kid" in key}
    _jwks_cache = (time.monotonic(), keys)
    return keys


def reset_jwks_cache() -> None:
    """Drop the cached key set. For tests, and for a deliberate reload."""
    global _jwks_cache
    _jwks_cache = None


def decode_supabase_jwt(
    token: str,
    *,
    secret: str | None = None,
    audience: str = JWT_AUDIENCE,
) -> JwtClaims:
    """Verify an **HS256** Supabase token against the shared project secret.

    Kept synchronous and unchanged in shape because it is the symmetric path: it
    needs no network and a test can drive it with a literal secret. Projects on
    Supabase's newer asymmetric keys arrive as ES256 and go through
    `verify_access_token()` instead — see `JWT_ALGORITHMS`.
    """
    try:
        payload = jwt.decode(
            token,
            secret if secret is not None else _jwt_secret(),
            algorithms=["HS256"],
            audience=audience,
        )
    except JWTError as exc:
        raise _unauthorised(exc) from exc
    return JwtClaims.model_validate(payload)


async def verify_access_token(token: str, *, audience: str = JWT_AUDIENCE) -> JwtClaims:
    """Verify a Supabase access token of either signing scheme.

    The algorithm is read from the token's own header and then **checked against
    `JWT_ALGORITHMS` before anything else happens**. That ordering is the point:
    accepting whatever `alg` the token names is how `alg: none` and RS/HS
    confusion attacks work, so an unrecognised algorithm is rejected outright
    rather than passed to a verifier that might do something accommodating with
    it.

    ES256 keys come from JWKS by `kid`; HS256 falls back to the shared secret.
    """
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise _unauthorised(exc) from exc

    algorithm = header.get("alg")
    if algorithm not in JWT_ALGORITHMS:
        raise _unauthorised()

    if algorithm == "HS256":
        return decode_supabase_jwt(token, audience=audience)

    kid = header.get("kid")
    if not kid:
        raise _unauthorised()

    keys = await _jwks()
    key = keys.get(kid)
    if key is None:
        # Unknown kid: assume rotation, refetch once, and only then give up.
        keys = await _jwks(force_refresh=True)
        key = keys.get(kid)
    if key is None:
        raise _unauthorised()

    try:
        payload = jwt.decode(token, key, algorithms=["ES256"], audience=audience)
    except JWTError as exc:
        raise _unauthorised(exc) from exc
    return JwtClaims.model_validate(payload)


# --- resolving a principal from the database ----------------------------------


async def resolve_principal(session: AsyncSession, user_id: UUID) -> Principal:
    """Load persona, admin flag and college reach for `user_id`.

    Two statements, both mirroring SQL that the database would have run for us if
    we were not on a `BYPASSRLS` connection:

    * `profiles` -> persona + `is_admin`  (`app_role()`, `is_admin()`)
    * assignments UNION cluster expansion (`my_college_ids()`)

    A user with no profile row is rejected rather than defaulted. The signup
    trigger guarantees one exists, so its absence means the token outlived the
    account — and "no profile" must never resolve to a usable persona.
    """
    profile = (
        await session.execute(select(Profile).where(Profile.id == user_id))
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="No profile for this user",
            headers={"WWW-Authenticate": "Bearer"},
        )

    direct = select(UserCollegeAssignment.college_id).where(
        UserCollegeAssignment.user_id == user_id
    )
    via_cluster = (
        select(College.id)
        .join(Cluster, Cluster.id == College.cluster_id)
        .join(UserClusterAssignment, UserClusterAssignment.cluster_id == Cluster.id)
        .where(UserClusterAssignment.user_id == user_id)
    )
    reach = (await session.execute(union(direct, via_cluster))).scalars().all()

    return Principal(
        user_id=profile.id,
        persona=Persona(profile.role),
        college_ids=frozenset(reach),
        is_admin=profile.is_admin,
    )


_bearer = HTTPBearer(auto_error=False, description="Supabase access token")


async def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(get_session)],
) -> Principal:
    """FastAPI dependency: the authenticated caller, or 401."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    claims = await verify_access_token(credentials.credentials)
    return await resolve_principal(session, claims.sub)


CurrentPrincipal = Annotated[Principal, Depends(get_principal)]


# --- the guards ----------------------------------------------------------------
#
# Each raises 403 and returns nothing useful on failure. They are plain functions
# taking a Principal (so they are unit-testable without a request, a token or a
# database) and `require_persona` additionally composes into `Depends`.


def require_persona(*personas: Persona) -> Callable[[CurrentPrincipal], Principal]:
    """Dependency factory: allow only these personas.

    Usage::

        @router.post("/x", dependencies=[Depends(require_persona(Persona.MANAGER))])

    or, to keep the principal::

        principal: Annotated[Principal, Depends(require_persona(Persona.MANAGER))]

    Passing no persona is a programming error, not "allow everything" — a guard
    that silently permits everyone is worse than no guard.
    """
    if not personas:
        raise ValueError("require_persona() needs at least one persona")
    allowed = frozenset(personas)

    def _dependency(principal: CurrentPrincipal) -> Principal:
        if principal.persona not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This action is not available to your role",
            )
        return principal

    return _dependency


def require_internal(principal: Principal) -> None:
    """Mirror of `public.is_internal()`. Raises 403 for a trainer or college."""
    if not principal.is_internal:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action is available to byteXL staff only",
        )


def require_commercials(principal: Principal) -> None:
    """Mirror of `public.can_see_commercials()` — the CLAUDE.md §4 wall.

    Senior Manager and Manager only. An LDE Executive is refused here, which is
    the app-side half of "zero rows from `pnl`, remuneration, invoices and
    work-order rates" (R5). This is the wall and NOT the scope: pair it with
    `require_college_reach()`, the way every policy in `0700_finance.sql` does.
    """
    if not principal.can_see_commercials:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Commercial data is not available to your role",
        )


def require_college_reach(principal: Principal, college_id: UUID) -> None:
    """Mirror of `public.can_reach_college(uuid)`.

    Reach only — it says nothing about persona, exactly like its SQL counterpart.
    A trainer has no assignment rows so they fail anyway, but persona is still
    checked independently (`require_internal`) so that one bad row in
    `user_college_assignments` cannot become a privilege escalation. That
    reasoning is `0200_identity.sql`'s, on `is_internal()`.
    """
    if not principal.can_reach_college(college_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this college",
        )
