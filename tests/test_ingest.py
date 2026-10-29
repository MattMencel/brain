from brain import config
from brain.cli import _scope_changed


def test_a_config_change_after_the_last_success_forces_a_full_read():
    before = config.digest({"repos": ["acme/a"]})
    after = config.digest({"repos": ["acme/a", "acme/b"]})
    assert _scope_changed(before, after)
    assert not _scope_changed(after, config.digest({"repos": ["acme/a", "acme/b"]}))
    assert not _scope_changed(None, after)  # runs recorded before hashes existed adopt the current config


def test_a_connector_already_running_is_skipped(monkeypatch):
    import psycopg
    import pytest

    from brain import cli, connectors, db

    try:
        holder = db.connect()
    except psycopg.OperationalError:
        pytest.skip("no database")
    holder.execute("select pg_advisory_lock(hashtext('brain:fake'))")
    monkeypatch.setattr(connectors, "registry", lambda: pytest.fail("fetched while another run held the lock"))
    try:
        assert cli._ingest("fake", None) == 0
    finally:
        holder.close()


def test_events_json_and_a_warning_when_n_truncates(monkeypatch, capsys):
    import json
    from argparse import Namespace
    from datetime import UTC, datetime

    from brain import cli, config, db

    row = (datetime(2026, 10, 1, tzinfo=UTC), "jira", "status", "Matt M", "To Do → Done", "jira:X-1", "X-1 t")

    class Conn:
        def execute(self, sql, params):
            assert params["n"] == 2  # one past -n, to tell a cut from an exact fit
            return self

        def fetchall(self):
            return [row, row]

    monkeypatch.setattr(db, "connect", Conn)
    monkeypatch.setattr(config, "load", lambda: {"people": {"me": {"aliases": ["Matt M"]}}})
    args = Namespace(actor="me", since=2, ref=None, kind=None, to=None, bots=False, n=1, json=True)
    assert cli.events(args) == 0
    out = capsys.readouterr()
    assert json.loads(out.out) == [{"ts": "2026-10-01T00:00:00+00:00", "source": "jira", "kind": "status", "actor": "me",
                                    "detail": "To Do → Done", "ref": "jira:X-1", "title": "X-1 t"}]
    assert "first 1" in out.err
