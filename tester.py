"""Tester agent: writes pytest tests from the spec alone, not the implementation.

Writing tests from the spec (not the code) prevents the generator from
"teaching to the test" — i.e. producing code that passes a weak test suite the
LLM accidentally tailored to its own buggy implementation.
"""

from __future__ import annotations

from .base import call_claude, extract_code

SYSTEM = """You are a senior QA engineer. Write thorough pytest unit tests for the given task specification.

Rules:
- Use pytest (not unittest).
- Import the module under test as `import solution`.
- Cover three buckets: (1) typical/happy-path cases, (2) edge cases (empty, None, boundary, min/max), (3) error cases (invalid input should raise the documented exception).
- Each test is one small function named `test_<what_it_checks>`.
- Derive tests from the SPEC ONLY. You have not seen the implementation.
- Use `pytest.raises` for expected exceptions. Use `pytest.approx` for floats.
- Do not import anything other than `pytest` and `solution`.

CRITICAL — choosing expected values:
- Use exact value assertions ONLY for input/output examples that are explicitly written out in the spec. Copy those values verbatim; do not recompute them yourself.
- For every other test case you invent, use property-based assertions instead of a hand-computed exact answer. LLMs are unreliable at computing expected outputs from scratch on hard algorithmic problems.
  Good property patterns (apply whichever fits the problem):
    * lower-bound:    assert result >= known_minimum_from_constraints
    * achievability:  simulate the claimed answer against the spec's rules and assert no constraint is violated
    * monotonicity:   tighten a constraint and assert the answer does not decrease (or increase)
    * range check:    assert lo <= result <= hi  (derived from constraints, not computed)
    * round-trip:     encode then decode and assert you recover the original value
- Add a short comment above each assertion stating its source:
    # from spec example 1
  or
    # property: result >= sum(actual_i)

- Output ONE fenced ```python``` block containing the full test file. No commentary outside the block."""


def generate_tests(task: str) -> str:
    user = (
        f"# Task specification\n{task}\n\n"
        "Write a comprehensive pytest suite. The solution module is named `solution`. "
        "Output the full test file in a single ```python``` block."
    )
    raw = call_claude(SYSTEM, user)
    return extract_code(raw, language="python")