"""The Escalation Engine is provably LLM-free. CLAUDE.md §8:

    "**Escalation Engine — deterministic SLA rules. Not LLM judgement.**"

A docstring saying so is not enforcement. This module parses every source file in
`app/services/escalation/` with `ast` and asserts it structurally — the same
technique `tools/rule_linter.py` L2 uses to keep the remuneration engine free of
model calls (R2), and the same one `tests/unit/test_generators.py` uses to make a
claim about code rather than about behaviour.

Three assertions, in increasing strength:

1. no banned import (`app.core.llm`, `openai`, `anthropic`, `httpx`, anything
   whose dotted path contains "llm");
2. no import outside a tiny allowlist — the rule and evaluation modules import
   only from `app.domain`, which is itself I/O-free (§3, linter L4);
3. no attribute access shaped like a model call (`.chat`, `.completions`,
   `.invoke`, `.generate`), which is what an LLM would look like if it arrived
   through an already-permitted module.

If someone later adds an LLM call here, this fails. That is the point: §8's
guarantee has to be enforced by something that runs, not by review memory.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "app" / "services" / "escalation"

BANNED_SUBSTRINGS = ("llm", "openai", "anthropic", "openrouter", "ollama", "langchain")
BANNED_MODULES = ("httpx", "requests", "aiohttp", "app.core.llm", "app.agents", "app.rag")

#: Everything the package is allowed to import from the project.
#:
#: `app.core.audit` is the single concession, used only by `audit.py` to build the
#: §11 audit row. `rules.py` and `engine.py` — where the determinism argument
#: lives — import nothing but `app.domain`, and the second assertion below pins
#: that separately so the concession cannot spread.
ALLOWED_PROJECT_PREFIXES = ("app.domain", "app.services.escalation", "app.core.audit")

PURE_MODULES = ("rules.py", "engine.py", "targets.py")

MODEL_CALL_ATTRS = frozenset(
    {"chat", "completions", "complete", "invoke", "ainvoke", "generate", "predict"}
)


def _sources() -> list[tuple[Path, ast.Module]]:
    files = sorted(PACKAGE.glob("*.py"))
    assert files, "no escalation sources found — the guard would pass vacuously"
    return [(path, ast.parse(path.read_text(encoding="utf-8"), str(path))) for path in files]


def _imported(tree: ast.Module) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            out.append((base, node.lineno))
            out.extend((f"{base}.{alias.name}", node.lineno) for alias in node.names)
    return out


@pytest.mark.parametrize("path_and_tree", _sources(), ids=lambda p: p[0].name)
def test_no_llm_import_anywhere_in_the_package(path_and_tree):
    path, tree = path_and_tree
    for dotted, lineno in _imported(tree):
        lowered = dotted.lower()
        assert not any(token in lowered for token in BANNED_SUBSTRINGS), (
            f"{path.name}:{lineno} imports `{dotted}` — the Escalation Engine is "
            "deterministic SLA rules, not LLM judgement (CLAUDE.md §8)"
        )
        assert not any(
            lowered == banned or lowered.startswith(f"{banned}.") for banned in BANNED_MODULES
        ), f"{path.name}:{lineno} imports `{dotted}`, which can reach a model or a network"


@pytest.mark.parametrize("path_and_tree", _sources(), ids=lambda p: p[0].name)
def test_package_imports_stay_inside_the_allowlist(path_and_tree):
    path, tree = path_and_tree
    for dotted, lineno in _imported(tree):
        if not dotted.startswith("app."):
            continue
        assert any(dotted.startswith(prefix) for prefix in ALLOWED_PROJECT_PREFIXES), (
            f"{path.name}:{lineno} imports `{dotted}` — the escalation package depends only "
            "on app.domain (plus app.core.audit for the §11 row). A wider dependency is how "
            "a model call gets in through the back door."
        )


def test_rule_and_evaluation_modules_depend_only_on_the_domain():
    """The strongest form: where the rules live, there is nothing but pure data."""
    for path, tree in _sources():
        if path.name not in PURE_MODULES:
            continue
        for dotted, lineno in _imported(tree):
            if not dotted.startswith("app."):
                continue
            assert dotted.startswith(("app.domain", "app.services.escalation")), (
                f"{path.name}:{lineno} imports `{dotted}` — {path.name} carries the "
                "determinism guarantee and must import nothing beyond app.domain"
            )


@pytest.mark.parametrize("path_and_tree", _sources(), ids=lambda p: p[0].name)
def test_no_model_shaped_call(path_and_tree):
    """Catches a model reached through an object rather than an import."""
    path, tree = path_and_tree
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in MODEL_CALL_ATTRS:
            pytest.fail(
                f"{path.name}:{node.lineno} accesses `.{node.attr}` — that is the shape of an "
                "inference call. CLAUDE.md §8: deterministic SLA rules, not LLM judgement."
            )
