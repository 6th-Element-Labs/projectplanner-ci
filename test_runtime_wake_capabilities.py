#!/usr/bin/env python3
"""ADAPTER-12: executable runtime wake/resume matrix and fail-closed fixtures."""

from adapters.wake_capabilities import (
    CAPABILITIES,
    evaluate_capability,
    load_matrix,
    validate_matrix,
)


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


matrix = load_matrix()
ok(not validate_matrix(matrix), "checked-in matrix satisfies the executable schema")

runtime_ids = {runtime["id"] for runtime in matrix["runtimes"]}
required_runtimes = {
    "claude-code",
    "codex-cli",
    "codex-app",
    "cursor-agent",
    "langgraph-worker",
    "openai-responses-loop",
    "anthropic-messages-loop",
    "shell-ci-runner",
}
ok(required_runtimes <= runtime_ids, "matrix covers every ADAPTER-12 runtime class")

for runtime in matrix["runtimes"]:
    runtime_id = runtime["id"]
    for capability_name in CAPABILITIES:
        capability = runtime["capabilities"][capability_name]
        denied = evaluate_capability(
            runtime_id, capability_name, (), matrix=matrix
        )
        ok(
            denied["allowed"] is False and denied.get("reason"),
            f"{runtime_id}.{capability_name} fails closed with no setup",
        )
        if capability["support"] == "conditional":
            requirements = capability["requires"]
            allowed = evaluate_capability(
                runtime_id, capability_name, requirements, matrix=matrix
            )
            ok(
                allowed["allowed"] is True and allowed["missing"] == [],
                f"{runtime_id}.{capability_name} allows only its complete setup",
            )
            partial = evaluate_capability(
                runtime_id, capability_name, requirements[:-1], matrix=matrix
            )
            ok(
                partial["allowed"] is False
                and requirements[-1] in partial["missing"],
                f"{runtime_id}.{capability_name} denies one missing requirement",
            )

resume_classes = {
    runtime["id"]: runtime["capabilities"]["same_session_resume"]
    .get("when_ready", {})
    .get("continuity_mode")
    for runtime in matrix["runtimes"]
}
ok(
    resume_classes["claude-code"] == "exact_vendor_session"
    and resume_classes["codex-cli"] == "exact_vendor_session"
    and resume_classes["codex-app"] == "exact_vendor_session"
    and resume_classes["cursor-agent"] == "exact_vendor_session",
    "vendor coding runtimes distinguish exact conversation resume",
)
ok(
    resume_classes["langgraph-worker"] == "checkpoint_resume"
    and resume_classes["anthropic-messages-loop"] == "reconstructed_history",
    "checkpoint and reconstructed-history continuity are not mislabeled exact",
)
ok(
    resume_classes["shell-ci-runner"] is None,
    "generic shell/CI does not claim same-process session resume",
)

unknown_runtime = evaluate_capability("missing-runtime", CAPABILITIES[0], ())
unknown_capability = evaluate_capability("codex-cli", "telepathy", ())
ok(
    unknown_runtime == {
        "allowed": False,
        "reason": "unknown_runtime",
        "failure_class": "invalid_input",
    },
    "unknown runtime fails closed",
)
ok(
    unknown_capability == {
        "allowed": False,
        "reason": "unknown_capability",
        "failure_class": "invalid_input",
    },
    "unknown capability fails closed",
)

doc = open("docs/RUNTIME-WAKE-CAPABILITY-MATRIX.md", encoding="utf-8").read()
ok(all(runtime_id in doc for runtime_id in required_runtimes),
   "published matrix names every required runtime id")
ok("resume_required" in doc and "resume_preferred" in doc and "fresh_only" in doc,
   "published matrix defines all continuity policies")
ok("delivery_status" in doc and "wake_status" in doc,
   "published matrix documents both status vocabularies")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
