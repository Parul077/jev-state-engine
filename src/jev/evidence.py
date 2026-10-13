from __future__ import annotations

from typing import Any, Iterable, Optional, Tuple, Union

from jev.models import CompileOutcome, TestOutcome, TestPolicy

EVIDENCE_ORDER: Tuple[str, ...] = ("semantic-only", "compile-only", "tests-only", "tests+compile")
ORDER = EVIDENCE_ORDER


def _normalize_outcome(val: Any) -> Optional[str]:
    if val is None:
        return None
    if isinstance(val, dict):
        val = val.get("outcome")
    if hasattr(val, "value"):
        val = val.value
    if val is None:
        return None
    return str(val).strip()


def _normalize_policy(val: Any) -> Optional[str]:
    if val is None:
        return None
    if hasattr(val, "value"):
        val = val.value
    return str(val).strip().lower()


def evidence_level(
    compile_outcome: Any,
    test_outcome: Any,
    test_policy: Optional[Any] = None,
) -> str:
    """Derives evidence level from compile outcome and test outcome.

    Rules:
    - compile PASSED and tests PASSED -> 'tests+compile'
    - tests PASSED and compile is not PASSED (EXEMPT, not run, no compile command) -> 'tests-only'
    - compile PASSED and tests anything else (NO_TESTS_COLLECTED, NO_TEST_FRAMEWORK, not run, test_policy never or verify-only) -> 'compile-only'
    - everything else -> 'semantic-only'
    """
    c = _normalize_outcome(compile_outcome)
    t = _normalize_outcome(test_outcome)
    p = _normalize_policy(test_policy)

    compile_passed = (c == CompileOutcome.PASSED.value or c == "PASSED")

    # If policy is never, tests are disabled and cannot count as passed
    if p in ("never", TestPolicy.NEVER.value):
        tests_passed = False
    else:
        tests_passed = (t == TestOutcome.PASSED.value or t == "PASSED")

    if compile_passed and tests_passed:
        return "tests+compile"
    elif tests_passed and not compile_passed:
        return "tests-only"
    elif compile_passed and not tests_passed:
        return "compile-only"
    else:
        return "semantic-only"


def weakest(levels: Iterable[Optional[str]]) -> Optional[str]:
    """Returns the weakest evidence level from an iterable of levels using EVIDENCE_ORDER.

    semantic-only < compile-only < tests-only < tests+compile.
    Returns None if no valid levels are present.
    """
    valid = [lvl for lvl in levels if lvl in EVIDENCE_ORDER]
    if not valid:
        return None
    return min(valid, key=EVIDENCE_ORDER.index)


def format_evidence_summary(
    evidence_lvl: Optional[str],
    test_policy: Optional[str] = None,
) -> str:
    """Formats the evidence level summary line.

    Wording must say tests did not run whenever the level has no tests.
    E.g. 'Evidence: compile-only (no tests ran: test_policy=verify-only)'
    """
    if evidence_lvl in ("tests+compile", "tests-only"):
        return f"Evidence: {evidence_lvl}"

    lvl_str = evidence_lvl or "none"
    if test_policy:
        return f"Evidence: {lvl_str} (no tests ran: test_policy={test_policy})"
    return f"Evidence: {lvl_str} (no tests ran)"
