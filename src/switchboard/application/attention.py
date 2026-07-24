"""Authoritative attention-request use cases shared by REST and Agent Host ingress."""
from __future__ import annotations

import time
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
        if not ctx.principal_id:
            raise AttentionStoreError(
                "attention_principal_unbound",
                "an authenticated principal is required to decide a request")
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
        runner_session_id: str = "", work_session_id: str = "",
    ) -> Optional[dict[str, Any]]:
        project = self._project(ctx)
        if request_id:
            request = self._repository.get_request(request_id, project=project)
            self._assert_delivery_binding(
                request, host_id=host_id, provider=provider,
                runner_session_id=runner_session_id,
                work_session_id=work_session_id)
        return self._repository.claim_decision(
            project=project, host_id=host_id, actor=actor,
            provider=provider, request_id=request_id,
            runner_session_id=runner_session_id,
            work_session_id=work_session_id)

    @staticmethod
    def _assert_delivery_binding(
        request: Mapping[str, Any], *, host_id: str, provider: str = "",
        runner_session_id: str = "", work_session_id: str = "",
    ) -> None:
        bindings = {
            "host_id": host_id,
            "provider": provider,
            "runner_session_id": runner_session_id,
            "work_session_id": work_session_id,
        }
        mismatches = {
            field: {"expected": request.get(field), "received": received}
            for field, received in bindings.items()
            if str(request.get(field) or "") != str(received or "")
        }
        if mismatches:
            raise AttentionStoreError(
                "attention_binding_mismatch",
                "request delivery binding does not match the bound provider session",
                details={"mismatches": mismatches})

    def acknowledge_delivery(
        self, ctx: ProjectContext, request_id: str, *, expected_version: int,
        host_id: str, actor: str, receipt: Any = None, error: str = "",
        provider: str = "", runner_session_id: str = "",
        work_session_id: str = "",
    ) -> dict[str, Any]:
        project = self._project(ctx)
        request = self._repository.get_request(request_id, project=project)
        self._assert_delivery_binding(
            request, host_id=host_id, provider=provider,
            runner_session_id=runner_session_id,
            work_session_id=work_session_id)
        if (request.get("expires_at") is not None
                and float(request["expires_at"]) <= time.time()
                and request.get("status") == "delivering"):
            terminal = self._repository.transition(
                request_id, expected_version=expected_version,
                target_status="orphaned", actor=actor,
                reason="delivery_after_expiry_rejected", project=project)
            raise AttentionStoreError(
                "attention_request_expired",
                "delivery arrived after the request expiry",
                details={"current_status": terminal["status"],
                         "current_version": terminal["version"]})
        if not error and (not isinstance(receipt, Mapping) or not receipt):
            raise AttentionStoreError(
                "attention_delivery_receipt_required",
                "successful delivery requires a non-empty provider receipt")
        return self._repository.transition(
            request_id, expected_version=expected_version,
            target_status="failed" if error else "resolved", actor=actor,
            reason=error or "delivery_receipt_recorded", delivery_receipt=receipt,
            project=project)


default_attention_service = AttentionService()

__all__ = ["AttentionService", "default_attention_service"]
