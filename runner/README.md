# Maxwell Claude Code runner (push bridge)

Self-hosted Claude Code on the demo box (`claude-runner` user) that Maxwell dispatches dev
tasks to. Maxwell `dispatch.py` POSTs a task brief → `service.py` runs `run_task.sh` →
Claude Code headless makes the change on a `claude/<task>` branch → pushes it → returns a PR
compare URL (a human opens the PR; never auto-merged to main).

- `service.py` — token-auth HTTP listener (`/dispatch`, `/job/<id>`), systemd `maxwell-runner`.
- `run_task.sh` — branch off `development` → `claude -p` → commit → push → emit PR url.
- Secrets on demo (never in git): `~/.maxwell/key` (Anthropic API key), `~/.maxwell/dispatch_token`.
- Reachable from the plan VM only (SG + ufw scoped to the plan VM's private IP).
