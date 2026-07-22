"""DOGFOOD-8: the public launch has one explicit, fail-closed packaging decision."""
from path_setup import ROOT


PLAN = (ROOT / "docs" / "OPEN-CORE-RELEASE-PLAN.md").read_text()


def require(*phrases: str) -> None:
    missing = [phrase for phrase in phrases if phrase not in PLAN]
    assert not missing, f"release plan is missing required decisions: {missing}"


require(
    "allow-list",
    "do not publish it as a history-preserving mirror",
    "Apache License 2.0",
    "Developer Certificate of Origin",
    "Trademark",
    "GitHub `6th-Element-Labs/switchboard`",
    "npm `@6th-element-labs/switchboard`",
    "Security and privacy caveats",
    "Adapter support matrix at launch",
    "External launch is **No-Go**",
    "switchboard.public-release.v1",
)

for runtime in ("Claude Code", "Codex CLI", "Cursor", "LangGraph", "Raw OpenAI agent loop"):
    assert runtime in PLAN, f"adapter matrix omits {runtime}"

for boundary in ("Public repository", "Private or hosted product"):
    assert boundary in PLAN, f"open-core boundary omits {boundary}"

assert not (ROOT / "LICENSE").exists(), (
    "A root LICENSE now exists; reassess the plan's warning that this private repository is not "
    "relicensed before keeping this regression assertion."
)

print("ok - DOGFOOD-8 open-core release plan is explicit and fail-closed")
