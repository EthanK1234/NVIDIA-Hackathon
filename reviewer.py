"""Reviewer agent: critiques correctness, edge cases, and design.

Sees the code AND the executor's test output so it can comment on real
failures rather than guessing. Returns a structured verdict the controller
can branch on.
"""

from __future__ import annotations

from typing import Dict

from .base import call_claude, parse_verdict, GENERATOR_MODEL

SYSTEM = """You are a strict but fair code reviewer. Critique the submitted code with the rigor of a staff engineer doing a PR review.

Focus on, in order:
1. CORRECTNESS vs. the task spec — does it actually solve the problem?
2. EDGE CASES — what inputs would break this? (empty, None, negative, very large, unicode, etc.)
3. LOGIC BUGS — off-by-one, wrong operator, mutated default arg, swallowed exception.
4. DESIGN — bad complexity, hidden side effects, leaky abstractions, unsafe defaults.
5. INPUT VALIDATION — does it raise the right exception type for bad input, where the spec demands it?

Test results below tell you what actually fails. Trust them.

Output format (exact):
VERDICT: APPROVE
or
VERDICT: REQUEST_CHANGES

ISSUES:
- <one specific actionable issue per bullet; quote the line or behavior. Empty list if APPROVE.>

SUGGESTIONS:
- <optional improvements; nice-to-haves that don't block approval.>

Be precise. "Could use better error handling" is useless; "Line 12: catches `Exception` then `pass`, swallowing all errors silently" is useful.
APPROVE only when the code is correct AND tests pass AND no material edge case is missed."""


def review(task: str, code: str, test_output: str) -> Dict[str, str]:
    """Returns {'verdict': 'APPROVE'|'REQUEST_CHANGES', 'text': <full review>}."""
    user = (
        f"# Task\n{task}\n\n"
        f"# Submitted code\n```python\n{code}\n```\n\n"
        f"# Test execution result\n{test_output}\n\n"
        "Review the code now."
    )
    text = call_claude(SYSTEM, user, model=GENERATOR_MODEL)
    return {"verdict": parse_verdict(text), "text": text}