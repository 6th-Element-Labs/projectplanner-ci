#!/usr/bin/env python3
"""ENFORCE-11: the OpenAI provider key remains gateway-only."""
from __future__ import annotations

import json
import os
from pathlib import Path

from path_setup import ROOT  # noqa: F401

from fastapi import FastAPI
from fastapi.testclient import TestClient

from switchboard.application.queries.audit_export import _audit_redact
from switchboard.api.middleware import register_auth_gate
from switchboard.mcp.deps import dumps
from switchboard.security import redact_provider_secrets, redact_provider_secrets_bytes


passed = failed = 0


def ok(condition, message):
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += 1 if condition else 0
    failed += 0 if condition else 1


SECRET = "sk-enforce11-provider-key-must-never-escape"
os.environ["OPENAI_API_KEY"] = SECRET

# Model every outward/persisted evidence family with the key deliberately hidden
# under non-secret field names. This proves value redaction, not merely key-name filtering.
payload = {
    "browser_response": {"detail": f"failure: {SECRET}"},
    "mcp_response": {"result": SECRET},
    "queued_job": {"params": {"prompt": SECRET}, "manifest": {"error": SECRET}},
    "runner_logs": [{"text": f"stderr={SECRET}"}],
    "tally_receipt": {"metadata": {"debug": SECRET}, "verification": SECRET},
    "audit_export": {"payload": {"innocent_name": SECRET}},
    "generated_proof": {"evidence": [SECRET]},
}
redacted = redact_provider_secrets(payload)
ok(SECRET not in json.dumps(redacted, sort_keys=True),
   "recursive boundary removes the provider key from every residue family")
ok(payload["mcp_response"]["result"] == SECRET,
   "redaction does not mutate the caller's evidence object")
ok(SECRET.encode() not in redact_provider_secrets_bytes(
       f'{{"result":"{SECRET}"}}'.encode()),
   "HTTP byte boundary removes the provider key from browser/API responses")
ok(SECRET not in dumps(payload),
   "MCP deterministic serializer removes the provider key from every tool response")
ok(SECRET not in json.dumps(_audit_redact(payload), sort_keys=True),
   "audit/ops export removes provider-key values even under benign field names")

os.environ["PM_AUTH_MODE"] = "dev-open"
app = FastAPI()


@app.get("/api/enforce11-leak-probe")
def _leak_probe():
    return {"innocent_name": SECRET}


register_auth_gate(
    app,
    global_user_scopes=lambda _user, _project: [],
    global_principal=lambda _user, _scopes: {},
    admin_scopes=[],
)
http_response = TestClient(app).get(
    "/api/enforce11-leak-probe?project=switchboard")
ok(http_response.status_code == 200 and SECRET not in http_response.text
   and "[REDACTED]" in http_response.text,
   "live FastAPI response middleware redacts the key before browser delivery")

root = Path(ROOT)
jobs_source = (root / "jobs_store.py").read_text(encoding="utf-8")
jobs_engine_source = (root / "background_jobs.py").read_text(encoding="utf-8")
tally_source = (root / "src/switchboard/storage/repositories/kpis_economics.py").read_text(
    encoding="utf-8")
transcript_source = (root / "src/switchboard/application/commands/task_execution.py").read_text(
    encoding="utf-8")
middleware_source = (root / "src/switchboard/api/middleware.py").read_text(encoding="utf-8")
ok("redact_provider_secrets(params" in jobs_source
   and "redact_provider_secrets(dict(params" in jobs_engine_source,
   "queued background-job params are scrubbed before manifest persistence")
ok("redact_provider_secrets(metadata" in tally_source
   and "redact_provider_secrets(evidence" in tally_source,
   "Tally metadata and outcome evidence are scrubbed before receipt persistence")
ok("return redact_provider_secrets({" in transcript_source,
   "runner log transcript generation has a final provider-key boundary")
ok("_provider_secret_response_boundary" in middleware_source
   and "redact_provider_secrets_bytes" in middleware_source,
   "browser, REST, and protocol JSON responses share the HTTP containment boundary")

gateway_unit = (root / "deploy/projectplanner-gateway.service").read_text(encoding="utf-8")
least_privilege = (root / "deploy/apply-least-privilege.sh").read_text(encoding="utf-8")
ok("--host 127.0.0.1 --port 8095" in gateway_unit,
   "provider gateway remains bound to localhost")
ok('chown "root:$SERVICE_GROUP" "$CODE_ROOT/.env"' in least_privilege
   and 'chmod 640 "$CODE_ROOT/.env"' in least_privilege,
   "deployment keeps the secret file root-owned and inaccessible to other users")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
