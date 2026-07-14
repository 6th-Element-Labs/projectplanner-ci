#!/usr/bin/env python3
"""ARCH-MS-54: external side-effect ledger under storage/repositories."""
from __future__ import annotations

import importlib
import os
import shutil
import tempfile
from pathlib import Path

from path_setup import ROOT

import scripts.switchboard_path  # noqa: F401

TMP = tempfile.mkdtemp(prefix="arch-ms54-external-effects-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "project_registry.db")
os.environ["PM_AUTH_MODE"] = "dev-open"

passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


try:
    for name in (
        "switchboard.storage.repositories.external_effects",
        "external_effects_store",
    ):
        try:
            importlib.import_module(name)
            ok(True, f"{name} imports cleanly")
        except Exception as exc:  # noqa: BLE001
            ok(False, f"{name} import failed: {exc!r}")

    ok((ROOT / "src/switchboard/storage/repositories/external_effects.py").is_file(),
       "external_effects.py exists under storage/repositories")
    ok((ROOT / "external_effects_store.py").is_file(),
       "external_effects_store.py shim exists at repo root")

    from switchboard.storage.repositories import external_effects as fx_repo  # noqa: E402
    from switchboard.storage.repositories import external_ci as eci_repo  # noqa: E402
    import external_effects_store  # noqa: E402
    import store  # noqa: E402

    ok(external_effects_store.claim_external_effect is fx_repo.claim_external_effect,
       "external_effects_store shim re-exports claim_external_effect")
    ok(store.claim_external_effect is fx_repo.claim_external_effect,
       "store facade delegates claim_external_effect to package module")
    ok(store.make_external_effect_key is fx_repo.make_external_effect_key,
       "store facade delegates make_external_effect_key to package module")
    ok(store.mark_external_effect_issued is fx_repo.mark_external_effect_issued,
       "store facade delegates mark_external_effect_issued")
    ok(store.verify_external_effect is fx_repo.verify_external_effect,
       "store facade delegates verify_external_effect")
    ok(store.fail_external_effect is fx_repo.fail_external_effect,
       "store facade delegates fail_external_effect")
    ok(store.list_external_effects is fx_repo.list_external_effects,
       "store facade delegates list_external_effects")
    ok(store.claim_external_effect.__module__
       == "switchboard.storage.repositories.external_effects",
       "claim_external_effect lives under switchboard.storage.repositories.external_effects")
    ok(isinstance(store.external_effects_repository,
                  fx_repo.StoreExternalEffectsRepository),
       "store.external_effects_repository is StoreExternalEffectsRepository")

    shell_src = (ROOT / "src/switchboard/storage/repositories/shell.py").read_text()
    fx_src = (ROOT / "src/switchboard/storage/repositories/external_effects.py").read_text()
    eci_src = (ROOT / "src/switchboard/storage/repositories/external_ci.py").read_text()
    ok("def make_external_effect_key(" not in shell_src,
       "shell residual no longer defines make_external_effect_key")
    ok("def claim_external_effect(" not in shell_src,
       "shell residual no longer defines claim_external_effect")
    ok("def _claim_external_effect_in(" not in shell_src,
       "shell residual no longer defines _claim_external_effect_in")
    ok("def list_external_effects(" not in shell_src,
       "shell residual no longer defines list_external_effects")
    ok("EXTERNAL_EFFECT_TERMINAL_STATUSES" not in shell_src
       or "EXTERNAL_EFFECT_TERMINAL_STATUSES =" not in shell_src,
       "shell residual no longer owns EXTERNAL_EFFECT_TERMINAL_STATUSES assignment")
    ok("def make_external_effect_key(" in fx_src
       and "def claim_external_effect(" in fx_src
       and "INSERT INTO external_side_effects" in fx_src,
       "ledger SQL and public helpers live in external_effects.py")
    ok("from switchboard.storage.repositories.external_effects import" in eci_src
       and "_store_facade()._claim_external_effect_in" not in eci_src,
       "external_ci imports claim helpers directly from external_effects")

    shell_lines = shell_src.count("\n") + 1
    ok(shell_lines <= 3116 - 150,
       f"shell residual shrank by >=150 lines ({shell_lines} <= {3116 - 150})")

    # Functional smoke through the store façade
    store.init_db("switchboard")
    claim = store.claim_external_effect(
        "github_write", "github", "repos/org/repo/statuses/abc",
        {"state": "pending", "context": "switchboard/vm-gate"},
        task_id="ARCH-MS-54", agent_id="cursor/test", actor="cursor/test",
        project="switchboard")
    ok(claim.get("claimed") is True and claim["effect"]["status"] == "claimed",
       "extracted claim_external_effect persists a claimed row")
    issued = store.mark_external_effect_issued(
        claim["effect_key"], {"provider_status": 201}, actor="cursor/test",
        project="switchboard")
    ok(issued["effect"]["status"] == "issued",
       "extracted mark_external_effect_issued updates status")
    verified = store.verify_external_effect(
        claim["effect_key"], {"provider_status": 201, "sha": "abc"},
        actor="cursor/test", project="switchboard")
    ok(verified["effect"]["status"] == "verified",
       "extracted verify_external_effect confirms the ledger row")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
