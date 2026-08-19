"""Start the API with an event loop psycopg can actually use.

Use this instead of `uvicorn app.main:app`:

    python run_api.py                     # 127.0.0.1:8000, reload off
    python run_api.py --reload
    python run_api.py --host 0.0.0.0 --port 9000

WHY THIS FILE EXISTS
--------------------
On Windows, Python builds a `ProactorEventLoop` by default and psycopg's async
driver refuses to run on it. The failure is nasty because it is partial: the app
starts, logs "Application startup complete", serves `/health` at 200 — and then
every endpoint that touches the database returns 500 with
`psycopg.InterfaceError`. A health check would call the service up.

The loop has to be chosen at the point it is created. uvicorn creates its own
loop *after* importing `app.main`, so no amount of setup inside that module
changes anything, and `asyncio.set_event_loop_policy` — the pre-3.12 answer — is
both ineffective here and deprecated for removal in 3.16. The supported route is
to build the loop ourselves and hand `uvicorn.Server.serve()` to it, which is
what `asyncio.run(..., loop_factory=...)` does below.

On Linux and macOS this is a plain `asyncio.run`, so the same command works
everywhere and CI needs no special case.
"""

from __future__ import annotations

import argparse
import asyncio
import selectors
import sys

import uvicorn


def _loop_factory() -> asyncio.AbstractEventLoop:
    """A selector-based loop, which psycopg supports on every platform."""
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    if args.reload:
        # The reloader supervises child processes and builds their loops itself,
        # so the factory below would not reach them. Reload is a convenience;
        # a working database connection is not. Refuse rather than hand back a
        # server that 500s on every query.
        if sys.platform == "win32":
            parser.error(
                "--reload cannot be combined with the selector loop on Windows. "
                "Run without --reload, or accept restarting manually."
            )
        uvicorn.run(
            "app.main:app",
            host=args.host,
            port=args.port,
            reload=True,
            log_level=args.log_level,
        )
        return 0

    config = uvicorn.Config(
        "app.main:app",
        host=args.host,
        port=args.port,
        log_level=args.log_level,
    )
    server = uvicorn.Server(config)

    # `asyncio.run(..., loop_factory=...)` is Python 3.12+. This project pins
    # 3.11 (see README and requirements-dev.txt), where that keyword is a
    # TypeError raised AFTER uvicorn has printed its startup banner — so the
    # server appears to boot and then dies, which is the same confusing shape of
    # failure the module docstring above exists to prevent.
    #
    # The docstring's point still stands and is why this is not simply
    # `asyncio.run(server.serve())`: the loop must be CHOSEN where it is
    # CREATED. Below, the selector loop is built first and driven directly, so
    # 3.11 gets the same loop 3.12 gets from `loop_factory`.
    if sys.version_info >= (3, 12):
        asyncio.run(server.serve(), loop_factory=_loop_factory)
        return 0

    loop = _loop_factory()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(server.serve())
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
