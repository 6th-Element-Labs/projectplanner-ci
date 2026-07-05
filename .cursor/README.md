# Cursor + Switchboard

Project-level Cursor config for the **taikun-plan** MCP server (`https://plan.taikunai.com/mcp`).

## Local Cursor (Desktop / CLI)

1. Pull `master` (includes `.cursor/mcp.json` and `.cursor/rules/switchboard.mdc`).
2. Export your Switchboard token before starting Cursor:
   ```bash
   export PM_MCP_TOKEN=<your-switchboard-token>
   ```
3. Restart Cursor so it picks up the env var and MCP config.
4. Confirm **taikun-plan** is enabled under **Settings → Tools & MCP**.

Reads work without a token. Writes (`register_agent`, `create_task`, `complete_claim`, etc.) require `PM_MCP_TOKEN` or a scoped bearer token in the Authorization header.

## Cloud Agents

Cloud Agents **do not** read `.cursor/mcp.json`. Configure MCP in the dashboard instead:

1. [cursor.com/agents](https://cursor.com/agents) → **MCP** (or **Dashboard → Integrations & MCP** on Team plans).
2. Add an HTTP server:
   - **Name:** `taikun-plan`
   - **URL:** `https://plan.taikunai.com/mcp`
   - **Header:** `Authorization: Bearer <your-switchboard-token>`
3. Enable it for runs on this repo.

Cloud MCP does **not** support `${env:PM_MCP_TOKEN}` interpolation — paste the actual bearer token in the dashboard header, or store it as a Cloud Agent secret and reference it per Cursor's dashboard docs.

Optional: add `PM_MCP_TOKEN` under [Cloud Agent secrets](https://cursor.com/dashboard/cloud-agents) if your runtime or scripts need the env var inside the VM. MCP auth still comes from the dashboard MCP entry.

## Session handshake

The always-on rule in `.cursor/rules/switchboard.mdc` instructs agents to:

1. `prepare_agent_session` (runtime + project/task/lane when known)
2. `get_working_agreement(project)`
3. `register_agent`
4. Drain inbox / pending acks

Use `project=switchboard` for this repo's product work.
