"""Build the retrieval eval inputs: a text mirror of brain's session bodies for the rg arm,
and candidate cases written by Opus from randomly sampled raw transcripts.

Opus sees only the condensed transcript, never brain's summary or atoms, so the
questions don't inherit the indexer's vocabulary.
"""

import json
import os
import random
import re
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from brain import db

# Cases, corpus and results quote work content, so they live outside the repo.
DATA = Path(os.environ.get("BRAIN_EVAL_DIR", Path.home() / ".local/share/brain/eval"))
HILL = DATA / "hillclimb"
FLOW = HILL / "retrieval"
CORPUS = FLOW / "corpus"
SEED = 20261026
SAMPLE = 45  # oversampled; Opus marks sessions with nothing reusable as unusable
MIN_BODY = 5_000
JIRA_SAMPLE, JIRA_MIN_BODY = 18, 1_500
EXCLUDE = set(os.environ.get("BRAIN_EVAL_EXCLUDE", "").split())  # session ids to leave out, such as the one that built the eval

SYSTEM = """You write test cases for a personal search tool over one engineer's past Claude Code sessions.
Given one session transcript, pick the single most reusable concrete fact in it: a fix, a root cause,
a working command, or a decision with its reason. Then write the question the engineer would type
months later when they hit the same problem again.

Rules for the question:
- Describe the symptom or the goal the way someone who forgot the answer would. Do not include the
  answer, its distinctive keywords, ticket numbers, PR numbers, or exact error strings that only appear
  next to the answer.
- Name the system so it is findable (e.g. "staging cluster", "Terraform", "Kafka"), as a real person would.
- One question, under 25 words.

must_mention: 1-3 short facts a correct answer has to state (e.g. "snapshot.mode=recovery").
usable=false when the session has no concrete, reusable, non-obvious fact (status reports, chit-chat,
general knowledge any engineer would know without the transcript)."""

JIRA_SYSTEM = (
    SYSTEM.replace("one engineer's past Claude Code sessions", "one engineer's team Jira tickets")
    .replace("Given one session transcript", "Given one Jira ticket with its comments")
    .replace("usable=false when the session", "usable=false when the ticket")
    .replace("without the transcript", "without the ticket")
)

SCHEMA = {
    "type": "object",
    "properties": {
        "usable": {"type": "boolean"},
        "question": {"type": "string"},
        "gold": {"type": "string"},
        "must_mention": {"type": "array", "items": {"type": "string"}},
        "kind": {"type": "string", "enum": ["fix", "root_cause", "command", "decision"]},
    },
    "required": ["usable", "question", "gold", "must_mention", "kind"],
}

_CWD = tempfile.mkdtemp(prefix="brain-eval-")


def mirror_name(ref: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", ref.removeprefix("session:")) + ".txt"


def write_corpus(rows) -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    for ref, ts, project, _title, body in rows:
        (CORPUS / mirror_name(ref)).write_text(f"date: {ts:%Y-%m-%d}\nproject: {project or ''}\n\n{body}")


def write_case(row, system: str = SYSTEM) -> dict:
    ref, ts, project, _title, body = row
    proc = subprocess.run(
        ["claude", "-p", "--model", "opus", "--no-session-persistence", "--tools", "",
         "--strict-mcp-config", "--setting-sources", "project", "--system-prompt", system,
         "--json-schema", json.dumps(SCHEMA), "--output-format", "json"],
        input=body, capture_output=True, text=True, cwd=_CWD, timeout=900, check=False,
    )
    out = json.loads(proc.stdout) if proc.stdout else {}
    if proc.returncode or out.get("is_error"):
        return {"ref": ref, "error": (proc.stderr or out.get("result", ""))[:300]}
    case = out.get("structured_output") or json.loads(out["result"])
    return {"ref": ref, "date": f"{ts:%Y-%m-%d}", "project": project, **case}


def main() -> None:
    if sys.argv[1:] == ["jira"]:
        return jira()
    rows = db.connect().execute(
        "select ref, ts, project, title, body from records where source = 'sessions' order by ref"
    ).fetchall()
    write_corpus(rows)
    pool = [r for r in rows if len(r[4]) >= MIN_BODY and r[0].removeprefix("session:").split("#")[0] not in EXCLUDE]
    sample = random.Random(SEED).sample(pool, SAMPLE)
    with ThreadPoolExecutor(4) as ex, (FLOW / "candidates.jsonl").open("w") as f:
        for case in ex.map(write_case, sample):
            f.write(json.dumps(case) + "\n")
            f.flush()
            print(case["ref"], case.get("question") or case.get("error"), flush=True, file=sys.stderr)



def jira() -> None:
    """Same method over Jira tickets: mirror every ticket for the rg arm, sample tickets with substance."""
    global CORPUS
    flow = HILL / "jira"
    CORPUS = flow / "corpus"
    rows = db.connect().execute(
        "select ref, ts, project, title, body from records where source = 'jira' order by ref"
    ).fetchall()
    write_corpus(rows)
    pool = [r for r in rows if len(r[4]) >= JIRA_MIN_BODY]
    sample = random.Random(SEED).sample(pool, JIRA_SAMPLE)
    with ThreadPoolExecutor(4) as ex, (flow / "candidates.jsonl").open("w") as f:
        for case in ex.map(lambda r: write_case(r, JIRA_SYSTEM), sample):
            f.write(json.dumps(case) + "\n")
            f.flush()
            print(case["ref"], case.get("question") or case.get("error"), flush=True, file=sys.stderr)


if __name__ == "__main__":
    main()
