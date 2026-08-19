"""Shared fixtures for the whole test suite.

Per-area fixtures belong in `tests/<area>/conftest.py`. What lives here is the
opposite kind of thing: guards against PROCESS-GLOBAL state, which no per-area
conftest can own because the leak crosses directory boundaries by definition.

WHY THERE ARE AUTOUSE FIXTURES IN A ROOT CONFTEST
=================================================
A test that passes alone and fails in a full run is not a test, it is a
coin-flip with a plausible explanation attached. Both leaks below were real:

1. `structlog` configuration. `app.main` used to reconfigure it as an import
   side effect, which permanently disabled `log.debug()` for every module in
   `app/`. `tests/rag_eval/test_logging.py` asserts a DEBUG event exists, so it
   passed alone and failed after anything imported `app.main`. Fixed at source
   (see `app/main.py`); `_structlog_isolation` keeps it fixed.
2. `app.core.config.get_settings`' `lru_cache`. Several tests mutate the
   environment with `monkeypatch` and call `cache_clear()` so the app re-reads
   it — `tests/security/test_auth_and_release_integrity.py` swaps
   `SUPABASE_JWT_SECRET` for a probe value that way. If the cache is repopulated
   while the environment is still swapped, every later suite that authenticates
   through the real app inherits the probe secret and 401s. That is a whole-suite
   failure with a cause several directories away.

Neither fixture SNAPSHOTS AND RESTORES; both reset to a known-good value. That
distinction matters. pytest instantiates higher-scoped fixtures first, so by the
time a function-scoped fixture gets control a session-scoped one
(`tests/api_contract`'s `client`, for one) may already have polluted the state —
and a teardown-restore would then faithfully restore the pollution it had
snapshotted.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from structlog._config import BoundLoggerLazyProxy

REPO_ROOT = Path(__file__).resolve().parents[1]

# `tools/` is not an installed package; make it importable for tooling tests.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

#: structlog's configuration as it is BEFORE any test or application module has
#: touched it. Captured at conftest import, which is the earliest point in the
#: session at which anything of ours runs.
_PRISTINE_STRUCTLOG: dict[str, Any] = {
    **structlog.get_config(),
    # `capture_logs()` mutates the processor list IN PLACE and relies on bound
    # loggers holding the same list object, so the snapshot has to own a copy or
    # it is not a snapshot.
    "processors": list(structlog.get_config()["processors"]),
}


def _uncache_module_loggers() -> None:
    """Undo `cache_logger_on_first_use` for every logger held by an `app.*` module.

    `structlog.get_logger()` returns a lazy proxy. Under
    `cache_logger_on_first_use=True` the proxy replaces its own `bind` with a
    closure over the bound logger it assembled the first time it was called, and
    that logger keeps the wrapper class (i.e. the level filter) and the processor
    list it was born with FOR THE LIFE OF THE PROCESS. Neither
    `structlog.reset_defaults()` nor `structlog.testing.capture_logs()` reaches
    it, because neither goes near the proxy.

    So restoring the configuration is not on its own enough to undo a leak: any
    `_log = structlog.get_logger(__name__)` that was first used while the leak
    was in effect stays broken. Dropping the memoised `bind` returns the proxy to
    its lazy state and the next call re-reads the live configuration.

    The memoisation is documented behaviour; the attribute it hides behind is
    not, so the `pop` is defensive. If a future structlog memoises somewhere else
    this quietly becomes a no-op and `_structlog_isolation` degrades to a plain
    configuration reset instead of raising.
    """
    for name, module in list(sys.modules.items()):
        if module is None or not (name == "app" or name.startswith("app.")):
            continue
        for value in list(vars(module).values()):
            if isinstance(value, BoundLoggerLazyProxy):
                vars(value).pop("bind", None)


@pytest.fixture(autouse=True)
def _structlog_isolation() -> Iterator[None]:
    """Give every test structlog exactly as the session found it.

    Cheap — a dict assignment and a walk over already-imported `app.*` modules —
    and it turns "did something import `app.main` before me?" from a question the
    suite's result depends on into one nothing depends on.
    """
    structlog.configure(**_PRISTINE_STRUCTLOG)
    _uncache_module_loggers()
    yield


@pytest.fixture(autouse=True)
def _settings_cache_isolation(request: pytest.FixtureRequest) -> Iterator[None]:
    """Drop a `Settings` object that may have been cached from a swapped environment.

    Runs only for tests whose fixture closure contains `monkeypatch` — the
    supported way to change the environment inside a test, and so the only way a
    stale `Settings` gets built. `request.fixturenames` is the FULL closure, so a
    test that pulls `monkeypatch` in indirectly through another fixture (the
    `_probe_secret` case in the module docstring) is covered as well.

    Teardown, not setup, and the ordering is the point. This fixture is autouse,
    so it is set up before `monkeypatch` and therefore finalised AFTER it: by the
    time the clear below runs, `monkeypatch` has already put the environment
    back, and the next `get_settings()` rebuilds from the real values. Clearing
    at setup instead would clear before the test dirtied anything, which is the
    wrong test entirely.
    """
    yield
    if "monkeypatch" in request.fixturenames:
        config = sys.modules.get("app.core.config")
        if config is not None:
            config.get_settings.cache_clear()


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """Absolute path to the repository root."""
    return REPO_ROOT


@pytest.fixture
def tmp_module(tmp_path: Path) -> Callable[[str, str], Path]:
    """Write a Python source string to a temp file and return its path.

    `rel_path` may be nested (`app/domain/money.py`); parents are created, so a
    caller can reproduce the repo layout a path-scoped check needs. The source is
    dedented, so an indented triple-quoted literal in a test stays readable.
    """

    def _write(source: str, rel_path: str = "module.py") -> Path:
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        return path

    return _write
