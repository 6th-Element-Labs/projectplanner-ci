"""Is an Agent Host safe to run work — as distinct from merely online.

A heartbeat proves a process is alive. It does not prove the bundle that
process is running can produce a contract this server will accept. Conflating
the two is what made the 2026-07-31 drain canary look healthy while every Wave A
launch was refused: three wakes were claimed by a live, green host whose bundled
``execution_assignment.py`` predated ``session_policy_profile``, and each died at
``execution_assignment_contract_mismatch`` after burning the 90s claim hold.

So liveness and readiness are two different lights here:

    online      the heartbeat has not expired
    readiness   the installed bundle can do the work we would give it

``ready``             live, digest matches the promoted release, contract agrees
``update_available``  compatible contract, but a newer release is promoted
``blocked``           contract disagrees, or the bundle is not the promoted one
``updating``          the host is drain-and-replacing itself right now
``update_failed``     the promoted digest failed to install on this host
``offline``           heartbeat expired

Only ``blocked`` withholds work, and it is a refusal with a reason
(``host_release_incompatible``) rather than a silent skip — a skip that records
nothing is the failure mode this board keeps re-finding.

Version strings alone are not enough and tonight proved it: the incident was
recovered by hand-copying one file into the deployed 0.4.15 tree, so the version
stayed ``0.4.15`` while the contents changed. Identity is the bundle digest; the
version is a label for humans.
"""

from __future__ import annotations

import time
from typing import Any, Mapping, Optional


SCHEMA = "switchboard.host_readiness.v1"

READY = "ready"
UPDATE_AVAILABLE = "update_available"
BLOCKED = "blocked"
UPDATING = "updating"
UPDATE_FAILED = "update_failed"
OFFLINE = "offline"

#: States in which this host must not be given new work.
#:
#: ``UPDATING`` belongs here and the reason is load-bearing: a host updates by
#: refusing new claims and waiting for its live runners to finish. Keep feeding
#: it and the drain never reaches zero, so the update never starts — a hang
#: dressed up as a healthy fleet. Withholding is what lets the drain converge.
WITHHOLDS_WORK = frozenset({BLOCKED, OFFLINE, UPDATING})

#: How long a host may claim to be updating before the control plane stops
#: believing it. A host that dies mid-update leaves `update_state` set in its
#: last heartbeat forever; without this bound that row would withhold work for
#: good. Past the deadline the host is judged on what it actually reports —
#: which is either fine (blocked/behind) or fine (ready).
UPDATE_STATE_MAX_AGE_S = 20 * 60

BLOCKED_REASON = "host_release_incompatible"


def _text(value: Any) -> str:
    return str(value or "").strip()


def _seconds(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def evaluate(host: Mapping[str, Any],
             required: Optional[Mapping[str, Any]],
             now: Optional[float] = None) -> dict[str, Any]:
    """Readiness for one host against the promoted release.

    ``required`` is the operator-promoted release record, never "whatever is on
    master right now": promoting is a deliberate act, and comparing hosts to an
    unpromoted tip would block the fleet on every merge.

    With no promoted release the control plane has no opinion, and a live host
    is ``ready``. Fail-open is deliberate here — this module must never be the
    reason a fleet cannot work — while the launch-time exact-contract check
    stays as the hard backstop it already is.
    """
    now = time.time() if now is None else float(now)
    live = not host.get("stale")
    installed_version = _text(host.get("agent_host_version"))
    installed_digest = _text(host.get("bundle_digest"))
    host_contract = _text(host.get("contract_fingerprint"))
    update_state = _text(host.get("update_state"))
    update_error = _text(host.get("update_error"))
    capacity = host.get("capacity") or {}
    release_management = _text(
        host.get("release_management")
        or (capacity.get("release_management") if isinstance(capacity, Mapping) else "")
    ) or "signed_bundle"
    self_updates = release_management == "signed_bundle"

    out: dict[str, Any] = {
        "schema": SCHEMA,
        "state": READY,
        "reason": "",
        "detail": "",
        "installed_version": installed_version,
        "installed_digest": installed_digest,
        "required_version": "",
        "required_digest": "",
        "contract_matches": True,
        "actionable": False,     # is there an install/update the operator can run
        "withholds_work": False,
        "enforcing": False,      # is a blocked verdict actually withholding work
        "release_management": release_management,
    }

    if not live:
        out.update(state=OFFLINE, reason="host_heartbeat_expired",
                   detail="No heartbeat inside the lease TTL.",
                   withholds_work=True)
        return out

    if update_state in {"draining", "installing", "restarting"}:
        started = _seconds(host.get("update_started_at"))
        age = max(0.0, _seconds(now) - started) if started else 0.0
        if started and age > UPDATE_STATE_MAX_AGE_S:
            # Stale claim: fall through and judge the host on the bundle it
            # actually reports. An update that never finished is not a reason
            # to withhold work forever.
            out["detail"] = (f"Ignoring a stale {update_state} claim "
                             f"({int(age // 60)}m old).")
        else:
            out.update(state=UPDATING, reason="host_update_in_progress",
                       withholds_work=True,
                       detail=(f"Host is {update_state}; it stops taking work "
                               f"until its live runners finish."))
            return out

    if not required:
        out.update(detail="No release has been promoted; the control plane has no opinion.")
        return out

    # Observe vs enforce. A promoted release starts in observe: the verdict is
    # computed and shown, but nothing is withheld. This exists because the FIRST
    # promotion on any fleet meets hosts that predate attestation — they report
    # no fingerprint, so they are judged incompatible, and the self-update that
    # would rescue them ships inside the release they do not have. Enforcing on
    # day one would strand every host at once. Observe lets the fleet converge,
    # then enforcement is flipped deliberately.
    enforcing = bool(required.get("enforce"))

    req_version = _text(required.get("version"))
    req_digest = _text(required.get("bundle_digest"))
    req_contract = _text(required.get("contract_fingerprint"))
    out["required_version"] = req_version
    out["required_digest"] = req_digest

    # Contract disagreement is the launch-refusal in advance. Check it first:
    # it is the only condition that makes work actually impossible.
    if req_contract and host_contract and host_contract != req_contract:
        out.update(state=BLOCKED, reason=BLOCKED_REASON, contract_matches=False,
                   actionable=self_updates, withholds_work=enforcing,
                   enforcing=enforcing,
                   detail=(f"Bundled execution-assignment contract {host_contract} "
                           f"cannot satisfy the server's {req_contract}. "
                           f"Every launch would be refused at admission."
                           + (" Update this Host through the Switchboard deployment."
                              if not self_updates else "")
                           + ("" if enforcing else
                              " Observe mode: work is not being withheld yet.")))
        return out

    # A host that never reports its contract is running a build from before
    # attestation existed. Unknown is not safe: that is exactly the state the
    # 0.4.15 host was in.
    if req_contract and not host_contract:
        out.update(state=BLOCKED, reason=BLOCKED_REASON, contract_matches=False,
                   actionable=self_updates, withholds_work=enforcing,
                   enforcing=enforcing,
                   detail=("Host does not report a contract fingerprint, so its "
                           "bundle predates attestation and cannot be trusted "
                           "to build a matching contract."
                           + (" Update this Host through the Switchboard deployment."
                              if not self_updates else "")
                           + ("" if enforcing else
                              " Observe mode: work is not being withheld yet.")))
        return out

    # Source-deployed service Hosts are kept current by the VM deployment, not
    # by the downloadable desktop package. Their source tree will naturally
    # have a different digest (and a legacy human version label) from the
    # promoted signed bundle. Once the wire contract agrees, that difference is
    # not an adapter update and must not produce a button that can never work.
    if not self_updates:
        out.update(required_version="", required_digest="", actionable=False,
                   detail="Managed by the Switchboard deployment; Host Adapter releases do not apply.")
        return out

    # The Host records one failed digest and deliberately refuses to retry it
    # forever. Keep that failure first-class in the projection: collapsing it
    # back to "update available" hides why the automatic path stopped. This is
    # intentionally after the contract gate: an incompatible Host remains
    # blocked even when its repair also failed.
    if update_error and req_digest and installed_digest != req_digest:
        out.update(state=UPDATE_FAILED, reason="host_update_failed",
                   actionable=True, detail=update_error)
        return out

    # Same contract, different bytes: the hand-patched-tree case. Not a launch
    # refusal, but it is not the promoted artifact either, so say so.
    if req_digest and installed_digest and installed_digest != req_digest:
        out.update(state=UPDATE_AVAILABLE, reason="host_bundle_digest_mismatch",
                   actionable=True,
                   detail=(f"Contract agrees, but the installed bundle "
                           f"({installed_digest[:16]}) is not the promoted "
                           f"release ({req_digest[:16]})."))
        return out

    if req_version and installed_version and installed_version != req_version:
        out.update(state=UPDATE_AVAILABLE, reason="host_release_behind",
                   actionable=True,
                   detail=f"Installed {installed_version}; {req_version} is promoted.")
        return out

    out["detail"] = f"Running the promoted release {req_version or installed_version}."
    return out


def withholds_work(host: Mapping[str, Any],
                   required: Optional[Mapping[str, Any]],
                   now: Optional[float] = None) -> bool:
    """True when this host must not be given new work."""
    return bool(evaluate(host, required, now).get("withholds_work"))
