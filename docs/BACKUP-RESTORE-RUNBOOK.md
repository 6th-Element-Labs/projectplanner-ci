# Backup & restore runbook (HARDEN-43)

Prod data is `~12` SQLite files in `/var/lib/projectplanner/*.db` (switchboard,
plan/maxwell, helm + friends, project_registry, …) on a single `t4g.micro` with
**no EBS snapshots**. Losing the box = losing every task, message, decision, and
provenance record. This runbook makes that survivable: a daily consistent
snapshot lands in S3, and a documented restore rebuilds the DBs on a fresh box.

## What runs

- **`scripts/backup_databases.py`** — snapshots every `*.db` in the data dir
  with the SQLite online-backup API (safe against live writers — *not* a `cp`),
  runs `PRAGMA quick_check` on each snapshot, gzips it, and uploads the set plus
  a `manifest.json` (sha256 sums) to `s3://<bucket>/<prefix>/<UTC-stamp>/`. The
  manifest is uploaded **last**, so its presence marks a complete set.
- **`scripts/restore_databases.py`** — pulls the latest (or a named) snapshot,
  verifies each file's sha256 against the manifest, gunzips into a target dir,
  and runs `PRAGMA integrity_check` on every restored DB.
- **`deploy/projectplanner-backup.{service,timer}`** — runs the backup daily at
  07:19 UTC, resource-capped so it can't starve the web app (HARDEN-32/40).
- **`scripts/provision_backup_s3.sh`** — one-time creation of the versioned,
  private, lifecycle-expiring bucket and a **put-only** IAM user.

### Why put-only credentials

The box holds credentials that can only `PutObject`/`ListBucket`. It cannot
delete or read snapshots. Retention is enforced server-side by an S3 lifecycle
rule (default 14 days), and versioning keeps prior object versions even if a key
is overwritten. So a compromised or buggy box can add snapshots but can never
destroy backup history. Restores use *operator* credentials, not the box's.

## Config (`/etc/projectplanner-backup.env`, mode 600, root:root)

Kept out of `/opt/projectplanner/.env` on purpose, so backup creds never enter
the app's environment.

```
AWS_ACCESS_KEY_ID=...            # from provision_backup_s3.sh CREATE_ACCESS_KEY=1
AWS_SECRET_ACCESS_KEY=...
AWS_DEFAULT_REGION=us-east-1
PM_BACKUP_S3_BUCKET=projectplanner-backups-<account-id>
PM_BACKUP_S3_PREFIX=prod
PM_BACKUP_DATA_DIR=/var/lib/projectplanner
```

## First-time setup

From an **operator machine** with admin AWS creds (not the box):

```bash
CREATE_ACCESS_KEY=1 scripts/provision_backup_s3.sh   # prints the env block above
```

On the **box**:

```bash
sudo install -m600 /dev/stdin /etc/projectplanner-backup.env   # paste the env block, ^D
sudo cp deploy/projectplanner-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now projectplanner-backup.timer
sudo systemctl start projectplanner-backup.service   # run once now, don't wait for 07:19
journalctl -u projectplanner-backup -n 40 --no-pager  # expect "manifest: s3://... (N ok, 0 failed)"
```

Verify the snapshot landed:

```bash
aws s3 ls s3://projectplanner-backups-<account-id>/prod/ --recursive | tail
systemctl list-timers projectplanner-backup.timer
```

## Restore to a fresh box (disaster recovery)

On a replacement box after installing the app (see PROVISION.md steps 1–3) but
**before** starting `projectplanner`, with **operator** AWS creds in the env
(read access to the bucket — the box's own put-only creds cannot restore):

```bash
cd /opt/projectplanner
sudo mkdir -p /var/lib/projectplanner
sudo AWS_DEFAULT_REGION=us-east-1 \
  .venv/bin/python scripts/restore_databases.py \
  --bucket projectplanner-backups-<account-id> --prefix prod \
  --target-dir /var/lib/projectplanner
# add --snapshot 2026-07-09T071900Z to pin a specific one (default: latest)
# add --force only when intentionally overwriting existing *.db files
sudo chown -R ubuntu:ubuntu /var/lib/projectplanner
```

Expect `restored <name>: <bytes> bytes, integrity ok` for each DB and a final
`restore complete: N databases`. Then start the app and confirm it boots against
the restored data:

```bash
sudo systemctl start projectplanner projectplanner-mcp
curl -s http://127.0.0.1:8110/health/deep    # task + project counts should be non-zero
```

## Proven end-to-end

Restore was proven on a scratch box on 2026-07-09: the latest prod snapshot was
restored into an isolated dir and the app booted read-only against it, serving
the real task counts. See the HARDEN-43 task activity for the evidence transcript.

## Failure handling

- One bad DB does not block the rest: the backup attempts every file and lists
  failures in `manifest.json.errors` and on stderr, exiting non-zero.
- A truncated/aborted upload leaves no `manifest.json`, so restore of that stamp
  fails fast rather than restoring a partial set.
- sha256 mismatch (corruption in transit or at rest) aborts that DB's restore.
- To alert on backup failure, the systemd unit's non-zero exit is visible via
  `systemctl is-failed projectplanner-backup.service`; wire it into the existing
  monitors sweep if desired (follow-on).
