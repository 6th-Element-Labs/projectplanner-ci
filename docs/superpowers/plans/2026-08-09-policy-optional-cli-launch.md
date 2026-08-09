# Policy-Optional MCP CLI Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the canonical MCP and Autopilot launch path for every unconfigured board project without requiring execution policy, while preserving strict immutable Execution Context enforcement for projects that explicitly opt in.

**Architecture:** `task_execution.start_task` remains the only admission door. `connect_dispatch.enqueue_task` branches solely on whether the project has an activated execution policy: active projects resolve the current immutable Execution Context; absent/draft projects emit a context-less Connect wake carrying canonical repository identity. Agent Host materializes both forms into private task worktrees, including exact-head review/remediation workspaces, and refuses compatibility wakes unless the selected source checkout's origin matches the project binding.

**Tech Stack:** Python 3.12, Switchboard MCP application commands, Connect wake contracts, Agent Host repository worktrees, direct executable regression scripts.

## Global Constraints

- The behavior is project-independent: no project-name allowlist, Maxwell exception, environment switch, or repository-specific launch branch.
- Manual MCP Start and Autopilot must both use `task_execution.start_task`; neither may dispatch a wake directly.
- Missing or draft execution policy means compatibility mode; active-but-invalid policy fails closed.
- Unconfigured launches do not require project execution readiness, provider selectors, SCM selectors, hybrid placement, or Autopilot execution-policy configuration.
- Configured launches retain exact repository, base SHA, provider, SCM, placement, checkout, and credential-generation enforcement.
- Every Connect process launches from a verified private task workspace, never the application checkout.
- Dependency, ownership, generation, idempotency, capacity, runner-fencing, and completion-provenance gates remain unchanged.

---

## File Map

- `src/switchboard/application/commands/connect_dispatch.py`: select configured immutable-context dispatch or unconfigured compatibility dispatch, then build one common assignment/wake.
- `adapters/agent_host.py`: derive context-less workspace requests, including the exact target SHA for review/remediation.
- `adapters/agent_host_enrollment.py`: advertise durable project-to-source-checkout bindings to the host process.
- `adapters/repository_workspace.py`: materialize and verify a context-less private worktree at an optional exact checkout SHA.
- `tests/test_unconfigured_project_legacy_dispatch.py`: behavioral proof that arbitrary unconfigured projects dispatch without context and configured projects remain strict.
- `tests/test_harden78_legacy_defaults.py`: replace the obsolete “legacy path must not exist” source ratchets with policy-optional invariants.
- `tests/test_adapter28_workspace_launch.py`: prove compatibility wakes launch implementation and exact-head roles safely in private worktrees.
- `tests/test_policy_optional_start_surfaces.py`: prove MCP/manual and Autopilot both reach the same `task_execution.start_task` and Connect seam.

### Task 1: Restore the project-independent Connect compatibility branch

**Files:**
- Modify: `tests/test_unconfigured_project_legacy_dispatch.py`
- Modify: `tests/test_harden78_legacy_defaults.py`
- Modify: `tests/test_bug190_readiness_gate_optin_only.py`
- Modify: `src/switchboard/application/commands/connect_dispatch.py`

**Interfaces:**
- Consumes: `get_project_execution_policy(project: str) -> dict`, `execution_context.resolve(...) -> dict`, `execution_context.with_checkout_sha(...) -> dict`.
- Produces: `enqueue_task(...) -> dict` with either a configured wake containing `policy.execution_context` and hybrid placement, or an unconfigured wake containing neither.

- [ ] **Step 1: Rewrite the legacy-dispatch regression to express the approved behavior**

Replace the unconfigured refusal assertion with two arbitrary project dispatches and one configured strict dispatch:

```python
def policy(configured):
    return {"configured": configured}

for project in ("maxwell", "future-board-created-after-deploy"):
    project_execution_policy.get_project_execution_policy = lambda _p: policy(False)
    execution_context.resolve = lambda **_kw: (_ for _ in ()).throw(
        AssertionError("unconfigured launch must not resolve execution context")
    )
    result = connect_dispatch.enqueue_task(dict(TASK), project=project, actor="compat-test")
    assert result["dispatched"] is True
    wake_policy = captured[-1]["policy"]
    assert "execution_context" not in wake_policy
    assert "placement" not in wake_policy
    assert wake_policy["assignment"]["workspace_ref"] == "repo:canonical"
```

Retain a configured case that returns a ready context and asserts immutable context plus hybrid placement are present.

- [ ] **Step 2: Run the rewritten regression and verify it fails**

Run:

```bash
.venv/bin/python tests/test_unconfigured_project_legacy_dispatch.py
```

Expected: FAIL because `execution_context.resolve` is still called for both unconfigured projects and no wake is requested.

- [ ] **Step 3: Restore the opt-in branch in Connect**

In `enqueue_task`, load policy once and resolve context only when configured:

```python
from switchboard.storage.repositories.project_execution_policy import (
    get_project_execution_policy,
)

context: dict[str, Any] = {}
    activated = bool(get_project_execution_policy(project).get("activated"))
    if activated:
    try:
        context = execution_context.resolve(
            project=project, task_id=task_id, runtime=runtime_name)
    except execution_context.ExecutionContextError as exc:
        return {"dispatched": False, **exc.as_dict(),
                "task_id": task_id, "project": project}
    except Exception as exc:
        return {
            "dispatched": False,
            "error": "execution_context_unavailable",
            "reason": str(exc),
            "failure_class": "broken_connection",
            "task_id": task_id,
            "project": project,
        }
```

Build the assignment and policy conditionally:

```python
workspace_ref = (
    f"repo:{context['repo_role']}:{context['repository']}@{context['base_sha']}"
    if context else "repo:canonical"
)
policy = {
    "mode": CONNECT_WAKE_MODE,
    **(_hybrid_policy(context, task, runtime_name) if context else {}),
    "assignment": {"schema": "switchboard.connect.assignment.v1", **asdict(assignment)},
    "lifecycle": lifecycle,
    "coordination_scope": coordination_scope,
}
if context:
    policy["execution_context"] = context
```

Keep configured exact-head validation unchanged. For an unconfigured exact-head role, validate the assigned head against persisted `git_state.head_sha`, but do not call `execution_context.with_checkout_sha`.

- [ ] **Step 4: Replace obsolete HARDEN-78 source ratchets**

Change the source assertions to require the compatibility branch without weakening unrelated hardening:

```python
ok('get_project_execution_policy(project).get("activated")' in connect,
   "Connect gates immutable context on explicit project opt-in")
ok('"repo:canonical"' in connect,
   "unconfigured projects retain the provider-neutral workspace reference")
ok("if context:" in connect[connect.index("def enqueue_task("):],
   "Connect includes immutable context only for configured projects")
```

Retain the existing assertions that forbid application-checkout execution, ambient GitHub tokens, and invented `master` defaults.

Update `tests/test_bug190_readiness_gate_optin_only.py` so its final source assertion requires the opt-in branch instead of forbidding it.

- [ ] **Step 5: Run the Connect policy-mode regressions**

Run:

```bash
.venv/bin/python tests/test_unconfigured_project_legacy_dispatch.py
.venv/bin/python tests/test_bug190_readiness_gate_optin_only.py
.venv/bin/python tests/test_harden78_legacy_defaults.py
```

Expected: all pass; the arbitrary future project and Maxwell both request context-less wakes, and configured policy remains fail-closed.

- [ ] **Step 6: Commit Task 1**

```bash
git add src/switchboard/application/commands/connect_dispatch.py \
  tests/test_unconfigured_project_legacy_dispatch.py \
  tests/test_bug190_readiness_gate_optin_only.py \
  tests/test_harden78_legacy_defaults.py
git commit -m "fix: restore policy-optional Connect dispatch"
```

### Task 2: Preserve private and exact-head workspaces without policy

**Files:**
- Modify: `tests/test_adapter28_workspace_launch.py`
- Modify: `adapters/agent_host.py`
- Modify: `adapters/repository_workspace.py`

**Interfaces:**
- Consumes: `connect_workspace_request(wake, inventory) -> dict`, lifecycle `role`, `head_sha`, and `pr_branch`.
- Produces: `materialize_host_worktree(..., checkout_sha: str = "") -> MaterializedWorkspace` and matching `verify_host_worktree` behavior.

- [ ] **Step 1: Add a failing context-less exact-head launch test**

Create a local PR branch at an exact commit, remove `execution_context`, rebuild `execution_assignment`, and assert review launch uses that commit:

```python
def test_legacy_review_wake_launches_at_exact_head(root):
    remote, _ = action_engine_remote(root)
    source = root / "sources" / "ActionEngine"
    git("remote", "add", "origin", remote, cwd=source)
    git("checkout", "-b", "agent/review-branch", cwd=source)
    (source / "review.txt").write_text("review\n", encoding="utf-8")
    git("add", "review.txt", cwd=source)
    git("commit", "-m", "review head", cwd=source)
    head = git("rev-parse", "HEAD", cwd=source).strip()
    git("checkout", "master", cwd=source)

    wake = connect_wake(context(head), execution_id="execlease-legacy-review")
    wake["policy"].pop("execution_context")
    wake["policy"]["lifecycle"].update({
        "role": "review_merge", "head_sha": head,
        "pr_branch": "agent/review-branch",
    })
    wake["policy"]["execution_assignment"] = build_execution_assignment(
        task_id="ADAPTER-28", assignment=wake["policy"]["assignment"],
        lifecycle=wake["policy"]["lifecycle"],
    )
    inventory = host_inventory()
    inventory["repo_root"] = str(source)
    with Launcher(remote):
        result = agent_host.launch(wake, inventory, runner_session_id="run-legacy-review")
    assert result["metadata"]["workspace_receipt"]["base_sha"] == head
    assert git("rev-parse", "HEAD", cwd=Path(result["cwd"])).strip() == head
```

- [ ] **Step 2: Run the Agent Host regression and verify it fails**

Run:

```bash
.venv/bin/python tests/test_adapter28_workspace_launch.py
```

Expected: FAIL with `workspace_branch_base_mismatch` because context-less materialization currently assumes the enrolled checkout's current HEAD.

- [ ] **Step 3: Carry exact checkout intent into the host-worktree request**

In `connect_workspace_request`, add an optional SHA only for exact-head roles:

```python
compat_checkout_sha = ""
if exact_head_role and not context:
    lifecycle_head = str(lifecycle.get("head_sha") or "").strip().lower()
    assignment_head = str(
        execution_assignment.get("exact_head_sha") or ""
    ).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", lifecycle_head) or lifecycle_head != assignment_head:
        raise WorkspaceMaterializationError(
            "workspace_exact_head_mismatch",
            "lifecycle head and assignment head must agree",
            role=desired_role,
        )
    compat_checkout_sha = lifecycle_head
```

Include `checkout_sha=compat_checkout_sha` in the context-less request.

- [ ] **Step 4: Materialize and verify the requested context-less SHA**

Extend both host-worktree functions with `checkout_sha: str = ""`. In `materialize_host_worktree`, validate the optional SHA and select it before creating/reusing the branch:

```python
requested_sha = str(checkout_sha or "").strip().lower()
if requested_sha and not _SHA.fullmatch(requested_sha):
    raise WorkspaceMaterializationError(
        "workspace_checkout_sha_invalid",
        "host worktree checkout SHA must be a full lowercase SHA-1",
    )
base_sha = requested_sha or str(
    existing_receipt.get("base_sha") or resolved["source_head"]
)
```

Include `checkout_sha` in the host-worktree static identity/receipt expectation so retries cannot reuse a workspace for another head. `verify_host_worktree` must require the same SHA from the request and receipt.

- [ ] **Step 5: Run workspace and launch regressions**

Run:

```bash
.venv/bin/python tests/test_adapter28_workspace_launch.py
.venv/bin/python tests/test_adapter27_repository_workspace.py
.venv/bin/python tests/test_bug168_immutable_assignment_contract.py
```

Expected: all pass; implementation compatibility wakes still use source HEAD, while review/remediation compatibility wakes use their exact assigned head.

The compatibility wake must also carry `policy.repository_binding` from the project's canonical repo topology. The host selects `inventory.project_source_repo_roots[project]` when present, otherwise accepts its default checkout only when that checkout's host/owner/repository identity matches the bound repository. Add a two-project-host test proving the correct root is selected, a same-slug wrong-host origin is rejected, and a missing/wrong binding starts no process. Enrollment and update must accept, validate, persist, and sandbox repeatable `--project-source PROJECT=/absolute/git/checkout` inputs so the happy path requires no manual config edit.

- [ ] **Step 6: Commit Task 2**

```bash
git add adapters/agent_host.py adapters/repository_workspace.py \
  tests/test_adapter28_workspace_launch.py
git commit -m "fix: preserve exact heads for policy-free host worktrees"
```

### Task 3: Prove MCP and Autopilot share the same launch door

**Files:**
- Create: `tests/test_policy_optional_start_surfaces.py`
- Verify: `src/switchboard/mcp/tools/task_execution.py`
- Verify: `src/switchboard/application/mission_bot_v4/runtime.py`
- Verify: `src/switchboard/application/commands/task_execution.py`

**Interfaces:**
- Consumes: MCP `start_task(...)`, `mission_bot_v4.runtime.build_ports(...).start_task`, and `task_execution.start_task(...)`.
- Produces: regression evidence that neither surface invokes Connect or wake storage directly.

- [ ] **Step 1: Add source and behavioral invariants for both entry points**

```python
def test_mcp_and_autopilot_delegate_to_task_execution_start():
    mcp_source = source("src/switchboard/mcp/tools/task_execution.py")
    runtime_source = source("src/switchboard/application/mission_bot_v4/runtime.py")
    assert '_run("start_task"' in mcp_source
    assert "task_execution.start_task(" in runtime_source
    for text in (mcp_source, runtime_source):
        assert "connect_dispatch.enqueue_task(" not in text
        assert "request_wake(" not in text

def test_unconfigured_manual_and_autopilot_starts_reach_one_connect_seam():
    calls = []
    original_start = task_execution.start_task
    try:
        def shared_start(task_id, **kwargs):
            calls.append((task_id, kwargs["project"]))
            return {"action": "started", "started": True}

        task_execution.start_task = shared_start
        ports = mission_runtime.production_ports(
            actor="surface-test",
            agent_id="codex/policy-optional-surface",
            scope_project="future-board-created-after-deploy",
            store_mod=SimpleNamespace(
                validate_autopilot_scope_authority=lambda *_a, **_kw: {
                    "allowed": True
                },
                get_task=lambda *_a, **_kw: {"task_id": "FUTURE-1"},
                task_has_live_execution=lambda *_a, **_kw: False,
                list_wake_intents=lambda **_kw: [],
            ),
        )
        manual = task_execution.start_task(
            "FUTURE-1", project="future-board-created-after-deploy"
        )
        automatic = ports.start_task(
            "FUTURE-1",
            project="future-board-created-after-deploy",
            role="implementation",
            scope_authority={"scope_id": "scope-future"},
        )
        assert manual["started"] is True
        assert automatic["started"] is True
        assert calls == [
            ("FUTURE-1", "future-board-created-after-deploy"),
            ("FUTURE-1", "future-board-created-after-deploy"),
        ]
    finally:
        task_execution.start_task = original_start
```

Import `SimpleNamespace` from `types`, `task_execution`, and
`mission_bot_v4.runtime as mission_runtime`. This test intentionally patches the
single canonical command, not Connect or wake storage; the source assertions
prove both surfaces are adapters around that command.

- [ ] **Step 2: Run the new test and verify any missing shared-door behavior fails**

Run:

```bash
.venv/bin/python tests/test_policy_optional_start_surfaces.py
```

Expected before final wiring: source invariants pass; the behavioral assertion exposes any Autopilot-specific policy/readiness gate.

- [ ] **Step 3: Remove any Autopilot-only execution-policy gate discovered by the test**

The allowed implementation is delegation to Task Execution:

```python
return task_execution.start_task(
    task_id,
    project=str(kwargs["project"]),
    actor=actor,
    agent_id=agent_id,
    role=str(kwargs["role"]),
    source_sha=str(kwargs.get("source_sha") or ""),
    instruction=str(kwargs.get("instruction") or ""),
    mission_key=str(kwargs.get("mission_key") or ""),
    mission_launch_pointer=dict(kwargs.get("mission_launch_pointer") or {}),
)
```

If current code already matches this exactly, make no production edit; the regression test is the deliverable.

- [ ] **Step 4: Run manual/Autopilot launch tests**

Run:

```bash
.venv/bin/python tests/test_policy_optional_start_surfaces.py
.venv/bin/python tests/test_bug144_autopilot_triage_start.py
.venv/bin/python tests/test_bug190_readiness_gate_optin_only.py
```

Expected: all pass and both surfaces traverse the same Task Execution/Connect chain.

- [ ] **Step 5: Commit Task 3**

```bash
git add tests/test_policy_optional_start_surfaces.py \
  src/switchboard/application/mission_bot_v4/runtime.py
git commit -m "test: pin one policy-optional launch door"
```

If `runtime.py` required no edit, omit it from `git add`.

### Task 4: Verify the compatibility restoration end to end

**Files:**
- Verify only: all files changed in Tasks 1-3

**Interfaces:**
- Consumes: the repository's executable test scripts and canonical CI script.
- Produces: clean diff, targeted launch proof, and full-suite evidence ready for review.

- [ ] **Step 1: Run the focused compatibility matrix**

```bash
.venv/bin/python tests/test_unconfigured_project_legacy_dispatch.py
.venv/bin/python tests/test_bug190_readiness_gate_optin_only.py
.venv/bin/python tests/test_harden78_legacy_defaults.py
.venv/bin/python tests/test_adapter28_workspace_launch.py
.venv/bin/python tests/test_policy_optional_start_surfaces.py
.venv/bin/python tests/test_bug144_autopilot_triage_start.py
```

Expected: every script exits 0.

- [ ] **Step 2: Run the canonical repository gate**

```bash
PATH="$PWD/.venv/bin:$PATH" scripts/switchboard_ci.sh
```

Expected: exit 0 with every discovered executable test passing.

- [ ] **Step 3: Verify repository hygiene**

```bash
git diff --check
git status --short
git log --oneline --decorate -4
```

Expected: no whitespace errors; only intentional plan/spec/implementation changes are present; commits are scoped.

- [ ] **Step 4: Request code review**

Use `superpowers:requesting-code-review` against the complete branch diff. Address only findings that affect the approved compatibility contract, security boundary, or test validity.

- [ ] **Step 5: Publish and deploy through the repository's normal workflow**

Push the branch and open a PR. Do not claim runtime restoration until the change is merged, deployed, and the installed Agent Host/control plane report matching contract versions.

- [ ] **Step 6: Launch and verify the three Total tasks through MCP**

After deployment, call MCP `start_task(project="maxwell", runtime="codex")` for:

```text
BUG-8
MXBT-1
REPORT-17
```

Verify each with `get_task_execution`: a task is launched only when `running=true`, `starting=true`, or a live `wake_status` is present. Report truthful capacity refusals if the personal CLI account serializes the starts; do not reinterpret them as policy failures.
