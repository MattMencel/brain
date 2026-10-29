import time
from datetime import UTC, date, datetime, timedelta

from brain.connectors import Context, Failed
from brain.connectors.slack import Client, Msg, SlackError, clean, fetch, to_record

USERS = {"U0ANA": "Ana Diaz", "U0BO": "Bo Lee", "U0ME": "Me"}


class FakeSlack(Client):
    """Answers Web API methods from canned data and records every call."""

    def __init__(self, history=None, replies=None, search=None):
        super().__init__("xoxp-test")
        self.calls = []
        self.history_msgs = history or []
        self.replies_by_ts = replies or {}
        self.search_fn = search or (lambda q: [])

    def get(self, method, **params):
        self.calls.append((method, params))
        if method == "users.info":
            return {"user": {"real_name": USERS.get(params["user"], params["user"]), "profile": {}}}
        if method == "conversations.info":
            return {"channel": {"id": params["channel"], "name": "team-ops"}}
        if method == "conversations.history":
            return {"messages": self.history_msgs}
        if method == "conversations.replies":
            if params["ts"] not in self.replies_by_ts:
                raise SlackError(method, "thread_not_found")
            return {"messages": self.replies_by_ts[params["ts"]]}
        if method == "search.messages":
            return {"messages": {"matches": self.search_fn(params["query"]), "paging": {"pages": 1}}}
        raise AssertionError(method)


class Conn:
    def __init__(self, last=None):
        self.last = last

    def execute(self, *a):
        return self

    def fetchone(self):
        return (self.last,)


def test_msg_resolves_mentions_files_and_reply_parent():
    m = FakeSlack().msg({"ts": "2.0", "thread_ts": "1.0", "user": "U0BO", "text": "<@U0ANA> see log",
                         "files": [{"name": "trace.txt"}]})
    assert (m.name, m.parent) == ("Bo Lee", "1.0")
    assert clean(m.text) == "@Ana Diaz see log\n[files: trace.txt]"
    parent = FakeSlack().msg({"ts": "1.0", "thread_ts": "1.0", "user": "U0ANA", "text": "q", "reply_count": 2})
    assert parent.parent is None and parent.replies == 2


def test_clean_rewrites_mentions_links_groups_and_drops_sent_using():
    text = "hi <@U1|Ana>, <!subteam^S1|@infra> see <https://x.io/a|the doc> in <#C1|ops> &amp; more\n*Sent using* <@U0BOT|Claude>"
    assert clean(text) == "hi @Ana, @infra see the doc (https://x.io/a) in #ops & more"


def test_to_record_orders_turns_and_skips_haiku_for_pings():
    msgs = [Msg("Bo Lee", "1792707060.0", "raise the probe timeout to 5s"), Msg("Ana Diaz", "1792174886.0", "Why does the canary fail?")]
    r = to_record("slack:C0TEAM/1792174886.0", "#team-ops", msgs, thread=True)
    assert r.body.startswith("[Ana Diaz ") and "\n\n[Bo Lee " in r.body
    assert r.title == "#team-ops: Why does the canary fail?" and r.author == "Ana Diaz"
    assert r.summarize is None
    assert [(e.actor, e.kind) for e in r.events] == [("Ana Diaz", "posted"), ("Bo Lee", "posted")]


def test_bot_messages_are_flagged():
    m = FakeSlack().msg({"ts": "1.0", "bot_id": "B1", "bot_profile": {"name": "CodeRabbit"}, "text": "review done"})
    assert (m.name, m.bot) == ("CodeRabbit", True)
    assert to_record("slack:C1/1.0", "#x", [m], thread=True).events[0].bot


def test_first_run_reads_channels_in_full_and_searches_one_day_at_a_time():
    now = f"{time.time() - 60:.6f}"
    slack = FakeSlack(history=[{"ts": now, "user": "U0ANA", "text": "standup moved"}])
    items = list(fetch(Context(Conn(), {"user_id": "U0ME", "groups": ["S0GRP"], "channels": ["C0TEAM"], "since_days": 3}), slack))
    assert [m for m, p in slack.calls if m == "conversations.history" and "latest" not in p]
    days = [(date.today() - timedelta(days=n)).isoformat() for n in range(4)]
    queries = [p["query"] for m, p in slack.calls if m == "search.messages"]
    assert sorted(queries) == sorted(f"{q} on:{d}" for d in days for q in ("from:<@U0ME>", "to:<@U0ME>", "<@U0ME>", "<@S0GRP>"))
    assert [i.ref for i in items] == [f"slack:C0TEAM/{datetime.fromtimestamp(float(now)):%Y-%m-%d}"]


def test_incremental_run_reads_only_what_new_messages_touch():
    now = f"{time.time() - 60:.6f}"
    old_thread, gone = f"{time.time() - 40 * 86400:.6f}", "1.000000"

    def search(query):
        if not query.startswith("in:<#C0TEAM>"):
            return []
        channel = {"id": "C0TEAM", "name": "team-ops"}
        return [
            {"ts": now, "user": "U0BO", "text": "late reply", "channel": channel,
             "permalink": f"https://x.slack.com/archives/C0TEAM/p1?thread_ts={old_thread}&cid=C0TEAM"},
            {"ts": now, "user": "U0BO", "text": "reply on a deleted thread", "channel": channel,
             "permalink": f"https://x.slack.com/archives/C0TEAM/p2?thread_ts={gone}&cid=C0TEAM"},
            {"ts": now, "user": "U0ANA", "text": "new post", "channel": channel, "permalink": "https://x.slack.com/p3"},
        ]

    slack = FakeSlack(
        history=[{"ts": now, "user": "U0ANA", "text": "new post"}],
        replies={old_thread: [{"ts": old_thread, "user": "U0ANA", "text": "old question", "reply_count": 1},
                              {"ts": now, "thread_ts": old_thread, "user": "U0BO", "text": "late reply"}]},
        search=search,
    )
    last = datetime.now(UTC) - timedelta(minutes=30)
    items = list(fetch(Context(Conn(last), {"user_id": "U0ME", "channels": ["C0TEAM"]}), slack))
    assert not [i for i in items if isinstance(i, Failed)]  # the deleted thread is skipped, not a failure
    assert all("latest" in p for m, p in slack.calls if m == "conversations.history")  # no full channel read
    assert {i.ref for i in items} == {f"slack:C0TEAM/{old_thread}", f"slack:C0TEAM/{datetime.now():%Y-%m-%d}"}


def test_mentions_name_people_groups_and_the_other_side_of_a_dm():
    slack = FakeSlack()
    m = slack.msg({"ts": "1.0", "user": "U0BO", "text": "<@U0ANA> and <!subteam^S0GRP> look"})
    assert [(e.actor, e.detail) for e in to_record("slack:C1/1.0", "#x", [m], thread=True, me="Me").events
            if e.kind == "mentioned"] == [("Bo Lee", "Ana Diaz"), ("Bo Lee", "S0GRP")]
    dm = [slack.msg({"ts": "1.0", "user": "U0BO", "text": "got a sec?"}), slack.msg({"ts": "2.0", "user": "U0ME", "text": "yes"})]
    assert [(e.actor, e.detail) for e in to_record("slack:D1/2026-10-01", "DM: Bo Lee", dm, thread=False, me="Me").events
            if e.kind == "mentioned"] == [("Bo Lee", "Me"), ("Me", "Bo Lee")]
