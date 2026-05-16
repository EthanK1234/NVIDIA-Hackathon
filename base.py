"""Shared utilities: the Anthropic client and a fenced-code extractor."""

from __future__ import annotations

import os
import re

from anthropic import Anthropic
from dotenv import load_dotenv
load_dotenv()
# Reads ANTHROPIC_API_KEY from the environment.
_client = Anthropic()

# Default model. Cheap+fast is good for the inner loop. Bump to opus for harder
# tasks (e.g. claude-opus-4-7). Override per-call via the `model=` arg.
DEFAULT_MODEL = "claude-opus-4-7"
GENERATOR_MODEL = "claude-opus-4-7"

def call_claude(
    system: str,
    user: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = 4096,
    use_thinking: bool = False,
) -> str:
    """Single-turn call. Returns the concatenated text from all text blocks.

    When `use_thinking=True`, adaptive extended thinking is enabled (claude-4+
    models only). Thinking blocks in the response are filtered out; only text
    blocks are returned.
    """
    kwargs: dict = dict(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if use_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
        kwargs["output_config"] = {"effort": "high"}
    response = _client.messages.create(**kwargs)
    parts = [block.text for block in response.content if block.type == "text"]
    return "\n".join(parts)


_FENCE_LANG = re.compile(r"```([a-zA-Z0-9_+-]*)\n(.*?)```", re.DOTALL)


def extract_code(text: str, language: str = "python") -> str:
    """Pull the largest fenced block whose tag matches `language`.

    Falls back to the largest fenced block of any language, then to the raw
    text if no fence exists. Picking the largest avoids grabbing a tiny inline
    example when the real answer is the big block below it.
    """
    candidates = _FENCE_LANG.findall(text)
    if not candidates:
        return text.strip()

    matching = [body for lang, body in candidates if lang.lower() == language.lower()]
    pool = matching if matching else [body for _, body in candidates]
    return max(pool, key=len).strip()


def parse_verdict(text: str) -> str:
    """Find the reviewer's VERDICT line. Defaults to REQUEST_CHANGES if absent.

    Requires the post-colon token to be exactly APPROVE. Partial matches like
    "APPROVE WITH MINOR CHANGES" or "MOSTLY APPROVE" return REQUEST_CHANGES.
    """
    for line in text.splitlines():
        stripped = line.strip().upper().lstrip("#*- ").strip()
        if stripped.startswith("VERDICT"):
            after = stripped[len("VERDICT"):].lstrip(": ").strip()
            if after == "APPROVE":
                return "APPROVE"
            return "REQUEST_CHANGES"
    return "REQUEST_CHANGES"