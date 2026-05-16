"""
ClawReviewer Tester — subprocess wrapper for the repo's local test runner.

Detects the testing framework dynamically, runs it, captures output, and
writes failure state back to todo.json for the Critic agent.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

LOG_PATH = Path(__file__).parent.parent / "openclaw_run.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("tester")

CONFIG_PATH = Path(__file__).parent.parent / ".openclaw" / "openclaw.json"
with open(CONFIG_PATH, encoding="utf-8") as _f:
    _CONFIG = json.load(_f)

REPO_PATH = Path(os.environ.get("OPENCLAW_REPO_PATH", ".")).resolve()
TODO_PATH = REPO_PATH / _CONFIG["pipeline"]["todo_file"]
TIMEOUT = _CONFIG["pipeline"]["test_timeout_seconds"]


# ---------------------------------------------------------------------------
# Framework detection
# ---------------------------------------------------------------------------

def detect_test_command(repo: Path) -> list[str]:
    """
    Return the best test command for this repo by inspecting its structure.
    Priority order: pytest > unittest discovery > jest > npm test > yarn test.
    """
    # Python: prefer pytest, fall back to unittest
    if (repo / "pytest.ini").exists() or (repo / "pyproject.toml").exists() \
            or (repo / "setup.cfg").exists() or list(repo.glob("**/test_*.py")):
        return [sys.executable, "-m", "pytest", "--tb=short", "-q"]

    if list(repo.glob("**/test*.py")):
        return [sys.executable, "-m", "unittest", "discover", "-s", "."]

    # Node/JS
    package_json = repo / "package.json"
    if package_json.exists():
        pkg = json.loads(package_json.read_text(encoding="utf-8"))
        scripts = pkg.get("scripts", {})

        yarn_lock = (repo / "yarn.lock").exists()
        runner = "yarn" if yarn_lock else "npm"

        if "test" in scripts:
            return [runner, "test", "--", "--ci", "--passWithNoTests"] \
                if "jest" in scripts.get("test", "") \
                else [runner, "test"]

    # Makefile fallback
    if (repo / "Makefile").exists():
        return ["make", "test"]

    log.warning("No test runner detected — defaulting to Python unittest discover")
    return [sys.executable, "-m", "unittest", "discover", "-s", "."]


# ---------------------------------------------------------------------------
# Main test runner
# ---------------------------------------------------------------------------

def run_tests() -> int:
    cmd = detect_test_command(REPO_PATH)
    log.info("Running tests: %s in %s", " ".join(cmd), REPO_PATH)

    result = subprocess.run(
        cmd,
        cwd=REPO_PATH,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        env={**os.environ, "PYTHONPATH": str(REPO_PATH)},
    )

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    exit_code = result.returncode

    log.info("Test exit code: %d", exit_code)
    if stdout:
        log.info("STDOUT:\n%s", stdout)
    if stderr:
        log.info("STDERR:\n%s", stderr)

    if exit_code != 0:
        _write_failure_to_todo(stdout, stderr)

    return exit_code


def _write_failure_to_todo(stdout: str, stderr: str) -> None:
    """Append error log to all in-progress tasks so Critic can read it."""
    if not TODO_PATH.exists():
        log.warning("todo.json not found — cannot write failure state")
        return

    with open(TODO_PATH, encoding="utf-8") as f:
        todo = json.load(f)

    error_log = f"=== STDOUT ===\n{stdout}\n\n=== STDERR ===\n{stderr}"
    updated = False
    for task in todo.get("tasks", []):
        if task.get("status") == "in-progress":
            task["status"] = "failed"
            task["error_log"] = error_log
            updated = True

    if updated:
        with open(TODO_PATH, "w", encoding="utf-8") as f:
            json.dump(todo, f, indent=2)
        log.info("Failure state written to todo.json")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        exit_code = run_tests()
    except subprocess.TimeoutExpired:
        log.error("Test runner timed out after %d seconds", TIMEOUT)
        _write_failure_to_todo("", f"TimeoutExpired after {TIMEOUT}s")
        exit_code = 1
    except Exception as exc:  # noqa: BLE001
        log.error("Unexpected tester error: %s", exc)
        _write_failure_to_todo("", str(exc))
        exit_code = 1

    sys.exit(exit_code)
