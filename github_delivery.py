"""
ClawReviewer GitHub Delivery — stages, pushes, and opens a Pull Request.

Flow:
  1. Confirm we are on the patch branch.
  2. Push branch to remote origin.
  3. Use Reviewer (Gemini Pro) to generate a PR description from the commit log.
  4. Create the Pull Request via the GitHub REST API.
"""

import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import openai
import requests

LOG_PATH = Path(__file__).parent.parent / "openclaw_run.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_PATH, encoding="utf-8"),
    ],
)
log = logging.getLogger("github_delivery")

CONFIG_PATH = Path(__file__).parent.parent / ".openclaw" / "openclaw.json"
with open(CONFIG_PATH, encoding="utf-8") as _f:
    _CONFIG = json.load(_f)

REPO_PATH = Path(os.environ.get("OPENCLAW_REPO_PATH", ".")).resolve()
TODO_PATH = REPO_PATH / _CONFIG["pipeline"]["todo_file"]
BRANCH = _CONFIG["pipeline"]["branch_name"]
GIT_NAME = _CONFIG["pipeline"]["git_author_name"]
GIT_EMAIL = _CONFIG["pipeline"]["git_author_email"]
GOOGLE_API_KEY = os.environ["GOOGLE_AI_STUDIO_API_KEY"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]


def audit(agent: str, action: str) -> None:
    log.info("[github_delivery] agent=%s | %s", agent, action)


# ---------------------------------------------------------------------------
# OpenAI-compatible client
# ---------------------------------------------------------------------------

def make_client() -> openai.OpenAI:
    return openai.OpenAI(
        api_key=GOOGLE_API_KEY,
        base_url=_CONFIG["model_routing"]["base_url"],
    )


def chat(agent_name: str, messages: list[dict], client: openai.OpenAI) -> str:
    agent_cfg = _CONFIG["agents"][agent_name]
    response = client.chat.completions.create(
        model=agent_cfg["model"],
        messages=messages,
        temperature=agent_cfg["temperature"],
        max_tokens=agent_cfg["max_tokens"],
    )
    return response.choices[0].message.content


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": GIT_NAME,
        "GIT_AUTHOR_EMAIL": GIT_EMAIL,
        "GIT_COMMITTER_NAME": GIT_NAME,
        "GIT_COMMITTER_EMAIL": GIT_EMAIL,
    }
    return subprocess.run(
        ["git", *args],
        cwd=REPO_PATH,
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )


def get_default_branch() -> str:
    result = git("remote", "show", "origin")
    for line in result.stdout.splitlines():
        if "HEAD branch" in line:
            return line.split(":")[-1].strip()
    return "main"


def get_remote_url() -> str:
    result = git("remote", "get-url", "origin")
    return result.stdout.strip()


def parse_github_owner_repo(remote_url: str) -> tuple[str, str]:
    """Parse owner/repo from HTTPS or SSH remote URL."""
    url = remote_url.rstrip(".git")
    if url.startswith("git@"):
        # git@github.com:owner/repo
        path = url.split(":", 1)[1]
    elif "github.com/" in url:
        path = url.split("github.com/", 1)[1]
    else:
        raise ValueError(f"Cannot parse GitHub owner/repo from remote: {remote_url}")
    parts = path.strip("/").split("/")
    if len(parts) < 2:
        raise ValueError(f"Unexpected path structure: {path}")
    return parts[0], parts[1]


def get_commit_log_since_branch_point(base_branch: str) -> str:
    result = git(
        "log", f"origin/{base_branch}..HEAD",
        "--pretty=format:- %s (%h)",
        "--no-merges",
    )
    return result.stdout.strip()


# ---------------------------------------------------------------------------
# PR description generation (Reviewer agent)
# ---------------------------------------------------------------------------

REVIEWER_SYSTEM = """\
You are the Reviewer agent for ClawReviewer, an autonomous repository \
optimization pipeline. Write a clean, professional GitHub Pull Request \
description in Markdown. Structure it as:

## Summary
One paragraph describing what was optimized and why.

## Changes
Bullet list of specific changes made.

## Testing
One sentence confirming the test suite passed with exit code 0.

## Notes
Any caveats, limitations, or follow-up recommendations.

Be concise and factual. Do not mention AI or automation tools by name.\
"""


def generate_pr_description(
    commit_log: str,
    completed_tasks: list[dict],
    client: openai.OpenAI,
) -> str:
    tasks_summary = "\n".join(
        f"- [{t['file_path']}] {t['task']}"
        for t in completed_tasks
    )
    messages = [
        {"role": "system", "content": REVIEWER_SYSTEM},
        {
            "role": "user",
            "content": (
                f"Commit log:\n{commit_log}\n\n"
                f"Completed optimization tasks:\n{tasks_summary}"
            ),
        },
    ]
    description = chat("reviewer", messages, client)
    audit("reviewer", "PR description generated")
    return description


# ---------------------------------------------------------------------------
# GitHub API — create PR
# ---------------------------------------------------------------------------

def create_pull_request(
    owner: str,
    repo: str,
    title: str,
    body: str,
    head: str,
    base: str,
) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls"
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload = {
        "title": title,
        "body": body,
        "head": head,
        "base": base,
        "draft": False,
    }
    response = requests.post(url, headers=headers, json=payload, timeout=30)
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Main delivery flow
# ---------------------------------------------------------------------------

def deliver() -> None:
    client = make_client()

    # Confirm branch
    result = git("branch", "--show-current")
    current_branch = result.stdout.strip()
    if current_branch != BRANCH:
        raise RuntimeError(
            f"Expected branch '{BRANCH}', found '{current_branch}'. Aborting."
        )

    # Load completed tasks
    with open(TODO_PATH, encoding="utf-8") as f:
        todo = json.load(f)
    completed = [t for t in todo["tasks"] if t["status"] == "completed"]
    if not completed:
        log.info("No completed tasks — nothing to deliver.")
        return

    # Push branch
    log.info("Pushing branch '%s' to origin...", BRANCH)
    push_result = git("push", "--set-upstream", "origin", BRANCH, check=False)
    if push_result.returncode != 0:
        # Force-push if branch already exists remotely
        push_result = git("push", "--force-with-lease", "origin", BRANCH)
    audit("github_delivery", f"Branch '{BRANCH}' pushed to origin")

    # Gather metadata
    base_branch = get_default_branch()
    remote_url = get_remote_url()
    owner, repo_name = parse_github_owner_repo(remote_url)
    commit_log = get_commit_log_since_branch_point(base_branch)

    # Generate PR description
    pr_body = generate_pr_description(commit_log, completed, client)

    pr_title = (
        f"[OpenClaw] Automated optimization — {len(completed)} file(s) improved"
    )

    # Create PR
    log.info("Creating Pull Request: %s", pr_title)
    pr = create_pull_request(
        owner=owner,
        repo=repo_name,
        title=pr_title,
        body=pr_body,
        head=BRANCH,
        base=base_branch,
    )

    pr_url = pr.get("html_url", "<unknown>")
    audit("github_delivery", f"Pull Request opened: {pr_url}")
    log.info("Pull Request created: %s", pr_url)

    # Write PR URL back to todo for audit trail
    todo["pull_request_url"] = pr_url
    with open(TODO_PATH, "w", encoding="utf-8") as f:
        json.dump(todo, f, indent=2)


if __name__ == "__main__":
    try:
        deliver()
    except Exception as exc:  # noqa: BLE001
        log.error("Delivery failed: %s", exc)
        sys.exit(1)
