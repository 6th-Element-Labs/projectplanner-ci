# Switchboard contributor and agent guide

This is the repository front door required by the
[repo constitution](fixtures/repo_constitution.python_modular_monolith.v1.json).
Use [docs/INDEX.md](docs/INDEX.md) to find deeper material.

## Read first

1. Read [docs/INDEX.md](docs/INDEX.md) for the current architecture and document map.
2. Treat the
   [three-plane ADR](docs/decisions/0008-three-plane-separation.md) and its
   [visual lifecycle explainer](docs/COMPLETION-LIFECYCLE-PIPELINE.md) as one
   cornerstone architecture packet.
3. For Switchboard-managed work, fetch the live project working agreement. It overrides
   the fallback [docs/WORKING-AGREEMENT.md](docs/WORKING-AGREEMENT.md) for workflow and
   provenance rules.
4. Read only the ADRs and specs relevant to the surface being changed. The
   [decision register](docs/decisions/INDEX.md) distinguishes current authority,
   program history, proposals, and superseded material.

If accepted architecture, executable enforcement, and current code disagree, surface the
conflict. Do not silently choose whichever source makes the change easiest.

## Non-negotiable architecture

Switchboard has three independent control planes:

- **Capacity** owns physical execution presence and managed-process lifecycle.
  `runner_sessions` is the liveness authority.
- **Communication** owns message storage, delivery, acknowledgement, and delivery truth.
  Messages and timeouts have no execution or coordination authority.
- **Coordination** owns explicitly started work through review, remediation, merge, and
  proven Done. It requests capacity only through `start_task`.

No state, timeout, or inference in one plane may impersonate authority from another. The
complete rules are C1-C3, M1-M3, and W1-W4 in the three-plane ADR; this summary does not
replace them.

Other current invariants:

- Canonical merge provenance, or verifier-stamped offline evidence for non-code work, owns
  Done. Agents do not self-declare Done.
- Enforce at dispatch, observe at merge, and stamp at Done. GitHub required contexts and
  the merge queue own landing; Switchboard merge authorization is advisory.
- Caddy remains the edge. Do not introduce a second reverse proxy.
- New product code lands under `src/switchboard/`; existing root modules are compatibility
  shims, not destinations for new modules.
- REST and MCP adapters remain thin and share application commands and queries.
- New SQL and backend-specific storage behavior stay under `src/switchboard/storage/`.

## Coding rules

- Use Python 3.12 or newer and the locked dependencies.
- Use explicit imports in new modules. Do not add `import *`.
- Use typed Pydantic request/response models for new HTTP contracts; do not add untyped
  `body: dict` routes.
- Keep domain and application code independent of FastAPI, MCP, SQLite, and provider SDK
  details. Put those details in adapters.
- Preserve project isolation and pass project identity through declared interfaces.
- Make failure and fallback states explicit. Do not turn missing evidence into a green result.
- Prefer subtraction and reuse over another gate, daemon, ledger, lifecycle owner, or
  parallel mechanism.

## Validation

The canonical local gate is:

```bash
scripts/switchboard_ci.sh
```

It discovers executable `test_*.py` and `*_test.py` files and runs each in its own Python
process. Tests must therefore pass when executed directly, not only under a pytest fixture
environment.

For documentation-only changes, at minimum run `git diff --check`. Also verify local
Markdown links when changing document paths or indexes.

Do not renumber or rewrite accepted ADR history as drive-by cleanup. Add an explicit
supersession record.

## Where information belongs

| Information | Home |
|---|---|
| Current repo map and reading path | `docs/INDEX.md` |
| Durable architectural choice and rejected alternatives | `docs/decisions/` |
| Current coding and placement rules | this file |
| Product or protocol contract | active spec under `docs/` |
| Operational recovery procedure | `docs/runbooks/` |
| Temporary execution sequence | board plus an execution tracker |
| Evidence, audit, or completed migration narrative | evidence or archive material, not a front door |
