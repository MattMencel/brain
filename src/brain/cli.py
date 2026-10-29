import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from brain import config, connectors, db, install, passages, summarize
from brain.connectors import Context, Event, Failed, Record
from brain.redact import redact

HALF_LIFE_DAYS = 90
STALE_HOURS = 2

# A record scores on its own fields plus its best topic and best passage, so a side fix inside a long
# session ranks on its own topic title and summary.
SEARCH_SQL = f"""
with rec as (
    select ref, pdb.score(id) as s from records
    where title ||| %(q)s::text::pdb.boost(3) or summary ||| %(q)s::text::pdb.boost(3)
       or questions ||| %(q)s::text::pdb.boost(2) or atoms ||| %(q)s::text::pdb.boost(2) or body ||| %(q)s
), pas as (
    select ref, n, text, pdb.score(id) as s from passages where text ||| %(q)s
), best as (
    select distinct on (ref) ref, n, text, s from pas order by ref, s desc
), top as (
    select ref, n, title, summary, pdb.score(id) as s from topics
    where title ||| %(q)s::text::pdb.boost(3) or summary ||| %(q)s::text::pdb.boost(3)
       or questions ||| %(q)s::text::pdb.boost(2) or atoms ||| %(q)s::text::pdb.boost(2)
), best_top as (
    select distinct on (ref) ref, n, title, summary, s from top order by ref, s desc
)
select r.ref, r.ts, r.source, r.project, r.author, r.title, r.summary, r.atoms, best.n, best.text,
       best_top.n, best_top.title, best_top.summary,
       (coalesce(rec.s, 0) + coalesce(best.s, 0) + coalesce(best_top.s, 0))
         * power(0.5, extract(epoch from now() - r.ts) / 86400 / {HALF_LIFE_DAYS}) as score
from records r
left join rec on rec.ref = r.ref
left join best on best.ref = r.ref
left join best_top on best_top.ref = r.ref
where (rec.ref is not null or best.ref is not null or best_top.ref is not null)
  and (%(source)s::text is null or r.source = %(source)s)
  and (%(project)s::text is null or r.project ilike '%%' || %(project)s || '%%')
  and (%(since)s::int is null or r.ts > now() - make_interval(days => %(since)s))
order by score desc
limit %(n)s
"""


def _events_hash(events: list[Event]) -> str:
    return hashlib.sha256(json.dumps(
        [(e.ts.isoformat(), e.actor, e.kind, e.detail, e.bot) for e in events]
    ).encode()).hexdigest()


def _write_events(conn, ref: str, events: list[Event]) -> None:
    conn.execute("delete from events where ref = %s", (ref,))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into events (ref, ts, actor, kind, detail, bot) values (%s, %s, %s, %s, %s, %s)",
            [(ref, e.ts, e.actor, e.kind, redact(e.detail) if e.detail else None, e.bot) for e in events],
        )
    conn.execute("update records set events_hash = %s where ref = %s", (_events_hash(events), ref))


def _upsert(conn, source: str, r: Record, digest: str, meta: dict | None) -> None:
    topics = meta["topics"] if meta else []
    conn.execute(
        """insert into records (source, ref, ts, project, title, summary, questions, atoms, body,
                                content_hash, source_mtime, summarizer_version, author)
           values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           on conflict (ref) do update set ts = excluded.ts, project = excluded.project, title = excluded.title,
               summary = excluded.summary, questions = excluded.questions, atoms = excluded.atoms,
               body = excluded.body, content_hash = excluded.content_hash,
               source_mtime = excluded.source_mtime, summarizer_version = excluded.summarizer_version,
               author = excluded.author, ingested_at = now()""",
        (
            source, r.ref, r.ts, r.project,
            meta["title"] + (f" [{r.title}]" if r.title else "") if meta else r.title,
            meta["summary"] if meta else r.summary,
            "\n".join(q for t in topics for q in t["questions"]) or None,
            "\n".join(a for t in topics for a in t["atoms"]) or None, r.body,
            digest, r.mtime, summarize.VERSION if meta else None, r.author,
        ),
    )
    for old in r.replaces:
        conn.execute("delete from records where ref = %s", (old,))
    _write_events(conn, r.ref, r.events)
    if not meta:
        return
    passages.write(conn, r.ref, r.body)
    conn.execute("delete from topics where ref = %s", (r.ref,))
    with conn.cursor() as cur:
        cur.executemany(
            """insert into topics (ref, n, title, summary, questions, atoms, turn_from, turn_to)
               values (%s, %s, %s, %s, %s, %s, %s, %s)""",
            [(r.ref, n, t["title"], t["summary"], "\n".join(t["questions"]), "\n".join(t["atoms"]),
              t["turn_from"], t["turn_to"]) for n, t in enumerate(topics, 1)],
        )


def migrate(args) -> int:
    conn = db.connect()
    db.migrate(conn)
    print(f"passages backfilled for {passages.backfill(conn)} records")
    return 0


def ingest(args) -> int:
    """One connector, or every connector enabled in the config."""
    names = [args.connector] if args.connector else config.enabled(list(connectors.registry()))
    return max((_ingest(name, args) for name in names), default=0)


def _scope_changed(stored: str | None, current: str) -> bool:
    """A connector's incremental window only covers what it was configured for at its last success, so a new
    repo, project or channel would never get its history. None: recorded before config hashes existed."""
    return stored is not None and stored != current


def _ingest(name: str, args) -> int:
    """A scheduled run and a manual one would fetch, and pay for, the same items twice. The lock dies with its
    connection, so a crashed run never leaves it held."""
    with db.connect() as lock:
        if not lock.execute("select pg_try_advisory_lock(hashtext(%s))", (f"brain:{name}",)).fetchone()[0]:
            print(f"{name}: already running, skipped", flush=True)
            return 0
        return _run(name, args)


def _run(name: str, args) -> int:
    conn = db.connect()
    cfg = config.connector(name)
    cfg_hash = config.digest(cfg)  # before --since, which is one run's window, not a config change
    if args.since:
        cfg = {**cfg, "since_days": args.since}
    row = conn.execute("select config_hash from connector_runs where connector = %s", (name,)).fetchone()
    full = args.full or (row is not None and _scope_changed(row[0], cfg_hash))
    if full and not args.full:
        print(f"{name}: config changed since the last success; reading the whole window", flush=True)
    fetch = connectors.registry()[name].fetch
    # Summarized records from an older summarizer count as unseen, so a VERSION bump re-summarizes them.
    seen = {ref: (h, v, eh) for ref, h, v, eh in conn.execute(
        "select ref, content_hash, summarizer_version, events_hash from records where source = %s", (name,)
    ).fetchall()}
    todo, failed, evented = [], 0, 0
    for item in fetch(Context(conn, cfg, full=full)):
        if isinstance(item, Failed):
            failed += 1
            print(f"FAILED {item.label}: {item.error}", file=sys.stderr, flush=True)
            db.record_run(conn, name, f"{item.label}: {item.error}")
            continue
        digest = hashlib.sha256(item.body.encode()).hexdigest()
        h, v, eh = seen.get(item.ref, (None, None, None))
        if h == digest and (not item.summarize or v == summarize.VERSION):
            # Same body: events can still change (a status transition), and never cost a summarizer call.
            if not args.dry_run:
                if eh != _events_hash(item.events):
                    _write_events(conn, item.ref, item.events)
                    evented += 1
                if item.mtime is not None:
                    conn.execute("update records set source_mtime = %s where ref = %s", (item.mtime, item.ref))
            continue
        todo.append((item, digest))
        if args.limit and sum(1 for r, _ in todo if r.summarize) >= args.limit:
            break
    if args.dry_run:
        for r, _digest in todo:
            print(f"would {'summarize' if r.summarize else 'store'} {r.ref} ({len(r.body)} chars) {r.title or ''}")
        print(f"{name}: stored=0 failed={failed} api_equiv_cost=$0.0000")
        return 0

    def run(item):
        r, _digest = item
        if not r.summarize:
            return item, None, None
        try:
            return item, summarize.summarize(r.body, r.summarize), None
        except Exception as e:  # noqa: BLE001 -- one record's summarizer failure is reported, not fatal
            return item, None, e
        finally:
            if args.sleep:
                time.sleep(args.sleep)

    done = cost = 0
    with ThreadPoolExecutor(args.workers) as ex:
        for (r, digest), meta, err in ex.map(run, todo):
            if err:
                failed += 1
                print(f"FAILED {r.ref}: {err}", file=sys.stderr, flush=True)
                db.record_run(conn, name, f"{r.ref}: {err}")
                continue
            _upsert(conn, name, r, digest, meta)
            done += 1
            if meta:
                cost += meta["_usage"]["cost_usd"] or 0
                print(f"{r.ref} {len(r.body)}ch {meta['_usage']['ms']}ms {len(meta['topics'])} topics  {meta['title']}", flush=True)
    if failed == 0:
        db.record_run(conn, name, config_hash=cfg_hash)
    print(f"{name}: stored={done} events_only={evented} failed={failed} api_equiv_cost=${cost:.4f}")
    return 1 if failed else 0


def check(args) -> int:
    """Notify when a connector has not succeeded recently; the launchd job runs this after ingest."""
    stale = db.connect().execute(
        "select connector from connector_runs where last_success is null or last_success < now() - make_interval(hours => %s)",
        (STALE_HOURS,),
    ).fetchall()
    if stale:
        names = ", ".join(r[0] for r in stale)
        if platform.system() == "Darwin":
            subprocess.run(["osascript", "-e", f'display notification "Stale connectors: {names}. Run brain status." with title "brain"'], check=False)
        elif shutil.which("notify-send"):
            subprocess.run(["notify-send", "brain", f"Stale connectors: {names}. Run brain status."], check=False)
        print(f"stale: {names}")
        return 1
    return 0


def search(args) -> int:
    conn = db.connect()
    rows = conn.execute(SEARCH_SQL, {
        "q": args.query, "source": args.source, "project": args.project, "since": args.since, "n": args.n,
    }).fetchall()
    if args.json:
        print(json.dumps([
            {"ref": ref, "ts": ts.isoformat(), "source": source, "project": project, "author": author,
             "title": title, "summary": summary, "atoms": (atoms or "").splitlines(),
             "passage": n, "excerpt": passages.excerpt(text, args.query) if text else [],
             "topic": tn, "topic_title": tt, "topic_summary": ts_, "score": round(score, 3)}
            for ref, ts, source, project, author, title, summary, atoms, n, text, tn, tt, ts_, score in rows
        ], indent=2))
        return 0 if rows else 1
    for ref, ts, source, project, author, title, summary, _atoms, n, text, tn, tt, ts_, score in rows:
        by = f" by {author}" if author else ""
        print(f"## {ref}  [{ts:%Y-%m-%d}] {source} {project or ''}{by}  score={score:.2f}")
        summary = summary or ""
        print(f"   {title}\n   {summary[:200]}{'…' if len(summary) > 200 else ''}")
        if tn:
            print(f"   topic {tn} (brain show {ref} --topic {tn}): {tt}\n     {ts_}")
        if text:
            print(f"   passage {n} (brain show {ref} --passage {n}):")
            for line in passages.excerpt(text, args.query):
                print(f"   | {line}")
        print()
    return 0 if rows else 1


def show(args) -> int:
    conn = db.connect()
    if args.passage:
        row = conn.execute("select text from passages where ref = %s and n = %s", (args.ref, args.passage)).fetchone()
        if not row:
            print(f"no passage {args.passage} in {args.ref}", file=sys.stderr)
            return 1
        print(row[0])
        return 0
    if args.topic:
        row = conn.execute(
            """select t.title, t.summary, t.atoms, t.turn_from, t.turn_to, r.body
               from topics t join records r using (ref) where t.ref = %s and t.n = %s""",
            (args.ref, args.topic),
        ).fetchone()
        if not row:
            print(f"no topic {args.topic} in {args.ref}", file=sys.stderr)
            return 1
        title, summary, atoms, lo, hi, body = row
        turns = "\n\n".join(passages.turns(body)[lo - 1 : hi])
        print(f"# {title}\nturns {lo}-{hi}\n\n{summary}\n\n## Atoms\n{atoms}\n\n## Transcript\n{turns}")
        return 0
    row = conn.execute(
        "select title, ts, project, summary, atoms, body from records where ref = %s", (args.ref,)
    ).fetchone()
    if not row:
        print(f"no record {args.ref}", file=sys.stderr)
        return 1
    title, ts, project, summary, atoms, body = row
    print(f"# {title}\n{ts:%Y-%m-%d %H:%M} {project or ''}\n\n{summary}\n\n## Atoms\n{atoms}\n\n## Body\n{body}")
    return 0


def events(args) -> int:
    """Who did what, when. [people] in brain.toml maps each source's names for one person to a canonical name."""
    people = {k.lower(): [a.lower() for a in v.get("aliases", [])] + [k.lower()]
              for k, v in config.load().get("people", {}).items()}
    canonical = {alias: k for k, aliases in people.items() for alias in aliases}
    actors = people.get(args.actor.lower(), [args.actor.lower()]) if args.actor else None
    to = people.get(args.to.lower(), [args.to.lower()]) if args.to else None
    since = args.since if args.since is not None else (None if args.ref else 7)
    rows = db.connect().execute(
        """select e.ts, r.source, e.kind, e.actor, e.detail, e.ref, r.title from events e join records r using (ref)
           where (%(since)s::int is null or e.ts > now() - make_interval(days => %(since)s))
             and (%(actors)s::text[] is null or lower(e.actor) = any(%(actors)s))
             and (%(ref)s::text is null or e.ref = %(ref)s)
             and (%(kind)s::text is null or e.kind = %(kind)s)
             and (%(to)s::text[] is null or (e.kind = 'mentioned' and lower(e.detail) = any(%(to)s)))
             and (%(bots)s or not e.bot)
           order by e.ts limit %(n)s""",
        {"since": since, "actors": actors, "ref": args.ref, "kind": args.kind, "to": to, "bots": args.bots,
         "n": args.n + 1 if args.n else None},
    ).fetchall()
    if args.n and len(rows) > args.n:
        rows = rows[:args.n]
        print(f"brain events: showing the first {args.n}; raise -n for the rest", file=sys.stderr)
    if args.json:
        print(json.dumps([{"ts": ts.isoformat(), "source": source, "kind": kind, "actor": canonical.get(actor.lower(), actor),
                           "detail": detail, "ref": ref, "title": title}
                          for ts, source, kind, actor, detail, ref, title in rows], indent=1))
        return 0 if rows else 1
    for ts, source, kind, actor, detail, ref, title in rows:
        who = canonical.get(actor.lower(), actor)
        detail = detail if not detail or len(detail) <= 40 else detail[:39] + "…"
        print(f"{ts.astimezone():%Y-%m-%d %H:%M}  {source:<8} {kind:<10} {who[:20]:<20} {detail or '':<40} {ref}  {title or ''}")
    return 0 if rows else 1


def status(args) -> int:
    conn = db.connect()
    counts = {}
    for source, n, newest in conn.execute(
        "select source, count(*), max(ts) from records group by 1 order by 1"
    ).fetchall():
        counts[source] = (n, newest)
    runs = {r[0]: r[1:] for r in conn.execute(
        "select connector, last_success, last_error, last_error_at from connector_runs"
    ).fetchall()}
    now = datetime.now(UTC)
    color = sys.stdout.isatty()
    paint = lambda code, s: f"\033[{code}m{s}\033[0m" if color else s

    print(paint("1", f"brain · {db.dsn()}") + "\n")
    print(paint("2", f"{'source':<10} {'records':>8}   {'newest':<16}   {'last run':<9}  health"))
    errors = []
    for name in sorted(set(counts) | set(runs)):
        n, newest = counts.get(name, (0, None))
        ok, err, err_at = runs.get(name, (None, None, None))
        if err and (ok is None or err_at > ok):
            health = paint("31", "✗ error")
            errors.append(f"{name}: {err_at:%Y-%m-%d %H:%M}  {err[:160]}")
        elif ok is None or now - ok > timedelta(hours=STALE_HOURS):
            health = paint("33", "⚠ stale")
        else:
            health = paint("32", "✓ ok")
        newest_s = f"{newest:%Y-%m-%d %H:%M}" if newest else "—"
        print(f"{name:<10} {n:>8,}   {newest_s:<16}   {_ago(now, ok):<9}  {health}")
    for line in errors:
        print("\n" + paint("31", line))
    return 0


def _ago(now: datetime, then: datetime | None) -> str:
    if then is None:
        return "never"
    minutes = int((now - then).total_seconds() // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    if minutes < 48 * 60:
        return f"{minutes // 60}h ago"
    return f"{minutes // 1440}d ago"


def main() -> None:
    p = argparse.ArgumentParser(prog="brain")
    sub = p.add_subparsers(required=True)

    sub.add_parser("migrate").set_defaults(fn=migrate)
    sub.add_parser("install", help="config, database, schema, secrets, schedule and skill; safe to re-run").set_defaults(fn=install.install)
    sub.add_parser("config", help="write ~/.config/brain/brain.toml from the example if absent").set_defaults(fn=install.write_config)
    sub.add_parser("db-up", help="start the docker database (mode = docker) and check the connection").set_defaults(fn=install.db_up)
    a = sub.add_parser("auth", help="store a connector's token in the OS keyring")
    a.add_argument("connector")
    a.set_defaults(fn=install.auth)
    sched = sub.add_parser("schedule")
    sched.add_subparsers(required=True).add_parser("install", help="launchd (macOS) or systemd user timer (Linux)").set_defaults(fn=install.schedule_install)
    sub.add_parser("link-skill", help="link the brain skill into ~/.claude/skills").set_defaults(fn=install.link_skill)

    ing = sub.add_parser("ingest", help="pull new records; no connector = every connector enabled in the config")
    ing.add_argument("connector", nargs="?", choices=list(connectors.registry()))
    ing.add_argument("--limit", type=int, help="max summarizer calls this run")
    ing.add_argument("--sleep", type=float, default=0, help="seconds between summarizer calls (throttle)")
    ing.add_argument("--workers", type=int, default=1, help="concurrent summarizer calls")
    ing.add_argument("--dry-run", action="store_true")
    ing.add_argument("--full", action="store_true", help="re-read the whole window, ignoring the last run and file mtimes")
    ing.add_argument("--since", type=int, help="days a --full or first read covers this run, in place of since_days")
    ing.set_defaults(fn=ingest)

    sub.add_parser("check").set_defaults(fn=check)

    q = sub.add_parser("search")
    q.add_argument("query")
    q.add_argument("--source")
    q.add_argument("--project")
    q.add_argument("--since", type=int, help="days")
    q.add_argument("-n", type=int, default=8)
    q.add_argument("--json", action="store_true", help="full results as a JSON array")
    q.set_defaults(fn=search)

    sh = sub.add_parser("show")
    sh.add_argument("ref")
    sh.add_argument("--passage", type=int, help="print only this passage of a session")
    sh.add_argument("--topic", type=int, help="print one topic: its summary, atoms and transcript turns")
    sh.set_defaults(fn=show)

    ev = sub.add_parser("events", help="who did what, when, across every source")
    ev.add_argument("--actor", help="a name, or a [people] key such as me")
    ev.add_argument("--since", type=int, help="days (default 7, or all time with --ref)")
    ev.add_argument("--ref")
    ev.add_argument("--kind")
    ev.add_argument("--to", help="mentioned events addressed to a name or [people] key, DMs included")
    ev.add_argument("--bots", action="store_true", help="include bot and automation events")
    ev.add_argument("-n", type=int, help="at most this many, oldest first (default: all)")
    ev.add_argument("--json", action="store_true", help="a JSON array: ts, source, kind, actor, detail, ref, title")
    ev.set_defaults(fn=events)

    sub.add_parser("status").set_defaults(fn=status)

    args = p.parse_args()
    sys.exit(args.fn(args))
