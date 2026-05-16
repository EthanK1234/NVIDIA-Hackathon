"""Verifier agent: traces spec examples to sanity-check the final solution.

Runs once after the main loop as a final correctness gate. Manually executes
the code on each input/output example stated in the spec and confirms the
results match. Catches cases where the code passes weak tests but fails on
the canonical examples.
"""

from __future__ import annotations

from typing import Dict

from .base import call_claude, GENERATOR_MODEL

SYSTEM = """You are a code verifier. Given a task spec and a Python solution, trace through each input/output example explicitly stated in the spec — step by step — to confirm the code produces the correct answer.

For each example in the spec:
1. Identify the exact input and expected output stated in the spec.
2. Mentally execute the code on that input, showing your reasoning step by step.
3. State whether the output matches the expected value.

Output format (exact):
VERDICT: VERIFIED
or
VERDICT: LIKELY_WRONG
or
VERDICT: UNCERTAIN

TRACE:
<step-by-step per-example trace>

ISSUES:
<describe any mismatch; empty if VERIFIED>

Use VERIFIED only when every spec example produces the correct output.
Use LIKELY_WRONG when at least one example yields a wrong result.
Use UNCERTAIN when you cannot confidently trace the execution."""


def verify(task: str, code: str) -> Dict[str, str]:
    """Trace spec examples against the final solution.

    Returns {'verdict': 'VERIFIED'|'LIKELY_WRONG'|'UNCERTAIN', 'text': <full trace>}.
    """
    user = (
        f"# Task spec\n{task}\n\n"
        f"# Solution\n```python\n{code}\n```\n\n"
        "Trace each spec example now."
    )
    text = call_claude(SYSTEM, user, model=GENERATOR_MODEL)
    return {"verdict": _parse_verdict(text), "text": text}


def _parse_verdict(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip().upper().lstrip("#*- ").strip()
        if stripped.startswith("VERDICT"):
            after = stripped[len("VERDICT"):].lstrip(": ").strip()
            if after == "VERIFIED":
                return "VERIFIED"
            if after == "LIKELY_WRONG":
                return "LIKELY_WRONG"
            return "UNCERTAIN"
    return "UNCERTAIN"
