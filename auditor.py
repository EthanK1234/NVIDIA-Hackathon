"""Auditor agent: decides whether persistently failing tests are wrong, not the code.

Only invoked by the controller when a stuck-minority pattern is detected —
the same small set of tests failing across consecutive iterations while the
majority pass. The default assumption is CODE_WRONG; TESTS_WRONG requires
step-by-step reasoning against the spec, not just agreement with code output.
"""

from __future__ import annotations

import re

from .base import call_claude, extract_code

SYSTEM = """You are a test-suite auditor. A multi-agent coding loop is stuck: a small set of tests keeps failing across multiple iterations while most tests pass, and the generated code looks plausible.

Your job: decide whether the FAILING TESTS contain wrong expected values (TESTS_WRONG), or whether the code is actually wrong (CODE_WRONG).

Default assumption: CODE_WRONG. Only return TESTS_WRONG when you can demonstrate via step-by-step reasoning — working directly from the task spec — that the expected value in a test assertion violates the spec. Do NOT use the code's output as evidence. Reason from the spec yourself, showing your work.

Output format (exact, in this order):
VERDICT: TESTS_WRONG
or
VERDICT: CODE_WRONG
or
VERDICT: UNCLEAR

REASONING: <2-4 sentences citing specific spec text or test assertions to justify your verdict>

CORRECTED_TESTS: <if and only if VERDICT is TESTS_WRONG, provide the full corrected test file in a fenced ```python``` block. Omit this section entirely for CODE_WRONG or UNCLEAR.>"""

# Detects the presence of a fenced python block without consuming it.
_HAS_PYTHON_FENCE = re.compile(r"```python\n", re.DOTALL)


def audit(
    task: str,
    tests: str,
    code: str,
    exec_summary: str,
) -> dict:
    """Audit failing tests to determine whether they or the code are wrong.

    Returns:
        {
            "verdict":         "TESTS_WRONG" | "CODE_WRONG" | "UNCLEAR",
            "reasoning":       plain-text REASONING section,
            "corrected_tests": extracted code only (no prose), or None,
            "raw":             full auditor response, for debugging,
        }
    """
    user = (
        f"# Task spec\n{task}\n\n"
        f"# Current test suite\n```python\n{tests}\n```\n\n"
        f"# Latest generated code\n```python\n{code}\n```\n\n"
        f"# Test execution output\n{exec_summary}\n\n"
        "Audit the failing tests now."
    )
    text = call_claude(SYSTEM, user)
    verdict = _parse_audit_verdict(text)
    reasoning = _extract_reasoning(text)

    corrected = None
    if verdict == "TESTS_WRONG":
        if _HAS_PYTHON_FENCE.search(text):
            corrected = extract_code(text, language="python")
        else:
            print("[auditor] WARNING: TESTS_WRONG verdict but no ```python``` block found — downgrading to UNCLEAR")
            verdict = "UNCLEAR"

    if verdict == "TESTS_WRONG" and not corrected:
        print("[auditor] WARNING: extract_code returned empty result — downgrading to UNCLEAR")
        verdict = "UNCLEAR"

    return {
        "verdict": verdict,
        "reasoning": reasoning,
        "corrected_tests": corrected,
        "raw": text,
    }


def _parse_audit_verdict(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().upper().lstrip("#*- ").strip()
        if stripped.startswith("VERDICT"):
            if "TESTS_WRONG" in stripped:
                return "TESTS_WRONG"
            if "CODE_WRONG" in stripped:
                return "CODE_WRONG"
            return "UNCLEAR"
    return "UNCLEAR"


def _extract_reasoning(text: str) -> str:
    """Pull the REASONING section from the auditor response."""
    in_section = False
    lines = []
    for line in text.splitlines():
        upper = line.strip().upper()
        if upper.startswith("REASONING"):
            in_section = True
            tail = line[line.find(":") + 1:].strip() if ":" in line else ""
            if tail:
                lines.append(tail)
            continue
        if in_section:
            if upper.startswith("CORRECTED_TESTS") or upper.startswith("VERDICT"):
                break
            lines.append(line)
    return "\n".join(lines).strip()
