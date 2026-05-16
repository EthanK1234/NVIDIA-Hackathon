from __future__ import annotations

from typing import Optional

from .base import call_claude, extract_code, GENERATOR_MODEL

SYSTEM = """You are a senior Python engineer. Write clean, correct, well-documented Python that solves the given task.

Rules:
- Produce a single self-contained Python module.
- Include type hints on all public functions.
- Use clear, descriptive names. No one-letter variables except loop indices.
- Handle edge cases explicitly (empty inputs, None, negative numbers, boundary values).
- No external dependencies unless the task requires them. Standard library only.
- The module must be importable as `solution` and expose the function(s) named in the task.
- If a test suite is provided, implement exactly the function signatures it imports.
- Output ONE fenced ```python``` block containing the full module. No commentary outside the block."""

def generate(
    task: str,
    feedback: Optional[str] = None,
    tests: Optional[str] = None,
) -> str:
    """Write code for `task`.

    Args:
        feedback: If supplied, revise the previous attempt using this feedback.
        tests: If supplied, include the test file as an interface contract so
               the generator knows exactly which functions to expose.
    """
    test_section = (
        f"\n\n# Test suite (interface contract — implement exactly these signatures)\n"
        f"```python\n{tests}\n```"
        if tests else ""
    )

    if feedback:
        user = (
            f"# Task\n{task}{test_section}\n\n"
            "# Previous attempt had problems. Read the feedback below and produce a corrected version.\n\n"
            f"{feedback}\n\n"
            "Output the full corrected module in a single ```python``` block."
        )
    else:
        user = (
            f"# Task\n{task}{test_section}\n\n"
            "Output the full solution as a single ```python``` block."
        )

    raw = call_claude(SYSTEM, user, model=GENERATOR_MODEL, use_thinking=True)
    return extract_code(raw, language="python")
