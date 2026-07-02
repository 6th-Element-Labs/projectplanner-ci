# QA-3 PR Lifecycle Proof

Date: 2026-07-03

This intentionally small artifact exists only to exercise the Switchboard
PR-backed task lifecycle for `QAPROOF-1`.

Expected proof chain:

- `QAPROOF-1` is claimed with an active task lease.
- A PR references `QAPROOF-1` and records branch/head/PR evidence.
- `complete_claim` moves `QAPROOF-1` to `In Review`, not `Done`.
- The VM gate runs against the PR merge ref.
- Squash merge records the resulting `merged_sha` and marks `QAPROOF-1`
  `Done` through GitHub/default-branch provenance.
- `QAPROOF-2`, which depends on `QAPROOF-1`, becomes ready only after that
  Done provenance exists.

Late-complete edge:

- `QAPROOF-3` stays claimed while its PR is opened and merged.
- After merge provenance marks `QAPROOF-3` `Done`, a late `complete_claim`
  call must release the claim and preserve `Done` instead of regressing the task
  to `In Review`.
- `QAPROOF-4` repeats the same late-complete flow after the `BUG-12` fix is
  deployed, proving the live backend reports `Done` and records preserved
  terminal provenance.
