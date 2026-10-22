"""Deterministic, provenance-tagged episodic memory store for Jev State Engine.

Stores and invalidates mechanically-derived facts (compile traps, test traps, env traps).
Memory is strictly derived from the run ledger, keyed by repo_key, stored outside the
target working tree, self-invalidating via git blob hashes, and fail-open.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Protocol, Tuple, Union

# Ensure src is importable
from jev.ledger import compute_repo_key, get_jev_home

logger = logging.getLogger(__name__)


def compute_git_blob_sha(content_bytes: bytes) -> str:
    """Compute the git blob SHA1 hash for a given byte string.

    Matches `git hash-object` exactly: sha1("blob <size>\\0<content>").
    """
    header = f"blob {len(content_bytes)}\0".encode("ascii")
    return hashlib.sha1(header + content_bytes).hexdigest()


def normalize_rel_path(path: Union[str, Path]) -> str:
    """Normalize a path to a relative POSIX path.

    Rejects absolute paths, Windows drive letters, and traversal attempts escaping root.
    """
    p_str = str(path).replace("\\", "/").strip()
    if not p_str:
        return ""
    # Reject absolute paths or drive letters
    if p_str.startswith("/") or (len(p_str) > 1 and p_str[1] == ":"):
        raise ValueError(f"Absolute paths or drive letters are not allowed in memory: {path}")

    # Remove leading ./
    while p_str.startswith("./"):
        p_str = p_str[2:]

    # Check for traversal
    parts = p_str.split("/")
    if ".." in parts:
        raise ValueError(f"Path traversal '..' is not allowed in memory: {path}")

    return p_str


@dataclass
class Fact:
    """A single mechanically-verified fact stored in knowledge.db."""

    repo_key: str
    kind: str  # 'compile_trap' | 'test_trap' | 'env_trap'
    signature: str  # '<toolchain>|<normalized_path>|<error_code_or_symbol>'
    target_file: str  # Normalized relative POSIX path
    statement: str  # Templated summary statement
    evidence_run_id: str
    evidence_attempt: int
    learned_at_commit: str
    valid_if_json: str  # JSON mapping of {"rel/path": "<git_blob_sha>"}
    id: Optional[int] = None
    fix_diff: Optional[str] = None
    hits: int = 0
    regressions: int = 0
    state: str = "active"  # 'active' | 'stale' | 'retired'
    created_at: Optional[str] = None
    last_validated_at: Optional[str] = None

    def __post_init__(self):
        now = datetime.now(timezone.utc).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.last_validated_at:
            self.last_validated_at = now
        self.target_file = normalize_rel_path(self.target_file)

    def parse_valid_if(self) -> Dict[str, str]:
        """Parse valid_if_json into a path-to-blob-sha dict."""
        try:
            data = json.loads(self.valid_if_json)
            if isinstance(data, dict):
                return {normalize_rel_path(k): str(v) for k, v in data.items()}
        except Exception:
            pass
        return {}


class KnowledgeStore(Protocol):
    """Protocol for cross-ticket knowledge storage and retrieval."""

    def store_fact(self, fact: Fact) -> bool:
        """Store a fact in knowledge.db. Returns True if inserted/updated."""
        ...

    def get_facts_for_files(
        self,
        repo_key: str,
        files: List[str],
        max_facts: int = 5,
        max_tokens: int = 400,
    ) -> List[Fact]:
        """Retrieve active, verified facts relevant to the specified in-scope files."""
        ...

    def get_fact_by_signature(self, signature: str) -> Optional[Fact]:
        """Retrieve a fact by its exact signature."""
        ...

    def mark_fact_stale(self, fact_id: int) -> None:
        """Mark a fact as stale when evidence check fails."""
        ...

    def record_hit(self, fact_id: int) -> None:
        """Record that a fact was retrieved and successfully applied."""
        ...

    def record_regression(self, fact_id: int) -> None:
        """Record that an attempt failed despite having this fact injected."""
        ...

    def is_fact_valid(self, fact: Fact, repo_dir: Path) -> bool:
        """Verify whether the fact's evidence files still match recorded blob hashes."""
        ...

    def list_facts(
        self,
        repo_key: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Fact]:
        """List stored facts with optional filtering."""
        ...

    def clear(self, repo_key: Optional[str] = None) -> None:
        """Clear facts from the store."""
        ...


class NullMemory:
    """Default no-op implementation of KnowledgeStore (memory off)."""

    def store_fact(self, fact: Fact) -> bool:
        return False

    def get_facts_for_files(
        self,
        repo_key: str,
        files: List[str],
        max_facts: int = 5,
        max_tokens: int = 400,
    ) -> List[Fact]:
        return []

    def get_fact_by_signature(self, signature: str) -> Optional[Fact]:
        return None

    def mark_fact_stale(self, fact_id: int) -> None:
        pass

    def record_hit(self, fact_id: int) -> None:
        pass

    def record_regression(self, fact_id: int) -> None:
        pass

    def is_fact_valid(self, fact: Fact, repo_dir: Path) -> bool:
        return False

    def list_facts(
        self,
        repo_key: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Fact]:
        return []

    def clear(self, repo_key: Optional[str] = None) -> None:
        pass


class SqliteKnowledgeStore:
    """SQLite implementation of KnowledgeStore storing facts outside working tree."""

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
            self.db_path = get_jev_home() / "repos" / repo_key / "knowledge.db"

        self.repo_key = repo_key
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
                CREATE TABLE IF NOT EXISTS facts (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  repo_key TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  signature TEXT NOT NULL UNIQUE,
                  target_file TEXT NOT NULL,
                  statement TEXT NOT NULL,
                  fix_diff TEXT,
                  evidence_run_id TEXT NOT NULL,
                  evidence_attempt INTEGER NOT NULL,
                  learned_at_commit TEXT NOT NULL,
                  valid_if_json TEXT NOT NULL,
                  hits INTEGER DEFAULT 0,
                  regressions INTEGER DEFAULT 0,
                  state TEXT DEFAULT 'active',
                  created_at TEXT NOT NULL,
                  last_validated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_facts_lookup 
                ON facts(repo_key, state, target_file);
                """
            )

    def _row_to_fact(self, row: Tuple) -> Fact:
        return Fact(
            id=row[0],
            repo_key=row[1],
            kind=row[2],
            signature=row[3],
            target_file=row[4],
            statement=row[5],
            fix_diff=row[6],
            evidence_run_id=row[7],
            evidence_attempt=row[8],
            learned_at_commit=row[9],
            valid_if_json=row[10],
            hits=row[11],
            regressions=row[12],
            state=row[13],
            created_at=row[14],
            last_validated_at=row[15],
        )

    def store_fact(self, fact: Fact) -> bool:
        """Insert or update a fact. Returns True on success."""
        try:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO facts (
                        repo_key, kind, signature, target_file, statement, fix_diff,
                        evidence_run_id, evidence_attempt, learned_at_commit, valid_if_json,
                        hits, regressions, state, created_at, last_validated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(signature) DO UPDATE SET
                        statement=excluded.statement,
                        fix_diff=excluded.fix_diff,
                        evidence_run_id=excluded.evidence_run_id,
                        evidence_attempt=excluded.evidence_attempt,
                        learned_at_commit=excluded.learned_at_commit,
                        valid_if_json=excluded.valid_if_json,
                        state='active',
                        last_validated_at=excluded.last_validated_at;
                    """,
                    (
                        fact.repo_key,
                        fact.kind,
                        fact.signature,
                        fact.target_file,
                        fact.statement,
                        fact.fix_diff,
                        fact.evidence_run_id,
                        fact.evidence_attempt,
                        fact.learned_at_commit,
                        fact.valid_if_json,
                        fact.hits,
                        fact.regressions,
                        fact.state,
                        fact.created_at,
                        fact.last_validated_at,
                    ),
                )
                return True
        except Exception as e:
            logger.warning("Failed to store fact %s: %s", fact.signature, e)
            return False

    def get_fact_by_signature(self, signature: str) -> Optional[Fact]:
        """Retrieve a fact by its unique signature."""
        try:
            with self._get_connection() as conn:
                row = conn.execute(
                    """
                    SELECT id, repo_key, kind, signature, target_file, statement, fix_diff,
                           evidence_run_id, evidence_attempt, learned_at_commit, valid_if_json,
                           hits, regressions, state, created_at, last_validated_at
                    FROM facts WHERE signature = ?;
                    """,
                    (signature,),
                ).fetchone()
                if row:
                    return self._row_to_fact(row)
        except Exception as e:
            logger.warning("Failed to get fact by signature %s: %s", signature, e)
        return None

    def mark_fact_stale(self, fact_id: int) -> None:
        """Mark a fact as stale."""
        try:
            with self._get_connection() as conn:
                conn.execute("UPDATE facts SET state = 'stale' WHERE id = ?;", (fact_id,))
        except Exception as e:
            logger.warning("Failed to mark fact %s as stale: %s", fact_id, e)

    def record_hit(self, fact_id: int) -> None:
        """Increment hit count for a helpful fact."""
        try:
            now = datetime.now(timezone.utc).isoformat()
            with self._get_connection() as conn:
                conn.execute(
                    "UPDATE facts SET hits = hits + 1, last_validated_at = ? WHERE id = ?;",
                    (now, fact_id),
                )
        except Exception as e:
            logger.warning("Failed to record hit for fact %s: %s", fact_id, e)

    def record_regression(self, fact_id: int) -> None:
        """Increment regression count for a fact that failed to prevent failure."""
        try:
            with self._get_connection() as conn:
                conn.execute("UPDATE facts SET regressions = regressions + 1 WHERE id = ?;", (fact_id,))
        except Exception as e:
            logger.warning("Failed to record regression for fact %s: %s", fact_id, e)

    def is_fact_valid(self, fact: Fact, repo_dir: Path) -> bool:
        """Validate evidence hashes against current repo_dir files.

        Returns True if all evidence matches. If any evidence file is missing
        or has an altered blob SHA, marks fact as stale and returns False.
        """
        if fact.state != "active":
            return False

        evidence = fact.parse_valid_if()
        if not evidence:
            return False

        for rel_path, expected_sha in evidence.items():
            file_path = repo_dir / rel_path
            if not file_path.exists() or not file_path.is_file():
                if fact.id is not None:
                    self.mark_fact_stale(fact.id)
                return False

            try:
                content = file_path.read_bytes()
                actual_sha = compute_git_blob_sha(content)
                if actual_sha != expected_sha:
                    if fact.id is not None:
                        self.mark_fact_stale(fact.id)
                    return False
            except Exception:
                if fact.id is not None:
                    self.mark_fact_stale(fact.id)
                return False

        # Update last_validated_at timestamp
        if fact.id is not None:
            try:
                now = datetime.now(timezone.utc).isoformat()
                with self._get_connection() as conn:
                    conn.execute("UPDATE facts SET last_validated_at = ? WHERE id = ?;", (now, fact.id))
            except Exception:
                pass

        return True

    def get_facts_for_files(
        self,
        repo_key: str,
        files: List[str],
        max_facts: int = 5,
        max_tokens: int = 400,
    ) -> List[Fact]:
        """Retrieve and rank active facts relevant to the given file list.

        Ranking score:
          (exact file match * 10) + (dir match * 5) + (hits * 2) - (regressions * 5)
        """
        try:
            normalized_files = set()
            for f in files:
                try:
                    norm = normalize_rel_path(f)
                    if norm:
                        normalized_files.add(norm)
                except ValueError:
                    continue

            with self._get_connection() as conn:
                rows = conn.execute(
                    """
                    SELECT id, repo_key, kind, signature, target_file, statement, fix_diff,
                           evidence_run_id, evidence_attempt, learned_at_commit, valid_if_json,
                           hits, regressions, state, created_at, last_validated_at
                    FROM facts 
                    WHERE repo_key = ? AND state = 'active';
                    """,
                    (repo_key,),
                ).fetchall()

            candidates: List[Tuple[float, Fact]] = []
            for row in rows:
                fact = self._row_to_fact(row)
                score = 0.0

                # Target file match (primary tier)
                if fact.target_file in normalized_files:
                    score += 100.0
                else:
                    # Directory prefix match (secondary tier)
                    fact_dir = str(Path(fact.target_file).parent.as_posix())
                    if fact_dir and fact_dir != ".":
                        for nf in normalized_files:
                            if nf.startswith(fact_dir + "/"):
                                score += 20.0
                                break

                # Only consider facts with non-zero relevance score
                if score > 0.0:
                    score += (fact.hits * 2.0) - (fact.regressions * 5.0)
                    candidates.append((score, fact))

            # Sort descending by score
            candidates.sort(key=lambda x: x[0], reverse=True)

            selected: List[Fact] = []
            chars_budget = max_tokens * 4  # Standard ~4 chars per token approximation
            current_chars = 0

            for _, fact in candidates[:max_facts]:
                fact_len = len(fact.statement) + (len(fact.fix_diff) if fact.fix_diff else 0)
                if selected and (current_chars + fact_len > chars_budget):
                    break
                selected.append(fact)
                current_chars += fact_len

            return selected
        except Exception as e:
            logger.warning("Failed to retrieve facts for repo %s: %s", repo_key, e)
            return []

    def list_facts(
        self,
        repo_key: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Fact]:
        """List facts matching criteria."""
        try:
            query = "SELECT id, repo_key, kind, signature, target_file, statement, fix_diff, evidence_run_id, evidence_attempt, learned_at_commit, valid_if_json, hits, regressions, state, created_at, last_validated_at FROM facts WHERE 1=1"
            params: List[Any] = []
            if repo_key:
                query += " AND repo_key = ?"
                params.append(repo_key)
            if state:
                query += " AND state = ?"
                params.append(state)
            query += " ORDER BY id DESC"

            with self._get_connection() as conn:
                rows = conn.execute(query, tuple(params)).fetchall()
                return [self._row_to_fact(r) for r in rows]
        except Exception as e:
            logger.warning("Failed to list facts: %s", e)
            return []

    def clear(self, repo_key: Optional[str] = None) -> None:
        """Clear facts."""
        try:
            with self._get_connection() as conn:
                if repo_key:
                    conn.execute("DELETE FROM facts WHERE repo_key = ?;", (repo_key,))
                else:
                    conn.execute("DELETE FROM facts;")
        except Exception as e:
            logger.warning("Failed to clear facts: %s", e)


class SafeKnowledgeStore:
    """Fail-open wrapper around any KnowledgeStore.

    Ensures that any exception inside the storage layer never raises to
    or interrupts the engine execution flow.
    """

    def __init__(self, inner: Optional[KnowledgeStore] = None):
        self.inner = inner or NullMemory()

    def store_fact(self, fact: Fact) -> bool:
        try:
            return self.inner.store_fact(fact)
        except Exception as e:
            logger.warning("SafeKnowledgeStore.store_fact suppressed exception: %s", e)
            return False

    def get_facts_for_files(
        self,
        repo_key: str,
        files: List[str],
        max_facts: int = 5,
        max_tokens: int = 400,
    ) -> List[Fact]:
        try:
            return self.inner.get_facts_for_files(
                repo_key=repo_key,
                files=files,
                max_facts=max_facts,
                max_tokens=max_tokens,
            )
        except Exception as e:
            logger.warning("SafeKnowledgeStore.get_facts_for_files suppressed exception: %s", e)
            return []

    def get_fact_by_signature(self, signature: str) -> Optional[Fact]:
        try:
            return self.inner.get_fact_by_signature(signature)
        except Exception as e:
            logger.warning("SafeKnowledgeStore.get_fact_by_signature suppressed exception: %s", e)
            return None

    def mark_fact_stale(self, fact_id: int) -> None:
        try:
            self.inner.mark_fact_stale(fact_id)
        except Exception as e:
            logger.warning("SafeKnowledgeStore.mark_fact_stale suppressed exception: %s", e)

    def record_hit(self, fact_id: int) -> None:
        try:
            self.inner.record_hit(fact_id)
        except Exception as e:
            logger.warning("SafeKnowledgeStore.record_hit suppressed exception: %s", e)

    def record_regression(self, fact_id: int) -> None:
        try:
            self.inner.record_regression(fact_id)
        except Exception as e:
            logger.warning("SafeKnowledgeStore.record_regression suppressed exception: %s", e)

    def is_fact_valid(self, fact: Fact, repo_dir: Path) -> bool:
        try:
            return self.inner.is_fact_valid(fact, repo_dir)
        except Exception as e:
            logger.warning("SafeKnowledgeStore.is_fact_valid suppressed exception: %s", e)
            return False

    def list_facts(
        self,
        repo_key: Optional[str] = None,
        state: Optional[str] = None,
    ) -> List[Fact]:
        try:
            return self.inner.list_facts(repo_key=repo_key, state=state)
        except Exception as e:
            logger.warning("SafeKnowledgeStore.list_facts suppressed exception: %s", e)
            return []

    def clear(self, repo_key: Optional[str] = None) -> None:
        try:
            self.inner.clear(repo_key=repo_key)
        except Exception as e:
            logger.warning("SafeKnowledgeStore.clear suppressed exception: %s", e)
