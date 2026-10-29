"""Jira Cloud issues -> records. Tickets are already written for people, so no LLM call."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from datetime import datetime

from brain import secrets
from brain.connectors import Context, Event, Failed, Record
from brain.redact import redact

SECRET = True
SINCE_DAYS = 365
MAX_COMMENT_CHARS = 4_000  # bot ledgers post 30k-char comments; same cap as pasted text in sessions
OVERLAP_DAYS = 1
# Changes to these mean someone worked the ticket; edits to other fields (labels, custom fields) do not.
ACTIVITY_FIELDS = {"status", "resolution", "summary", "description", "assignee"}  # re-read a day before the last success; unchanged tickets hash the same and are skipped
FIELDS = "summary,status,resolution,issuetype,labels,parent,description,comment,reporter,assignee,created,updated,project"

_BLOCKS = {"paragraph", "heading", "blockquote", "panel", "rule", "table", "tableRow", "mediaSingle"}


def adf_text(node) -> str:
    """Atlassian Document Format -> plain text."""
    if not isinstance(node, dict):
        return ""
    kind, attrs = node.get("type"), node.get("attrs", {})
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"
    if kind in ("mention", "emoji"):
        return attrs.get("text") or attrs.get("shortName", "")
    if kind in ("inlineCard", "blockCard"):
        return attrs.get("url", "")
    inner = "".join(adf_text(c) for c in node.get("content", []))
    if kind == "codeBlock":
        return f"```\n{inner}\n```\n"
    if kind == "listItem":
        return f"- {inner}"
    if kind in ("tableCell", "tableHeader"):
        return inner.strip() + " | "
    return inner + "\n" if kind in _BLOCKS else inner


def adf_mentions(node) -> list[str]:
    """Names @mentioned in an ADF document."""
    if not isinstance(node, dict):
        return []
    if node.get("type") == "mention":
        return [(node.get("attrs", {}).get("text") or "").lstrip("@")]
    return [n for c in node.get("content", []) for n in adf_mentions(c)]


def _clip(text: str) -> str:
    return text if len(text) <= MAX_COMMENT_CHARS else text[:MAX_COMMENT_CHARS] + f"\n…[{len(text) - MAX_COMMENT_CHARS} chars truncated]"


def _name(user: dict | None) -> str | None:
    return (user or {}).get("displayName")


def _when(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%f%z")


def _human(user: dict | None) -> bool:
    return (user or {}).get("accountType") != "app"


def activity(issue: dict, comments: list[dict], fields: set[str] = ACTIVITY_FIELDS) -> datetime:
    """When a person last worked the ticket: created, their comments, or their edits to fields that matter.

    `updated` also moves on bot comments and bulk edits, which would make old tickets rank as fresh.
    """
    stamps = [issue["fields"]["created"]]
    stamps += [c["created"] for c in comments if _human(c.get("author"))]
    stamps += [h["created"] for h in (issue.get("changelog") or {}).get("histories", [])
               if _human(h.get("author")) and any(i.get("field") in fields for i in h.get("items", []))]
    return max(_when(s) for s in stamps)


def old_keys(issue: dict) -> list[str]:
    """Keys this ticket had before a project move."""
    return [i["fromString"] for h in (issue.get("changelog") or {}).get("histories", [])
            for i in h.get("items", []) if i.get("field") == "Key" and i.get("fromString")]


def _changes(issue: dict) -> list[tuple[dict, str, dict]]:
    """(history, event kind, item) for each status or assignee change, oldest first."""
    return [(h, "status" if i["field"] == "status" else "assigned", i)
            for h in sorted((issue.get("changelog") or {}).get("histories", []), key=lambda h: h["created"])
            for i in h.get("items", []) if i.get("field") in ("status", "assignee")]


def _change(kind: str, item: dict) -> str:
    if kind == "status":
        return f"{item.get('fromString')} → {item.get('toString')}"
    return item.get("toString") or "unassigned"


def events(issue: dict, comments: list[dict]) -> list[Event]:
    f = issue["fields"]
    out = [Event(_when(f["created"]), _name(f.get("reporter")) or "unknown", "created", bot=not _human(f.get("reporter")))]
    out += [Event(_when(c["created"]), _name(c.get("author")) or "unknown", "commented",
                  adf_text(c.get("body")).strip().split("\n", 1)[0][:120] or None, not _human(c.get("author")))
            for c in comments]
    out += [Event(_when(h["created"]), _name(h.get("author")) or "unknown", kind, _change(kind, i), not _human(h.get("author")))
            for h, kind, i in _changes(issue)]
    said = [(f["created"], f.get("reporter"), f.get("description"))] + [(c["created"], c.get("author"), c.get("body")) for c in comments]
    out += [Event(_when(at), _name(who) or "unknown", "mentioned", name, not _human(who))
            for at, who, doc in said for name in dict.fromkeys(adf_mentions(doc)) if name]
    return sorted(out, key=lambda e: e.ts)


def to_record(issue: dict, comments: list[dict], fields: set[str] = ACTIVITY_FIELDS) -> Record:
    f = issue["fields"]
    key = issue["key"]
    description = adf_text(f.get("description")).strip()
    meta = " · ".join(x for x in (
        (f.get("issuetype") or {}).get("name"),
        f"Status: {(f.get('status') or {}).get('name')}",
        f"Resolution: {f['resolution']['name']}" if f.get("resolution") else None,
        f"Reporter: {_name(f.get('reporter'))}",
        f"Assignee: {_name(f.get('assignee')) or 'none'}",
        f"Parent: {f['parent']['key']}" if f.get("parent") else None,
        f"Labels: {', '.join(f['labels'])}" if f.get("labels") else None,
    ) if x)
    thread = "\n\n".join(
        f"[{c['created'][:10]}] {_name(c.get('author'))}: {_clip(adf_text(c.get('body')).strip())}" for c in comments
    )
    history = "\n".join(
        f"[{h['created'][:10]}] {_name(h.get('author'))}: {'Status' if kind == 'status' else 'Assignee'} {_change(kind, i)}"
        for h, kind, i in _changes(issue)
    )
    body = (f"{key}: {f['summary']}\n{meta}\n\n{description}" + (f"\n\n## History\n\n{history}" if history else "")
            + (f"\n\n## Comments\n\n{thread}" if thread else ""))
    return Record(
        ref=f"jira:{key}",
        ts=activity(issue, comments, fields),
        project=(f.get("project") or {}).get("key"),
        title=redact(f"{key} {f['summary']}"),
        summary=redact(description[:500]) or None,
        body=redact(body),
        author=_name(f.get("reporter")),
        replaces=[f"jira:{k}" for k in old_keys(issue)],
        events=events(issue, comments),
    )


def jql(projects: list[str], since: str) -> str:
    # Commenting makes you a watcher by default, so watcher covers "commented on".
    mine = "reporter = currentUser() OR assignee = currentUser() OR watcher = currentUser()"
    scope = f"project in ({', '.join(projects)}) OR {mine}" if projects else mine
    return f"({scope}) AND updated >= {since} ORDER BY updated DESC"


class _Client:
    def __init__(self, site: str, email: str, token: str):
        self.base = f"https://{site}/rest/api/3"
        self.auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()

    def get(self, path: str, **params) -> dict:
        url = f"{self.base}{path}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": self.auth, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)

    def issues(self, query: str) -> Iterator[dict]:
        token = None
        while True:
            page = self.get("/search/jql", jql=query, fields=FIELDS, expand="changelog", maxResults=100,
                            **({"nextPageToken": token} if token else {}))
            yield from page.get("issues", [])
            token = page.get("nextPageToken")
            if page.get("isLast", True) or not token:
                return

    def changelog(self, issue: dict) -> list[dict]:
        """Search embeds part of a long history (its total says how much); fetch the whole of it when there is more."""
        c = issue.get("changelog") or {}
        if len(c.get("histories", [])) >= c.get("total", 0):
            return c.get("histories", [])
        got = []
        while True:
            page = self.get(f"/issue/{issue['key']}/changelog", startAt=len(got), maxResults=100)
            got += page["values"]
            if page.get("isLast", True) or not page["values"]:
                return got

    def comments(self, issue: dict) -> list[dict]:
        """Search returns only the first page of comments; fetch the rest when there are more."""
        c = issue["fields"].get("comment") or {}
        got = c.get("comments", [])
        while len(got) < c.get("total", 0):
            more = self.get(f"/issue/{issue['key']}/comment", startAt=len(got), maxResults=100)["comments"]
            if not more:
                break
            got += more
        return got


def fetch(ctx: Context) -> Iterator[Record | Failed]:
    token = secrets.get("jira")
    if not token:
        yield Failed("auth", "no Jira token; run brain auth jira")
        return
    last = ctx.conn.execute("select last_success from connector_runs where connector = 'jira'").fetchone()
    since = f"-{ctx.cfg.get('since_days', SINCE_DAYS)}d"
    if last and last[0] and not ctx.full:
        since = f"-{(datetime.now(last[0].tzinfo) - last[0]).days + OVERLAP_DAYS}d"
    client = _Client(ctx.cfg["site"], ctx.cfg["email"], token)
    try:
        for issue in client.issues(jql(ctx.cfg.get("projects", []), since)):
            issue["changelog"] = {"histories": client.changelog(issue)}
            yield to_record(issue, client.comments(issue), ACTIVITY_FIELDS | set(ctx.cfg.get("activity_fields", [])))
    except urllib.error.HTTPError as e:
        yield Failed(f"jira {e.code}", e.read().decode(errors="replace")[:300])
