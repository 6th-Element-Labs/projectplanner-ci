# Ingest `:8126` cutover and rollback

ARCH-MS-121 cuts only `GET /api/inbox` and `POST /api/intake` to the standalone
Ingest process. Upload, confirm/dismiss, simulate, poll, and MCP remain on `:8110`/`:8111`.

## Deploy

Run `bash deploy/redeploy.sh`. The deploy snapshots Caddy, the monolith unit, and every
rollback-managed cut unit. It then installs and starts `switchboard-ingest`, requires both
`/health` and `/ready`, and only then reloads Caddy. On first activation the monolith is not
restarted with `PM_INGEST_HTTP_PRIMARY=service` until the healthy edge owns both exact routes.

The post-reload runtime proof checks all prior cuts, the `:8126` process and readiness, and
the two edge-owner matchers. A dead backend, stale Caddy configuration, failed monolith
restart, or failed runtime proof triggers automatic topology restoration.

## Automatic rollback order

1. Restore the prior monolith and cut-unit files.
2. Restart and prove the old monolith healthy on `:8110`.
3. Restore and reload the prior Caddyfile.
4. Restore the previous active/enabled lifecycle of `switchboard-ingest` and every prior cut.

The old edge is never restored before its prior monolith is healthy, and a cut service is
never stopped while the current edge might still depend on it. This prevents a half-cut.

## Manual verification

```bash
systemctl is-active switchboard-ingest
curl -fsS http://127.0.0.1:8126/health
curl -fsS http://127.0.0.1:8126/ready
bash deploy/redeploy.sh
```

Do not manually set `PM_INGEST_HTTP_PRIMARY=service` or reload the new Caddyfile ahead of
the deploy orchestration.
