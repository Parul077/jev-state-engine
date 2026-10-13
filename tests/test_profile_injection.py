import json
import logging
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, List, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest
from langchain_core.messages import AIMessage

from jev.engine import JevEngine
from jev.gatekeeper import Gatekeeper
from jev.ledger import NullRecorder, SqliteLedger
from jev.models import State, Subgoal, ValidationVerdict
from jev.workspace import Workspace
from main import build_parser


class RecordingScriptedChatModel:
    """Scripted LLM that records all invoked messages for assertions."""

    def __init__(self, responses: Optional[List[Any]] = None):
        self.responses = list(responses or [])
        self.recorded_invocations: List[List[Any]] = []

    def bind_tools(self, tools: List[Any], **kwargs: Any) -> "RecordingScriptedChatModel":
        bound = RecordingScriptedChatModel()
        bound.responses = self.responses
        bound.recorded_invocations = self.recorded_invocations
        return bound

    def invoke(self, messages: List[Any], **kwargs: Any) -> Any:
        self.recorded_invocations.append(messages)
        if not self.responses:
            return AIMessage(content="[]")
        resp = self.responses.pop(0)
        if isinstance(resp, str):
            return AIMessage(content=resp)
        return resp


class FakeGatekeeper:
    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.99, reason="Approved")
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.99, reason="Verified")
        self.validate_subgoal = MagicMock(side_effect=lambda sg, diff, mechanical_detail="", investigation_notes="": self.verdict)
        self.verify_ticket = MagicMock(side_effect=lambda ticket, diff, test_out, investigation_notes="": self.verify_verdict)
        self.escalate_deadlock = MagicMock()


def _create_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "master"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=path, check=True, capture_output=True)

    src = path / "src"
    src.mkdir(exist_ok=True)
    (src / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (path / "pyproject.toml").write_text('[project]\nname = "demo"\n')

    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture
def sample_repo(tmp_path):
    return _create_repo(tmp_path / "repo")


def _make_standard_responses():
    return [
        # 1. node_investigate
        AIMessage(
            content="",
            tool_calls=[{
                "id": "c1",
                "name": "finish_investigation",
                "args": {"summary": "Explored repo. Found src/app.py"},
            }],
        ),
        # 2. node_plan
        AIMessage(
            content=json.dumps([{
                "description": "Update app.py",
                "scope": ["src/app.py"],
                "expects_tests": False,
            }])
        ),
        # 3. node_implement turn 1
        AIMessage(
            content="",
            tool_calls=[{
                "id": "c2",
                "name": "stage_file_mutation",
                "args": {"path": "src/app.py", "content": "def add(a, b):\n    return a + b + 1\n"},
            }],
        ),
        # 4. node_implement turn 2
        AIMessage(
            content="",
            tool_calls=[{"id": "c3", "name": "submit_subgoal", "args": {}}],
        ),
    ]


def test_cli_parsing_repo_profile():
    """CLI parses --repo-profile on and off, and defaults to off."""
    parser = build_parser()
    args_default = parser.parse_args(["ticket-desc"])
    assert args_default.repo_profile == "off"

    args_on = parser.parse_args(["ticket-desc", "--repo-profile", "on"])
    assert args_on.repo_profile == "on"

    args_off = parser.parse_args(["ticket-desc", "--repo-profile", "off"])
    assert args_off.repo_profile == "off"


def test_flag_off_profile_builder_never_called_and_no_prompt_injection(sample_repo, tmp_path):
    """flag off: no message contains 'Repository Profile', and profile builder is never called (spy)."""
    ws = Workspace(repo_dir=sample_repo)
    gk = FakeGatekeeper()
    llm = RecordingScriptedChatModel(_make_standard_responses())

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        db_path=tmp_path / "checkpoints.db",
        repo_profile="off",
    )

    with patch("jev.repo_profile.get_or_build_repo_profile") as spy_builder:
        final_state = engine.execute(ticket="Update add function", thread_id="t-off")
        assert not spy_builder.called
        assert final_state.get("status") == "completed"

    all_messages_content = []
    for inv in llm.recorded_invocations:
        for msg in inv:
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                all_messages_content.append(content)

    for content in all_messages_content:
        assert "Repository Profile" not in content


def test_flag_on_prompts_contain_block_after_investigation_notes(sample_repo, tmp_path):
    """flag on: plan prompt and implement prompt each contain the block exactly once, after Investigation Notes."""
    ws = Workspace(repo_dir=sample_repo)
    gk = FakeGatekeeper()
    llm = RecordingScriptedChatModel(_make_standard_responses())

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        db_path=tmp_path / "checkpoints.db",
        repo_profile="on",
    )

    final_state = engine.execute(ticket="Update add function", thread_id="t-on")
    assert final_state.get("status") == "completed"

    # Find the plan prompt
    plan_prompts = [
        inv[0].content
        for inv in llm.recorded_invocations
        if inv and hasattr(inv[0], "content") and "Ticket Description:" in inv[0].content
    ]
    assert len(plan_prompts) == 1
    plan_text = plan_prompts[0]

    assert plan_text.count("Repository Profile (hard facts detected from manifests, not guesses):") == 1
    idx_notes_plan = plan_text.find("Investigation Notes:")
    idx_profile_plan = plan_text.find("Repository Profile (hard facts detected from manifests, not guesses):")
    assert idx_notes_plan != -1 and idx_profile_plan != -1
    assert idx_profile_plan > idx_notes_plan

    # Find the implement prompt (first turn of node_implement)
    impl_prompts = [
        inv[0].content
        for inv in llm.recorded_invocations
        if inv and hasattr(inv[0], "content") and "Current Subgoal:" in inv[0].content
    ]
    assert len(impl_prompts) >= 1
    impl_text = impl_prompts[0]

    assert impl_text.count("Repository Profile (hard facts detected from manifests, not guesses):") == 1
    idx_notes_impl = impl_text.find("Investigation Notes")
    idx_profile_impl = impl_text.find("Repository Profile (hard facts detected from manifests, not guesses):")
    assert idx_notes_impl != -1 and idx_profile_impl != -1
    assert idx_profile_impl > idx_notes_impl


def test_flag_on_builder_raises_failsafe(sample_repo, tmp_path, caplog):
    """flag on, builder raises: run completes, exactly one warning logged, no prompt has block, profile_chars is NULL."""
    ws = Workspace(repo_dir=sample_repo)
    gk = FakeGatekeeper()
    llm = RecordingScriptedChatModel(_make_standard_responses())
    db_file = tmp_path / "ledger_fail.db"
    ledger = SqliteLedger(db_path=db_file)

    engine = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm,
        recorder=ledger,
        db_path=tmp_path / "checkpoints.db",
        repo_profile="on",
    )

    with patch("jev.repo_profile.get_or_build_repo_profile", side_effect=RuntimeError("Profile build failed")):
        with caplog.at_level(logging.WARNING):
            final_state = engine.execute(ticket="Update add function", thread_id="t-fail")

    assert final_state.get("status") == "completed"

    # Exactly one warning logged with exception type and message only
    profile_warnings = [rec for rec in caplog.records if "Profile build failed" in rec.message or "RuntimeError" in rec.message]
    assert len(profile_warnings) == 1
    assert "RuntimeError: Profile build failed" in profile_warnings[0].message

    # Prompts must not contain the block
    for inv in llm.recorded_invocations:
        for msg in inv:
            content = getattr(msg, "content", "")
            if isinstance(content, str):
                assert "Repository Profile" not in content

    # DB profile_chars is NULL, repo_profile is "on"
    conn = sqlite3.connect(str(db_file))
    row = conn.execute("SELECT repo_profile, profile_chars FROM runs WHERE thread_id = 't-fail'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "on"
    assert row[1] is None


def test_jev_request_body_identical_flag_on_vs_off(tmp_path):
    """Asserts the Jev request body is identical with flag on and off."""
    captured_payloads = []

    def fake_post_with_retry(self, payload):
        captured_payloads.append(payload)
        return {
            "answers": {
                "valid": {
                    "value": True,
                    "probability": 0.99,
                    "reason": "OK",
                }
            },
            "model": "typesafe-test",
        }

    with patch.object(Gatekeeper, "_post_with_retry", new=fake_post_with_retry):
        gk = Gatekeeper(api_url="http://dummy.url", api_key="dummy-key")

        # Run 1: flag off
        repo_off = _create_repo(tmp_path / "repo_off")
        ws_off = Workspace(repo_dir=repo_off)
        llm_off = RecordingScriptedChatModel(_make_standard_responses())
        engine_off = JevEngine(
            workspace=ws_off,
            gatekeeper=gk,
            llm=llm_off,
            db_path=tmp_path / "checkpoints_off.db",
            repo_profile="off",
        )
        engine_off.execute(ticket="Update add function", thread_id="t-payload-off")
        payloads_off = list(captured_payloads)
        assert len(payloads_off) >= 1
        captured_payloads.clear()

        # Run 2: flag on
        repo_on = _create_repo(tmp_path / "repo_on")
        ws_on = Workspace(repo_dir=repo_on)
        llm_on = RecordingScriptedChatModel(_make_standard_responses())
        engine_on = JevEngine(
            workspace=ws_on,
            gatekeeper=gk,
            llm=llm_on,
            db_path=tmp_path / "checkpoints_on.db",
            repo_profile="on",
        )
        engine_on.execute(ticket="Update add function", thread_id="t-payload-on")
        payloads_on = list(captured_payloads)
        assert len(payloads_on) == len(payloads_off)

    assert payloads_off == payloads_on


def test_ledger_migration_and_recording(sample_repo, tmp_path):
    """Old schema DB migration keeps NULL for old rows; on-run records 'on' & len, off-run records 'off' & NULL."""
    db_file = tmp_path / "test_migration.db"
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
          retry_intel TEXT DEFAULT NULL,
          evidence_level TEXT DEFAULT NULL,
          status TEXT,
          started_at TEXT,
          ended_at TEXT,
          notes_chars INTEGER
        );
        """
    )
    conn.execute("INSERT INTO runs (run_id, thread_id, ticket, status) VALUES ('old-1', 'old-1', 'task', 'completed')")
    conn.commit()
    conn.close()

    # Open with SqliteLedger -> runs additive migration
    ledger = SqliteLedger(db_path=db_file)

    conn = sqlite3.connect(str(db_file))
    cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
    assert "repo_profile" in cols
    assert "profile_chars" in cols

    # Old row stays NULL
    old_row = conn.execute("SELECT repo_profile, profile_chars FROM runs WHERE run_id = 'old-1'").fetchone()
    assert old_row[0] is None
    assert old_row[1] is None
    conn.close()

    # Record run with on
    ws = Workspace(repo_dir=sample_repo)
    gk = FakeGatekeeper()
    llm_on = RecordingScriptedChatModel(_make_standard_responses())
    engine_on = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm_on,
        recorder=ledger,
        db_path=tmp_path / "cp_on.db",
        repo_profile="on",
    )
    engine_on.execute(ticket="Update add function", thread_id="run-on-1")

    conn = sqlite3.connect(str(db_file))
    on_row = conn.execute("SELECT repo_profile, profile_chars FROM runs WHERE thread_id = 'run-on-1'").fetchone()
    assert on_row[0] == "on"
    assert on_row[1] is not None
    assert on_row[1] > 0
    conn.close()

    # Record run with off
    llm_off = RecordingScriptedChatModel(_make_standard_responses())
    engine_off = JevEngine(
        workspace=ws,
        gatekeeper=gk,
        llm=llm_off,
        recorder=ledger,
        db_path=tmp_path / "cp_off.db",
        repo_profile="off",
    )
    engine_off.execute(ticket="Update add function", thread_id="run-off-1")

    conn = sqlite3.connect(str(db_file))
    off_row = conn.execute("SELECT repo_profile, profile_chars FROM runs WHERE thread_id = 'run-off-1'").fetchone()
    assert off_row[0] == "off"
    assert off_row[1] is None
    conn.close()
