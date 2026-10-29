"""A connector turns one source into Records; `brain ingest` stores, summarizes and indexes them the same way for every source."""

from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType

import psycopg


@dataclass
class Event:
    """One action on a record: who did what, when. Kinds are listed in docs/design-connectors.md."""

    ts: datetime
    actor: str  # the source's own name for the actor; [people] aliases map names to one person at query time
    kind: str
    detail: str | None = None
    bot: bool = False


@dataclass
class Record:
    ref: str
    ts: datetime
    project: str | None
    title: str | None
    summary: str | None
    body: str
    author: str | None = None
    mtime: float | None = None
    summarize: str | None = None  # summarizer prompt ("session", "slack"): Haiku writes title, summary and topics; the body also gets passages
    replaces: list[str] = field(default_factory=list)  # refs this record supersedes, e.g. a ticket's key before a move
    events: list[Event] = field(default_factory=list)


@dataclass
class Failed:
    """One item the connector could not read; the rest of the run continues."""

    label: str
    error: str


@dataclass
class Context:
    conn: psycopg.Connection
    cfg: dict  # this connector's table from brain.toml
    full: bool = False  # ignore last_success and mtimes: re-read the whole window (brain ingest --full)


def registry() -> dict[str, ModuleType]:
    """Each module has fetch(ctx) -> Iterable[Record | Failed], and SECRET = True if it needs a token from brain auth."""
    from brain.connectors import git, github, jira, sessions, slack

    return {"sessions": sessions, "git": git, "github": github, "jira": jira, "slack": slack}
