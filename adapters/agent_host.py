#!/usr/bin/env python3
"""Agent Host daemon — wake-intent consumer (AGENT-HOST-SPEC §7, ADAPTER-9, decision #5).

The always-on process on an agent host. It is the layer between the durable-but-pull-based bus
and the runtime adapters: it registers host inventory, polls Switchboard wake intents, and for
each eligible one launches/reuses a supervised run_agent session via supervisor.py — or lets the
substrate record that no eligible host answered.

    register_host
    loop every N s:
        heartbeat_host(capacity)
        pull eligible pending wake intents
        claim one (if capacity)  → launch supervised run_agent → confirm start → complete_wake
        reap exited sessions

Substrate endpoints (register_host / request_wake / claim_wake / complete_wake …) are Codex's
lane (store/app); this only CONSUMES them. Built fail-open against the spec's operation names —
a missing/!200 endpoint logs and is skipped, never crashes the daemon — so it is ready the moment
those land. Pin REST paths below once Codex publishes them. Config via env: PM_BASE, PM_PROJECT,
PM_MCP_TOKEN, PM_HOST_ID, PM_REPO_ROOT, PM_AGENT_HOST_SOURCE_REPO_ROOT,
PM_HOST_MAX_SESSIONS, PM_AGENT_WORK_MODULE (real work_fn;
absent -> --dry, which claims+abandons safely), PM_AGENT_HOST_ALLOW_WORK,
PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM.
"""
import hashlib
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
_SRC = os.path.join(_ROOT, "src")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
import switchboard_core as sb  # noqa: E402  (reuses _http + agent_id, same contract)
import co_drain  # noqa: E402
from agent_host_enrollment import (  # noqa: E402
    ACCOUNT_AFFINITIES_FILENAME,
    ACCOUNT_AFFINITY_IDS_KEY,
    preflight_codex_local_auth,
)
from codex.cloud_adapter import launch_wake as launch_codex_cloud_wake  # noqa: E402
from switchboard.connect import (  # noqa: E402
    Ack,
    Assignment,
    HostRuntimeConfig,
    ResourceLimits,
    build_launch_spec,
)
from switchboard.domain.coordination.runtime_profile import (  # noqa: E402
    RUNTIME_BINARIES,
    build_runtime_profile,
    runtime_env_key,
)

PROJECT = os.environ.get("PM_PROJECT", "switchboard")
SUPERVISOR = os.path.join(_HERE, "codex", "supervisor.py")
RUN_AGENT = os.path.join(_HERE, "run_agent.py")
CLOSURE_VERIFIER = os.path.join(_HERE, "closure_verifier.py")
DIRECT_CODEX_SESSION = os.path.join(_HERE, "direct_codex_session.py")

# Spec operation → REST path. Centralized so Codex's published paths get pinned in ONE place.
P_REGISTER_HOST = "/ixp/v1/register_host"
P_HEARTBEAT_HOST = "/ixp/v1/heartbeat_host"
P_LIST_WAKES = "/txp/v1/list_wake_intents"
P_CLAIM_WAKE = "/txp/v1/claim_wake"
P_COMPLETE_WAKE = "/txp/v1/complete_wake"
P_REGISTER_RUNNER = "/ixp/v1/register_runner_session"
P_HEARTBEAT_RUNNER = "/ixp/v1/heartbeat_runner_session"
P_MINT_HOST_TUNNEL_URL = "/ixp/v1/mint_host_tunnel_url"
P_LIST_RUNNER_CONTROLS = "/ixp/v1/runner_controls"
P_CLAIM_RUNNER_CONTROL = "/ixp/v1/claim_runner_control"
P_COMPLETE_RUNNER_CONTROL = "/ixp/v1/complete_runner_control"
P_LIST_RUNNERS = "/ixp/v1/runner_sessions"
P_LIST_WORK_SESSIONS = "/ixp/v1/work_sessions"
P_DIRECT_SESSION_MCP_TOKEN = "/ixp/v1/direct_assignments/mcp_token"
P_RUNNER_LEASE_DUE = "/ixp/v1/runner_lease_due"
P_TALLY_SPEND = "/tally/v1/spend/ingest"
MESSAGE_ONLY_LANE = "__MESSAGE_ONLY__"
RUNTIME_PROVIDERS = {
    "codex": "openai",
    "claude-code": "anthropic",
    "cursor": "cursor",
}
AGENT_HOST_VERSION = os.environ.get("PM_AGENT_HOST_VERSION", "0.2.0")
# Advertised when this build can serve browser Watch/Chat (supervisor PTY +
# outbound relay). Placement keys off this instead of sniffing version strings.
RUNNER_WATCH_CAPABILITY = "runner_watch"
RUNNER_LEASE_CAPABILITIES = ("execution_lease_v2", "runner_lease_enforcement")


def host_serves_runner_watch():
    """True only when this host can really deliver browser Watch/Chat.

    BUG-91: the capability gates placement, so a false positive puts work on a
    host whose runner nobody can watch -- the exact failure it exists to prevent.
    It therefore proves the relay path by importing the modules that carry it,
    the same ones _ensure_host_bridge needs at runtime. An image missing them
    advertises nothing and is skipped rather than silently accepting the work.
    """
    try:
        from switchboard.application import runner_pty_relay  # noqa: F401
        from codex.pty_host_ws_client import open_host_bridge  # noqa: F401
    except Exception:
        try:
            root = os.path.abspath(os.path.join(_HERE, ".."))
            for candidate in (root, os.path.join(root, "src")):
                if candidate not in sys.path:
                    sys.path.insert(0, candidate)
            from switchboard.application import runner_pty_relay  # noqa: F401,F811
            from codex.pty_host_ws_client import open_host_bridge  # noqa: F401,F811
        except Exception:
            return False
    return True
_LOCAL_AUTH_LAST_PROBE_AT = 0.0
_BOUND_FINALIZERS_LOCK = threading.Lock()
_BOUND_FINALIZERS = {}
_BOUND_FINALIZER_RESULTS = []

_RUNNER_TRANSPORT_METADATA_FIELDS = {
    "pty", "stream_url", "relay_url", "transport", "browser_safe",
    "relay_required",
}


def _csv(value):
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in str(value or "").replace("\n", ",").split(",") if x.strip()]


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def effective_work_modules(runtimes):
    """Return each runtime's effective module after the documented fallback."""
    fallback = str(os.environ.get("PM_AGENT_WORK_MODULE") or "").strip()
    out = {}
    for runtime in runtimes or []:
        runtime = str(runtime or "").strip()
        if not runtime:
            continue
        out[runtime] = str(os.environ.get(runtime_env_key(runtime)) or fallback).strip()
    return out


def effective_runtime_profile(runtimes, runner_watch=None):
    """Probe the current process environment and finishing toolchain."""
    normalized = [str(runtime or "").strip() for runtime in runtimes or []
                  if str(runtime or "").strip()]
    binary_names = {"git", "gh"}
    binary_names.update(
        RUNTIME_BINARIES[runtime] for runtime in normalized
        if runtime in RUNTIME_BINARIES
    )
    return build_runtime_profile(
        runtimes=normalized,
        work_modules=effective_work_modules(normalized),
        auto_work_session=_truthy(os.environ.get("PM_AUTO_WORK_SESSION")),
        agent_host_version=AGENT_HOST_VERSION,
        binaries={name: bool(shutil.which(name)) for name in binary_names},
        runner_watch=(host_serves_runner_watch()
                      if runner_watch is None else bool(runner_watch)),
    )


def _memory_resources():
    total = available = None
    try:
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
        total = int(os.sysconf("SC_PHYS_PAGES")) * page_size
    except (AttributeError, OSError, TypeError, ValueError):
        pass
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as source:
            values = {}
            for line in source:
                key, _, raw = line.partition(":")
                values[key] = int((raw.strip().split() or ["0"])[0]) * 1024
        total = values.get("MemTotal") or total
        available = values.get("MemAvailable")
    except (OSError, TypeError, ValueError):
        pass
    return {
        "memory_mb_total": round(total / 1024 / 1024, 1) if total else None,
        "memory_mb_available": round(available / 1024 / 1024, 1) if available else None,
    }


def _redacted_local_auth(runtime):
    """Advertise local personal-auth readiness without returning account material."""
    available = _truthy(os.environ.get("PM_HOST_LOCAL_AUTH_AVAILABLE"))
    mode = str(os.environ.get("PM_HOST_LOCAL_AUTH_MODE") or "").strip()
    raw_proof = str(os.environ.get("PM_HOST_LOCAL_AUTH_ACCOUNT_PROOF") or "").strip()
    fingerprint = ""
    if raw_proof:
        fingerprint = raw_proof if re.fullmatch(r"acct-[0-9a-f]{16}", raw_proof) else (
            "acct-" + hashlib.sha256(
                f"switchboard-local-auth:{runtime}:{raw_proof}".encode()).hexdigest()[:16])
    return {
        "available": available,
        "runtime": runtime,
        "auth_mode": mode or ("local" if available else "unavailable"),
        "account_fingerprint": fingerprint or None,
        "credential_values_redacted": True,
        "provider_credential_exported": False,
    }


def refresh_local_auth_inventory(inventory, *, now=None, force=False):
    """Re-probe personal Codex auth and atomically refresh admission inventory."""
    global _LOCAL_AUTH_LAST_PROBE_AT
    runtimes = inventory.get("runtimes") or []
    if len(runtimes) != 1 or runtimes[0].get("runtime") != "codex":
        return False
    current = dict(runtimes[0].get("local_auth") or {})
    if current.get("auth_mode") not in {"chatgpt_personal", "unavailable"}:
        return False
    checked_at = time.time() if now is None else float(now)
    try:
        interval = max(5.0, float(os.environ.get(
            "PM_HOST_LOCAL_AUTH_PROBE_INTERVAL_S", "30")))
    except ValueError:
        interval = 30.0
    if not force and checked_at - _LOCAL_AUTH_LAST_PROBE_AT < interval:
        return False
    _LOCAL_AUTH_LAST_PROBE_AT = checked_at
    try:
        proof = preflight_codex_local_auth(
            codex_executable=os.environ.get("PM_CODEX_EXECUTABLE") or "")
        if proof.get("authenticated") is not True:
            raise RuntimeError("native Codex local auth is unavailable")
        refreshed = {
            "available": True,
            "runtime": "codex",
            "auth_mode": "chatgpt_personal",
            "account_fingerprint": proof.get("account_fingerprint") or None,
            "credential_values_redacted": True,
            "provider_credential_exported": False,
        }
    except Exception as exc:
        refreshed = {
            "available": False,
            "runtime": "codex",
            "auth_mode": "chatgpt_personal",
            "account_fingerprint": None,
            "credential_values_redacted": True,
            "provider_credential_exported": False,
            "unavailable_reason": type(exc).__name__,
        }
    runtimes[0]["local_auth"] = refreshed
    inventory.setdefault("capacity", {})["local_auth"] = refreshed
    return current != refreshed


def _identity_inventory():
    generation = str(os.environ.get("PM_HOST_IDENTITY_GENERATION") or "").strip()
    return {
        "schema": "switchboard.agent_host_identity_proof.v1",
        "enrollment_id": os.environ.get("PM_HOST_ENROLLMENT_ID") or None,
        "identity_generation": int(generation) if generation.isdigit() else None,
        "public_key_fingerprint": os.environ.get("PM_HOST_PUBLIC_KEY_FINGERPRINT") or None,
        "credential_values_redacted": True,
    }


def _declared_account_affinities():
    """Read CO-6 account fingerprints this host's own bearer has declared locally
    (see `agent_host_enrollment.py declare-account`). Only the already-authenticated
    host process reads/writes this file, so a remote caller can never inject an
    affinity — it can only ever reflect what this host already asserted about itself."""
    config_path = str(os.environ.get("PM_AGENT_HOST_CONFIG_PATH") or "").strip()
    if not config_path:
        return []
    declarations_path = os.path.join(
        os.path.dirname(config_path), ACCOUNT_AFFINITIES_FILENAME)
    try:
        with open(declarations_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    fingerprints = data.get(ACCOUNT_AFFINITY_IDS_KEY) if isinstance(data, dict) else None
    if not isinstance(fingerprints, list):
        return []
    return [str(item).strip() for item in fingerprints if str(item or "").strip()]


def placement_inventory(repo, runtime, policy):
    """Build the truthful, non-secret host-placement advertisement used by CO-9."""
    try:
        disk = shutil.disk_usage(repo)
        disk_values = {
            "disk_gb_total": round(disk.total / 1024 ** 3, 2),
            "disk_gb_available": round(disk.free / 1024 ** 3, 2),
        }
    except OSError:
        disk_values = {"disk_gb_total": None, "disk_gb_available": None}
    binary_names = {"git", "python3", "gh"}
    binary_names.add("claude" if runtime == "claude-code" else runtime)
    binaries = sorted(name for name in binary_names if name and shutil.which(name))
    bound_wake_id = str(os.environ.get("PM_WAKE_ID") or "").strip()
    ephemeral = bool(bound_wake_id)
    scheduler_class = os.environ.get(
        "PM_HOST_CLASS", "ephemeral" if ephemeral else "persistent")
    supports_leases = _truthy(os.environ.get("PM_HOST_SUPPORTS_CREDENTIAL_LEASES"))
    # Capability taxonomy (CO-15). Scheduler class stays persistent/ephemeral.
    if os.environ.get("PM_AUTH_HOST_CLASSES"):
        auth_host_classes = _csv(os.environ.get("PM_AUTH_HOST_CLASSES"))
    elif ephemeral or scheduler_class == "ephemeral":
        auth_host_classes = ["managed_or_ephemeral_worker"]
    elif supports_leases:
        auth_host_classes = ["trusted_private_worker", "user_owned_persistent"]
    else:
        auth_host_classes = ["managed_or_user_owned_worker"]
    return {
        "schema": "switchboard.agent_host_placement.v1",
        "host_class": scheduler_class,
        "auth_host_classes": auth_host_classes,
        "cost_class": os.environ.get(
            "PM_HOST_COST_CLASS", "ephemeral_variable" if ephemeral else "already_paid"),
        "wakeable": True,
        # A provisioned CO worker is launched for exactly one wake.  Advertising the
        # non-secret wake id lets the coordinator exclude it from later placement;
        # the host-side queue filter remains the final enforcement boundary.
        "bound_wake_id": bound_wake_id or None,
        "drain_state": "accepting" if policy.get("allow_work") else "message_only",
        "tenant_ids": _csv(os.environ.get("PM_HOST_TENANTS", "")),
        # Provider-native enrollment is accepted only when this trusted host
        # explicitly attests the owning Switchboard user for the account affinity.
        # PM_HOST_OWNER_USERS (fleet/static) and PM_HOST_OWNER_USER_ID (ADAPTER-18
        # personal enrollment, one owner) are two producers of the same list.
        "owner_user_ids": sorted(set(
            _csv(os.environ.get("PM_HOST_OWNER_USERS", ""))
            + _csv(os.environ.get("PM_HOST_OWNER_USER_ID", ""))
        )),
        "projects": _csv(os.environ.get("PM_HOST_PROJECTS", PROJECT)),
        "providers": _csv(os.environ.get("PM_HOST_PROVIDERS", "")),
        "account_affinity_ids": sorted(set(
            _csv(os.environ.get("PM_HOST_ACCOUNT_AFFINITIES", ""))
            + _declared_account_affinities()
        )),
        "supports_credential_leases": supports_leases,
        "repositories": _csv(os.environ.get(
            "PM_HOST_REPOSITORIES", "6th-Element-Labs/projectplanner")),
        "session_policies": _csv(os.environ.get("PM_HOST_SESSION_POLICIES", "code_strict")),
        "isolation_modes": _csv(os.environ.get("PM_HOST_ISOLATION", "task_worktree")),
        "runtime_binaries": binaries,
        "provider_capacity_mode": "external_account_admission",
        "resources": {
            "cpu_total": os.cpu_count(),
            # CPU availability is scheduler input only when a host monitor supplies it;
            # total logical CPUs are not a truthful measure of current headroom.
            "cpu_available": (
                float(os.environ["PM_HOST_CPU_AVAILABLE"])
                if os.environ.get("PM_HOST_CPU_AVAILABLE") else None
            ),
            **_memory_resources(),
            **disk_values,
        },
        "concurrency": {
            "max_sessions": int(os.environ.get("PM_HOST_MAX_SESSIONS", "2")),
        },
    }


def host_policy_from_env(lanes):
    allow_work = _truthy(os.environ.get("PM_AGENT_HOST_ALLOW_WORK"))
    allow_global = _truthy(os.environ.get("PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM"))
    if not allow_work:
        mode = "message_only"
    elif allow_global:
        mode = "global_claim_allowed"
    elif lanes:
        mode = "lane_scoped"
    else:
        mode = "unconfigured_no_lanes"
    return {
        "mode": mode,
        "allow_message_only": True,
        "allow_work": allow_work,
        "allow_global_claim": allow_global,
        "allowed_lanes": lanes,
    }


def _try(method, path, body=None):
    """Fail-open REST: returns dict on success, None on any error (endpoint absent yet, etc.)."""
    try:
        return sb._http(method, path, body)
    except Exception as e:
        print(f"[agent_host] {method} {path} unavailable ({type(e).__name__}); skipping", flush=True)
        return None


def mint_host_tunnel_url(runner_session_id, host_id):
    """Ask Switchboard for a fresh relay URL without exposing its signing key."""
    result = _try("POST", P_MINT_HOST_TUNNEL_URL, {
        "project": PROJECT,
        "runner_session_id": str(runner_session_id or ""),
        "host_id": str(host_id or ""),
    }) or {}
    return dict(result.get("server_relay") or {})


def _fresh_server_relay(server_relay, runner_session_id, host_id):
    """Use an attached capability, pulling one when the bridge has none."""
    relay = dict(server_relay or {})
    if relay.get("host_url"):
        return relay
    return mint_host_tunnel_url(runner_session_id, host_id) or relay


def _consume_host_relay_refresh_request(runner_session_id, host_id):
    """Use the enrolled-host credential for a companion-requested refresh."""
    try:
        from codex import supervisor as _sup
        request_path = _sup._session_dir(
            str(runner_session_id or "")) / "host_relay.refresh"
        if not request_path.exists():
            return {}
        relay = mint_host_tunnel_url(runner_session_id, host_id)
        if relay.get("host_url"):
            request_path.unlink(missing_ok=True)
        return relay
    except Exception as exc:  # noqa: BLE001
        print(
            f"[agent_host] host relay refresh failed "
            f"runner_session_id={runner_session_id} error={type(exc).__name__}",
            flush=True,
        )
        return {}


def _require(method, path, body=None):
    """Fail-closed REST used for COORD-34 claim-bound runner registration."""
    try:
        return sb._http(method, path, body)
    except Exception as e:
        print(f"[agent_host] {method} {path} failed ({type(e).__name__}): {e}", flush=True)
        return {
            "error": "runner_bind_incomplete",
            "error_code": "runner_bind_incomplete",
            "failure_class": "unbound_identity",
            "refused": True,
            "message": f"{method} {path} failed: {type(e).__name__}",
        }


def default_inventory():
    repo = (os.environ.get("PM_AGENT_HOST_SOURCE_REPO_ROOT")
            or os.environ.get("PM_REPO_ROOT") or _git_root())
    host_id = os.environ.get("PM_HOST_ID") or f"host/{socket.gethostname().split('.')[0]}"
    env_lanes = _csv(os.environ.get("PM_HOST_LANES", ""))
    policy = host_policy_from_env(env_lanes)
    runtime_lanes = env_lanes or ([MESSAGE_ONLY_LANE] if not policy["allow_work"] else [])
    runtime = os.environ.get("PM_RUNTIME", "claude-code")
    provider = os.environ.get("PM_PROVIDER") or RUNTIME_PROVIDERS.get(runtime, runtime)
    cloud_enabled = runtime == "codex" and bool(os.environ.get("PM_CODEX_CLOUD_ENVIRONMENT_ID"))
    profiles = ["ixp.v1", "txp.dispatch.v0"]
    capabilities = ["docs", "python", "github", "tests"]
    capabilities.extend(RUNNER_LEASE_CAPABILITIES)
    # Fleet workers advertise a host-owned capability profile.  The wake payload may
    # select from this inventory, but it cannot add capabilities to the host.  Keeping
    # this in configuration lets co-general/co-build use the same immutable AMI while
    # still failing closed when a heavy-build wake lands on a general worker.
    capabilities.extend(_csv(os.environ.get("PM_HOST_CAPABILITIES", "")))
    # BUG-91: self-declare Watch/Chat only when this host can genuinely serve it.
    # The claim must mean "I can deliver PTY output, accept input, and reach the
    # relay" -- not merely "I am a newer build". A version number would be the
    # easy signal and the wrong one: it says nothing about whether the relay
    # modules are actually installed on this image.
    runner_watch_proven = host_serves_runner_watch()
    if runner_watch_proven:
        capabilities.append(RUNNER_WATCH_CAPABILITY)
    capabilities = list(dict.fromkeys(capabilities))
    if cloud_enabled:
        profiles.append("cloud_execution")
        capabilities.append("cloud_execution")
    placement = placement_inventory(repo, runtime, policy)
    local_auth = _redacted_local_auth(runtime)
    runtime_profile = effective_runtime_profile([runtime], runner_watch=runner_watch_proven)
    owner = {
        "user_id": os.environ.get("PM_HOST_OWNER_USER_ID") or None,
        "tenant_allowlist": placement.get("tenant_ids") or [],
        "project_allowlist": placement.get("projects") or [],
        "provider_allowlist": placement.get("providers") or [],
    }
    return {
        "project": PROJECT, "host_id": host_id, "hostname": socket.gethostname(),
        "agent_host_version": AGENT_HOST_VERSION, "repo_root": repo,
        "policy": policy,
        "runtimes": [{
            "runtime": runtime,
            "provider": provider,
            "launcher": "codex cloud exec" if cloud_enabled else (
                "codex" if runtime == "codex" else sys.executable),
            "profiles": profiles,
            "control": {"mode": "hook_deny", "runner_kill": True, "host_policy": policy["mode"]},
            "policy": policy,
            "lanes": runtime_lanes,
            "capabilities": capabilities,
            "local_auth": local_auth,
        }],
        "limits": {"max_sessions": int(os.environ.get("PM_HOST_MAX_SESSIONS", "2"))},
        "capacity": {
            "active_sessions": 0,
            "headroom": int(os.environ.get("PM_HOST_MAX_SESSIONS", "2")),
            "drain_state": placement.get("drain_state"),
            "placement": placement,
            "identity": _identity_inventory(),
            "owner": owner,
            "local_auth": local_auth,
            "runtime_profile": runtime_profile,
        },
        "heartbeat_ttl_s": 60,
    }


def heartbeat_capacity(inventory):
    """Return the full non-secret admission record for each heartbeat."""
    active = active_session_count(inventory)
    maximum = int((inventory.get("limits") or {}).get("max_sessions") or 0)
    capacity = dict(inventory.get("capacity") or {})
    capacity.update({
        "active_sessions": active,
        "headroom": max(0, maximum - active),
        "allow_work": bool((inventory.get("policy") or {}).get("allow_work")),
        "drain_state": ((capacity.get("placement") or {}).get("drain_state")
                        or capacity.get("drain_state") or "accepting"),
        # Re-probe on every registration/heartbeat.  A daemon-start snapshot
        # would miss an in-place binary removal or PATH/profile correction.
        "runtime_profile": effective_runtime_profile([
            entry.get("runtime") for entry in inventory.get("runtimes") or []
            if isinstance(entry, dict)
        ]),
    })
    return capacity


def registration_inventory(inventory, drain_request=None):
    """Build a host advertisement from live supervisor capacity.

    Registration is periodically renewed, so it must be just as current as a
    heartbeat.  Reusing the inventory constructed at process startup resets a
    busy host to 0 active sessions on every renewal.
    """
    advertised = dict(inventory)
    advertised["capacity"] = heartbeat_capacity(inventory)
    if drain_request:
        advertised = co_drain.inventory_for_drain(advertised)
        placement = ((advertised.get("capacity") or {}).get("placement") or {})
        placement["drain_state"] = "draining"
    return advertised


def apply_authoritative_execution_policy(inventory, response):
    """Hot-apply the authenticated server policy to one enrolled personal host.

    The enrollment record is the durable authority.  Local installer environment
    values are only bootstrap defaults, so an operator can broaden or tighten lane
    scope and concurrency without rotating credentials or touching launchd.
    """
    policy = dict((response or {}).get("authoritative_execution_policy") or {})
    if not policy:
        return False
    if policy.get("runtime") != "codex" or policy.get("allow_global_claim") is not False:
        print("[agent_host] refused invalid authoritative execution policy", flush=True)
        return False
    try:
        maximum = int(policy.get("max_sessions"))
    except (TypeError, ValueError):
        return False
    if not 1 <= maximum <= 32:
        return False
    lane_mode = str(policy.get("lane_mode") or "explicit")
    lanes = sorted({str(item).strip() for item in policy.get("lanes") or []
                    if str(item).strip()})
    if lane_mode not in {"explicit", "all_project_lanes"}:
        return False
    if lane_mode == "explicit" and not lanes:
        return False
    if lane_mode == "all_project_lanes":
        lanes = []
    runtimes = inventory.get("runtimes") or []
    if len(runtimes) != 1 or runtimes[0].get("runtime") != "codex":
        return False
    runtime = runtimes[0]
    before = json.dumps({
        "lanes": runtime.get("lanes"),
        "capabilities": runtime.get("capabilities"),
        "policy": runtime.get("policy"),
        "max_sessions": (inventory.get("limits") or {}).get("max_sessions"),
    }, sort_keys=True, default=str)
    host_policy = dict(runtime.get("policy") or {})
    host_policy.update({
        "mode": "project_wide" if lane_mode == "all_project_lanes" else "lane_scoped",
        "allow_message_only": True,
        "allow_work": bool(policy.get("allow_work")),
        "allow_global_claim": False,
        "allowed_lanes": lanes,
        "lane_mode": lane_mode,
    })
    # BUG-91: runner_watch is a host-PROVEN fact, not an operator-grantable
    # permission. The authoritative policy selects every other capability, but
    # it can neither grant Watch to a host that cannot serve it (work would land
    # on a host whose runner nobody can watch) nor strip it from one that can
    # (registration advertised it, then the first heartbeat's policy replaced
    # the list wholesale and silently un-advertised it — which would starve
    # placement the moment PM_COORD_REQUIRE_RUNNER_WATCH is enforced).
    capabilities = [item for item in (policy.get("capabilities") or [])
                    if str(item).strip().lower() != RUNNER_WATCH_CAPABILITY]
    # SIMPLIFY-20 / BUG-161: these are host-proven execution facts, not
    # operator-grantable permissions. An older enrolled policy must not strip
    # them after startup and make an enforcement-capable host ineligible.
    capabilities.extend(RUNNER_LEASE_CAPABILITIES)
    if host_serves_runner_watch():
        capabilities.append(RUNNER_WATCH_CAPABILITY)
    runtime.update({
        "lanes": lanes,
        "capabilities": list(dict.fromkeys(capabilities)),
        "policy": host_policy,
    })
    runtime.setdefault("control", {})["host_policy"] = host_policy["mode"]
    inventory["policy"] = host_policy
    inventory.setdefault("limits", {})["max_sessions"] = maximum
    capacity = inventory.setdefault("capacity", {})
    capacity["headroom"] = max(0, maximum - active_session_count(inventory))
    placement = capacity.setdefault("placement", {})
    placement.setdefault("concurrency", {})["max_sessions"] = maximum
    after = json.dumps({
        "lanes": runtime.get("lanes"),
        "capabilities": runtime.get("capabilities"),
        "policy": runtime.get("policy"),
        "max_sessions": inventory["limits"]["max_sessions"],
    }, sort_keys=True, default=str)
    changed = before != after
    if changed:
        print(
            f"[agent_host] applied policy revision {policy.get('revision') or '?'}: "
            f"lane_mode={lane_mode} max_sessions={maximum}", flush=True)
    return changed


def validate_personal_wake_binding(wake, inventory):
    """Fail closed when a personal-host wake opts into the exact-bind contract."""
    policy = (wake or {}).get("policy") or {}
    personal = (policy.get("execution_mode") == "personal_agent_host"
                or policy.get("require_exact_host_binding") is True)
    if not personal:
        return {"required": False, "valid": True}
    selector = (wake or {}).get("selector") or {}
    binding = policy.get("account_binding") or {}
    execution = policy.get("execution_binding") or {}
    expected_runner_session_id = _runner_session_id_for_wake(
        wake or {}, str(inventory.get("host_id") or ""))
    sources = {
        "wake_id": [(wake or {}).get("wake_id"), execution.get("wake_id")],
        "task_id": [
            (wake or {}).get("task_id"), binding.get("task_id"), execution.get("task_id")],
        "claim_id": [binding.get("claim_id"), execution.get("claim_id")],
        "work_session_id": [
            binding.get("work_session_id"), execution.get("work_session_id")],
        "runner_session_id": [
            binding.get("runner_session_id"), execution.get("runner_session_id"),
            expected_runner_session_id],
        "host_id": [
            inventory.get("host_id"), binding.get("host_id"), execution.get("host_id")],
        "agent_id": [selector.get("agent_id"), binding.get("agent_id"),
                     execution.get("agent_id")],
        "execution_connection_id": [
            policy.get("execution_connection_id"),
            execution.get("execution_connection_id")],
        "source_sha": [policy.get("source_sha"), execution.get("source_sha")],
    }
    missing = sorted(
        f"{key}[{index}]"
        for key, candidates in sources.items()
        for index, value in enumerate(candidates)
        if not str(value or "").strip()
    )
    if selector.get("runtime") != "codex":
        missing.append("selector.runtime=codex")
    if missing:
        return {"required": True, "valid": False, "error": "wake_binding_incomplete",
                "failure_class": "unbound_identity", "missing": sorted(set(missing))}

    normalized = {
        key: [str(value).strip() for value in candidates]
        for key, candidates in sources.items()
    }
    mismatches = sorted(
        key for key, candidates in normalized.items() if len(set(candidates)) != 1)
    opaque_fields = (
        "wake_id", "task_id", "claim_id", "work_session_id", "runner_session_id",
        "host_id", "agent_id", "execution_connection_id",
    )
    malformed = sorted(
        key for key in opaque_fields
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{2,255}", value)
               for value in normalized[key])
    )
    if any(not re.fullmatch(r"[0-9a-f]{40}", value)
           for value in normalized["source_sha"]):
        malformed.append("source_sha")
    if mismatches or malformed:
        return {
            "required": True,
            "valid": False,
            "error": "wake_binding_inconsistent",
            "failure_class": "unbound_identity",
            "mismatches": mismatches,
            "malformed": sorted(set(malformed)),
        }
    return {"required": True, "valid": True,
            "binding": {key: candidates[0] for key, candidates in normalized.items()}}


def _git_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=3)
        return r.stdout.strip() or os.getcwd()
    except Exception:
        return os.getcwd()


def eligible_runtime(wake, inventory):
    """Return the host runtime entry that can serve this wake, else None (skip → don't claim)."""
    sel = (wake or {}).get("selector") or {}
    want_rt, want_lane = sel.get("runtime"), sel.get("lane")
    want_provider = sel.get("provider")
    want_caps = set(_csv(sel.get("capabilities") or []))
    requested_mode = str(((wake or {}).get("policy") or {}).get("mode") or "").strip()
    wants_claim = requested_mode in {"claim_next", "direct_task"} or bool(
        want_lane and requested_mode != "message_only")
    for rt in inventory["runtimes"]:
        if want_rt and rt["runtime"] != want_rt:
            continue
        host_provider = rt.get("provider") or RUNTIME_PROVIDERS.get(
            str(rt.get("runtime") or ""), rt.get("runtime"))
        if want_provider and host_provider != want_provider:
            continue
        rt_policy = {**(inventory.get("policy") or {}), **(rt.get("policy") or {})}
        rt_lanes = set(rt.get("lanes") or [])
        if wants_claim:
            if not rt_policy.get("allow_work"):
                continue
            if want_lane:
                if (rt_policy.get("lane_mode") != "all_project_lanes"
                        and want_lane not in rt_lanes):
                    continue
            elif not rt_policy.get("allow_global_claim"):
                continue
        elif want_lane and rt_lanes and want_lane not in rt_lanes and MESSAGE_ONLY_LANE not in rt_lanes:
            continue
        if want_caps and not want_caps.issubset(set(rt.get("capabilities") or [])):
            continue
        return rt
    return None


def wakes_bound_to_host(wakes):
    """Restrict an ephemeral fleet host to the exact wake that launched it.

    Persistent Agent Hosts do not set ``PM_WAKE_ID`` and retain the shared eligible
    queue behavior. A fleet worker does set it; accepting another same-lane wake would
    break the provisioner's task/runtime/credential affinity guarantee.
    """
    bound_wake_id = str(os.environ.get("PM_WAKE_ID") or "").strip()
    if not bound_wake_id:
        return list(wakes or [])
    return [wake for wake in (wakes or []) if wake.get("wake_id") == bound_wake_id]


def wake_mode(wake, inventory=None):
    """Choose the safe launch mode for a wake.

    Lane-scoped wakes may enter the claim_next loop. Lane-less wakes are message-only by
    construction: they can register and read inbox, but must never ask for global work.
    A closure_verification wake is a special case of message-only: still lane-less (never
    a claim_next grab), but instead of the inbox-only ack stub it runs the deterministic
    closure engine (DELIVERABLES-23) — bounded gate checks, not an open-ended agent.
    """
    policy = (wake or {}).get("policy") or {}
    selector = (wake or {}).get("selector") or {}
    explicit = (policy.get("mode") or "").strip()
    if explicit == "connect":
        return "connect"
    if explicit == "direct_task":
        return "direct_task"
    if explicit == "cloud_execution" or policy.get("kind") == "cloud_execution":
        return "cloud_execution"
    if policy.get("kind") == "closure_verification" and policy.get("deliverable_id"):
        return "closure_verify"
    if explicit in ("inbox_only", "message_only"):
        return "inbox_only"
    if explicit == "claim_next" and selector.get("lane"):
        return "claim_next"
    if explicit == "claim_next":
        inv_policy = (inventory or {}).get("policy") or {}
        return "claim_next" if inv_policy.get("allow_global_claim") else "refused"
    if selector.get("lane"):
        return "claim_next"
    return "inbox_only"


def active_session_count(inventory):
    """Best-effort live session count from the supervisor (capacity gate). 0 on any error."""
    try:
        out = subprocess.run(
            [sys.executable, SUPERVISOR, "list"],
            capture_output=True, text=True, timeout=10)
        data = json.loads(out.stdout or "[]")
        sessions = data if isinstance(data, list) else data.get("sessions", [])
        return sum(1 for s in sessions if s.get("status") == "running")
    except Exception:
        return 0


def active_codex_cloud_session_count():
    """Count centrally bound non-terminal Codex cloud sessions; None fails capacity closed."""
    result = _try(
        "GET",
        f"{P_LIST_RUNNERS}?project={PROJECT}&runtime=codex&include_stale=false",
    )
    if result is None:
        return None
    sessions = result.get("sessions") if isinstance(result, dict) else result
    if not isinstance(sessions, list):
        return None
    active = 0
    for session in sessions:
        metadata = session.get("metadata") or {}
        if metadata.get("vendor_id") != "openai-codex-cloud" or session.get("stale"):
            continue
        if str(session.get("status") or "").lower() not in _TERMINAL_RUNNER_STATES:
            active += 1
    return active


def _connect_mcp_endpoint():
    """Public MCP URL the host already uses for Switchboard Communicate."""
    base = str(os.environ.get("PM_BASE") or "https://plan.taikunai.com").rstrip("/")
    return f"{base}/mcp"


def _connect_codex_mcp_argv():
    """Codex -c overrides that require host Communicate for Connect sessions."""
    endpoint = _connect_mcp_endpoint()
    return (
        "-c", f"mcp_servers.taikun_plan.url={json.dumps(endpoint)}",
        "-c", 'mcp_servers.taikun_plan.bearer_token_env_var='
              '"SWITCHBOARD_CONNECT_SESSION_TOKEN"',
        "-c", "mcp_servers.taikun_plan.required=true",
    )


def _issue_connect_session_mcp_token(wake, inventory, runner_session_id):
    """Mint the task principal used by a Connect session's MCP client."""
    result = sb._http("POST", P_DIRECT_SESSION_MCP_TOKEN, {
        "project": PROJECT,
        "wake_id": str(wake.get("wake_id") or ""),
        "host_id": str(inventory.get("host_id") or ""),
        "runner_session_id": str(runner_session_id or ""),
    })
    token = str((result or {}).get("token") or "").strip()
    if (result or {}).get("issued") is not True or not token.startswith("dst-"):
        raise RuntimeError("Connect Switchboard MCP authentication was denied")
    return token


def launch_command(wake, inventory, runner_session_id=""):
    """Build the supervisor command for a wake without executing it."""
    sel = wake.get("selector") or {}
    eligible = eligible_runtime(wake, inventory)
    if not eligible:
        raise ValueError("wake is not eligible for this host policy/runtime inventory")
    agent_id = sel.get("agent_id") or sel.get("runtime") or "claude-code"
    lane = sel.get("lane") or ""
    runtime = sel.get("runtime") or eligible.get("runtime") or "claude-code"
    runtime_key = re.sub(r"[^A-Z0-9]+", "_", str(runtime).upper()).strip("_")
    work_mod = os.environ.get(f"PM_AGENT_WORK_MODULE_{runtime_key}", "").strip()
    if not work_mod:
        work_mod = os.environ.get("PM_AGENT_WORK_MODULE", "").strip()
    runtime_markers = {
        "codex": ("codex",),
        "claude-code": ("claude",),
        "cursor": ("cursor",),
    }
    markers = runtime_markers.get(str(runtime), ())
    if work_mod and markers and not any(marker in work_mod.lower() for marker in markers):
        raise ValueError(
            f"work module {work_mod!r} does not match requested runtime {runtime!r}")
    mode = wake_mode(wake, inventory)
    if mode == "refused":
        raise ValueError("wake asks for global claim_next but host policy forbids global work")
    if mode == "connect":
        connect_policy = wake.get("policy") or {}
        assignment_data = dict(connect_policy.get("assignment") or {})
        assignment_schema = assignment_data.pop("schema", "")
        if assignment_schema != "switchboard.connect.assignment.v1":
            raise ValueError("connect assignment schema is invalid")
        limits = assignment_data.get("limits") or {}
        assignment_data["limits"] = ResourceLimits(**limits)
        assignment = Assignment(**assignment_data)
        if assignment.runtime != runtime:
            raise ValueError("connect assignment runtime mismatch")
        # Connect boots the INTERACTIVE CLI session inside the supervised PTY;
        # Switchboard assigns the task through the normal handshake. One-shot
        # batch modes (codex exec / claude -p / cursor-agent -p) render as a
        # scrolling log with no composer, so Watch cannot show a real session
        # and chat injection has nothing to type into. PM_CONNECT_<RT>_ARGS
        # still overrides per host.
        runtime_defaults = {
            "codex": ("codex", "--dangerously-bypass-approvals-and-sandbox"),
            "claude-code": ("claude", "--dangerously-skip-permissions"),
            "cursor": ("cursor-agent", "--force"),
        }
        executable_default, args_default = runtime_defaults.get(
            runtime, (runtime, "--prompt"))
        executable = str(os.environ.get(
            f"PM_CONNECT_{runtime_key}_EXECUTABLE", executable_default)).strip()
        before = tuple(shlex.split(str(os.environ.get(
            f"PM_CONNECT_{runtime_key}_ARGS", args_default))))
        # Host-side Communicate attachment (not Connect assignment content):
        # require the same taikun_plan MCP surface Direct already uses so
        # "via Switchboard" means MCP tools, not improvised REST/curl.
        if runtime == "codex":
            before = before + _connect_codex_mcp_argv()
        config = HostRuntimeConfig(
            runtime=runtime,
            provider=assignment.provider,
            executable=executable,
            arguments_before_note=before,
        )
        lifecycle = dict(connect_policy.get("lifecycle") or {})
        execution_assignment = dict(
            connect_policy.get("execution_assignment") or {})
        if not execution_assignment:
            raise ValueError("connect execution assignment contract is missing")
        from switchboard.connect.execution_assignment import (
            ExecutionAssignmentError,
            build_execution_assignment,
            require_exact_execution_assignment,
        )
        try:
            expected = build_execution_assignment(
                task_id=str(wake.get("task_id") or ""),
                assignment=assignment_data,
                lifecycle=lifecycle,
            )
            require_exact_execution_assignment(execution_assignment, expected)
        except ExecutionAssignmentError as exc:
            raise ValueError(
                "connect execution assignment disagrees with persisted lease: "
                f"{exc.code}") from exc
        now = time.time()
        spec = build_launch_spec(
            Ack(
                lease_id=str(wake.get("wake_id") or assignment.assignment_id),
                runner_id=runner_session_id or _runner_session_id_for_wake(
                    wake, inventory.get("host_id") or ""),
                assignment=assignment,
                host_id=str(inventory.get("host_id") or ""),
                issued_at=now,
                expires_at=now + assignment.limits.max_runtime_seconds,
                heartbeat_interval_seconds=30,
                last_heartbeat_at=now,
            ),
            config,
            workspace_path=str(inventory.get("repo_root") or _git_root()),
            completion_contract=execution_assignment,
        )
        child = list(spec.argv)
    elif mode == "direct_task":
        if runtime != "codex" or not wake.get("task_id"):
            raise ValueError("direct task assignment requires a task-bound Codex runtime")
        child = [sys.executable, DIRECT_CODEX_SESSION]
    elif mode == "closure_verify":
        policy = wake.get("policy") or {}
        child = [sys.executable, CLOSURE_VERIFIER, "--project", PROJECT,
                 "--deliverable-id", policy.get("deliverable_id"),
                 "--host-id", inventory.get("host_id", "")]
        if wake.get("wake_id"):
            child += ["--wake-id", wake.get("wake_id")]
    elif mode == "inbox_only":
        idle = os.environ.get("PM_AGENT_HOST_INBOX_IDLE_SECONDS", "6")
        child = [sys.executable, RUN_AGENT, "--runtime", runtime,
                 "--inbox-only", "--idle-seconds", idle]
        if _truthy(os.environ.get("PM_AGENT_HOST_ACK_INBOX_ONLY", "1")):
            child.append("--ack-inbox")
    else:
        child = [sys.executable, RUN_AGENT, "--runtime", runtime, "--max-tasks", "1"]
        if lane:
            child += ["--lanes", lane]
        elif not (inventory.get("policy") or {}).get("allow_global_claim"):
            raise ValueError("global claim_next requires PM_AGENT_HOST_ALLOW_GLOBAL_CLAIM=1")
        idle = os.environ.get("PM_AGENT_HOST_CLAIM_IDLE_SECONDS", "6")
        child += ["--idle-seconds", idle]
        if (wake.get("task_id")
                and (wake.get("policy") or {}).get("require_runner_bind") is True):
            # Task-bound Autopilot wakes must take the exact bootstrap route. Do
            # not rely on an inherited default that can fall through to globally
            # forbidden claim_next for a narrow Agent Host principal.
            child.append("--auto-work-session")
        child += (["--work-module", work_mod] if work_mod else ["--dry"])
    cmd = [sys.executable, SUPERVISOR, "start", "--agent-id", agent_id,
           "--cwd", (spec.cwd if mode == "connect" else inventory["repo_root"])]
    if runner_session_id:
        cmd += ["--runner-session-id", runner_session_id]
    if wake.get("wake_id"):
        cmd += ["--wake-id", str(wake.get("wake_id"))]
    if mode:
        cmd += ["--wake-mode", str(mode)]
    if wake.get("task_id"):
        cmd += ["--task-id", wake.get("task_id")]
    cmd += ["--"] + child
    return cmd, mode


def launch(wake, inventory, runner_session_id="", extra_env=None):
    """Spawn a supervised run_agent for this wake via supervisor.py (the proven CLI). Returns the
    supervisor session record (with runner_session_id, pid) or None on failure."""
    mode = wake_mode(wake, inventory)
    if mode == "cloud_execution":
        selector = wake.get("selector") or {}
        if selector.get("runtime") != "codex":
            return {"started": False, "cloud_session": True, "wake_mode": mode,
                    "reason": "cloud_runtime_unsupported", "failure_class": "invalid_input"}
        count = active_codex_cloud_session_count()
        if count is None:
            return {"started": False, "cloud_session": True, "wake_mode": mode,
                    "reason": "cloud_capacity_readback_unavailable",
                    "failure_class": "broken_connection"}
        rec = launch_codex_cloud_wake(wake, inventory, active_sessions=count)
        rec["host_id"] = inventory.get("host_id")
        return rec
    cmd, mode = launch_command(wake, inventory, runner_session_id=runner_session_id)
    try:
        env = os.environ.copy()
        if mode == "connect":
            assignment = dict((wake.get("policy") or {}).get("assignment") or {})
            execution_assignment = dict(
                (wake.get("policy") or {}).get("execution_assignment") or {})
            env.update({
                "SWITCHBOARD_CONNECT_ASSIGNMENT_ID": str(
                    assignment.get("assignment_id") or ""),
                "SWITCHBOARD_CONNECT_PRINCIPAL_REF": str(
                    assignment.get("principal_ref") or ""),
                "SWITCHBOARD_CONNECT_WORK_REF": str(assignment.get("work_ref") or ""),
                "SWITCHBOARD_CONNECT_WORKSPACE_REF": str(
                    assignment.get("workspace_ref") or ""),
                "SWITCHBOARD_CONNECT_LEASE_ID": str(wake.get("wake_id") or ""),
                "SWITCHBOARD_CONNECT_RUNNER_ID": str(runner_session_id or ""),
            })
            encoded_assignment = json.dumps(
                execution_assignment, sort_keys=True, separators=(",", ":"))
            env["SWITCHBOARD_EXECUTION_ASSIGNMENT_JSON"] = encoded_assignment
            env["SWITCHBOARD_COMPLETION_CONTRACT_JSON"] = encoded_assignment
            # Never expose the enrolled host bearer to the child.  It only has
            # host-management authority; the session receives an exact,
            # short-lived task principal minted after claim_wake.  The session's
            # pre-configured Switchboard MCP client reads its bearer from
            # PM_MCP_TOKEN (bearer_token_env_var), so the minted principal MUST
            # override the inherited host bearer there — otherwise the child
            # still authenticates as the narrow host and register_agent/claims
            # stay forbidden (the exact BUG-139 symptom).
            session_token = _issue_connect_session_mcp_token(
                wake, inventory, runner_session_id)
            env["SWITCHBOARD_CONNECT_SESSION_TOKEN"] = session_token
            env["PM_MCP_TOKEN"] = session_token
            env.pop("SWITCHBOARD_TOKEN", None)
        env.update({str(k): str(v) for k, v in (extra_env or {}).items()})
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=env)
        if out.returncode != 0 or not (out.stdout or "").strip():
            detail = (out.stderr or out.stdout or "supervisor emitted no receipt")[-4000:]
            print(
                f"[agent_host] supervisor start failed rc={out.returncode} "
                f"stderr={detail!r}", flush=True)
            return {
                "runner_session_id": runner_session_id or None,
                "started": False,
                "wake_mode": mode,
                "host_id": inventory.get("host_id"),
                "runtime": (wake.get("selector") or {}).get("runtime") or "",
                "task_id": wake.get("task_id") or "",
                "reason": "supervisor_start_failed",
                "failure_class": "failed_gate",
                "provider_error": detail,
            }
        rec = json.loads(out.stdout)
        if isinstance(rec, dict):
            rec["wake_mode"] = mode
            rec["host_id"] = inventory.get("host_id")
            rec["runtime"] = (wake.get("selector") or {}).get("runtime") or ""
            rec["task_id"] = rec.get("task_id") or wake.get("task_id") or ""
        return rec
    except Exception as e:
        print(f"[agent_host] launch failed: {e}", flush=True)
        return {
            "runner_session_id": runner_session_id or None,
            "started": False,
            "wake_mode": mode,
            "host_id": inventory.get("host_id"),
            "runtime": (wake.get("selector") or {}).get("runtime") or "",
            "task_id": wake.get("task_id") or "",
            "reason": "runtime_launch_exception",
            "failure_class": "failed_gate",
            "provider_error": str(e)[:4000],
        }


def confirm_started(rec, grace_s=4.0):
    """Confirm the launched process is alive after a short grace (proxy for 'runtime came up')."""
    if (rec or {}).get("cloud_session"):
        return bool(rec.get("started") and rec.get("provider_session_id") and rec.get("session_url"))
    pid = (rec or {}).get("pid")
    if not pid:
        return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            return False
        time.sleep(0.5)
    return True


def _tail_json_result(log_path):
    """Best-effort parse of a launched job's own last JSON line from its log. Returns
    the parsed dict, or None if the file is missing/empty/unparsable."""
    if not log_path:
        return None
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            lines = [ln.strip() for ln in f if ln.strip()]
    except OSError:
        return None
    if not lines:
        return None
    try:
        result = json.loads(lines[-1])
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None


def confirm_closure_verified(rec, grace_s=4.0):
    """Like confirm_started, but for the closure_verify job: it is deterministic and
    often finishes within confirm_started's own liveness window on success (a
    scope-only gate resolves in well under a second) — 'no longer alive' must not be
    conflated with 'crashed', or the daemon logs launch_failed for jobs that actually
    ran fine and persisted a report. Still-alive at the deadline is success (matches
    confirm_started). Once it has exited, trust its own last-line JSON verdict
    (adapters/closure_verifier.py always prints one) rather than raw process liveness.
    """
    pid = (rec or {}).get("pid")
    if not pid:
        return False
    deadline = time.time() + grace_s
    while time.time() < deadline:
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            result = _tail_json_result((rec or {}).get("log_path"))
            return bool(result) and not result.get("error")
        time.sleep(0.5)
    return True


def register_runner_session(rec, wake, inventory):
    """Publish the supervisor session to Switchboard's central runner registry.

    COORD-34: claimed/watchable registrations must carry task/claim/host/wake/
    work_session bind fields. Incomplete bind returns a typed error payload.
    """
    if not rec or not rec.get("runner_session_id"):
        return None
    policy = wake.get("policy") or {}
    binding = (policy.get("account_binding") or {})
    execution = policy.get("execution_binding") or {}
    assignment = policy.get("assignment") or {}
    lifecycle = policy.get("lifecycle") or {}
    connect_assignment = (
        assignment.get("schema") == "switchboard.connect.assignment.v1")
    metadata = {
        "wake_id": wake.get("wake_id"),
        "wake_mode": rec.get("wake_mode"),
        "log_path": rec.get("log_path"),
        "command": rec.get("command"),
        "pty": bool(rec.get("pty")),
        "work_session_id": (
            (rec.get("metadata") or {}).get("work_session_id")
            or binding.get("work_session_id")
            or rec.get("work_session_id")
        ),
        "credential_lease_id": binding.get("credential_lease_id"),
        "provider": assignment.get("provider") or binding.get("provider"),
        "account_affinity_id": binding.get("account_affinity_id"),
        **({
            "connect_assignment": True,
            "assignment_schema": assignment.get("schema"),
            "assignment_id": assignment.get("assignment_id"),
            "principal_ref": assignment.get("principal_ref"),
            "work_ref": assignment.get("work_ref"),
            "workspace_ref": assignment.get("workspace_ref"),
            "execution_id": lifecycle.get("execution_id"),
            "execution_generation": lifecycle.get("generation"),
            "execution_role": lifecycle.get("role"),
            "execution_head_sha": lifecycle.get("head_sha"),
            "lease_epoch": lifecycle.get("fence_epoch"),
        } if connect_assignment else {
            "role": assignment.get("role") or lifecycle.get("role") or "implementation",
            "lifecycle_role": assignment.get("role") or lifecycle.get("role") or "implementation",
        }),
        **(rec.get("metadata") or {}),
        "source_sha": (execution.get("source_sha") or assignment.get("source_sha")
                       or lifecycle.get("source_sha")),
        "execution_connection_id": execution.get("execution_connection_id"),
    }
    host_preflight = _host_repo_preflight(rec, inventory, metadata)
    if host_preflight:
        metadata["host_repo_preflight"] = host_preflight
    # Prefer explicit host/<instance-id> from inventory; never invent task-row EC2 ids.
    host_id = inventory.get("host_id") or ""
    body = {
        "project": PROJECT,
        "runner_session_id": rec.get("runner_session_id"),
        "host_id": host_id,
        "agent_id": rec.get("agent_id") or (wake.get("selector") or {}).get("agent_id"),
        "runtime": rec.get("runtime") or (wake.get("selector") or {}).get("runtime"),
        "task_id": rec.get("task_id") or wake.get("task_id") or "",
        "claim_id": rec.get("claim_id") or binding.get("claim_id") or "",
        "pid": rec.get("pid"),
        "status": rec.get("status") or "running",
        "cwd": rec.get("cwd") or inventory.get("repo_root"),
        "control": rec.get("control") or {"tier": "T3", "runner_kill": True,
                                           "managed_process": True},
        "metadata": metadata,
        "heartbeat_ttl_s": (3600 if rec.get("cloud_session") else
                            180 if rec.get("wake_mode") == "direct_task" else 60),
    }
    # Use hard POST when this registration claims to be claim-bound / watchable so
    # agent hosts fail closed instead of silently skipping (_try returns None).
    require_bind = bool(
        body.get("claim_id")
        or metadata.get("credential_admission_phase") == "claim_bound"
        or rec.get("require_task_bind")
    )
    if require_bind:
        body["require_task_bind"] = True
        return _require("POST", P_REGISTER_RUNNER, body)
    if connect_assignment:
        return _require("POST", P_REGISTER_RUNNER, body)
    return _try("POST", P_REGISTER_RUNNER, body)


def _host_repo_preflight(rec, inventory, metadata=None):
    """Return a host-attested Git snapshot for a host-local Work Session.

    The coordinator cannot stat a Mac/AWS worker path.  The authenticated Agent
    Host already owns the runner heartbeat, so attach the supervisor's local Git
    snapshot to that same heartbeat and let the coordinator validate the binding.
    """
    rec = dict(rec or {})
    metadata = dict(metadata or rec.get("metadata") or {})
    runner_session_id = str(rec.get("runner_session_id") or "").strip()
    work_session_id = str(
        metadata.get("work_session_id") or rec.get("work_session_id") or "").strip()
    if not runner_session_id or not work_session_id:
        return None
    try:
        result = supervisor_action("snapshot", runner_session_id)
    except Exception:
        return None
    if not isinstance(result, dict) or result.get("error"):
        return None
    snap = dict(result.get("last_snapshot") or result.get("snapshot") or result)
    cwd = str(snap.get("cwd") or rec.get("cwd") or "").strip()
    branch = str(snap.get("branch") or "").strip()
    head_sha = str(snap.get("head_sha") or "").strip().lower()
    status_porcelain = str(snap.get("status_porcelain") or "")
    diff_check = str(snap.get("diff_check") or "")
    findings = []
    if not cwd or not branch or not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        findings.append({
            "code": "host_git_snapshot_incomplete",
            "message": "Agent Host could not resolve workspace, branch, and head SHA.",
            "failure_class": "missing_data", "severity": "high", "blocking": True,
        })
    if status_porcelain:
        findings.append({
            "code": "dirty_worktree",
            "message": "Agent Host reports uncommitted workspace changes.",
            "failure_class": "dirty_work_session", "severity": "high", "blocking": True,
        })
    if diff_check:
        findings.append({
            "code": "git_diff_check_failed",
            "message": "Agent Host git diff --check failed.",
            "failure_class": "conflict_markers", "severity": "high", "blocking": True,
        })
    blocking = any(item.get("blocking") for item in findings)
    return {
        "schema": "switchboard.repo_preflight.v1",
        "attestation_schema": "switchboard.agent_host_repo_preflight.v1",
        "source": "agent_host_attestation",
        "captured_at": float(snap.get("captured_at") or time.time()),
        "host_id": str((inventory or {}).get("host_id") or rec.get("host_id") or ""),
        "runner_session_id": runner_session_id,
        "work_session_id": work_session_id,
        "task_id": str(rec.get("task_id") or snap.get("task_id") or "").upper(),
        "agent_id": str(rec.get("agent_id") or snap.get("agent_id") or ""),
        "repo_path": cwd,
        "branch": branch,
        "head_sha": head_sha,
        "origin_url": str(snap.get("origin_url") or ""),
        "upstream": str(snap.get("upstream") or ""),
        "dirty": bool(status_porcelain),
        "conflict_marker_count": 1 if diff_check else 0,
        "findings": findings,
        "verdict": "deny" if blocking else "pass",
        "ok": not blocking,
    }


def report_cloud_usage(rec, wake):
    receipt = (rec or {}).get("usage_receipt") or {}
    if not receipt:
        return None
    return _try("POST", P_TALLY_SPEND, {
        "project": PROJECT,
        "source": receipt.get("source") or "agent_report",
        "confidence": receipt.get("confidence") or "unknown",
        "task_id": receipt.get("task_id") or wake.get("task_id"),
        "claim_id": rec.get("claim_id") or "",
        "agent_id": rec.get("agent_id") or (wake.get("selector") or {}).get("agent_id"),
        "runtime": "codex",
        "provider": "openai",
        "call_site": "cloud_execution",
        "total_tokens": 0,
        "cost_usd": 0,
        "status": "unknown",
        "request_id": f"codex-cloud:{receipt.get('provider_session_id')}",
        "metadata": receipt,
    })


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return False


def _tcp_port_open(host, port, timeout_s=0.5):
    import socket
    try:
        with socket.create_connection((str(host), int(port)), timeout=float(timeout_s)):
            return True
    except OSError:
        return False


# SIMPLIFY-9: one HostBridgeSession per live runner_session_id — the host
# tunnel WS. PTY I/O is owned by the executor (master_fd + file log); there is
# no LocalPtyRelayBridge / localhost /stream+/control hop on the Watch path.
_HOST_BRIDGES = {}
_HOST_BRIDGES_LOCK = threading.Lock()
# BUG-162: remember the applied host-tunnel ticket expiry so heartbeat mints
# (fresh JWT every ~10s tick) do not tear down a healthy live WebSocket.
_HOST_RELAY_APPLIED = {}
# Rotate when the *applied* ticket has this many seconds (or fewer) remaining.
# Server tickets are ttl=900; Agent Host ticks every ~10s, so 120s leaves many
# renewal chances without flashing Watch Detached on every heartbeat.
HOST_RELAY_ROTATE_SKEW_S = 120.0


def _host_relay_needs_rotation(expires_at, *, now=None, skew_s=None) -> bool:
    """True when a live host tunnel should accept a freshly minted host_url.

    Gate on the *currently applied* ticket's expiry, not the newly minted one
    (every heartbeat returns expires_at≈now+900). Missing/invalid expiry fails
    closed to rotate so a tunnel cannot go dark from a missing ledger entry.
    """
    try:
        exp = float(expires_at or 0)
    except (TypeError, ValueError):
        return True
    if exp <= 0:
        return True
    clock = time.time() if now is None else float(now)
    window = HOST_RELAY_ROTATE_SKEW_S if skew_s is None else float(skew_s)
    return (exp - clock) <= max(0.0, window)


def _publish_host_relay_url(runner_session_id, relay_ws_url) -> None:
    """Best-effort publish for the executor companion (host_relay.url)."""
    if not relay_ws_url:
        return
    try:
        from codex import supervisor as _sup
        relay_path = _sup._session_dir(runner_session_id) / "host_relay.url"
        relay_path.parent.mkdir(parents=True, exist_ok=True)
        relay_path.write_text(relay_ws_url, encoding="utf-8")
    except Exception:
        pass


def _record_applied_host_relay(runner_session_id, relay_ws_url, expires_at) -> None:
    sid = str(runner_session_id or "").strip()
    if not sid or not relay_ws_url:
        return
    try:
        exp = float(expires_at or 0)
    except (TypeError, ValueError):
        exp = 0.0
    # Server host tickets default to ttl=900. When a caller attaches without an
    # expires_at, assume a full lifetime so we do not immediately re-enter the
    # rotate path on every heartbeat (needs_rotation(0) is True by design for
    # *missing* ledger rows, not for a successfully applied URL).
    if exp <= 0:
        exp = time.time() + 900.0
    _HOST_RELAY_APPLIED[sid] = {"url": str(relay_ws_url), "expires_at": exp}


def _drop_host_bridge(runner_session_id):
    sid = str(runner_session_id or "").strip()
    with _HOST_BRIDGES_LOCK:
        session = _HOST_BRIDGES.pop(sid, None)
        _HOST_RELAY_APPLIED.pop(sid, None)
    if session is not None:
        try:
            session.stop()
        except Exception:
            pass


def _ensure_host_bridge(*, runner_session_id, host_id, binding, public_base,
                         host_relay_url="", master_fd=None, child_pid=0,
                         log_path="", expires_at=None, force_rotate=False):
    """Idempotently ensure a live host tunnel for this session.

    Starting *is* opening: dial /pty/host immediately. Optional master_fd makes
    this process the executor (PTY I/O + stdout.log). Re-entrant across
    poll-loop iterations: a healthy existing bridge is a no-op; a dead one is
    replaced. No localhost stream/control URLs are required.

    BUG-162: a freshly minted ``host_relay_url`` only rotates an already-live
    tunnel when the *applied* ticket is within ``HOST_RELAY_ROTATE_SKEW_S`` of
    expiry (or unknown). Mid-lifetime heartbeat mints must not close the WS.
    Pass ``force_rotate=True`` for an explicit companion refresh request.
    """
    relay_ws_url = str(host_relay_url or "").strip()
    sid = str(runner_session_id or "").strip()
    with _HOST_BRIDGES_LOCK:
        existing = _HOST_BRIDGES.get(sid)
        if existing is not None and existing.is_alive():
            if relay_ws_url:
                applied = _HOST_RELAY_APPLIED.get(sid) or {}
                if force_rotate or _host_relay_needs_rotation(applied.get("expires_at")):
                    # Ticket renewal must reach an already-running executor.
                    # The companion watches host_relay.url and reconnects; an
                    # in-process bridge rotates directly via update_relay_url.
                    _publish_host_relay_url(sid, relay_ws_url)
                    try:
                        existing.update_relay_url(relay_ws_url)
                    except Exception:
                        pass
                    _record_applied_host_relay(sid, relay_ws_url, expires_at)
            return existing
    if existing is not None:
        _drop_host_bridge(sid)

    try:
        from switchboard.application import runner_pty_relay as pty_relay
        from codex.pty_host_ws_client import open_host_bridge
    except ModuleNotFoundError:
        _root = os.path.abspath(os.path.join(_HERE, ".."))
        if os.path.join(_root, "src") not in sys.path:
            sys.path.insert(0, os.path.join(_root, "src"))
        if _HERE not in sys.path:
            sys.path.insert(0, _HERE)
        from switchboard.application import runner_pty_relay as pty_relay
        from codex.pty_host_ws_client import open_host_bridge

    minted_expires_at = expires_at
    if not relay_ws_url:
        # Legacy/in-process compatibility. Real enrolled hosts receive a
        # server-minted one-session URL in the claimed control request because
        # they must never possess the server relay signing secret.
        host_ticket, host_payload = pty_relay.mint_host_tunnel_ticket(
            binding, ttl_seconds=3600)
        relay_ws_url = pty_relay.public_host_relay_url(
            public_base, runner_session_id, host_ticket)
        relay_ws_url = relay_ws_url + "&" + urllib.parse.urlencode({"host_id": host_id})
        if minted_expires_at is None:
            minted_expires_at = host_payload.get("exp")

    # Publish the host relay URL for the executor companion (same Mac/AWS binary)
    # so it can dial without a localhost HTTP hop. The companion owns master_fd
    # and is the single outbound WS speaker when master_fd is not in-process.
    _publish_host_relay_url(sid, relay_ws_url)

    session = open_host_bridge(
        runner_session_id=runner_session_id,
        relay_ws_url=relay_ws_url,
        master_fd=master_fd,
        child_pid=int(child_pid or 0),
        log_path=str(log_path or ""),
        on_close=lambda reason: _drop_host_bridge(runner_session_id),
    )
    with _HOST_BRIDGES_LOCK:
        _HOST_BRIDGES[sid] = session
        _record_applied_host_relay(sid, relay_ws_url, minted_expires_at)
    return session


def supervisor_action(action, runner_session_id, options=None):
    options = options or {}
    if action == "snapshot":
        cmd = [sys.executable, SUPERVISOR, "snapshot", runner_session_id]
    elif action == "health":
        cmd = [sys.executable, SUPERVISOR, "status", runner_session_id]
    elif action == "logs":
        cmd = [sys.executable, SUPERVISOR, "snapshot", runner_session_id]
    elif action == "kill":
        cmd = [sys.executable, SUPERVISOR, "kill", runner_session_id,
               "--grace-seconds", str(options.get("grace_seconds") or 5.0),
               "--signal", options.get("signal") or "TERM"]
    elif action == "open":
        status_cmd = [sys.executable, SUPERVISOR, "status", runner_session_id]
        try:
            out = subprocess.run(status_cmd, capture_output=True, text=True, timeout=15)
            if out.returncode != 0:
                return {"error": "supervisor_failed", "stderr": (out.stderr or "")[-4000:]}
            meta = json.loads(out.stdout or "{}")
        except Exception as e:
            return {"error": type(e).__name__, "message": str(e)}
        control = meta.get("control") or {}
        public_base = str(
            os.environ.get("PM_RUNNER_PTY_RELAY_PUBLIC_BASE")
            or os.environ.get("PM_SWITCHBOARD_PUBLIC_BASE")
            or ""
        ).rstrip("/")
        # SIMPLIFY-9/WATCH-11: starting IS opening. Watch attaches only through
        # the Switchboard relay; the retired host-local HTTP transport is not a
        # readiness or fallback path.
        pty_alive = bool(meta.get("pty") and control.get("runner_open") and meta.get("alive"))
        if not pty_alive:
            return {
                "error": "not_supported",
                "reason": "runner_open requires a live PTY-backed local session",
            }
        host_id = str(meta.get("host_id") or os.environ.get("PM_HOST_ID") or "")
        relay_url = ""
        ticket = None
        expires_at = 0.0

        def _open_fail_closed(error, reason):
            # The relay is the only browser transport; relay failures fail closed.
            return {
                "error": error,
                "reason": reason,
                "failure_class": "hidden_fallback",
                "opened": False,
                "runner_session_id": runner_session_id,
                "transport": None,
                "browser_safe": False,
                "relay_required": True,
                "capabilities": {"stream": "denied", "open": "denied"},
            }

        if public_base:
            try:
                from switchboard.application import runner_pty_relay as pty_relay
                from switchboard.domain import runner_pty as pty_domain
            except ModuleNotFoundError:
                _root = os.path.abspath(os.path.join(_HERE, ".."))
                if _root not in sys.path:
                    sys.path.insert(0, os.path.join(_root, "src"))
                from switchboard.application import runner_pty_relay as pty_relay
                from switchboard.domain import runner_pty as pty_domain
            if pty_relay.is_loopback_url(public_base):
                return {
                    "error": "not_supported",
                    "reason": "runner_open requires a non-loopback relay public base",
                }
            else:
                binding = {
                    "tenant_id": str(options.get("tenant_id") or meta.get("tenant_id") or "tenant/default"),
                    "user_id": str(options.get("user_id") or meta.get("user_id") or "operator"),
                    "project_id": str(options.get("project_id") or options.get("project")
                                      or os.environ.get("PM_PROJECT") or "switchboard"),
                    "task_id": str(options.get("task_id") or meta.get("task_id") or "unbound"),
                    "claim_id": str(options.get("claim_id") or meta.get("claim_id") or "unbound"),
                    "work_session_id": str(
                        options.get("work_session_id")
                        or (meta.get("metadata") or {}).get("work_session_id")
                        or meta.get("work_session_id")
                        or "unbound"),
                    "runner_session_id": runner_session_id,
                    "host_id": host_id or "host/unknown",
                    "wake_id": str(
                        options.get("wake_id")
                        or (meta.get("metadata") or {}).get("wake_id")
                        or meta.get("wake_id")
                        or "unbound"),
                    "execution_connection_id": str(
                        options.get("execution_connection_id")
                        or meta.get("execution_connection_id")
                        or "execconn/unspecified"),
                    "source_sha": str(options.get("source_sha") or meta.get("source_sha") or "unknown"),
                    "permission_profile": str(
                        options.get("permission_profile") or "operator_watch"),
                }
                server_relay = _fresh_server_relay(
                    options.get("server_relay"), runner_session_id, host_id)
                host_relay_url = str(server_relay.get("host_url") or "")
                browser_relay_url = str(server_relay.get("browser_url") or "")
                if isinstance(server_relay.get("binding"), dict):
                    binding = dict(server_relay["binding"])
                try:
                    if host_relay_url and browser_relay_url:
                        relay_url = browser_relay_url
                        expires_at = float(server_relay.get("expires_at") or expires_at)
                        ticket = None
                    else:
                        if server_relay.get("error"):
                            raise RuntimeError(str(server_relay.get("error")))
                        scopes = options.get("scopes") or [
                            "watch", "input", "resize", "signal"]
                        relay_ticket, relay_payload = pty_relay.mint_capability_ticket(
                            binding, scopes,
                            ttl_seconds=int(options.get("ttl_seconds") or 900))
                        relay_url = pty_relay.public_relay_url(
                            public_base, runner_session_id, relay_ticket)
                        expires_at = float(relay_payload.get("exp") or expires_at)
                        ticket = relay_ticket
                    transport = pty_domain.TRANSPORT_SWITCHBOARD_PTY_RELAY
                except Exception as mint_exc:
                    return _open_fail_closed(
                        "relay_mint_failed", str(mint_exc) or type(mint_exc).__name__)
                try:
                    # SIMPLIFY-9: dial host tunnel immediately (no localhost stream).
                    # Hub buffers until the executor owning master_fd attaches.
                    _ensure_host_bridge(
                        runner_session_id=runner_session_id,
                        host_id=host_id,
                        binding=binding,
                        public_base=public_base,
                        host_relay_url=host_relay_url,
                        log_path=str(meta.get("log_path") or ""),
                        child_pid=int(meta.get("pid") or 0),
                        expires_at=server_relay.get("expires_at") or expires_at,
                    )
                except Exception as bridge_exc:
                    return _open_fail_closed(
                        "host_bridge_failed", str(bridge_exc) or type(bridge_exc).__name__)
        else:
            return {
                "error": "not_supported",
                "reason": "runner_open requires a non-loopback relay public base",
            }
        metadata = {
            "pty": True,
            "stream_url": relay_url,
            "stream_ticket_exp": expires_at,
            "transport": transport,
            "browser_safe": True,
            "relay_required": False,
        }
        if relay_url:
            metadata["relay_url"] = relay_url
            try:
                from switchboard.application.runner_pty_relay import (
                    sanitize_browser_stream_metadata,
                )
            except ModuleNotFoundError:
                sanitize_browser_stream_metadata = None
            if sanitize_browser_stream_metadata is not None:
                metadata = sanitize_browser_stream_metadata(
                    metadata, relay_url=relay_url)
        return {
            "opened": True,
            "runner_session_id": runner_session_id,
            "transport": transport,
            "stream_url": relay_url,
            "relay_url": relay_url or None,
            "ticket": ticket,
            "expires_at": expires_at,
            "browser_safe": True,
            "relay_required": False,
            "capabilities": {"stream": "supported", "open": "supported"},
            "metadata": metadata,
        }
    elif action == "inject":
        return {
            "error": "not_supported",
            "reason": "runner input is delivered through the Switchboard PTY relay",
            "runner_session_id": runner_session_id,
            "capabilities": {"inject": "denied", "relay_input": "supported"},
        }
    else:
        return {"error": f"unsupported runner action {action}"}
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            return {"error": "supervisor_failed", "stderr": out.stderr[-4000:]}
        data = json.loads(out.stdout or "{}")
        if action == "health":
            started = data.get("started_at")
            data["health"] = {
                "status": data.get("status") or "unknown",
                "alive": bool(data.get("alive")),
                "uptime_seconds": max(0.0, time.time() - float(started)) if started else None,
            }
        elif action == "logs":
            snap = data.get("last_snapshot") or {}
            data["logs"] = {"log_tail": snap.get("log_tail") or "", "log_path": data.get("log_path")}
        return data
    except Exception as e:
        return {"error": type(e).__name__, "message": str(e)}


def handle_runner_controls(inventory):
    """Consume pending snapshot/kill requests for runner sessions hosted here."""
    host_id = inventory["host_id"]
    listed = _try(
        "GET",
        f"{P_LIST_RUNNER_CONTROLS}?project={PROJECT}&status=pending&host_id={host_id}",
    ) or {}
    requests = listed.get("requests") or []
    handled = []
    for req in requests:
        req_id = req.get("request_id")
        claimed = _try("POST", P_CLAIM_RUNNER_CONTROL,
                       {"project": PROJECT, "host_id": host_id, "request_id": req_id})
        if not claimed or not claimed.get("claimed"):
            continue
        req = claimed.get("request") or req
        action = req.get("action")
        result = supervisor_action(action, req.get("runner_session_id"), req.get("options") or {})
        snapshot = result.get("last_snapshot") or result.get("snapshot") or {}
        if action == "health" and not snapshot:
            snapshot = {"captured_at": time.time(), "source": "supervisor_status",
                        "status": result.get("status"), "alive": result.get("alive"),
                        "health": result.get("health") or {}}
        if action == "snapshot" and not snapshot:
            snapshot = result
        if action == "logs" and not snapshot:
            snapshot = {"captured_at": time.time(), "source": "supervisor_logs",
                        "log_tail": (result.get("logs") or {}).get("log_tail") or "",
                        "log_path": (result.get("logs") or {}).get("log_path")}
        if action == "open" and result.get("opened"):
            open_meta = dict(result.get("metadata") or {})
            # Browser-facing registration must never publish loopback stream URLs.
            try:
                from switchboard.application.runner_pty_relay import (
                    sanitize_browser_stream_metadata,
                )
            except ModuleNotFoundError:
                sanitize_browser_stream_metadata = lambda meta, relay_url="": dict(meta or {})  # noqa: E731
            browser_meta = sanitize_browser_stream_metadata(
                {
                    "stream_url": result.get("stream_url"),
                    "relay_url": result.get("relay_url") or open_meta.get("relay_url"),
                    "stream_ticket_exp": result.get("expires_at"),
                    "transport": result.get("transport"),
                    "browser_safe": result.get("browser_safe"),
                    "relay_required": result.get("relay_required"),
                    "pty": True,
                },
                relay_url=str(result.get("relay_url") or open_meta.get("relay_url") or ""),
            )
            safe_stream_url = browser_meta.get("stream_url")
            snapshot = {
                "captured_at": time.time(),
                "source": "runner_open",
                "stream_url": safe_stream_url,
                "transport": result.get("transport"),
                "expires_at": result.get("expires_at"),
                "browser_safe": result.get("browser_safe"),
                "relay_required": result.get("relay_required"),
                "pty": True,
            }
            # Advertise stream coordinates on the central runner_session metadata.
            _try("POST", P_REGISTER_RUNNER, {
                "project": PROJECT,
                "runner_session_id": req.get("runner_session_id"),
                "host_id": host_id,
                "status": "running",
                "control": {"tier": "T3", "runner_kill": True, "managed_process": True,
                            "runner_open": True, "runner_inject": True, "runner_logs": True},
                "metadata": browser_meta,
                "heartbeat_ttl_s": 60,
            })
        if action == "inject" and result.get("injected"):
            snapshot = {
                "captured_at": time.time(),
                "source": "runner_inject",
                "runner_session_id": result.get("runner_session_id"),
                "task_id": result.get("task_id"),
                "kind": result.get("kind"),
                "bytes_written": result.get("bytes_written"),
            }
        status = "failed" if result.get("error") else "completed"
        if action == "kill" and status == "completed":
            # UI-24: deterministic cleanup — no orphan host tunnel outliving
            # the runner it was pumping bytes for.
            _drop_host_bridge(req.get("runner_session_id"))
        _try("POST", P_COMPLETE_RUNNER_CONTROL,
             {"project": PROJECT, "host_id": host_id, "request_id": req_id,
              "status": status, "result": result, "snapshot": snapshot})
        handled.append({"request_id": req_id, "action": action, "status": status,
                        "runner_session_id": req.get("runner_session_id")})
    return handled


def _drain_query(path, **query):
    return f"{path}?{urllib.parse.urlencode({'project': PROJECT, **query})}"


def _drain_runners(host_id, recover_stale_local=True):
    """Join supervisor truth to only the central rows this tick can act on.

    A long-lived personal host can accumulate thousands of stale historical
    runner rows.  Downloading all of them before renewing a handful of live
    local PTYs makes the heartbeat itself miss its lease.  Recovery therefore
    asks for stale rows only for task ids that the local supervisor says are
    alive.  The graceful-drain caller opts out and fetches only centrally-live
    rows for the host.
    """
    try:
        out = subprocess.run(
            [sys.executable, SUPERVISOR, "list"],
            capture_output=True, text=True, timeout=10)
        local = (json.loads(out.stdout or "{}").get("sessions") or []) \
            if out.returncode == 0 else []
    except Exception:
        local = []
    sessions = []
    if recover_stale_local:
        live_task_ids = sorted({
            str(row.get("task_id") or "") for row in local
            if row.get("alive") is True and str(row.get("task_id") or "")
        })
        for task_id in live_task_ids:
            result = _try("GET", _drain_query(
                P_LIST_RUNNERS, host_id=host_id, task_id=task_id,
                include_stale="true")) or {}
            rows = result.get("sessions") or result.get("runner_sessions") or []
            if isinstance(rows, list):
                sessions.extend(rows)
    else:
        result = _try("GET", _drain_query(
            P_LIST_RUNNERS, host_id=host_id, include_stale="false")) or {}
        rows = result.get("sessions") or result.get("runner_sessions") or []
        sessions = rows if isinstance(rows, list) else []
    local_by_id = {row.get("runner_session_id"): dict(row) for row in local
                   if row.get("runner_session_id")}
    merged = dict(local_by_id)
    for row in sessions:
        runner_id = row.get("runner_session_id")
        if runner_id:
            local_row = local_by_id.get(runner_id, {})
            combined = {**local_row, **dict(row)}
            if local_row.get("alive") is True:
                # Central identity/claim state is authoritative, but only the
                # local supervisor can report the live PTY transport.  Repair
                # an older preclaim placeholder on every daemon tick.
                for key in ("pty", "streamer_pid", "log_path", "pid", "alive"):
                    if local_row.get(key) not in (None, ""):
                        combined[key] = local_row.get(key)
                combined["metadata"] = {
                    **dict(row.get("metadata") or {}),
                    **{key: local_row.get(key) for key in
                       ("pty",)
                       if local_row.get(key) not in (None, "")},
                }
                combined["control"] = {
                    **dict(row.get("control") or {}),
                    **dict(local_row.get("control") or {}),
                }
            merged[runner_id] = combined
    return list(merged.values())


# SIMPLIFY-18: the host shares the server's one terminal vocabulary. The
# release bundle ships src/, so there is no second spelling to drift.
from switchboard.domain.execution_liveness import (
    TERMINAL_EXECUTION_STATES as _TERMINAL_RUNNER_STATES)


def _positive_seconds(env_name, default):
    try:
        return max(0.0, float(os.environ.get(env_name, str(default)) or default))
    except (TypeError, ValueError):
        return float(default)


def _runner_last_output_at(session):
    """Return durable local PTY activity without reading or exposing its log."""
    metadata = dict(session.get("metadata") or {})
    log_path = str(session.get("log_path") or metadata.get("log_path") or "").strip()
    if log_path:
        try:
            return float(os.stat(log_path).st_mtime)
        except OSError:
            pass
    return _runner_timestamp(session.get("started_at"))


def _runner_timestamp(value):
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def expire_runner_leases(inventory, *, now=None):
    """Enforce the single process-stop clock: the renewable runner lease."""
    now = time.time() if now is None else float(now)
    host_id = str((inventory or {}).get("host_id") or "")
    outcomes = _drain_pending_stop_receipts(host_id)
    for session in _drain_runners(host_id):
        if session.get("alive") is not True or not session.get("stale"):
            continue
        runner_id = str(session.get("runner_session_id") or "")
        task_id = str(session.get("task_id") or "")
        outcome = {"runner_session_id": runner_id, "task_id": task_id,
                   "reason": "runner_lease_expired"}
        stopped = supervisor_action("kill", runner_id, {
            "reason": "runner heartbeat lease expired", "task_id": task_id})
        ok = bool(stopped and not stopped.get("error") and stopped.get("alive") is not True)
        if ok:
            _drop_host_bridge(runner_id)
            metadata = dict(session.get("metadata") or {})
            receipt = {
                "project": PROJECT, "runner_session_id": runner_id,
                "host_id": host_id, "task_id": task_id,
                "claim_id": session.get("claim_id") or "",
                "agent_id": session.get("agent_id") or f"codex/{task_id}",
                "status": "expired",
                "metadata": {**metadata, "terminalized_by": "runner_lease_expiry",
                             "lease_expired_at": now,
                             "failure_reason": "runner heartbeat lease expired"},
            }
            # The process death and central acknowledgement are separate durable
            # steps.  Persist first so a network loss or daemon restart cannot
            # strand a successfully killed execution in Stopping forever.
            _persist_pending_stop_receipt(receipt)
            terminal = _try("POST", P_HEARTBEAT_RUNNER, receipt)
            if terminal and not terminal.get("error"):
                _delete_pending_stop_receipt(runner_id)
            outcome["expired"] = bool(terminal and not terminal.get("error"))
        else:
            outcome["expired"] = False
            outcome["error"] = (stopped or {}).get("error")
        outcomes.append(outcome)
    return outcomes


def _pending_stop_receipt_dir():
    root = Path(os.environ.get("PM_RUNNER_DIR", ".switchboard/runner")).resolve()
    path = root / "_pending_stops"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _pending_stop_receipt_path(runner_session_id):
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(runner_session_id or ""))
    return _pending_stop_receipt_dir() / f"{safe_id}.json"


def _persist_pending_stop_receipt(receipt):
    path = _pending_stop_receipt_path(receipt.get("runner_session_id"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(receipt, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _delete_pending_stop_receipt(runner_session_id):
    try:
        _pending_stop_receipt_path(runner_session_id).unlink()
    except FileNotFoundError:
        pass


def _drain_pending_stop_receipts(host_id):
    """Retry exact terminal acknowledgements even after local process removal."""
    outcomes = []
    for path in sorted(_pending_stop_receipt_dir().glob("*.json")):
        try:
            receipt = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(receipt.get("host_id") or "") != str(host_id or ""):
            continue
        terminal = _try("POST", P_HEARTBEAT_RUNNER, receipt)
        ok = bool(terminal and not terminal.get("error"))
        if ok:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        outcomes.append({
            "runner_session_id": receipt.get("runner_session_id"),
            "task_id": receipt.get("task_id"),
            "reason": "pending_terminal_ack_retry",
            "expired": ok,
            "error": None if ok else (terminal or {}).get("error"),
        })
    return outcomes


def converge_terminal_task_runners(inventory, heartbeat):
    """Refuse legacy terminal-task kill directives for a live process.

    Kept temporarily as a compatibility boundary while old servers drain.
    Only an already-exited runner may be acknowledged; lease expiry owns kills.
    """
    host_id = str((inventory or {}).get("host_id") or "")
    cleanup = (heartbeat or {}).get("terminal_runner_cleanup") or {}
    directives = cleanup.get("sessions") or []
    outcomes = []
    for directive in directives if isinstance(directives, list) else []:
        runner_session_id = str(directive.get("runner_session_id") or "")
        task_id = str(directive.get("task_id") or "")
        if not runner_session_id or not task_id:
            continue
        health = supervisor_action("health", runner_session_id)
        alive = bool(health and not health.get("error") and health.get("alive"))
        killed = ({"status": "observed_only", "alive": alive,
                   "error": "lease expiry is the only kill authority"}
                  if alive else {"status": "already_exited", "alive": False})
        kill_ok = not killed.get("error") and killed.get("alive") is not True
        if kill_ok:
            _drop_host_bridge(runner_session_id)
        terminal = None
        if kill_ok:
            terminal_status = (
                "completed" if directive.get("task_status") == "Done" else "cancelled"
            )
            terminal = _try("POST", P_HEARTBEAT_RUNNER, {
                "project": PROJECT,
                "runner_session_id": runner_session_id,
                "host_id": host_id,
                "task_id": task_id,
                "status": terminal_status,
                "metadata": {
                    "terminalized_by": "terminal_task",
                    "terminal_task_status": directive.get("task_status"),
                    "terminal_cleanup_reason": directive.get("reason"),
                },
            })
        outcomes.append({
            "runner_session_id": runner_session_id,
            "task_id": task_id,
            "task_status": directive.get("task_status"),
            "killed": kill_ok,
            "terminalized": bool(terminal and not terminal.get("error")),
            "error": killed.get("error") or (
                (terminal or {}).get("error") if isinstance(terminal, dict) else None),
        })
    return outcomes


def renew_live_direct_runners(inventory):
    """Keep browser Watch/Chat bound to every live Mac Codex PTY.

    Direct-task wakes are acknowledged immediately after launch, so they leave
    the pending-wake feed while the native CLI continues working.  The launch
    registration has a deliberately short lease; without this host heartbeat a
    close/reopen of Watch loses the centrally discoverable row even though the
    supervisor-owned process and PTY are still alive.

    Claim-bound Autopilot sessions need the same renewal.  The worker heartbeat
    owns claim/Work Session state but cannot see the outer supervisor's PTY, so
    this host heartbeat continuously joins both halves. ``_drain_runners`` also
    repairs sessions whose central preclaim placeholders hid a live local PTY.
    """
    host_id = str((inventory or {}).get("host_id") or "")
    renewed = []
    sessions = _drain_runners(host_id)
    needs_late_binding = any(
        str((row.get("metadata") or {}).get("credential_admission_phase") or "").lower()
        in {"preclaim", "pending"}
        and row.get("alive") is True
        and str(row.get("status") or "").lower() == "running"
        for row in sessions
    )
    work_sessions = _drain_work_sessions() if needs_late_binding else []
    for session in sessions:
        metadata = dict(session.get("metadata") or {})
        native_transport = metadata.get("native_host_execution") is True
        admission_preclaim = str(
            metadata.get("credential_admission_phase") or "").lower() in {
                "preclaim", "pending"}
        claim_id = str(session.get("claim_id") or "")
        work_session_id = str(metadata.get("work_session_id") or "")
        late_binding = _direct_work_session_binding(session, work_sessions)
        if (not late_binding and needs_late_binding
                and admission_preclaim
                and not session.get("claim_id")
                and not metadata.get("work_session_id")):
            # A short task can create and complete its managed Work Session
            # between two host ticks.  Query only this task's completed rows so
            # the exact direct-session principal can still close the binding
            # race without scanning historical Work Sessions fleet-wide.
            completed = _drain_work_sessions(
                task_id=str(session.get("task_id") or ""),
                status="completed",
            )
            late_binding = _direct_work_session_binding(
                session, completed, allowed_statuses={"completed"})
        if late_binding:
            claim_id = str(late_binding.get("claim_id") or "")
            work_session_id = str(late_binding.get("work_session_id") or "")
            metadata.update({
                "work_session_id": work_session_id,
                "credential_admission_phase": "claim_bound",
                "late_bound_by": "agent_host_work_session_join",
            })
        claim_bound = bool(claim_id and work_session_id)
        wake_id = str(metadata.get("wake_id") or session.get("wake_id") or "")
        task_id = str(session.get("task_id") or "")
        # BUG-91: an exited process must go terminal NOW, not drift to stale and
        # then expired. A row that merely stops being renewed still looks like a
        # live session for a whole lease, and stays the newest thing the browser
        # can find for the task long after that. The supervisor's `alive` is the
        # only local truth about the process, so report it the moment it flips.
        if (session.get("alive") is False and task_id
                and str(session.get("status") or "").lower() not in _TERMINAL_RUNNER_STATES):
            reason = str(
                metadata.get("failure_reason")
                or "supervisor reported the process exited"
            ).strip()
            receipt = {
                "project": PROJECT,
                "runner_session_id": session.get("runner_session_id"),
                "host_id": host_id,
                "task_id": task_id,
                "status": "exited",
                "metadata": {**metadata,
                             "failure_reason": reason,
                             "terminalized_by": "host_supervisor"},
            }
            _persist_pending_stop_receipt(receipt)
            terminal = _try("POST", P_HEARTBEAT_RUNNER, receipt)
            if terminal and not terminal.get("error"):
                _delete_pending_stop_receipt(session.get("runner_session_id"))
            wake_repaired = False
            # SIMPLIFY-3 / BUG-102: same tick — if a wake is bound, force
            # complete_wake(started=false) so claimed limbo cannot outlive the
            # local death. Already-terminal rows stay skipped (BUG-91).
            if wake_id and terminal and not terminal.get("error"):
                completion = _try("POST", P_COMPLETE_WAKE, {
                    "project": PROJECT,
                    "wake_id": wake_id,
                    "runner_session_id": session.get("runner_session_id") or "",
                    "agent_id": session.get("agent_id") or f"codex/{task_id}",
                    "result": {
                        "started": False,
                        "reason": reason,
                        "error": reason,
                        "failure_class": "launch_failed",
                        "runner_session_id": session.get("runner_session_id"),
                        "host_id": host_id,
                        "task_id": task_id,
                    },
                })
                wake_repaired = bool(
                    completion and not completion.get("error")
                    and not completion.get("error_code")
                )
            renewed.append({
                "runner_session_id": session.get("runner_session_id"),
                "task_id": task_id,
                "wake_id": wake_id or None,
                "terminalized": bool(terminal and not terminal.get("error")),
                "wake_repaired": wake_repaired,
            })
            continue
        if (not native_transport or session.get("alive") is not True
                or str(session.get("status") or "").lower() != "running"):
            continue
        if not wake_id or not task_id:
            continue
        body = {
            "project": PROJECT,
            "runner_session_id": session.get("runner_session_id"),
            "host_id": host_id,
            "agent_id": session.get("agent_id") or f"codex/{task_id}",
            "runtime": session.get("runtime") or "codex",
            "task_id": task_id,
            "claim_id": claim_id if claim_bound else "",
            "pid": session.get("pid"),
            "status": "running",
            "cwd": session.get("cwd") or inventory.get("repo_root"),
            "control": session.get("control") or {
                "tier": "T3", "runner_kill": True, "managed_process": True,
                "runner_open": True, "runner_inject": True, "runner_logs": True,
            },
            "metadata": {
                **metadata,
                "wake_id": wake_id,
                "wake_mode": (session.get("wake_mode") or
                              "claim_next"),
                **({
                    "direct_assignment": True,
                    "assignment_schema": "switchboard.direct_cli_assignment.v1",
                } if metadata.get("direct_assignment") is True else {}),
            },
            # Busy hosts may spend longer than one nominal tick finalizing other
            # work. A three-minute lease prevents a healthy direct PTY from
            # flickering out of Watch between successful renewals.
            "heartbeat_ttl_s": 180,
        }
        host_preflight = _host_repo_preflight(
            session, inventory, body["metadata"])
        if host_preflight:
            body["metadata"]["host_repo_preflight"] = host_preflight
        result = _try("POST", P_HEARTBEAT_RUNNER, body)
        requested_relay = _consume_host_relay_refresh_request(
            session.get("runner_session_id"), host_id)
        server_relay = requested_relay or _fresh_server_relay((
            (result or {}).get("server_relay")
            if isinstance(result, dict) else None
        ), session.get("runner_session_id"), host_id)
        if server_relay.get("host_url"):
            try:
                _ensure_host_bridge(
                    runner_session_id=str(session.get("runner_session_id") or ""),
                    host_id=host_id,
                    binding=dict(server_relay.get("binding") or {}),
                    public_base="",
                    host_relay_url=str(server_relay.get("host_url") or ""),
                    child_pid=int(session.get("pid") or 0),
                    log_path=str(session.get("log_path") or metadata.get("log_path") or ""),
                    expires_at=server_relay.get("expires_at"),
                    force_rotate=bool(requested_relay and requested_relay.get("host_url")),
                )
            except Exception as exc:
                if isinstance(result, dict):
                    result["host_relay_error"] = type(exc).__name__
        renewed.append({
            "runner_session_id": session.get("runner_session_id"),
            "task_id": task_id,
            "renewed": bool(result and not result.get("error")),
            "error": (result or {}).get("error") if isinstance(result, dict) else None,
            "relay_url_minted": bool(server_relay.get("host_url")),
            **({
                "server_relay_error": server_relay.get("error"),
                "server_relay_missing": list(server_relay.get("missing") or []),
            } if not server_relay.get("host_url") else {}),
        })
    return renewed


def _drain_work_sessions(*, task_id="", status="active"):
    result = _try("GET", _drain_query(
        P_LIST_WORK_SESSIONS, status=status, task_id=task_id,
        include_expired="true")) or {}
    sessions = result.get("work_sessions") or []
    return sessions if isinstance(sessions, list) else []


def _direct_work_session_binding(session, work_sessions, *, allowed_statuses=None):
    """Find the one Work Session created by this exact direct Codex process.

    Direct sessions intentionally launch before a claim exists.  The direct MCP
    token later creates the claim and managed Work Session under a principal
    derived from the runner id.  Join those two phases here; task/agent matching
    alone is insufficient because retries may exist for the same task.
    """
    session = dict(session or {})
    metadata = dict(session.get("metadata") or {})
    runner_session_id = str(session.get("runner_session_id") or "").strip()
    if (not runner_session_id
            or not (metadata.get("direct_assignment") is True
                    or metadata.get("connect_assignment") is True)
            or session.get("claim_id")
            or metadata.get("work_session_id")):
        return None
    expected_principal = f"direct-session/{runner_session_id}"
    task_id = str(session.get("task_id") or "").upper()
    agent_id = str(session.get("agent_id") or "")
    allowed = {str(value).lower() for value in (allowed_statuses or {"active"})}
    matches = []
    for candidate in work_sessions or []:
        if (str(candidate.get("status") or "").lower() not in allowed
                or str(candidate.get("principal_id") or "") != expected_principal
                or str(candidate.get("task_id") or "").upper() != task_id
                or str(candidate.get("agent_id") or "") != agent_id
                or not str(candidate.get("claim_id") or "").strip()
                or not str(candidate.get("work_session_id") or "").strip()):
            continue
        matches.append(candidate)
    return dict(matches[0]) if len(matches) == 1 else None


def _release_provider_lease(lease_id, reason):
    return _try(
        "POST",
        f"/api/projects/{urllib.parse.quote(PROJECT, safe='')}/"
        f"provider-credential-leases/{urllib.parse.quote(lease_id, safe='')}/release",
        {"project": PROJECT, "reason": reason},
    ) or {"state": "release_failed"}


def _runner_session_id_for_wake(wake, host_id):
    try:
        from switchboard.domain.runner_pty import planned_runner_session_id
    except ModuleNotFoundError:
        _root = os.path.abspath(os.path.join(_HERE, ".."))
        if os.path.join(_root, "src") not in sys.path:
            sys.path.insert(0, os.path.join(_root, "src"))
        from switchboard.domain.runner_pty import planned_runner_session_id
    return planned_runner_session_id(wake.get("wake_id"), host_id)


def _bound_finalizer_key(wake, inventory, runner_session_id):
    return (f"{inventory.get('host_id')}:{wake.get('wake_id')}:"
            f"{runner_session_id}")


def _reuse_inflight_bound_runner(wake, inventory, runner_session_id,
                                  preclaim_registration=None):
    """Return a pending receipt when this exact local boot already exists.

    A claimed wake may be requeued if the central host heartbeat briefly expires
    while a slow local fetch/worktree is still running.  The deterministic runner
    id then leads the next host tick back to the same supervised process.  Reclaim
    the wake, but never call ``supervisor start`` a second time: doing so rejects
    the duplicate id and incorrectly terminalizes the wake that the first process
    still owns.

    The in-memory finalizer is authoritative within one daemon lifetime.  The
    supervisor record also lets a restarted daemon reattach to a surviving local
    process.
    """
    key = _bound_finalizer_key(wake, inventory, runner_session_id)
    with _BOUND_FINALIZERS_LOCK:
        finalizer_active = key in _BOUND_FINALIZERS
    health = supervisor_action("health", runner_session_id)
    local_alive = bool(
        health and not health.get("error") and health.get("alive"))
    if not finalizer_active and not local_alive:
        return None
    rec = dict(health or {}) if local_alive else {}
    if not finalizer_active:
        _submit_bound_finalizer(wake, inventory, runner_session_id, rec)
    return {
        "wake_id": wake.get("wake_id"),
        "started": True,
        "runner_session_id": runner_session_id,
        "wake_mode": rec.get("wake_mode") or wake_mode(wake, inventory),
        "reason": "runner_binding_pending_reused",
        "pid": rec.get("pid"),
        "cwd": rec.get("cwd") or inventory.get("repo_root"),
        "task_id": rec.get("task_id") or wake.get("task_id"),
        "claim_id": None,
        "work_session_id": None,
        "control": rec.get("control") or {},
        "session_url": rec.get("session_url"),
        "provider_session_id": rec.get("provider_session_id"),
        "failure_class": None,
        "provider_error": None,
        "runner_registered": bool(
            preclaim_registration
            and not preclaim_registration.get("error")
            and not preclaim_registration.get("error_code")),
        "usage_registered": False,
        "binding_pending": True,
        "reused_local_runner": True,
    }


def _register_preclaim_runner(wake, inventory, runner_session_id, *, renewal=False):
    binding = ((wake.get("policy") or {}).get("account_binding") or {})
    selector = wake.get("selector") or {}
    return register_runner_session({
        "runner_session_id": runner_session_id,
        "agent_id": selector.get("agent_id"),
        "runtime": selector.get("runtime"),
        "task_id": wake.get("task_id"),
        "claim_id": binding.get("claim_id"),
        "status": "starting",
        "cwd": inventory.get("repo_root"),
        "control": {"tier": "T3", "runner_kill": True, "managed_process": True},
        "metadata": {
            "credential_admission_phase": "preclaim",
            **({"preclaim_renewal": True} if renewal else {}),
        },
    }, wake, inventory)


def wait_for_runner_binding(wake, inventory, runner_session_id, timeout_s=None,
                            max_timeout_s=None, runner_alive=None,
                            sleep=time.sleep, monotonic=time.monotonic):
    """Wait until the child has published its exact claim + Work Session tuple.

    Process liveness is not execution readiness.  Autopilot may report Running only
    after the child owns the exact task and its Watch/Chat row is fully bound.
    """
    explicit_timeout = timeout_s is not None
    timeout_s = float(timeout_s if explicit_timeout else os.environ.get(
        "PM_AGENT_HOST_BIND_TIMEOUT_S", "90"))
    if max_timeout_s is None:
        max_timeout_s = (timeout_s if explicit_timeout else os.environ.get(
            "PM_AGENT_HOST_BIND_MAX_TIMEOUT_S", "600"))
    max_timeout_s = max(timeout_s, float(max_timeout_s))
    started_at = monotonic()
    deadline = started_at + max(0.0, timeout_s)
    hard_deadline = started_at + max(0.0, max_timeout_s)
    extended_for_live_boot = False
    renew_interval_s = max(1.0, float(os.environ.get(
        "PM_AGENT_HOST_PRECLAIM_RENEW_INTERVAL_S", "15")))
    next_renewal = monotonic() + renew_interval_s
    expected = {
        "runner_session_id": str(runner_session_id or ""),
        "task_id": str(wake.get("task_id") or ""),
        "host_id": str(inventory.get("host_id") or ""),
        "wake_id": str(wake.get("wake_id") or ""),
        "agent_id": str((wake.get("selector") or {}).get("agent_id") or ""),
        "runtime": str((wake.get("selector") or {}).get("runtime") or ""),
    }
    last = None
    last_exact_preclaim = False
    while monotonic() <= deadline:
        query = urllib.parse.urlencode({
            "project": PROJECT,
            "task_id": expected["task_id"],
            "host_id": expected["host_id"],
            "include_stale": "false",
        })
        result = _try("GET", f"{P_LIST_RUNNERS}?{query}") or {}
        sessions = result.get("sessions") or result.get("runner_sessions") or []
        for row in sessions if isinstance(sessions, list) else []:
            if str(row.get("runner_session_id") or "") != expected["runner_session_id"]:
                continue
            last = row
            metadata = row.get("metadata") or {}
            status = str(row.get("status") or "").lower()
            phase = str(
                metadata.get("credential_admission_phase") or "").lower()
            if (str(row.get("task_id") or "") == expected["task_id"]
                    and str(row.get("host_id") or "") == expected["host_id"]
                    and str(row.get("agent_id") or "") == expected["agent_id"]
                    and str(row.get("runtime") or "") == expected["runtime"]
                    and str(metadata.get("wake_id") or "") == expected["wake_id"]
                    and row.get("claim_id")
                    and metadata.get("work_session_id")
                    and phase == "claim_bound"
                    and not row.get("stale")
                    and status in {"ready", "running"}):
                return {"bound": True, "session": row}
            exact_preclaim = (
                str(row.get("task_id") or "") == expected["task_id"]
                and str(row.get("host_id") or "") == expected["host_id"]
                and str(row.get("agent_id") or "") == expected["agent_id"]
                and str(row.get("runtime") or "") == expected["runtime"]
                and str(metadata.get("wake_id") or "") == expected["wake_id"]
                and not row.get("claim_id")
                and not metadata.get("work_session_id")
                and phase == "preclaim"
                and status == "starting"
            )
            last_exact_preclaim = exact_preclaim
            now_mono = monotonic()
            if exact_preclaim and now_mono >= next_renewal:
                # The server performs an atomic compare-and-refresh.  If the child
                # bound between this read and POST, it returns the stronger row
                # unchanged instead of letting this preclaim record downgrade it.
                _register_preclaim_runner(
                    wake, inventory, runner_session_id, renewal=True)
                next_renewal = now_mono + renew_interval_s
        if monotonic() >= deadline:
            # Worktree creation on user-owned storage can legitimately exceed the
            # normal readiness SLO. Keep waiting only when both halves of the
            # admission proof agree that this is still the exact boot we launched:
            # the server still has our renewable preclaim and the local supervised
            # process is alive. A dead/mismatched boot still fails closed at the
            # original deadline; even a live boot is capped by hard_deadline.
            if (not extended_for_live_boot and last_exact_preclaim
                    and hard_deadline > deadline):
                if runner_alive is None:
                    health = supervisor_action("health", runner_session_id)
                    alive = bool((health or {}).get("alive"))
                else:
                    alive = bool(runner_alive(runner_session_id))
                if alive:
                    extended_for_live_boot = True
                    deadline = hard_deadline
                    continue
            break
        sleep(min(1.0, max(0.0, deadline - monotonic())))
    return {"bound": False, "reason": "runner_bind_timeout", "session": last}


def _enrich_bound_runner_record(rec, session):
    """Combine worker authority with supervisor-local Watch/Chat transport.

    The worker owns the claim, Work Session, phase, status, and workspace.  The
    supervisor owns the PTY/log/stream process details.  Preserve the former
    while adding the latter so the central row becomes both authoritative and
    actually watchable from the web.
    """
    rec = dict(rec or {})
    session = dict(session or {})
    local_metadata = dict(rec.get("metadata") or {})
    bound_metadata = dict(session.get("metadata") or {})
    # The worker publishes claim/Work Session authority before the Agent Host
    # finalizer joins in the supervisor-owned PTY.  Its row inherits the
    # preclaim transport placeholders (pty=false, null stream coordinates).
    # Never let those placeholders overwrite the supervisor's live transport.
    for key in _RUNNER_TRANSPORT_METADATA_FIELDS:
        bound_metadata.pop(key, None)
    return {
        **rec,
        "agent_id": session.get("agent_id") or rec.get("agent_id"),
        "runtime": session.get("runtime") or rec.get("runtime"),
        "task_id": session.get("task_id") or rec.get("task_id"),
        "claim_id": session.get("claim_id") or rec.get("claim_id"),
        "status": session.get("status") or rec.get("status"),
        "cwd": session.get("cwd") or rec.get("cwd"),
        "control": {
            **dict(session.get("control") or {}),
            **dict(rec.get("control") or {}),
        },
        # Bound values win if the local launch record still contains preclaim
        # metadata. Top-level PTY fields are folded in by register_runner_session.
        "metadata": {**local_metadata, **bound_metadata},
    }


def _missing_local_runner_transport(rec):
    """Return the missing supervisor-owned fields for a watchable relay PTY."""
    rec = dict(rec or {})
    missing = []
    if rec.get("pty") is not True:
        missing.append("pty")
    return missing


def _finalize_bound_runner(wake, inventory, runner_session_id, rec):
    """Finish claim-bound admission without blocking host dispatch/heartbeats."""
    bound_result = wait_for_runner_binding(wake, inventory, runner_session_id)
    runner_registration = (bound_result or {}).get("session")
    started = bool((bound_result or {}).get("bound"))
    reason = (bound_result or {}).get("reason") or "runner_bind_timeout"
    transport_missing = _missing_local_runner_transport(rec) if started else []
    if transport_missing:
        started = False
        reason = "runner_stream_not_ready"
    if not started:
        supervisor_action("kill", runner_session_id, {
            "grace_seconds": 2.0, "reason": "spawn failed before runner binding"})
        failed_rec = {
            **(rec or {}),
            "runner_session_id": runner_session_id,
            "status": "failed",
            "metadata": {
                **((rec or {}).get("metadata") or {}),
                "credential_admission_phase": "preclaim_failed",
                "failure_reason": reason,
                **({"missing_transport": transport_missing}
                   if transport_missing else {}),
            },
        }
        runner_registration = register_runner_session(
            failed_rec, wake, inventory)
    else:
        # The child owns claim/Work Session authority and the supervisor owns
        # Watch/Chat transport. Publish their joined row before acknowledging
        # the wake so the web can observe the runner as soon as it is Running.
        runner_registration = register_runner_session(
            _enrich_bound_runner_record(rec, runner_registration), wake, inventory)
        if (not runner_registration
                or runner_registration.get("error")
                or runner_registration.get("error_code")):
            started = False
            reason = ((runner_registration or {}).get("error_code")
                      or (runner_registration or {}).get("error")
                      or "runner_bind_registration_failed")
            supervisor_action("kill", runner_session_id, {
                "grace_seconds": 2.0, "reason": reason})
            failed_rec = {
                **(rec or {}),
                "runner_session_id": runner_session_id,
                "status": "failed",
                "metadata": {
                    **((rec or {}).get("metadata") or {}),
                    "credential_admission_phase": "preclaim_failed",
                    "failure_reason": reason,
                },
            }
            runner_registration = register_runner_session(
                failed_rec, wake, inventory)
        else:
            reason = "runner_bound"

            # BUG-126: Connect does not pass through the direct-task launch
            # branch that opens Watch/Chat immediately.  Once the child has
            # supplied its exact claim + Work Session tuple, registration can
            # mint the host relay ticket.  Open that bridge before completing
            # the wake so the first visible Running receipt is already
            # watchable; the heartbeat path remains an idempotent repair loop.
            server_relay = _fresh_server_relay(
                (runner_registration or {}).get("server_relay"),
                runner_session_id, str(inventory.get("host_id") or ""))
            if server_relay.get("host_url"):
                try:
                    _ensure_host_bridge(
                        runner_session_id=runner_session_id,
                        host_id=str(inventory.get("host_id") or ""),
                        binding=dict(server_relay.get("binding") or {}),
                        public_base="",
                        host_relay_url=str(server_relay.get("host_url") or ""),
                        child_pid=int((rec or {}).get("pid") or 0),
                        log_path=str((rec or {}).get("log_path") or ""),
                        expires_at=server_relay.get("expires_at"),
                    )
                except Exception as exc:
                    # Keep the provider process alive: the heartbeat will retry
                    # the idempotent bridge.  Preserve the launch-time failure
                    # on the durable receipt instead of hiding it.
                    runner_registration["host_relay_error"] = type(exc).__name__
            else:
                # A bound Connect process without a host capability remains
                # alive and retryable, but it is not Watch-ready.  Name that
                # state on the wake receipt; never silently equate process
                # liveness with an attached terminal.
                runner_registration["host_relay_error"] = "missing_host_url"

    result = {
        "started": started,
        "runner_session_id": ((rec or {}).get("runner_session_id")
                              or runner_session_id),
        "wake_mode": (rec or {}).get("wake_mode") or wake_mode(wake, inventory),
        "reason": reason,
        "pid": (rec or {}).get("pid"),
        "cwd": (rec or {}).get("cwd"),
        "task_id": (rec or {}).get("task_id") or wake.get("task_id"),
        "claim_id": ((runner_registration or {}).get("claim_id")
                     if started else None),
        "work_session_id": (((runner_registration or {}).get("metadata") or {})
                            .get("work_session_id") if started else None),
        "control": (rec or {}).get("control") or {},
        "session_url": (rec or {}).get("session_url"),
        "provider_session_id": (rec or {}).get("provider_session_id"),
        "failure_class": ((rec or {}).get("failure_class")
                          or (None if started else "failed_gate")),
        "provider_error": (rec or {}).get("provider_error"),
        "runner_registered": bool(
            runner_registration and not runner_registration.get("error")
            and not runner_registration.get("error_code")),
        "host_relay_error": (
            (runner_registration or {}).get("host_relay_error") or None),
        "usage_registered": False,
        "binding_pending": False,
    }
    completion = _try("POST", P_COMPLETE_WAKE, {
        "project": PROJECT,
        "wake_id": wake.get("wake_id"),
        "runner_session_id": result["runner_session_id"],
        "agent_id": (wake.get("selector") or {}).get("agent_id"),
        "result": result,
    })
    result["wake_completed"] = bool(completion and not completion.get("error"))
    return {"host_id": inventory.get("host_id"),
            "wake_id": wake.get("wake_id"), **result}


def _submit_bound_finalizer(wake, inventory, runner_session_id, rec):
    """Start one daemon finalizer per claimed wake and return immediately."""
    key = _bound_finalizer_key(wake, inventory, runner_session_id)

    def finish():
        try:
            receipt = _finalize_bound_runner(
                wake, inventory, runner_session_id, rec)
        except Exception as exc:
            # A background exception must still fail closed and release the
            # durable wake instead of silently stranding it as claimed.
            supervisor_action("kill", runner_session_id, {
                "grace_seconds": 2.0, "reason": "runner bind finalizer failed"})
            result = {
                "started": False,
                "runner_session_id": runner_session_id,
                "wake_mode": (rec or {}).get("wake_mode") or wake_mode(wake, inventory),
                "reason": "runner_bind_finalizer_error",
                "task_id": wake.get("task_id"),
                "failure_class": "failed_gate",
                "provider_error": str(exc)[:500],
                "binding_pending": False,
            }
            register_runner_session({
                **(rec or {}),
                "runner_session_id": runner_session_id,
                "status": "failed",
                "metadata": {
                    **((rec or {}).get("metadata") or {}),
                    "credential_admission_phase": "preclaim_failed",
                    "failure_reason": "runner_bind_finalizer_error",
                },
            }, wake, inventory)
            _try("POST", P_COMPLETE_WAKE, {
                "project": PROJECT,
                "wake_id": wake.get("wake_id"),
                "runner_session_id": runner_session_id,
                "agent_id": (wake.get("selector") or {}).get("agent_id"),
                "result": result,
            })
            receipt = {"host_id": inventory.get("host_id"),
                       "wake_id": wake.get("wake_id"), **result}
        with _BOUND_FINALIZERS_LOCK:
            _BOUND_FINALIZERS.pop(key, None)
            _BOUND_FINALIZER_RESULTS.append(receipt)

    with _BOUND_FINALIZERS_LOCK:
        if key in _BOUND_FINALIZERS:
            return False
        thread = threading.Thread(
            target=finish,
            name=f"agent-host-bind-{str(wake.get('wake_id') or '')[-12:]}",
            daemon=True,
        )
        _BOUND_FINALIZERS[key] = thread
        thread.start()
    return True


def _reap_bound_finalizers(host_id):
    """Return completed async receipts for this host without blocking."""
    with _BOUND_FINALIZERS_LOCK:
        ours = [row for row in _BOUND_FINALIZER_RESULTS
                if row.get("host_id") == host_id]
        _BOUND_FINALIZER_RESULTS[:] = [
            row for row in _BOUND_FINALIZER_RESULTS
            if row.get("host_id") != host_id
        ]
    return [{k: v for k, v in row.items() if k != "host_id"} for row in ours]


def _acquire_provider_lease(wake, inventory, runner_session_id):
    binding = ((wake.get("policy") or {}).get("account_binding") or {})
    reference = str(binding.get("credential_reference") or "")
    if not reference:
        return {"error": "credential_reference_missing"}
    return _try(
        "POST",
        f"/api/projects/{urllib.parse.quote(PROJECT, safe='')}/"
        f"provider-connections/{urllib.parse.quote(reference, safe='')}/leases",
        {
            "project": PROJECT,
            "user_id": binding.get("user_id"),
            "provider": binding.get("provider"),
            "provider_account_id": binding.get("provider_account_id"),
            "task_id": wake.get("task_id"),
            "host_id": inventory.get("host_id"),
            "runner_session_id": runner_session_id,
            "work_session_id": binding.get("work_session_id"),
            "account_affinity_id": binding.get("account_affinity_id"),
            "ttl_seconds": int((wake.get("policy") or {}).get(
                "credential_lease_ttl_seconds") or 900),
        },
    ) or {"error": "credential_lease_acquisition_failed"}


def _publish_drain_host(inventory, status, capacity):
    return _try("POST", P_HEARTBEAT_HOST, {
        "project": PROJECT,
        "host_id": inventory["host_id"],
        "status": status,
        "active_sessions": capacity.get("active_sessions"),
        "capacity": capacity,
        "last_error": "" if status == "drained" else ",".join(
            (capacity.get("drain_receipt") or {}).get("failures") or []),
    })


def _update_drained_runner(runner):
    return _try("POST", P_REGISTER_RUNNER, {"project": PROJECT, **dict(runner)})


def _drain_runner_action(inventory, action, runner_session_id, options=None):
    """Route automatic CO drain stops through the one execution-lease clock."""
    if action != "lease_stop":
        return supervisor_action(action, runner_session_id, options)
    transition = _try("POST", P_RUNNER_LEASE_DUE, {
        "project": PROJECT,
        "host_id": inventory["host_id"],
        "runner_session_id": runner_session_id,
        "reason": str((options or {}).get("reason") or "host drain"),
        "authority": "co_drain",
    }) or {}
    if transition.get("error"):
        return {"error": transition.get("error"), "alive": True,
                "lease_transition": transition}
    outcomes = expire_runner_leases(inventory)
    outcome = next((
        item for item in outcomes
        if item.get("runner_session_id") == runner_session_id
    ), {})
    health = supervisor_action("health", runner_session_id)
    return {
        **outcome,
        "alive": bool(health and not health.get("error") and health.get("alive")),
        "lease_transition": transition,
    }


def handle_drain(request, inventory):
    """Stop claims first, then interrupt/checkpoint/release/purge and acknowledge."""
    current = co_drain.read_receipt()
    if current and current.get("request_id") == request.get("request_id"):
        published = _publish_drain_host(
            inventory, current.get("status") or "drain_failed", {
            "active_sessions": 0 if current.get("status") == "drained" else 1,
            "drain_receipt": current,
        })
        current["durable_acknowledged"] = bool(
            published and not published.get("error"))
        co_drain.write_receipt(current)
        return {"host_id": inventory["host_id"], "draining": True,
                "drain_receipt": current, "acted": [], "pending": 0,
                "runner_controls": []}
    receipt = co_drain.drain_host(
        request,
        co_drain.inventory_for_drain(inventory),
        runners=_drain_runners(inventory["host_id"], recover_stale_local=False),
        work_sessions=_drain_work_sessions(),
        supervisor=lambda action, runner_id, options=None: _drain_runner_action(
            inventory, action, runner_id, options),
        release_lease=_release_provider_lease,
        publish_host=lambda status, capacity: _publish_drain_host(
            inventory, status, capacity),
        update_runner=_update_drained_runner,
        workspace_root=os.environ.get("PM_WORKSPACE_ROOT")
        or os.path.dirname(os.environ.get("PM_REPO_ROOT")
                           or inventory.get("repo_root") or os.getcwd()),
        runtime_root=os.environ.get("PM_PROVIDER_RUNTIME_ROOT"),
    )
    co_drain.write_receipt(receipt)
    return {"host_id": inventory["host_id"], "draining": True,
            "drain_receipt": receipt, "acted": [], "pending": 0,
            "runner_controls": []}


def run_once(inventory):
    """One daemon iteration. Returns a summary of what it did (for tests + logging)."""
    drain_request = co_drain.discover_request()
    if drain_request:
        return handle_drain(drain_request, inventory)
    host_id = inventory["host_id"]
    finalized = _reap_bound_finalizers(host_id)
    capacity = heartbeat_capacity(inventory)
    heartbeat = _try("POST", P_HEARTBEAT_HOST, {
        "project": PROJECT, "host_id": host_id,
        "active_sessions": capacity["active_sessions"], "capacity": capacity,
    })
    if apply_authoritative_execution_policy(inventory, heartbeat):
        advertised = _try("POST", P_REGISTER_HOST, registration_inventory(inventory))
        apply_authoritative_execution_policy(inventory, advertised)
        capacity = heartbeat_capacity(inventory)
    expired_runner_leases = expire_runner_leases(inventory)
    if expired_runner_leases:
        capacity = heartbeat_capacity(inventory)
    runner_heartbeats = renew_live_direct_runners(inventory)
    local_auth = capacity.get("local_auth")
    if isinstance(local_auth, dict) and local_auth.get("available") is not True:
        return {
            "host_id": host_id,
            "pending": 0,
            "acted": finalized,
            "refused": [],
            "runner_controls": [],
            "runner_heartbeats": runner_heartbeats,
            "expired_runner_leases": expired_runner_leases,
            "auth_available": False,
        }
    recovery = None
    recovery_enabled = (
        _truthy(os.environ.get("PM_PERSONAL_AGENT_HOST_RECOVERY"))
        or _truthy(os.environ.get("PM_PERSONAL_AGENT_HOST_EXECUTION"))
    )
    if recovery_enabled:
        try:
            from codex_local_worker import resume_pending_postprocessing
            recovery = resume_pending_postprocessing()
        except Exception as exc:
            recovery = {
                "schema": "switchboard.personal_postprocessing_recovery_scan.v1",
                "recovered": [],
                "pending": [{"error": str(exc)}],
                "recovered_count": 0,
                "pending_count": 1,
            }
        # Never accept another wake while exact pushed work still needs its
        # checkpoint/claim completion. The daemon retries this durable receipt on
        # every poll; after the bounded deadline it is retained as an operator-visible
        # quarantine instead of permanently disabling unrelated host work.
    if recovery and recovery.get("pending_count"):
        return {
            "host_id": host_id,
            "pending": 0,
            "acted": finalized,
            "refused": [],
            "runner_controls": [],
            "expired_runner_leases": expired_runner_leases,
            "postprocessing_recovery": recovery,
        }
    controls = handle_runner_controls(inventory)
    listed = _try("GET", f"{P_LIST_WAKES}?project={PROJECT}&status=pending") or {}
    wakes = wakes_bound_to_host(listed.get("wake_intents") or listed.get("wakes") or [])
    acted = list(finalized)
    refused = []
    cap = inventory["limits"]["max_sessions"]
    for w in wakes:
        # The supervisor list already includes sessions launched earlier in this
        # tick. Adding len(acted) counts those children a second time (and also
        # counts failed launches), which silently cuts usable fanout roughly in
        # half. Treat the supervisor's live inventory as the capacity authority.
        if active_session_count(inventory) >= cap:
            print("[agent_host] at capacity; leaving remaining wakes for other hosts", flush=True)
            break
        exact_binding = validate_personal_wake_binding(w, inventory)
        if not exact_binding.get("valid"):
            refused.append({"wake_id": w.get("wake_id"), **exact_binding})
            continue
        if not eligible_runtime(w, inventory):
            continue  # not ours — let an eligible host claim it (substrate records if none do)
        wake_id = w.get("wake_id")
        if wake_mode(w, inventory) == "direct_task":
            selected_host = str((w.get("selector") or {}).get("host_id") or "")
            if selected_host != str(host_id or ""):
                continue
            assignment = dict((w.get("policy") or {}).get("assignment") or {})
            if (assignment.get("schema") != "switchboard.direct_cli_assignment.v1"
                    or str(assignment.get("task_id") or "") != str(w.get("task_id") or "")
                    or str(assignment.get("host_id") or "") != str(host_id or "")):
                refused.append({
                    "wake_id": wake_id,
                    "error": "direct_assignment_invalid",
                    "reason": "direct assignment does not match task and selected host",
                })
                continue
            runner_session_id = _runner_session_id_for_wake(w, host_id)
            health = supervisor_action("health", runner_session_id)
            reused = bool(health and not health.get("error") and health.get("alive"))
            if reused:
                rec = dict(health)
                rec.update({
                    "runner_session_id": runner_session_id,
                    "wake_mode": "direct_task",
                    "host_id": host_id,
                    "runtime": "codex",
                    "task_id": w.get("task_id") or "",
                })
            else:
                try:
                    rec = launch(
                        w, inventory, runner_session_id=runner_session_id,
                        extra_env={
                            "PM_DIRECT_CODEX_ASSIGNMENT_JSON": json.dumps(
                                assignment, sort_keys=True),
                            "PM_CO_WAKE_ID": str(wake_id or ""),
                            "PM_CO_HOST_ID": str(host_id or ""),
                        },
                    )
                except Exception as exc:
                    rec = {
                        "runner_session_id": runner_session_id,
                        "started": False,
                        "wake_mode": "direct_task",
                        "reason": "direct_cli_launch_configuration_error",
                        "failure_class": "failed_gate",
                        "provider_error": str(exc)[:500],
                    }
            started = bool(reused or confirm_started(rec))
            assignment_path = os.path.join(
                str(os.environ.get("PM_AGENT_HOST_RUNNER_DIR")
                    or os.environ.get("PM_RUNNER_DIR") or ".switchboard/runner"),
                runner_session_id, "assignment.toml",
            )
            if started:
                rec["status"] = "running"
                rec["metadata"] = {
                    **((rec or {}).get("metadata") or {}),
                    "direct_assignment": True,
                    "assignment_schema": assignment.get("schema"),
                    "assignment_toml": assignment_path,
                    "auth_lane": "enrolled_agent_host_token",
                }
                runner_registration = register_runner_session(rec, w, inventory)
                server_relay = _fresh_server_relay((
                    (runner_registration or {}).get("server_relay")
                    if isinstance(runner_registration, dict) else None
                ), runner_session_id, host_id)
                if server_relay.get("host_url"):
                    try:
                        _ensure_host_bridge(
                            runner_session_id=runner_session_id,
                            host_id=host_id,
                            binding=dict(server_relay.get("binding") or {}),
                            public_base="",
                            host_relay_url=str(server_relay.get("host_url") or ""),
                            child_pid=int((rec or {}).get("pid") or 0),
                            log_path=str((rec or {}).get("log_path") or ""),
                            expires_at=server_relay.get("expires_at"),
                        )
                    except Exception as exc:
                        if isinstance(runner_registration, dict):
                            runner_registration["host_relay_error"] = type(exc).__name__
            else:
                runner_registration = None
            registered = bool(
                runner_registration
                and not runner_registration.get("error")
                and not runner_registration.get("error_code")
            )
            completion = None
            if started and registered:
                result = {
                    "started": True,
                    "reason": "direct_cli_started",
                    "runner_session_id": runner_session_id,
                    "task_id": w.get("task_id"),
                    "host_id": host_id,
                    "pid": (rec or {}).get("pid"),
                    "cwd": (rec or {}).get("cwd"),
                }
                # Acknowledge only after the PTY is live and centrally visible.
                # There is deliberately no ownership handshake before launch.
                completion = _try("POST", P_COMPLETE_WAKE, {
                    "project": PROJECT,
                    "wake_id": wake_id,
                    "runner_session_id": runner_session_id,
                    "agent_id": (w.get("selector") or {}).get("agent_id") or "",
                    "result": result,
                })
            completion_recorded = bool(
                completion and not completion.get("error")
                and not completion.get("error_code")
            )
            if started and (not registered or not completion_recorded):
                # A native process without its durable runner/wake receipt cannot
                # be discovered, watched, or safely deduplicated.  Stop it instead
                # of leaving an invisible orphan, publish a terminal row when the
                # registry is reachable, and leave the wake retryable.
                failure_reason = (
                    "direct_runner_registration_failed" if not registered
                    else "direct_complete_wake_failed"
                )
                supervisor_action("kill", runner_session_id, {
                    "grace_seconds": 2.0, "reason": failure_reason})
                failed_rec = {
                    **(rec or {}),
                    "runner_session_id": runner_session_id,
                    "status": "failed",
                    "metadata": {
                        **((rec or {}).get("metadata") or {}),
                        "failure_reason": failure_reason,
                    },
                }
                register_runner_session(failed_rec, w, inventory)
                rec = {**(rec or {}), "reason": failure_reason,
                       "failure_class": "failed_gate"}
                started = False
            acted.append({
                "wake_id": wake_id,
                "started": started,
                "runner_session_id": runner_session_id,
                "wake_mode": "direct_task",
                "reason": (
                    "direct_cli_started" if started and registered
                    else "direct_runner_registration_failed" if started
                    else (rec or {}).get("reason") or "direct_cli_launch_failed"
                ),
                "pid": (rec or {}).get("pid"),
                "cwd": (rec or {}).get("cwd"),
                "task_id": w.get("task_id"),
                "host_id": host_id,
                "runner_registered": registered,
                "assignment_toml": assignment_path,
                "completion_recorded": completion_recorded,
                "provider_error": (rec or {}).get("provider_error"),
            })
            continue
        binding = ((w.get("policy") or {}).get("account_binding") or {})
        bind_required = bool(
            w.get("task_id")
            and (w.get("policy") or {}).get("require_runner_bind") is True
        )
        runner_session_id = ""
        if wake_mode(w, inventory) == "connect":
            # Connect leases one stable runner identity before launch. The same
            # id is carried by the Ack, supervisor, environment, and registry.
            runner_session_id = _runner_session_id_for_wake(w, host_id)
        preclaim_registration = None
        if binding or bind_required:
            runner_session_id = _runner_session_id_for_wake(w, host_id)
            preclaim_registration = _register_preclaim_runner(
                w, inventory, runner_session_id)
            if not preclaim_registration or preclaim_registration.get("error"):
                continue
        claimed = _try("POST", P_CLAIM_WAKE, {
            "project": PROJECT,
            "host_id": host_id,
            "wake_id": wake_id,
            "runner_session_id": runner_session_id,
        })
        if not claimed or not (claimed.get("claimed", True)):
            continue  # another host won it (atomic claim)
        claimed_wake = claimed.get("wake") or w
        claimed_exact_binding = validate_personal_wake_binding(
            claimed_wake, inventory)
        if not claimed_exact_binding.get("valid"):
            refused.append({"wake_id": wake_id, "phase": "post_claim",
                            **claimed_exact_binding})
            _try("POST", P_COMPLETE_WAKE, {
                "project": PROJECT,
                "wake_id": wake_id,
                "runner_session_id": runner_session_id,
                "agent_id": ((claimed_wake.get("selector") or {}).get("agent_id") or ""),
                "result": {"started": False, "reason": "exact_binding_denied"},
            })
            continue
        if bind_required and runner_session_id:
            reused = _reuse_inflight_bound_runner(
                claimed_wake, inventory, runner_session_id,
                preclaim_registration=preclaim_registration)
            if reused:
                acted.append(reused)
                continue
        execution_binding = ((claimed_wake.get("policy") or {}).get(
            "execution_binding") or {})
        if wake_mode(claimed_wake, inventory) == "connect":
            # Connect hands the CLI only its six immutable connection refs.
            # Legacy Work Session/claim/lifecycle bootstrap belongs above this
            # layer and must not leak into a provider-neutral launch.
            launch_env = {}
        else:
            launch_env = ({
                "PM_CO_WAKE_ID": str(claimed_wake.get("wake_id") or wake_id or ""),
                "PM_CO_HOST_ID": str(host_id or ""),
                "PM_REMOTE_WORK_SESSION_REGISTRATION": "1",
                "PM_AUTO_WORK_SESSION": "1",
                "PM_WORK_SESSION_POLICY_PROFILE": "code_strict",
                "PM_RUNTIME": str((claimed_wake.get("selector") or {}).get(
                    "runtime") or ""),
                "PM_WORK_SESSION_SOURCE_PATH": str(inventory.get("repo_root") or ""),
                "PM_AGENT_HOST_ISOLATE_TASK_WORKSPACE": "1",
                "PM_PERSONAL_AGENT_HOST_EXECUTION": "0",
            } if claimed_wake.get("task_id") else {})
        if binding:
            launch_env.update({
                "PM_CO_ACCOUNT_BINDING_JSON": json.dumps(
                    (claimed_wake.get("policy") or {}).get("account_binding") or {},
                    sort_keys=True,
                ),
                "PM_CO_WAKE_ID": str(claimed_wake.get("wake_id") or wake_id or ""),
                "PM_CO_HOST_ID": str(host_id or ""),
                "PM_REMOTE_WORK_SESSION_REGISTRATION": "1",
                "PM_AUTO_WORK_SESSION": "1",
                "PM_WORK_SESSION_POLICY_PROFILE": "code_strict",
                "PM_PERSONAL_AGENT_HOST_EXECUTION": (
                    "1" if claimed_exact_binding.get("required") else "0"),
                "PM_WORK_SESSION_ID": str(binding.get("work_session_id") or ""),
                "PM_CLAIM_ID": str(binding.get("claim_id") or ""),
                "PM_SOURCE_SHA": str(execution_binding.get("source_sha") or ""),
                "PM_EXECUTION_CONNECTION_ID": str(
                    execution_binding.get("execution_connection_id") or ""),
            })
        try:
            rec = (launch(claimed_wake, inventory, runner_session_id=runner_session_id,
                          extra_env=launch_env)
                   if runner_session_id else launch(claimed_wake, inventory))
        except Exception as exc:
            rec = {
                "runner_session_id": runner_session_id or None,
                "started": False,
                "wake_mode": wake_mode(claimed_wake, inventory),
                "reason": "runtime_launch_configuration_error",
                "failure_class": "failed_gate",
                "provider_error": str(exc)[:500],
            }
        rec_mode = (rec or {}).get("wake_mode") or wake_mode(w, inventory)
        started = (confirm_closure_verified(rec) if rec_mode == "closure_verify"
                  else confirm_started(rec))
        # BYOA runners rebind this preclaim row themselves after claim_next has
        # produced the active task claim and Work Session. A generic post-launch
        # upsert here would race that update and erase the exact binding.
        bound_result = None
        if binding and started:
            runner_registration = preclaim_registration
        elif binding:
            failed_rec = {
                **(rec or {}),
                "runner_session_id": (
                    (rec or {}).get("runner_session_id") or runner_session_id
                ),
                "status": "failed",
                "metadata": {
                    **((rec or {}).get("metadata") or {}),
                    "credential_admission_phase": "preclaim_failed",
                    "failure_reason": "launch_failed",
                },
            }
            runner_registration = register_runner_session(
                failed_rec, claimed_wake, inventory)
        elif bind_required and started:
            _submit_bound_finalizer(
                claimed_wake, inventory, runner_session_id, rec)
            # Launch acknowledgement is intentionally distinct from durable
            # wake completion. The finalizer will publish the exact claim-bound
            # Watch/Chat row and complete this wake independently.
            acted.append({
                "wake_id": wake_id,
                "started": True,
                "runner_session_id": ((rec or {}).get("runner_session_id")
                                      or runner_session_id),
                "wake_mode": (rec or {}).get("wake_mode") or wake_mode(w, inventory),
                "reason": "runner_binding_pending",
                "pid": (rec or {}).get("pid"),
                "cwd": (rec or {}).get("cwd"),
                "task_id": (rec or {}).get("task_id") or w.get("task_id"),
                "claim_id": None,
                "work_session_id": None,
                "control": (rec or {}).get("control") or {},
                "session_url": (rec or {}).get("session_url"),
                "provider_session_id": (rec or {}).get("provider_session_id"),
                "failure_class": None,
                "provider_error": None,
                "runner_registered": bool(
                    preclaim_registration
                    and not preclaim_registration.get("error")),
                "usage_registered": False,
                "binding_pending": True,
            })
            continue
        elif bind_required:
            result_reason = (rec or {}).get("reason") or "launch_failed"
            failed_rec = {
                **(rec or {}),
                "runner_session_id": runner_session_id,
                "status": "failed",
                "metadata": {
                    **((rec or {}).get("metadata") or {}),
                    "credential_admission_phase": "preclaim_failed",
                    "failure_reason": result_reason,
                },
            }
            runner_registration = register_runner_session(
                failed_rec, claimed_wake, inventory)
        else:
            runner_registration = (
                register_runner_session(rec, claimed_wake, inventory) if started else None
            )
            connect_mode = wake_mode(claimed_wake, inventory) == "connect"
            if started and connect_mode and (
                    not runner_registration
                    or runner_registration.get("error")
                    or runner_registration.get("error_code")):
                rec = {
                    **(rec or {}),
                    "failure_class": "failed_gate",
                    "provider_error": "Connect runner registry rejected the launch",
                }
                supervisor_action("kill", runner_session_id, {
                    "grace_seconds": 2.0,
                    "reason": "connect runner registration failed"})
                started = False
                result_reason = "connect_runner_registration_failed"
            # COORD-34: non-BYOA claimed-task boots must publish a successful bind
            # before Watch/Chat may open. Incomplete/failed register fails the wake.
            elif started and (rec or {}).get("claim_id"):
                if (not runner_registration
                        or runner_registration.get("error")
                        or runner_registration.get("error_code") == "runner_bind_incomplete"):
                    started = False
                    result_reason = (
                        (runner_registration or {}).get("error_code")
                        or (runner_registration or {}).get("error")
                        or "runner_bind_incomplete"
                    )
                    supervisor_action("kill", runner_session_id, {
                        "grace_seconds": 2.0, "reason": result_reason})
                else:
                    result_reason = "started"
            else:
                result_reason = ("started" if started else
                                 (rec or {}).get("reason") or "launch_failed")
        usage_registration = report_cloud_usage(
            rec, claimed_wake) if started and rec.get("cloud_session") else None
        if binding:
            result_reason = "started" if started else "launch_failed"
        result = {"started": started,
                  "runner_session_id": ((rec or {}).get("runner_session_id")
                                        or runner_session_id or None),
                  "wake_mode": (rec or {}).get("wake_mode") or wake_mode(w, inventory),
                  "reason": result_reason,
                  "pid": (rec or {}).get("pid"),
                  "cwd": (rec or {}).get("cwd"),
                  "task_id": (rec or {}).get("task_id") or w.get("task_id"),
                  "claim_id": ((runner_registration or {}).get("claim_id")
                               if bind_required else (rec or {}).get("claim_id")),
                  "work_session_id": (((runner_registration or {}).get("metadata") or {})
                                      .get("work_session_id")
                                      if bind_required else (rec or {}).get("work_session_id")),
                  "control": (rec or {}).get("control") or {},
                  "session_url": (rec or {}).get("session_url"),
                  "provider_session_id": (rec or {}).get("provider_session_id"),
                  "failure_class": (rec or {}).get("failure_class"),
                  "provider_error": (rec or {}).get("provider_error"),
                  "runner_registered": bool(runner_registration and not runner_registration.get("error")),
                  "usage_registered": bool(usage_registration and not usage_registration.get("error"))}
        # A BYOA wake is only reserved here. The child must establish its task claim,
        # Work Session, exact lease, encrypted materialization, and provider preflight
        # before it completes the wake. Completing it now would race the second-phase
        # claim_wake call and make the admission contract impossible.
        if binding:
            result["wake_completion_delegated"] = bool(started)
        if not binding or not started:
            _try("POST", P_COMPLETE_WAKE, {"project": PROJECT, "wake_id": wake_id,
                                           "runner_session_id": result["runner_session_id"],
                                           "agent_id": (w.get("selector") or {}).get("agent_id"),
                                           "result": result})
        acted.append({"wake_id": wake_id, **result})
    return {"host_id": host_id, "pending": len(wakes), "acted": acted,
            "refused": refused,
            "runner_controls": controls,
            "runner_heartbeats": runner_heartbeats,
            "expired_runner_leases": expired_runner_leases,
            "postprocessing_recovery": recovery}


def run(interval=10, once=False):
    inv = default_inventory()
    registered = False
    last_register_at = 0.0
    drain_advertised = False
    register_every = max(10, int(inv.get("heartbeat_ttl_s") or 60) // 2)
    while True:
        now = time.time()
        auth_changed = refresh_local_auth_inventory(inv, now=now)
        drain_request = co_drain.discover_request()
        advertised = registration_inventory(inv, drain_request=drain_request)
        should_register = (not registered or auth_changed
                           or now - last_register_at >= register_every
                           or bool(drain_request) != drain_advertised)
        if should_register:
            reg = _try("POST", P_REGISTER_HOST, advertised)
            if apply_authoritative_execution_policy(inv, reg):
                advertised = registration_inventory(inv, drain_request=drain_request)
                reg = _try("POST", P_REGISTER_HOST, advertised)
            registered = bool(reg and not reg.get("error"))
            drain_advertised = bool(drain_request and reg)
            last_register_at = now
            print(f"[agent_host] registered {inv['host_id']} ({'ok' if reg else 'retrying'})",
                  flush=True)
        summary = run_once(inv)
        print(f"[agent_host] {json.dumps(summary)}", flush=True)
        if once:
            return summary
        time.sleep(max(1, interval))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Switchboard Agent Host daemon")
    ap.add_argument("--once", action="store_true", help="one iteration then exit (for tests/cron)")
    ap.add_argument("--interval", type=int, default=10)
    a = ap.parse_args()
    out = run(interval=a.interval, once=a.once)
    if a.once:
        print(json.dumps(out))
