import subprocess
from pathlib import Path
from unittest.mock import MagicMock
import pytest

from jev.engine import node_gate
from jev.models import State, Subgoal, ValidationVerdict
from jev.workspace import Workspace


class FakeGatekeeper:
    def __init__(self):
        self.verdict = ValidationVerdict(valid=True, probability=0.95, reason="Approved")
        self.validate_subgoal = MagicMock(return_value=self.verdict)
        self.escalate_deadlock = MagicMock()


@pytest.fixture
def repo_workspace(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=repo, check=True, capture_output=True)

    # Initial commit
    (repo / "init.txt").write_text("initial", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    return Workspace(repo_dir=repo)


def test_suppression_ts_ignore_in_tsx_fails(repo_workspace):
    """Case 1: // @ts-ignore added in .tsx must fail Tier 0 with failed_check='suppression'."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add Button", scope=["src/Button.tsx"], expects_tests=False)

    btn_file = ws.repo_dir / "src" / "Button.tsx"
    btn_file.parent.mkdir(parents=True, exist_ok=True)
    btn_file.write_text(
        "import React from 'react';\n"
        "// @ts-ignore\n"
        "import NonExistent from 'missing-pkg';\n"
        "export const Button = () => <div />;\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/Button.tsx"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "suppression"
    assert "@ts-ignore" in res.detail
    assert "src/Button.tsx" in res.detail
    assert "Do not suppress compiler errors. Fix the cause, or escalate if it cannot be fixed." in res.detail


def test_suppression_type_ignore_in_py_fails(repo_workspace):
    """Case 2: # type: ignore added in .py must fail Tier 0 with failed_check='suppression'."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add service", scope=["src/service.py"], expects_tests=False)

    srv_file = ws.repo_dir / "src" / "service.py"
    srv_file.parent.mkdir(parents=True, exist_ok=True)
    srv_file.write_text(
        "def compute() -> int:\n"
        "    x = None  # type: ignore\n"
        "    return 42\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/service.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.passed is False
    assert res.failed_check == "suppression"
    assert "# type: ignore" in res.detail
    assert "src/service.py" in res.detail
    assert "Do not suppress compiler errors. Fix the cause, or escalate if it cannot be fixed." in res.detail


def test_suppression_word_in_markdown_passes(repo_workspace):
    """Case 3: The directive words only in a .md file must pass (non-code files must not trigger it)."""
    ws = repo_workspace
    subgoal = Subgoal(description="Update docs", scope=["README.md"], expects_tests=False)

    readme = ws.repo_dir / "README.md"
    readme.write_text(
        "# Documentation\n"
        "Do not use // @ts-ignore or # type: ignore in code.\n"
        "Also avoid @ts-nocheck or @ts-expect-error.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "README.md"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "suppression"
    assert res.passed is True


def test_suppression_preexisting_directive_passes(repo_workspace):
    """Case 4: A pre-existing directive already in the file (context line) must pass."""
    ws = repo_workspace
    # Commit a file with a pre-existing directive
    app_file = ws.repo_dir / "src" / "legacy.tsx"
    app_file.parent.mkdir(parents=True, exist_ok=True)
    app_file.write_text(
        "import React from 'react';\n"
        "// @ts-ignore\n"
        "const old_value = 1;\n"
        "export const Legacy = () => <div>{old_value}</div>;\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/legacy.tsx"], cwd=ws.repo_dir, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add legacy"], cwd=ws.repo_dir, check=True, capture_output=True)

    # Now make a change that doesn't add any suppression directive
    subgoal = Subgoal(description="Update legacy", scope=["src/legacy.tsx"], expects_tests=False)
    app_file.write_text(
        "import React from 'react';\n"
        "// @ts-ignore\n"
        "const old_value = 1;\n"
        "export const updated_value = 2;\n"
        "export const Legacy = () => <div>{old_value}</div>;\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/legacy.tsx"], cwd=ws.repo_dir, check=True, capture_output=True)

    res = ws.run_mechanical_checks(subgoal)
    assert res.failed_check != "suppression"


def test_suppression_retry_feedback_names_directive_and_instruction(repo_workspace):
    """Case 5: The retry feedback text in node_gate names the file, the line, and directive with the exact instruction."""
    ws = repo_workspace
    subgoal = Subgoal(description="Add handler", scope=["src/handler.py"], expects_tests=False)

    py_file = ws.repo_dir / "src" / "handler.py"
    py_file.parent.mkdir(parents=True, exist_ok=True)
    py_file.write_text(
        "def handler():\n"
        "    result = None  # mypy: ignore-errors\n"
        "    return result\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "src/handler.py"], cwd=ws.repo_dir, check=True, capture_output=True)

    gk = FakeGatekeeper()
    state: State = {
        "ticket": "Implement handler",
        "plan_queue": [],
        "current_subgoal": subgoal,
        "mechanical_strike_count": 0,
        "semantic_strike_count": 0,
        "trajectory": [],
        "gate_status": None,
        "last_feedback": None,
        "status": None,
        "investigation_notes": "Notes",
        "current_worktree_path": None,
        "current_worktree_branch": None,
    }

    out = node_gate(state, workspace=ws, gatekeeper=gk)
    assert out["gate_status"] == "mechanical_failure"
    feedback = out["last_feedback"] or ""

    # Must name the file
    assert "src/handler.py" in feedback
    # Must name the directive
    assert "# mypy: ignore-errors" in feedback
    # Must name the line
    assert "line 2" in feedback or "result = None  # mypy: ignore-errors" in feedback
    # Must contain exact phrasing
    assert "Do not suppress compiler errors. Fix the cause, or escalate if it cannot be fixed." in feedback
