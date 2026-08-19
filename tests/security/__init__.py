"""Adversarial security regression tests.

Written during the 2026-08 internal security review. Each `xfail(strict=True)`
test encodes an OPEN finding from `docs/security-findings.md`: it asserts the
SECURE behaviour, so it fails today and turns green when the finding is fixed —
at which point `strict=True` reports an XPASS as a failure, forcing whoever
fixed it to delete the marker and lock the invariant in.

Everything unmarked in this package is a control that currently HOLDS. Those
are ordinary regression tests: if one goes red, a wall came down.
"""
