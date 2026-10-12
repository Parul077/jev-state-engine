import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from dotenv import dotenv_values

from jev.engine import JevEngine
from jev.gatekeeper import Gatekeeper
from jev.ledger import NullRecorder, SqliteLedger
from jev.workspace import Workspace


import uuid
from datetime import datetime, timezone


def generate_default_thread_id() -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rnd = uuid.uuid4().hex[:4]
    return f"run-{ts}-{rnd}"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Jev State Engine: Deterministic State Machine with Two-Tier Gating"
    )
    parser.add_argument(
        "ticket",
        type=str,
        help="The ticket or task description for the engine to execute",
    )
    parser.add_argument(
        "--repo-dir",
        type=str,
        default=None,
        help="Path to the repository to execute within (defaults to current working directory)",
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="checkpoints.db",
        help="Path to the SQLite checkpoint database file (defaults to checkpoints.db)",
    )
    parser.add_argument(
        "--thread-id",
        type=str,
        default=generate_default_thread_id(),
        help="Execution thread identifier for checkpoint persistence (defaults to run-<utc-timestamp>-<4 hex>)",
    )
    parser.add_argument(
        "--test-policy",
        type=str,
        choices=["auto", "verify-only", "never", "always"],
        default="auto",
        help="Execution policy for repository test suites (auto, verify-only, never, always; defaults to auto)",
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        default=False,
        help="Disable recording to SQLite ledger",
    )
    parser.add_argument(
        "--retry-intel",
        choices=["on", "off"],
        default="on",
        help="Enable or disable retry intelligence (dead-end block, duplicate-diff guard, typed semantic feedback). Default: on.",
    )
    return parser



def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    repo_dir = Path(args.repo_dir).resolve() if args.repo_dir else Path.cwd().resolve()

    # Wire Dependency Injection
    workspace = Workspace(repo_dir=repo_dir)
    engine_env = Path.cwd() / ".env"
    if not engine_env.exists():
        engine_env = Path(__file__).resolve().parent.parent / ".env"
    env_file = engine_env if engine_env.exists() else ((repo_dir / ".env") if (repo_dir / ".env").exists() else None)
    gatekeeper = Gatekeeper(env_file=env_file)

    if args.no_ledger:
        recorder = NullRecorder()
    else:
        try:
            recorder = SqliteLedger(repo_dir=repo_dir)
        except Exception as e:
            print(f"ledger disabled: {e}", file=sys.stderr, flush=True)
            recorder = NullRecorder()

    # Wire Gemini LLM (gemini-3.5-flash-lite) via langchain-google-genai
    google_api_key = os.environ.get("GOOGLE_API_KEY")
    if not google_api_key and env_file and env_file.exists():
        parsed_env = dotenv_values(env_file)
        google_api_key = parsed_env.get("GOOGLE_API_KEY")

    llm = None
    if google_api_key:
        os.environ["GOOGLE_API_KEY"] = google_api_key
        try:
            from langchain_google_genai import ChatGoogleGenerativeAI

            llm = ChatGoogleGenerativeAI(
                model="gemini-3.5-flash-lite",
                google_api_key=google_api_key,
            )
        except Exception as e:
            print(f"[WARNING] Could not initialize ChatGoogleGenerativeAI: {e}")
            llm = None

    engine = JevEngine(
        workspace=workspace,
        gatekeeper=gatekeeper,
        llm=llm,
        recorder=recorder,
        db_path=args.db_path,
        test_policy=args.test_policy,
        retry_intel=args.retry_intel,
    )


    final_state = engine.execute(ticket=args.ticket, thread_id=args.thread_id)

    status = final_state.get("status")
    gate_status = final_state.get("gate_status")

    if status == "escalated":
        print(f"[ESCALATION] Engine halted. Triggering status: {gate_status}")
        return 1
    elif status != "completed":
        print(f"[FAILURE] Engine halted with status: {status}, gate_status: {gate_status}")
        return 1

    print(f"[SUCCESS] Ticket executed successfully. Final status: {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
