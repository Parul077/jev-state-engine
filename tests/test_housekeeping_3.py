import argparse
import ast
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, ToolCall

import main
from jev.engine import JevEngine, node_implement, node_verify
from jev.models import State, Subgoal, TestOutcome, ValidationVerdict
from jev.workspace import Workspace


class FakeGatekeeper:
    def __init__(self, verdict=None, verify_verdict=None):
        self.verdict = verdict or ValidationVerdict(valid=True, probability=0.95, reason="Approved")
        self.verify_verdict = verify_verdict or ValidationVerdict(valid=True, probability=0.98, reason="Verified")
        self.validate_subgoal = MagicMock(side_effect=lambda sg, diff, mechanical_detail="", investigation_notes="": self.verdict)
        self.verify_ticket = MagicMock(side_effect=lambda ticket, diff, test_out, investigation_notes="": self.verify_verdict)
        self.escalate_deadlock = MagicMock()
        self.escalate_deadlock_calls = []

        def _escalate(*args, **kwargs):
            self.escalate_deadlock_calls.append((args, kwargs))

        self.escalate_deadlock.side_effect = _escalate



class ScriptedChatModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = tools
        return self

    def invoke(self, messages, **kwargs):
        self.calls.append(messages)
        if not self.responses:
            raise RuntimeError("No more scripted responses.")
        resp = self.responses.pop(0)
        if isinstance(resp, str):
            return AIMessage(content=resp)
        return resp


@pytest.fixture
def clean_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


# ==============================================================================
# PART 1: Test Timeout
# ==============================================================================

def test_run_tests_real_hanging_command_times_out(clean_repo):
    """A real hanging command with timeout=1 returns FAILED quickly (<15s) with 'timed out' in stderr."""
    ws = Workspace(repo_dir=clean_repo, test_timeout=1)
    from jev.workspace import ResolvedTestCommand
    hang_cmd = [sys.executable, "-c", "import time; time.sleep(60)"]
    with patch("jev.workspace.resolve_test_command", return_value=ResolvedTestCommand(ecosystem="pytest", command=hang_cmd, source="pytest")):
        start = time.time()
        outcome = ws.run_tests()
        duration = time.time() - start

    assert duration < 15, f"Hanging command took too long to timeout: {duration}s"
    assert outcome == TestOutcome.FAILED
    assert ws.last_test_run is not None
    stderr_tail = (ws.last_test_run.get("stderr_tail") or "").lower()
    assert "timed out" in stderr_tail


def test_run_tests_process_tree_killed_on_timeout(clean_repo, tmp_path):
    """Parent process spawns a sleeping grandchild with PID in file; after timeout grandchild is gone."""
    pid_file = tmp_path / "grandchild.pid"
    # Script that spawns grandchild that sleeps for 60 seconds and writes PID to file
    spawner_script = tmp_path / "spawner.py"
    spawner_script.write_text(
        f"""
import subprocess, sys, time
proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
with open(r"{pid_file}", "w") as f:
    f.write(str(proc.pid))
proc.wait()
""",
        encoding="utf-8",
    )

    ws = Workspace(repo_dir=clean_repo, test_timeout=1)
    from jev.workspace import ResolvedTestCommand
    with patch(
        "jev.workspace.resolve_test_command",
        return_value=ResolvedTestCommand(ecosystem="pytest", command=[sys.executable, str(spawner_script)], source="pytest"),
    ):
        start = time.time()
        outcome = ws.run_tests()
        duration = time.time() - start

    assert duration < 15
    assert outcome == TestOutcome.FAILED

    # Verify grandchild PID is dead
    assert pid_file.exists(), "Grandchild PID file was not created"
    grandchild_pid = int(pid_file.read_text().strip())

    if sys.platform == "win32":
        res = subprocess.run(["tasklist", "/FI", f"PID eq {grandchild_pid}"], capture_output=True, text=True)
        assert str(grandchild_pid) not in res.stdout, f"Grandchild process {grandchild_pid} is still alive!"
    else:
        try:
            os.kill(grandchild_pid, 0)
            alive = True
        except OSError:
            alive = False
        assert not alive, f"Grandchild process {grandchild_pid} is still alive!"


def test_run_tests_fast_passing_command_succeeds(clean_repo):
    """A fast passing command still returns PASSED unchanged."""
    ws = Workspace(repo_dir=clean_repo, test_timeout=60)
    from jev.workspace import ResolvedTestCommand
    pass_cmd = [sys.executable, "-c", "import sys; sys.exit(0)"]
    with patch("jev.workspace.resolve_test_command", return_value=ResolvedTestCommand(ecosystem="pytest", command=pass_cmd, source="pytest")):
        outcome = ws.run_tests()
    assert outcome == TestOutcome.PASSED


def test_test_timeout_cli_env_default_precedence(clean_repo):
    """Precedence: CLI, then env JEV_TEST_TIMEOUT, then default 600."""
    parser = main.build_parser()

    # Default 600
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("JEV_TEST_TIMEOUT", None)
        args = parser.parse_args(["ticket"])
        # If args.test_timeout is evaluated via main precedence helper
        assert getattr(args, "test_timeout", None) is None
        # Workspace default
        ws = Workspace(repo_dir=clean_repo)
        assert ws.test_timeout == 600

    # Env precedence when CLI not set
    with patch.dict(os.environ, {"JEV_TEST_TIMEOUT": "120"}):
        ws_env = Workspace(repo_dir=clean_repo)
        assert ws_env.test_timeout == 120

    # CLI takes precedence over Env
    with patch.dict(os.environ, {"JEV_TEST_TIMEOUT": "120"}):
        args = parser.parse_args(["ticket", "--test-timeout", "45"])
        assert args.test_timeout == 45
        ws_cli = Workspace(repo_dir=clean_repo, test_timeout=args.test_timeout)
        assert ws_cli.test_timeout == 45


def test_test_timeout_invalid_values_rejected(clean_repo):
    """Invalid timeout values (<=0 or non-int) are rejected with argparse error or ValueError."""
    parser = main.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["ticket", "--test-timeout", "0"])

    with pytest.raises(SystemExit):
        parser.parse_args(["ticket", "--test-timeout", "-10"])

    with pytest.raises(SystemExit):
        parser.parse_args(["ticket", "--test-timeout", "invalid"])

    with pytest.raises(ValueError):
        Workspace(repo_dir=clean_repo, test_timeout=0)

    with pytest.raises(ValueError):
        Workspace(repo_dir=clean_repo, test_timeout=-5)


def test_production_code_has_no_test_double_detection():
    """src/jev/workspace.py must not import unittest.mock or check isinstance on subprocess.run."""
    ws_file = Path(__file__).resolve().parent.parent / "src" / "jev" / "workspace.py"
    content = ws_file.read_text(encoding="utf-8")
    assert "unittest.mock" not in content
    assert "isinstance(subprocess.run" not in content


def test_run_test_subprocess_never_delegates_to_mocked_subprocess_run():
    """_run_test_subprocess must not detect or delegate to mocked subprocess.run; it always uses Popen."""
    ws = Workspace(repo_dir=Path.cwd())
    with patch("subprocess.run") as mock_run, patch("subprocess.Popen") as mock_popen:
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = ("out", "err")
        mock_proc.returncode = 0
        mock_popen.return_value = mock_proc

        ws._run_test_subprocess(["echo", "hi"], cwd=Path.cwd(), timeout=5)

        assert mock_run.call_count == 0
        assert mock_popen.call_count == 1


@pytest.mark.parametrize("bad_val", ["abc", "-5", "0"])
def test_invalid_jev_test_timeout_logs_warning_and_falls_back_to_600(clean_repo, caplog, bad_val):
    """Invalid JEV_TEST_TIMEOUT logs ONE warning naming variable and bad value, falling back to 600."""
    import logging
    with patch.dict(os.environ, {"JEV_TEST_TIMEOUT": bad_val}):
        with caplog.at_level(logging.WARNING):
            ws = Workspace(repo_dir=clean_repo)
            assert ws.test_timeout == 600
        # Assert exactly ONE warning naming JEV_TEST_TIMEOUT and the bad value
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING and "JEV_TEST_TIMEOUT" in r.message]
        assert len(warnings) == 1
        assert bad_val in warnings[0].message


def test_node_verify_uses_timeout_and_handles_timeout_failure(clean_repo):
    """node_verify uses workspace.run_tests and records verification_failed on test timeout."""
    # Ensure integration branch exists in clean_repo
    subprocess.run(["git", "branch", "integration-branch"], cwd=clean_repo, check=True)

    ws = Workspace(repo_dir=clean_repo, test_timeout=1)
    ws.check_compile = MagicMock(return_value={"outcome": "PASSED"})
    gk = FakeGatekeeper(verify_verdict=ValidationVerdict(valid=False, reason="Tests timed out"))
    from jev.workspace import ResolvedTestCommand
    hang_cmd = [sys.executable, "-c", "import time; time.sleep(60)"]

    state: State = {
        "ticket": "Timeout in verify",
        "integration_branch": "integration-branch",
        "base_commit": ws.base_commit,
        "test_policy": "auto",
        "trajectory": [],
        "status": "in_progress",
    }

    with patch("jev.workspace.resolve_test_command", return_value=ResolvedTestCommand(ecosystem="pytest", command=hang_cmd, source="pytest")):
        final_state = node_verify(state, workspace=ws, gatekeeper=gk)

    assert final_state["gate_status"] == "verification_failed"
    assert ws.last_test_run is not None
    assert "timed out" in (ws.last_test_run.get("stderr_tail") or "").lower()
    gk.verify_ticket.assert_called_once()
    assert "timed out" in gk.verify_ticket.call_args[0][2].lower()


# ==============================================================================
# PART 2: Read Caps & Implement Scope File Size Limit
# ==============================================================================

def test_read_file_over_cap_is_truncated_and_under_cap_unchanged(clean_repo):
    """read_file caps returned text at 40,000 chars with exact notice; under cap is unchanged."""
    ws = Workspace(repo_dir=clean_repo)

    # 1. Under cap: 100 characters
    under_text = "x" * 100
    (clean_repo / "small.txt").write_text(under_text, encoding="utf-8")
    res_under = ws.run_read_tool("cat", ["small.txt"])
    assert res_under == under_text

    # 2. Over cap: 45,000 characters
    over_text = "A" * 45000
    (clean_repo / "large.txt").write_text(over_text, encoding="utf-8")
    res_over = ws.run_read_tool("cat", ["large.txt"])
    expected_notice = "\n[truncated: showing first 40000 of 45000 characters]"
    assert res_over.endswith(expected_notice)
    assert res_over == ("A" * 40000) + expected_notice


def test_node_implement_in_scope_file_over_80k_escalates_without_llm_invocation(clean_repo):
    """If an existing file in subgoal.scope is > 80,000 chars, escalate without calling LLM."""
    ws = Workspace(repo_dir=clean_repo)
    gk = FakeGatekeeper()

    huge_content = "Z" * 80005
    ws.stage_file_mutation("huge_file.py", huge_content)
    ws.commit_subgoal("Add huge file")

    subgoal = Subgoal(id="sg-huge", description="Modify huge file", scope=["huge_file.py"], expects_tests=False)
    state: State = {
        "ticket": "Handle huge file",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "status": "in_progress",
        "trajectory": [],
    }

    mock_llm = MagicMock()

    res_state = node_implement(state, workspace=ws, llm=mock_llm, gatekeeper=gk)

    mock_llm.invoke.assert_not_called()
    assert res_state["status"] == "escalated"
    assert "huge_file.py" in res_state.get("last_feedback", "")
    assert "80005" in res_state.get("last_feedback", "")
    assert "too large for a safe full-file rewrite" in res_state.get("last_feedback", "").lower()
    assert gk.escalate_deadlock_calls, "Expected gatekeeper.escalate_deadlock to be called"


def test_node_implement_file_just_under_80k_proceeds_normally(clean_repo):
    """A file just under 80,000 chars (e.g. 80,000) proceeds normally and invokes LLM."""
    ws = Workspace(repo_dir=clean_repo)
    gk = FakeGatekeeper()

    border_content = "Y" * 80000
    ws.stage_file_mutation("border_file.py", border_content)
    ws.commit_subgoal("Add border file")

    subgoal = Subgoal(id="sg-border", description="Modify border file", scope=["border_file.py"], expects_tests=False)
    state: State = {
        "ticket": "Handle border file",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "status": "in_progress",
        "trajectory": [],
    }

    llm = ScriptedChatModel([
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c1")]),
    ])

    res_state = node_implement(state, workspace=ws, llm=llm, gatekeeper=gk)
    assert len(llm.calls) > 0, "Expected LLM to be invoked for border file"
    assert res_state.get("status") != "escalated"


def test_node_implement_new_nonexistent_files_unaffected(clean_repo):
    """Files in scope that do not exist yet proceed normally without escalation."""
    ws = Workspace(repo_dir=clean_repo)
    gk = FakeGatekeeper()

    subgoal = Subgoal(id="sg-new", description="Create new file", scope=["new_file.py"], expects_tests=False)
    state: State = {
        "ticket": "Create new file",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "status": "in_progress",
        "trajectory": [],
    }

    llm = ScriptedChatModel([
        AIMessage(content="", tool_calls=[ToolCall(name="submit_subgoal", args={}, id="c1")]),
    ])

    res_state = node_implement(state, workspace=ws, llm=llm, gatekeeper=gk)
    assert len(llm.calls) > 0, "Expected LLM to be invoked for new file"
    assert res_state.get("status") != "escalated"


# ==============================================================================
# PART 3: Requirements.txt Top-Level Import Coverage
# ==============================================================================

def test_requirements_covers_all_third_party_imports_in_src():
    """All third-party imports in src/ must map to distributions listed in requirements.txt."""
    src_dir = Path(__file__).resolve().parent.parent / "src"
    req_file = Path(__file__).resolve().parent.parent / "requirements.txt"

    assert req_file.exists(), "requirements.txt not found"

    # Parse requirements distributions
    req_dists = set()
    for line in req_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip environment markers (after ';') and version specifiers
        pkg_part = line.split(";")[0].strip()
        for sep in ["==", ">=", "<=", "~=", "<", ">", "!=", "["]:
            pkg_part = pkg_part.split(sep)[0].strip()
        if pkg_part:
            req_dists.add(pkg_part.lower())

    # Collect top-level imports in src/ using AST
    src_imports = set()
    for py_path in src_dir.rglob("*.py"):
        tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    src_imports.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    src_imports.add(node.module.split(".")[0])

    stdlib = set(getattr(sys, "stdlib_module_names", [])) | set(sys.builtin_module_names) | {"__future__", "_winapi"}
    own_modules = {"jev", "main"}
    third_party = src_imports - stdlib - own_modules

    explicit_mapping = {
        "dotenv": "python-dotenv",
        "langchain_core": "langchain-core",
        "langchain_google_genai": "langchain-google-genai",
        "tomli": "tomli",
        "typing_extensions": "typing-extensions",
        "tomllib": "tomli",
    }

    missing = []
    for mod in sorted(third_party):
        dist_name = explicit_mapping.get(mod, mod).lower()
        if dist_name not in req_dists:
            missing.append(f"{mod} -> {dist_name}")

    assert not missing, f"Third-party imports in src/ missing from requirements.txt: {missing}"
