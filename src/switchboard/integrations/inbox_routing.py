"""Project routing policy for messages entering the shared inbox.

Routing is source-independent: IMAP uses it today, while future mail or messaging
adapters can apply the same plus-address, sender-domain, and allowlist policy.
"""
from __future__ import annotations

import email.utils
import logging
import os
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

import store


log = logging.getLogger("switchboard.integrations.inbox_routing")


@dataclass(frozen=True)
class RouteDecision:
    accepted: bool
    project: str | None
    reason: str


@dataclass(frozen=True)
class RouteIndex:
    domains: Mapping[str, str]
    plus_addresses: Mapping[str, str]
    projects: frozenset[str]
    env_routes: str


_index_lock = threading.Lock()
_route_index: RouteIndex | None = None


def invalidate_routes() -> None:
    """Atomically discard the published index after communications config changes."""
    global _route_index
    with _index_lock:
        _route_index = None


def _build_index(env_routes: str) -> RouteIndex:
    """Merge deploy bootstrap routes with operator-managed project associations.

    The web-managed map wins conflicts because it is the live operator surface.
    Failure to read that map is visible but does not disable the environment map.
    """
    routes: dict[str, str] = {}
    valid_projects = frozenset(store.project_ids())
    for part in env_routes.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        domain, project = part.split("=", 1)
        domain = domain.strip().lstrip("@").lower()
        project = project.strip()
        if not domain or "@" in domain or " " in domain or "." not in domain:
            raise ValueError(f"invalid PM_INBOX_ROUTES domain: {domain!r}")
        if domain in routes and routes[domain] != project:
            raise ValueError(f"ambiguous PM_INBOX_ROUTES domain: {domain}")
        if domain and project in valid_projects:
            routes[domain] = project
        elif domain and project:
            log.warning("inbox routing maps %s -> unknown project %r; ignoring", domain, project)
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
    except Exception as exc:
        # A partial index could misroute mail. Publish nothing and fail the message closed.
        raise RuntimeError(f"could not build complete inbox route index: {exc}") from exc
    plus_addresses = {comms.plus_address(project).lower(): project for project in valid_projects}
    return RouteIndex(domains=MappingProxyType(routes),
                      plus_addresses=MappingProxyType(plus_addresses),
                      projects=valid_projects, env_routes=env_routes)


def route_index() -> RouteIndex:
    """Return the immutable cached index; project databases are read only on rebuild."""
    global _route_index
    env_routes = os.environ.get("PM_INBOX_ROUTES") or ""
    current = _route_index
    if current is not None and current.env_routes == env_routes:
        return current
    with _index_lock:
        current = _route_index
        if current is None or current.env_routes != env_routes:
            current = _build_index(env_routes)
            _route_index = current
        return current


def routes_map() -> dict[str, str]:
    """Compatibility snapshot of the cached domain index."""
    return dict(route_index().domains)


def plus_projects(recipients: str | None, index: RouteIndex) -> tuple[set[str], bool]:
    """Return routed projects and whether the shared mailbox had an unknown plus tag."""
    projects: set[str] = set()
    unknown = False
    known = index.plus_addresses
    shared = next(iter(known), "")
    shared_local, _, shared_host = shared.partition("@")
    shared_base = shared_local.split("+", 1)[0]
    for _name, address in email.utils.getaddresses([recipients or ""]):
        address = (address or "").strip().lower()
        local, sep, host = address.partition("@")
        if sep and host == shared_host and local.startswith(shared_base + "+"):
            project = known.get(address)
            if project:
                projects.add(project)
            else:
                unknown = True
    return projects, unknown


def domain_project(sender: str | None, index: RouteIndex) -> str | None:
    """Resolve an exact or parent-domain sender route, rejecting unknown projects."""
    address = email.utils.parseaddr(sender or "")[1].lower()
    domain = address.split("@", 1)[1] if "@" in address else ""
    if not domain:
        return None
    routes = index.domains
    # Direct dictionary probes for each DNS suffix keep lookup independent of the
    # number of projects/routes (normally only 2-4 labels for an email domain).
    labels = domain.split(".")
    project = next((routes[suffix] for suffix in
                    (".".join(labels[offset:]) for offset in range(max(1, len(labels) - 1)))
                    if suffix in routes), None)
    if not project:
        return None
    return project


def route_decision(sender: str | None, recipients: str | None) -> RouteDecision:
    """Resolve exactly one explicit route, otherwise return a quarantine decision."""
    try:
        index = route_index()
    except Exception:
        log.exception("inbox routing index unavailable")
        return RouteDecision(False, None, "route_index_unavailable")
    plus_routes, unknown_plus = plus_projects(recipients, index)
    if unknown_plus:
        return RouteDecision(False, None, "unknown_plus_tag")
    if plus_routes:
        if len(plus_routes) > 1:
            return RouteDecision(False, None, "ambiguous_plus_tags")
        return RouteDecision(True, next(iter(plus_routes)), "plus_address")
    project = domain_project(sender, index)
    if project:
        return RouteDecision(True, project, "sender_domain")
    return RouteDecision(False, None, "unmapped_sender")


def route(sender: str | None, recipients: str | None) -> tuple[bool, str | None]:
    """Compatibility pair for callers that do not need the quarantine reason."""
    decision = route_decision(sender, recipients)
    return decision.accepted, decision.project
