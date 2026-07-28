"""UI-41's mobile full-width rule must key on a VISIBLE Needs queue.

Causal chain, verified live on 2026-07-28:
- #tab-needs is the default sub-tab inside the #tab-inbox-hub pane, so it
  correctly ships `active show` (Bootstrap's pre-selected-sub-tab markup)
  even though the hub itself is hidden behind the Overview default.
- #827 (UI-41) added `body:has(#tab-needs.active) ...{display:none}` inside
  `@media (max-width:640px)` to give the Needs queue the full phone width.
  A nested pane's `.active` means "selected within its group", NOT visible —
  so the rule hid the sidebar (and its hamburger) on every phone-portrait
  boot. Rotating past 640px exits the media query, which is why landscape
  appeared to fix it.

The rule must require the ancestor hub to be active too, and the default
sub-tab markup must stay exactly as Bootstrap expects it.
"""
from path_setup import ROOT

html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

# The Bootstrap default-sub-tab markup is CORRECT and must stay: pane and its
# pill both pre-active inside the (initially hidden) Inbox hub.
assert '<div class="tab-pane active show" id="tab-needs"' in html, \
    "tab-needs must stay the hub's pre-active default sub-tab"
assert 'class="nav-link active" data-bs-toggle="tab" href="#tab-needs"' in html, \
    "the Needs pill must stay the hub's pre-active default link"

# The UI-41 rules must be scoped to a VISIBLE needs queue (hub active too).
assert ('body:has(#tab-inbox-hub.active #tab-needs.active) '
        '.navbar-vertical{display:none}') in html, \
    "sidebar-hide rule must require the ancestor hub to be active"
assert ('body:has(#tab-inbox-hub.active #tab-needs.active) '
        '.page-wrapper{margin-left:0!important;width:100%!important}') in html, \
    "full-width rule must require the ancestor hub to be active"

# The unscoped selector must never come back: nested-active is not visibility.
assert "body:has(#tab-needs.active) .navbar-vertical" not in html, \
    "unscoped :has(#tab-needs.active) hides the mobile sidebar on every boot"
assert "body:has(#tab-needs.active) .page-wrapper" not in html

print("PASS test_ui41_needs_pane_not_preactive")
