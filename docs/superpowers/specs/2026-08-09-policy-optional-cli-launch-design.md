# Policy-Optional MCP CLI Launch Design

## Outcome

Every project on the board can launch a registered provider CLI through the canonical MCP `start_task` path without first configuring project execution policy, provider selectors, or SCM selectors. Projects that explicitly activate a complete execution policy retain the stricter immutable Execution Context path.

This restores the compatibility contract that existed before HARDEN-78 while keeping the newer policy system available as an opt-in capability.

## Problem

HARDEN-78 changed Connect dispatch from:

1. resolve an immutable Execution Context when the project has opted into execution policy; otherwise
2. create the established provider-neutral Connect wake for a registered Agent Host

to an unconditional Execution Context resolution. An unconfigured project therefore reaches `start_task`, but Connect refuses it with `project_execution_policy_missing` before requesting a wake. The refusal affects Maxwell and any current or future board project that has not configured the newer policy stack.

The normal execution model remains MCP coordination launching a real CLI on a registered Agent Host. Policy configuration must not be a prerequisite for that baseline capability.

## Design

### Global admission rule

`start_task` remains the sole launch admission door for every project.

- If a project has an activated execution policy, Connect resolves and embeds the immutable Execution Context. An active but incomplete or invalid policy fails closed.
- If a project has no configured execution policy, Connect creates the legacy-compatible provider-neutral assignment and wake. It does not consult provider or SCM selectors.
- A draft, absent, or never-configured policy is treated as unconfigured. A project cannot accidentally opt in merely because policy tables or read APIs exist.
- The rule is project-independent. No project-name allowlist, Maxwell exception, environment switch, or repository-specific branch is permitted.

### Connect assignment

For an unconfigured project, Connect uses the existing compatibility assignment:

- runtime and provider derived from the requested CLI runtime;
- task-scoped worker principal minted by the server;
- `repo:canonical` workspace reference;
- existing task lifecycle, generation, idempotency, ownership, capacity, and runner-lease controls;
- no `execution_context` or hybrid placement envelope.

For a configured project, the current exact repository, base SHA, provider, SCM, placement, and workspace fields remain unchanged.

### Agent Host compatibility

The registered Agent Host must accept both supported wake forms:

- configured: immutable Execution Context plus isolated materialized workspace;
- unconfigured: compatibility wake using the established host-local/project contract path.

Compatibility must never mean executing in the Switchboard application checkout by accident. The host must derive the task repository/workspace from its established project/repository attachment behavior and continue to use task isolation. If no safe repository can be selected, launch fails with a repository/workspace error rather than a policy error.

Every compatibility wake therefore carries the server-owned canonical repository binding from project topology. A multi-project host selects an explicitly configured project-to-source-checkout binding (with a matching-origin fallback only when the default checkout is the same canonical repository). Repository identity includes the Git host plus owner/repository, so a same-named GitLab or local checkout cannot impersonate the canonical GitHub source. A missing or mismatched binding refuses before materialization; one project's wake can never borrow another project's checkout.

Enrollment and signed update accept repeatable `--project-source PROJECT=/absolute/git/checkout` arguments. Each checkout is validated as an absolute, non-symlink Git root with an origin and is added to the service's allowed filesystem roots. The durable config survives restart and update without hand-editing.

### Scope of policy removal

Execution policy is removed as a prerequisite for baseline task launch across all projects. The policy subsystem itself is not deleted because configured projects still use it for stricter repository, provider, SCM, placement, and immutable-context authority.

The following must not gate an unconfigured project's normal MCP launch:

- project execution readiness;
- provider connection selection;
- SCM connection selection;
- hybrid placement configuration;
- Autopilot execution-policy configuration.

Task dependency gates, claim ownership, runtime availability, host capacity, repository safety, runner fencing, and completion provenance remain enforced.

## Error behavior

- Configured but invalid project: return the existing typed policy/readiness refusal.
- Unconfigured project with no compatible online host: return the existing capacity refusal.
- Unconfigured project whose host cannot resolve a safe repository/workspace: return a typed repository/workspace refusal.
- Unsupported runtime, dependency failure, claim conflict, or duplicate execution: preserve current typed refusals.
- Never suggest provider-policy setup as the repair for an unconfigured project's baseline launch.

## Tests

Regression coverage must prove:

1. An unconfigured synthetic project reaches Connect and requests exactly one CLI wake.
2. Maxwell follows the same generic path without project-specific code.
3. A second arbitrary future project also launches without policy, proving the behavior is global.
4. Activated projects still resolve immutable Execution Context and fail closed when active authority is invalid; persisted drafts remain compatibility mode.
5. Unconfigured wakes retain task ownership, generation idempotency, runner fencing, and repository/workspace safety.
6. Agent Host accepts the compatibility wake and launches the requested CLI in a task-isolated workspace.
7. A host polling multiple projects selects the source checkout bound to the wake project and refuses a missing or wrong repository binding.
8. Existing configured-project, runner-control, completion, and merge-provenance suites remain green.

## Rollout

Deploy the control-plane compatibility change and matching Agent Host support together. Verify one unconfigured non-Switchboard project end to end with `start_task`, a live runner session, CLI transcript, task-isolated workspace, and truthful terminal state. Then launch the three Total work items through MCP.

No project-by-project policy migration is required.
