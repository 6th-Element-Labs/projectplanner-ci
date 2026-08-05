# Compand pilot deploy and recovery

This runbook deploys the opt-in Compand Responses gateway. Caddy remains the only public
edge. Do not route the normal Switchboard application through Compand.

## Required configuration

Copy `deploy/compand/switchboard-compand.service.example` into systemd and provide
`/etc/projectplanner/compand.env` readable only by the service account. Required values are
`COMPAND_UPSTREAM_OPENAI_API_KEY`, `COMPAND_CLIENT_CREDENTIALS_JSON`,
`COMPAND_CAPABILITY_SECRET`, and `COMPAND_STATE_DB_PATH`. Use
`COMPAND_GATEWAY_MODE=scan` first. `enforce` is an explicit later operator choice.

Set `COMPAND_ARTIFACT_RETENTION_SECONDS` to the approved recovery window. Zero disables
artifact recovery. Set `COMPAND_SESSION_RETENTION_SECONDS` to the approved retry and
continuation window. The database directory must be the only writable service path. Client and
upstream credentials must be different; startup fails if they are the same.

Add the example Caddy fragment only after DNS and the service health are verified. Validate
with `caddy validate`, reload Caddy, and exercise `/v1/models` and the Responses loop through
the public hostname.

## Recovery and rollback

1. Set `COMPAND_GATEWAY_MODE=passthrough` and restart the service to stop new transforms
   while retaining the same endpoint and credentials.
2. If the process itself is unhealthy, remove the Compand Caddy site and reload Caddy. This
   does not affect the Switchboard application site.
3. Restore a transformed command result through
   `GET /compand/v1/artifacts/{capability}` with the original Compand bearer credential and
   `x-compand-session-id`. A capability never crosses tenant or session scope.
4. Run `POST /compand/v1/purge` after the recovery window or incident handling completes.
   Expired artifacts are inaccessible even before physical purge.

Receipts and observations contain hashes, counts, classifications, and timing only. They must
never contain prompt, command, or tool-output content. Provider-process observation is a
separate evidence source; gateway self-observation does not prove egress coverage.
