# tools/

Repo tooling. Stdlib only — these run in CI before dependencies are installed.

## `rule_linter.py`

A static linter for the CLAUDE.md §1 hard rules. `ruff` catches style; this
catches the five things that are defects rather than preferences. It parses with
`ast` (no imports of `app/`, no database, no network) and exits non-zero on any
violation, so CI fails the build.

```bash
python tools/rule_linter.py                      # whole repo
python tools/rule_linter.py app/agents app/domain # explicit paths
python tools/rule_linter.py --root . app/agents   # scopes resolve against --root
```

Output is `path:line: [RULE] message` — relative, clickable, one line each —
followed by a summary:

```
app/agents/payout.py:12: [L3] RELEASE-CAPABLE TOOL `send_email` bound to an agent. ...

rule_linter: 5 rules checked, 22 files scanned, 1 violations (L1=0, L2=0, L3=1, L4=0, L5=0)
rule_linter: FAIL
```

Missing directories are silent — the tree is still being built, and a rule with
nothing in scope is not an error. `tests/`, `frontend/`, `.venv/` and
`node_modules/` are never scanned.

### The rules

| Rule | Scope | Enforces |
|---|---|---|
| **L1** | `app/services/remuneration/`, `app/domain/money.py` | **R7** — no `float` name, float literal, `float()` call, or builtin `round()`. `Decimal` only. Floats silently lose rupees, and `round()` both returns a float for float input and rounds mid-calculation (R6: round once, at net pay, via `Decimal.quantize`). Carve-out: `isinstance(x, float)` is allowed — a guard that *rejects* a float is R7 being enforced. |
| **L2** | `app/services/remuneration/` | **R2** — no import of `app.core.llm`, `openai`, `anthropic`, `httpx`, or any module whose name contains `llm`. An agent may explain a number; it may never produce one. |
| **L3** | `app/agents/` | **R3** — any collection assigned to a `*tools`/`*toolset` name, or passed as `tools=`, may contain only `read_`/`get_`/`list_`/`search_` names plus `save_draft`. `send_`, `post_`, `release_`, `mark_released`, `email_`, `whatsapp_`, `notify_`, `publish_` are hard failures, as is importing a module whose name suggests sending. |
| **L4** | `app/domain/` | **§3** — no import of `app.db`, `app.api`, `app.agents`, `app.services`, `sqlalchemy`, `fastapi`, `supabase` (relative imports are resolved first). Business rules stay testable without a database. |
| **L5** | `app/` | **§11** — enums, never string literals for status. Flags `==`/`!=`/`in` between an attribute ending in `status`/`state`/`role`/`stage`/`mark` and a plain string literal, and names the exact member from `app/domain/enums.py` when the value matches one. |

**L3 is the important one.** It is the machine-checked half of R3: *"enforced by
tool binding, not by prompt instruction — never add a send-capable tool to an
agent's toolset 'temporarily'."* A prompt instruction not to send is a request;
an empty send-capable toolset is a guarantee. `tests/unit/test_rule_linter.py`
exercises every banned verb, the `tools=` keyword form, factory-wrapped tools
(`Tool(name="send_invoice", ...)`) and starred splats, so the guarantee itself
has a test.

### Escape hatch

L5 only. Put the reason on the same line:

```python
if response.state == "ok":  # lint: allow-literal third-party ERM vocabulary
```

The run reports how many escapes are in use, so they stay visible rather than
accumulating quietly. L1–L4 have no escape hatch by design: if one of those
fires, the code is wrong, not the linter.

### Adding a rule

Subclass `Rule`, set `code`, `title` and `scopes` (path prefixes; a trailing `/`
means directory), implement the `visit_*` methods, and add the class to `RULES`.
`self.report(node, message)` records a violation. Every rule needs a violating
*and* a clean test in `tests/unit/test_rule_linter.py` — a false positive is
worse than a missed case, because it gets the whole linter switched off.

### False positives

Report them; do not disable the rule. If a rule is wrong, fix the rule and add
the clean snippet to the test file so it stays fixed.
