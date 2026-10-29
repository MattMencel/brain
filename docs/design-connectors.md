# brain — connectors design

Adds Jira and Slack as sources, and makes brain installable by someone other than its author. Extends `design.md`, whose "Slack never" decision this replaces.

## Shared

| Area | Decision | Why |
|---|---|---|
| Audience | Org engineers first, public later. No org-identifying value (company name, Atlassian site, channel, path, email domain) in the repo | Public release becomes a docs task, not a rewrite |
| Connector | A module in `brain/connectors/` exposing `fetch(ctx) -> Iterable[Record \| Failed]`; `ingest` stores, summarizes and indexes every source the same way | Adding an intake is one file plus one config block |
| Mechanical vs model work | Connectors fetch with plain code (APIs, CLIs, files). A model is called only for judgment: summaries and topics. Never route fetching through an LLM | An LLM fetch layer measured $0.20 per 30-min Slack run (~$9.80/day) before any summary, and can alter or truncate text |
| Config | `~/.config/brain/brain.toml` (`BRAIN_CONFIG` overrides); the repo ships only `brain.example.toml` with placeholders. A connector runs when its table exists and `enabled` is not false | Keeps per-user and org values out of the repo |
| Secrets | Python `keyring` (macOS Keychain, Linux Secret Service, Windows Credential Locker); an env var overrides for CI/containers. `brain auth <connector>` prompts once | launchd does not load the shell env |
| Database | `[db] mode = "docker"` (default): pinned `paradedb/paradedb` image via `docker compose` on a non-default port. `mode = "external"` takes a `dsn` | Cross-platform default; Homebrew PG 18 + pg_search documented as the alternative |
| Install | `brain install`, idempotent: write config from the example if absent (it picks the DB mode, so it goes first) → start/check DB → migrate → `brain auth` for enabled connectors without a secret → `brain schedule install` → link the skill into `~/.claude/skills`. Each step is also its own subcommand (`config`, `db-up`, `migrate`, `auth`, `schedule install`, `link-skill`) | One command for a new user; a failed step re-runs alone |
| Schedule | `brain schedule install`: launchd agent `local.brain` on macOS, systemd user timer `brain.timer` on Linux (untested); Windows unsupported. Runs `brain ingest --limit N; brain check` with the installing shell's PATH | |
| Repo | Stays local while building. A public release is a fresh single-commit history from this private working repo, gated by a grep for org identifiers. Eval cases, corpus and hillclimb state live under `~/.local/share/brain/eval/`, outside the repo | History already holds org content; a filter-repo scrub can miss a blob |

## Jira

| Area | Decision |
|---|---|
| Scope | Every ticket in the configured projects, plus tickets elsewhere where the user is reporter, assignee or commenter |
| Fetch | Jira Cloud REST API with an API token from `keyring` |
| Record | `jira:<KEY>`; project = Jira project key; ts = `updated`; author = reporter. Body = summary, description, comments and status. Title = the ticket summary. No Haiku. Replaced whenever `updated` moves. Attachments skipped |
| Window | 12 months backfill, then incremental on `updated >= last_success` |
| Eval | ~10 cases from randomly sampled tickets after the build |

## Slack

| Area | Decision |
|---|---|
| Scope | Threads and DMs the user posted in (`from:me`), pulled in full, plus the configured channels in full |
| Fetch | Slack Web API with a user token (`xoxp`, `brain auth slack`) from an internal Slack app; internal apps keep Tier 3 history limits. First run reads configured channels in full; later runs find new messages with per-day searches (incl. `in:<#channel>`) and re-read only the threads and days they touch. Replaced a `claude -p` + claude.ai connector path (git `43b8f62`) |
| Record | One per thread (`thread_ts`); channel messages outside threads and DMs grouped per conversation per day |
| Privacy | Full threads, others' messages included, through the existing redactor. The index stays local |
| Summarizer | Haiku topics with a Slack variant of the session prompt (~$0.003/record) |
| Window | 1 month backfill by default, configurable |
| Eval | ~10 cases from randomly sampled records after the build |

## GitHub pull requests

| Area | Decision |
|---|---|
| Scope | PRs that involve the user (`involves:@me`: author, assignee, reviewer, commenter, mention), plus every PR in configured `repos` |
| Fetch | `gh api graphql` under gh's own login; brain stores no token. Searches run in weekly slices of `updated:`, halved while a slice exceeds search's 1,000-result cap. About 1 rate-limit point per PR (measured: 26 for a page of 25); below 200 points left, the run waits for the hourly reset |
| Record | `github:<owner>/<repo>#<n>`; project = repo name, matching git records. Body = title, state, branches, description, human reviews and comments (review comments with `path:line`), and `Merged as git:<repo>@<sha>` linking the merge commit, which the git connector skips when it is a merge commit. Bot comments are left out of the body. ts = latest human activity. No Haiku |
| Events | `opened`, `reviewed` (detail: approved/changes_requested/commented/dismissed), `commented`, `merged`, `closed`; bots kept with `bot` set |
| Limits | First 100 comments, 100 reviews and 100 review threads (50 comments each) per PR |
| Window | 12 months backfill, then incremental from a day before the last success |

## Events

A record says what a thing is and when it last moved; an event says who did what to it, and when.

| Area | Decision |
|---|---|
| Purpose | "What did I do on Tuesday" (`--actor me`) and "who did what on this thing" (`--ref`) |
| Table | `events(ref → records on delete cascade, ts, actor, kind, detail, bot)`. Everyone's actions, not only the user's; `bot` marks automation (Jira `accountType = app`, Slack `bot_id`), hidden unless `--bots` |
| Kinds | `created`, `commented` (detail: first line), `status` (detail: `From → To`), `assigned` (detail: assignee), `committed`, `posted`, `session`, `mentioned` (detail: who was mentioned); for PRs `opened`, `reviewed`, `merged`, `closed`. Free text, so a new kind is a doc edit |
| Per source | Jira: `created` by the reporter, `commented` per comment, `status` and `assigned` from the full changelog (paged past what search embeds); transitions also go in the body under `## History`. Slack: `posted` per message. `mentioned` comes from Slack `<@user>` and `<!subteam^id>` (by id, since a user token gets no group names) and from each message in a one-to-one DM, addressed to the other side; Jira mention nodes in descriptions and comments; and GitHub `@login` or `@org/team` in PR bodies, reviews and comments. Git: `committed` per commit. Sessions: one `session` per record by `me`, at the record's ts (its last activity) |
| Writes | `Record.events`; replaced per ref when `records.events_hash` differs. A changed event set with an unchanged body never calls the summarizer |
| Backfill | `brain ingest --full [connector]` ignores `last_success` and mtimes and re-reads the whole configured window; `--since N` narrows that one run's window without counting as a config change |
| Identity | Raw source names are stored. `[people.<name>] aliases = [...]` in brain.toml maps them to one person at query time, so editing aliases fixes history without re-ingesting. `brain install` seeds `[people.me]` from `git config user.name` |
| Interface | `brain events [--actor me\|<name>] [--since days] [--ref R] [--kind K] [--to me\|<name>] [--bots] [-n N] [--json]`, oldest first. No limit unless `-n`; a cut warns on stderr. `--json` emits `ts, source, kind, actor, detail, ref, title`, actor resolved through `[people]` |

## Build order

1. Connector protocol and config; sessions and git moved onto it.
2. `keyring` auth, `brain install`, `brain schedule install`, docker compose.
3. Jira connector, then its eval.
4. Slack connector, then its eval.
