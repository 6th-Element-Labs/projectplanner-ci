# Personal Agent Host enrollment

ADAPTER-18 turns the existing Agent Host daemon into a user-installable, host-owned
Codex execution path for macOS and Linux. Switchboard wakes the registered host and
native Codex CLI; it does not wake or remote-drive the Codex desktop application.

## Trust model

- Release bundles are versioned and signed with Ed25519. Install and update refuse an
  invalid signature, undeclared file, unsafe path/mode, or hash mismatch before changing
  local state or consuming a bootstrap code.
- An operator creates a short-lived bootstrap code (60–900 seconds). The code is
  single-use and only its hash is stored.
- Bootstrap completion creates a stable `host/...` identity, an Ed25519 device key, and
  a narrow project bearer with `read` + `write:ixp`. Raw bearers are returned once and
  stored only in the user's `0600` identity file.
- Before consuming the bootstrap, the installer also persists a one-install recovery
  secret in the pending `0600` identity. For ten minutes, the same bootstrap plus that
  secret can recover an ambiguous completion response by issuing a replacement bearer;
  the first possibly-lost bearer is invalidated. The secret is removed after completion.
- The Codex personal login and all provider credentials remain on the user-owned host.
  Registration and heartbeat expose only redacted readiness/account fingerprints.
- Rotating the host identity invalidates the old bearer immediately. Revocation fences
  both REST authentication and reuse of the enrolled `host_id`.
- If Switchboard is offline during revoke, the service is stopped and local state becomes
  `revocation_pending`; the only bearer is retained for an explicit retry. No cloud/API
  lane is selected as a silent fallback.

## Build and sign a release

Keep the Ed25519 private signing key outside the repository. Publish its public key with
the release channel.

```bash
python adapters/agent_host_enrollment.py build-bundle \
  --source-root . \
  --output dist/switchboard-agent-host-0.2.0 \
  --version 0.2.0 \
  --signing-key /secure/release/agent-host-ed25519-private.pem

python adapters/agent_host_enrollment.py verify-bundle \
  --bundle dist/switchboard-agent-host-0.2.0 \
  --public-key /secure/release/agent-host-ed25519-public.pem
```

The signed manifest covers the Agent Host adapters, the Switchboard runtime modules they
import, and both service templates. Update accepts only a strictly newer semantic version
and swaps the `current` release symlink atomically. A service restart failure rolls the
symlink back to the prior release.

## Create the device bootstrap

From an operator session, call `begin_agent_host_enrollment` through MCP or
`POST /ixp/v1/agent-host-enrollments` through REST:

```json
{
  "schema": "switchboard.agent.begin_host_enrollment_command.v1",
  "project": "switchboard",
  "owner_user_id": "user-123",
  "requested_host_id": "host/steve-mbp",
  "tenant_allowlist": ["tenant-123"],
  "project_allowlist": ["switchboard"],
  "provider_allowlist": ["openai-codex"],
  "package_version": "0.2.0",
  "ttl_seconds": 600
}
```

Capture `bootstrap_code` immediately. It is returned once. Put it in a `0600` file so it
does not enter shell history.

## Install

The same command works on macOS and Linux; platform detection chooses a per-user LaunchAgent
or systemd user service.

```bash
python adapters/agent_host_enrollment.py install \
  --bundle /path/to/switchboard-agent-host-0.2.0 \
  --public-key /path/to/agent-host-ed25519-public.pem \
  --bootstrap-code-file /secure/tmp/switchboard-bootstrap-code \
  --base-url https://plan.taikunai.com \
  --project switchboard \
  --owner-user-id user-123 \
  --lanes ADAPTER,COORD
```

Default paths:

| Platform | Service | Identity/config | Releases |
|---|---|---|---|
| macOS | `~/Library/LaunchAgents/com.6thelement.switchboard-agent-host.plist` | `~/.config/switchboard-agent-host` | `~/.local/share/switchboard-agent-host/releases` |
| Linux | `~/.config/systemd/user/switchboard-agent-host.service` | `~/.config/switchboard-agent-host` | `~/.local/share/switchboard-agent-host/releases` |

The service loads the bearer locally, advertises Codex `chatgpt_personal` readiness, and
executes `adapters/agent_host.py`. Personal work uses
`adapters.codex_local_worker:run`, which uses the already signed-in native Codex CLI directly,
refuses inherited OpenAI/Codex metered API keys, and requires the exact managed workspace,
task/claim/Work Session/runner/host/wake/source/connection binding. It does not request or
materialize a centrally stored provider credential. The separate
`adapters.codex_personal_worker:run` remains the CO credential-vault path and is not selected
by fresh host-local enrollment.

## Update, rotate, revoke, and uninstall

```bash
python adapters/agent_host_enrollment.py update \
  --bundle /path/to/switchboard-agent-host-0.2.1 \
  --public-key /path/to/agent-host-ed25519-public.pem \
  --state ~/.local/state/switchboard-agent-host/state.json

python adapters/agent_host_enrollment.py rotate \
  --identity ~/.config/switchboard-agent-host/identity.json \
  --config ~/.config/switchboard-agent-host/config.json

python adapters/agent_host_enrollment.py revoke \
  --identity ~/.config/switchboard-agent-host/identity.json \
  --config ~/.config/switchboard-agent-host/config.json \
  --state ~/.local/state/switchboard-agent-host/state.json

python adapters/agent_host_enrollment.py uninstall \
  --identity ~/.config/switchboard-agent-host/identity.json \
  --config ~/.config/switchboard-agent-host/config.json \
  --state ~/.local/state/switchboard-agent-host/state.json
```

Rotation invalidates the old bearer for ordinary host APIs immediately. For five minutes,
that hash is accepted only by the same host's rotation endpoint, so a lost HTTP response can
be retried without stranding the installation. A successful retry writes the replacement
identity atomically; revoke/uninstall removes every outstanding recovery hash.

After revoke/uninstall, confirm the identity and provider-runtime roots are clean:

```bash
python adapters/agent_host_enrollment.py residue-scan \
  ~/.config/switchboard-agent-host \
  ~/.local/state/switchboard-agent-host/provider-runtimes
```

## Wake admission

A wake with `execution_mode=personal_agent_host` or
`require_exact_host_binding=true` is refused before claim unless it contains the exact:

- wake and task IDs;
- claim, Work Session, and runner-session IDs;
- target agent and `runtime=codex`;
- exact host, source SHA, and typed execution-connection ID.

The account binding, execution binding, wake, selector, and local inventory must repeat the
same values; presence alone is insufficient. Source SHA must be a lowercase 40-character Git
SHA and all opaque IDs must use the bounded identifier grammar. Any mismatch or malformed
value is refused before the wake is claimed. Switchboard constructs this binding from the
live active claim and Work Session, derives the runner identity from the wake and host, and
revalidates those database relations atomically at claim time. The launched worker adopts
that existing claim and Work Session; it never creates a replacement session behind the wake.

The daemon heartbeat publishes `allow_work`, runtime capabilities/version, drain state,
session headroom, owner user/tenant/project/provider allowlists, local-auth availability,
and identity generation. Host token, local account proof, provider credential, and provider
profile contents never enter inventory, heartbeat, activity, or wake receipts.

Install fails before consuming the one-time bootstrap unless both `codex --version` and
`codex login status` succeed on the target host, the signed release is installed, and the
`0600` identity/state paths have been written durably. The preflight strips inherited
metered-key variables and persists only a redacted account fingerprint. Operators can repeat
that safe check independently with `python adapters/agent_host_enrollment.py preflight`.
If completion returns ambiguously, rerun the same install command with the same bootstrap
file within ten minutes. The installer reuses the pending key and recovery secret rather than
generating a second identity or requiring a new bootstrap.

## Executable proof

`test_agent_host_enrollment.py` builds and signs real bundles, tampers one to prove denial,
performs fresh sandboxed macOS and Linux installs through the REST ceremony, registers the
new host bearer, rotates and updates it, exercises offline and successful revoke, proves
post-revoke denial, recovers a deliberately lost enrollment response, launches the
host-local worker without a credential-vault binding, uninstalls Linux, and scans for residue.
`test_agent_host.py` covers the redacted heartbeat/inventory and relational exact-wake
admission contract.
