from datetime import date

from brain.connectors.github import prs, to_record

USER = {"__typename": "User", "login": "ana"}
BOT = {"__typename": "Bot", "login": "coderabbitai"}


def _pr(**over):
    pr = {
        "number": 7, "title": "Raise the probe timeout", "body": "token=hunter22abc fixes flaky canary", "state": "MERGED",
        "createdAt": "2026-08-01T10:00:00Z", "mergedAt": "2026-08-03T10:00:00Z", "baseRefName": "main", "headRefName": "probe",
        "repository": {"nameWithOwner": "acme/infra", "name": "infra"}, "author": USER, "mergeCommit": {"oid": "abc123"},
        "timelineItems": {"nodes": [
            {"__typename": "ClosedEvent", "createdAt": "2026-08-03T10:00:00Z", "actor": USER},
            {"__typename": "MergedEvent", "createdAt": "2026-08-03T10:00:00Z", "actor": USER},
        ]},
        "comments": {"nodes": [
            {"createdAt": "2026-08-01T11:00:00Z", "body": "## Walkthrough\nlong bot summary", "author": BOT},
            {"createdAt": "2026-08-02T09:00:00Z", "body": "why 5s?", "author": {"__typename": "User", "login": "bo"}},
        ]},
        "reviews": {"nodes": [
            {"createdAt": "2026-08-02T12:00:00Z", "state": "APPROVED", "body": "", "author": {"__typename": "User", "login": "bo"}},
            {"createdAt": "2026-08-02T12:30:00Z", "state": "PENDING", "body": "", "author": USER},
        ]},
        "reviewThreads": {"nodes": [{"path": "probe.yaml", "line": 4, "comments": {"nodes": [
            {"createdAt": "2026-08-02T11:00:00Z", "body": "use a var", "author": {"__typename": "User", "login": "bo"}}]}}]},
    }
    return pr | over


def test_to_record_keeps_people_in_the_body_and_everyone_in_events():
    r = to_record(_pr())
    assert r.ref == "github:acme/infra#7" and r.project == "infra" and r.author == "ana"
    assert "Merged as git:infra@abc123" in r.body and "probe → main" in r.body
    assert "[2026-08-02] bo on probe.yaml:4: use a var" in r.body and "[2026-08-02] bo: APPROVED" in r.body
    assert "Walkthrough" not in r.body and "hunter22abc" not in r.body
    assert [(e.actor, e.kind, e.detail, e.bot) for e in r.events] == [
        ("ana", "opened", None, False),
        ("coderabbitai", "commented", "## Walkthrough", True),
        ("bo", "commented", "why 5s?", False),
        ("bo", "commented", "use a var", False),
        ("bo", "reviewed", "approved", False),
        ("ana", "merged", None, False),
    ]
    assert r.ts.isoformat() == "2026-08-03T10:00:00+00:00" and not r.summarize


def test_closed_unmerged_pr_gets_a_closed_event():
    r = to_record(_pr(state="CLOSED", mergeCommit=None))
    assert r.events[-1].kind == "closed" and "Merged as" not in r.body


def test_search_halves_a_range_over_the_result_cap_and_pages():
    calls = []

    def run(query, q, cursor):
        calls.append((q, cursor))
        start, end = q.split("updated:")[1].split("..")
        big = start == "2026-08-01" and end == "2026-08-07"
        page2 = cursor is None and start == "2026-08-05"
        nodes = [{"n": f"{start}/{cursor}"}]
        return {"rateLimit": {"remaining": 5000, "resetAt": "2026-08-01T00:00:00Z"},
                "search": {"issueCount": 1500 if big else 30, "nodes": nodes,
                           "pageInfo": {"hasNextPage": page2, "endCursor": "c2"}}}

    got = list(prs("involves:@me", date(2026, 8, 1), date(2026, 8, 7), run))
    assert [q.split("updated:")[1] for q, c in calls] == [
        "2026-08-01..2026-08-07", "2026-08-01..2026-08-04", "2026-08-05..2026-08-07", "2026-08-05..2026-08-07"]
    assert got == [{"n": "2026-08-01/None"}, {"n": "2026-08-05/None"}, {"n": "2026-08-05/c2"}]


def test_a_transient_error_is_retried_before_the_slice_fails(monkeypatch):
    from brain.connectors import github

    monkeypatch.setattr(github.time, "sleep", lambda s: None)
    errors = ["stream error: stream ID 1; CANCEL; received from peer"]

    def run(query, q, cursor):
        if errors:
            raise github.GhError(errors.pop())
        return {"rateLimit": {"remaining": 5000, "resetAt": "2026-08-01T00:00:00Z"},
                "search": {"issueCount": 1, "nodes": [{"n": 1}], "pageInfo": {"hasNextPage": False, "endCursor": None}}}

    assert list(prs("involves:@me", date(2026, 5, 5), date(2026, 5, 11), run)) == [{"n": 1}]


def test_mentions_of_logins_and_teams_but_not_emails_or_code():
    pr = _pr(body="cc @matt and @acme/infra, mail ops@acme.io, `@types/node`")
    assert [(e.actor, e.detail) for e in to_record(pr).events if e.kind == "mentioned"] == [
        ("ana", "matt"), ("ana", "acme/infra")]
