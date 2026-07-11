#!/usr/bin/env python3
"""ARCH-MS-13: prove the Python 3.12 lock and exports remain reproducible."""
from __future__ import annotations

import re
import tomllib

from path_setup import ROOT

NAME_RE = re.compile(r"^([A-Za-z0-9_.-]+)(?:\[([^]]+)\])?(.*)$")
PIN_RE = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)(?:\s*;.*)?$")

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


def project_requirement(value: str) -> tuple[str, tuple[str, ...], str]:
    match = NAME_RE.fullmatch(value.strip())
    if not match:
        raise AssertionError(f"malformed project requirement: {value!r}")
    name, extras, specifier = match.groups()
    return (
        normalized_name(name),
        tuple(sorted(part.strip().lower() for part in (extras or "").split(",") if part.strip())),
        specifier.strip(),
    )


def locked_requirement(item: dict) -> tuple[str, tuple[str, ...], str]:
    return (
        normalized_name(item["name"]),
        tuple(sorted(item.get("extras") or ())),
        item.get("specifier", ""),
    )


def exported_pins(relative_path: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    malformed: list[str] = []
    for raw_line in (ROOT / relative_path).read_text(encoding="utf-8").splitlines():
        if not raw_line or raw_line.startswith("#") or raw_line[0].isspace():
            continue
        match = PIN_RE.fullmatch(raw_line)
        if not match:
            malformed.append(raw_line)
            continue
        name, version = match.groups()
        pins[normalized_name(name)] = version
    ok(not malformed, f"{relative_path} contains only exact == pins")
    return pins


pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
python_version = (ROOT / ".python-version").read_text(encoding="utf-8").strip()

project = pyproject["project"]
ok(python_version == "3.12", ".python-version selects the supported Python 3.12 line")
ok(project["requires-python"] == ">=3.12", "pyproject.toml declares Python >=3.12")
ok(lock["requires-python"] == project["requires-python"], "uv.lock carries the same Python floor")
ok(pyproject["tool"]["uv"]["package"] is False, "uv treats Switchboard as a non-package app")

packages = lock["package"]
package_names = [normalized_name(item["name"]) for item in packages]
ok(len(package_names) == len(set(package_names)), "uv.lock has one resolved version per package")
root_package = next(item for item in packages if item["name"] == project["name"])

declared_runtime = sorted(project_requirement(value) for value in project["dependencies"])
locked_runtime = sorted(locked_requirement(value) for value in root_package["metadata"]["requires-dist"])
ok(locked_runtime == declared_runtime, "uv.lock runtime metadata exactly matches pyproject.toml")

for group_name, declared in pyproject["dependency-groups"].items():
    locked = root_package["metadata"]["requires-dev"][group_name]
    ok(
        sorted(locked_requirement(value) for value in locked)
        == sorted(project_requirement(value) for value in declared),
        f"uv.lock {group_name} metadata exactly matches pyproject.toml",
    )

registry_packages = [item for item in packages if "registry" in item.get("source", {})]
missing_hashes = []
for item in registry_packages:
    artifacts = ([item["sdist"]] if item.get("sdist") else []) + item.get("wheels", [])
    if not artifacts or any(not artifact.get("hash", "").startswith("sha256:") for artifact in artifacts):
        missing_hashes.append(item["name"])
ok(not missing_hashes, "every registry artifact in uv.lock has a SHA-256 hash")

lock_versions = {normalized_name(item["name"]): item["version"] for item in registry_packages}
core_pins = exported_pins("requirements.txt")
gateway_pins = exported_pins("deploy/gateway/requirements.txt")
ok(
    all(lock_versions.get(name) == version for name, version in core_pins.items()),
    "requirements.txt pins versions from uv.lock",
)
ok(
    all(lock_versions.get(name) == version for name, version in gateway_pins.items()),
    "gateway requirements pin versions from uv.lock",
)
runtime_names = {name for name, _extras, _specifier in declared_runtime}
gateway_names = {
    project_requirement(value)[0] for value in pyproject["dependency-groups"]["gateway"]
}
ok(runtime_names <= core_pins.keys(), "core export includes every runtime dependency")
ok(gateway_names <= gateway_pins.keys(), "gateway export includes every gateway dependency")
ok("litellm" not in core_pins, "core export excludes the gateway-only LiteLLM stack")

core_header = "\n".join((ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()[:2])
gateway_header = "\n".join(
    (ROOT / "deploy/gateway/requirements.txt").read_text(encoding="utf-8").splitlines()[:2]
)
ok(
    core_header.endswith(
        "uv export --no-hashes --no-emit-project --no-default-groups -o requirements.txt"
    ),
    "core export records its canonical regeneration command",
)
ok(
    gateway_header.endswith(
        "uv export --no-hashes --no-emit-project --only-group gateway "
        "-o deploy/gateway/requirements.txt"
    ),
    "gateway export records its canonical regeneration command",
)

print(f"\nARCH-MS-13 reproducible-build proof: {passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
