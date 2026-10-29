"""Conversation bodies -> overlapping passages, so a short fix inside a long session scores on its own."""

import re

import psycopg

WINDOW_CHARS = 4_000
EXCERPT_LINES = 15

# Sessions are USER:/CLAUDE: turns; Slack messages start "[Name YYYY-MM-DD HH:MM] ".
_TURN = re.compile(r"\n\n(?=USER: |CLAUDE: |\[[^\]\n]+ \d{4}-\d\d-\d\d \d\d:\d\d\] )")


def turns(body: str) -> list[str]:
    """Turns (session prompts and replies, or Slack messages) in order; topic turn ranges index into this list (1-based)."""
    return _TURN.split(body)


def chunk(body: str) -> list[str]:
    """Pack whole turns into ~WINDOW_CHARS windows; each window repeats the previous one's last turn."""
    turns = []
    for turn in _TURN.split(body):
        turns += [turn[i : i + WINDOW_CHARS] for i in range(0, len(turn), WINDOW_CHARS)] or [turn]
    out, start = [], 0
    while start < len(turns):
        end, size = start, 0
        while end < len(turns) and (end == start or size + len(turns[end]) <= WINDOW_CHARS):
            size += len(turns[end]) + 2
            end += 1
        out.append("\n\n".join(turns[start:end]))
        if end == len(turns):
            break
        start = end - 1 if end - 1 > start else end
    return out


def write(conn: psycopg.Connection, ref: str, body: str) -> None:
    conn.execute("delete from passages where ref = %s", (ref,))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into passages (ref, n, text) values (%s, %s, %s)",
            [(ref, n, text) for n, text in enumerate(chunk(body), 1)],
        )


def backfill(conn: psycopg.Connection) -> int:
    rows = conn.execute(
        """select ref, body from records r where source = 'sessions'
           and not exists (select 1 from passages p where p.ref = r.ref)"""
    ).fetchall()
    for ref, body in rows:
        write(conn, ref, body)
    return len(rows)


def excerpt(text: str, query: str) -> list[str]:
    """Lines of a passage around the query terms, so search shows evidence rather than a summary."""
    terms = [t.lower() for t in re.findall(r"\w[\w.:/-]{2,}", query)]
    lines = [line for line in text.splitlines() if line.strip()]
    hits = [i for i, line in enumerate(lines) if any(t in line.lower() for t in terms)]
    keep = sorted({j for i in hits for j in range(i - 2, i + 3) if 0 <= j < len(lines)}) or list(range(min(10, len(lines))))
    return [lines[j][:200] for j in keep[:EXCERPT_LINES]]
