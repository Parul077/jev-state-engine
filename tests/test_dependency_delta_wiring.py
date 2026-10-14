import json
import logging
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolCall

from jev.dependency_delta import (
    DepChange,
    DependencyDelta,
    compute_dependency_delta,
    format_dependency_summary,
    is_dependency_manifest_path,
)
from jev.engine import JevEngine
from jev.ledger import NullRecorder, SqliteLedger
from jev.models import State, Subgoal, ValidationVerdict
from jev.workspace import Workspace


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
        self.invoked_messages = []

    def bind_tools(self, tools, **kwargs):
        return self

    def invoke(self, messages, **kwargs):
        self.invoked_messages.append(messages)
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

    pkg = repo / "package.json"
    pkg.write_text('{\n  "dependencies": {\n    "lodash": "4.17.21"\n  }\n}\n', encoding="utf-8")

    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=repo, check=True, capture_output=True)

    return repo


# ==============================================================================
# 1. Helpers & format_dependency_summary tests
# ==============================================================================

def test_is_dependency_manifest_path():
    # Supported manifests
    assert is_dependency_manifest_path("package.json") is True
    assert is_dependency_manifest_path("apps/web/package.json") is True
    assert is_dependency_manifest_path("pyproject.toml") is True
    assert is_dependency_manifest_path("requirements.txt") is True
    assert is_dependency_manifest_path("requirements-dev.txt") is True
    assert is_dependency_manifest_path("go.mod") is True
    assert is_dependency_manifest_path("Cargo.toml") is True
    assert is_dependency_manifest_path("pom.xml") is True
    assert is_dependency_manifest_path("build.gradle") is True
    assert is_dependency_manifest_path("build.gradle.kts") is True

    # Known unsupported manifests
    assert is_dependency_manifest_path("Gemfile") is True
    assert is_dependency_manifest_path("composer.json") is True
    assert is_dependency_manifest_path("build.sbt") is True
    assert is_dependency_manifest_path("MyApp.csproj") is True
    assert is_dependency_manifest_path("MyLib.fsproj") is True
    assert is_dependency_manifest_path("Legacy.vbproj") is True
    assert is_dependency_manifest_path("my.gemspec") is True
    assert is_dependency_manifest_path("My.podspec") is True

    # Lockfiles are False
    assert is_dependency_manifest_path("package-lock.json") is False
    assert is_dependency_manifest_path("yarn.lock") is False
    assert is_dependency_manifest_path("pnpm-lock.yaml") is False
    assert is_dependency_manifest_path("Cargo.lock") is False
    assert is_dependency_manifest_path("go.sum") is False
    assert is_dependency_manifest_path("Gemfile.lock") is False

    # Other files are False
    assert is_dependency_manifest_path("src/app.py") is False
    assert is_dependency_manifest_path("README.md") is False
    assert is_dependency_manifest_path("tsconfig.json") is False


def test_dependency_delta_json_helpers_and_summary_formatting():
    # None -> "Dependencies: not computed"
    assert format_dependency_summary(None) == "Dependencies: not computed"

    # Empty delta -> "Dependencies: no changes"
    empty_delta = DependencyDelta()
    assert empty_delta.to_json() is not None
    loaded = DependencyDelta.from_json(empty_delta.to_json())
    assert loaded.is_empty is True
    assert format_dependency_summary(empty_delta.to_json()) == "Dependencies: no changes"

    # Added delta -> "Dependencies: " plus to_summary_text()
    delta_with_added = DependencyDelta(
        added=[
            DepChange(
                name="axios",
                ecosystem="js_ts",
                manifest="package.json",
                section="dependencies",
                new_version="1.4.0",
            )
        ]
    )
    summary = format_dependency_summary(delta_with_added.to_json())
    assert summary.startswith("Dependencies: ")
    assert "axios" in summary

    # Unparsed manifest
    unparsed_delta = DependencyDelta(unparsed=["Gemfile"])
    summary_unparsed = format_dependency_summary(unparsed_delta.to_json())
    assert "unparsed: Gemfile" in summary_unparsed


# ==============================================================================
# 2. Workspace.get_manifest_changes test
# ==============================================================================

def test_get_manifest_changes_does_not_modify_repo(test_repo):
    ws = Workspace(repo_dir=test_repo)
    status_before = subprocess.run(["git", "status", "--porcelain"], cwd=test_repo, capture_output=True, text=True).stdout

    changes = ws.get_manifest_changes("HEAD")
    assert isinstance(changes, dict)

    status_after = subprocess.run(["git", "status", "--porcelain"], cwd=test_repo, capture_output=True, text=True).stdout
    assert status_before == status_after


# ==============================================================================
# 3. Integration tests with Engine and Ledger
# ==============================================================================

def test_worker_edits_package_json_recorded_in_attempt_and_run(test_repo, tmp_path):
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    new_pkg = '{\n  "dependencies": {\n    "lodash": "4.17.21",\n    "axios": "1.4.0"\n  }\n}\n'
    # Responses: 1. investigate finish, 2. plan, 3. implement stage mutation, 4. implement submit
    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="finish_investigation", args={"summary": "ready"}, id="c1")]),
        json.dumps([{"description": "Add axios to package.json", "scope": ["package.json"], "expects_tests": False}]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "package.json", "content": new_pkg}, id="c2")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c3")]),
    ]
    llm = ScriptedChatModel(responses)

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        recorder=ledger,
        db_path=db_file,
    )
    final_state = engine.execute(ticket="Add axios", thread_id="t-axios")
    assert final_state["status"] == "completed"

    with sqlite3.connect(str(db_file)) as conn:
        run_row = conn.execute("SELECT dependency_delta FROM runs WHERE run_id = 't-axios'").fetchone()
        assert run_row is not None and run_row[0] is not None
        run_delta = DependencyDelta.from_json(run_row[0])
        assert any(c.name == "axios" for c in run_delta.added)

        att_row = conn.execute("SELECT dependency_delta FROM attempts WHERE run_id = 't-axios' AND subgoal_id != '__verify__'").fetchone()
        assert att_row is not None and att_row[0] is not None
        att_delta = DependencyDelta.from_json(att_row[0])
        assert any(c.name == "axios" for c in att_delta.added)

    summary = format_dependency_summary(ledger.get_run_dependency_delta("t-axios"))
    assert "axios" in summary


def test_worker_creates_brand_new_manifest_untracked_case(test_repo, tmp_path):
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_new.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    req_content = "requests==2.31.0\n"
    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="finish_investigation", args={"summary": "ready"}, id="c1")]),
        json.dumps([{"description": "Create requirements.txt", "scope": ["requirements.txt"], "expects_tests": False}]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "requirements.txt", "content": req_content}, id="c2")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c3")]),
    ]
    llm = ScriptedChatModel(responses)

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        recorder=ledger,
        db_path=db_file,
    )
    final_state = engine.execute(ticket="Add requirements", thread_id="t-req")
    assert final_state["status"] == "completed"

    with sqlite3.connect(str(db_file)) as conn:
        run_row = conn.execute("SELECT dependency_delta FROM runs WHERE run_id = 't-req'").fetchone()
        assert run_row is not None and run_row[0] is not None
        run_delta = DependencyDelta.from_json(run_row[0])
        assert any(c.name == "requests" for c in run_delta.added)


def test_worker_changes_only_source_file_empty_delta_stored(test_repo, tmp_path):
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_src.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    new_app = "def add(a, b):\n    return a + b + 0\n"
    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="finish_investigation", args={"summary": "ready"}, id="c1")]),
        json.dumps([{"description": "Update app.py", "scope": ["src/app.py"], "expects_tests": False}]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "src/app.py", "content": new_app}, id="c2")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c3")]),
    ]
    llm = ScriptedChatModel(responses)

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        recorder=ledger,
        db_path=db_file,
    )
    final_state = engine.execute(ticket="Update app", thread_id="t-src")
    assert final_state["status"] == "completed"

    with sqlite3.connect(str(db_file)) as conn:
        run_row = conn.execute("SELECT dependency_delta FROM runs WHERE run_id = 't-src'").fetchone()
        assert run_row is not None
        assert run_row[0] is not None  # NOT NULL
        run_delta = DependencyDelta.from_json(run_row[0])
        assert run_delta.is_empty is True

        att_row = conn.execute("SELECT dependency_delta FROM attempts WHERE run_id = 't-src' AND subgoal_id != '__verify__'").fetchone()
        assert att_row is not None
        assert att_row[0] is not None  # NOT NULL
        att_delta = DependencyDelta.from_json(att_row[0])
        assert att_delta.is_empty is True

    summary = format_dependency_summary(ledger.get_run_dependency_delta("t-src"))
    assert summary == "Dependencies: no changes"


def test_tier0_rejected_attempt_dependency_delta_is_null(test_repo, tmp_path):
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_t0.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    # Syntax error in app.py -> fails Tier 0 AST check
    bad_syntax = "def broken(:\n"
    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="finish_investigation", args={"summary": "ready"}, id="c1")]),
        json.dumps([{"description": "Broken syntax", "scope": ["src/app.py"], "expects_tests": False}]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "src/app.py", "content": bad_syntax}, id="c2")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c3")]),
        # After 1st strike retry:
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "src/app.py", "content": bad_syntax}, id="c4")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c5")]),
        # After 2nd strike retry:
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "src/app.py", "content": bad_syntax}, id="c6")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c7")]),
    ]
    llm = ScriptedChatModel(responses)

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        recorder=ledger,
        db_path=db_file,
    )
    final_state = engine.execute(ticket="Bad syntax", thread_id="t-bad")
    assert final_state["status"] == "escalated"

    with sqlite3.connect(str(db_file)) as conn:
        att_rows = conn.execute("SELECT dependency_delta FROM attempts WHERE run_id = 't-bad'").fetchall()
        assert len(att_rows) > 0
        for r in att_rows:
            assert r[0] is None  # Attempts rejected at Tier 0 stay NULL


def test_escalated_run_dependency_delta_is_null(test_repo, tmp_path):
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_esc.db"
    ledger = SqliteLedger(db_path=db_file)
    # Reject 3 times semantically -> escalation
    gk = FakeGatekeeper(verdict=ValidationVerdict(valid=False, probability=0.2, reason="Denied"))

    valid_content = "def add(a, b):\n    return a + b + 1\n"
    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="finish_investigation", args={"summary": "ready"}, id="c1")]),
        json.dumps([{"description": "Semantic fail", "scope": ["src/app.py"], "expects_tests": False}]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "src/app.py", "content": valid_content}, id="c2")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c3")]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "src/app.py", "content": valid_content}, id="c4")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c5")]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "src/app.py", "content": valid_content}, id="c6")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c7")]),
    ]
    llm = ScriptedChatModel(responses)

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        recorder=ledger,
        db_path=db_file,
    )
    final_state = engine.execute(ticket="Escalate ticket", thread_id="t-esc")
    assert final_state["status"] == "escalated"

    with sqlite3.connect(str(db_file)) as conn:
        run_row = conn.execute("SELECT dependency_delta FROM runs WHERE run_id = 't-esc'").fetchone()
        assert run_row is not None
        assert run_row[0] is None  # runs.dependency_delta is NULL

    summary = format_dependency_summary(ledger.get_run_dependency_delta("t-esc"))
    assert summary == "Dependencies: not computed"


def test_unsupported_manifest_changed_summary_names_unparsed(test_repo, tmp_path):
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_gem.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    gemfile_content = "source 'https://rubygems.org'\ngem 'rails'\n"
    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="finish_investigation", args={"summary": "ready"}, id="c1")]),
        json.dumps([{"description": "Add Gemfile", "scope": ["Gemfile"], "expects_tests": False}]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "Gemfile", "content": gemfile_content}, id="c2")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c3")]),
    ]
    llm = ScriptedChatModel(responses)

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        recorder=ledger,
        db_path=db_file,
    )
    final_state = engine.execute(ticket="Add Gemfile", thread_id="t-gem")
    assert final_state["status"] == "completed"

    summary = format_dependency_summary(ledger.get_run_dependency_delta("t-gem"))
    assert "unparsed: Gemfile" in summary


def test_builder_raising_run_completes_warning_logged_null_stored(test_repo, tmp_path, caplog):
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_raise.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    new_pkg = '{\n  "dependencies": {\n    "lodash": "4.17.21",\n    "axios": "1.4.0"\n  }\n}\n'
    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="finish_investigation", args={"summary": "ready"}, id="c1")]),
        json.dumps([{"description": "Edit package.json", "scope": ["package.json"], "expects_tests": False}]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "package.json", "content": new_pkg}, id="c2")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c3")]),
    ]
    llm = ScriptedChatModel(responses)

    def _broken_delta(*args, **kwargs):
        raise RuntimeError("Delta parser crashed unexpectedly")

    with patch("jev.engine.compute_dependency_delta", side_effect=_broken_delta), \
         caplog.at_level(logging.WARNING):
        engine = JevEngine(
            workspace=ws,
            gatekeeper=gk,
            llm=llm,
            recorder=ledger,
            db_path=db_file,
        )
        final_state = engine.execute(ticket="Raise test", thread_id="t-raise")
        assert final_state["status"] == "completed"

    # Assert exactly one warning logged for the builder failure on the run end (and one for the attempt if it also crashed)
    crash_warnings = [rec for rec in caplog.records if "RuntimeError: Delta parser crashed unexpectedly" in rec.message]
    assert len(crash_warnings) >= 1

    with sqlite3.connect(str(db_file)) as conn:
        run_row = conn.execute("SELECT dependency_delta FROM runs WHERE run_id = 't-raise'").fetchone()
        assert run_row is not None
        assert run_row[0] is None  # NULL stored on builder failure


def test_jev_request_and_prompts_contain_no_dependency_delta(test_repo, tmp_path):
    ws = Workspace(repo_dir=test_repo)
    db_file = tmp_path / "ledger_p.db"
    ledger = SqliteLedger(db_path=db_file)
    gk = FakeGatekeeper()

    new_pkg = '{\n  "dependencies": {\n    "lodash": "4.17.21",\n    "axios": "1.4.0"\n  }\n}\n'
    responses = [
        AIMessage(content="", tool_calls=[ToolCall(name="finish_investigation", args={"summary": "ready"}, id="c1")]),
        json.dumps([{"description": "Edit package.json", "scope": ["package.json"], "expects_tests": False}]),
        AIMessage(content="", tool_calls=[ToolCall(name="stage_file_mutation", args={"path": "package.json", "content": new_pkg}, id="c2")]),
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c3")]),
    ]
    llm = ScriptedChatModel(responses)

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        recorder=ledger,
        db_path=db_file,
    )
    final_state = engine.execute(ticket="Check prompts", thread_id="t-prompts")
    assert final_state["status"] == "completed"

    # Check Gatekeeper payloads
    for call in gk.validate_subgoal.call_args_list:
        subgoal_arg = call[0][0]
        diff_arg = call[0][1]
        notes_arg = call[1].get("investigation_notes") if "investigation_notes" in call[1] else (call[0][3] if len(call[0]) > 3 else "")
        assert "Dependencies:" not in subgoal_arg.description
        assert "Dependencies:" not in (notes_arg or "")

    # Check LLM messages
    for msg_list in llm.invoked_messages:
        for msg in msg_list:
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                assert "Dependencies: " not in content
                assert "Dependencies: no changes" not in content


def test_old_schema_migration_leaves_old_rows_null(tmp_path):
    db_file = tmp_path / "old_ledger.db"
    with sqlite3.connect(str(db_file)) as conn:
        conn.execute("""
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                thread_id TEXT,
                ticket TEXT,
                base_commit TEXT,
                final_commit TEXT,
                engine_commit TEXT,
                worker_model TEXT,
                test_policy TEXT,
                status TEXT,
                started_at TEXT,
                ended_at TEXT
            );
        """)
        conn.execute("""
            INSERT INTO runs (run_id, thread_id, ticket, status)
            VALUES ('old-run-1', 'old-thread-1', 'ticket text', 'completed');
        """)
        conn.execute("""
            CREATE TABLE attempts (
                run_id TEXT,
                subgoal_id TEXT,
                attempt_no INTEGER,
                tier TEXT,
                outcome TEXT,
                PRIMARY KEY (run_id, subgoal_id, attempt_no)
            );
        """)
        conn.execute("""
            INSERT INTO attempts (run_id, subgoal_id, attempt_no, tier, outcome)
            VALUES ('old-run-1', 'sg-1', 1, 'tier0', 'passed');
        """)

    # Open with SqliteLedger to trigger migrations
    ledger = SqliteLedger(db_path=db_file)
    assert ledger.get_run_dependency_delta("old-run-1") is None

    with sqlite3.connect(str(db_file)) as conn:
        att = conn.execute("SELECT dependency_delta FROM attempts WHERE run_id = 'old-run-1'").fetchone()
        assert att is not None
        assert att[0] is None
