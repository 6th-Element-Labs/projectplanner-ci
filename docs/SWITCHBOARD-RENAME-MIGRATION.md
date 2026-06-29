# Switchboard Rename Migration

Status: planned migration, safe docs bridge in progress.
Owner: Switchboard Operator + RECON-6.
Last live inventory: 2026-06-29.

Switchboard is the product name. `projectplanner` is still the deployed compatibility identity
for the repository, service units, checkout path, data path, and environment variable prefix.

This document is the migration contract. Do not treat the rename as a global string replace.
The goal is to make the product read as Switchboard while preserving `plan.taikunai.com`,
existing MCP clients, GitHub links, systemd services, data paths, and agent-host boot scripts.

## Non-negotiables

- `https://plan.taikunai.com` and `https://plan.taikunai.com/mcp` stay live during the migration.
- `project="switchboard"` is the board id. It is not a filesystem, systemd, or GitHub repo name.
- The existing `projectplanner` names remain compatibility aliases until all runtime adapters,
  deployment scripts, and host daemons have been verified against the new names.
- Unknown project ids fail closed. Rename work must not weaken project boundary checks.
- No destructive remote rename happens until backup, alias validation, service validation, and
  rollback commands are written and rehearsed.
- `PM_*` environment variables stay supported through the migration. Introduce `SWITCHBOARD_*`
  aliases only with code support and tests.

## Current Live State

Verified against the live AWS instance on 2026-06-29:

| Surface | Current value |
|---|---|
| EC2 tag | `Name=projectplanner` |
| Public URL | `https://plan.taikunai.com` |
| GitHub repo | `6th-Element-Labs/projectplanner` |
| Live checkout | `/opt/projectplanner` |
| Live data dir | `/var/lib/projectplanner` |
| Default DB | `/var/lib/projectplanner/plan.db` |
| Helm board DB | `/var/lib/projectplanner/helm.db` |
| Switchboard board DB | `/var/lib/projectplanner/switchboard.db` |
| Web app service | `projectplanner.service` on `127.0.0.1:8110` |
| MCP service | `projectplanner-mcp.service` on `127.0.0.1:8111` |
| Gateway service | `projectplanner-gateway.service` on `127.0.0.1:8095` |
| Agent host service | `projectplanner-agent-host.service` |
| Timer units | `projectplanner-monitors.timer`, `projectplanner-reconcile.timer`, `projectplanner-inbox.timer`, `projectplanner-digest.timer`, `projectplanner-summarize.timer` |
| Env prefix | `PM_*` |

The live health response still reports `service="taikun-pm"`. That is a display/API naming
cleanup candidate, not a blocker for the operational rename.

## Target State

| Surface | Target | Compatibility rule |
|---|---|---|
| Product name | Switchboard | Use in README, specs, UI copy, and operator docs. |
| GitHub repo | `6th-Element-Labs/switchboard` | Keep GitHub redirect from `projectplanner`; update remotes after redirect is proven. |
| Live checkout | `/opt/switchboard` | Keep `/opt/projectplanner` as a symlink or alias during at least two deploy cycles. |
| Live data dir | `/var/lib/switchboard` | Keep `/var/lib/projectplanner` readable until env alias support is proven. |
| Services | `switchboard*.service` | Keep `projectplanner*.service` aliases during migration. |
| Env prefix | `SWITCHBOARD_*` | Keep `PM_*` as compatibility source of truth until alias tests pass. |
| Public URL | `plan.taikunai.com` | No URL change required for P0. |
| Board id | `switchboard` | Already live. Do not rename to `projectplanner` or infer from repo name. |

## Migration Phases

### Phase 0 - Inventory and Freeze

Purpose: make the existing live state explicit before changing names.

Checklist:

- Record `git rev-parse HEAD` on the VM.
- Record active service and timer names with `systemctl list-units 'projectplanner*'`.
- Record configured DB paths from `.env` without printing secrets.
- Confirm `/health` and MCP reachability before any change.
- Announce a short operator window before path or service changes.

Gate:

```bash
curl -fsS https://plan.taikunai.com/health
ssh plan-vm 'cd /opt/projectplanner && git status --short --branch'
ssh plan-vm 'systemctl is-active projectplanner projectplanner-mcp projectplanner-agent-host'
```

### Phase 1 - Docs and Brand Bridge

Purpose: make Switchboard the product identity without changing live process names.

Allowed now:

- README and product docs say Switchboard first.
- Deployment docs state that `projectplanner` is the current compatibility name.
- Runbooks link to this migration plan.
- Service descriptions may mention Switchboard while keeping unit filenames unchanged.

Do not yet:

- Rename the GitHub repo.
- Rename `/opt/projectplanner`.
- Rename `/var/lib/projectplanner`.
- Rename systemd units.
- Rename `PM_*` variables.

### Phase 2 - Config Alias Support

Purpose: let new deployments prefer Switchboard names while old deployments keep working.

Implement and test code support for:

- `SWITCHBOARD_DB_PATH` as an alias for `PM_SWITCHBOARD_DB_PATH`.
- `SWITCHBOARD_PROJECT_REGISTRY_DB_PATH` as an alias for `PM_PROJECT_REGISTRY_DB_PATH`.
- `SWITCHBOARD_DYNAMIC_PROJECTS_DIR` as an alias for `PM_DYNAMIC_PROJECTS_DIR`.
- `SWITCHBOARD_MCP_TOKEN` as an alias for `PM_MCP_TOKEN`.
- `SWITCHBOARD_AUTH_TOKEN` as an alias for `PM_AUTH_TOKEN`.
- `SWITCHBOARD_GITHUB_REPO` as an alias for `PM_GITHUB_REPO`.

Compatibility rule: `PM_*` continues to win when both are set until a later cleanup release.

Required tests:

- Store loads the same DB with `PM_*` only.
- Store loads the same DB with `SWITCHBOARD_*` only.
- If both are set, the documented precedence wins.
- MCP auth accepts the new token env name without breaking `PM_MCP_TOKEN`.
- Agent host starts with the new repo/data path aliases.

### Phase 3 - Path Aliases

Purpose: make `/opt/switchboard` and `/var/lib/switchboard` valid without moving the canonical
runtime out from under active services.

Recommended first step:

```bash
sudo ln -sfn /opt/projectplanner /opt/switchboard
sudo ln -sfn /var/lib/projectplanner /var/lib/switchboard
ls -ld /opt/projectplanner /opt/switchboard /var/lib/projectplanner /var/lib/switchboard
```

Gate:

```bash
cd /opt/switchboard
git rev-parse HEAD
.venv/bin/python -m py_compile app.py mcp_server.py store.py adapters/agent_host.py
curl -fsS https://plan.taikunai.com/health
```

Rollback:

```bash
sudo rm -f /opt/switchboard /var/lib/switchboard
sudo systemctl restart projectplanner projectplanner-mcp projectplanner-agent-host
```

### Phase 4 - Service Aliases

Purpose: let operators use `switchboard*` service names while existing automation still works.

Recommended approach:

- Add explicit `switchboard*.service` unit files or verified systemd aliases.
- Keep `projectplanner*.service` installed and enabled.
- Ensure both names target the same working directory and env file during the alias phase.
- Restart through the old names first, then verify the new names report the same process health.

Minimum aliases:

- `switchboard.service`
- `switchboard-mcp.service`
- `switchboard-gateway.service`
- `switchboard-agent-host.service`
- `switchboard-monitors.timer`
- `switchboard-reconcile.timer`

Gate:

```bash
sudo systemctl daemon-reload
systemctl status projectplanner switchboard --no-pager
systemctl status projectplanner-mcp switchboard-mcp --no-pager
curl -fsS https://plan.taikunai.com/health
```

Rollback:

```bash
sudo systemctl disable --now switchboard switchboard-mcp switchboard-gateway switchboard-agent-host || true
sudo systemctl start projectplanner-gateway projectplanner projectplanner-mcp projectplanner-agent-host
```

### Phase 5 - GitHub Repo Rename

Purpose: move the public repo identity after deployed aliases are proven.

Preconditions:

- `/opt/switchboard` works.
- `switchboard*.service` aliases work.
- A fresh clone from the new repo URL works.
- The old GitHub URL redirects.
- Webhook secrets and Caddy config are unaffected.

Steps:

1. Rename `6th-Element-Labs/projectplanner` to `6th-Element-Labs/switchboard` in GitHub settings.
2. Verify the old URL redirects:

```bash
git ls-remote https://github.com/6th-Element-Labs/projectplanner.git HEAD
git ls-remote https://github.com/6th-Element-Labs/switchboard.git HEAD
```

3. Update the VM remote:

```bash
cd /opt/projectplanner
git remote set-url origin git@github.com:6th-Element-Labs/switchboard.git
git fetch origin
git status --short --branch
```

4. Update local agent checkouts gradually. Old remotes should keep working through GitHub redirect,
   but agents should converge on the new URL.

Rollback:

- GitHub repo can be renamed back if the redirect or webhook path fails.
- VM remote can be set back to `git@github.com:6th-Element-Labs/projectplanner.git`.
- Existing checked-out code and data paths are unaffected by the repo rename.

### Phase 6 - Canonical Path Move

Purpose: make `/opt/switchboard` and `/var/lib/switchboard` canonical only after aliases and repo
rename have survived real deploys.

Preconditions:

- Two successful deploy cycles using `switchboard` aliases.
- No active agent host, adapter, or deployment script hard-requires `/opt/projectplanner`.
- Backups exist for both checkout and DB directory.

Sketch:

```bash
sudo systemctl stop projectplanner projectplanner-mcp projectplanner-agent-host
sudo rsync -a /var/lib/projectplanner/ /var/lib/switchboard/
sudo rsync -a --delete /opt/projectplanner/ /opt/switchboard/
sudo mv /opt/projectplanner /opt/projectplanner.pre-switchboard
sudo ln -sfn /opt/switchboard /opt/projectplanner
sudo mv /var/lib/projectplanner /var/lib/projectplanner.pre-switchboard
sudo ln -sfn /var/lib/switchboard /var/lib/projectplanner
sudo systemctl start switchboard-gateway switchboard switchboard-mcp switchboard-agent-host
```

Gate:

```bash
curl -fsS https://plan.taikunai.com/health
cd /opt/switchboard && git rev-parse HEAD
systemctl is-active switchboard switchboard-mcp switchboard-agent-host
```

Rollback:

```bash
sudo systemctl stop switchboard switchboard-mcp switchboard-agent-host || true
sudo rm -f /opt/projectplanner /var/lib/projectplanner
sudo mv /opt/projectplanner.pre-switchboard /opt/projectplanner
sudo mv /var/lib/projectplanner.pre-switchboard /var/lib/projectplanner
sudo systemctl start projectplanner-gateway projectplanner projectplanner-mcp projectplanner-agent-host
```

## Validation Matrix

Every rename phase must verify:

| Area | Check |
|---|---|
| Web | `curl -fsS https://plan.taikunai.com/health` |
| MCP | MCP connect to `https://plan.taikunai.com/mcp` and `list_projects()` |
| Project boundaries | `project="switchboard"` reads and writes only Switchboard; unknown ids fail closed |
| Agent session | `prepare_agent_session(project="switchboard")`, `register_agent`, inbox drain |
| Dispatch | `claim_next(project="switchboard")` remains project-scoped |
| Host wake | `projectplanner-agent-host` or `switchboard-agent-host` can register and consume message-only wakes |
| Tally | `GET /tally/v1/project?project=switchboard` still returns cost/outcome data |
| Reconcile | `jobs.py reconcile_alerts` can run against `switchboard` |
| Tests | `test_switchboard_runtime.py`, adapter conformance, and Agent Host tests pass locally |

## Operator Guidance

Use this language during the transition:

- "Switchboard is the product and protocol surface."
- "`projectplanner` is the legacy deployment/repo identity still kept as a compatibility alias."
- "Do not infer board project from repo, path, or service name. Always pass `project` explicitly."
- "A rename is complete only after agents, docs, deploy scripts, service units, data paths, and
  rollback have all been verified."

## Future Cleanup Tasks

- Add `SWITCHBOARD_*` env aliases and tests.
- Add `switchboard*.service` units or verified systemd aliases.
- Update `/health` service display from `taikun-pm` to `switchboard` with compatibility expectations.
- Move production checkout/data paths only after path aliases have survived deploys.
- Rename the GitHub repo after path and service aliases are proven.
- Remove `projectplanner` compatibility names only in a later major cleanup window.
