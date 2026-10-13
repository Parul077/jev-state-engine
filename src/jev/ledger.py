from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Union

from jev.models import Subgoal


def get_jev_home() -> Path:
    """Returns the JEV_HOME directory (defaults to ~/.jev)."""
    return Path(os.environ.get("JEV_HOME", Path.home() / ".jev")).expanduser().resolve()


def compute_repo_key(repo_dir: Union[str, Path]) -> str:
    """Computes a stable repository key: first 16 hex chars of sha256 over sorted root commit hashes.

    Runs git rev-list --max-parents=0 HEAD. If it cannot be computed, returns 'unknown'.
    """
    try:
        res = subprocess.run(
            ["git", "rev-list", "--max-parents=0", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [line.strip() for line in res.stdout.strip().splitlines() if line.strip()]
        if not lines:
            return "unknown"
        lines.sort()
        combined = "\n".join(lines).encode("utf-8")
        return hashlib.sha256(combined).hexdigest()[:16]
    except Exception:
        return "unknown"


class Recorder(Protocol):
    """Protocol defining the recording interface for the run ledger."""

    def record_run_start(
        self,
        run_id: str,
        thread_id: str,
        ticket: str,
        base_commit: Optional[str] = None,
        engine_commit: Optional[str] = None,
        worker_model: Optional[str] = None,
        test_policy: Optional[str] = None,
        started_at: Optional[str] = None,
        retry_intel: Optional[str] = None,
    ) -> None: ...


    def record_run_end(
        self,
        run_id: str,
        final_status: str,
        final_commit: Optional[str] = None,
        ended_at: Optional[str] = None,
        notes_chars: Optional[int] = None,
    ) -> None: ...

    def record_subgoals(
        self,
        run_id: str,
        subgoals: List[Subgoal],
    ) -> None: ...

    def record_subgoal_committed(
        self,
        run_id: str,
        subgoal_id: str,
        parent_sha: Optional[str] = None,
        commit_sha: Optional[str] = None,
    ) -> None: ...

    def record_subgoal_status(
        self,
        run_id: str,
        subgoal_id: str,
        status: str,
    ) -> None: ...

    def record_attempt(
        self,
        run_id: str,
        subgoal_id: str,
        tier: str,
        outcome: str,
        failed_check: Optional[str] = None,
        diff: str = "",
        diff_sha: str = "",
        files: Optional[List[str]] = None,
        compile_errors: Optional[Any] = None,
        tests_ran: bool = False,
        test_outcome: Optional[str] = None,
        jev_valid: Optional[bool] = None,
        jev_probability: Optional[float] = None,
        jev_model: Optional[str] = None,
        jev_raw: Optional[str] = None,
        jev_tokens_in: Optional[int] = None,
        jev_tokens_out: Optional[int] = None,
        feedback_sent: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        created_at: Optional[str] = None,
    ) -> None: ...

    def has_run(self, run_id: str) -> bool: ...


class NullRecorder:
    """Default no-op recorder for JevEngine."""

    def record_run_start(
        self,
        run_id: str,
        thread_id: str,
        ticket: str,
        base_commit: Optional[str] = None,
        engine_commit: Optional[str] = None,
        worker_model: Optional[str] = None,
        test_policy: Optional[str] = None,
        started_at: Optional[str] = None,
        retry_intel: Optional[str] = None,
    ) -> None:
        pass


    def record_run_end(
        self,
        run_id: str,
        final_status: str,
        final_commit: Optional[str] = None,
        ended_at: Optional[str] = None,
        notes_chars: Optional[int] = None,
    ) -> None:
        pass

    def record_subgoals(
        self,
        run_id: str,
        subgoals: List[Subgoal],
    ) -> None:
        pass

    def record_subgoal_committed(
        self,
        run_id: str,
        subgoal_id: str,
        parent_sha: Optional[str] = None,
        commit_sha: Optional[str] = None,
    ) -> None:
        pass

    def record_subgoal_status(
        self,
        run_id: str,
        subgoal_id: str,
        status: str,
    ) -> None:
        pass

    def record_attempt(
        self,
        run_id: str,
        subgoal_id: str,
        tier: str,
        outcome: str,
        failed_check: Optional[str] = None,
        diff: str = "",
        diff_sha: str = "",
        files: Optional[List[str]] = None,
        compile_errors: Optional[Any] = None,
        tests_ran: bool = False,
        test_outcome: Optional[str] = None,
        jev_valid: Optional[bool] = None,
        jev_probability: Optional[float] = None,
        jev_model: Optional[str] = None,
        jev_raw: Optional[str] = None,
        jev_tokens_in: Optional[int] = None,
        jev_tokens_out: Optional[int] = None,
        feedback_sent: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        created_at: Optional[str] = None,
    ) -> None:
        pass

    def has_run(self, run_id: str) -> bool:
        return False


class SqliteLedger:
    """SQLite implementation of the run and failure ledger."""

    def __init__(
        self,
        repo_dir: Optional[Union[str, Path]] = None,
        repo_key: Optional[str] = None,
        db_path: Optional[Union[str, Path]] = None,
    ):
        if db_path is not None:
            self.db_path = Path(db_path).resolve()
        else:
            if not repo_key:
                repo_key = compute_repo_key(repo_dir) if repo_dir else "unknown"
            self.db_path = get_jev_home() / "repos" / repo_key / "ledger.db"

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=5.0)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY,
                  thread_id TEXT,
                  ticket TEXT,
                  base_commit TEXT,
                  final_commit TEXT,
                  engine_commit TEXT,
                  worker_model TEXT,
                  test_policy TEXT,
                  retry_intel TEXT,
                  evidence_level TEXT,
                  status TEXT,
                  started_at TEXT,
                  ended_at TEXT,
                  notes_chars INTEGER
                );
                """
            )
            # Additive migrations for existing databases
            cols = [r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()]
            if "retry_intel" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN retry_intel TEXT DEFAULT NULL")
            if "notes_chars" not in cols:
                conn.execute("ALTER TABLE runs ADD COLUMN notes_chars INTEGER")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subgoals (
                  run_id TEXT,
                  subgoal_id TEXT,
                  ordinal INTEGER,
                  description TEXT,
                  scope_json TEXT,
                  expects_tests INTEGER,
                  status TEXT,
                  parent_sha TEXT,
                  commit_sha TEXT,
                  PRIMARY KEY (run_id, subgoal_id)
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS attempts (
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

    def has_run(self, run_id: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            return row is not None

    def record_run_start(
        self,
        run_id: str,
        thread_id: str,
        ticket: str,
        base_commit: Optional[str] = None,
        engine_commit: Optional[str] = None,
        worker_model: Optional[str] = None,
        test_policy: Optional[str] = None,
        started_at: Optional[str] = None,
        retry_intel: Optional[str] = None,
    ) -> None:
        start_time = started_at or datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                  run_id, thread_id, ticket, base_commit, engine_commit,
                  worker_model, test_policy, retry_intel, status, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'in_progress', ?)
                ON CONFLICT(run_id) DO UPDATE SET
                  thread_id=excluded.thread_id,
                  ticket=excluded.ticket,
                  base_commit=excluded.base_commit,
                  engine_commit=excluded.engine_commit,
                  worker_model=excluded.worker_model,
                  test_policy=excluded.test_policy,
                  retry_intel=excluded.retry_intel,
                  status='in_progress',
                  started_at=excluded.started_at
                """,
                (
                    run_id,
                    thread_id,
                    ticket,
                    base_commit,
                    engine_commit,
                    worker_model,
                    test_policy,
                    retry_intel or "on",
                    start_time,
                ),
            )


    def record_run_end(
        self,
        run_id: str,
        final_status: str,
        final_commit: Optional[str] = None,
        ended_at: Optional[str] = None,
        notes_chars: Optional[int] = None,
    ) -> None:
        end_time = ended_at or datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE runs SET
                  status = ?,
                  final_commit = ?,
                  ended_at = ?,
                  notes_chars = ?
                WHERE run_id = ?
                """,
                (final_status, final_commit, end_time, notes_chars, run_id),
            )

    def record_subgoals(
        self,
        run_id: str,
        subgoals: List[Subgoal],
    ) -> None:
        with self._get_connection() as conn:
            for idx, sg in enumerate(subgoals):
                sg_id = sg.id or f"sg-{idx + 1}"
                scope_json = json.dumps(sg.scope)
                expects_tests = 1 if sg.expects_tests else 0
                conn.execute(
                    """
                    INSERT INTO subgoals (
                      run_id, subgoal_id, ordinal, description, scope_json,
                      expects_tests, status, parent_sha, commit_sha
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                    ON CONFLICT(run_id, subgoal_id) DO UPDATE SET
                      ordinal=excluded.ordinal,
                      description=excluded.description,
                      scope_json=excluded.scope_json,
                      expects_tests=excluded.expects_tests,
                      status=excluded.status
                    """,
                    (
                        run_id,
                        sg_id,
                        idx + 1,
                        sg.description,
                        scope_json,
                        expects_tests,
                        sg.status or "pending",
                    ),
                )

    def record_subgoal_committed(
        self,
        run_id: str,
        subgoal_id: str,
        parent_sha: Optional[str] = None,
        commit_sha: Optional[str] = None,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE subgoals SET
                  status = 'committed',
                  parent_sha = ?,
                  commit_sha = ?
                WHERE run_id = ? AND subgoal_id = ?
                """,
                (parent_sha, commit_sha, run_id, subgoal_id),
            )

    def record_subgoal_status(
        self,
        run_id: str,
        subgoal_id: str,
        status: str,
    ) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE subgoals SET
                  status = ?
                WHERE run_id = ? AND subgoal_id = ?
                """,
                (status, run_id, subgoal_id),
            )

    def record_attempt(
        self,
        run_id: str,
        subgoal_id: str,
        tier: str,
        outcome: str,
        failed_check: Optional[str] = None,
        diff: str = "",
        diff_sha: str = "",
        files: Optional[List[str]] = None,
        compile_errors: Optional[Any] = None,
        tests_ran: bool = False,
        test_outcome: Optional[str] = None,
        jev_valid: Optional[bool] = None,
        jev_probability: Optional[float] = None,
        jev_model: Optional[str] = None,
        jev_raw: Optional[str] = None,
        jev_tokens_in: Optional[int] = None,
        jev_tokens_out: Optional[int] = None,
        feedback_sent: Optional[str] = None,
        tokens_in: Optional[int] = None,
        tokens_out: Optional[int] = None,
        created_at: Optional[str] = None,
    ) -> None:
        now_ts = created_at or datetime.now(timezone.utc).isoformat()
        if not diff_sha:
            diff_sha = hashlib.sha256(diff.encode("utf-8")).hexdigest() if diff else hashlib.sha256(b"").hexdigest()

        files_json = json.dumps(files if files is not None else [])
        compile_json = None
        if compile_errors is not None:
            compile_json = json.dumps(compile_errors) if not isinstance(compile_errors, str) else compile_errors

        int_jev_valid = None
        if jev_valid is not None:
            int_jev_valid = 1 if jev_valid else 0

        with self._get_connection() as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM attempts WHERE run_id = ? AND subgoal_id = ?",
                (run_id, subgoal_id),
            ).fetchone()[0]
            attempt_no = count + 1

            conn.execute(
                """
                INSERT INTO attempts (
                  run_id, subgoal_id, attempt_no, tier, outcome, failed_check,
                  diff, diff_sha, files_json, compile_errors_json,
                  tests_ran, test_outcome,
                  jev_valid, jev_probability, jev_model, jev_raw,
                  jev_tokens_in, jev_tokens_out,
                  feedback_sent, tokens_in, tokens_out, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    subgoal_id,
                    attempt_no,
                    tier,
                    outcome,
                    failed_check,
                    diff,
                    diff_sha,
                    files_json,
                    compile_json,
                    1 if tests_ran else 0,
                    test_outcome,
                    int_jev_valid,
                    jev_probability,
                    jev_model,
                    jev_raw,
                    jev_tokens_in,
                    jev_tokens_out,
                    feedback_sent,
                    tokens_in,
                    tokens_out,
                    now_ts,
                ),
            )
