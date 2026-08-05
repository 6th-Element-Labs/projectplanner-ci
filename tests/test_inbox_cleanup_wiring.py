#!/usr/bin/env python3
"""Inbox cleanup contract: the approved shell keeps every owning workflow."""
from pathlib import Path

from path_setup import ROOT

STATIC = Path(ROOT) / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
APP = (STATIC / "app.js").read_text(encoding="utf-8")
ATTENTION = (STATIC / "js" / "attention.js").read_text(encoding="utf-8")
CSS = (STATIC / "taikun-ui.css").read_text(encoding="utf-8")

passed = failed = 0


def ok(condition: bool, message: str) -> None:
    global passed, failed
    print(("  PASS  " if condition else "  FAIL  ") + message)
    passed += int(condition)
    failed += int(not condition)


inbox_start = INDEX.index('id="tab-inbox-hub"')
inbox_end = INDEX.index('id="tab-ask"')
INBOX = INDEX[inbox_start:inbox_end]

ok('class="tab-pane tk-inbox-page" id="tab-inbox-hub"' in INDEX,
   "Inbox uses the dedicated clean page surface")
ok("Human attention, inbound email, decisions, and delivery risks." in INBOX,
   "approved Inbox heading copy is present")
ok('id="inbox-view-options"' in INBOX,
   "Inbox owns its View options control")
ok(INBOX.count('class="nav-link') == 5,
   "Inbox keeps five primary views")
for href in ("#tab-needs", "#tab-inbox", "#tab-email-inbox", "#tab-decisions", "#tab-risks"):
    ok(f'href="{href}"' in INBOX, f"Inbox view remains wired: {href}")
for hook in (
    "needs-list", "needs-detail", "needs-search", "needs-source",
    "needs-state-filter", "inbox-content", "email-inbox-content",
    "decisions-table", "risks-table",
):
    ok(f'id="{hook}"' in INBOX, f"existing renderer hook is preserved: {hook}")

ok("api/attention" in ATTENTION and "PMAttention = { load, deleteAll }" in ATTENTION,
   "Needs you remains backed by the authoritative attention projection")
for endpoint in (
    "api/agent_messages/ack", "api/inbox/${it.payload.inbox_id}/confirm",
    "api/inbox/${it.payload.inbox_id}/dismiss", "fetch(`${it.decide.path}?project=",
    "api/attention/requests?project=",
):
    ok(endpoint in ATTENTION, f"attention action remains wired: {endpoint}")
ok("confirmAll(true)" in APP and "confirmAll(false)" in APP,
   "Action Queue keeps safe and explicit bulk confirmation")
ok("openQueueItem" in APP and "inbox-sim-go" in APP,
   "Action Queue detail and inbound simulation remain functional")
ok(".tk-inbox-master.show-detail" in CSS and ".tk-inbox-back" in CSS,
   "mobile Needs you uses list-to-detail navigation")
ok(".tk-inbox-queue-table thead{ display:none" in CSS,
   "mobile Action Queue reflows instead of overflowing")

print(f"\n{passed} passed, {failed} failed")
raise SystemExit(1 if failed else 0)
