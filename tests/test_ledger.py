import json
import os
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jev.engine import JevEngine, node_gate, node_implement, node_plan
from jev.models import (
    MechanicalCheckResult,
    State,
    Subgoal,
    ValidationVerdict,
)
from jev.workspace import Workspace


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(path), check=True, capture_output=True)
    (path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(path), check=True, capture_output=True)
    return path


# ---------------------------------------------------------------------------
# Test Case 1: Exactly one attempts row per gate outcome
# -----------------------------------------------------------
def test_exactly_one_attempts_row_per_gate_outcome(tmp_path):
    from jev.ledger import SqliteLedger

    repo = _init_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo)
    ledger_db = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=ledger_db)

    run_id = "test-run-1"
    ledger.record_run_start(
        run_id=run_id,
        thread_id="test-thread-1",
        ticket="Test gate outcomes",
        base_commit=ws.base_commit,
        engine_commit="dummy-engine-commit",
        worker_model="dummy-model",
        test_policy="auto",
    )

    # 1. Mechanical failure
    sg1 = Subgoal(id="sg-1", description="Subgoal 1", scope=["foo.py"])
    ledger.record_subgoals(run_id, [sg1])
    state1: State = {
        "run_id": run_id,
        "ticket": "Test gate outcomes",
        "current_subgoal": sg1,
        "plan_queue": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    mock_ws_mech_fail = MagicMock()
    mock_ws_mech_fail.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False, failed_check="build", detail="Syntax error in foo.py"
    )
    mock_ws_mech_fail.get_staged_diff.return_value = "diff --git a/foo.py b/foo.py\n+invalid code"
    mock_gk = MagicMock()

    node_gate(state1, workspace=mock_ws_mech_fail, gatekeeper=mock_gk, recorder=ledger)

    # 2. Semantic failure
    sg2 = Subgoal(id="sg-2", description="Subgoal 2", scope=["bar.py"])
    ledger.record_subgoals(run_id, [sg2])
    state2: State = {
        "run_id": run_id,
        "ticket": "Test gate outcomes",
        "current_subgoal": sg2,
        "plan_queue": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    mock_ws_sem = MagicMock()
    mock_ws_sem.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    mock_ws_sem.get_staged_diff.return_value = "diff --git a/bar.py b/bar.py\n+x = 2"
    mock_gk_sem_fail = MagicMock()
    mock_gk_sem_fail.validate_subgoal.return_value = ValidationVerdict(
        valid=False, probability=0.25, reason="Does not meet requirements"
    )

    node_gate(state2, workspace=mock_ws_sem, gatekeeper=mock_gk_sem_fail, recorder=ledger)

    # 3. Passed
    sg3 = Subgoal(id="sg-3", description="Subgoal 3", scope=["baz.py"])
    ledger.record_subgoals(run_id, [sg3])
    state3: State = {
        "run_id": run_id,
        "ticket": "Test gate outcomes",
        "current_subgoal": sg3,
        "plan_queue": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    mock_ws_pass = MagicMock()
    mock_ws_pass.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    mock_ws_pass.get_staged_diff.return_value = "diff --git a/baz.py b/baz.py\n+x = 3"
    mock_gk_pass = MagicMock()
    mock_gk_pass.validate_subgoal.return_value = ValidationVerdict(
        valid=True, probability=0.98, reason="Looks great"
    )

    node_gate(state3, workspace=mock_ws_pass, gatekeeper=mock_gk_pass, recorder=ledger)

    # 4. Escalation inside gate (env_not_ready)
    sg4 = Subgoal(id="sg-4", description="Subgoal 4", scope=["env.py"])
    ledger.record_subgoals(run_id, [sg4])
    state4: State = {
        "run_id": run_id,
        "ticket": "Test gate outcomes",
        "current_subgoal": sg4,
        "plan_queue": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    mock_ws_env = MagicMock()
    mock_ws_env.run_mechanical_checks.return_value = MechanicalCheckResult(
        passed=False, failed_check="env_not_ready", detail="Rust toolchain missing"
    )
    mock_ws_env.get_staged_diff.return_value = "diff --git a/env.py b/env.py\n+x = 4"

    node_gate(state4, workspace=mock_ws_env, gatekeeper=mock_gk, recorder=ledger)

    with sqlite3.connect(str(ledger_db)) as conn:
        cursor = conn.cursor()
        rows = cursor.execute("SELECT subgoal_id, outcome, failed_check, jev_valid FROM attempts WHERE run_id = ? ORDER BY subgoal_id", (run_id,)).fetchall()

    assert len(rows) == 4
    # Check outcomes
    subgoal_outcomes = {r[0]: (r[1], r[2], r[3]) for r in rows}
    assert subgoal_outcomes["sg-1"] == ("mechanical_failure", "build", None)
    assert subgoal_outcomes["sg-2"] == ("semantic_failure", None, 0)
    assert subgoal_outcomes["sg-3"] == ("passed", None, 1)
    assert subgoal_outcomes["sg-4"] == ("env_not_ready", "env_not_ready", None)


# ---------------------------------------------------------------------------
# Test Case 2: Rows survive a rollback
# -----------------------------------------------------------
def test_rows_survive_a_rollback(tmp_path):
    from jev.ledger import SqliteLedger

    repo = _init_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo)
    ledger_db = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=ledger_db)

    run_id = "test-run-rollback"
    ledger.record_run_start(
        run_id=run_id,
        thread_id="test-thread-rb",
        ticket="Test rollback persistence",
        base_commit=ws.base_commit,
        engine_commit="dummy",
        worker_model="dummy",
        test_policy="auto",
    )
    sg = Subgoal(id="sg-1", description="Failing subgoal", scope=["broken.py"])
    ledger.record_subgoals(run_id, [sg])

    # Create subgoal worktree and stage mutation
    ws.create_integration_branch("jev-ticket-test-thread-rb", base_commit=ws.base_commit)
    wt_path = ws.create_subgoal_worktree("sg1", base_ref="jev-ticket-test-thread-rb")
    ws.stage_file_mutation("broken.py", "def broken():\n  syntax error here")

    state: State = {
        "run_id": run_id,
        "ticket": "Test rollback",
        "current_subgoal": sg,
        "current_worktree_path": str(wt_path),
        "current_worktree_branch": "jev-subgoal-sg1",
        "integration_branch": "jev-ticket-test-thread-rb",
        "plan_queue": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    # Execute gate: mechanical check fails (syntax error), worktree is discarded!
    res_state = node_gate(state, workspace=ws, gatekeeper=MagicMock(), recorder=ledger)
    assert res_state["gate_status"] == "mechanical_failure"
    # Worktree was discarded
    assert not wt_path.exists()

    # Query ledger: attempt row survived and diff contains the staged changes
    with sqlite3.connect(str(ledger_db)) as conn:
        cursor = conn.cursor()
        row = cursor.execute("SELECT diff, diff_sha, outcome, failed_check FROM attempts WHERE run_id = ? AND subgoal_id = 'sg-1'", (run_id,)).fetchone()

    assert row is not None
    diff, diff_sha, outcome, failed_check = row
    assert "broken.py" in diff
    assert "syntax error here" in diff
    assert len(diff_sha) == 64
    assert outcome == "mechanical_failure"
    assert failed_check == "build"


# ---------------------------------------------------------------------------
# Test Case 3: NullRecorder leaves engine behavior identical
# -----------------------------------------------------------
def test_null_recorder_leaves_engine_behavior_identical(tmp_path):
    from jev.ledger import NullRecorder

    repo = _init_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo)
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.99)
    gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.99)

    recorder = NullRecorder()
    engine = JevEngine(workspace=ws, gatekeeper=gk, recorder=recorder)
    assert isinstance(engine.recorder, NullRecorder)

    # Initial state and gate
    sg = Subgoal(id="sg-1", description="Null recorder test", scope=["README.md"])
    state: State = {
        "ticket": "Null recorder",
        "current_subgoal": sg,
        "plan_queue": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }
    mock_ws = MagicMock()
    mock_ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    mock_ws.get_staged_diff.return_value = "diff --git a/README.md b/README.md"

    out_state = node_gate(state, workspace=mock_ws, gatekeeper=gk, recorder=recorder)
    assert out_state["gate_status"] == "passed"
    assert out_state["mechanical_strike_count"] == 0
    assert out_state["semantic_strike_count"] == 0


# ---------------------------------------------------------------------------
# Test Case 4: Recorded commit SHAs equal git rev-parse
# -----------------------------------------------------------
def test_recorded_commit_shas_equal_git_rev_parse(tmp_path):
    from jev.ledger import SqliteLedger

    repo = _init_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo)
    ledger_db = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=ledger_db)

    run_id = "test-run-shas"
    base_commit = ws.base_commit
    ledger.record_run_start(
        run_id=run_id,
        thread_id="thread-shas",
        ticket="Check commit SHAs",
        base_commit=base_commit,
        engine_commit="dummy",
        worker_model="dummy",
        test_policy="auto",
    )

    sg = Subgoal(id="sg-1", description="Commit SHA test", scope=["feature.py"], expects_tests=False)
    ledger.record_subgoals(run_id, [sg])

    # Worktree + commit
    integration_branch = "jev-ticket-thread-shas"
    ws.create_integration_branch(integration_branch, base_commit=base_commit)
    wt_path = ws.create_subgoal_worktree("sg1", base_ref=integration_branch)
    ws.stage_file_mutation("feature.py", "def feature(): return 42\n")

    state: State = {
        "run_id": run_id,
        "ticket": "Check commit SHAs",
        "current_subgoal": sg,
        "current_worktree_path": str(wt_path),
        "current_worktree_branch": "jev-subgoal-sg1",
        "integration_branch": integration_branch,
        "plan_queue": [],
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
    }

    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.99)

    out_state = node_gate(state, workspace=ws, gatekeeper=gk, recorder=ledger)
    assert out_state["gate_status"] == "passed"

    # Query git for the actual integration branch commit
    actual_parent_sha = base_commit
    res = subprocess.run(["git", "rev-parse", integration_branch], cwd=str(repo), capture_output=True, text=True, check=True)
    actual_commit_sha = res.stdout.strip()

    with sqlite3.connect(str(ledger_db)) as conn:
        cursor = conn.cursor()
        sg_row = cursor.execute("SELECT parent_sha, commit_sha, status FROM subgoals WHERE run_id = ? AND subgoal_id = 'sg-1'", (run_id,)).fetchone()

    assert sg_row is not None
    rec_parent, rec_commit, rec_status = sg_row
    assert rec_parent == actual_parent_sha
    assert rec_commit == actual_commit_sha
    assert rec_status == "committed"


# ---------------------------------------------------------------------------
# Test Case 5: Faulty ledger logs warning and run succeeds anyway
# -----------------------------------------------------------
def test_faulty_ledger_logs_warning_and_run_succeeds(tmp_path, capsys):
    from jev.ledger import Recorder

    class FaultyRecorder(Recorder):
        def record_run_start(self, *args, **kwargs):
            raise RuntimeError("Database disk image is malformed")

        def record_run_end(self, *args, **kwargs):
            raise RuntimeError("Disk full")

        def record_subgoals(self, *args, **kwargs):
            raise RuntimeError("Write failed")

        def record_attempt(self, *args, **kwargs):
            raise RuntimeError("Constraint failed")

        def record_subgoal_committed(self, *args, **kwargs):
            raise RuntimeError("Commit failed")

    repo = _init_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo)
    gk = MagicMock()
    gk.validate_subgoal.return_value = ValidationVerdict(valid=True, probability=0.9)
    gk.verify_ticket.return_value = ValidationVerdict(valid=True, probability=0.9)

    recorder = FaultyRecorder()
    engine = JevEngine(workspace=ws, gatekeeper=gk, recorder=recorder)

    # Execute a run
    res = engine.execute("Simple task", thread_id="faulty-thread")

    # Run must succeed without raising exception
    assert res is not None
    # Captured warning message must be printed to stderr only (not stdout), exactly once
    captured = capsys.readouterr()
    assert "ledger disabled for this run:" in captured.err
    assert "ledger disabled for this run:" not in captured.out
    assert captured.err.count("ledger disabled for this run:") == 1


# ---------------------------------------------------------------------------
# Test Case 6: repo_key matches for two clones
# -----------------------------------------------------------
def test_repo_key_matches_for_two_clones(tmp_path):
    from jev.ledger import compute_repo_key

    repo1 = _init_git_repo(tmp_path / "repo1")
    repo2 = tmp_path / "repo2"
    subprocess.run(["git", "clone", str(repo1), str(repo2)], check=True, capture_output=True)

    key1 = compute_repo_key(repo1)
    key2 = compute_repo_key(repo2)

    assert key1 != "unknown"
    assert len(key1) == 16
    assert key1 == key2


# ---------------------------------------------------------------------------
# Test Case 7: --no-ledger CLI flag works
# -----------------------------------------------------------
def test_cli_no_ledger_flag_works(tmp_path, monkeypatch):
    from main import build_parser, main
    from jev.ledger import NullRecorder
    from unittest.mock import patch, MagicMock

    parser = build_parser()
    args = parser.parse_args(["some ticket", "--no-ledger"])
    assert args.no_ledger is True

    # Run main with --no-ledger
    repo = _init_git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    # Ensure no API keys in environment
    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_URL", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    mock_eng = MagicMock()
    mock_eng.execute.return_value = {"status": "completed"}
    with patch("main.Gatekeeper") as mock_gk_cls, patch("main.JevEngine") as mock_eng_cls:
        mock_eng_cls.return_value = mock_eng
        exit_code = main(["some ticket", "--no-ledger", "--repo-dir", str(repo)])
        assert exit_code == 0
        call_kwargs = mock_eng_cls.call_args[1]
        assert isinstance(call_kwargs.get("recorder"), NullRecorder)


# ---------------------------------------------------------------------------
# Test Case 7b: SqliteLedger construction failure falls back to NullRecorder
# ---------------------------------------------------------------------------
def test_cli_sqlite_ledger_construction_failure_falls_back_to_null_recorder(tmp_path, monkeypatch, capsys):
    from main import main
    from jev.ledger import NullRecorder
    from unittest.mock import patch, MagicMock

    repo = _init_git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    # Point JEV_HOME to a regular file instead of a directory
    dummy_file = tmp_path / "jev_home_file"
    dummy_file.write_text("not a directory")
    monkeypatch.setenv("JEV_HOME", str(dummy_file))

    monkeypatch.delenv("JEV_API_KEY", raising=False)
    monkeypatch.delenv("JEV_API_URL", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    mock_eng = MagicMock()
    mock_eng.execute.return_value = {"status": "completed"}
    with patch("main.Gatekeeper") as mock_gk_cls, patch("main.JevEngine") as mock_eng_cls:
        mock_eng_cls.return_value = mock_eng
        exit_code = main(["some ticket", "--repo-dir", str(repo)])
        assert exit_code == 0
        call_kwargs = mock_eng_cls.call_args[1]
        assert isinstance(call_kwargs.get("recorder"), NullRecorder)

    captured = capsys.readouterr()
    assert "ledger disabled:" in captured.err


# ---------------------------------------------------------------------------
# Test Case 8: Trajectory bloat - implement node truncates args
# -----------------------------------------------------------
def test_trajectory_bloat_implement_node_truncates_args(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    ws = Workspace(repo_dir=repo)
    sg = Subgoal(id="sg-1", description="Implement test", scope=["code.py"])

    fake_tool_call = {
        "name": "stage_file_mutation",
        "args": {"path": "code.py", "content": "x = 42\nprint(x)\n"},
        "id": "c1",
    }
    mock_ai_msg = MagicMock()
    mock_ai_msg.tool_calls = [fake_tool_call]
    mock_ai_msg.content = ""

    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    mock_llm.invoke.side_effect = [mock_ai_msg, MagicMock(tool_calls=[{"name": "submit_subgoal", "args": {}, "id": "c2"}], content="")]

    state: State = {
        "ticket": "Implement test",
        "current_subgoal": sg,
        "plan_queue": [sg],
        "trajectory": [],
    }

    res_state = node_implement(state, workspace=ws, llm=mock_llm)
    last_step = res_state["trajectory"][-1]
    tool_calls = last_step.get("tool_calls", [])
    assert len(tool_calls) >= 1

    stage_call = [tc for tc in tool_calls if tc["name"] == "stage_file_mutation"][0]
    args = stage_call["args"]
    # Must NOT contain raw content
    assert "content" not in args
    assert "code" not in args
    assert "text" not in args
    # Must contain path, content_sha, content_chars
    assert args["path"] == "code.py"
    assert "content_sha" in args
    assert len(args["content_sha"]) == 64
    assert args["content_chars"] == len("x = 42\nprint(x)\n")


# ---------------------------------------------------------------------------
# Test Case 9: Subgoal identity and status transitions
# -----------------------------------------------------------
def test_subgoal_identity_and_status_transitions(tmp_path):
    # Subgoal default values
    sg = Subgoal(description="Default subgoal")
    assert sg.id == ""
    assert sg.status == "pending"

    # node_plan assigns sg-1, sg-2, ...
    state: State = {
        "ticket": "Default task",
        "plan_queue": [
            Subgoal(description="Step 1"),
            Subgoal(description="Step 2"),
        ],
        "trajectory": [],
    }
    plan_res = node_plan(state, llm=None)
    subgoals = plan_res["plan_queue"]
    assert subgoals[0].id == "sg-1"
    assert subgoals[0].status == "pending"
    assert subgoals[1].id == "sg-2"
    assert subgoals[1].status == "pending"


# ---------------------------------------------------------------------------
# Test Case 10: scripts/ledger_report.py viewer works
# -----------------------------------------------------------
def test_ledger_report_cli(tmp_path, capsys):
    from jev.ledger import SqliteLedger
    import scripts.ledger_report as lr

    repo = _init_git_repo(tmp_path / "repo")
    ledger_db = tmp_path / "ledger.db"
    ledger = SqliteLedger(db_path=ledger_db)

    run_id = "run-report-test"
    ledger.record_run_start(
        run_id=run_id,
        thread_id="thread-report",
        ticket="Report test ticket",
        base_commit="abc1234",
        engine_commit="def5678",
        worker_model="gemini-test",
        test_policy="auto",
    )
    sg = Subgoal(id="sg-1", description="Implement something", scope=["a.py"], status="committed")
    ledger.record_subgoals(run_id, [sg])
    ledger.record_subgoal_committed(run_id, "sg-1", parent_sha="abc1234", commit_sha="fed9876")
    ledger.record_attempt(
        run_id=run_id,
        subgoal_id="sg-1",
        tier="semantic",
        outcome="passed",
        failed_check=None,
        diff="diff --git a/a.py b/a.py\n+a = 1",
        diff_sha="sha123",
        files=["a.py"],
        compile_errors=[],
        tests_ran=True,
        test_outcome="PASSED",
        jev_valid=True,
        jev_probability=0.95,
        jev_model="jev-1.13.0",
        feedback_sent="LGTM",
    )
    ledger.record_run_end(
        run_id=run_id,
        final_status="completed",
        final_commit="fed9876",
        notes_chars=120,
    )

    exit_code = lr.main(["--db-path", str(ledger_db), "--run-id", run_id])
    assert exit_code == 0
    captured = capsys.readouterr().out
    assert "Report test ticket" in captured
    assert "sg-1" in captured
    assert "committed" in captured
    assert "passed" in captured
    assert "0.95" in captured
    assert "jev-1.13.0" in captured


def test_ledger_report_subprocess_without_pythonpath(tmp_path):
    repo = _init_git_repo(tmp_path / "repo")
    ledger_db = tmp_path / "ledger.db"
    from jev.ledger import SqliteLedger

    ledger = SqliteLedger(db_path=ledger_db)
    run_id = "run-subp-test"
    ledger.record_run_start(
        run_id=run_id,
        thread_id="thread-subp",
        ticket="Subprocess report test ticket",
        base_commit="abc1234",
        engine_commit="def5678",
        worker_model="dummy",
        test_policy="auto",
    )
    ledger.record_run_end(
        run_id=run_id,
        final_status="completed",
        final_commit="abc1234",
        notes_chars=50,
    )

    repo_root = Path(__file__).resolve().parent.parent
    script_path = repo_root / "scripts" / "ledger_report.py"

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)

    import sys
    res = subprocess.run(
        [sys.executable, str(script_path), "--db-path", str(ledger_db), "--run-id", run_id],
        cwd=str(repo_root),
        env=env,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert "Subprocess report test ticket" in res.stdout
