# Memory Design: Deterministic, Provenance-Tagged Cross-Ticket Memory for Jev State Engine

## 1. Summary

We recommend adding cross-ticket episodic memory via an append-only derivation pipeline: an external SQLite store (`knowledge.db`) located in `$JEV_HOME/repos/<repo_key>/`, populated strictly by deterministic extraction from the run ledger (`ledger.db`). Memory stores only mechanically verified facts (compiler diagnostic signatures, test error signatures, and their committed fix diffs), never freeform LLM opinions or post-mortem prose. Stored facts are self-invalidating via git blob/tree SHA hashes of their evidence files and subject to a hard budget (max 5 facts, max 400 tokens). Memory defaults to `off`, preserving 100% byte-identical prompts to today's engine.

---

## 2. Findings from the Code & Current State

### Test Suite Baseline
Execution of `pytest -q -m "not live"` on `master` branch (`09e4747`):
```text
546 passed, 3 skipped, 1 deselected, 1 warning in 273.43s (0:04:33)
```
*(Warning: `src/jev/models.py:24: PytestCollectionWarning: cannot collect test class 'TestPolicy' because it has a __init__ constructor`)*.

### Codebase Inspection Findings

1. **State Key Rigidity & AST Audit:**
   - In [`src/jev/models.py`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/models.py#L65-L98), the `State` TypedDict defines execution context.
   - Any new memory field (e.g. `memory_hints`, `memory_policy`) must be explicitly declared in `State.__annotations__`, otherwise `tests/test_state_guard.py` (`test_schema_guard_static_ast_audit`) fails mechanically by auditing all subscript writes `state['...']` in `src/jev/engine.py`.

2. **Inversion of Control in Constructor:**
   - In [`src/jev/engine.py`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/engine.py#L3493-L3520), `JevEngine.__init__` receives all components via dependency injection: `workspace`, `gatekeeper`, `llm`, `recorder`, `profiler`.
   - `knowledge_store` or `memory` must follow this exact IoC invariant, defaulting to `NullMemory`.

3. **Prompt Construction Points:**
   - In [`src/jev/engine.py`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/engine.py#L1505-L1535) (`node_plan`), prompts are assembled sequentially as a list of strings (`prompt_lines`).
   - In [`src/jev/engine.py`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/engine.py#L1818-L1858) (`node_implement`), prompts assemble `Ticket`, `Current Subgoal`, `Declared Scope`, `Investigation Notes`, `Repository Profile`, `Previous attempt failed gate verification. Feedback`, and the Dead-end block.
   - When memory is disabled (`off`), prompt assembly must omit the memory block entirely to guarantee byte-identical prompts.

4. **Gate Execution & Telemetry Recording:**
   - In [`src/jev/engine.py`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/engine.py#L2296-L2450) (`node_gate`), `_snapshot_attempt` records attempts to the ledger before worktree teardown.
   - Duplicate diffs are checked mechanically in `node_gate` (lines 2349–2376) against `state['failed_attempts']`.
   - `_invoke_qwen_strike_postmortem` runs strictly after gate failure and records advisory JSON to `qwen_postmortem`.

5. **Diagnostic Normalization & Baseline Diffing:**
   - In [`src/jev/workspace.py`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/workspace.py#L2231-L2340) (`Workspace.parse_diagnostics`), compiler and linter outputs are parsed into normalized objects with `(file, code, message)`.
   - In [`src/jev/workspace.py`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/workspace.py#L2354-L2650) (`Workspace.check_compile`), new errors are computed by subtracting baseline repository errors: `new_errors = wt_errors - baseline_errors`. Pre-existing breakage is already isolated mechanically.

6. **Ledger Immutability & Repo Keying:**
   - In [`src/jev/ledger.py`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/ledger.py#L40-L75), `compute_repo_key` calculates a stable 16-hex digest from initial root commit hashes (`git rev-list --max-parents=0 --all`).
   - The ledger resides in `$JEV_HOME/repos/<repo_key>/ledger.db` and records `runs`, `subgoals`, and `attempts`.

---

## 3. Proposed Design

### Data Model & Taxonomy

Memory stores three distinct, mechanically verified categories of facts:

| Fact Kind | Description | Example Signature | Evidence Hash |
| :--- | :--- | :--- | :--- |
| `compile_trap` | A compiler error introduced on attempt $N$ and solved on attempt $N+k$. | `TS2305\|src/auth.ts\|has no exported member 'validate'` | `package.json` SHA + `src/auth.ts` baseline SHA |
| `test_trap` | A test assertion failure caused by an invalid assumption or mock. | `pytest\|tests/test_auth.py::test_login\|KeyError: 'token'` | Test file SHA + target module SHA |
| `env_trap` | A missing runtime tool or runner configuration tripping `ENV_NOT_READY`. | `npm_vs_pnpm\|package.json\|lockfile_conflict` | Manifest and lockfile SHAs |

### What Never to Store (Prohibited in Memory)
- **Model-authored lessons or summaries:** Never store LLM self-reflections, as models hallucinate false causes.
- **Qwen Advisory Post-Mortems:** Qwen's `chain_of_thought` and `suggested_fix` are speculative debugging hints; storing them as ground-truth memory compounds hallucination across runs.
- **Raw ticket descriptions or full files:** Prevents context bloat and data leakage.
- **Pre-existing repository defects:** Handled by `Workspace.check_compile` baseline filtering.

### Storage Architecture
- **Location:** `$JEV_HOME/repos/<repo_key>/knowledge.db` (SQLite).
- **Separation:** Kept completely separate from `ledger.db` (append-only audit log) and `checkpoints.db` (LangGraph session graph).
- **Schema:**
```sql
CREATE TABLE facts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_key TEXT NOT NULL,
  kind TEXT NOT NULL,                 -- 'compile_trap' | 'test_trap' | 'env_trap'
  signature TEXT NOT NULL UNIQUE,     -- '<toolchain>|<normalized_path>|<error_code_or_symbol>'
  target_file TEXT NOT NULL,          -- Normalized relative POSIX path
  statement TEXT NOT NULL,            -- Templated statement: "File X failed Y when Z was attempted."
  fix_diff TEXT,                      -- Minimal patch (<= 40 lines) that resolved the failure
  evidence_run_id TEXT NOT NULL,
  evidence_attempt INTEGER NOT NULL,
  learned_at_commit TEXT NOT NULL,
  valid_if_json TEXT NOT NULL,         -- JSON map: {"path/to/file": "<git_blob_sha>", ...}
  hits INTEGER DEFAULT 0,
  regressions INTEGER DEFAULT 0,
  state TEXT DEFAULT 'active',        -- 'active' | 'stale' | 'retired'
  created_at TEXT NOT NULL,
  last_validated_at TEXT NOT NULL
);
CREATE INDEX idx_facts_lookup ON facts(repo_key, state, target_file);
```

### Extraction Pipeline
Facts are derived offline or at run conclusion by querying `ledger.db`:
1. Find all subgoals in `attempts` that took $\ge 1$ failure on check $C$ (e.g. `failed_check="compile"` with error signature $S$) and subsequently reached `outcome="passed"` on attempt $N+k$.
2. Extract the minimal diff hunk touching `target_file`.
3. Compute git blob hashes of `target_file` and relevant manifests at `learned_at_commit`.
4. Insert or update the fact in `knowledge.db`.

### Validation & Self-Invalidation (Honesty Guard)
On retrieval, every candidate fact is audited against the active workspace before injection:
1. Parse `valid_if_json`. For each file, inspect the active worktree commit (`git hash-object <path>` or `git rev-parse HEAD:<path>`).
2. If any hash differs from `valid_if_json`, or if the file no longer exists:
   - Mark fact as `stale` in `knowledge.db`.
   - **Do not inject into prompt.**
3. Apply a Time-To-Live (TTL): facts older than 30 days or not hit in 20 runs transition to `retired`.

### Retrieval & Ranking
- Query: Retrieve active facts where `target_file` matches `subgoal.scope` or directory prefixes of `subgoal.scope`.
- Ranking: Deterministic score:
  $$\text{Score} = (\text{Exact file match} \times 10) + (\text{Hits} \times 2) - (\text{Regressions} \times 5) + (\text{Recency rank})$$
- Hard Budget:
  - Maximum **5 facts**.
  - Maximum **400 tokens / 1,600 characters**.
  - Truncated deterministically with warning if exceeded.

### Injection Template & Position
- **In `node_plan`:** Injected directly after `Repository Profile` and before `Ticket Description`:
```text
Prior Lessons from Repository History (Advisory hints only - verify against current code):
- [compile_trap] In src/utils/math.py: Function `add` requires float return type; previously failed with TypeError.
```
- **In `node_implement`:** Injected directly after `Repository Profile` and before `Previous attempt failed gate verification`:
```text
Prior Traps & Verified Fixes for In-Scope Files (Advisory hints only):
- In math_utils.py (failed attempt run-101):
  Do not use floor division '//' for pagination.
  Verified fix diff:
  @@ -2,2 +2,2 @@
  - return (items // size)
  + return (items + size - 1) // size
```
- **Byte-Identical Invariant:** When `--memory off`, the string injected is empty (`""`), and no extra newlines are added, ensuring byte-identical prompt generation.

### CLI Flags & Environment Precedence
| Option | CLI Flag | Environment Variable | Default | Allowed Values |
| :--- | :--- | :--- | :--- | :--- |
| Memory Mode | `--memory` | `JEV_MEMORY` | `off` | `off`, `read`, `readwrite` |
| Max Facts | `--memory-max-facts` | `JEV_MEMORY_MAX_FACTS` | `5` | Positive integer |
| Max Tokens | `--memory-max-tokens`| `JEV_MEMORY_MAX_TOKENS`| `400` | Positive integer |

---

## 4. Enforcement Table

| Behavior | Enforcement | Mechanical Verification / Failure Mode |
| :--- | :--- | :--- |
| Memory defaults to `off` | **Mechanically Enforced** | `test_memory_cli_default_is_off` asserts default config is `off`. |
| Byte-identical prompts when `off` | **Mechanically Enforced** | `test_prompt_byte_identical_when_memory_off` compares sha256 of prompts with memory off vs baseline. |
| Invalidation on code modification | **Mechanically Enforced** | `test_fact_invalidated_when_file_hash_changes` checks fact is marked `stale` and omitted when git blob sha changes. |
| Pre-existing repo errors excluded | **Mechanically Enforced** | `Workspace.check_compile` baseline subtraction prevents pre-existing errors from being recorded in `failed_attempts`. |
| Hard prompt budget ceiling | **Mechanically Enforced** | `test_memory_budget_hard_cap` asserts injected string $\le 1,600$ characters regardless of fact volume. |
| Worktree isolation invariant | **Mechanically Enforced** | `knowledge.db` is stored under `~/.jev/repos/<key>/`, never inside the target repository working tree. |
| Worker follows memory hints | **Prompt-Only** | LLM may ignore or misapply fix hints. Backstopped mechanically by Tier 0 compiler/test gate and duplicate-diff detector. |
| Model opinion exclusion | **Mechanically Enforced** | Extractor accepts only typed tuples from `compile_errors_json` and git diffs; parser rejects freeform string inputs. |

---

## 5. Phased Build Order

### Phase 4.1: Storage & Invalidation Core (`phase-4a-memory-store`)
- **Deliverables:** `src/jev/memory.py` implementing `KnowledgeStore` protocol and `SqliteKnowledgeStore`. Schema creation, `valid_if_json` hashing, `is_valid()` evaluation via git CLI.
- **Test:** Unit tests verifying insertion, hash validation against git commits, and stale marking.
- **Exit Condition:** 100% passing tests for storage and invalidation without touching `engine.py`.

### Phase 4.2: Fact Extractor (`phase-4b-memory-extractor`)
- **Deliverables:** `src/jev/memory_extractor.py` and `scripts/memory.py` CLI (`list`, `rebuild`, `prune`). Analyzes `ledger.db` attempts to find failure $\to$ pass transitions on matching subgoals.
- **Test:** Synthetic ledger fixtures generating clean `compile_trap` and `test_trap` records.
- **Exit Condition:** Rebuilding `knowledge.db` from a sample run ledger produces verified facts with valid git hashes.

### Phase 4.3: Engine Retrieval & Prompt Injection (`phase-4c-memory-injection`)
- **Deliverables:** Add `--memory` flag in `src/main.py`. Update `State` in `src/jev/models.py`. Inject budgeted hints into `node_plan` and `node_implement` in `src/jev/engine.py`.
- **Test:** `test_prompt_byte_identical_when_memory_off` and `test_memory_injection_in_plan_and_implement`.
- **Exit Condition:** Full pytest suite passes with 0 regressions; prompts verified byte-identical when `--memory off`.

### Phase 4.4: Memory Update on Run Completion (`phase-4d-memory-writeback`)
- **Deliverables:** When `--memory readwrite`, `JevEngine.execute` triggers background extraction for the current run upon reaching `END`.
- **Test:** End-to-end multi-run test: Run 1 encounters and fixes a trap; Run 2 on a similar subgoal retrieves and avoids the trap.
- **Exit Condition:** Multi-run integration test demonstrates trap hit reduction.

---

## 6. Evaluation Plan

To prove that cross-ticket memory improves system performance without causing regressions:

### A/B Experimental Setup
- **Benchmark Suite:** 15 standardized tickets across 3 distinct ecosystems:
  - 5 Python tickets (`pytest`)
  - 5 TypeScript tickets (`npm test` / `tsc`)
  - 5 Go/Rust tickets (`go test` / `cargo test`)
- **Condition A (Control):** `--memory off` (current baseline engine).
- **Condition B (Treatment):** `--memory readwrite` (learning active across the ticket sequence).

### Quantitative Metrics
1. **First-Attempt Pass Rate ($Pass@1$):** Percentage of subgoals passing Tier 0 & Tier 1 on Attempt 1.
2. **Mechanical Strike Reduction:** Total count of Tier 0 build/compile/test failures across all runs.
3. **Escalation Rate:** Percentage of tickets tripping the 3-strike circuit breaker.
4. **Token & Latency Overhead:** Net token difference (memory prompt cost vs tokens saved by avoiding retries).

### Decision Thresholds (When to Keep Memory OFF)
Memory will **remain disabled by default** if:
- Final-verification rejection rate increases by $> 0\%$.
- $Pass@1$ fails to improve by at least $10\%$.
- Prompt token consumption from memory exceeds token savings from avoided retries.

---

## 7. Risks and Open Questions

1. **Context Pollution / Stale Trap Injection:**
   - *Risk:* A compiler error hint from an earlier library version misleads the worker when modifying modern code.
   - *Mitigation:* Strict git hash validation of manifest and target files prior to retrieval.
2. **Cross-Platform Path Inconsistency:**
   - *Risk:* Windows backslashes (`src\utils\math.py`) vs POSIX slashes (`src/utils/math.py`) causing key mismatch.
   - *Mitigation:* Enforce `Path(p).as_posix()` across all signatures, queries, and `valid_if` dictionaries.
3. **Prompt Bloat vs Useful Context:**
   - *Risk:* Injecting 5 diff hunks exhausts the worker's prompt window or distracts from the active ticket.
   - *Mitigation:* Hard mechanical cap of 400 tokens / 1,600 characters; diff hunks capped at 40 lines.
4. **Concurrency & Lock Contention:**
   - *Risk:* Multiple workers executing across concurrent threads writing to `knowledge.db`.
   - *Mitigation:* Enable SQLite WAL mode (`PRAGMA journal_mode=WAL;`) with a 5000ms busy timeout.
5. **Open Question: Codebase Reorganizations:**
   - If files are renamed (`git mv`), path-based signatures lose connection to their history. Evaluating AST symbol hashing instead of file paths is deferred to a future phase.

---

## 8. Unrelated Findings

During code inspection and test execution, the following unrelated issues were identified:

1. **Pytest Collection Warning on `TestPolicy`:**
   - In [`src/jev/models.py:24`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/models.py#L24), `class TestPolicy(str, Enum)` triggers `PytestCollectionWarning: cannot collect test class 'TestPolicy' because it has a __init__ constructor` because its name starts with `Test`.
   - *Recommendation:* Add `__test__ = False` inside `TestPolicy` (similar to `TestOutcome` in `models.py:8`).

2. **Ecosystem Asymmetry in Product Identity Discovery:**
   - In [`src/jev/engine.py:1864-1909`](file:///C:/Users/parul/PycharmProjects/jev-state-engine/src/jev/engine.py#L1864-L1909), product identity extraction hardcodes Next.js/React layout paths (`layout.tsx`, `layout.jsx`) and `package.json`. Repositories in Python, Go, Rust, and Java never have domain metadata extracted.

3. **Temporary Test Files in Git Status:**
   - When running scripts locally, files like `test_dataset.jsonl` or local test worktrees risk polluting the repository root if test cleanups fail.
