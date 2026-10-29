from brain.connectors.jira import activity, adf_text, jql, old_keys, to_record


def _doc(*blocks):
    return {"type": "doc", "content": list(blocks)}


def _p(*parts):
    return {"type": "paragraph", "content": list(parts)}


def _t(text):
    return {"type": "text", "text": text}


def test_adf_text_keeps_blocks_lists_code_and_mentions():
    doc = _doc(
        _p(_t("Pods restart "), {"type": "mention", "attrs": {"text": "@Ana"}}),
        {"type": "bulletList", "content": [{"type": "listItem", "content": [_p(_t("check probes"))]}]},
        {"type": "codeBlock", "content": [_t("kubectl get pods")]},
    )
    assert adf_text(doc) == "Pods restart @Ana\n- check probes\n```\nkubectl get pods\n```\n"


def test_to_record_builds_body_with_status_and_comments_and_redacts():
    issue = {"key": "PROJ-7", "fields": {
        "summary": "Rotate the db password",
        "created": "2026-08-01T10:00:00.000+0000",
        "updated": "2026-09-01T10:00:00.000+0000",
        "project": {"key": "PROJ"},
        "issuetype": {"name": "Task"},
        "status": {"name": "Done"},
        "reporter": {"displayName": "Ana"},
        "assignee": None,
        "description": _doc(_p(_t("password=hunter22 leaked"))),
    }}
    comments = [{"created": "2026-09-02T09:00:00.000+0000", "author": {"displayName": "Bo"}, "body": _doc(_p(_t("rotated")))}]
    r = to_record(issue, comments)
    assert r.ref == "jira:PROJ-7" and r.project == "PROJ" and r.author == "Ana"
    assert "Task · Status: Done · Reporter: Ana · Assignee: none" in r.body
    assert "[2026-09-02] Bo: rotated" in r.body
    assert "hunter22" not in r.body and "hunter22" not in r.summary
    assert not r.summarize


def test_jql_scopes_projects_plus_own_tickets():
    assert jql(["A", "B"], "-30d") == (
        "(project in (A, B) OR reporter = currentUser() OR assignee = currentUser() OR watcher = currentUser())"
        " AND updated >= -30d ORDER BY updated DESC"
    )
    assert jql([], "-30d").startswith("(reporter = currentUser()")


def _history(when, field, account="atlassian", **item):
    return {"created": when, "author": {"accountType": account}, "items": [{"field": field, **item}]}


def test_activity_ignores_bots_and_fields_that_do_not_mean_work():
    issue = {"fields": {"created": "2022-10-14T00:00:00.000+0000"}, "changelog": {"histories": [
        _history("2026-08-13T00:00:00.000+0000", "Customer Name (Labels)"),  # bulk edit by a person
        _history("2026-09-01T00:00:00.000+0000", "status", account="app"),  # automation
        _history("2023-07-28T00:00:00.000+0000", "status"),
    ]}}
    comments = [
        {"created": "2022-11-03T00:00:00.000+0000", "author": {"accountType": "atlassian"}},
        {"created": "2026-10-01T00:00:00.000+0000", "author": {"accountType": "app"}},
    ]
    assert activity(issue, comments).date().isoformat() == "2023-07-28"


def test_moved_ticket_replaces_its_old_key():
    issue = {"changelog": {"histories": [_history("2023-02-27T00:00:00.000+0000", "Key", fromString="OLD-12", toString="PROJ-69")]}}
    assert old_keys(issue) == ["OLD-12"]


def test_events_cover_creation_comments_and_transitions_and_flag_bots():
    issue = {"key": "PROJ-8", "fields": {
        "summary": "Fix the canary", "created": "2026-08-01T10:00:00.000+0000", "project": {"key": "PROJ"},
        "reporter": {"displayName": "Ana"}, "status": {"name": "Done"},
    }, "changelog": {"histories": [
        {"created": "2026-08-03T10:00:00.000+0000", "author": {"displayName": "Automation", "accountType": "app"},
         "items": [{"field": "status", "fromString": "In Progress", "toString": "Done"}]},
        {"created": "2026-08-02T10:00:00.000+0000", "author": {"displayName": "Bo"},
         "items": [{"field": "assignee", "toString": "Bo"}, {"field": "labels", "toString": "x"}]},
    ]}}
    comments = [{"created": "2026-08-02T12:00:00.000+0000", "author": {"displayName": "Bo"},
                 "body": _doc(_p(_t("probe timeout raised")))}]
    r = to_record(issue, comments)
    assert [(e.actor, e.kind, e.detail, e.bot) for e in r.events] == [
        ("Ana", "created", None, False),
        ("Bo", "assigned", "Bo", False),
        ("Bo", "commented", "probe timeout raised", False),
        ("Automation", "status", "In Progress → Done", True),
    ]
    assert "## History\n\n[2026-08-02] Bo: Assignee Bo\n[2026-08-03] Automation: Status In Progress → Done" in r.body


def test_changelog_pages_when_search_embeds_part_of_it():
    from brain.connectors.jira import _Client

    class Fake(_Client):
        def __init__(self):
            self.calls = []

        def get(self, path, **params):
            self.calls.append(params["startAt"])
            start = params["startAt"]
            return {"values": [{"n": i} for i in range(start, min(start + 2, 3))], "isLast": start + 2 >= 3}

    c = Fake()
    assert c.changelog({"key": "P-1", "changelog": {"total": 1, "histories": [{"n": 0}]}}) == [{"n": 0}]
    assert c.calls == []
    assert len(c.changelog({"key": "P-1", "changelog": {"total": 3, "histories": [{"n": 0}]}})) == 3
    assert c.calls == [0, 2]


def test_mentions_in_the_description_and_comments_are_events():
    mention = {"type": "mention", "attrs": {"id": "acc-1", "text": "@Matt M"}}
    issue = {"key": "PROJ-9", "fields": {
        "summary": "s", "created": "2026-08-01T10:00:00.000+0000", "project": {"key": "PROJ"},
        "reporter": {"displayName": "Ana"}, "status": {"name": "To Do"}, "description": _doc(_p(mention)),
    }}
    comments = [{"created": "2026-08-02T12:00:00.000+0000", "author": {"displayName": "Bo"}, "body": _doc(_p(_t("ping "), mention))}]
    assert [(e.actor, e.detail) for e in to_record(issue, comments).events if e.kind == "mentioned"] == [
        ("Ana", "Matt M"), ("Bo", "Matt M")]
