# Native merge queue — validation canary

`master-merge-queue` (ruleset 18821466) on `master`: SQUASH, build/merge up to 5, ALLGREEN,
60-min check timeout. GitHub tests the merge-group head SHA; `github_sync.handle_merge_group`
mirrors that SHA to the scratchpad and `verify.yml` posts the required `Switchboard CI / VM gate`
status on it (CI-STRATEGY → "Native merge queue"). This PR was the enablement canary (2026-07-15).
