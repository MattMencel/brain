# brain — design

A personal, laptop-local knowledge base: past Claude Code sessions and git history, searchable by keyword so Claude can recall prior work before re-investigating it.

Inspired by Nick Saraev's "Engram" walkthrough (YouTube `LP7bfT4vOlo`): connectors pull deltas, a cheap model writes a searchable layer, plain BM25 search, recency weighting, health checks.

## Decisions

| Area | Decision | Why |
|---|---|---|
| Store | Homebrew `postgresql@18` + ParadeDB `pg_search` (BM25) | Real BM25 (IDF), concurrent connector writes, `pgvector` already present if hybrid search is ever wanted |
| Install | ParadeDB macOS 26 arm64 `.pkg` payload unpacked to `/opt/homebrew/opt/paradedb`, symlinked into pg18's lib/extension dirs (the pkg's own postinstall, minus Cellar writes). Loads on macOS 27 | No sudo; fallback was `pg_textsearch` |
| Sources (v1) | Claude Code main sessions; git (every author, last 12 months) | Already on disk — no new exposure. Jira and Slack: see `design-connectors.md` |
| Session unit | One record per session, condensed to user prompts + Claude prose (tool output dropped, pastes/stdout clipped). Split at 300k chars. Only sessions idle ≥ 30 min. Subagent transcripts skipped. Bodies < 300 chars skipped (bare slash commands) | Atoms preserve exact strings; subagent results already appear in the parent |
| Git unit | One record per commit: message + changed paths, no LLM | Commit messages already are summaries |
| Summarizer | Haiku via `claude -p` (plan billing), tools/MCP off, `--setting-sources project`, empty cwd, `--no-session-persistence`. Output: title, summary, questions, atoms (`ERROR:/FIX:/CMD:/DECISION:/REF:`) | ~0.6k tokens overhead per call vs ~5.7k with user settings |
| Redaction | Regex at ingest, before storage **and** before the summarizer | Raw secrets never reach the DB or Haiku |
| Index | BM25 over title, summary, questions, atoms, body; query boosts title/summary ×3, questions/atoms ×2 | Body catches exact strings Haiku dropped |
| Recency | `score × 0.5^(age_days / 90)`; nothing deleted in v1 | Deletion is one-way; decay solves stale facts |
| Re-summarize | `summarizer_version` per row; manual re-run only | Each re-run costs a full backfill of plan usage |
| Schedule | launchd every 30 min; macOS notification if a connector has no success in 2 h; `brain status` | No Slack token with write scope |
| Retrieval | Skill + `brain search` / `brain show` on demand; `UserPromptSubmit` hook only if on-demand proves its hit rate | Avoid per-prompt latency and context cost |

## Gate

Before building beyond v1: 10 real "how did I fix X" questions, brain vs `rg` over raw transcripts. If brain doesn't clearly win, stop.

## Known gaps

- Redaction is pattern-based; novel secret formats can slip through.
- Desktop-generated session titles can be junk; Haiku's title leads, the session name is kept in brackets.
- ParadeDB on macOS 27 is unsupported upstream; re-check after each ParadeDB or Postgres upgrade (`brew upgrade` does not touch the symlinks).
