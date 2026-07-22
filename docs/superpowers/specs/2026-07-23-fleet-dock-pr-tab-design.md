# Fleet dock rework: runner console + pull-request tab

Date: 2026-07-23 · Approved by Steve in-session.

## Problem

The floating fleet dock (bottom-right, `#fleet-dock`, static/app.js `_renderFleetDock`)
surfaces work-session preflight findings whose repair strings are written for agents
("Run preflight_work_session…"), with no operator action attached. Steve has never used
it and can't act on it. Meanwhile nothing in the web UI shows live PR status — PR links
are bare anchors.

## Design (approved)

Keep the dock slot and collapsed pill. Replace the body with two tabs:

### Runners tab
A narrow-format port of the Fleet page's Live runners table, same data source
(`GET /ixp/v1/runner_sessions?project=…&include_stale=true`). Each runner is a
two-line card: id + status badge + uptime on top; `TASK · runtime · host · agent`
below; buttons **Watch / Logs / On disk / Kill**.

- **Watch** → existing `openRunnerSessionPanel(task_id)` (only when running + task bound).
- **Logs** → existing `request_runner_logs` action.
- **On disk** → the existing `snapshot` control action, relabelled. It reports branch,
  head SHA, `git status --porcelain`, and log tail from the runner's worktree
  (adapters/codex/supervisor.py `_snapshot`). Rendered as "N uncommitted files" on the
  card — the safety check before Kill. No "Health" button (dropped as noise).
- **Kill** → existing `request_runner_kill` with typed-id confirmation.

Old session-health rows are removed. A stale runner shows one plain-English line
("No heartbeat since HH:MM · N uncommitted files") instead of preflight jargon.

### Pull requests tab
Every open PR on the project's **canonical repo** (option B — includes PRs with no
board task), newest-updated first. Each row links to the PR on GitHub and carries
hover-card-parity badges:

- State pill: Open / Draft (merged/closed never appear — the list is open-only).
- `repo #number`, title (hyperlink), relative updated age.
- CI badge: checks pass / failed (worst failing context name) / running / none —
  combined commit statuses + check runs for the head SHA.
- Mergeable badge: conflicts (`mergeable_state=dirty`) / blocked (`blocked`) / clean.
- Review badge: approved / changes requested / none (from mergeable_state + reviews
  where cheap; best-effort).
- Merge-queue position when enqueued (repo-level GraphQL mergeQueue query, one call,
  best-effort — absent on error).
- Diff stats `+adds −dels · N files` (from the per-PR detail GET).
- Footer: board task id(s) via `task_id_parser.task_ids_for_pr`, else a grey
  "no board task" badge (the orphan-PR signal); author; "no activity Nd" when
  updated_at is older than 24h.

### Attention model (rule C)
"Blocked" = anything blocking a merge: CI failed, conflicts, mergeable_state
blocked while checks are green (stuck), or a stale/dead runner. The collapsed pill
goes red and the dock auto-opens when blocked count > 0 (explicit user collapse
always wins). Header count follows the active tab ("3 running" / "2 blocked").

## Server

New root module `open_prs.py` + route `GET /ixp/v1/open_prs?project=…` on the board
router (operator dashboard reads live there; global auth middleware applies).

- Repo from `store.get_project_github_repo(project)`; token via the gate's env chain
  (`PM_GITHUB_TOKEN` / `GITHUB_TOKEN` / `SWITCHBOARD_CI_GITHUB_TOKEN`).
- Calls per refresh: 1 list + 1 GraphQL queue + per PR (detail, combined status,
  check-runs) ≈ `2 + 3N`. All fetchers injectable for tests.
- Cached via `read_cache.ttl_read_cache` with a 60-second time-bucket stamp
  (`int(now/60)`) — the poll path serves the cache; at most one GitHub sweep per
  minute per project, stale-while-revalidate applies.
- No token → `{"prs": [], "unavailable": "no_github_token"}`; the tab shows a
  plain-English empty state. GitHub errors degrade the same way (never 500 the dock).
- Response rows are pre-classified server-side (`blocked: bool, blocked_reason`),
  so the frontend renders without re-deriving GitHub semantics.

## Non-goals

- The Fleet *page* is unchanged (full table, wake intents, hosts).
- No merge/close actions from the dock — read-only plus existing runner controls.
- No per-review-thread detail; approval state only.

## Testing

Script-style root test (repo convention): fetchers stubbed, no network —
classification matrix (failed CI / conflicts / stuck-green / clean), orphan-PR
join, cache bucket behavior, no-token degrade. Frontend attention model covered
by asserting the classification lands server-side; JS stays declarative.
