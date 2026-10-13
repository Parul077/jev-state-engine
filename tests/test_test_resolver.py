import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jev.models import TestOutcome
from jev.workspace import ResolvedTestCommand, Workspace, resolve_test_command


@pytest.fixture
def repo_dir(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "TestUser"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
    return repo


def _mock_success_process(cmd=None):
    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = "OK"
    proc.stderr = ""
    return proc


# ==============================================================================
# Step 1: Characterization tests against unchanged run_tests()
# ==============================================================================

def test_char_package_json_npm(repo_dir):
    """package.json with test script, default to npm."""
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/npm"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["npm", "test"]
        assert mock_sub.call_args[0][0] == ["/bin/npm", "test"]


def test_char_package_json_yarn(repo_dir):
    """package.json with yarn.lock uses yarn."""
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    (repo_dir / "yarn.lock").write_text("", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/yarn"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["yarn", "test"]
        assert mock_sub.call_args[0][0] == ["/bin/yarn", "test"]


def test_char_package_json_pnpm(repo_dir):
    """package.json with pnpm-lock.yaml uses pnpm."""
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    (repo_dir / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/pnpm"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["pnpm", "test"]
        assert mock_sub.call_args[0][0] == ["/bin/pnpm", "test"]


def test_char_package_json_tool_missing_env_not_ready(repo_dir):
    """package.json when npm is missing from PATH returns ENV_NOT_READY."""
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value=None), patch("subprocess.run") as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.ENV_NOT_READY
        assert ws.last_test_run["command"] == ["npm", "test"]
        assert mock_sub.call_count == 0


def test_char_package_json_malformed(repo_dir):
    """package.json with invalid JSON returns FAILED."""
    (repo_dir / "package.json").write_text("{invalid json", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    outcome = ws.run_tests()
    assert outcome == TestOutcome.FAILED
    assert ws.last_test_run["ecosystem"] == "package_json"
    assert ws.last_test_run["command"] is None


def test_char_package_json_no_test_script_falls_through(repo_dir):
    """package.json without test script falls through to no tests collected."""
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"build": "tsc"}}), encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    outcome = ws.run_tests()
    assert outcome == TestOutcome.NO_TESTS_COLLECTED
    assert ws.last_test_run["command"] is None


def test_char_go_mod(repo_dir):
    """go.mod uses go test ./..."""
    (repo_dir / "go.mod").write_text("module example.com/foo\n\ngo 1.20\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/go"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["go", "test", "./..."]
        assert mock_sub.call_args[0][0] == ["/bin/go", "test", "./..."]


def test_char_go_mod_tool_missing_env_not_ready(repo_dir):
    """go.mod when go is missing returns ENV_NOT_READY."""
    (repo_dir / "go.mod").write_text("module example.com/foo\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value=None), patch("subprocess.run") as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.ENV_NOT_READY
        assert ws.last_test_run["command"] == ["go", "test", "./..."]
        assert mock_sub.call_count == 0


def test_char_go_mod_subprocess_exception_returns_failed(repo_dir):
    """go.mod when subprocess.run raises returns FAILED."""
    (repo_dir / "go.mod").write_text("module example.com/foo\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/go"), patch("subprocess.run", side_effect=OSError("Boom")):
        outcome = ws.run_tests()
        assert outcome == TestOutcome.FAILED
        assert ws.last_test_run["command"] == ["go", "test", "./..."]


def test_char_cargo_toml(repo_dir):
    """Cargo.toml uses cargo test."""
    (repo_dir / "Cargo.toml").write_text("[package]\nname = \"foo\"\nversion = \"0.1.0\"\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/cargo"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["cargo", "test"]
        assert mock_sub.call_args[0][0] == ["/bin/cargo", "test"]


def test_char_cargo_toml_tool_missing_env_not_ready(repo_dir):
    """Cargo.toml when cargo is missing returns ENV_NOT_READY."""
    (repo_dir / "Cargo.toml").write_text("[package]\nname = \"foo\"\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value=None), patch("subprocess.run") as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.ENV_NOT_READY
        assert ws.last_test_run["command"] == ["cargo", "test"]
        assert mock_sub.call_count == 0


def test_char_pom_xml_default_mvn(repo_dir):
    """pom.xml with no wrapper uses mvn test."""
    (repo_dir / "pom.xml").write_text("<project></project>", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/mvn"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["mvn", "test"]
        assert mock_sub.call_args[0][0] == ["/bin/mvn", "test"]


def test_char_pom_xml_wrapper(repo_dir):
    """pom.xml with wrapper uses mvnw or mvnw.cmd."""
    (repo_dir / "pom.xml").write_text("<project></project>", encoding="utf-8")
    if sys.platform == "win32":
        (repo_dir / "mvnw.cmd").write_text("@echo off", encoding="utf-8")
        expected_cmd = [str(repo_dir / "mvnw.cmd"), "test"]
    else:
        (repo_dir / "mvnw").write_text("#!/bin/sh", encoding="utf-8")
        expected_cmd = ["./mvnw", "test"]
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/resolved/mvnw"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == expected_cmd
        assert mock_sub.call_args[0][0] == ["/resolved/mvnw", "test"]


def test_char_pom_xml_tool_missing_env_not_ready(repo_dir):
    """pom.xml when mvn is missing returns ENV_NOT_READY."""
    (repo_dir / "pom.xml").write_text("<project></project>", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value=None), patch("subprocess.run") as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.ENV_NOT_READY
        assert ws.last_test_run["command"] == ["mvn", "test"]
        assert mock_sub.call_count == 0


def test_char_build_gradle_default(repo_dir):
    """build.gradle with no wrapper uses gradle test."""
    (repo_dir / "build.gradle").write_text("plugins {}", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/gradle"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["gradle", "test"]
        assert mock_sub.call_args[0][0] == ["/bin/gradle", "test"]


def test_char_build_gradle_kts(repo_dir):
    """build.gradle.kts with no wrapper uses gradle test."""
    (repo_dir / "build.gradle.kts").write_text("plugins {}", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/gradle"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["gradle", "test"]
        assert mock_sub.call_args[0][0] == ["/bin/gradle", "test"]


def test_char_build_gradle_wrapper(repo_dir):
    """build.gradle with gradlew wrapper uses gradlew or gradlew.bat."""
    (repo_dir / "build.gradle").write_text("plugins {}", encoding="utf-8")
    if sys.platform == "win32":
        (repo_dir / "gradlew.bat").write_text("@echo off", encoding="utf-8")
        expected_cmd = [str(repo_dir / "gradlew.bat"), "test"]
    else:
        (repo_dir / "gradlew").write_text("#!/bin/sh", encoding="utf-8")
        expected_cmd = ["./gradlew", "test"]
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/resolved/gradlew"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == expected_cmd
        assert mock_sub.call_args[0][0] == ["/resolved/gradlew", "test"]


def test_char_build_gradle_tool_missing_env_not_ready(repo_dir):
    """build.gradle when gradle is missing returns ENV_NOT_READY."""
    (repo_dir / "build.gradle").write_text("plugins {}", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value=None), patch("subprocess.run") as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.ENV_NOT_READY
        assert ws.last_test_run["command"] == ["gradle", "test"]
        assert mock_sub.call_count == 0


def test_char_pytest_ini(repo_dir):
    """pytest.ini uses [sys.executable, -m, pytest, -q]."""
    (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == [sys.executable, "-m", "pytest", "-q"]
        assert mock_sub.call_args[0][0] == [sys.executable, "-m", "pytest", "-q"]


def test_char_pyproject_toml_pytest(repo_dir):
    """pyproject.toml with [tool.pytest uses pytest."""
    (repo_dir / "pyproject.toml").write_text("[tool.pytest.ini_options]\nminversion = '6.0'\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == [sys.executable, "-m", "pytest", "-q"]


def test_char_python_test_file_pattern(repo_dir):
    """Repository with tests/test_app.py uses pytest."""
    tests_dir = repo_dir / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_app.py").write_text("def test_one(): pass\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == [sys.executable, "-m", "pytest", "-q"]


def test_char_pytest_returncode_5_no_tests_collected(repo_dir):
    """pytest exit code 5 maps to NO_TESTS_COLLECTED."""
    (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    proc = MagicMock()
    proc.returncode = 5
    proc.stdout = "no tests collected"
    proc.stderr = ""
    with patch("subprocess.run", return_value=proc):
        outcome = ws.run_tests()
        assert outcome == TestOutcome.NO_TESTS_COLLECTED
        assert ws.last_test_run["command"] == [sys.executable, "-m", "pytest", "-q"]


def test_char_pytest_returncode_1_failed(repo_dir):
    """pytest exit code 1 maps to FAILED."""
    (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    proc = MagicMock()
    proc.returncode = 1
    proc.stdout = "1 failed"
    proc.stderr = ""
    with patch("subprocess.run", return_value=proc):
        outcome = ws.run_tests()
        assert outcome == TestOutcome.FAILED
        assert ws.last_test_run["command"] == [sys.executable, "-m", "pytest", "-q"]


def test_char_terraform_validate(repo_dir):
    """Terraform repo with .tf file uses terraform validate."""
    (repo_dir / "main.tf").write_text('resource "null_resource" "x" {}\n', encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/terraform"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["terraform", "validate"]
        assert mock_sub.call_args[0][0] == ["terraform", "validate"]


def test_char_terraform_tool_missing_env_not_ready(repo_dir):
    """Terraform repo when terraform binary is missing returns ENV_NOT_READY."""
    (repo_dir / "main.tf").write_text('resource "null_resource" "x" {}\n', encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value=None), patch("subprocess.run") as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.ENV_NOT_READY
        assert ws.last_test_run["command"] == ["terraform", "validate"]
        assert mock_sub.call_count == 0


def test_char_ansible_lint(repo_dir):
    """Ansible repo with ansible.cfg and ansible-lint installed uses ansible-lint."""
    (repo_dir / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    def which_side_effect(cmd):
        return "/bin/ansible-lint" if cmd == "ansible-lint" else None

    with patch("shutil.which", side_effect=which_side_effect), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["ansible-lint"]
        assert mock_sub.call_args[0][0] == ["ansible-lint"]


def test_char_ansible_playbook_syntax_check(repo_dir):
    """Ansible repo without ansible-lint falls back to ansible-playbook --syntax-check."""
    (repo_dir / "playbook.yml").write_text("- hosts: all\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    def which_side_effect(cmd):
        return "/bin/ansible-playbook" if cmd == "ansible-playbook" else None

    with patch("shutil.which", side_effect=which_side_effect), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        expected_cmd = ["ansible-playbook", "--syntax-check", str(repo_dir / "playbook.yml")]
        assert ws.last_test_run["command"] == expected_cmd
        assert mock_sub.call_args[0][0] == expected_cmd


def test_char_ansible_tools_missing_env_not_ready(repo_dir):
    """Ansible repo when neither tool is present returns ENV_NOT_READY."""
    (repo_dir / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value=None), patch("subprocess.run") as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.ENV_NOT_READY
        assert ws.last_test_run["command"] == ["ansible-lint"]
        assert mock_sub.call_count == 0


def test_char_helm_lint(repo_dir):
    """Helm repo with Chart.yaml uses helm lint ."""
    (repo_dir / "Chart.yaml").write_text("name: test\nversion: 0.1.0\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value="/bin/helm"), patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == ["helm", "lint", "."]
        assert mock_sub.call_args[0][0] == ["helm", "lint", "."]


def test_char_helm_tool_missing_env_not_ready(repo_dir):
    """Helm repo when helm is missing returns ENV_NOT_READY."""
    (repo_dir / "Chart.yaml").write_text("name: test\nversion: 0.1.0\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    with patch("shutil.which", return_value=None), patch("subprocess.run") as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.ENV_NOT_READY
        assert ws.last_test_run["command"] == ["helm", "lint", "."]
        assert mock_sub.call_count == 0


def test_char_fallback_no_tests_collected(repo_dir):
    """Repo with no recognizable test framework returns NO_TESTS_COLLECTED and command None."""
    ws = Workspace(repo_dir=repo_dir)
    outcome = ws.run_tests()
    assert outcome == TestOutcome.NO_TESTS_COLLECTED
    assert ws.last_test_run["command"] is None
    assert ws.last_test_run["ecosystem"] is None


# ==============================================================================
# Step 2: Tests for resolve_test_command itself
# ==============================================================================

def test_resolve_test_command_runs_no_subprocess(repo_dir):
    """resolve_test_command runs no subprocess (assert via mock)."""
    (repo_dir / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8")
    with patch("subprocess.run") as mock_sub_run, patch("subprocess.Popen") as mock_sub_popen:
        res = resolve_test_command(repo_dir)
        assert res.command == ["npm", "test"]
        assert mock_sub_run.call_count == 0
        assert mock_sub_popen.call_count == 0


def test_resolve_test_command_returns_same_command_run_tests_executes(repo_dir):
    """resolve_test_command returns the same command run_tests then executes."""
    (repo_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    ws = Workspace(repo_dir=repo_dir)

    res = resolve_test_command(repo_dir)
    assert res.command == [sys.executable, "-m", "pytest", "-q"]

    with patch("subprocess.run", return_value=_mock_success_process()) as mock_sub:
        outcome = ws.run_tests()
        assert outcome == TestOutcome.PASSED
        assert ws.last_test_run["command"] == res.command
        assert mock_sub.call_args[0][0] == res.command


def test_resolve_test_command_returns_none_with_reason_when_no_tests_exist(repo_dir):
    """resolve_test_command returns command None with a reason when no tests exist."""
    res = resolve_test_command(repo_dir)
    assert res.command is None
    assert res.reason is not None
    assert len(res.reason) > 0
    assert res.source == "none"

