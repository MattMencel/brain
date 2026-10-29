---
name: brain
description: Search the user's own past work - Claude Code sessions, git commits across their repos, GitHub pull requests, Jira tickets and Slack threads - to answer how something was fixed, why something was decided, or what command worked before. Use when a question refers to earlier work ("how did we fix", "what did we decide", "last time", "again") or a problem looks like one already solved, and to answer "what did I do" or "who did what, when".
---

# brain

`brain` is a local BM25 index (Postgres + ParadeDB) over condensed, redacted session transcripts, git commit messages, GitHub pull requests, Jira tickets and Slack threads. A session has a Haiku-written title and summary for its main thread, plus one topic per distinct problem, fix or decision in it - side quests included - each with its own title, summary, atoms and transcript turn range. Transcripts are also indexed as ~4k-character passages. A hit's session title may look unrelated when the match is a side topic. Summaries and atoms are lossy; causes, caveats and the rest of a fix live in the transcript.

## Interface

- `brain search "<words>"` - ranked hits with ref, date, project, session title and short summary; for sessions, the best-matching topic and passage with the transcript lines around the query terms. Options: `-n N` (default 8), `--source sessions|git|github|jira|slack`, `--project <substring>`, `--since <days>`, `--json`.
- Ranking is BM25 over any of the query words, with each hit's score halved for every 90 days of age; git history covers the default branch for the last 12 months. An older answer can sit below the first page - `-n` or a narrower query brings it up, and `--since` cuts the old ones out.
- `brain show <ref> --topic N` - that topic's summary, atoms and transcript turns. `brain show <ref> --passage N` - one passage. `brain show <ref>` - the whole record. The ref is passed exactly as search prints it, prefix included (`session:<uuid>`, `session:<uuid>#2`, `git:<repo>@<sha>`, `github:<owner>/<repo>#<n>`, `jira:<KEY>`).
- `brain events` - who did what, when: `--actor me` (or a name) for the user's own timeline across sources, `--ref <ref>` for one thing's history (who created, commented, moved status, committed). Options: `--since <days>` (default 7), `--kind created|commented|status|assigned|committed|posted|session|mentioned|opened|reviewed|merged|closed`, `--to me` for what was addressed to the user (mentions of them or their groups, and DMs), `--bots` to include automation, `-n N` to cap (default: all), `--json` for `ts, source, kind, actor, detail, ref, title`. Use it for "what did I do" and "who/when" questions; search answers "how/why".
- Long transcripts read better through `brain show <ref> | grep -n -i -C 8 '<term>'` than in full.

## Answering

The answer comes from the transcript or commit body, not from a summary. The topic and excerpt in search results show where to read; open the matching topic (or passage) and confirm the fix, cause or decision in what was actually said before stating it. If nothing relevant turns up after a few differently worded searches, say so rather than filling the gap from general knowledge.
