# syntax=docker/dockerfile:1
#
# The API service. The console is a separate, static deployment — this image
# holds only the work RLS cannot express (payout computation, approval and
# release, the RAG copilot, sheet and report generation).
#
# WHY THE DEPENDENCY INSTALL LOOKS LIKE THIS
# ==========================================
# `requirements.txt` is a hashed lock generated from pyproject.toml the same way
# `requirements-dev.txt` is, minus the dev extra:
#
#   uv pip compile pyproject.toml --universal --generate-hashes -o requirements.txt
#
# `--require-hashes` makes the install verifiable as well as reproducible: a
# tampered or substituted artifact fails the build rather than running. That is
# the property `.github/workflows/ci.yml` argues for at length, and an image
# that reached for `pip install .` instead would quietly drop it — a plain
# `pip install .` builds a wheel in an isolated environment populated by
# downloading an unpinned, unhashed setuptools from PyPI and executing its build
# code.
#
# There is no `pip install -e .` step for the same reason there is none in CI.
# Nothing imports the distribution name; every consumer imports `app.…`, so
# `PYTHONPATH` is all that is needed and it is set below.

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/srv \
    PORT=8000

WORKDIR /srv

# Copied and installed before the source, so a code change does not reinstall
# ninety packages.
COPY requirements.txt ./
RUN pip install --require-hashes --no-cache-dir -r requirements.txt

# Only what the service runs. Tests, migrations, the console and the operator
# tools in `tools/` are deliberately absent — see .dockerignore. `tools/` in
# particular can SEND MAIL (tools/agentmail.py), and CLAUDE.md R3 keeps that
# outside anything an agent can reach; keeping it out of the image keeps it
# outside anything the running service can reach either.
COPY app/ ./app/
COPY run_api.py ./

# Unprivileged, and it owns nothing it does not need to write.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin ops \
    && chown -R ops:ops /srv
USER ops

EXPOSE 8000

# `/health` is the only unauthenticated route. It answers without touching the
# database, which is the point: a database that is unreachable must not read as
# a dead process to the orchestrator, or it will restart a healthy container in
# a loop while the real fault sits in Postgres.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/health',timeout=4)"

# `run_api.py`, not `uvicorn app.main:app`. The selector event loop it installs
# is a Windows requirement rather than a Linux one, but the entry point also
# routes through `app.main.__getattr__`, which configures structured logging
# before the app is built. Shell form because the host assigns $PORT.
CMD ["sh", "-c", "exec python run_api.py --host 0.0.0.0 --port ${PORT:-8000}"]
