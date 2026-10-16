# Public deployment and release runners

Standard hosted runners in this public repository execute deployment and release
work. Canonical source, merge authority, and product release assets remain in the
private repositories. Enterprise and organization Actions hard budgets stay $0.

## ActionEngine

`delivery-router.yml` polls canonical `main` and `totalPreProd` every five minutes.
A new SHA must have passed `ActionEngine CI / public mirror gate`. The public
deployment workflow checks the status again and verifies branch ancestry before
assuming its environment-scoped AWS role. It invokes the canonical
`deploy/total/deploy.sh` over SSM, preserving canaries, rollback, and host receipts.

For compatibility, the public workflow file remains
`actionengine-preprod-deploy.yml`; it now supports `environment=preprod` and
`environment=production`. Each environment has a separate OIDC role, a single
allowed SSM instance, and a branch policy restricted to this repository's
`master`. Production cannot skip FMP reconciliation. `DEPLOY_PREPROD_AUTO=off`
pauses automatic preprod releases. Manual rollbacks still require a gated SHA
contained in the target branch. No deploy in flight is cancelled.

```bash
gh workflow run actionengine-preprod-deploy.yml --repo 6th-Element-Labs/projectplanner-ci --ref master -f environment=preprod -f source_sha=EXACT_GATED_SHA
gh workflow run actionengine-preprod-deploy.yml --repo 6th-Element-Labs/projectplanner-ci --ref master -f environment=production -f source_sha=EXACT_GATED_SHA
```

The private `ActionEngine/deploy.yml` is retired, disabled, and guarded against
private runner allocation. Do not re-enable it or add a private relay job.

## SimpleMark

The existing `StevenRidder/simplemark-ci` sandbox continues to verify source.
This trusted public control repository handles release execution with its
existing GitHub App; no credential is copied to another repository. Tokens are
restricted to canonical `6th-Element-Labs/simplemark`.

The router discovers new canonical `v*` tags. `simplemark-release.yml` requires
the exact public full-suite result and canonical main ancestry for ordinary
releases. It builds both macOS architectures, Windows, and Linux on the original
standard native runners. Version, native capability, installer completeness,
format, checksum, and release-note checks remain in force.

Installers are handed between jobs using a temporary **private draft release**.
They are never uploaded as public Actions artifacts. The staging draft is removed
after the run. Only after all build legs pass is a complete canonical product
draft created. Validation-only runs test the same handoff and checks without
creating a product release or publishing anything.

`simplemark-release-promote.yml` remains manually dispatched. It requires a
draft and the original macOS, Windows, and Linux smoke/trust gates, with signing
material scoped to the `simplemark-release-signing` environment. Missing signing
credentials or evidence still fail closed. Emergency drafts require an explicit
reason; a skipped gate is disclosed in the draft title and notes.

```bash
gh workflow run simplemark-release.yml --repo 6th-Element-Labs/projectplanner-ci --ref master -f source_ref=main -f validation_only=true
gh workflow run simplemark-release.yml --repo 6th-Element-Labs/projectplanner-ci --ref master -f source_ref=v0.1.0 -f tag=v0.1.0 -f validation_only=false
gh workflow run simplemark-release-promote.yml --repo 6th-Element-Labs/projectplanner-ci --ref master -f tag=v0.1.0
```

Failed operations are retried explicitly. A pending, failed, or successful
canonical delivery status prevents the router from repeating that same request.
An existing product release is never overwritten.

## Validation

```bash
python3 .github/scripts/test_delivery.py
actionlint -shellcheck= .github/workflows/delivery-router.yml .github/workflows/actionengine-preprod-deploy.yml .github/workflows/simplemark-release.yml .github/workflows/simplemark-release-promote.yml
bash -n .github/scripts/actionengine_deploy.sh
```

Runtime acceptance additionally requires successful public deploy runs with live
SHA/canary readback and a successful four-platform SimpleMark validation run.
