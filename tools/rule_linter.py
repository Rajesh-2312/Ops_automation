"""Static linter for the byteXL Ops Intelligence Platform hard rules.

Run from the repo root::

    python tools/rule_linter.py            # lint the whole repo
    python tools/rule_linter.py app/agents # lint explicit paths

Exit code 0 when clean, 1 when any rule is violated. Stdlib only (``ast``) so it
runs in CI before anything is installed.

The rules encode CLAUDE.md §1 hard rules that are cheap to state and expensive to
discover late:

===  ============================================================  ==============
L1   no ``float`` anywhere money is computed                        R7
L2   no LLM/network client imported into the remuneration engine    R2
L3   agent toolsets are read-only + ``save_draft``                  R3
L4   ``app/domain/`` imports no I/O layer                           §3
L5   status comparisons use enums, not string literals              §11
===  ============================================================  ==============

L5 is deliberately conservative: a linter that cries wolf gets disabled, and a
disabled linter enforces nothing. It also honours a same-line escape hatch,
``# lint: allow-literal <reason>``, and reports how many are in use.
"""

from __future__ import annotations

import argparse
import ast
import contextlib
import re
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

ROOT: Path = Path(__file__).resolve().parents[1]

#: Directory names never scanned. ``tests/`` writes deliberate violations as
#: fixtures; ``frontend/`` is not Python.
SKIP_DIRS: frozenset[str] = frozenset(
    {
        "tests",
        ".venv",
        "venv",
        ".env",
        "node_modules",
        "frontend",
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "build",
        "dist",
        ".eggs",
    }
)

ESCAPE_RE = re.compile(r"#\s*lint:\s*allow-literal(?P<reason>.*)$")


# --- reporting ---------------------------------------------------------------


@dataclass(frozen=True)
class Violation:
    """One rule breach, at one line, in one file."""

    rule: str
    path: str
    line: int
    message: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: [{self.rule}] {self.message}"


@dataclass
class Report:
    """Everything one lint run found."""

    violations: list[Violation] = field(default_factory=list)
    parse_errors: list[Violation] = field(default_factory=list)
    files_scanned: int = 0
    escapes_used: int = 0

    @property
    def ok(self) -> bool:
        return not self.violations and not self.parse_errors

    def by_rule(self, code: str) -> list[Violation]:
        return [v for v in self.violations if v.rule == code]


# --- per-file context --------------------------------------------------------


def _module_name(rel_path: str) -> str:
    """``app/domain/enums.py`` -> ``app.domain.enums``."""
    parts = rel_path.split("/")
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(p for p in parts if p)


class FileContext:
    """The file under inspection, plus the project knowledge rules need."""

    def __init__(
        self,
        rel_path: str,
        source: str,
        enum_index: dict[str, list[str]] | None = None,
    ) -> None:
        self.rel_path = rel_path
        self.lines = source.splitlines()
        self.module = _module_name(rel_path)
        # The package a relative import is resolved against.
        is_package = rel_path.endswith("__init__.py")
        self.package = self.module if is_package else self.module.rpartition(".")[0]
        self.enum_index = enum_index or {}

    def line_text(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""

    def escape_on(self, lineno: int) -> bool:
        """True when the line carries ``# lint: allow-literal <reason>``."""
        return ESCAPE_RE.search(self.line_text(lineno)) is not None

    def resolve_import_from(self, node: ast.ImportFrom) -> str:
        """Absolute dotted module for an ``from ... import`` node."""
        if not node.level:
            return node.module or ""
        base = self.package.split(".") if self.package else []
        climb = node.level - 1
        if climb:
            base = base[:-climb] if climb <= len(base) else []
        tail = [node.module] if node.module else []
        return ".".join([*base, *tail])


# --- rule base ---------------------------------------------------------------


class Rule(ast.NodeVisitor):
    """A single rule. Scoped by path prefix, walks the AST, collects violations."""

    code: ClassVar[str] = ""
    title: ClassVar[str] = ""
    #: Repo-relative prefixes this rule applies to. A trailing "/" means directory.
    scopes: ClassVar[tuple[str, ...]] = ()

    def __init__(self, ctx: FileContext) -> None:
        self.ctx = ctx
        self.violations: list[Violation] = []
        self.escapes_used = 0

    @classmethod
    def applies_to(cls, rel_path: str) -> bool:
        return any(
            rel_path.startswith(scope) if scope.endswith("/") else rel_path == scope
            for scope in cls.scopes
        )

    def report(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", 0)
        self.violations.append(Violation(self.code, self.ctx.rel_path, line, message))

    def run(self, tree: ast.Module) -> list[Violation]:
        self.visit(tree)
        return self.violations


def _imported_modules(node: ast.Import | ast.ImportFrom, ctx: FileContext) -> list[str]:
    """Every dotted path an import statement brings into scope."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    base = ctx.resolve_import_from(node)
    names = [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
    return [base, *names] if base else names


# --- L1: no float where money lives ------------------------------------------

FLOAT_WHY = "floats silently lose rupees; use Decimal (CLAUDE.md R7, R6)"


class NoFloatMoney(Rule):
    """R7 — never use float for money.

    One carve-out: `isinstance(x, float)` / `issubclass(t, float)`. Naming the
    type in a guard that *rejects* a float is R7 being enforced, not broken — and
    `money.py` is exactly where that guard belongs.
    """

    code = "L1"
    title = "no float in money code"
    scopes = ("app/services/remuneration/", "app/domain/money.py")

    def __init__(self, ctx: FileContext) -> None:
        super().__init__(ctx)
        self._exempt: set[int] = set()

    def run(self, tree: ast.Module) -> list[Violation]:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"isinstance", "issubclass"}
                and len(node.args) == 2
            ):
                self._exempt.update(id(n) for n in ast.walk(node.args[1]))
        return super().run(tree)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "float":
            self.report(node, f"`float()` conversion in money code — {FLOAT_WHY}")
            for child in [*node.args, *node.keywords]:
                self.visit(child)
            return
        if isinstance(func, ast.Name) and func.id == "round":
            self.report(
                node,
                "builtin `round()` in money code — it returns a float for float input "
                "and rounds mid-calculation; use Decimal.quantize() once at net pay "
                "(CLAUDE.md R6, R7)",
            )
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "float" and id(node) not in self._exempt:
            self.report(node, f"name `float` in money code — {FLOAT_WHY}")
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, float):
            self.report(
                node,
                f'float literal `{node.value!r}` in money code — write Decimal("{node.value}"); '
                f"{FLOAT_WHY}",
            )
        self.generic_visit(node)


# --- L2: no LLM in the remuneration engine -----------------------------------

BANNED_MONEY_IMPORTS: tuple[str, ...] = ("app.core.llm", "openai", "anthropic", "httpx")


class NoLLMMoney(Rule):
    """R2 — no money is computed by an LLM."""

    code = "L2"
    title = "no LLM in remuneration"
    scopes = ("app/services/remuneration/",)

    def _check(self, node: ast.Import | ast.ImportFrom) -> None:
        for dotted in _imported_modules(node, self.ctx):
            lowered = dotted.lower()
            hit = next(
                (b for b in BANNED_MONEY_IMPORTS if lowered == b or lowered.startswith(f"{b}.")),
                None,
            )
            if hit is None and "llm" in lowered:
                hit = dotted
            if hit is not None:
                self.report(
                    node,
                    f"`{dotted}` imported into the remuneration engine — an agent may explain "
                    "a number, it may never produce one. All monetary arithmetic is pure "
                    "Python Decimal (CLAUDE.md R2)",
                )
                return

    def visit_Import(self, node: ast.Import) -> None:
        self._check(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._check(node)


# --- L3: agents have no release capability -----------------------------------

R3_QUOTE = (
    "CLAUDE.md R3: agent tool sets contain read and `save_draft` only — "
    '"enforced by tool binding, not by prompt instruction — never add a '
    "send-capable tool to an agent's toolset 'temporarily'.\""
)

ALLOWED_TOOL_RE = re.compile(r"^(read_|get_|list_|search_)|^save_draft$", re.IGNORECASE)
FORBIDDEN_TOOL_RE = re.compile(
    r"(^|_)(send|post|release|released|email|whatsapp|notify|publish|dispatch)(_|$)",
    re.IGNORECASE,
)
TOOLSET_NAME_RE = re.compile(r"^[A-Za-z_]*(tools|toolset)$", re.IGNORECASE)
SENDING_MODULE_TOKENS: tuple[str, ...] = (
    "send",
    "mail",
    "whatsapp",
    "notify",
    "publish",
    "release",
    "smtp",
    "twilio",
    "sendgrid",
    "sms",
    "outbound",
)


class AgentToolsetGuard(Rule):
    """R3 — agents have no release capability. The platform's load-bearing rule."""

    code = "L3"
    title = "agent toolsets are read-only"
    scopes = ("app/agents/",)

    # -- toolset literals

    def _check_collection(self, value: ast.expr) -> None:
        if not isinstance(value, ast.List | ast.Tuple | ast.Set):
            return
        for element in value.elts:
            if isinstance(element, ast.Starred):
                # Unresolvable at parse time: only flag an obviously send-capable name.
                for name, node in _tool_names(element.value):
                    if FORBIDDEN_TOOL_RE.search(name):
                        self._forbidden(node, name)
                continue
            for name, node in _tool_names(element):
                # Deny BEFORE allow. Order matters: a tool named `read_and_send`
                # or `get_or_publish` satisfies the read-prefix allowlist while
                # still being send-capable. Checking the allowlist first would
                # short-circuit and wave it through — which is exactly the
                # "temporarily" case R3 warns about, arriving by accident.
                if FORBIDDEN_TOOL_RE.search(name):
                    self._forbidden(node, name)
                elif ALLOWED_TOOL_RE.search(name):
                    continue
                else:
                    self.report(
                        node,
                        f"`{name}` is in an agent toolset but is not a read tool "
                        "(read_/get_/list_/search_) nor `save_draft`. " + R3_QUOTE,
                    )

    def _forbidden(self, node: ast.AST, name: str) -> None:
        self.report(
            node,
            f"RELEASE-CAPABLE TOOL `{name}` bound to an agent. " + R3_QUOTE,
        )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name) and TOOLSET_NAME_RE.match(target.id):
                self._check_collection(node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        target = node.target
        if (
            isinstance(target, ast.Name)
            and node.value is not None
            and TOOLSET_NAME_RE.match(target.id)
        ):
            self._check_collection(node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            if keyword.arg and TOOLSET_NAME_RE.match(keyword.arg):
                self._check_collection(keyword.value)
        self.generic_visit(node)

    # -- send-capable imports

    def _check_import(self, node: ast.Import | ast.ImportFrom) -> None:
        for dotted in _imported_modules(node, self.ctx):
            parts = dotted.lower().split(".")
            for part in parts:
                if any(token in part for token in SENDING_MODULE_TOKENS):
                    self.report(
                        node,
                        f"`{dotted}` imported under app/agents/ — its name suggests a send "
                        "capability. " + R3_QUOTE,
                    )
                    return

    def visit_Import(self, node: ast.Import) -> None:
        self._check_import(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._check_import(node)


def _tool_names(element: ast.expr) -> list[tuple[str, ast.expr]]:
    """Names a toolset element contributes, e.g. ``Tool(name="send_email")``."""
    if isinstance(element, ast.Name):
        return [(element.id, element)]
    if isinstance(element, ast.Attribute):
        return [(element.attr, element)]
    if isinstance(element, ast.Constant) and isinstance(element.value, str):
        return [(element.value, element)]
    if isinstance(element, ast.Call):
        # Ignore the factory's own name (`Tool`, `StructuredTool.from_function`);
        # what matters is the function or name it wraps.
        found: list[tuple[str, ast.expr]] = []
        for arg in element.args:
            found.extend(_tool_names(arg))
        for keyword in element.keywords:
            if keyword.arg in {"name", "func", "coroutine", "fn", "tool"}:
                found.extend(_tool_names(keyword.value))
        return found
    return []


# --- L4: domain purity -------------------------------------------------------

BANNED_DOMAIN_IMPORTS: tuple[str, ...] = (
    "app.db",
    "app.api",
    "app.agents",
    "app.services",
    "sqlalchemy",
    "fastapi",
    "supabase",
)


class DomainPurity(Rule):
    """§3 — `domain/` imports nothing from db/, api/, agents/ or services/."""

    code = "L4"
    title = "domain layer stays pure"
    scopes = ("app/domain/",)

    def _check(self, node: ast.Import | ast.ImportFrom) -> None:
        for dotted in _imported_modules(node, self.ctx):
            lowered = dotted.lower()
            for banned in BANNED_DOMAIN_IMPORTS:
                if lowered == banned or lowered.startswith(f"{banned}."):
                    self.report(
                        node,
                        f"`{dotted}` imported into app/domain/ — the domain layer is pure "
                        "dataclasses and enums with zero I/O. Keep the business rules "
                        "testable without a database (CLAUDE.md §3)",
                    )
                    return

    def visit_Import(self, node: ast.Import) -> None:
        self._check(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._check(node)


# --- L5: enums, never string literals for status -----------------------------

STATUS_SUFFIXES: tuple[str, ...] = ("status", "state", "role", "stage", "mark")


class StatusLiteral(Rule):
    """§11 — enums in `domain/`, never string literals for status values."""

    code = "L5"
    title = "status compared to enum, not string"
    scopes = ("app/",)

    def visit_Compare(self, node: ast.Compare) -> None:
        left = node.left
        for op, right in zip(node.ops, node.comparators, strict=True):
            if isinstance(op, ast.Eq | ast.NotEq):
                pair = _status_literal_pair(left, right)
            elif isinstance(op, ast.In | ast.NotIn):
                pair = _status_in_pair(left, right)
            else:
                pair = None
            if pair is None:
                left = right
                continue
            attr, literal = pair
            if self.ctx.escape_on(node.lineno):
                self.escapes_used += 1
                left = right
                continue
            self.report(
                node,
                f"`{attr}` compared against the string literal {literal!r} — "
                f"{_enum_hint(self.ctx, literal)} (CLAUDE.md §11: enums in domain/, never "
                "string literals for status values). Escape with "
                "`# lint: allow-literal <reason>` if this really is not a status.",
            )
            left = right
        self.generic_visit(node)


def _attr_chain(node: ast.expr) -> str | None:
    """``run.trainer.status`` -> that dotted text, for attribute access only."""
    if not isinstance(node, ast.Attribute):
        return None
    parts: list[str] = []
    cursor: ast.expr = node
    while isinstance(cursor, ast.Attribute):
        parts.append(cursor.attr)
        cursor = cursor.value
    if isinstance(cursor, ast.Name):
        parts.append(cursor.id)
    parts.reverse()
    return ".".join(parts)


def _is_status_attr(node: ast.expr) -> str | None:
    chain = _attr_chain(node)
    if chain is None:
        return None
    last = chain.rpartition(".")[2].lower()
    return chain if last.endswith(STATUS_SUFFIXES) else None


def _string_const(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _status_literal_pair(left: ast.expr, right: ast.expr) -> tuple[str, str] | None:
    for attr_side, lit_side in ((left, right), (right, left)):
        attr = _is_status_attr(attr_side)
        literal = _string_const(lit_side)
        if attr is not None and literal is not None:
            return attr, literal
    return None


def _status_in_pair(left: ast.expr, right: ast.expr) -> tuple[str, str] | None:
    """``x.status in ("a", "b")`` — only when every member is a string literal."""
    attr = _is_status_attr(left)
    if attr is None or not isinstance(right, ast.List | ast.Tuple | ast.Set):
        return None
    literals = [_string_const(e) for e in right.elts]
    if not literals or any(lit is None for lit in literals):
        return None
    return attr, str(literals[0])


def _enum_hint(ctx: FileContext, literal: str) -> str:
    members = ctx.enum_index.get(literal)
    if members:
        return "use " + " or ".join(f"`{m}`" for m in sorted(members)[:3])
    return "use the matching enum from app/domain/enums.py"


def build_enum_index(root: Path) -> dict[str, list[str]]:
    """Map every enum *value* in app/domain/enums.py to ``EnumName.MEMBER``."""
    source_file = root / "app" / "domain" / "enums.py"
    index: dict[str, list[str]] = {}
    try:
        tree = ast.parse(source_file.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return index
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign) or len(stmt.targets) != 1:
                continue
            target = stmt.targets[0]
            value = _string_const(stmt.value)
            if isinstance(target, ast.Name) and value is not None:
                index.setdefault(value, []).append(f"{node.name}.{target.id}")
    return index


# --- runner ------------------------------------------------------------------

RULES: tuple[type[Rule], ...] = (
    NoFloatMoney,
    NoLLMMoney,
    AgentToolsetGuard,
    DomainPurity,
    StatusLiteral,
)


def iter_python_files(target: Path) -> Iterator[Path]:
    """Every lintable .py file under `target`, skipping vendored/test trees."""
    if target.is_file():
        if target.suffix == ".py":
            yield target
        return
    if not target.exists():
        return  # other agents are still creating directories; stay silent
    for path in sorted(target.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def check_source(
    source: str,
    rel_path: str,
    enum_index: dict[str, list[str]] | None = None,
    rules: Sequence[type[Rule]] | None = None,
) -> tuple[list[Violation], int]:
    """Lint one source string. Returns (violations, escape hatches used)."""
    ctx = FileContext(rel_path, source, enum_index)
    selected = [r for r in (rules or RULES) if rules is not None or r.applies_to(rel_path)]
    if not selected:
        return [], 0
    tree = ast.parse(source, filename=rel_path)
    violations: list[Violation] = []
    escapes = 0
    for rule_cls in selected:
        rule = rule_cls(ctx)
        violations.extend(rule.run(tree))
        escapes += rule.escapes_used
    return violations, escapes


def lint_paths(paths: Sequence[Path], root: Path = ROOT) -> Report:
    """Lint every applicable file under `paths`, resolving scopes against `root`."""
    report = Report()
    enum_index = build_enum_index(root)
    seen: set[Path] = set()
    for target in paths:
        for path in iter_python_files(target):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            try:
                rel = resolved.relative_to(root.resolve()).as_posix()
            except ValueError:
                rel = resolved.as_posix()
            if not any(rule.applies_to(rel) for rule in RULES):
                continue
            report.files_scanned += 1
            source = resolved.read_text(encoding="utf-8")
            try:
                violations, escapes = check_source(source, rel, enum_index)
            except SyntaxError as exc:
                report.parse_errors.append(
                    Violation("--", rel, exc.lineno or 0, f"could not parse: {exc.msg}")
                )
                continue
            report.violations.extend(violations)
            report.escapes_used += escapes
    report.violations.sort(key=lambda v: (v.path, v.line, v.rule))
    return report


def print_report(report: Report) -> None:
    for violation in [*report.parse_errors, *report.violations]:
        print(violation.render())
    if not report.ok:
        print()
    counts = ", ".join(f"{rule.code}={len(report.by_rule(rule.code))}" for rule in RULES)
    total = len(report.violations) + len(report.parse_errors)
    print(
        f"rule_linter: {len(RULES)} rules checked, {report.files_scanned} files scanned, "
        f"{total} violations ({counts})"
    )
    if report.escapes_used:
        print(f"rule_linter: {report.escapes_used} `# lint: allow-literal` escape(s) in use")
    print("rule_linter: PASS" if report.ok else "rule_linter: FAIL")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="rule_linter",
        description="Enforce the CLAUDE.md hard rules (L1-L5) with stdlib ast only.",
    )
    parser.add_argument("paths", nargs="*", help="files or directories (default: the repo root)")
    parser.add_argument(
        "--root",
        type=Path,
        default=ROOT,
        help="repo root that path scopes resolve against (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    root: Path = args.root.resolve()
    targets = [Path(p).resolve() for p in args.paths] if args.paths else [root]
    report = lint_paths(targets, root)
    print_report(report)
    return 0 if report.ok else 1


if __name__ == "__main__":
    # Messages carry em dashes and §; a legacy Windows console is cp1252.
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    sys.exit(main())
