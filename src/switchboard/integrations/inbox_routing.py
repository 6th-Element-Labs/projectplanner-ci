"""Project routing policy for messages entering the shared inbox.

Routing is source-independent: IMAP uses it today, while future mail or messaging
adapters can apply the same plus-address, sender-domain, and allowlist policy.
"""
from __future__ import annotations

import email.utils
import logging
import os

import store


log = logging.getLogger("switchboard.integrations.inbox_routing")


def allow_sender(sender: str | None) -> bool:
    """Apply the fallback allowlist for a sender with no explicit project route."""
    allowlist = (os.environ.get("PM_INBOX_ALLOWLIST") or "").strip()
    if not allowlist:
        return True
    normalized = (sender or "").lower()
    return any(item.strip().lower() in normalized
               for item in allowlist.split(",") if item.strip())


def routes_map() -> dict[str, str]:
    """Merge deploy bootstrap routes with operator-managed project associations.

    The web-managed map wins conflicts because it is the live operator surface.
    Failure to read that map is visible but does not disable the environment map.
    """
    routes: dict[str, str] = {}
    for part in (os.environ.get("PM_INBOX_ROUTES") or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        domain, project = part.split("=", 1)
        domain = domain.strip().lstrip("@").lower()
        project = project.strip()
        if domain and project:
            routes[domain] = project
    try:
        import comms

        for domain, project in comms.persisted_routes().items():
            if routes.get(domain) not in (None, project):
                log.info(
                    "inbox routing: web association %s -> %s overrides PM_INBOX_ROUTES -> %s",
                    domain,
                    project,
                    routes[domain],
                )
            routes[domain] = project
    except Exception as exc:  # preserve env routing if persisted config is unavailable
        log.warning(
            "inbox routing: could not load web-managed routes (%s); using env map only",
            exc,
        )
    return routes


def plus_project(recipients: str | None, valid_projects: set[str]) -> str | None:
    """Resolve a valid project from any ``local+project@host`` recipient."""
    for _name, address in email.utils.getaddresses([recipients or ""]):
        local = (address or "").split("@", 1)[0]
        if "+" in local:
            tag = local.split("+", 1)[1].strip().lower()
            if tag in valid_projects:
                return tag
    return None


def domain_project(sender: str | None, valid_projects: set[str]) -> str | None:
    """Resolve an exact or parent-domain sender route, rejecting unknown projects."""
    address = email.utils.parseaddr(sender or "")[1].lower()
    domain = address.split("@", 1)[1] if "@" in address else ""
    if not domain:
        return None
    routes = routes_map()
    project = routes.get(domain)
    if not project:
        for routed_domain, routed_project in routes.items():
            if domain == routed_domain or domain.endswith("." + routed_domain):
                project = routed_project
                break
    if not project:
        return None
    if project not in valid_projects:
        log.warning(
            "inbox routing maps %s -> unknown project %r; ignoring",
            domain,
            project,
        )
        return None
    return project


def route(sender: str | None, recipients: str | None) -> tuple[bool, str]:
    """Return ``(accepted, project)`` using the stable inbox precedence policy."""
    valid_projects = set(store.project_ids())
    project = plus_project(recipients, valid_projects)
    if project:
        return True, project
    project = domain_project(sender, valid_projects)
    if project:
        return True, project
    return allow_sender(sender), store.DEFAULT_PROJECT
