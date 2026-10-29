"""Slack threads and conversation-days -> records, read with the Slack Web API and a user token (brain auth slack).

The token comes from an internal Slack app with user scopes search:read, channels:history, groups:history,
im:history, mpim:history, channels:read, groups:read, im:read, mpim:read and users:read.
"""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta

from brain import secrets
from brain.connectors import Context, Event, Failed, Record
from brain.redact import redact

SECRET = True
SINCE_DAYS = 30
OVERLAP_HOURS = 1
MIN_SUMMARIZE_CHARS = 300  # below this a record is a ping ("code review <link>"), not worth a Haiku call
API = "https://slack.com/api/"


@dataclass
class Msg:
    name: str
    ts: str  # Slack ts, epoch seconds as a string; also the id threads are fetched by
    text: str
    replies: int = 0
    latest: float = 0.0  # latest reply, epoch seconds
    parent: str | None = None  # thread_ts when this message is a reply
    bot: bool = False

    @property
    def at(self) -> datetime:
        return datetime.fromtimestamp(float(self.ts))


@dataclass
class Hit:
    channel_id: str
    dm: bool
    thread_ts: str | None
    msg: Msg


class SlackError(Exception):
    def __init__(self, method: str, code: str):
        super().__init__(f"{method}: {code}")
        self.code = code


class Client:
    def __init__(self, token: str):
        self.token = token
        self.users: dict[str, str] = {}
        self.projects: dict[str, str] = {}

    def get(self, method: str, **params) -> dict:
        url = f"{API}{method}?{urllib.parse.urlencode(params)}"
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self.token}"})
        for _ in range(5):
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code != 429:
                    raise SlackError(method, f"HTTP {e.code}") from e
                time.sleep(int(e.headers.get("Retry-After", "30")))
                continue
            if not body.get("ok"):
                raise SlackError(method, body.get("error", "unknown"))
            return body
        raise SlackError(method, "rate limited")

    def user(self, uid: str) -> str:
        if uid not in self.users:
            try:
                u = self.get("users.info", user=uid)["user"]
                self.users[uid] = u.get("real_name") or u["profile"].get("display_name") or u.get("name") or uid
            except SlackError:
                self.users[uid] = uid
        return self.users[uid]

    def project(self, cid: str) -> str:
        """#channel, or "DM: names" for a direct or group message."""
        if cid not in self.projects:
            c = self.get("conversations.info", channel=cid)["channel"]
            if c.get("is_im"):
                self.projects[cid] = f"DM: {self.user(c['user'])}"
            elif c.get("is_mpim"):
                members = self.get("conversations.members", channel=cid)["members"]
                self.projects[cid] = "DM: " + ", ".join(self.user(m) for m in members)
            else:
                self.projects[cid] = f"#{c['name']}"
        return self.projects[cid]

    def msg(self, m: dict) -> Msg:
        name = self.user(m["user"]) if m.get("user") else (m.get("bot_profile") or {}).get("name") or m.get("username") or "unknown"
        text = re.sub(r"<@(U\w+)>", lambda x: f"<@{x.group(1)}|{self.user(x.group(1))}>", m.get("text", ""))
        if files := [f.get("name") or f.get("title") or "file" for f in m.get("files", [])]:
            text += "\n[files: " + ", ".join(files) + "]"
        parent = m.get("thread_ts") if m.get("thread_ts") not in (None, m["ts"]) else None
        return Msg(name, m["ts"], text, m.get("reply_count", 0), float(m.get("latest_reply", 0)), parent,
                   bool(m.get("bot_id")) or m.get("subtype") == "bot_message")

    def history(self, cid: str, oldest: datetime, latest: datetime | None = None) -> list[Msg]:
        params = {"channel": cid, "oldest": f"{oldest.timestamp():.6f}", "limit": 200}
        if latest:
            params["latest"] = f"{latest.timestamp():.6f}"
        return [self.msg(m) for page in self._pages("conversations.history", **params) for m in page["messages"]]

    def replies(self, cid: str, ts: str) -> list[Msg]:
        return [self.msg(m) for page in self._pages("conversations.replies", channel=cid, ts=ts, limit=200)
                for m in page["messages"]]

    def search(self, query: str) -> list[Hit]:
        hits, page = [], 1
        while True:
            r = self.get("search.messages", query=query, count=100, page=page, sort="timestamp")["messages"]
            for m in r.get("matches", []):
                c = m.get("channel") or {}
                thread = re.search(r"thread_ts=([\d.]+)", m.get("permalink", ""))
                hits.append(Hit(c.get("id", ""), bool(c.get("is_im") or c.get("is_mpim")),
                                thread.group(1) if thread else None, self.msg(m)))
            if page >= (r.get("paging") or {}).get("pages", 1):
                return hits
            page += 1

    def _pages(self, method: str, **params) -> Iterator[dict]:
        cursor = None
        while True:
            page = self.get(method, **params, **({"cursor": cursor} if cursor else {}))
            yield page
            cursor = (page.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                return


def clean(text: str) -> str:
    text = re.sub(r"\n?\*Sent using\* <@\w+\|[^>]+>", "", text)
    text = re.sub(r"<@\w+\|([^>]+)>", r"@\1", text)
    text = re.sub(r"<!subteam\^\w+\|?([^>]*)>", lambda m: m.group(1) or "@group", text)
    text = re.sub(r"<#\w+\|([^>]+)>", r"#\1", text)
    text = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2 (\1)", text)
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").strip()


def mentions(m: Msg, dm_with: str | None = None, me: str | None = None) -> list[Event]:
    """Who a message is addressed to: each @person, each @group by id (a user token gets no group names), and the
    other side of a direct message."""
    targets = re.findall(r"<@\w+\|([^>]+)>", m.text) + re.findall(r"<!subteam\^(\w+)", m.text)
    if dm_with and me:
        targets.append(dm_with if m.name == me else me)
    return [Event(m.at.astimezone(), m.name, "mentioned", t, m.bot) for t in dict.fromkeys(targets) if t != m.name]


def to_record(ref: str, project: str, msgs: list[Msg], thread: bool, me: str | None = None) -> Record:
    msgs = sorted({m.ts: m for m in msgs}.values(), key=lambda m: float(m.ts))
    body = redact("\n\n".join(f"[{m.name} {m.at:%Y-%m-%d %H:%M}] {clean(m.text)}" for m in msgs))
    first = clean(msgs[0].text).split("\n", 1)[0][:120]
    dm_with = project.removeprefix("DM: ") if ref.startswith("slack:D") else None  # D: one-to-one; G/C: group DMs
    return Record(
        ref=ref, ts=msgs[-1].at.astimezone(), project=project,
        title=redact(f"{project}: {first}" if thread else f"{project} {msgs[0].at:%Y-%m-%d}"),
        summary=None, body=body, author=msgs[0].name if thread else None,
        summarize="slack" if len(body) >= MIN_SUMMARIZE_CHARS else None,
        events=[e for m in msgs for e in [Event(m.at.astimezone(), m.name, "posted", bot=m.bot), *mentions(m, dm_with, me)]],
    )


def fetch(ctx: Context, client: Client | None = None) -> Iterator[Record | Failed]:
    """First run: configured channels read in full over the window. Later runs: per-day searches find new
    messages, and just the threads and days they touch are re-read."""
    if client is None:
        token = secrets.get("slack")
        if not token:
            yield Failed("auth", "no Slack token; run brain auth slack")
            return
        client = Client(token)
    cfg = ctx.cfg
    last = ctx.conn.execute("select last_success from connector_runs where connector = 'slack'").fetchone()
    now = datetime.now()
    incremental = bool(last and last[0]) and not ctx.full
    if incremental:
        since = last[0].astimezone().replace(tzinfo=None) - timedelta(hours=OVERLAP_HOURS)
    else:
        since = now - timedelta(days=cfg.get("since_days", SINCE_DAYS))
    day0 = since.replace(hour=0, minute=0, second=0, microsecond=0)  # whole days, so a day record is never partial
    channels = cfg.get("channels", [])
    threads: set[tuple[str, str]] = set()
    days: dict[tuple[str, str], list[Msg]] = defaultdict(list)

    def fresh(ts: str | float) -> bool:
        return float(ts) >= since.timestamp()

    def collect(cid: str, msgs: list[Msg]) -> None:
        for m in msgs:
            if m.parent:  # a reply broadcast to the channel belongs to its thread
                if fresh(m.ts):
                    threads.add((cid, m.parent))
            elif m.replies:
                if fresh(m.ts) or fresh(m.latest):
                    threads.add((cid, m.ts))
            else:
                days[(cid, f"{m.at:%Y-%m-%d}")].append(m)

    if not incremental:  # configured channels, in full
        for cid in channels:
            try:
                collect(cid, client.history(cid, day0))
            except SlackError as e:
                yield Failed(cid, str(e))

    # The user's own messages anywhere, and messages addressed to them: their threads, DM days, and lone posts
    # in other channels. to: finds DMs to the user; the raw <@id> token finds mentions of a user or group.
    # Later runs also search each configured channel: a reply's hit carries its thread, however old the thread is.
    me = cfg.get("user_id")
    me_name = client.user(me) if me else None
    queries = [f"from:<@{me}>" if me else "from:me"]
    if me:
        queries.append(f"to:<@{me}>")
    queries += [f"<@{i}>" for i in ([me] if me else []) + cfg.get("groups", [])]
    if incremental:
        queries += [f"in:<#{cid}>" for cid in channels]
    found: list[Hit] = []
    # One search per day: a search's results stop at a page limit that a month of messages can pass.
    for d in (since.date() + timedelta(days=n) for n in range((now.date() - since.date()).days + 1)):
        for q in queries:
            try:
                found += client.search(f"{q} on:{d:%Y-%m-%d}")
            except SlackError as e:
                yield Failed(f"search {q} on:{d}", str(e))
    # A day's searches return all of that day's hits, so a lone-post day is rebuilt whole, never from new hits only.
    full_days, touched = set(), set()
    lone: dict[tuple[str, str], list[Msg]] = defaultdict(list)
    for h in found:
        key = (h.channel_id, f"{h.msg.at:%Y-%m-%d}")
        if h.thread_ts:
            if fresh(h.msg.ts):
                threads.add((h.channel_id, h.thread_ts))
        elif h.dm or h.channel_id in channels:
            if fresh(h.msg.ts) and (h.dm or incremental):  # a first run already read configured channels in full
                full_days.add(key)
        else:
            lone[key].append(h.msg)
            if fresh(h.msg.ts):
                touched.add(key)
    for key in touched:
        days[key] = lone[key]

    for cid, day in full_days:  # DM days, and configured-channel days with new messages, in full
        start = datetime.strptime(day, "%Y-%m-%d")
        try:
            collect(cid, client.history(cid, start, start + timedelta(days=1)))
        except SlackError as e:
            yield Failed(cid, str(e))

    for cid, ts in threads:
        try:
            msgs = client.replies(cid, ts)
        except SlackError as e:
            if e.code != "thread_not_found":  # deleted since it was seen
                yield Failed(f"{cid}/{ts}", str(e))
            continue
        if msgs:
            yield to_record(f"slack:{cid}/{ts}", client.project(cid), msgs, thread=True, me=me_name)

    for (cid, day), msgs in days.items():
        try:
            project = client.project(cid)
        except SlackError as e:
            yield Failed(cid, str(e))
            continue
        yield to_record(f"slack:{cid}/{day}", project, msgs, thread=False, me=me_name)
