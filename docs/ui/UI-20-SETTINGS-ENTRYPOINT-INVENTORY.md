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
| 1 | Access tokens (UI-4) | `#apikeys-modal` + rail `#btn-project-apikeys` | `tokens` — relabel **Switchboard access tokens** (not model-provider API keys) | pending |
| 2 | Communications (UI-14) | `#comms-modal` + rail `#btn-project-comms` | `comms` | pending |
| 3 | Members & access (UI-5) | `#members-modal` + rail `#btn-project-members` | `members` | pending |
| 4 | Connect GitHub repo (UI-15) | `#github-assoc-modal` + rail `#btn-project-github` | `github` | pending |
| 5 | Account & password | `/account` + `static/account.html` | `profile` | pending |
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
Save/Test up front instead of only failing on POST."*

**3. GitHub — never probe on open.** *"Pass `?check=1` (the Verify button) to also probe repo
reachability; the panel open path omits it so it never makes a network call until the operator
asks."* Sections render on selection, so the open path must keep the `loadGithubAssoc()` /
`loadGithubAssoc(true)` split or every visit hits GitHub's API. Also `app.js` calls
`openGithubAssoc(id, {switchTo: id})` right after New Project — that flow's exit (`#ga-goto`)
must survive.

**4. Members — role change is grant-then-revoke.** *"Change role = grant the new role, then
revoke the old one (grants are per-role rows)."* Not an update.

**5. Account — the bounce has no in-tab equivalent.** `account.html` redirects to
`/login?return_to=/account` when unauthenticated. A tab cannot do that. `profile` is
`scope: null` and reachable under `PM_AUTH_MODE=dev-open` with no session at all, so the
existing `signedIn` branch in `_settingsProfileSection` is the gate. Keep
`'Password updated. Other devices have been signed out.'` — it is the only place the user
learns their other sessions were revoked (`test_auth_password_change.py` asserts the
revocation).

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
