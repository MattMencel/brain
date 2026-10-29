# brain

Local search over a person's own work (Claude Code sessions, git, GitHub PRs, Jira, Slack) in Postgres + ParadeDB BM25. Read `README.md` for commands and `docs/design-connectors.md` before changing a connector or the events table; record any design change there.

## Verification

- `uv run pytest` and `uv run ruff check .` must both pass. Tests never need the network; one skips without the database.
- A connector change is verified against live data too: `uv run brain ingest <connector> --dry-run`, then a real run and `brain status`.

## How the code is shaped

- A connector is one module in `src/brain/connectors/` exposing `fetch(ctx) -> Iterable[Record | Failed]`; `cli._ingest` stores, summarizes and indexes every source the same way. Register it in `connectors/__init__.py`.
- Fetching is plain code (APIs, CLIs, files). A model is called only for judgment: Haiku summaries and topics via `claude -p` in `summarize.py`. Never route fetching through an LLM.
- Every record also carries `events` (who did what, when). They are rewritten per ref when their hash changes, without a summarizer call, so adding an event kind costs a `--full` re-read, not model spend.
- Identity is resolved at query time from `[people.<name>] aliases` in the user's config; store raw source names.
- A failed item yields `Failed` and the run carries on; a run records success only when nothing failed.

## Gotchas

- Per-user values (repos, projects, channels, user ids, tokens) belong in `~/.config/brain/brain.toml` or the OS keyring, never in the repo. `src/brain/brain.example.toml` holds placeholders only.
- Eval cases, corpus and results quote real work and live in `~/.local/share/brain/eval`, outside the repo.
- Changing a connector's config section forces its next run to re-read the whole window (`connector_runs.config_hash`). `ingest --since` does not count as a config change.
- GitHub costs about one rate-limit point per PR, shared with the user's own `gh` use. A `--full` read over a year can spend the whole hourly budget; prefer `--full --since N`.
- `brain ingest` takes a per-connector Postgres advisory lock, so a manual run alongside the scheduled one skips rather than doubling the work.
