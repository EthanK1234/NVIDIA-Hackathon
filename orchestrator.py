"""
ClawReviewer Orchestrator — Map-Plan-Execute-Verify state loop.

State machine:
  MAP    -> Scout scans repo, Planner builds todo.json
  PLAN   -> Planner decomposes tasks (one file per task)
  EXECUTE-> Coder applies Search/Replace diffs per task
  VERIFY -> tester.py validates; Critic self-heals on failure
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import openai

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CONFIG_PATH = Path(__file__).parent.parent / ".openclaw" / "openclaw.json"
LOG_PATH = Path(__file__).parent.parent / "openclaw_run.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("orchestrator")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


CONFIG = load_config()
REPO_PATH = Path(os.environ.get("OPENCLAW_REPO_PATH", ".")).resolve()
TODO_PATH = REPO_PATH / CONFIG["pipeline"]["todo_file"]
BRANCH = CONFIG["pipeline"]["branch_name"]
MAX_RETRIES = CONFIG["pipeline"]["max_self_heal_retries"]
GOOGLE_API_KEY = os.environ["GOOGLE_AI_STUDIO_API_KEY"]


def audit(agent: str, action: str) -> None:
    ts = datetime.now(timezone.utc).isoformat()
    log.info("[%s] agent=%s | %s", ts, agent, action)


# ---------------------------------------------------------------------------
# OpenAI-compatible client targeting Google AI Studio v1beta
# ---------------------------------------------------------------------------

def make_client() -> openai.OpenAI:
    return openai.OpenAI(
        api_key=GOOGLE_API_KEY,
        base_url=CONFIG["model_routing"]["base_url"],
    )


def chat(agent_name: str, messages: list[dict], client: openai.OpenAI) -> str:
    agent_cfg = CONFIG["agents"][agent_name]
    response = client.chat.completions.create(
        model=agent_cfg["model"],
        messages=messages,
        temperature=agent_cfg["temperature"],
        max_tokens=agent_cfg["max_tokens"],
    )
    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Branch safety gate (Directive 1)
# ---------------------------------------------------------------------------

def ensure_patch_branch() -> None:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=REPO_PATH, capture_output=True, text=True, check=True,
    )
    current = result.stdout.strip()
    if current == BRANCH:
        audit("orchestrator", f"Already on branch '{BRANCH}'")
        return

    subprocess.run(
        ["git", "checkout", "-B", BRANCH],
        cwd=REPO_PATH, check=True,
    )
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=REPO_PATH, capture_output=True, text=True, check=True,
    )
    if result.stdout.strip() != BRANCH:
        raise RuntimeError(f"Branch checkout failed — aborting pipeline.")
    audit("orchestrator", f"Checked out branch '{BRANCH}'")


# ---------------------------------------------------------------------------
# MAP phase — Scout surveys the repo
# ---------------------------------------------------------------------------

def map_phase(client: openai.OpenAI) -> str:
    """Scout reads the repo tree and returns a raw description."""
    tree_lines = []
    for p in sorted(REPO_PATH.rglob("*")):
        if any(part.startswith(".git") for part in p.parts):
            continue
        rel = p.relative_to(REPO_PATH)
        tree_lines.append(str(rel))

    repo_tree = "\n".join(tree_lines[:500])  # cap to avoid token overflow
    audit("scout", f"Scanned {len(tree_lines)} paths")

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Scout agent. Analyze the repository file tree "
                "and identify files that are candidates for optimization "
                "(performance, readability, type safety, test coverage, "
                "dead code removal). Return a JSON array of objects with keys "
                "'file_path' (relative), 'reason' (one sentence), and "
                "'priority' (1=high, 2=medium, 3=low). Output only valid JSON."
            ),
        },
        {
            "role": "user",
            "content": f"Repository tree:\n{repo_tree}",
        },
    ]
    raw = chat("scout", messages, client)
    audit("scout", "Repo scan complete")
    return raw


# ---------------------------------------------------------------------------
# PLAN phase — Planner builds todo.json
# ---------------------------------------------------------------------------

TODO_SCHEMA = {
    "tasks": [
        {
            "id": "<uuid>",
            "file_path": "<relative path>",
            "task": "<one-sentence description>",
            "priority": 1,
            "status": "pending",
            "attempts": 0,
            "error_log": None,
            "patch": None,
        }
    ]
}


def plan_phase(scout_output: str, client: openai.OpenAI) -> dict:
    """Planner converts Scout output into a structured todo.json."""
    import uuid

    messages = [
        {
            "role": "system",
            "content": (
                "You are the Planner agent. Convert the Scout's file analysis "
                "into an ordered task list. Each task must target exactly ONE "
                "file. Return a JSON object matching this schema exactly:\n"
                + json.dumps(TODO_SCHEMA, indent=2)
                + "\n\nRules:\n"
                "- Generate a unique UUID v4 for each task id.\n"
                "- status must be 'pending'.\n"
                "- attempts must be 0.\n"
                "- error_log and patch must be null.\n"
                "- Output only valid JSON, no prose."
            ),
        },
        {
            "role": "user",
            "content": f"Scout analysis:\n{scout_output}",
        },
    ]
    raw = chat("planner", messages, client)

    # Extract JSON even if wrapped in markdown fences
    json_match = re.search(r"\{[\s\S]*\}", raw)
    if not json_match:
        raise ValueError(f"Planner returned non-JSON output:\n{raw}")
    todo = json.loads(json_match.group())

    # Back-fill missing UUIDs if model forgot
    for task in todo.get("tasks", []):
        if not task.get("id"):
            task["id"] = str(uuid.uuid4())
        task.setdefault("status", "pending")
        task.setdefault("attempts", 0)
        task.setdefault("error_log", None)
        task.setdefault("patch", None)

    with open(TODO_PATH, "w", encoding="utf-8") as f:
        json.dump(todo, f, indent=2)

    audit("planner", f"todo.json written with {len(todo['tasks'])} tasks")
    return todo


def load_todo() -> dict:
    with open(TODO_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_todo(todo: dict) -> None:
    with open(TODO_PATH, "w", encoding="utf-8") as f:
        json.dump(todo, f, indent=2)


# ---------------------------------------------------------------------------
# Search/Replace diff parser and applier (Directive 4)
# ---------------------------------------------------------------------------

DIFF_PATTERN = re.compile(
    r"<<<<<<< SEARCH\n(.*?)\n=======\n(.*?)\n>>>>>>> REPLACE",
    re.DOTALL,
)


def apply_diff(file_path: Path, diff_text: str) -> int:
    """Apply all Search/Replace blocks to file_path. Returns number of hunks applied."""
    blocks = DIFF_PATTERN.findall(diff_text)
    if not blocks:
        raise ValueError("No valid Search/Replace diff blocks found in Coder output.")

    content = file_path.read_text(encoding="utf-8")
    applied = 0
    for search, replace in blocks:
        if search not in content:
            raise ValueError(
                f"SEARCH block not found in {file_path}.\n"
                f"Expected:\n{search!r}"
            )
        content = content.replace(search, replace, 1)
        applied += 1

    file_path.write_text(content, encoding="utf-8")
    return applied


# ---------------------------------------------------------------------------
# EXECUTE phase — Coder applies patches
# ---------------------------------------------------------------------------

CODER_SYSTEM = """\
You are the Coder agent. You will receive the full contents of a single source \
file and a one-sentence optimization task. Produce ONLY Search/Replace diff \
blocks in this exact format — no prose, no explanation, no other text:

<<<<<<< SEARCH
[exact original code]
=======
[replacement code]
>>>>>>> REPLACE

Rules:
- The SEARCH block must match the file content character-for-character.
- You may output multiple diff blocks for a single file.
- Never include API keys, passwords, or secrets.
- Output nothing outside the diff blocks.\
"""


def execute_task(task: dict, client: openai.OpenAI) -> str:
    """Ask Coder to patch a single file. Returns the raw diff string."""
    file_path = REPO_PATH / task["file_path"]
    if not file_path.exists():
        raise FileNotFoundError(f"Target file not found: {file_path}")

    source = file_path.read_text(encoding="utf-8")
    messages = [
        {"role": "system", "content": CODER_SYSTEM},
        {
            "role": "user",
            "content": (
                f"File: {task['file_path']}\n"
                f"Task: {task['task']}\n\n"
                f"File contents:\n```\n{source}\n```"
            ),
        },
    ]
    diff = chat("coder", messages, client)
    audit("coder", f"Patch generated for {task['file_path']}")
    return diff


# ---------------------------------------------------------------------------
# Critic agent — self-healing on test failure
# ---------------------------------------------------------------------------

CRITIC_SYSTEM = """\
You are the Critic agent. A code change was applied but the test suite failed. \
Analyze the stack trace and the current file contents, then produce corrected \
Search/Replace diff blocks to fix the failure. Output ONLY diff blocks — no \
prose. Format:

<<<<<<< SEARCH
[exact current code to replace]
=======
[corrected code]
>>>>>>> REPLACE\
"""


def critic_heal(task: dict, error_log: str, client: openai.OpenAI) -> str:
    file_path = REPO_PATH / task["file_path"]
    source = file_path.read_text(encoding="utf-8") if file_path.exists() else ""
    messages = [
        {"role": "system", "content": CRITIC_SYSTEM},
        {
            "role": "user",
            "content": (
                f"File: {task['file_path']}\n"
                f"Original task: {task['task']}\n\n"
                f"Current file contents:\n```\n{source}\n```\n\n"
                f"Test failure:\n```\n{error_log}\n```"
            ),
        },
    ]
    diff = chat("critic", messages, client)
    audit("critic", f"Self-heal diff generated for {task['file_path']}")
    return diff


# ---------------------------------------------------------------------------
# VERIFY phase — run tests and loop with self-healing
# ---------------------------------------------------------------------------

def verify_phase(todo: dict, client: openai.OpenAI) -> bool:
    """Run tests. On failure, route to Critic and retry. Returns True if clean."""
    tester = Path(__file__).parent / "tester.py"

    for attempt in range(1, MAX_RETRIES + 2):  # +1 for initial attempt
        result = subprocess.run(
            [sys.executable, str(tester)],
            env={**os.environ, "OPENCLAW_REPO_PATH": str(REPO_PATH)},
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            audit("orchestrator", f"Tests passed on attempt {attempt}")
            return True

        error_log = (result.stdout + "\n" + result.stderr).strip()
        audit("orchestrator", f"Tests FAILED (attempt {attempt}): {error_log[:200]}")

        if attempt > MAX_RETRIES:
            # Mark all in-progress tasks as failed
            for task in todo["tasks"]:
                if task["status"] == "in-progress":
                    task["status"] = "failed"
                    task["error_log"] = error_log
            save_todo(todo)
            log.error("Max self-heal retries exhausted. Pipeline halted.")
            return False

        # Route to Critic for each in-progress task
        for task in todo["tasks"]:
            if task["status"] == "in-progress":
                task["attempts"] = task.get("attempts", 0) + 1
                task["error_log"] = error_log
                heal_diff = critic_heal(task, error_log, client)
                try:
                    file_path = REPO_PATH / task["file_path"]
                    applied = apply_diff(file_path, heal_diff)
                    audit("critic", f"Applied {applied} heal hunk(s) to {task['file_path']}")
                except ValueError as e:
                    log.warning("Critic diff could not be applied: %s", e)
        save_todo(todo)

    return False


# ---------------------------------------------------------------------------
# Main pipeline loop
# ---------------------------------------------------------------------------

def run_pipeline() -> None:
    client = make_client()

    # --- Branch safety gate ---
    ensure_patch_branch()

    # --- MAP ---
    audit("orchestrator", "=== PHASE: MAP ===")
    scout_output = map_phase(client)

    # --- PLAN ---
    audit("orchestrator", "=== PHASE: PLAN ===")
    todo = plan_phase(scout_output, client)

    # --- EXECUTE + VERIFY loop ---
    audit("orchestrator", "=== PHASE: EXECUTE ===")
    pending = [t for t in todo["tasks"] if t["status"] == "pending"]

    for task in pending:
        log.info("Processing task [%s]: %s", task["id"], task["task"])
        task["status"] = "in-progress"
        save_todo(todo)

        try:
            diff = execute_task(task, client)
            file_path = REPO_PATH / task["file_path"]
            hunks = apply_diff(file_path, diff)
            task["patch"] = diff
            audit("coder", f"Applied {hunks} hunk(s) to {task['file_path']}")
        except (ValueError, FileNotFoundError) as e:
            task["status"] = "failed"
            task["error_log"] = str(e)
            save_todo(todo)
            log.warning("Skipping task [%s] — patch error: %s", task["id"], e)
            continue

        # --- VERIFY after each file patch ---
        audit("orchestrator", "=== PHASE: VERIFY ===")
        clean = verify_phase(todo, client)

        if clean:
            task["status"] = "completed"
            # Commit after each verified file change
            subprocess.run(
                ["git", "add", task["file_path"]],
                cwd=REPO_PATH, check=True,
            )
            subprocess.run(
                ["git", "commit", "-m",
                 f"openclaw: {task['task'][:72]}"],
                cwd=REPO_PATH,
                env={
                    **os.environ,
                    "GIT_AUTHOR_NAME": CONFIG["pipeline"]["git_author_name"],
                    "GIT_AUTHOR_EMAIL": CONFIG["pipeline"]["git_author_email"],
                    "GIT_COMMITTER_NAME": CONFIG["pipeline"]["git_author_name"],
                    "GIT_COMMITTER_EMAIL": CONFIG["pipeline"]["git_author_email"],
                },
                check=True,
            )
            audit("orchestrator", f"Committed changes for task [{task['id']}]")
        else:
            task["status"] = "failed"
            log.error("Task [%s] could not be verified — skipping commit.", task["id"])

        save_todo(todo)

    completed = sum(1 for t in todo["tasks"] if t["status"] == "completed")
    failed = sum(1 for t in todo["tasks"] if t["status"] == "failed")
    audit("orchestrator", f"Pipeline complete. completed={completed} failed={failed}")

    if completed > 0:
        log.info("Handing off to github_delivery.py...")
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "github_delivery.py")],
            env={**os.environ, "OPENCLAW_REPO_PATH": str(REPO_PATH)},
            check=True,
        )


if __name__ == "__main__":
    run_pipeline()
