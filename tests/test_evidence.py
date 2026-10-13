import json
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from jev.evidence import (
    EVIDENCE_ORDER,
    evidence_level,
    format_evidence_summary,
    weakest,
)
from jev.models import (
    CompileOutcome,
    MechanicalCheckResult,
    State,
    Subgoal,
    TestOutcome,
    TestPolicy,
    ValidationVerdict,
)
from jev.engine import JevEngine, node_gate, node_verify
from jev.gatekeeper import Gatekeeper
from jev.ledger import NullRecorder, SqliteLedger
from jev.workspace import Workspace


# ==============================================================================
# 1. Table-driven tests of evidence_level
# ==============================================================================

@pytest.mark.parametrize(
    "compile_out, test_out, policy, expected",
    [
        # Rule 1: compile PASSED and tests PASSED -> "tests+compile"
        (CompileOutcome.PASSED, TestOutcome.PASSED, "auto", "tests+compile"),
        ("PASSED", "PASSED", "auto", "tests+compile"),
        (CompileOutcome.PASSED, TestOutcome.PASSED, "always", "tests+compile"),
        ({"outcome": "PASSED"}, {"outcome": "PASSED"}, "auto", "tests+compile"),

        # Rule 2: tests PASSED and compile is not PASSED -> "tests-only"
        (CompileOutcome.EXEMPT, TestOutcome.PASSED, "auto", "tests-only"),
        (None, TestOutcome.PASSED, "auto", "tests-only"),
        (CompileOutcome.NO_COMPILE_COMMAND, TestOutcome.PASSED, "auto", "tests-only"),
        ("EXEMPT", "PASSED", "auto", "tests-only"),
        ("NO_COMPILE_COMMAND", "PASSED", "auto", "tests-only"),
        (CompileOutcome.FAILED, TestOutcome.PASSED, "auto", "tests-only"),
        (CompileOutcome.ENV_NOT_READY, TestOutcome.PASSED, "auto", "tests-only"),

        # Rule 3: compile PASSED and tests anything else -> "compile-only"
        (CompileOutcome.PASSED, TestOutcome.NO_TESTS_COLLECTED, "auto", "compile-only"),
        (CompileOutcome.PASSED, TestOutcome.NO_TEST_FRAMEWORK, "auto", "compile-only"),
        (CompileOutcome.PASSED, None, "auto", "compile-only"),
        (CompileOutcome.PASSED, None, "never", "compile-only"),
        (CompileOutcome.PASSED, None, "verify-only", "compile-only"),
        (CompileOutcome.PASSED, TestOutcome.FAILED, "auto", "compile-only"),
        (CompileOutcome.PASSED, TestOutcome.ENV_NOT_READY, "auto", "compile-only"),
        ("PASSED", "NO_TESTS_COLLECTED", "auto", "compile-only"),
        ("PASSED", "NO_TEST_FRAMEWORK", "auto", "compile-only"),
        # tests_ran True with NO_TESTS_COLLECTED -> compile-only
        ("PASSED", {"outcome": "NO_TESTS_COLLECTED", "ran": True}, "auto", "compile-only"),
        (CompileOutcome.PASSED, {"outcome": "NO_TESTS_COLLECTED", "ran": True}, "auto", "compile-only"),
        # policy never overrides even if test_out was PASSED
        (CompileOutcome.PASSED, TestOutcome.PASSED, "never", "compile-only"),

        # Rule 4: everything else -> "semantic-only"
        (CompileOutcome.EXEMPT, TestOutcome.NO_TESTS_COLLECTED, "auto", "semantic-only"),
        (CompileOutcome.NO_COMPILE_COMMAND, TestOutcome.NO_TESTS_COLLECTED, "auto", "semantic-only"),
        (None, None, "auto", "semantic-only"),
        (None, None, "never", "semantic-only"),
        (CompileOutcome.FAILED, TestOutcome.FAILED, "auto", "semantic-only"),
        (CompileOutcome.ENV_NOT_READY, TestOutcome.FAILED, "auto", "semantic-only"),
        (CompileOutcome.EXEMPT, None, "verify-only", "semantic-only"),
        ("EXEMPT", "NO_TESTS_COLLECTED", "auto", "semantic-only"),
        ("FAILED", "FAILED", "auto", "semantic-only"),
    ],
)
def test_evidence_level_rules(compile_out, test_out, policy, expected):
    """Verifies all evidence_level derivation rules across outcomes and policies."""
    assert evidence_level(compile_out, test_out, policy) == expected


# ==============================================================================
# 2. weakest() ordering tests
# ==============================================================================

def test_weakest_ordering():
    """Ordering: semantic-only < compile-only < tests-only < tests+compile."""
    assert EVIDENCE_ORDER == ("semantic-only", "compile-only", "tests-only", "tests+compile")

    assert weakest(["tests+compile", "compile-only"]) == "compile-only"
    assert weakest(["tests+compile", "tests-only"]) == "tests-only"
    assert weakest(["tests-only", "compile-only"]) == "compile-only"
    assert weakest(["compile-only", "semantic-only"]) == "semantic-only"
    assert weakest(["tests+compile", "semantic-only", "compile-only"]) == "semantic-only"
    assert weakest(["tests+compile"]) == "tests+compile"
    assert weakest(["tests-only"]) == "tests-only"
    assert weakest(["compile-only"]) == "compile-only"
    assert weakest(["semantic-only"]) == "semantic-only"
    assert weakest([]) is None
    assert weakest([None]) is None
    assert weakest([None, "tests+compile", None]) == "tests+compile"


# ==============================================================================
# 3. Additive migration on old-schema DB: old rows stay NULL
# ==============================================================================

def test_old_schema_migration_preserves_null_evidence_level(tmp_path):
    """Migration adds attempts.evidence_level and runs.evidence_level with DEFAULT NULL and NO backfill."""
    db_file = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        """
        CREATE TABLE runs (
          run_id TEXT PRIMARY KEY,
          thread_id TEXT,
          ticket TEXT,
          base_commit TEXT,
          final_commit TEXT,
          engine_commit TEXT,
          worker_model TEXT,
          test_policy TEXT,
          retry_intel TEXT,
          status TEXT,
          started_at TEXT,
          ended_at TEXT,
          notes_chars INTEGER
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE attempts (
          run_id TEXT,
          subgoal_id TEXT,
          attempt_no INTEGER,
          tier TEXT,
          outcome TEXT,
          failed_check TEXT,
          diff TEXT,
          diff_sha TEXT,
          files_json TEXT,
          compile_errors_json TEXT,
          tests_ran INTEGER,
          test_outcome TEXT,
          jev_valid INTEGER,
          jev_probability REAL,
          jev_model TEXT,
          jev_raw TEXT,
          jev_tokens_in INTEGER,
          jev_tokens_out INTEGER,
          feedback_sent TEXT,
          tokens_in INTEGER,
          tokens_out INTEGER,
          created_at TEXT,
          PRIMARY KEY (run_id, subgoal_id, attempt_no)
        );
        """
    )
    conn.execute("INSERT INTO runs (run_id, ticket) VALUES ('run-old', 'ticket-old')")
    conn.execute(
        "INSERT INTO attempts (run_id, subgoal_id, attempt_no, tier, outcome) VALUES ('run-old', 'sg-1', 1, 'mechanical', 'failed')"
    )
    conn.commit()
    conn.close()

    # Opening with SqliteLedger must migrate schema additively
    ledger = SqliteLedger(db_path=db_file)

    conn = sqlite3.connect(str(db_file))
    runs_cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    att_cols = [r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()]
    assert "evidence_level" in runs_cols
    assert "evidence_level" in att_cols

    # Old rows must stay NULL
    run_row = conn.execute("SELECT evidence_level FROM runs WHERE run_id = 'run-old'").fetchone()
    att_row = conn.execute("SELECT evidence_level FROM attempts WHERE run_id = 'run-old'").fetchone()
    conn.close()

    assert run_row[0] is None
    assert att_row[0] is None


# ==============================================================================
# 4. Engine-level tests
# ==============================================================================

class FakeGatekeeper:
    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.95, reason="Approved")
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.98, reason="Verified")
        self.validate_subgoal = MagicMock(side_effect=lambda sg, diff, mechanical_detail="", investigation_notes="": self.verdict)
        self.verify_ticket = MagicMock(side_effect=lambda ticket, diff, test_out, investigation_notes="": self.verify_verdict)
        self.escalate_deadlock = MagicMock()


class ScriptedChatModel:
    def __init__(self, responses):
        self.responses = list(responses)

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        from langchain_core.messages import AIMessage
        if not self.responses:
            return AIMessage(content="[]")
        resp = self.responses.pop(0)
        if isinstance(resp, str):
            return AIMessage(content=resp)
        return resp


@pytest.fixture
def test_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "master"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    src = repo / "src"
    src.mkdir()
    f = src / "app.py"
    f.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    tests = repo / "tests"
    tests.mkdir()
    t = tests / "test_app.py"
    t.write_text("from src.app import add\ndef test_add():\n    assert add(1, 2) == 3\n", encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    return repo


def test_engine_test_policy_never_evidence_level(test_repo, tmp_path):
    """--test-policy never -> run and attempt evidence 'compile-only' (or 'semantic-only' if compile exempt)."""
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_never.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    subgoal = Subgoal(id="sg-1", description="Update app.py", scope=["src/app.py"], expects_tests=True)
    # Stage a valid change
    target = test_repo / "src" / "app.py"
    target.write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=test_repo, check=True, capture_output=True)

    diff = ws.get_staged_diff()

    state: State = {
        "run_id": "run-never",
        "thread_id": "run-never",
        "ticket": "Add feature",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "never",
    }
    ledger.record_run_start("run-never", "run-never", "Add feature", test_policy="never")
    ledger.record_subgoals("run-never", [subgoal])

    res = node_gate(state, workspace=ws, gatekeeper=gk, recorder=ledger)
    assert res["gate_status"] == "passed"

    # End run to trigger runs.evidence_level computation
    ledger.record_run_end("run-never", final_status="completed")

    conn = sqlite3.connect(str(db_file))
    run_row = conn.execute("SELECT evidence_level FROM runs WHERE run_id = 'run-never'").fetchone()
    att_row = conn.execute("SELECT evidence_level, tier, outcome FROM attempts WHERE run_id = 'run-never' AND subgoal_id = 'sg-1'").fetchone()
    conn.close()

    expected_level = "compile-only"  # Python check_compile is PASSED via AST check
    assert run_row[0] == expected_level
    assert att_row[0] == expected_level


def test_engine_test_policy_verify_only_same_as_never_for_subgoals(test_repo, tmp_path):
    """--test-policy verify-only -> same as never for per-subgoal attempts."""
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_vo.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    subgoal = Subgoal(id="sg-1", description="Update app.py", scope=["src/app.py"], expects_tests=True)
    target = test_repo / "src" / "app.py"
    target.write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=test_repo, check=True, capture_output=True)

    state: State = {
        "run_id": "run-vo",
        "thread_id": "run-vo",
        "ticket": "Add feature",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "verify-only",
    }
    ledger.record_run_start("run-vo", "run-vo", "Add feature", test_policy="verify-only")
    ledger.record_subgoals("run-vo", [subgoal])

    res = node_gate(state, workspace=ws, gatekeeper=gk, recorder=ledger)
    assert res["gate_status"] == "passed"

    ledger.record_run_end("run-vo", final_status="completed")

    conn = sqlite3.connect(str(db_file))
    run_row = conn.execute("SELECT evidence_level FROM runs WHERE run_id = 'run-vo'").fetchone()
    att_row = conn.execute("SELECT evidence_level FROM attempts WHERE run_id = 'run-vo' AND subgoal_id = 'sg-1'").fetchone()
    conn.close()

    expected_level = "compile-only"
    assert run_row[0] == expected_level
    assert att_row[0] == expected_level


def test_engine_passing_tests_and_compile_produces_tests_plus_compile(test_repo, tmp_path):
    """A passing test run + compile -> 'tests+compile'."""
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_pass.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    subgoal = Subgoal(id="sg-1", description="Update app.py", scope=["src/app.py"], expects_tests=True)
    target = test_repo / "src" / "app.py"
    target.write_text("def add(a, b):\n    return a + b + 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=test_repo, check=True, capture_output=True)

    state: State = {
        "run_id": "run-pass",
        "thread_id": "run-pass",
        "ticket": "Add feature",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "always",
    }
    ledger.record_run_start("run-pass", "run-pass", "Add feature", test_policy="always")
    ledger.record_subgoals("run-pass", [subgoal])

    res = node_gate(state, workspace=ws, gatekeeper=gk, recorder=ledger)
    assert res["gate_status"] == "passed"

    ledger.record_run_end("run-pass", final_status="completed")

    conn = sqlite3.connect(str(db_file))
    run_row = conn.execute("SELECT evidence_level FROM runs WHERE run_id = 'run-pass'").fetchone()
    att_row = conn.execute("SELECT evidence_level FROM attempts WHERE run_id = 'run-pass' AND subgoal_id = 'sg-1'").fetchone()
    conn.close()

    assert run_row[0] == "tests+compile"
    assert att_row[0] == "tests+compile"


def test_engine_tier0_rejection_leaves_attempt_evidence_level_null(test_repo, tmp_path):
    """Attempts rejected at Tier 0 stay NULL in attempts.evidence_level."""
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_tier0.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    subgoal = Subgoal(id="sg-1", description="Break syntax", scope=["src/app.py"], expects_tests=False)
    # Stage syntax error
    target = test_repo / "src" / "app.py"
    target.write_text("def broken(:\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=test_repo, check=True, capture_output=True)

    state: State = {
        "run_id": "run-t0",
        "thread_id": "run-t0",
        "ticket": "Break syntax",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "never",
    }
    ledger.record_run_start("run-t0", "run-t0", "Break syntax")
    ledger.record_subgoals("run-t0", [subgoal])

    res = node_gate(state, workspace=ws, gatekeeper=gk, recorder=ledger)
    assert res["gate_status"] == "mechanical_failure"

    ledger.record_run_end("run-t0", final_status="failed")

    conn = sqlite3.connect(str(db_file))
    att_row = conn.execute("SELECT evidence_level, tier, outcome FROM attempts WHERE run_id = 'run-t0' AND subgoal_id = 'sg-1'").fetchone()
    run_row = conn.execute("SELECT evidence_level FROM runs WHERE run_id = 'run-t0'").fetchone()
    conn.close()

    assert att_row[0] is None
    assert run_row[0] is None  # No subgoal committed -> NULL


# ==============================================================================
# 5. Deterministic test: Jev request body contains no evidence key or string
# ==============================================================================

def test_gatekeeper_validate_subgoal_request_contains_no_evidence():
    """Construct Gatekeeper with explicit api_url and api_key, mock transport, assert no evidence."""
    captured_requests = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        captured_requests.append(request)
        return httpx.Response(
            200,
            json={"answers": {"valid": {"value": True, "probability": 0.95}}},
        )

    gk = Gatekeeper(
        api_url="https://api.typesafe.ai/v1/systemone",
        api_key="test-key-no-env",
    )
    gk.client = httpx.Client(transport=httpx.MockTransport(mock_handler))

    subgoal = Subgoal(id="sg-1", description="Implement test", scope=["a.py"], expects_tests=True)
    gk.validate_subgoal(
        subgoal,
        "diff --git a/a.py b/a.py\n+x = 1\n",
        mechanical_detail="ok",
        investigation_notes="notes",
    )

    assert len(captured_requests) == 1
    req_body_str = captured_requests[0].content.decode("utf-8")
    assert "evidence" not in req_body_str.lower()


# ==============================================================================
# 6. Final summary line formatting
# ==============================================================================

def test_final_summary_wording():
    """Wording must say tests did not run whenever the level has no tests, and not recorded when None."""
    # None level: print exactly "Evidence: not recorded (no committed subgoal, or ledger off)"
    s_none = format_evidence_summary(None, test_policy="verify-only")
    assert s_none == "Evidence: not recorded (no committed subgoal, or ledger off)"
    assert "no tests ran" not in s_none

    s_none_no_policy = format_evidence_summary(None)
    assert s_none_no_policy == "Evidence: not recorded (no committed subgoal, or ledger off)"
    assert "no tests ran" not in s_none_no_policy

    # compile-only with verify-only
    s_vo = format_evidence_summary("compile-only", test_policy="verify-only")
    assert s_vo == "Evidence: compile-only (no tests ran: test_policy=verify-only)"

    s_never = format_evidence_summary("compile-only", test_policy="never")
    assert s_never == "Evidence: compile-only (no tests ran: test_policy=never)"

    s_sem = format_evidence_summary("semantic-only", test_policy="verify-only")
    assert s_sem == "Evidence: semantic-only (no tests ran: test_policy=verify-only)"

    # tests+compile
    s_tests = format_evidence_summary("tests+compile", test_policy="auto")
    assert "no tests ran" not in s_tests
    assert s_tests == "Evidence: tests+compile"

    s_testsonly = format_evidence_summary("tests-only", test_policy="auto")
    assert "no tests ran" not in s_testsonly
    assert s_testsonly == "Evidence: tests-only"


def test_escalated_run_evidence_summary_not_recorded(test_repo, tmp_path):
    """Escalated run (no committed subgoal) prints the 'not recorded' line, not 'no tests ran' line."""
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "escalated_summary.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    subgoal = Subgoal(id="sg-1", description="Syntax error", scope=["src/app.py"], expects_tests=False)
    target = test_repo / "src" / "app.py"
    target.write_text("def broken(:\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=test_repo, check=True, capture_output=True)

    state: State = {
        "run_id": "run-esc",
        "thread_id": "run-esc",
        "ticket": "Syntax error ticket",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "test_policy": "verify-only",
    }
    ledger.record_run_start("run-esc", "run-esc", "Syntax error ticket", test_policy="verify-only")
    ledger.record_subgoals("run-esc", [subgoal])

    # Node gate fails at Tier 0
    res = node_gate(state, workspace=ws, gatekeeper=gk, recorder=ledger)
    assert res["gate_status"] == "mechanical_failure"

    # Run escalates, no subgoals committed
    ledger.record_run_end("run-esc", final_status="escalated")

    ev_level = ledger.get_run_evidence_level("run-esc")
    assert ev_level is None

    summary_line = format_evidence_summary(ev_level, test_policy="verify-only")
    assert summary_line == "Evidence: not recorded (no committed subgoal, or ledger off)"
    assert "no tests ran" not in summary_line


def test_cli_escalated_run_evidence_summary_in_main(test_repo, monkeypatch, capsys):
    """CLI run ending in escalation prints 'Evidence: not recorded (no committed subgoal, or ledger off)'."""
    from main import main
    monkeypatch.chdir(test_repo)
    monkeypatch.setenv("JEV_API_KEY", "dummy-key")
    monkeypatch.setenv("JEV_API_URL", "https://api.typesafe.ai/v1/systemone")

    mock_eng = MagicMock()
    mock_eng.execute.return_value = {"status": "escalated", "gate_status": "mechanical_deadlock"}
    with patch("main.Gatekeeper"), patch("main.JevEngine", return_value=mock_eng):
        exit_code = main(["Broken ticket", "--no-ledger", "--test-policy", "verify-only", "--repo-dir", str(test_repo)])
        assert exit_code == 1

    captured = capsys.readouterr()
    assert "Evidence: not recorded (no committed subgoal, or ledger off)" in captured.out
    assert "no tests ran" not in captured.out
