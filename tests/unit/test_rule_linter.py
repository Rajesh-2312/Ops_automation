"""Tests for tools/rule_linter.py.

A linter with no tests is a linter nobody trusts: every rule gets a
deliberately-violating snippet and a clean snippet. L3 (agents have no release
capability, CLAUDE.md R3) gets the most coverage — it is the backstop for the
platform's most important safety property, and it is the one someone will be
tempted to work around at 11pm.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from tools.rule_linter import Report, check_source, lint_paths, main

Writer = Callable[[str, str], Path]


def codes(report: Report) -> list[str]:
    return [v.rule for v in report.violations]


def lint(tmp_path: Path) -> Report:
    """Lint a fake repo rooted at tmp_path, so path scopes resolve normally."""
    return lint_paths([tmp_path], tmp_path)


# --- L1: no float where money lives ------------------------------------------


def test_l1_flags_float_in_remuneration(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        def earned(rate: float, days: int) -> float:
            return round(float(rate) * days * 1.5, 2)
        """,
        "app/services/remuneration/engine.py",
    )
    report = lint(tmp_path)
    assert codes(report).count("L1") >= 4  # annotation x2, float(), round(), 1.5
    assert "rupees" in report.by_rule("L1")[0].message


def test_l1_flags_float_in_domain_money(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module("TDS_RATE = 0.10\n", "app/domain/money.py")
    report = lint(tmp_path)
    assert codes(report) == ["L1"]


def test_l1_clean_decimal_engine(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        from decimal import Decimal

        def earned(rate: Decimal, payable_days: Decimal, days_in_month: Decimal) -> Decimal:
            return rate * payable_days / days_in_month
        """,
        "app/services/remuneration/engine.py",
    )
    assert lint(tmp_path).violations == []


def test_l1_does_not_reach_outside_money_code(tmp_module: Writer, tmp_path: Path) -> None:
    """A float in a non-money module is somebody else's problem."""
    tmp_module("LATENCY_BUDGET_SECONDS = 2.5\n", "app/core/config.py")
    assert lint(tmp_path).violations == []


# --- L2: no LLM in the remuneration engine -----------------------------------


def test_l2_flags_llm_import_in_remuneration(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        from app.core.llm import complete

        def variance_reason() -> str:
            return complete("why is this payout high?")
        """,
        "app/services/remuneration/validators.py",
    )
    report = lint(tmp_path)
    assert codes(report) == ["L2"]
    assert "never produce one" in report.violations[0].message


def test_l2_flags_network_clients(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module("import httpx\nimport openai\n", "app/services/remuneration/generators.py")
    assert codes(lint(tmp_path)) == ["L2", "L2"]


def test_l2_flags_any_name_containing_llm(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module("from app.core import llm_router\n", "app/services/remuneration/engine.py")
    assert codes(lint(tmp_path)) == ["L2"]


def test_l2_clean_pure_python_engine(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        from decimal import Decimal

        from app.domain.enums import RateBasis

        def rate_per_day(rate: Decimal, basis: RateBasis) -> Decimal:
            return rate
        """,
        "app/services/remuneration/engine.py",
    )
    assert lint(tmp_path).violations == []


# --- L3: agents have no release capability -----------------------------------


def test_l3_flags_send_tool_in_toolset(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        tools = [read_program, send_email]
        """,
        "app/agents/payout.py",
    )
    report = lint(tmp_path)
    assert codes(report) == ["L3"]
    message = report.violations[0].message
    assert "send_email" in message
    assert "enforced by tool binding, not by prompt instruction" in message
    assert "temporarily" in message


def test_l3_flags_every_release_verb(tmp_module: Writer, tmp_path: Path) -> None:
    banned = [
        "send_whatsapp",
        "post_message",
        "release_payout",
        "mark_released",
        "email_college",
        "whatsapp_trainer",
        "notify_finance",
        "publish_report",
    ]
    for index, name in enumerate(banned):
        tmp_module(f"TOOLS = [{name}]\n", f"app/agents/a{index}.py")
    report = lint(tmp_path)
    assert len(report.violations) == len(banned)
    assert all(v.rule == "L3" for v in report.violations)
    for name in banned:
        assert any(name in v.message for v in report.violations), name


def test_l3_flags_tools_keyword_argument(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        graph = create_agent(model, tools=[read_batch, mark_released])
        """,
        "app/agents/supervisor.py",
    )
    report = lint(tmp_path)
    assert codes(report) == ["L3"]
    assert "mark_released" in report.violations[0].message


def test_l3_flags_tool_wrapped_in_a_factory(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        toolset = (Tool(name="send_invoice", func=send_invoice),)
        """,
        "app/agents/tools/__init__.py",
    )
    report = lint(tmp_path)
    assert report.violations, "a send tool hidden behind a Tool(...) factory must still fail"
    assert all(v.rule == "L3" for v in report.violations)
    assert any("send_invoice" in v.message for v in report.violations)


def test_l3_flags_non_allowlisted_tool(tmp_module: Writer, tmp_path: Path) -> None:
    """The allowlist is positive: unknown verbs fail even without a send prefix."""
    tmp_module("tools = [read_program, update_program]\n", "app/agents/intake.py")
    report = lint(tmp_path)
    assert codes(report) == ["L3"]
    assert "update_program" in report.violations[0].message


def test_l3_flags_send_capable_import(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        "from app.services.comms.outbound import queue_whatsapp\n",
        "app/agents/monitor.py",
    )
    report = lint(tmp_path)
    assert codes(report) and set(codes(report)) == {"L3"}
    assert "send capability" in report.violations[0].message


def test_l3_flags_starred_send_toolset(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module("tools = [*READ_TOOLS, *SEND_TOOLS]\n", "app/agents/comms.py")
    report = lint(tmp_path)
    assert codes(report) == ["L3"]
    assert "SEND_TOOLS" in report.violations[0].message


def test_l3_denylist_beats_allowlist(tmp_module: Writer, tmp_path: Path) -> None:
    """A send-capable tool must not hide behind a read-shaped prefix.

    Regression: the allowlist was originally checked first and short-circuited,
    so `read_and_send` matched `^read_` and was waved through without ever being
    tested against the forbidden verbs. That is R3's "temporarily" failure mode
    arriving by accident rather than by decision, which is worse — nobody
    reviews a tool that the linter says is fine.
    """
    tmp_module(
        "tools = [read_and_send, get_or_publish, list_then_notify]\n",
        "app/agents/sneaky.py",
    )
    report = lint(tmp_path)
    assert codes(report) == ["L3", "L3", "L3"]
    flagged = " ".join(v.message for v in report.violations)
    for name in ("read_and_send", "get_or_publish", "list_then_notify"):
        assert name in flagged
        assert "RELEASE-CAPABLE" in flagged


def test_l3_clean_read_and_draft_toolset(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        from app.agents.tools.reads import read_program

        PAYOUT_TOOLS = [
            read_program,
            get_attendance,
            list_deployments,
            search_sops,
            save_draft,
        ]

        graph = create_agent(model, tools=PAYOUT_TOOLS)
        """,
        "app/agents/payout.py",
    )
    assert lint(tmp_path).violations == []


def test_l3_does_not_reach_outside_agents(tmp_module: Writer, tmp_path: Path) -> None:
    """The comms service is *allowed* to send. Only agents are constrained."""
    tmp_module("tools = [send_email]\n", "app/services/comms/queue.py")
    assert lint(tmp_path).violations == []


# --- L4: domain purity -------------------------------------------------------


def test_l4_flags_infrastructure_import_in_domain(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        import sqlalchemy
        from app.db.models import Trainer
        """,
        "app/domain/payout.py",
    )
    report = lint(tmp_path)
    assert codes(report) == ["L4", "L4"]
    assert "without a database" in report.violations[0].message


def test_l4_flags_relative_escape_from_domain(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module("from ..db import session\n", "app/domain/rules.py")
    assert codes(lint(tmp_path)) == ["L4"]


def test_l4_clean_domain_module(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        from dataclasses import dataclass
        from decimal import Decimal

        from app.domain.enums import ProgramType


        @dataclass(frozen=True)
        class Engagement:
            program_type: ProgramType
            rate: Decimal
        """,
        "app/domain/engagement.py",
    )
    assert lint(tmp_path).violations == []


# --- L5: enums, never string literals for status -----------------------------


def test_l5_flags_status_string_comparison(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        def is_open(task) -> bool:
            return task.status == "pending"
        """,
        "app/api/tasks.py",
    )
    report = lint(tmp_path)
    assert codes(report) == ["L5"]
    assert "TaskStatus.PENDING" not in report.violations[0].message  # no enums.py in tmp repo
    assert "enums" in report.violations[0].message


def test_l5_flags_membership_and_reversed_operands(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        def check(run, user) -> bool:
            if "DRAFT" != run.artifact_state:
                return False
            return user.role in ("manager", "senior_manager")
        """,
        "app/api/approvals.py",
    )
    assert codes(lint(tmp_path)) == ["L5", "L5"]


def test_l5_suggests_the_real_enum_member(
    tmp_module: Writer, tmp_path: Path, repo_root: Path
) -> None:
    """With the real enums.py present the message names the exact member."""
    tmp_module(
        (repo_root / "app" / "domain" / "enums.py").read_text(encoding="utf-8"),
        "app/domain/enums.py",
    )
    tmp_module('ok = program.stage == "deployment"\n', "app/api/programs.py")
    report = lint(tmp_path)
    assert codes(report) == ["L5"]
    assert "ProgramStage.DEPLOYMENT" in report.violations[0].message


def test_l5_escape_hatch_suppresses_and_is_counted(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module(
        """
        def is_ok(response) -> bool:
            return response.state == "ok"  # lint: allow-literal third-party API vocabulary
        """,
        "app/services/erm/sync.py",
    )
    report = lint(tmp_path)
    assert report.violations == []
    assert report.escapes_used == 1


def test_l5_stays_conservative(tmp_module: Writer, tmp_path: Path) -> None:
    """False positives get linters disabled, which is worse than a missed case."""
    tmp_module(
        """
        from app.domain.enums import TaskStatus


        def looks_fine(task, name: str, other) -> bool:
            a = task.status == TaskStatus.PENDING       # enum: fine
            b = task.title == "kickoff"                 # not a status attribute
            c = name == "pending"                       # plain name, not attribute access
            d = task.status == other.status             # attribute vs attribute
            e = task.status in ALLOWED_STATUSES         # non-literal container
            f = task.status is None                     # `is`, not == / in
            return a and b and c and d and e and f
        """,
        "app/services/approval/state.py",
    )
    assert lint(tmp_path).violations == []


# --- runner behaviour --------------------------------------------------------


def test_missing_directories_are_silent(tmp_path: Path) -> None:
    """Other agents are still creating app/; an absent tree is not an error."""
    report = lint_paths([tmp_path / "app", tmp_path / "nope"], tmp_path)
    assert report.ok
    assert report.files_scanned == 0


def test_tests_and_vendor_trees_are_skipped(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module("tools = [send_email]\n", "tests/unit/test_fixture.py")
    tmp_module("tools = [send_email]\n", "node_modules/pkg/app/agents/x.py")
    tmp_module("tools = [send_email]\n", "frontend/app/agents/x.py")
    report = lint(tmp_path)
    assert report.files_scanned == 0
    assert report.violations == []


def test_unparseable_file_is_reported_not_crashed(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module("def broken(:\n", "app/agents/wip.py")
    report = lint(tmp_path)
    assert not report.ok
    assert report.parse_errors and report.parse_errors[0].rule == "--"


def test_check_source_selects_rules_by_path() -> None:
    violations, escapes = check_source("x = 1.5\n", "anywhere.py")
    assert violations == [] and escapes == 0
    violations, _ = check_source("x = 1.5\n", "app/services/remuneration/engine.py")
    assert [v.rule for v in violations] == ["L1"]


def test_violation_line_format_is_clickable(tmp_module: Writer, tmp_path: Path) -> None:
    tmp_module("# one\n# two\ntools = [send_email]\n", "app/agents/x.py")
    rendered = lint(tmp_path).violations[0].render()
    assert rendered.startswith("app/agents/x.py:3: [L3] ")


def test_main_exits_zero_on_a_clean_tree(tmp_module: Writer, tmp_path: Path, capsys) -> None:
    tmp_module("tools = [read_program, save_draft]\n", "app/agents/x.py")
    assert main(["--root", str(tmp_path), str(tmp_path / "app")]) == 0
    assert "rule_linter: PASS" in capsys.readouterr().out


def test_main_exits_one_on_a_violation(tmp_module: Writer, tmp_path: Path, capsys) -> None:
    tmp_module("tools = [send_email]\n", "app/agents/x.py")
    assert main(["--root", str(tmp_path), str(tmp_path / "app")]) == 1
    out = capsys.readouterr().out
    assert "rule_linter: FAIL" in out
    assert "rules checked" in out and "files scanned" in out


def test_real_repo_lints_without_crashing(repo_root: Path) -> None:
    report = lint_paths([repo_root / "app"], repo_root)
    assert isinstance(report, Report)
