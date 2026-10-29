"""Claude Code session transcripts -> condensed, redacted text records."""

import json
import re
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from brain import summarize
from brain.connectors import Context, Event, Failed, Record
from brain.redact import redact

PROJECTS = Path.home() / ".claude" / "projects"
IDLE_SECONDS = 30 * 60
SEGMENT_CHARS = 300_000  # keeps each summarizer call well inside Haiku's context
MAX_USER_CHARS = 4_000  # pasted logs and `!` output get truncated, not dropped
MAX_STDOUT_CHARS = 1_000

MIN_BODY_CHARS = 300  # below this a session is a bare slash command, not work

_REMINDER = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_CAVEAT = re.compile(r"<local-command-caveat>.*?</local-command-caveat>", re.DOTALL)
_BARE_COMMAND = re.compile(
    r"^\s*<command-name>[^<]*</command-name>\s*<command-message>[^<]*</command-message>\s*<command-args>\s*</command-args>\s*$"
)
_STDOUT = re.compile(r"<bash-stdout>(.*?)</bash-stdout>", re.DOTALL)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…[{len(text) - limit} chars truncated]"


def _user_text(content) -> str:
    if isinstance(content, str):
        text = content
    else:
        text = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")
    text = _CAVEAT.sub("", _REMINDER.sub("", text))
    if _BARE_COMMAND.match(text):
        return ""
    text = _STDOUT.sub(lambda m: "<bash-stdout>" + _clip(m.group(1), MAX_STDOUT_CHARS) + "</bash-stdout>", text)
    return _clip(text.strip(), MAX_USER_CHARS)


def condense(path: Path) -> tuple[str, str | None, str | None, datetime | None]:
    """Return (body, title, project, last_ts) keeping only prompts and Claude's prose."""
    lines, title, project, last_ts = [], None, None, None
    with path.open() as f:
        for raw in f:
            try:
                entry = json.loads(raw)
            except json.JSONDecodeError:
                continue
            kind = entry.get("type")
            if kind == "custom-title":
                title = entry.get("customTitle") or title
                continue
            if kind not in ("user", "assistant") or entry.get("isSidechain") or entry.get("isMeta"):
                continue
            project = project or entry.get("cwd")
            if entry.get("timestamp"):
                last_ts = datetime.fromisoformat(entry["timestamp"])
            content = (entry.get("message") or {}).get("content")
            if content is None:
                continue
            if kind == "user":
                text = _user_text(content)
                if text:
                    lines.append(f"USER: {text}")
            elif isinstance(content, list):
                text = "\n".join(p.get("text", "") for p in content if p.get("type") == "text").strip()
                if text:
                    lines.append(f"CLAUDE: {text}")
    return redact("\n\n".join(lines)), title, project, last_ts


def discover(projects: Path = PROJECTS, now: float | None = None) -> Iterator[Path]:
    """Main-session transcripts idle long enough to be finished, newest first so recent work lands first.

    Subagent files live one level deeper. Resuming a session in another directory copies its transcript
    there under the same id; only the newest copy is complete, so older copies are skipped.
    """
    now = now or time.time()
    seen = set()
    for path in sorted(projects.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if path.stem in seen:
            continue
        seen.add(path.stem)
        if now - path.stat().st_mtime >= IDLE_SECONDS:
            yield path


def load(path: Path) -> list[Record]:
    body, title, project, ts = condense(path)
    if ts is None or "USER:" not in body or len(body) < MIN_BODY_CHARS:
        return []
    mtime = path.stat().st_mtime
    chunks = [body[i : i + SEGMENT_CHARS] for i in range(0, len(body), SEGMENT_CHARS)]
    sid = path.stem
    return [
        Record(
            ref=f"session:{sid}" + (f"#{n}" if len(chunks) > 1 else ""),
            ts=ts,
            project=project,
            title=title,
            summary=None,
            body=chunk,
            mtime=mtime,
            summarize="session",
            events=[Event(ts, "me", "session")],  # sessions are always the user's own; "me" is the [people] key
        )
        for n, chunk in enumerate(chunks, 1)
    ]


def fetch(ctx: Context) -> Iterator[Record | Failed]:
    # A transcript whose mtime matches its indexed copy has not changed; skip it without condensing it.
    mtimes = dict(ctx.conn.execute(
        """select split_part(ref, '#', 1), max(source_mtime) from records
           where source = 'sessions' and summarizer_version = %s group by 1""",
        (summarize.VERSION,),
    ).fetchall())
    for path in discover(Path(ctx.cfg.get("projects_dir", PROJECTS)).expanduser()):
        if not ctx.full and mtimes.get(f"session:{path.stem}") == path.stat().st_mtime:
            continue
        try:
            loaded = load(path)
        except Exception as e:  # noqa: BLE001 -- one bad transcript must not stop the run
            yield Failed(str(path), str(e))
            continue
        yield from loaded
