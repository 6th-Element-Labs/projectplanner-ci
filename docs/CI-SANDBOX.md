# CI sandbox (`projectplanner-ci`)

projectplanner keeps its canonical source on the **private**
`6th-Element-Labs/projectplanner`. Private-repo GitHub Actions draw down the org's
monthly included minutes; **public repos get free, unlimited minutes**. So — the
same pattern as Helm — CI runs in a **public sandbox repo in the same org**
(`6th-Element-Labs/projectplanner-ci`) so it never spends the private budget, and
so the heavy per-PR test run moves off the production VM (where building a venv +
running the full suite per PR was the single biggest CPU load on a 1 GiB box).
Verified: the org runs Actions on this public repo for free.

| Repo | Visibility | Role |
|---|---|---|
| `6th-Element-Labs/projectplanner` | private | Canonical source, PRs, Switchboard merge webhook, provenance gate |
| `6th-Element-Labs/projectplanner-ci` | **public** | CI sandbox — push branches here; the full suite runs for free |

The sandbox receives the **full actual tree** at the exact SHA under test — it is
deliberately *not* scrubbed, so CI exercises the same code that will land on the
canonical repo. No secrets/keys/tokens/DBs are committed to the tree, so the
public sandbox exposes source and deploy topology only, never credentials. Run a
secrets scan before the first push and after any change that adds config.

## What runs on the sandbox

One workflow, [`backend-tests.yml`](../.github/workflows/backend-tests.yml), which
mirrors the on-box VM gate exactly:

1. `actions/checkout`
2. Python 3.12 + Node 20 (both preinstalled on `ubuntu-latest`)
3. `pip install -r requirements.txt`
4. `scripts/switchboard_ci.sh` with `SWITCHBOARD_CI_STRICT=1` and
   `SWITCHBOARD_CI_REQUIRE_NODE=1` — the ~40 script-style `test_*.py` plus the
   `node --check` frontend syntax gate.

The suite is hermetic (every test pins its own `PM_*` state), needs no database
service, and finishes well under the 30-minute job timeout.

## How the gate is enforced

`scripts/ci-sandbox.sh` pushes a branch to the public sandbox, dispatches the
workflow, waits for green, then **stamps a commit status
(`projectplanner-ci/full-suite`) back onto the exact SHA on the private canonical
repo** via the statuses API. Branch protection on canonical `master` requires
that status — so a PR cannot merge unless the public suite passed for that exact
SHA. Skipping the sandbox = an unmergeable PR.

This is a *test* gate. It sits **alongside** the existing SESSION-12 provenance /
claim gate (`switchboard_pr_gate.py`), which reads the live board DB and cannot
run on a public repo. After go-live, canonical `master` requires two checks:

- `projectplanner-ci/full-suite` — tests, from the public sandbox (this doc)
- `Switchboard CI / VM gate` — provenance/claim, on-box, DB-coupled (unchanged)

## Go-live (one-time, owner-run)

These steps are outward-facing / hard to reverse — do them deliberately:

```bash
# 1. Create the public sandbox repo (owner token; cloud-agent tokens often can't)
gh repo create 6th-Element-Labs/projectplanner-ci --public \
  --description "Public CI sandbox for projectplanner - full tree, all workflows"

# 2. Disable Actions on the PRIVATE canonical repo so the workflow file, once it
#    lands in the shared tree, stays inert there (no false-red on private PRs).
gh api -X PUT repos/6th-Element-Labs/projectplanner/actions/permissions \
  -f enabled=false

# 3. Move the staged workflow into place on a branch, and flip the policy test
#    (test_ci_gate_policy.py) to the new policy, in ONE PR (go-live PR):
git mv docs/ci-sandbox/backend-tests.yml .github/workflows/backend-tests.yml

# 4. Wire the sandbox and seed its baseline from canonical master
scripts/ci-sandbox.sh setup
scripts/ci-sandbox.sh refresh-main

# 5. Require the sandbox status before canonical master can move
scripts/ci-sandbox.sh protect-main

# 6. Retire the on-box heavy test runner (keep the provenance gate): stop/disable
#    the ci-gate's venv+pytest step so the box no longer runs the suite. See
#    deploy/PROVISION.md and the projectplanner-ci-gate unit.
```

Until step 2 is done, do **not** add anything under `.github/workflows/` on the
canonical repo — that is exactly the false-red the VM-gate era avoided.

## Typical branch loop

```bash
git checkout -b codex/MY-TASK-slug
# ... edit, commit ...

scripts/ci-sandbox.sh doctor                 # prove this checkout can use the sandbox
scripts/ci-sandbox.sh open-pr codex/MY-TASK-slug
#   -> pushes to projectplanner-ci, waits for green, pushes the exact SHA to
#      canonical, stamps projectplanner-ci/full-suite, opens the PR

# after merge on canonical:
scripts/ci-sandbox.sh refresh-main
scripts/ci-sandbox.sh delete codex/MY-TASK-slug
```

## Command reference

| Command | Purpose |
|---|---|
| `setup` | Create the sandbox repo (if missing) + add the `ci` remote |
| `refresh-main` | Fetch canonical `master`, seed the sandbox baseline |
| `doctor [branch]` | Verify tools, repos, remotes, baseline, workflows, branch wiring |
| `push [branch]` | Push branch, dispatch the workflow, wait for the Actions batch |
| `wait` / `status [branch]` | Wait for / print run conclusions |
| `prove [branch]` | Require the exact SHA green on the sandbox, then stamp canonical status |
| `protect-main` | Require `projectplanner-ci/full-suite` before canonical `master` can move |
| `open-pr [branch]` | push → wait green → push canonical → prove → open PR |
| `delete [branch]` | Remove a temporary branch from the sandbox |

Env overrides: `CI_REPO`, `CANONICAL_REPO`, `SANDBOX_WORKFLOWS`, `STATUS_CONTEXT`,
`MAIN_REF`, `WAIT_TIMEOUT_SEC`. Defaults are projectplanner's.

## Switchboard agents

The board coordinates tasks; it does not run GitHub Actions. The merge webhook on
the **canonical** `projectplanner` still marks tasks Done (`github_pr_merged`).
The public sandbox is intentionally **not** wired to Switchboard — only canonical
PR merges close tasks. Agent flow: claim task → `ci-sandbox.sh doctor` →
`ci-sandbox.sh open-pr <branch>` → confirm both required statuses on the PR →
`complete_claim` with the **canonical** PR URL plus the sandbox Actions URL.

## Security notes

- The sandbox receives the **full actual** tree so CI tests real code. Confirm no
  secrets/keys/`.env`/DBs are committed (they are gitignored today) before the
  first push.
- Delete sandbox feature branches after merge so stale code does not linger on a
  public repo.
- The sandbox is never wired to the Switchboard webhook; only canonical merges
  close tasks.
