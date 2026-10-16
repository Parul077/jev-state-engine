import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import httpx
import pytest

from jev.dependency_delta import DepChange, DependencyDelta
from jev.engine import JevEngine, SafeRecorder, _accepts_param, node_gate
from jev.gatekeeper import Gatekeeper
from jev.ledger import NullRecorder, SqliteLedger
from jev.models import MechanicalCheckResult, Subgoal, ValidationVerdict
from main import build_parser


# ============================================================================
# 1. Flag & CLI parsing
# ============================================================================

def test_cli_dep_delta_jev_flag_default():
    parser = build_parser()
    args = parser.parse_args(["ticket-123"])
    assert hasattr(args, "dep_delta_jev")
    assert args.dep_delta_jev == "off"


def test_cli_dep_delta_jev_flag_explicit():
    parser = build_parser()
    args_on = parser.parse_args(["ticket-123", "--dep-delta-jev", "on"])
    assert args_on.dep_delta_jev == "on"

    args_off = parser.parse_args(["ticket-123", "--dep-delta-jev", "off"])
    assert args_off.dep_delta_jev == "off"


def test_cli_dep_delta_jev_flag_invalid():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["ticket-123", "--dep-delta-jev", "invalid"])


# ============================================================================
# 2. Gatekeeper.validate_subgoal payload, capping, and last_state_keys
# ============================================================================

def test_gatekeeper_validate_subgoal_without_dependency_delta(monkeypatch):
    gk = Gatekeeper(api_url="https://fake.typesafe.ai/v1/systemone", api_key="fake_key")
    captured_payloads = []

    def fake_post(url, json=None, headers=None):
        captured_payloads.append(json)
        return httpx.Response(
            200,
            json={"valid": {"noul": 0.95}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(gk.client, "post", fake_post)

    subgoal = Subgoal(description="Add test", scope=["test.py"])
    verdict = gk.validate_subgoal(subgoal, diff="diff --git a/test.py b/test.py")

    assert verdict.valid is True
    assert len(captured_payloads) == 1
    state = captured_payloads[0]["state"]
    assert "dependency_delta" not in state
    assert gk.last_state_keys == "description,scope,diff"


def test_gatekeeper_validate_subgoal_with_dependency_delta(monkeypatch):
    gk = Gatekeeper(api_url="https://fake.typesafe.ai/v1/systemone", api_key="fake_key")
    captured_payloads = []

    def fake_post(url, json=None, headers=None):
        captured_payloads.append(json)
        return httpx.Response(
            200,
            json={"valid": {"noul": 0.95}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(gk.client, "post", fake_post)

    delta_dict = {
        "added": [{"name": "requests", "ecosystem": "pypi", "manifest": "requirements.txt", "section": "dependencies"}],
        "removed": [],
        "changed": [],
        "unparsed": [],
    }

    subgoal = Subgoal(description="Add requests", scope=["requirements.txt"])
    verdict = gk.validate_subgoal(
        subgoal,
        diff="diff --git a/requirements.txt b/requirements.txt",
        dependency_delta=delta_dict,
    )

    assert verdict.valid is True
    assert len(captured_payloads) == 1
    state = captured_payloads[0]["state"]
    assert "dependency_delta" in state
    assert state["dependency_delta"]["added"][0]["name"] == "requests"
    assert "truncated" not in state["dependency_delta"]
    assert gk.last_state_keys == "description,scope,diff,dependency_delta"
    # Ensure questions dict is unchanged (all 5 questions present)
    assert set(captured_payloads[0]["questions"].keys()) == {
        "valid", "incomplete", "wrong_behavior", "conflicts_with_notes", "kind"
    }


def test_gatekeeper_validate_subgoal_capping_50_entries(monkeypatch):
    gk = Gatekeeper(api_url="https://fake.typesafe.ai/v1/systemone", api_key="fake_key")
    captured_payloads = []

    def fake_post(url, json=None, headers=None):
        captured_payloads.append(json)
        return httpx.Response(
            200,
            json={"valid": {"noul": 0.95}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(gk.client, "post", fake_post)

    # 60 added entries, 55 removed entries, 10 changed entries
    added_list = [{"name": f"pkg-add-{i}", "ecosystem": "pypi", "manifest": "req.txt", "section": "deps"} for i in range(60)]
    removed_list = [{"name": f"pkg-rem-{i}", "ecosystem": "pypi", "manifest": "req.txt", "section": "deps"} for i in range(55)]
    changed_list = [{"name": f"pkg-chg-{i}", "ecosystem": "pypi", "manifest": "req.txt", "section": "deps"} for i in range(10)]

    delta_dict = {
        "added": added_list,
        "removed": removed_list,
        "changed": changed_list,
        "unparsed": [],
    }

    subgoal = Subgoal(description="Mass update", scope=["req.txt"])
    gk.validate_subgoal(subgoal, diff="", dependency_delta=delta_dict)

    state_delta = captured_payloads[0]["state"]["dependency_delta"]
    assert len(state_delta["added"]) == 50
    assert len(state_delta["removed"]) == 50
    assert len(state_delta["changed"]) == 10
    # Dropped entries: (60 - 50) + (55 - 50) = 10 + 5 = 15
    assert state_delta.get("truncated") == 15

    # Original caller dict should not be mutated
    assert len(delta_dict["added"]) == 60


# ============================================================================
# 3. Gatekeeper safety fallback on 400 / 422
# ============================================================================

def test_gatekeeper_400_fallback_retries_without_dependency_delta_and_persists(monkeypatch, caplog):
    gk = Gatekeeper(api_url="https://fake.typesafe.ai/v1/systemone", api_key="fake_key")
    call_count = 0
    captured_payloads = []

    def fake_post(url, json=None, headers=None):
        nonlocal call_count
        call_count += 1
        captured_payloads.append(json)
        if "dependency_delta" in json.get("state", {}):
            # Request with dependency_delta gets 400
            return httpx.Response(
                400,
                text="Invalid state key: dependency_delta is unrecognized",
                request=httpx.Request("POST", url),
            )
        # Request without dependency_delta succeeds
        return httpx.Response(
            200,
            json={"valid": {"noul": 0.92}},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(gk.client, "post", fake_post)

    delta_dict = {"added": [{"name": "foo"}], "removed": [], "changed": [], "unparsed": []}
    subgoal = Subgoal(description="Add foo", scope=["f.txt"])

    with caplog.at_level(logging.WARNING):
        verdict = gk.validate_subgoal(subgoal, diff="some diff", dependency_delta=delta_dict)

    assert verdict.valid is True
    assert call_count == 2
    # First payload had dependency_delta
    assert "dependency_delta" in captured_payloads[0]["state"]
    # Second payload had all 5 questions and NO dependency_delta
    assert "dependency_delta" not in captured_payloads[1]["state"]
    assert set(captured_payloads[1]["questions"].keys()) == {
        "valid", "incomplete", "wrong_behavior", "conflicts_with_notes", "kind"
    }
    # Instance remembers unsupported
    assert getattr(gk, "_dep_delta_unsupported", False) is True
    assert gk.last_state_keys == "description,scope,diff"

    # Exactly one warning logged naming 400 and truncated response body
    warnings = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "400" in warnings[0]
    assert "dependency_delta is unrecognized" in warnings[0]

    # Subsequent call on same instance NEVER sends dependency_delta again
    caplog.clear()
    captured_payloads.clear()
    call_count = 0

    verdict2 = gk.validate_subgoal(subgoal, diff="some diff 2", dependency_delta=delta_dict)
    assert verdict2.valid is True
    assert call_count == 1
    assert "dependency_delta" not in captured_payloads[0]["state"]
    # No new warnings logged
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 0


def test_gatekeeper_422_fallback_falls_through_to_valid_only_when_retry_also_fails(monkeypatch, caplog):
    gk = Gatekeeper(api_url="https://fake.typesafe.ai/v1/systemone", api_key="fake_key")
    call_count = 0
    captured_payloads = []

    def fake_post(url, json=None, headers=None):
        nonlocal call_count
        call_count += 1
        captured_payloads.append(json)
        if call_count == 1:
            # First call (with dep_delta) fails 422
            return httpx.Response(422, text="Unprocessable entity: dependency_delta", request=httpx.Request("POST", url))
        elif call_count == 2:
            # Second call (without dep_delta, all 5 questions) fails 422
            return httpx.Response(422, text="Unprocessable entity: questions schema", request=httpx.Request("POST", url))
        # Third call (valid-only fallback) succeeds
        return httpx.Response(200, json={"valid": {"noul": 0.88}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(gk.client, "post", fake_post)

    delta_dict = {"added": [{"name": "bar"}], "removed": [], "changed": [], "unparsed": []}
    subgoal = Subgoal(description="Add bar", scope=["b.txt"])

    verdict = gk.validate_subgoal(subgoal, diff="diff", dependency_delta=delta_dict)

    assert verdict.valid is True
    assert call_count == 3
    assert "dependency_delta" in captured_payloads[0]["state"]
    assert "dependency_delta" not in captured_payloads[1]["state"]
    assert len(captured_payloads[1]["questions"]) == 5
    # Third request is valid-only
    assert "dependency_delta" not in captured_payloads[2]["state"]
    assert list(captured_payloads[2]["questions"].keys()) == ["valid"]
    assert gk.last_state_keys == "description,scope,diff"


def test_gatekeeper_401_403_429_500_raise_immediately_without_delta_fallback(monkeypatch):
    for status_code in (401, 403, 429, 500):
        gk = Gatekeeper(api_url="https://fake.typesafe.ai/v1/systemone", api_key="fake_key", max_retries=1)
        call_count = 0

        def fake_post(url, json=None, headers=None):
            nonlocal call_count
            call_count += 1
            return httpx.Response(status_code, text=f"Error {status_code}", request=httpx.Request("POST", url))

        monkeypatch.setattr(gk.client, "post", fake_post)

        delta_dict = {"added": [{"name": "x"}], "removed": [], "changed": [], "unparsed": []}
        subgoal = Subgoal(description="Test", scope=["x.txt"])

        with pytest.raises(httpx.HTTPStatusError):
            gk.validate_subgoal(subgoal, diff="diff", dependency_delta=delta_dict)

        # Must not retry without dependency_delta (call_count should be 1)
        assert call_count == 1


# ============================================================================
# 4. Engine node_gate dependency_delta passing behavior
# ============================================================================

class MockOldGatekeeper:
    """Mock gatekeeper with old signature (no dependency_delta parameter)."""
    def __init__(self):
        self.call_args = None
        self.last_state_keys = "description,scope,diff"

    def validate_subgoal(self, subgoal, diff, mechanical_detail="", investigation_notes=None):
        self.call_args = {
            "subgoal": subgoal,
            "diff": diff,
            "mechanical_detail": mechanical_detail,
            "investigation_notes": investigation_notes,
        }
        return ValidationVerdict(valid=True, probability=0.99)


class MockNewGatekeeper:
    """Mock gatekeeper with dependency_delta parameter."""
    def __init__(self):
        self.called_with_dep_delta = None
        self.last_state_keys = "description,scope,diff"

    def validate_subgoal(self, subgoal, diff, mechanical_detail="", investigation_notes=None, dependency_delta=None):
        self.called_with_dep_delta = dependency_delta
        if dependency_delta is not None:
            self.last_state_keys = "description,scope,diff,dependency_delta"
        else:
            self.last_state_keys = "description,scope,diff"
        return ValidationVerdict(valid=True, probability=0.99)


def test_node_gate_flag_off_does_not_pass_dependency_delta():
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/package.json b/package.json"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    ws.get_manifest_changes.return_value = {
        "package.json": ('{"dependencies": {}}', '{"dependencies": {"express": "^4.18.2"}}')
    }
    ws._is_active_worktree = False

    old_gk = MockOldGatekeeper()
    state = {
        "dep_delta_jev": "off",
        "current_subgoal": Subgoal(description="Add express", scope=["package.json"]),
        "trajectory": [],
    }

    # Must succeed and NOT fail even though old_gk does not accept dependency_delta
    node_gate(state, workspace=ws, gatekeeper=old_gk)
    assert state["gate_status"] == "passed"


def test_node_gate_flag_on_passes_dependency_delta_when_changes_present():
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/package.json b/package.json"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    ws.get_manifest_changes.return_value = {
        "package.json": ('{"dependencies": {}}', '{"dependencies": {"express": "^4.18.2"}}')
    }
    ws._is_active_worktree = False

    new_gk = MockNewGatekeeper()
    state = {
        "dep_delta_jev": "on",
        "current_subgoal": Subgoal(description="Add express", scope=["package.json"]),
        "trajectory": [],
    }

    node_gate(state, workspace=ws, gatekeeper=new_gk)
    assert state["gate_status"] == "passed"
    assert new_gk.called_with_dep_delta is not None
    assert len(new_gk.called_with_dep_delta["added"]) == 1
    assert new_gk.called_with_dep_delta["added"][0]["name"] == "express"


def test_node_gate_flag_on_does_not_pass_dependency_delta_when_computed_empty():
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/src/index.js b/src/index.js"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    ws.get_manifest_changes.return_value = {}  # Empty manifest changes
    ws._is_active_worktree = False

    new_gk = MockNewGatekeeper()
    state = {
        "dep_delta_jev": "on",
        "current_subgoal": Subgoal(description="Update code", scope=["src/index.js"]),
        "trajectory": [],
    }

    node_gate(state, workspace=ws, gatekeeper=new_gk)
    assert state["gate_status"] == "passed"
    # When computed-empty, dependency_delta must NOT be passed
    assert new_gk.called_with_dep_delta is None


def test_node_gate_flag_on_does_not_pass_dependency_delta_when_workspace_raises():
    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/src/index.js b/src/index.js"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    ws.get_manifest_changes.side_effect = RuntimeError("git failure")
    ws._is_active_worktree = False

    new_gk = MockNewGatekeeper()
    state = {
        "dep_delta_jev": "on",
        "current_subgoal": Subgoal(description="Update code", scope=["src/index.js"]),
        "trajectory": [],
    }

    node_gate(state, workspace=ws, gatekeeper=new_gk)
    assert state["gate_status"] == "passed"
    # When computation fails (NULL), dependency_delta must NOT be passed
    assert new_gk.called_with_dep_delta is None


# ============================================================================
# 5. Ledger schema migrations and recording
# ============================================================================

def test_ledger_schema_migrations_and_recording(tmp_path):
    db_file = tmp_path / "test_ledger.db"
    ledger = SqliteLedger(db_path=db_file)

    # 1. Verify runs table has dep_delta_jev column
    ledger.record_run_start(
        run_id="run-1",
        thread_id="thread-1",
        ticket="Test ticket",
        dep_delta_jev="on",
    )

    with ledger._get_connection() as conn:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
        assert "dep_delta_jev" in cols
        row = conn.execute("SELECT dep_delta_jev FROM runs WHERE run_id = ?", ("run-1",)).fetchone()
        assert row[0] == "on"

    # 2. Verify attempts table has jev_state_keys column
    ledger.record_attempt(
        run_id="run-1",
        subgoal_id="sg-1",
        tier="semantic",
        outcome="passed",
        diff="some diff",
        jev_valid=True,
        jev_state_keys="description,scope,diff,dependency_delta",
    )

    with ledger._get_connection() as conn:
        att_cols = [r[1] for r in conn.execute("PRAGMA table_info(attempts)").fetchall()]
        assert "jev_state_keys" in att_cols
        row = conn.execute("SELECT jev_state_keys FROM attempts WHERE run_id = ? AND subgoal_id = ?", ("run-1", "sg-1")).fetchone()
        assert row[0] == "description,scope,diff,dependency_delta"


def test_engine_records_jev_state_keys_in_attempt(tmp_path):
    db_file = tmp_path / "engine_ledger.db"
    ledger = SqliteLedger(db_path=db_file)

    ws = MagicMock()
    ws.get_staged_diff.return_value = "diff --git a/requirements.txt b/requirements.txt"
    ws.run_mechanical_checks.return_value = MechanicalCheckResult(passed=True)
    ws.get_manifest_changes.return_value = {
        "requirements.txt": ("", "pytest>=7.0.0\n")
    }
    ws._is_active_worktree = False
    ws.base_commit = "commit-0"
    ws._get_head_commit.return_value = "commit-1"

    gk = MockNewGatekeeper()

    state = {
        "run_id": "run-keys-1",
        "thread_id": "thread-keys-1",
        "dep_delta_jev": "on",
        "current_subgoal": Subgoal(description="Add pytest", scope=["requirements.txt"]),
        "trajectory": [],
    }

    node_gate(state, workspace=ws, gatekeeper=gk, recorder=ledger)

    with ledger._get_connection() as conn:
        row = conn.execute(
            "SELECT jev_state_keys FROM attempts WHERE run_id = ?",
            ("run-keys-1",),
        ).fetchone()
        assert row is not None
        assert row[0] == "description,scope,diff,dependency_delta"
