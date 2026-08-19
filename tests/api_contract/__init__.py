"""Contract tests: real HTTP round trips against the real routers and database.

Separate from `tests/unit/` because these need `DATABASE_URL` and
`SUPABASE_JWT_SECRET` and skip without them. See `conftest.py`.
"""
