import os

from brain.connectors.sessions import discover


def test_resumed_session_copied_into_another_project_yields_only_newest(tmp_path):
    # Resuming a session in another directory copies its transcript there; both share the session id.
    old = tmp_path / "proj-a" / "sid.jsonl"
    new = tmp_path / "proj-b" / "sid.jsonl"
    for path, mtime in ((old, 1_000), (new, 2_000)):
        path.parent.mkdir()
        path.write_text("{}\n")
        os.utime(path, (mtime, mtime))
    assert list(discover(tmp_path, now=10_000)) == [new]
