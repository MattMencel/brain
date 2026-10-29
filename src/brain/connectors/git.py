"""Git commits -> records. Commit messages already read as summaries, so no LLM call."""

import subprocess
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path

from brain.connectors import Context, Event, Failed, Record
from brain.redact import redact

SINCE = "12 months ago"
MAX_PATHS = 200

_FORMAT = "%x1e%H%x1f%aI%x1f%an%x1f%s%x1f%b%x1f"


def repos(root: Path) -> Iterator[Path]:
    for path in sorted(root.iterdir()):
        if (path / ".git").exists():
            yield path


def _branch(repo: Path) -> str:
    """Default branch as of the last fetch; falls back to HEAD for repos without origin/HEAD."""
    ok = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "-q", "origin/HEAD"], capture_output=True, check=False
    ).returncode == 0
    return "origin/HEAD" if ok else "HEAD"


def commits(repo: Path, since: str = SINCE) -> Iterator[Record]:
    out = subprocess.run(
        # Merge commits only repeat the PR they merge
        ["git", "-C", str(repo), "log", _branch(repo), "--no-merges", f"--since={since}", f"--format={_FORMAT}", "--name-only"],
        capture_output=True, text=True, errors="replace", check=True,
    ).stdout
    for chunk in out.split("\x1e")[1:]:
        sha, date, author, subject, message, files = chunk.split("\x1f", 5)
        paths = [p for p in files.splitlines() if p]
        body = redact(
            f"{subject}\n\n{message.strip()}\n\nAuthor: {author}\nFiles:\n" + "\n".join(paths[:MAX_PATHS])
            + (f"\n…{len(paths) - MAX_PATHS} more" if len(paths) > MAX_PATHS else "")
        )
        ts = datetime.fromisoformat(date)
        yield Record(
            ref=f"git:{repo.name}@{sha}", ts=ts, project=repo.name,
            title=redact(subject), summary=redact(message.strip()), body=body, author=author,
            events=[Event(ts, author, "committed")],
        )


def fetch(ctx: Context) -> Iterator[Record | Failed]:
    """Every repo directly under each configured root."""
    for root in ctx.cfg.get("roots", []):
        for repo in repos(Path(root).expanduser()):
            try:
                rows = list(commits(repo, ctx.cfg.get("since", SINCE)))
            except subprocess.CalledProcessError as e:  # empty repo, broken clone
                yield Failed(repo.name, e.stderr.strip()[:300])
                continue
            yield from rows
