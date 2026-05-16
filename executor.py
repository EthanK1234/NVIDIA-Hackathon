"""Executor: runs code+tests in an isolated tempdir via subprocess.

NOT an LLM. Just a tool the controller calls. Keep it boring and safe.

SAFETY NOTE: This runs LLM-generated Python with the same privileges as the
caller. For production use, run inside a container, a firejail/sandbox, or a
disposable VM. Network is NOT blocked here. Timeout protects against infinite
loops but not against intentional misbehavior.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict


def run_tests(code: str, tests: str, timeout: int = 30) -> Dict:
    with tempfile.TemporaryDirectory(prefix="agent_exec_") as tmp:
        tmpdir = Path(tmp)
        (tmpdir / "solution.py").write_text(code)
        (tmpdir / "test_solution.py").write_text(tests)

        try:
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "test_solution.py",
                 "-v", "--tb=short", "--no-header"],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            stdout, stderr, rc = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired as e:
            stdout = e.stdout or ""
            stderr = (e.stderr or "") + \
                     f"\n\n[timeout] Tests exceeded {timeout}s. Likely an infinite loop."
            rc = -1

            
    summary = stdout[-2500:]
    if stderr.strip():
        summary += "\n--- stderr ---\n" + stderr[-1500:]

    return {
        "passed": rc == 0,
        "returncode": rc,
        "stdout": stdout,
        "stderr": stderr,
        "summary": summary,
    }