"""Haiku writes the searchable layer: a summary plus one entry per topic, for sessions and Slack conversations.

Runs through `claude -p` so it bills to the Claude plan, not an API key.
"""

import json
import subprocess
import tempfile

from brain.passages import turns

VERSION = 2

SYSTEM = """You index work transcripts for later keyword search.
The input is one Claude Code session as numbered turns ([n] USER: / [n] CLAUDE:). Return:
- title: under 10 words, the session's main thread, specific (systems, tickets, repos named)
- summary: 2-4 sentences: what was attempted, what was found, how it ended
- topics: one entry per distinct problem, fix, investigation or decision in the session, including side
  quests that take only a few turns. A single-thread session has one topic. Each topic has:
  - title: under 10 words, specific to that topic, not the session
  - summary: 1-3 sentences: the problem, the cause or decision, the fix or outcome
  - questions: 2-4 questions someone would type later to find this topic, using the words they would actually search
  - atoms: exact, reusable facts copied verbatim from those turns: error messages, working commands, fixes, config values, decisions with their reason, ticket keys, PR numbers. Prefix each with ERROR:, FIX:, CMD:, DECISION:, or REF:. Omit anything uncertain.
  - turn_from, turn_to: the first and last turn numbers the topic covers
Never include secrets or [REDACTED] values. Return only the JSON object."""

SLACK_SYSTEM = (
    SYSTEM.replace(
        "The input is one Claude Code session as numbered turns ([n] USER: / [n] CLAUDE:).",
        "The input is one Slack thread, or one day of a Slack channel or DM, as numbered messages ([n] [Name date] text).",
    )
    .replace("the session's main thread", "the conversation's main subject")
    .replace("in the session, including side", "in the conversation, including side")
    .replace("A single-thread session has one topic", "A single-subject conversation has one topic")
    .replace("specific to that topic, not the session", "specific to that topic, not the conversation")
    .replace("copied verbatim from those turns", "copied verbatim from those messages")
    .replace("turn numbers the topic covers", "message numbers the topic covers")
)
PROMPTS = {"session": SYSTEM, "slack": SLACK_SYSTEM}

_TOPIC = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "questions": {"type": "array", "items": {"type": "string"}},
        "atoms": {"type": "array", "items": {"type": "string"}},
        "turn_from": {"type": "integer"},
        "turn_to": {"type": "integer"},
    },
    "required": ["title", "summary", "questions", "atoms", "turn_from", "turn_to"],
}

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "topics": {"type": "array", "items": _TOPIC},
    },
    "required": ["title", "summary", "topics"],
}

# An empty cwd keeps project CLAUDE.md files out; `project` sources skip user settings, hooks and plugins.
_CWD = tempfile.mkdtemp(prefix="brain-haiku-")


def number(body: str) -> str:
    return "\n\n".join(f"[{n}] {turn}" for n, turn in enumerate(turns(body), 1))


def clamp(topics: list[dict], last: int) -> list[dict]:
    """Haiku's turn ranges are hints; keep each inside the transcript and ordered."""
    for t in topics:
        t["turn_from"] = min(max(1, t["turn_from"]), last)
        t["turn_to"] = min(max(t["turn_from"], t["turn_to"]), last)
    return topics


def summarize(body: str, prompt: str = "session") -> dict:
    proc = subprocess.run(
        [
            "claude", "-p",
            "--model", "haiku",
            "--no-session-persistence",
            "--tools", "",
            "--strict-mcp-config",
            "--setting-sources", "project",
            "--system-prompt", PROMPTS[prompt],
            "--json-schema", json.dumps(SCHEMA),
            "--output-format", "json",
        ],
        input=number(body),
        capture_output=True,
        text=True,
        cwd=_CWD,
        timeout=600,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"claude -p failed ({proc.returncode}): {proc.stderr.strip()[:500] or proc.stdout[:500]}")
    out = json.loads(proc.stdout)
    if out.get("is_error"):
        raise RuntimeError(f"claude -p error: {out.get('result', '')[:500]}")
    result = out.get("structured_output") or json.loads(out["result"])
    clamp(result["topics"], len(turns(body)))
    result["_usage"] = {"cost_usd": out.get("total_cost_usd"), "ms": out.get("duration_ms")}
    return result
