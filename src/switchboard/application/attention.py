"""Authoritative attention-request use cases shared by REST and Agent Host ingress."""
from __future__ import annotations

from typing import Any, Mapping, Optional

from switchboard.domain.projects.context import ProjectContext
from switchboard.storage.repositories.attention import (
    AttentionRepository,
    AttentionStoreError,
    default_attention_repository,
)


class AttentionService:
    def __init__(self, repository: AttentionRepository = default_attention_repository) -> None:
        self._repository = repository

    @staticmethod
    def _project(ctx: ProjectContext) -> str:
        if not isinstance(ctx, ProjectContext):
            raise TypeError("ProjectContext is required")
        return ctx.project_id

    def list_operator_queue(
        self, ctx: ProjectContext, *, limit: int = 100, offset: int = 0,
    ) -> dict[str, Any]:
        project = self._project(ctx)
        items = self._repository.list_requests(
            project=project, limit=limit, offset=offset)
        return {"project": project, "count": self._repository.count_requests(
            project=project), "items": items}

    def count_operator_queue(self, ctx: ProjectContext) -> dict[str, Any]:
        project = self._project(ctx)
        return {"project": project,
                "count": self._repository.count_requests(project=project)}

    def get_request(self, ctx: ProjectContext, request_id: str) -> dict[str, Any]:
        return self._repository.get_request(
            request_id, project=self._project(ctx))

    def decide(
        self, ctx: ProjectContext, request_id: str, data: Mapping[str, Any],
        *, actor: str,
    ) -> dict[str, Any]:
        return self._repository.record_decision(
            request_id, data, actor=actor, actor_principal_id=ctx.principal_id,
            project=self._project(ctx))

    def upsert_request(
        self, ctx: ProjectContext, data: Mapping[str, Any], *, actor: str,
    ) -> dict[str, Any]:
        return self._repository.create_request(
            data, actor=actor, project=self._project(ctx))

    def claim_decision(
        self, ctx: ProjectContext, *, host_id: str, actor: str,
        provider: str = "", request_id: str = "",
    ) -> Optional[dict[str, Any]]:
        return self._repository.claim_decision(
            project=self._project(ctx), host_id=host_id, actor=actor,
            provider=provider, request_id=request_id)

    def acknowledge_delivery(
        self, ctx: ProjectContext, request_id: str, *, expected_version: int,
        host_id: str, actor: str, receipt: Any = None, error: str = "",
    ) -> dict[str, Any]:
        project = self._project(ctx)
        request = self._repository.get_request(request_id, project=project)
        if request["host_id"] != host_id:
            raise AttentionStoreError(
                "attention_host_mismatch",
                "request is bound to a different Agent Host")
        return self._repository.transition(
            request_id, expected_version=expected_version,
            target_status="failed" if error else "resolved", actor=actor,
            reason=error, delivery_receipt=receipt,
            project=project)


default_attention_service = AttentionService()

__all__ = ["AttentionService", "default_attention_service"]
