# Mindless Operator Launch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make opt-in launcher boot (`intent=launch|operator|start`) point agents at `start_task` (UI Start door) while leaving the default worker boot path byte-stable for direct human↔agent work.

**Architecture:** Branch only inside `session_boot` builders and working-agreement payload on a normalized launcher-intent helper. Keep `start_task` → Connect as the sole launch primitive; enrich `unsupported_runtime` refusals with an explicit repair string so `runtime=cli` steers back to `codex`/`claude` + `start_task`.

**Tech Stack:** Python application layer (`src/switchboard/application/`), store working-agreement query, hermetic pytest-style scripts under `tests/` using existing `ok()` helpers.

**Spec:** `docs/superpowers/specs/2026-07-24-mindless-operator-launch-design.md`

## Global Constraints

- Launch path is **opt-in only**. Empty / `work` / `implement` / unknown `intent` must keep today’s worker `first_calls` and `startup_prompt`.
- Launcher intents (case-insensitive): `launch`, `operator`, `start` only.
- No new Connect runtime named `cli`. No batch `launch_tasks` tool in v1.
- Do not change UI Start. Do not refuse `claim_task` from launcher ids in v1.
- Serialize MCP writes when manually verifying fan-out; plan tests are unit-level.

## File map

| File | Responsibility |
|------|----------------|
| `src/switchboard/application/session_boot.py` | `is_launcher_intent`, launcher agent_id suggestion, branched `build_startup_prompt` / `build_first_calls` / `prepare_agent_session` |
| `src/switchboard/application/queries/working_agreement.py` | Add `session_start_sequence_launcher`; leave default sequence unchanged |
| `src/switchboard/application/commands/connect_dispatch.py` | Richer `unsupported_runtime` reason/repair on enqueue |
| `tests/test_operator_launch_boot.py` | New hermetic tests for opt-in launch vs default worker |
| `tests/test_arch_ms68_session_boot.py` | Keep passing (worker regression); extend only if signature requires |
| `docs/MCP.md` | One short note: operator launch uses `prepare_agent_session(intent=launch)` then `start_task` |

---

### Task 1: Launcher intent helper + agent_id suggestion

**Files:**
- Modify: `src/switchboard/application/session_boot.py`
- Create: `tests/test_operator_launch_boot.py`

**Interfaces:**
- Produces: `is_launcher_intent(intent: str) -> bool`
- Produces: `suggest_agent_id(..., intent: str = "")` — when launcher intent and no explicit `agent_id`, return `{runtime}/launcher` (runtime default `cursor` if empty); never `cli/<TASK>-…` from launcher suggestion
- Consumes: existing `suggest_agent_id` callers — add optional `intent=""` kw-only default so worker call sites stay valid

- [ ] **Step 1: Write the failing tests**

Create `tests/test_operator_launch_boot.py` using the ARCH-MS-68 env-before-import pattern (so later prepare/store tests share one hermetic DB):

```python
#!/usr/bin/env python3
"""Opt-in operator/launcher boot via prepare_agent_session(intent=launch)."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="operator-launch-boot-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_AUTH_MODE"] = "dev-open"
os.environ.pop("PM_MCP_TOKEN", None)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from switchboard.application import session_boot  # noqa: E402

passed = failed = 0


def ok(cond: bool, msg: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  ok  {msg}")
    else:
        failed += 1
        print(f"FAIL  {msg}")


def test_intent_helper() -> None:
    for value in ("launch", "OPERATOR", "Start", " launch "):
        ok(session_boot.is_launcher_intent(value) is True,
           f"launcher intent recognized: {value!r}")
    for value in ("", "work", "implement", "unit-test", "cli", None):
        ok(session_boot.is_launcher_intent(value) is False,
           f"non-launcher intent ignored: {value!r}")


def test_launcher_agent_id() -> None:
    aid = session_boot.suggest_agent_id(
        "cursor", "", "COORD-41", "COORD",
        {"title": "attention projection"}, intent="launch")
    ok(aid == "cursor/launcher",
       "launcher suggest_agent_id is runtime/launcher, not task-scoped")
    explicit = session_boot.suggest_agent_id(
        "cursor", "desktop/steve-launcher", "COORD-41", "COORD",
        {"title": "x"}, intent="launch")
    ok(explicit == "desktop/steve-launcher",
       "explicit agent_id wins over launcher suggestion")
    worker = session_boot.suggest_agent_id(
        "cursor", "", "COORD-41", "COORD",
        {"title": "attention projection"}, intent="")
    ok(worker.startswith("cursor/COORD-41-"),
       "default worker suggest_agent_id still task-scoped")


if __name__ == "__main__":
    try:
        test_intent_helper()
        test_launcher_agent_id()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
```

In Tasks 2–4, append tests into this same file and keep a single `try`/`finally` that cleans `TMP` once at the end (move cleanup out of individual tests).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tests/test_operator_launch_boot.py`

Expected: FAIL — `is_launcher_intent` missing / `suggest_agent_id` unexpected keyword `intent`

- [ ] **Step 3: Minimal implementation**

In `src/switchboard/application/session_boot.py`, add:

```python
_LAUNCHER_INTENTS = frozenset({"launch", "operator", "start"})


def is_launcher_intent(intent: str | None) -> bool:
    return str(intent or "").strip().lower() in _LAUNCHER_INTENTS
```

Update `suggest_agent_id` signature to accept `intent: str = ""` and, after the explicit `agent_id` early return:

```python
    if is_launcher_intent(intent):
        rt = (runtime or "").strip() or "cursor"
        return f"{rt}/launcher"
```

Keep the existing task/lane worker logic unchanged below that branch.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 tests/test_operator_launch_boot.py`

Expected: all intent + agent_id checks PASS

- [ ] **Step 5: Commit**

```bash
git add src/switchboard/application/session_boot.py tests/test_operator_launch_boot.py
git commit -m "$(cat <<'EOF'
feat: add opt-in launcher intent helper for session boot

EOF
)"
```

---

### Task 2: Launcher `first_calls` + `startup_prompt` (worker path unchanged)

**Files:**
- Modify: `src/switchboard/application/session_boot.py` (`build_startup_prompt`, `build_first_calls`, `prepare_agent_session`)
- Modify: `tests/test_operator_launch_boot.py`
- Verify: `tests/test_arch_ms68_session_boot.py` still passes

**Interfaces:**
- Consumes: `is_launcher_intent`, launcher `suggest_agent_id`
- Produces: when launcher intent + `task_id`, `first_calls` includes
  `{"tool": "start_task", "args": {"task_id", "project", "runtime": "codex", "role": "implementation"}}`
  and does **not** include `get_task` / `claim_task`
- Produces: launcher `startup_prompt` contains `start_task` and the phrases `do not claim` (or `Do not claim`) and forbids local worktree language
- Produces: `prepare_agent_session(..., intent="launch")` wires the above; `intent=""` / `"unit-test"` still ends with `get_task`

- [ ] **Step 1: Write the failing integration assertions**

Append to `tests/test_operator_launch_boot.py` (reuse hermetic task creation pattern from `tests/test_arch_ms68_session_boot.py` — create a BOOT lane task on switchboard, or call builders directly without DB when possible).

Prefer pure builders first, then one `prepare_agent_session` round-trip:

```python
def test_launcher_first_calls_and_prompt() -> None:
    agreement = {"protocol": {"name": "switchboard", "version": "ixp.v1"}}
    calls = session_boot.build_first_calls(
        "switchboard", "cursor/launcher", "cursor", "",
        "COORD-41", "COORD", agreement, intent="launch")
    tools = [c["tool"] for c in calls]
    ok(tools[:4] == [
        "get_working_agreement", "register_agent",
        "list_unacked_messages", "list_unblock_requests",
    ], "launcher first_calls keep handshake prefix")
    ok("get_task" not in tools, "launcher first_calls omit get_task")
    ok("claim_task" not in tools and "claim_next" not in tools,
       "launcher first_calls omit claim tools")
    start = next(c for c in calls if c["tool"] == "start_task")
    ok(start["args"] == {
        "task_id": "COORD-41",
        "project": "switchboard",
        "runtime": "codex",
        "role": "implementation",
    }, "launcher first_calls end with start_task codex implementation")

    prompt = session_boot.build_startup_prompt(
        "switchboard", "cursor/launcher", "COORD-41", "COORD",
        intent="launch")
    ok("start_task" in prompt, "launcher prompt names start_task")
    ok("do not claim" in prompt.lower(), "launcher prompt forbids claim")
    ok("worktree" in prompt.lower() and "do not" in prompt.lower(),
       "launcher prompt forbids local worktree for launched tasks")

    worker_calls = session_boot.build_first_calls(
        "switchboard", "cursor/COORD-41-x", "cursor", "",
        "COORD-41", "COORD", agreement, intent="")
    worker_tools = [c["tool"] for c in worker_calls]
    ok("start_task" not in worker_tools, "default first_calls omit start_task")
    ok("get_task" in worker_tools, "default first_calls still include get_task")


def test_prepare_opt_in_only() -> None:
    import store
    store.init_project_registry()
    store.init_db("switchboard")
    created = store.create_task({
        "workstream_id": "BOOT",
        "workstream_name": "Boot lane",
        "title": "Launcher boot fixture",
        "description": "hermetic fixture for intent=launch",
    }, project="switchboard")
    tid = created["task_id"]
    boot_launch = session_boot.prepare_agent_session(
        runtime="cursor", project="switchboard", task_id=tid,
        intent="launch")
    boot_default = session_boot.prepare_agent_session(
        runtime="cursor", project="switchboard", task_id=tid,
        intent="unit-test")
    launch_tools = [c["tool"] for c in boot_launch["first_calls"]]
    default_tools = [c["tool"] for c in boot_default["first_calls"]]
    ok(boot_launch.get("mode") == "launcher", "launch boot sets mode=launcher")
    ok(boot_default.get("mode") == "worker", "default boot sets mode=worker")
    ok("start_task" in launch_tools and "get_task" not in launch_tools,
       "prepare(intent=launch) first_calls use start_task")
    ok("start_task" not in default_tools and "get_task" in default_tools,
       "prepare without launcher intent keeps worker get_task")
    ok(boot_launch.get("agent_id") == "cursor/launcher",
       "prepare(intent=launch) suggests cursor/launcher")
```

Uses the module-level hermetic `TMP` from Task 1 (env set before `store` import).

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 tests/test_operator_launch_boot.py`

Expected: FAIL — `build_first_calls` / `build_startup_prompt` unexpected keyword `intent`, or no `start_task` in calls

- [ ] **Step 3: Minimal implementation**

1. Add `intent: str = ""` to `build_first_calls` and `build_startup_prompt`.
2. When `is_launcher_intent(intent)`:
   - **Prompt:** after the shared project-boundary lines, use a launcher boot sequence instead of get_task/claim language:

```text
You are a Switchboard launcher (operator) on this project — not the implementing CLI agent.
Boot sequence:
1. get_working_agreement(...)
2. register_agent(... launcher agent_id ...)
3. list_unacked_messages(...)
4. list_unblock_requests(...)
5. start_task(task_id="...", project="...", runtime="codex", role="implementation")
   — or call start_task once per task_id when launching a set (serialize writes).
Do not claim_task or claim_next. Do not open a local worktree for launched tasks.
Connect boots the CLI (codex/claude); poll get_task_execution for status.
```

   - If no `task_id`, omit step 5’s concrete call; keep the “once per task_id” rule.
   - **first_calls:** handshake + (optional get_project_contract — keep it for project binding) + `start_task` when `task_id` set. Omit `get_task` / mission claim guidance. For deliverable-only launcher boots without task_id, keep inbox drain + prompt rule only (no invented task start).
3. In `prepare_agent_session`, pass `intent` into `suggest_agent_id`, `build_first_calls`, and `build_startup_prompt`. Echo `mode: "launcher"|"worker"` on the boot payload for clarity (`"mode": "launcher" if is_launcher_intent(intent) else "worker"`).

Keep worker branches identical to today when intent is not launcher.

- [ ] **Step 4: Run tests**

Run:
```bash
python3 tests/test_operator_launch_boot.py
python3 tests/test_arch_ms68_session_boot.py
```

Expected: both PASS; ARCH-MS-68 proves worker path unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/switchboard/application/session_boot.py tests/test_operator_launch_boot.py
git commit -m "$(cat <<'EOF'
feat: opt-in launcher boot routes first_calls to start_task

EOF
)"
```

---

### Task 3: Working agreement launcher sequence

**Files:**
- Modify: `src/switchboard/application/queries/working_agreement.py`
- Modify: `tests/test_operator_launch_boot.py` (or add assertion in `test_agent_bootstrap.py` pattern)

**Interfaces:**
- Produces: `session_start_sequence_launcher` list on agreement dict
- Produces: default `session_start_sequence` unchanged

- [ ] **Step 1: Write the failing test**

```python
def test_working_agreement_launcher_sequence() -> None:
    import store
    agreement = store.get_working_agreement(project="switchboard")
    ok("session_start_sequence_launcher" in agreement,
       "working agreement advertises launcher sequence")
    seq = agreement["session_start_sequence_launcher"]
    ok(any("start_task" in step for step in seq),
       "launcher sequence names start_task")
    ok(any("do not claim" in step.lower() or "not claim" in step.lower()
           for step in seq),
       "launcher sequence forbids claim")
    worker = agreement["session_start_sequence"]
    ok(worker[0].startswith("get_working_agreement"),
       "default session_start_sequence unchanged prefix")
    ok(not any("start_task" in step for step in worker),
       "default sequence is still worker/claim oriented")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_operator_launch_boot.py`

Expected: FAIL — missing `session_start_sequence_launcher`

- [ ] **Step 3: Minimal implementation**

In `working_agreement.py` default dict (alongside `session_start_sequence`), add:

```python
        "session_start_sequence_launcher": [
            "get_working_agreement(project)",
            "register_agent (launcher identity; intent=launch|operator|start)",
            "inbox(unacked)",
            "for each task: start_task(task_id, runtime=codex|claude|cursor) — serialize writes",
            "poll get_task_execution — do not claim",
        ],
```

Do not alter `session_start_sequence` or deliverable sequences.

- [ ] **Step 4: Run tests**

Run: `python3 tests/test_operator_launch_boot.py`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/switchboard/application/queries/working_agreement.py tests/test_operator_launch_boot.py
git commit -m "$(cat <<'EOF'
feat: advertise launcher session_start_sequence in working agreement

EOF
)"
```

---

### Task 4: `unsupported_runtime` repair string + MCP doc note

**Files:**
- Modify: `src/switchboard/application/commands/connect_dispatch.py` (`enqueue_task` ValueError branch)
- Modify: `src/switchboard/application/commands/task_execution.py` if needed so `reason`/`repair` reach `TaskExecutionError.as_dict()`
- Create or extend: assertion in `tests/test_operator_launch_boot.py` (unit-test `enqueue_task` with fake task dict, no host required)
- Modify: `docs/MCP.md` — short “Operator launch” bullet under session start / task execution

**Interfaces:**
- Produces: `enqueue_task(..., runtime="cli")` returns
  `dispatched=False`, `error="unsupported_runtime"`, and `reason` (and `repair`) containing:
  `Use runtime=codex or runtime=claude (Connect boots the CLI). cli is not a Connect runtime. From a controller/launcher session call start_task; do not claim_task.`
- Produces: `start_task` refusal `message` surfaces that repair text (via existing `reason or error` mapping)

- [ ] **Step 1: Write the failing test**

```python
def test_cli_runtime_repair() -> None:
    from switchboard.application.commands import connect_dispatch
    result = connect_dispatch.enqueue_task(
        {"task_id": "COORD-41", "_wsId": "COORD"},
        project="switchboard", actor="test", runtime="cli")
    ok(result.get("dispatched") is False, "cli runtime does not dispatch")
    ok(result.get("error") == "unsupported_runtime", "typed unsupported_runtime")
    blob = " ".join(str(result.get(k) or "") for k in ("reason", "repair", "message"))
    ok("runtime=codex" in blob and "do not claim_task" in blob.lower(),
       "refusal steers to start_task + codex/claude, not claim")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 tests/test_operator_launch_boot.py`

Expected: FAIL — reason/repair missing or too terse

- [ ] **Step 3: Minimal implementation**

In `connect_dispatch.py`:

```python
_UNSUPPORTED_RUNTIME_REPAIR = (
    "Use runtime=codex or runtime=claude (Connect boots the CLI). "
    "cli is not a Connect runtime. From a launcher session call start_task; "
    "do not claim_task."
)

# in enqueue_task:
    except ValueError as exc:
        err = str(exc)
        payload = {
            "dispatched": False,
            "error": err,
            "runtime": runtime,
        }
        if err == "unsupported_runtime":
            payload["reason"] = _UNSUPPORTED_RUNTIME_REPAIR
            payload["repair"] = _UNSUPPORTED_RUNTIME_REPAIR
        return payload
```

Confirm `start_task` already raises with `message=result["reason"]` on refuse; if not, pass `repair=` into `TaskExecutionError` details.

Add to `docs/MCP.md` near task-execution / session boot:

```markdown
- **Operator launch (opt-in):** call
  `prepare_agent_session(project=..., task_id=..., intent="launch")`, follow
  `first_calls`, then `start_task` (same door as the UI Start button). Default
  boot without launcher intent remains the worker claim path for direct work.
```

- [ ] **Step 4: Run full related suite**

Run:
```bash
python3 tests/test_operator_launch_boot.py
python3 tests/test_arch_ms68_session_boot.py
python3 tests/test_dispatch12_connect_cutover.py
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add src/switchboard/application/commands/connect_dispatch.py \
  src/switchboard/application/commands/task_execution.py \
  tests/test_operator_launch_boot.py docs/MCP.md
git commit -m "$(cat <<'EOF'
fix: steer unsupported_runtime toward start_task codex/claude

EOF
)"
```

---

## Spec coverage checklist

| Spec acceptance | Task |
|-----------------|------|
| Default worker boot unchanged | Task 2 (+ ARCH-MS-68 regression) |
| `intent=launch` → `start_task` in first_calls; no claim | Task 2 |
| Fan-out = N× `start_task` guidance in prompt/agreement | Tasks 2–3 |
| `runtime=cli` repair points at codex/claude + start_task | Task 4 |
| Working agreement launcher sequence | Task 3 |
| Opt-in only hard constraint | Tasks 1–2 |

## Out of scope (do not implement)

- Batch `launch_tasks` MCP tool
- Claim refusal for launcher agent ids
- Autopilot / UI Start changes
- New Connect runtime `cli`
