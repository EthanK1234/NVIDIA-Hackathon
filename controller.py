from __future__ import annotations

import ast
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Set, Tuple

from . import auditor, executor, generator, reviewer, tester


def _valid_tests(code: str) -> bool:
    """Return True iff code parses as Python, imports solution, and defines at least one test."""
    try:
        ast.parse(code)
    except SyntaxError:
        return False
    return "import solution" in code and "def test_" in code


def _failing_test_names(summary: str) -> Set[str]:
    """Extract failing test function names from a pytest -v summary string.

    Matches lines like: FAILED test_solution.py::test_foo_bar
    """
    names: Set[str] = set()
    for line in summary.splitlines():
        m = re.match(r"FAILED test_solution\.py::(\S+)", line.strip())
        if m:
            names.add(m.group(1))
    return names


def _best_of_n(
    task: str,
    tests: str,
    *,
    n: int,
    feedback: Optional[str],
    log: Callable[[str], None],
) -> Tuple[str, Dict]:

    log(f"[generator] sampling {n} candidates in parallel (extended thinking)...")
    with ThreadPoolExecutor(max_workers=n) as pool:
        gen_futures = [pool.submit(generator.generate, task, feedback, tests) for _ in range(n)]
        codes = [fut.result() for fut in gen_futures]

    log(f"[executor] running tests on {n} candidates in parallel...")
    with ThreadPoolExecutor(max_workers=n) as pool:
        exec_futures = [pool.submit(executor.run_tests, code, tests) for code in codes]
        exec_results = [fut.result() for fut in exec_futures]

    def _score(exec_res: Dict) -> Tuple[bool, int]:
        n_passed = len(re.findall(r"test_solution\.py::\S+ PASSED", exec_res["summary"]))
        return (exec_res["passed"], n_passed)

    scores = [_score(er) for er in exec_results]
    best_idx = max(range(n), key=lambda i: scores[i])
    log(f"[generator] candidate scores (all_pass, n_passed): {scores} → using #{best_idx + 1}")
    return codes[best_idx], exec_results[best_idx]


def run(
    task: str,
    max_iterations: int = 5,
    verbose: bool = True,
    tests: Optional[str] = None,
    samples: int = 3,
) -> Dict:
    """Run the full loop. Returns final code, tests, and per-iteration history.

    Args:
        tests:   Pre-generated test suite; skips the tester if provided.
        samples: Number of candidate solutions to generate per iteration.
                 The best is selected before review.
    """
    if max_iterations < 1:
        raise ValueError(f"max_iterations must be >= 1, got {max_iterations}")

    def log(msg: str) -> None:
        if verbose:
            print(msg, flush=True)

    log(f"[task] {task.strip()[:200]}{'...' if len(task) > 200 else ''}")
    if tests is None:
        log("[tester] writing tests from spec...")
        tests = tester.generate_tests(task)
    else:
        log("[tester] using provided tests, skipping generation.")

    history: List[Dict] = []
    feedback: Optional[str] = None
    code = ""
    prev_failing: Optional[Set[str]] = None

    for i in range(1, max_iterations + 1):
        log(f"\n=== iteration {i} ===")

        code, exec_result = _best_of_n(task, tests, n=samples, feedback=feedback, log=log)
        log(f"[executor] best candidate: tests {'PASSED' if exec_result['passed'] else 'FAILED'} "
            f"(returncode={exec_result['returncode']})")

        # Skip the reviewer when tests failed — no point reviewing code that
        # doesn't pass its own tests, and it saves one LLM call per failed iteration.
        if exec_result["passed"]:
            log("[reviewer] reviewing...")
            review_result = reviewer.review(task, code, exec_result["summary"])
            log(f"[reviewer] verdict: {review_result['verdict']}")
        else:
            log("[reviewer] skipped — tests failed.")
            review_result = {
                "verdict": "REQUEST_CHANGES",
                "text": "Tests failed — automatic rejection without reviewer call.",
            }

        # Compute and log interim confidence (test-based only; verifier runs at the end)
        _n_passed = len(re.findall(r"test_solution\.py::\S+ PASSED", exec_result["summary"]))
        _n_failed = len(re.findall(r"FAILED test_solution\.py::", exec_result["summary"]))
        _n_total = _n_passed + _n_failed
        if exec_result["passed"] and review_result["verdict"] == "APPROVE":
            _iter_conf = max(72, 95 - (i - 1) * 3)
        elif exec_result["passed"]:
            _iter_conf = 65
        elif _n_total > 0:
            _iter_conf = max(10, int((_n_passed / _n_total) * 65))
        else:
            _iter_conf = 10
        log(f"[confidence] iteration {i}: {_iter_conf}%  "
            f"({_n_passed}/{_n_total} tests passed, verdict: {review_result['verdict']})")

        history.append({
            "iteration": i,
            "code": code,
            "exec_summary": exec_result["summary"],
            "passed": exec_result["passed"],
            "review": review_result,
        })

        # Stop: success
        if exec_result["passed"] and review_result["verdict"] == "APPROVE":
            log("\n✓ DONE: tests pass and reviewer approves.")
            return {
                "status": "success",
                "code": code,
                "tests": tests,
                "iterations": i,
                "history": history,
            }
    
        if not exec_result["passed"]:
            current_failing = _failing_test_names(exec_result["summary"])
            passing_count = len(
                re.findall(r"test_solution\.py::\S+ PASSED", exec_result["summary"])
            )

            if (
                i >= 2
                and current_failing
                and current_failing == prev_failing
                and len(current_failing) <= 3
                and passing_count >= 5 * len(current_failing)
            ):
                log(
                    f"[auditor] stuck minority detected — {len(current_failing)} test(s) "
                    f"failing for 2 consecutive iterations with {passing_count} passing. "
                    f"Auditing: {current_failing}"
                )
                audit_result = auditor.audit(task, tests, code, exec_result["summary"])
                log(f"[auditor] verdict: {audit_result['verdict']}")
                log(f"[auditor] reasoning: {audit_result['reasoning'][:300]}")

                if audit_result["verdict"] == "TESTS_WRONG":
                    corrected = audit_result["corrected_tests"]
                    if corrected and _valid_tests(corrected):
                        log("[auditor] replacing tests with corrected version and retrying.")
                        tests = corrected
                        prev_failing = None
                        feedback = None  # old feedback was based on the wrong tests
                        continue
                    else:
                        log("[auditor] corrected tests invalid, keeping original tests.")

            prev_failing = current_failing
        else:
            prev_failing = None

        # Build feedback bundle for the next generator call
        parts: List[str] = []
        if not exec_result["passed"]:
            parts.append("## Test failures\n```\n" + exec_result["summary"] + "\n```")
        if review_result["verdict"] != "APPROVE":
            parts.append("## Reviewer feedback\n" + review_result["text"])
        feedback = "\n\n".join(parts)

    log(f"\n✗ STOPPED: hit max_iterations ({max_iterations}) without convergence.")
    return {
        "status": "max_iterations_reached",
        "code": code,
        "tests": tests,
        "iterations": max_iterations,
        "history": history,
    }
