#!/usr/bin/env python3
"""UI-90 — visible email history and explicit fail-closed quarantine recovery."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TMP = tempfile.mkdtemp(prefix="ui90-email-inbox-")
os.environ["PM_DB_PATH"] = str(Path(TMP) / "maxwell.db")
os.environ["PM_HELM_DB_PATH"] = str(Path(TMP) / "helm.db")
os.environ["PM_SWITCHBOARD_DB_PATH"] = str(Path(TMP) / "switchboard.db")
os.environ["PM_PROJECT_REGISTRY_DB_PATH"] = str(Path(TMP) / "registry.db")
os.environ["PM_DYNAMIC_PROJECTS_DIR"] = TMP
os.environ["PM_IMAP_USER"] = "plan@taikunai.com"
os.environ["PM_IMAP_PASSWORD"] = "test-only"
os.environ["PM_INBOX_ROUTES"] = ""
os.environ["PM_AUTH_MODE"] = "dev-open"

import store  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from switchboard.api.routers.intake_inbox import create_router  # noqa: E402
from switchboard.domain.projects.context import ProjectContext  # noqa: E402
from switchboard.integrations import imap_quarantine, inbox_routing  # noqa: E402


for project in ("maxwell", "helm", "switchboard"):
    store.init_db(project)
inbox_routing.invalidate_routes()

NORMAL = (
    b"From: Steve Ridder <steve@taikunai.com>\r\n"
    b"To: plan@taikunai.com\r\n"
    b"Subject: Fwd: FMP API v1\r\n"
    b"Date: Wed, 5 Aug 2026 13:59:16 +1200\r\n"
    b"Message-ID: <normal-ui90@test>\r\n\r\n"
    b"Please ingest the API handoff."
)
SENSITIVE = (
    b"From: Steve Ridder <steve@taikunai.com>\r\n"
    b"To: plan@taikunai.com\r\n"
    b"Subject: Fwd: FMP API Client Secret secure links\r\n"
    b"Date: Wed, 5 Aug 2026 13:59:21 +1200\r\n"
    b"Message-ID: <secret-ui90@test>\r\n\r\n"
    b"A credential must never enter the plan corpus."
)


class FakeIMAP:
    instances = []

    def __init__(self, _host):
        self.calls = []
        self.messages = {b"1": NORMAL, b"2": SENSITIVE}
        FakeIMAP.instances.append(self)

    def login(self, *_args):
        self.calls.append(("login",))

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [str(len(self.messages)).encode()]

    def search(self, *_args):
        return "OK", [b" ".join(self.messages)]

    def fetch(self, message_id, _query):
        return "OK", [(b"1 (RFC822 {1})", self.messages[message_id])]

    def create(self, folder):
        self.calls.append(("create", folder))
        return "OK", []

    def copy(self, message_id, folder):
        self.calls.append(("copy", message_id, folder))
        return "OK", []

    def store(self, message_id, *_args):
        self.calls.append(("store", message_id))
        return "OK", []

    def expunge(self):
        self.calls.append(("expunge",))
        return "OK", []

    def logout(self):
        self.calls.append(("logout",))


def check(condition, message):
    print(("  ok   " if condition else "  FAIL ") + message)
    if not condition:
        raise AssertionError(message)


try:
    imap_quarantine.imaplib.IMAP4_SSL = FakeIMAP

    listed = imap_quarantine.list_quarantined()
    check(listed["configured"] and listed["count"] == 2, "quarantine headers are listed")
    by_subject = {item["subject"]: item for item in listed["items"]}
    normal = by_subject["Fwd: FMP API v1"]
    secret = by_subject["Fwd: FMP API Client Secret secure links"]
    check(normal["reason"] == "unmapped_sender", "unmapped reason remains visible")
    check(not normal["sensitive"] and secret["sensitive"], "credential-looking subject is held")
    check("body" not in normal and "text" not in normal, "listing never exposes message bodies")

    calls = []
    imap_quarantine.inbox.process = lambda *args, **kwargs: (
        calls.append((args, kwargs)) or {"id": 90, "status": "applied"}
    )
    recovered = imap_quarantine.process_quarantined(
        normal["token"], ProjectContext(project_id="maxwell", source="test")
    )
    check(recovered["processed"] and not recovered["deduped"], "selected mail is processed")
    check(calls[0][1]["project_context"].project_id == "maxwell",
          "operator-selected project is the immutable intake scope")
    check(any(call[0] == "copy" and call[2] == "Switchboard-Processed"
              for call in FakeIMAP.instances[-1].calls),
          "processed mail is preserved in the processed folder")

    try:
        imap_quarantine.process_quarantined(
            secret["token"], ProjectContext(project_id="maxwell", source="test")
        )
        raise AssertionError("sensitive mail unexpectedly reached intake")
    except imap_quarantine.SensitiveMessageError:
        pass
    check(len(calls) == 1, "credential-looking mail never reaches the plan agent")

    auth_calls = []

    def authorize(_request, project, scopes, dev_actor=""):
        auth_calls.append((project, scopes, dev_actor))
        return {"id": "operator"}

    original_list = imap_quarantine.list_quarantined
    imap_quarantine.list_quarantined = lambda: {
        "configured": True, "folder": "Switchboard-Quarantine", "count": 1,
        "items": [normal],
    }
    app = FastAPI()
    app.include_router(create_router(resolve_project=lambda value: value,
                                     resolve_principal=authorize))
    response = TestClient(app).get("/api/inbox/quarantine", params={"project": "maxwell"})
    imap_quarantine.list_quarantined = original_list
    check(response.status_code == 200 and response.json()["items"][0]["token"] == normal["token"],
          "header-only quarantine endpoint returns recoverable items")
    check(auth_calls == [("maxwell", ("write:system",), "inbox-quarantine")],
          "shared quarantine is system-admin gated")

    index_html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    app_js = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    check('href="#tab-email-inbox"' in index_html and "Unrouted mail" in app_js,
          "new Inbox exposes an Email view and unrouted-mail section")
    check("Process in ${this.esc(projectName)}" in app_js and "window.confirm" in app_js,
          "operator must explicitly bind and confirm before the old email agent runs")
    check("Credential-looking subject" in app_js and "Keep quarantined" in app_js,
          "UI explains why credential mail cannot be ingested")
finally:
    shutil.rmtree(TMP, ignore_errors=True)

print("PASS: UI-90 email inbox history + quarantine recovery")
