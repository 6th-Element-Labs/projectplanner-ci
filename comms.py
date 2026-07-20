"""Per-project communications config (UI-14 — Settings → Communications).

Backs the operator-facing Communications surface. All state lives in each project's `meta`
table (via store.get_meta/set_meta) — no new tables, no store.py growth. Two halves:

INBOUND (routing): each project owns a list of sender DOMAINS that route inbound mail to it.
This is the UI-13 domain→project map (`switchboard.integrations.inbox_routing`), now editable
from the web instead of the deploy-level PM_INBOX_ROUTES env — so an operator can associate
@client.com with a project with no .env edit. `plus_address(project)` is the zero-config routing
path for the same mailbox.

OUTBOUND (digest/notify): each project owns its own digest + notify recipient lists and a digest
cadence. notify.py reads these first and falls back to the global .env list (PM_NOTIFY_EMAIL_TO),
so today's single-tenant behavior is preserved when a project sets nothing.

Domains are stored lowercased with any leading '@' stripped, deduped. A domain may map to at most
one project: set-time validation rejects a domain already owned by another project (fail-loud, so
a misroute is never silent), and persisted_routes() keeps a first-writer-wins guard as backstop.
"""
import logging
import os

import store
from store import get_meta, set_meta

log = logging.getLogger("comms")

INBOUND_KEY = "comms_inbound_domains"
DIGEST_KEY = "comms_digest_recipients"
NOTIFY_KEY = "comms_notify_recipients"
CADENCE_KEY = "comms_digest_cadence"

CADENCE_OPTIONS = ["off", "daily", "weekly", "monthly"]
DEFAULT_CADENCE = "weekly"


# ---- normalization ---------------------------------------------------------

def _norm_domains(domains):
    """Lowercase, strip a leading '@', dedupe. Returns a clean list preserving first-seen order."""
    items = domains if isinstance(domains, (list, tuple)) else str(domains or "").split(",")
    out, seen = [], set()
    for d in items:
        d = (d or "").strip().lstrip("@").lower()
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out


def _norm_addrs(addrs):
    """Trim/dedupe a str-or-list of email addresses (case-insensitive dedupe, keep original case)."""
    items = addrs if isinstance(addrs, (list, tuple)) else str(addrs or "").split(",")
    out, seen = [], set()
    for a in items:
        a = (a or "").strip()
        if a and a.lower() not in seen:
            seen.add(a.lower())
            out.append(a)
    return out


def _valid_domain(d):
    return bool(d) and "@" not in d and " " not in d and "." in d


def _valid_addr(a):
    return bool(a) and "@" in a and " " not in a and "." in a.split("@", 1)[1]


# ---- plus-address ----------------------------------------------------------

def _mailbox():
    """(local, host) for the shared inbox — derived from PM_IMAP_USER (e.g. plan@taikunai.com),
    overridable via PM_INBOX_PLUS_HOST. Defaults to plan@taikunai.com so the display is stable
    before the mailbox env is set."""
    user = (os.environ.get("PM_IMAP_USER") or "").strip()
    local, _, host = user.partition("@")
    local = local or "plan"
    host = (os.environ.get("PM_INBOX_PLUS_HOST") or host or "taikunai.com").strip()
    return local, host


def plus_address(project):
    """plan+<project>@<host> — the zero-config routing path (UI-13). Same mailbox, +tag = board."""
    local, host = _mailbox()
    return f"{local}+{project}@{host}"


# ---- inbound (routing) -----------------------------------------------------

def inbound_domains(project):
    return _norm_domains(get_meta(INBOUND_KEY, [], project=project) or [])


def _domain_owners(exclude=None):
    """{domain: project} across every OTHER project — used to reject a domain already claimed."""
    owners = {}
    for pid in store.project_ids():
        if pid == exclude:
            continue
        for d in inbound_domains(pid):
            owners.setdefault(d, pid)
    return owners


def set_inbound_domains(project, domains):
    """Persist this project's associated inbound domains. Rejects malformed domains and any domain
    already owned by another project (a domain routes to exactly one board). Returns the stored list
    or {'error': ...}."""
    clean = _norm_domains(domains)
    bad = [d for d in clean if not _valid_domain(d)]
    if bad:
        return {"error": f"invalid domain(s): {', '.join(bad)}"}
    owners = _domain_owners(exclude=project)
    conflicts = [f"{d} → {owners[d]}" for d in clean if d in owners]
    if conflicts:
        return {"error": "domain already associated with another project: " + "; ".join(conflicts)}
    set_meta(INBOUND_KEY, clean, project=project)
    # The shared-inbox hot path reads one immutable routing index. Invalidate it only
    # after the configuration write succeeds; the next message rebuilds the complete
    # index before publishing it to readers.
    try:
        from switchboard.integrations import inbox_routing
        inbox_routing.invalidate_routes()
    except ImportError:
        pass
    return {"inbound_domains": clean}


def persisted_routes():
    """Merged {domain: project} over every project's associated domains — the web-managed half of
    shared inbox routing map. First-writer-wins with a loud warning if two boards ever claim the
    same domain (set-time validation normally prevents this)."""
    out = {}
    for pid in sorted(store.project_ids()):
        for d in inbound_domains(pid):
            if d in out and out[d] != pid:
                log.warning("comms routing conflict: domain %s claimed by %s and %s; keeping %s",
                            d, out[d], pid, out[d])
                continue
            out.setdefault(d, pid)
    return out


# ---- outbound (digest / notify) --------------------------------------------

def outbound(project):
    cad = get_meta(CADENCE_KEY, DEFAULT_CADENCE, project=project) or DEFAULT_CADENCE
    if cad not in CADENCE_OPTIONS:
        cad = DEFAULT_CADENCE
    return {
        "digest_recipients": _norm_addrs(get_meta(DIGEST_KEY, [], project=project) or []),
        "notify_recipients": _norm_addrs(get_meta(NOTIFY_KEY, [], project=project) or []),
        "cadence": cad,
    }


def recipients_for(project, kind):
    """Per-project recipient list for kind in {'digest','notify'}; [] if unset (caller falls back
    to the global PM_NOTIFY_EMAIL_TO). notify.py calls this."""
    key = DIGEST_KEY if kind == "digest" else NOTIFY_KEY
    return _norm_addrs(get_meta(key, [], project=project) or [])


def global_fallback_recipients():
    return _norm_addrs(os.environ.get("PM_NOTIFY_EMAIL_TO"))


def set_outbound(project, digest_recipients=None, notify_recipients=None, cadence=None):
    """Persist outbound recipients / cadence (only the provided fields). Rejects malformed
    addresses / unknown cadence. Returns the stored values or {'error': ...}."""
    if cadence is not None and cadence not in CADENCE_OPTIONS:
        return {"error": f"cadence must be one of: {', '.join(CADENCE_OPTIONS)}"}
    for label, val in (("digest", digest_recipients), ("notify", notify_recipients)):
        if val is None:
            continue
        bad = [a for a in _norm_addrs(val) if not _valid_addr(a)]
        if bad:
            return {"error": f"invalid {label} recipient(s): {', '.join(bad)}"}
    if digest_recipients is not None:
        set_meta(DIGEST_KEY, _norm_addrs(digest_recipients), project=project)
    if notify_recipients is not None:
        set_meta(NOTIFY_KEY, _norm_addrs(notify_recipients), project=project)
    if cadence is not None:
        set_meta(CADENCE_KEY, cadence, project=project)
    return outbound(project)


# ---- config surface (API) --------------------------------------------------

def get_config(project):
    """Everything the Settings → Communications screen needs for `project`."""
    import notify  # lazy: notify imports comms for recipient resolution
    out = outbound(project)
    fallback = global_fallback_recipients()
    return {
        "project": project,
        "inbound": {
            "plus_address": plus_address(project),
            "domains": inbound_domains(project),
        },
        "outbound": {
            "digest_recipients": out["digest_recipients"],
            "notify_recipients": out["notify_recipients"],
            "cadence": out["cadence"],
            "cadence_options": CADENCE_OPTIONS,
        },
        "global_fallback": {
            "notify_to": fallback,
            "configured": bool(fallback),
        },
        "channels": notify.status(),
        # Advisory: per-project digest cadence is stored here; the scheduled digest timer is still
        # global, so cadence records intent and gates 'off'. Recipients are the functional half.
        "cadence_is_advisory": True,
    }


def update_config(body, project, actor=""):
    """Apply a Communications settings edit (inbound domains and/or outbound recipients/cadence).
    Only provided keys change. Returns {'config', 'audit'} or {'error': ...}."""
    body = body or {}
    changes = {}
    inbound = body.get("inbound") if isinstance(body.get("inbound"), dict) else body
    outbound_body = body.get("outbound") if isinstance(body.get("outbound"), dict) else body

    if "domains" in inbound or "inbound_domains" in body:
        raw = inbound.get("domains", body.get("inbound_domains"))
        res = set_inbound_domains(project, raw)
        if res.get("error"):
            return res
        changes["inbound_domains"] = res["inbound_domains"]

    has_outbound = any(k in outbound_body for k in ("digest_recipients", "notify_recipients", "cadence"))
    if has_outbound:
        res = set_outbound(
            project,
            digest_recipients=outbound_body.get("digest_recipients") if "digest_recipients" in outbound_body else None,
            notify_recipients=outbound_body.get("notify_recipients") if "notify_recipients" in outbound_body else None,
            cadence=outbound_body.get("cadence") if "cadence" in outbound_body else None,
        )
        if res.get("error"):
            return res
        changes["outbound"] = res

    return {"config": get_config(project), "audit": {"actor": actor, "changes": changes}}
