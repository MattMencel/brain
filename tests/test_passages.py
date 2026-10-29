import itertools

from brain.passages import WINDOW_CHARS, chunk, excerpt


def body(*turns: str) -> str:
    return "\n\n".join(turns)


def test_short_body_is_one_passage():
    assert chunk(body("USER: hi", "CLAUDE: hello")) == ["USER: hi\n\nCLAUDE: hello"]


def test_windows_overlap_by_one_turn_and_cover_every_turn():
    turns = [f"USER: q{i} " + "x" * 1500 if i % 2 == 0 else f"CLAUDE: a{i} " + "y" * 1500 for i in range(8)]
    parts = chunk(body(*turns))
    assert len(parts) > 1
    assert all(len(p) <= WINDOW_CHARS + 2000 for p in parts)
    for prev, nxt in itertools.pairwise(parts):
        assert prev.split("\n\n")[-1] == nxt.split("\n\n")[0]
    joined = "\n\n".join(parts)
    assert all(t in joined for t in turns)


def test_oversized_turn_is_split_hard():
    parts = chunk(body("USER: go", "CLAUDE: " + "z" * (WINDOW_CHARS * 2 + 10)))
    assert all(len(p) <= WINDOW_CHARS + 20 for p in parts)
    assert sum(p.count("z") for p in parts) >= WINDOW_CHARS * 2


def test_excerpt_keeps_lines_around_query_terms():
    text = "\n".join(f"line {i}" for i in range(30)) + "\nCLAUDE: run npm update form-data --package-lock-only\n" + "tail"
    lines = excerpt(text, "lockfile form-data")
    assert any("form-data" in line for line in lines)
    assert "line 0" not in lines
