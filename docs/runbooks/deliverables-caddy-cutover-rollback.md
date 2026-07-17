# Deliverables Caddy cutover and rollback

ARCH-MS-111 moves only the eight GET routes chartered by ADR-0014 to
`switchboard-deliverables` on `127.0.0.1:8124`. Deliverables writes and every
non-chartered sibling remain on the monolith at `:8110`.

## Deploy

Use `bash deploy/redeploy.sh`. The transaction installs and enables the unit,
restarts all four cut services, and requires successful local health responses
from `:8110`, `:8121`, `:8122`, `:8123`, and `:8124` before the live Caddyfile is
overwritten or reloaded. The final gate verifies the exact deployed SHA, Caddy
checksum, service identity, GET ownership on `:8124`, and write ownership on
`:8110`.

On first activation, the old monolith process keeps serving the eight reads while
`:8124` starts and passes health. Caddy moves those reads next; only then does the
transaction restart the monolith with dual-strip enabled. Subsequent redeploys may
restart the already-cut topology together because the live edge already owns 8124.

Never stop the monolith or any cut service first. A dead `:8124` backend is a
No-Go: repair it and rerun the transaction while the prior live edge remains in
place.

## Automatic rollback

Before mutation, `redeploy.sh` snapshots the live Caddyfile, monolith unit, every
cut unit, and each cut service's active/enabled state. Any failure from unit
mutation through authenticated public-edge proof triggers rollback in this order:

1. Restore the prior monolith and cut unit files.
2. Restart and health-check the prior monolith configuration.
3. Restore and reload the prior Caddyfile only after the monolith is healthy.
4. Restore the previous active/enabled lifecycle of Tasks, Coord, and Deliverables.

This ordering prevents a half-cut: the rollback never removes a backend while the
current edge may still route to it, and it never restores an old edge before its
monolith owner is healthy. If either prerequisite fails, the script preserves the
currently routed services and exits non-zero for operator recovery.

## Manual verification

```bash
curl -fsS http://127.0.0.1:8124/health
sudo caddy validate --adapter caddyfile --config deploy/Caddyfile
python scripts/verify_runtime_deploy.py \
  --canonical-sha "$(git rev-parse origin/master)" \
  --service switchboard-deliverables:8124 \
  --edge-owns '@deliverables_day_one_reads:8124' \
  --edge-base-url https://plan.taikunai.com
```

The verifier requires `PM_RUNTIME_PROOF_TOKEN` for authenticated route probes.
An absent token, stale Caddyfile, wrong SHA, dead backend, anonymous read, or
incorrect owner is a failed deployment, not a warning.
