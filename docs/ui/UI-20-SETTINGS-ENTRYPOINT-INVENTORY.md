# UI-20 — legacy settings entry-point inventory

UI-20 exit criterion 1: *"A checked inventory maps every legacy entrypoint to one canonical
destination and proves no orphan setting remains."* This is that map. It is the plan of record
for the migration; each row lands in its own PR so a single surface can be reviewed and
reverted independently.

Canonical destination is always a section of the UI-18 shell (`static/js/settings.js`),
addressed as `#tab-settings/<section>`.

## The map

| # | Legacy entry point | Where it lives now | Canonical destination | Status |
|---|---|---|---|---|
| 1 | Access tokens (UI-4) | `#apikeys-modal` + rail `#btn-project-apikeys` | `tokens` — relabel **Switchboard access tokens** (not model-provider API keys) | **done (2/6)** — inlined into `_settingsTokensSection`; modal + rail button retired; shown-once wipe re-anchored onto the panel swap |
| 2 | Communications (UI-14) | `#comms-modal` + rail `#btn-project-comms` | `comms` | **done (3/6)** — inlined into `_settingsCommsSection` (inbound domains + outbound recipients/cadence, chip add/remove, save, send-test); modal + rail button retired; admin gate re-scoped from the `#comms-modal .comms-editable` selector to inline `disabled` driven by the server `can_edit` probe |
| 3 | Members & access (UI-5) | `#members-modal` + rail `#btn-project-members` | `members` | **done (4/6)** — inlined into `_settingsMembersSection` (member table with per-row role select + revoke, add-a-member form, visibility explainer); modal + rail button retired; role change kept as grant-then-revoke; `btn-project-members` command-palette entry repointed to the `#tab-settings/members` deep link |
| 4 | Connect GitHub repo (UI-15) | `#github-assoc-modal` + rail `#btn-project-github` | `github` | **done (5/6)** — inlined into `_settingsGithubSection` below the repo-topology card (repo save + guided webhook wiring: payload URL, secret, `gh` one-liner, Verify); modal + rail button retired; open path never probes (only Verify passes `?check=1`); New Project handoff + palette entry repointed to the `#tab-settings/github` deep link |
| 5 | Account & password | `/account` + `static/account.html` | `profile` | **done (6/6)** — change-password form inlined into `_settingsProfileSection` (guarded by the `signedIn` branch; posts to `/api/auth/change-password` as a raw fetch so the 403/422 detail surfaces and the revocation line stays verbatim); `account.html` reduced to a compatibility redirect into `#tab-settings/profile`; user-menu "Account & password" item repointed to the same deep link |
| 6 | Project/provenance admin cards | Settings tab (UI-9) | `provenance` / `advanced` | **done (UI-18)** |
| 7 | Appearance | legacy theme cog (retired) | `appearance` | **done (UI-18)** — read-only; the app is deliberately single-theme (`static/taikun-ui.js`) |
| 8 | Status docks | `#fleet-dock`, `#saturation-dock`, `#narration-ops-dock` | `fleet` / `capacity` / `narration`, docks exception-only + deep-link | **carved out → UI-22** |
| — | Settings entry itself | left rail tab | top-right user dropdown | **done (UI-23)** |

`#btn-new-project` stays in the rail: creating a project is an action, not a setting.

## Rules each move must preserve

Load-bearing behaviour that is easy to drop silently. Each is quoted from the code that states it.

**1. Access tokens — the show-once secret.** `app.js`: *"mint with chosen scopes, show the raw
key ONCE … Storage is hash-only, so the raw token is never re-displayed after creation"*, and
the wipe is *"called when the banner is superseded, the modal closes, or it re-opens"*. The
wipe is currently anchored to `hidden.bs.modal` on `#apikeys-modal`. **Deleting the modal
deletes the trigger.** In the shell the equivalent moment is navigating away from the section
(`_settingsSelect` swapping `#settings-panel`), so `_clearApiKeySecret()` must be re-anchored
there. `test_api_keys_settings.py` asserts `"hidden.bs.modal" in app_js` — that needle must be
rewritten to name the new mechanism, not deleted.

**2. Communications — the admin gate is a selector.** `_renderCommsChips()` disables controls
via `#comms-modal .comms-editable`. Re-hosting the markup without rewriting that prefix leaves
every control enabled for non-admins (the server still refuses, but the UI lies). The gate
itself comes from the server: *"Reflect whether THIS caller may edit, so the UI can disable
Save/Test up front instead of only failing on POST."* — **resolved (3/6):** the modal-scoped
`querySelectorAll` is gone; `_settingsCommsSection` stamps `disabled` inline at render from the
`can_edit` probe (and shows the `write:system` warning), so there is no selector prefix left to
drift. `test_ui14_comms_settings.py` asserts the modal-scoped selector is absent.

**3. GitHub — never probe on open.** *"Pass `?check=1` (the Verify button) to also probe repo
reachability; the panel open path omits it so it never makes a network call until the operator
asks."* Sections render on selection, so the open path must keep the `loadGithubAssoc()` /
`loadGithubAssoc(true)` split or every visit hits GitHub's API. Also `app.js` calls
`openGithubAssoc(id, {switchTo: id})` right after New Project — that flow's exit (`#ga-goto`)
must survive. — **resolved (5/6):** `_settingsGithubSection` fetches `github_association`
without `?check=1`, so selecting the section never probes; `_settingsGithubLoad(true)` (the
Verify button) is the only path that passes `?check=1`. The New Project handoff no longer opens
a modal — when a repo was named it reloads into `?project=<id>#tab-settings/github`, which both
switches boards and opens the inline wiring panel (so the `#ga-goto` "Go to project" button is
subsumed and removed). `test_ui18_settings_shell.py` asserts the no-probe-on-open split and the
deep-link handoff.

**4. Members — role change is grant-then-revoke.** *"Change role = grant the new role, then
revoke the old one (grants are per-role rows)."* Not an update. — **resolved (4/6):**
`_settingsMembersChangeRole` grants the new role, then revokes the old (`project_role` then
`project_role/revoke`); the per-row `<select>` routes through the Settings `change`
delegation. The section nav stays `write:system`, so anyone who can open it can edit and the
server still enforces per action.

**5. Account — the bounce has no in-tab equivalent.** `account.html` redirects to
`/login?return_to=/account` when unauthenticated. A tab cannot do that. `profile` is
`scope: null` and reachable under `PM_AUTH_MODE=dev-open` with no session at all, so the
existing `signedIn` branch in `_settingsProfileSection` is the gate. Keep
`'Password updated. Other devices have been signed out.'` — it is the only place the user
learns their other sessions were revoked (`test_auth_password_change.py` asserts the
revocation). — **resolved (6/6):** the change-password form is folded into the `signedIn`
branch of `_settingsProfileSection`; with no session the section renders read-only (no
in-tab bounce, none needed). `_settingsChangePassword` keeps the client-side guards and the
exact revocation line, and uses a raw `fetch` (not `_sSend`) so the wrong-current 403 and
too-short/unchanged 422 detail surface verbatim rather than as a generic write error.
`account.html` is now a redirect stub (still `GET /account` → 200 for old links, but no
password form), and the user-menu item deep-links to `#tab-settings/profile`.
`test_ui18_settings_shell.py` asserts the inline form, the retained message, the redirect
stub, and the repointed menu item.

## Scope authority (resolved)

The nav's coarse gate must not promise access the routes refuse.

| Section | Nav scope | Backing routes | Resolution |
|---|---|---|---|
| `members` | was `write:projects` | `write:system` (`access.py` members / project_role / revoke / invite) | **fixed → `write:system`**. A contributor previously saw an unlocked section and a 403 on every action. |
| `comms` | `write:projects` | read: anyone who can read the project; write: `write:system` | **kept**. The section already disables its edit path from the server's `can_edit` probe, so a project editor legitimately sees it read-only. |
| `tokens`, `github` | `write:system` | `write:system` | agrees |

## Known traps in the existing tests

These will break on the move and must be re-pointed, not deleted:

- `test_api_keys_settings.py` — asserts `id="apikeys-modal"`, `id="btn-project-apikeys"`,
  `id="apikeys-scopes"`, `id="apikeys-new-banner"` in the served HTML, plus the
  `hidden.bs.modal` needle above.
- `test_ui14_comms_settings.py` — asserts `id="comms-modal"`, `id="btn-project-comms"`,
  `id="comms-plus"`.
- `test_ui18_settings_shell.py` — its `[10] Launchers` block asserts the `open-*` dispatch
  cases exist. That block *is* UI-20's own spec ("the legacy surfaces still work; UI-20 retires
  their entry points"); it inverts as each surface lands.
- `test_ui23_topright_user_menu.py` and `test_auth_password_change.py` both assert
  `GET /account` → 200. Removing the route breaks them.
- Members (`mm-*`, `#members-modal`) and GitHub (`ga-*`, `#github-assoc-modal`) have **no DOM
  test coverage at all** — nothing will catch a dropped id, so those two moves need the most
  browser verification.

Moving JS between `static/app.js` and `static/js/settings.js` is free: `read_frontend_source()`
concatenates both, so needles phrased "app.js defines `openComms`" are file-agnostic. Only
needles read from `index.html`, or from `settings.js` by path, actually break.

## Command palette

`index.html` `commands()` has entries for only two of the five surfaces, both via
`clickId('btn-project-github')` / `clickId('btn-project-members')`. `clickId` is
`if (b) b.click()` — **deleting the buttons leaves both entries rendering and doing nothing,
with no throw and no test**. Repoint them to `jump('#tab-settings/github')` /
`jump('#tab-settings/members')` in the same commit that removes each button, and add the three
missing surfaces while there. `#toptab-settings` is already a "Jump to" entry because the
(hidden) controller still lives in `#main-nav`.
