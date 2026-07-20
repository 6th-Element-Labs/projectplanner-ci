"""Project-bound digest and notification application boundary (SEG-6)."""
from __future__ import annotations

from typing import Any, Iterable

import digest
import notify
import store

from switchboard.domain.projects.context import ProjectContext


def _project(ctx: ProjectContext) -> str:
    if not isinstance(ctx, ProjectContext):
        raise TypeError("ProjectContext is required")
    return ctx.project_id


def generate(ctx: ProjectContext, *, since_ts: float | None = None) -> dict[str, Any]:
    return digest.generate_digest(since_ts, project=_project(ctx))


def list_recent(ctx: ProjectContext, *, limit: int = 20) -> list[dict[str, Any]]:
    return store.list_digests(limit, project=_project(ctx))


def send_one(
    ctx: ProjectContext,
    *,
    digest_id: int,
    channels: Iterable[str] = ("slack", "email"),
) -> list[dict[str, Any]] | None:
    project = _project(ctx)
    row = next((item for item in store.list_digests(50, project=project)
                if item["id"] == digest_id), None)
    if row is None:
        return None
    label = store.get_meta("project", project=project) or project
    return notify.send(
        f"{label} — digest", row["content"], tuple(channels),
        project=project, kind="digest")


def send_test(ctx: ProjectContext) -> list[dict[str, Any]]:
    project = _project(ctx)
    return notify.send(
        f"{project} — test",
        "Notify is wired (test message from plan.taikunai.com).",
        project=project,
    )
