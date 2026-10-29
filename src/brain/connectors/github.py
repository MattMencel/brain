"""GitHub pull requests -> records, read with `gh api graphql` under gh's own login (gh auth login); brain stores no token.

PR descriptions and reviews are written for people, so no LLM call. Nested lists stop at the first 100 comments,
100 reviews and 100 review threads (50 comments each); longer PRs are rare and their tail is dropped.
"""

import json
import re
import subprocess
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta

from brain.connectors import Context, Event, Failed, Record
from brain.redact import redact

SINCE_DAYS = 365
OVERLAP_DAYS = 1
SLICE_DAYS = 7
SEARCH_CAP = 1000  # a search returns at most this many results, however many pages
MIN_POINTS = 200  # below this many rate-limit points left, wait for the hourly reset
MAX_COMMENT_CHARS = 4_000
RETRY_SECONDS = (10, 30, 90)  # GitHub resets the odd stream mid-backfill; the same page succeeds on a re-run

_ACTOR = "author { __typename login }"
# 25 PRs a page costs about 25 points of the 5,000 an hour.
QUERY = f"""
query($q: String!, $cursor: String) {{
  rateLimit {{ remaining resetAt }}
  search(type: ISSUE, query: $q, first: 25, after: $cursor) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ... on PullRequest {{
      number title body state createdAt mergedAt baseRefName headRefName
      repository {{ nameWithOwner name }} {_ACTOR} mergeCommit {{ oid }}
      timelineItems(itemTypes: [MERGED_EVENT, CLOSED_EVENT], last: 10) {{ nodes {{ __typename
        ... on MergedEvent {{ createdAt actor {{ __typename login }} }}
        ... on ClosedEvent {{ createdAt actor {{ __typename login }} }} }} }}
      comments(first: 100) {{ nodes {{ createdAt body {_ACTOR} }} }}
      reviews(first: 100) {{ nodes {{ createdAt state body {_ACTOR} }} }}
      reviewThreads(first: 100) {{ nodes {{ path line comments(first: 50) {{ nodes {{ createdAt body {_ACTOR} }} }} }} }}
    }} }}
  }}
}}
"""


class GhError(Exception):
    pass


def gh(query: str, **variables) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        if v is not None:
            args += ["-f", f"{k}={v}"]
    out = subprocess.run(args, capture_output=True, text=True, check=False)
    if out.returncode:
        raise GhError(out.stderr.strip()[:300])
    return json.loads(out.stdout)["data"]


def _when(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp)


def _login(actor: dict | None) -> str:
    return (actor or {}).get("login") or "ghost"  # GitHub's name for a deleted account


def _bot(actor: dict | None) -> bool:
    return (actor or {}).get("__typename") == "Bot" or _login(actor).endswith("[bot]")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_COMMENT_CHARS else text[:MAX_COMMENT_CHARS] + f"\n…[{len(text) - MAX_COMMENT_CHARS} chars truncated]"


def _first_line(text: str) -> str | None:
    return text.strip().split("\n", 1)[0][:120] or None


_MENTION = re.compile(r"(?<![\w/`])@([A-Za-z0-9][A-Za-z0-9-]*(?:/[A-Za-z0-9_.-]+)?)")  # a login, or org/team


def _mentions(at: str, actor: dict | None, text: str | None) -> list[Event]:
    return [Event(_when(at), _login(actor), "mentioned", name, _bot(actor))
            for name in dict.fromkeys(_MENTION.findall(text or "")) if name != _login(actor)]


def to_record(pr: dict) -> Record:
    nwo, n = pr["repository"]["nameWithOwner"], pr["number"]
    author = pr.get("author")
    reviews = [r for r in pr["reviews"]["nodes"] if r["state"] != "PENDING"]
    notes = [(c, None) for c in pr["comments"]["nodes"]] + [
        (c, f"{t['path']}:{t['line']}" if t.get("line") else t["path"])
        for t in pr["reviewThreads"]["nodes"] for c in t["comments"]["nodes"]
    ]
    ends = {t["__typename"]: t for t in pr["timelineItems"]["nodes"]}  # last of each kind wins

    events = [Event(_when(pr["createdAt"]), _login(author), "opened", bot=_bot(author))]
    events += [Event(_when(r["createdAt"]), _login(r["author"]), "reviewed", r["state"].lower(), _bot(r["author"]))
               for r in reviews]
    events += [Event(_when(c["createdAt"]), _login(c["author"]), "commented", _first_line(c["body"]), _bot(c["author"]))
               for c, _where in notes]
    events += _mentions(pr["createdAt"], author, pr.get("body"))
    events += [e for r in reviews for e in _mentions(r["createdAt"], r["author"], r["body"])]
    events += [e for c, _where in notes for e in _mentions(c["createdAt"], c["author"], c["body"])]
    if pr["state"] == "MERGED" and (m := ends.get("MergedEvent")):
        events.append(Event(_when(m["createdAt"]), _login(m["actor"]), "merged", bot=_bot(m["actor"])))
    elif pr["state"] == "CLOSED" and (c := ends.get("ClosedEvent")):
        events.append(Event(_when(c["createdAt"]), _login(c["actor"]), "closed", bot=_bot(c["actor"])))
    events.sort(key=lambda e: e.ts)

    # Bots (CodeRabbit, Renovate, CI) write most comments on many PRs; they stay as events but out of the body.
    meta = " · ".join(x for x in (
        f"State: {pr['state']}", f"Author: {_login(author)}", f"{pr['headRefName']} → {pr['baseRefName']}",
        f"Merged as git:{pr['repository']['name']}@{pr['mergeCommit']['oid']}" if pr.get("mergeCommit") else None,
    ) if x)
    review_lines = "\n\n".join(
        f"[{r['createdAt'][:10]}] {_login(r['author'])}: {r['state']}" + (f": {_clip(r['body'].strip())}" if r["body"].strip() else "")
        for r in sorted(reviews, key=lambda r: r["createdAt"]) if not _bot(r["author"])
    )
    comment_lines = "\n\n".join(
        f"[{c['createdAt'][:10]}] {_login(c['author'])}" + (f" on {where}" if where else "") + f": {_clip(c['body'].strip())}"
        for c, where in sorted(notes, key=lambda x: x[0]["createdAt"]) if not _bot(c["author"])
    )
    description = (pr.get("body") or "").strip()
    body = (f"{nwo}#{n}: {pr['title']}\n{meta}\n\n{description}"
            + (f"\n\n## Reviews\n\n{review_lines}" if review_lines else "")
            + (f"\n\n## Comments\n\n{comment_lines}" if comment_lines else ""))
    human = [e.ts for e in events if not e.bot]
    return Record(
        ref=f"github:{nwo}#{n}",
        ts=max(human) if human else events[0].ts,
        project=pr["repository"]["name"],  # matches the git connector's project, the clone's directory name
        title=redact(f"{nwo}#{n} {pr['title']}"),
        summary=redact(description[:500]) or None,
        body=redact(body),
        author=_login(author),
        events=events,
    )


def _retry(run: Callable[..., dict], *args, **kwargs) -> dict:
    for wait in RETRY_SECONDS:
        try:
            return run(*args, **kwargs)
        except GhError as e:
            print(f"github: {e}; retrying in {wait}s", flush=True)
            time.sleep(wait)
    return run(*args, **kwargs)


def _throttle(rate: dict) -> None:
    if rate["remaining"] < MIN_POINTS:
        wait = (_when(rate["resetAt"]) - datetime.now(UTC)).total_seconds() + 5
        print(f"github: {rate['remaining']} rate-limit points left; waiting {wait:.0f}s for the reset", flush=True)
        time.sleep(max(wait, 0))


def prs(scope: str, start: date, end: date, run: Callable[..., dict] = gh) -> Iterator[dict]:
    """Every PR in scope last updated in [start, end]. A range holding more than search returns is halved."""
    q = f"{scope} is:pr updated:{start}..{end}"
    cursor = None
    while True:
        data = _retry(run, QUERY, q=q, cursor=cursor)
        s = data["search"]
        if cursor is None and s["issueCount"] > SEARCH_CAP and end > start:
            mid = start + (end - start) // 2
            yield from prs(scope, start, mid, run)
            yield from prs(scope, mid + timedelta(days=1), end, run)
            return
        yield from (n for n in s["nodes"] if n)
        _throttle(data["rateLimit"])
        if not s["pageInfo"]["hasNextPage"]:
            return
        cursor = s["pageInfo"]["endCursor"]


def fetch(ctx: Context, run: Callable[..., dict] = gh) -> Iterator[Record | Failed]:
    """PRs that involve the user (author, assignee, reviewer, commenter, mention), plus every PR in configured repos."""
    last = ctx.conn.execute("select last_success from connector_runs where connector = 'github'").fetchone()
    today = date.today()
    if last and last[0] and not ctx.full:
        start = last[0].date() - timedelta(days=OVERLAP_DAYS)
    else:
        start = today - timedelta(days=ctx.cfg.get("since_days", SINCE_DAYS))
    seen = set()
    for scope in ["involves:@me"] + [f"repo:{r}" for r in ctx.cfg.get("repos", [])]:
        # Weekly slices keep each search under its result cap; a PR matches only the slice of its last update.
        d = start
        while d <= today:
            end = min(d + timedelta(days=SLICE_DAYS - 1), today)
            try:
                for pr in prs(scope, d, end, run):
                    r = to_record(pr)
                    if r.ref not in seen:
                        seen.add(r.ref)
                        yield r
            except GhError as e:
                yield Failed(f"{scope} {d}..{end}", str(e))
            d = end + timedelta(days=1)
