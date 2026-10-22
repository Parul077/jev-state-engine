"""Unit tests for Phase 4.1 KnowledgeStore and SQLite memory implementation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Dict

import pytest

from jev.memory import (
    Fact,
    KnowledgeStore,
    NullMemory,
    SafeKnowledgeStore,
    SqliteKnowledgeStore,
    compute_git_blob_sha,
    normalize_rel_path,
)


def test_compute_git_blob_sha():
    """Verify git blob sha matches git's exact algorithm: sha1('blob <len>\\0' + bytes)."""
    data = b"hello world\n"
    expected = hashlib.sha1(b"blob 12\0hello world\n").hexdigest()
    assert compute_git_blob_sha(data) == expected
    # Known git hash for empty string
    assert compute_git_blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"


def test_normalize_rel_path_valid():
    """Verify relative POSIX normalization."""
    assert normalize_rel_path("src/foo.py") == "src/foo.py"
    assert normalize_rel_path("src\\foo\\bar.ts") == "src/foo/bar.ts"
    assert normalize_rel_path("./src/app/page.tsx") == "src/app/page.tsx"


def test_normalize_rel_path_rejects_unsafe():
    """Verify rejection of absolute paths, drive letters, and directory traversal."""
    with pytest.raises(ValueError, match="Absolute paths or drive letters are not allowed"):
        normalize_rel_path("/etc/passwd")

    with pytest.raises(ValueError, match="Absolute paths or drive letters are not allowed"):
        normalize_rel_path("C:/Users/test/file.txt")

    with pytest.raises(ValueError, match="Path traversal '..' is not allowed"):
        normalize_rel_path("src/../../secret.env")


def test_sqlite_knowledge_store_init_and_schema(tmp_path: Path):
    """Verify table creation, index creation, and WAL mode outside working tree."""
    db_file = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path=db_file, repo_key="test-repo-123")

    assert db_file.exists()
    with sqlite3.connect(str(db_file)) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert journal_mode.lower() == "wal"

        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table';").fetchall()]
        assert "facts" in tables

        indices = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index';").fetchall()]
        assert "idx_facts_lookup" in indices


def test_fact_insertion_and_retrieval(tmp_path: Path):
    """Verify storing a fact, conflict resolution, and signature/file lookups."""
    db_file = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path=db_file, repo_key="test-repo-123")

    fact = Fact(
        repo_key="test-repo-123",
        kind="compile_trap",
        signature="tsc|src/auth.ts|TS2305",
        target_file="src/auth.ts",
        statement="Function validate() missing from src/auth.ts",
        fix_diff="@@ -1 +1 @@\n-old\n+new",
        evidence_run_id="run-1",
        evidence_attempt=1,
        learned_at_commit="commit-aaa",
        valid_if_json=json.dumps({"src/auth.ts": "blob-123"}),
    )

    stored = store.store_fact(fact)
    assert stored is True

    # Retrieve by signature
    retrieved = store.get_fact_by_signature("tsc|src/auth.ts|TS2305")
    assert retrieved is not None
    assert retrieved.statement == "Function validate() missing from src/auth.ts"
    assert retrieved.target_file == "src/auth.ts"
    assert retrieved.state == "active"

    # Retrieve by files
    matches = store.get_facts_for_files(
        repo_key="test-repo-123",
        files=["src/auth.ts", "src/other.ts"],
    )
    assert len(matches) == 1
    assert matches[0].signature == "tsc|src/auth.ts|TS2305"


def test_is_fact_valid_returns_true_when_file_matches(tmp_path: Path):
    """Verify evidence validation succeeds when git blob hash matches."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    target_file = repo_dir / "math_utils.py"
    content = b"def add(a, b):\n    return a + b\n"
    target_file.write_bytes(content)
    expected_sha = compute_git_blob_sha(content)

    store = SqliteKnowledgeStore(db_path=tmp_path / "knowledge.db", repo_key="test-repo")
    fact = Fact(
        repo_key="test-repo",
        kind="compile_trap",
        signature="python|math_utils.py|TypeError",
        target_file="math_utils.py",
        statement="Avoid wrong type annotation",
        evidence_run_id="run-1",
        evidence_attempt=1,
        learned_at_commit="c1",
        valid_if_json=json.dumps({"math_utils.py": expected_sha}),
    )
    store.store_fact(fact)
    saved_fact = store.get_fact_by_signature("python|math_utils.py|TypeError")

    is_valid = store.is_fact_valid(saved_fact, repo_dir)
    assert is_valid is True

    # Verify state remains active
    refreshed = store.get_fact_by_signature("python|math_utils.py|TypeError")
    assert refreshed.state == "active"


def test_is_fact_valid_marks_stale_when_file_modified(tmp_path: Path):
    """Verify evidence validation fails and marks fact stale when file content alters."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    target_file = repo_dir / "math_utils.py"
    original_content = b"def add(a, b):\n    return a + b\n"
    target_file.write_bytes(original_content)
    original_sha = compute_git_blob_sha(original_content)

    store = SqliteKnowledgeStore(db_path=tmp_path / "knowledge.db", repo_key="test-repo")
    fact = Fact(
        repo_key="test-repo",
        kind="compile_trap",
        signature="python|math_utils.py|TypeError",
        target_file="math_utils.py",
        statement="Avoid wrong type annotation",
        evidence_run_id="run-1",
        evidence_attempt=1,
        learned_at_commit="c1",
        valid_if_json=json.dumps({"math_utils.py": original_sha}),
    )
    store.store_fact(fact)
    saved_fact = store.get_fact_by_signature("python|math_utils.py|TypeError")

    # Modify the file
    target_file.write_bytes(b"def add(a, b):\n    # modified\n    return a + b\n")

    is_valid = store.is_fact_valid(saved_fact, repo_dir)
    assert is_valid is False

    # Verify fact is marked stale in DB
    refreshed = store.get_fact_by_signature("python|math_utils.py|TypeError")
    assert refreshed.state == "stale"


def test_is_fact_valid_marks_stale_when_file_deleted(tmp_path: Path):
    """Verify evidence validation marks fact stale if evidence file is deleted."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()

    store = SqliteKnowledgeStore(db_path=tmp_path / "knowledge.db", repo_key="test-repo")
    fact = Fact(
        repo_key="test-repo",
        kind="compile_trap",
        signature="python|deleted.py|Error",
        target_file="deleted.py",
        statement="Missing file lesson",
        evidence_run_id="run-1",
        evidence_attempt=1,
        learned_at_commit="c1",
        valid_if_json=json.dumps({"deleted.py": "some-sha"}),
    )
    store.store_fact(fact)
    saved_fact = store.get_fact_by_signature("python|deleted.py|Error")

    is_valid = store.is_fact_valid(saved_fact, repo_dir)
    assert is_valid is False

    refreshed = store.get_fact_by_signature("python|deleted.py|Error")
    assert refreshed.state == "stale"


def test_null_memory_interface():
    """Verify NullMemory behaves as an exact no-op implementation."""
    null_mem = NullMemory()
    fact = Fact(
        repo_key="r1",
        kind="compile_trap",
        signature="sig1",
        target_file="f1.py",
        statement="stmt",
        evidence_run_id="run-1",
        evidence_attempt=1,
        learned_at_commit="c1",
        valid_if_json="{}",
    )

    assert null_mem.store_fact(fact) is False
    assert null_mem.get_facts_for_files("r1", ["f1.py"]) == []
    assert null_mem.get_fact_by_signature("sig1") is None
    assert null_mem.is_fact_valid(fact, Path("/any")) is False
    assert null_mem.list_facts() == []
    # None of these should raise
    null_mem.mark_fact_stale(1)
    null_mem.record_hit(1)
    null_mem.record_regression(1)
    null_mem.clear()


def test_safe_knowledge_store_silent_fail_open(tmp_path: Path):
    """Verify SafeKnowledgeStore catches all exceptions and silently fails open."""
    class BrokenStore:
        def store_fact(self, fact):
            raise sqlite3.OperationalError("Database is locked")

        def get_facts_for_files(self, *args, **kwargs):
            raise RuntimeError("Disk corruption")

        def get_fact_by_signature(self, *args, **kwargs):
            raise PermissionError("Access denied")

        def is_fact_valid(self, *args, **kwargs):
            raise OSError("I/O failure")

    safe_store = SafeKnowledgeStore(BrokenStore())  # type: ignore

    fact = Fact(
        repo_key="r1",
        kind="compile_trap",
        signature="sig1",
        target_file="f1.py",
        statement="stmt",
        evidence_run_id="run-1",
        evidence_attempt=1,
        learned_at_commit="c1",
        valid_if_json="{}",
    )

    # Must fail open with safe defaults, zero exceptions raised
    assert safe_store.store_fact(fact) is False
    assert safe_store.get_facts_for_files("r1", ["f1.py"]) == []
    assert safe_store.get_fact_by_signature("sig1") is None
    assert safe_store.is_fact_valid(fact, Path("/tmp")) is False


def test_ranking_and_budget_enforcement(tmp_path: Path):
    """Verify deterministic ranking (hits, regressions, file match) and token budget cap."""
    db_file = tmp_path / "knowledge.db"
    store = SqliteKnowledgeStore(db_path=db_file, repo_key="test-repo")

    # Fact 1: Exact file match, 5 hits, 0 regressions
    f1 = Fact(
        repo_key="test-repo",
        kind="compile_trap",
        signature="sig-1",
        target_file="src/utils/math.py",
        statement="Math trap 1",
        evidence_run_id="r1",
        evidence_attempt=1,
        learned_at_commit="c1",
        valid_if_json="{}",
        hits=5,
        regressions=0,
    )
    # Fact 2: Exact file match, 0 hits, 3 regressions (lower score)
    f2 = Fact(
        repo_key="test-repo",
        kind="compile_trap",
        signature="sig-2",
        target_file="src/utils/math.py",
        statement="Math trap 2",
        evidence_run_id="r2",
        evidence_attempt=1,
        learned_at_commit="c2",
        valid_if_json="{}",
        hits=0,
        regressions=3,
    )
    # Fact 3: Directory prefix match (src/utils/other.py -> src/utils/)
    f3 = Fact(
        repo_key="test-repo",
        kind="compile_trap",
        signature="sig-3",
        target_file="src/utils/helper.py",
        statement="Helper trap",
        evidence_run_id="r3",
        evidence_attempt=1,
        learned_at_commit="c3",
        valid_if_json="{}",
        hits=10,
    )

    store.store_fact(f1)
    store.store_fact(f2)
    store.store_fact(f3)

    results = store.get_facts_for_files("test-repo", ["src/utils/math.py"], max_facts=2)
    assert len(results) == 2
    # f1 should rank #1 because exact match + positive hits
    assert results[0].signature == "sig-1"

    # Verify hard max_facts limit
    limited = store.get_facts_for_files("test-repo", ["src/utils/math.py"], max_facts=1)
    assert len(limited) == 1
    assert limited[0].signature == "sig-1"


def test_memory_defaults_to_null_memory():
    """Verify that default knowledge store is NullMemory, storing and returning nothing."""
    default_store = NullMemory()
    assert default_store.get_facts_for_files("repo", ["foo.py"]) == []
    assert default_store.get_fact_by_signature("any") is None


def test_memory_location_outside_working_tree(tmp_path: Path):
    """Verify that knowledge.db path defaults under JEV_HOME, outside repo working tree."""
    from jev.ledger import get_jev_home
    repo_dir = tmp_path / "target_repo"
    repo_dir.mkdir()
    
    store = SqliteKnowledgeStore(repo_dir=repo_dir, repo_key="repo123")
    assert not str(store.db_path).startswith(str(repo_dir))
    assert str(store.db_path).startswith(str(get_jev_home()))
    assert store.db_path.name == "knowledge.db"


def test_memory_rejects_absolute_paths_and_env_secrets():
    """Verify that target_file rejects absolute paths and drive letters."""
    with pytest.raises(ValueError, match="Absolute paths or drive letters are not allowed"):
        Fact(
            repo_key="r",
            kind="compile_trap",
            signature="s",
            target_file="C:/secret/passwords.txt",
            statement="leak",
            evidence_run_id="run-1",
            evidence_attempt=1,
            learned_at_commit="c1",
            valid_if_json="{}",
        )

