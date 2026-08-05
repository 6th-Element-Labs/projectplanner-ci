from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from path_setup import ROOT  # noqa: F401 - adds src/ to sys.path
from switchboard.contracts.compand import ProviderPriceTable
from switchboard.domain.compand import GatewayMode
from switchboard.services.compand.app import create_app
from switchboard.services.compand.settings import CompandGatewaySettings


CLIENT_A = "client-a-secret"
CLIENT_B = "client-b-secret"
UPSTREAM = "upstream-secret"


def output_item(call_id: str, output: str) -> dict[str, object]:
    return {"type": "function_call_output", "call_id": call_id, "output": output}


def command_receipt(call_id: str, output: str, **updates: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": "compand.command_result.v1",
        "call_id": call_id,
        "source_kind": "command_result",
        "trusted_adapter": True,
        "exit_status": 0,
        "content_type": "text/plain",
        "encoding": "utf-8",
        "truncated": False,
        "signed": False,
        "new_suffix": True,
        "byte_count": len(output.encode()),
        "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
    }
    value.update(updates)
    return value


def request_body(items: list[object]) -> bytes:
    return json.dumps(
        {
            "model": "gpt-5.4",
            "input": items,
            "store": False,
            "stream": True,
            "reasoning": {"effort": "high"},
        },
        separators=(",", ":"),
    ).encode()


class ProviderFixture:
    def __init__(self) -> None:
        self.forwarded: list[bytes] = []
        self.correlations: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/responses/input_tokens":
            payload = json.loads(request.content)
            output = payload["input"][-1]["output"]
            count = 4 if "[repeated " in output else 20
            return httpx.Response(
                200,
                json={"object": "response.input_tokens", "input_tokens": count},
            )
        self.forwarded.append(request.content)
        self.correlations.append(request.headers.get("x-compand-correlation-id", ""))
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            stream=_OneChunk(b"{}"),
        )


class _OneChunk(httpx.AsyncByteStream):
    def __init__(self, body: bytes) -> None:
        self.body = body

    async def __aiter__(self):
        yield self.body


class Enforce25CompandRuntimeTest(unittest.TestCase):
    def make_app(self, db_path: str, provider: ProviderFixture, **changes: object):
        values: dict[str, object] = {
            "upstream_origin": "https://api.openai.com",
            "upstream_api_key": UPSTREAM,
            "client_credentials": {"tenant-a": CLIENT_A, "tenant-b": CLIENT_B},
            "mode": GatewayMode.ENFORCE,
            "source_version": "ENFORCE-25-test",
            "frozen_tuple_config_attested": True,
            "state_db_path": db_path,
            "artifact_retention_seconds": 3600,
            "capability_secret": "test-capability-secret",
        }
        values.update(changes)
        return create_app(
            CompandGatewaySettings(**values),
            transport=httpx.MockTransport(provider),
        )

    def post(
        self,
        client: TestClient,
        body: bytes,
        receipt: dict[str, object] | None,
        *,
        token: str = CLIENT_A,
        session: str = "session-1",
    ):
        headers = {
            "authorization": f"Bearer {token}",
            "content-type": "application/json",
            "accept": "text/event-stream",
            "x-compand-session-id": session,
        }
        if receipt is not None:
            headers["x-compand-command-receipt"] = json.dumps(
                receipt, separators=(",", ":")
            )
        return client.post("/v1/responses", content=body, headers=headers)

    def test_enforce_counts_mutates_forwards_and_recovers_exact_source(self) -> None:
        output = "same line\nsame line\nsame line\n"
        body = request_body([output_item("call-1", output)])
        provider = ProviderFixture()
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "state.sqlite"
            app = self.make_app(str(db_path), provider)
            with TestClient(app) as client:
                response = self.post(client, body, command_receipt("call-1", output))
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["x-compand-outcome"], "enforced_cheaper")
                self.assertEqual(response.headers["x-compand-transformed"], "true")
                forwarded = json.loads(provider.forwarded[-1])
                self.assertEqual(
                    forwarded["input"][-1]["output"],
                    "same line [repeated 3 times]\n",
                )
                capability = response.headers["x-compand-recovery-capability"]
                recovered = client.get(
                    f"/compand/v1/artifacts/{capability}",
                    headers={
                        "authorization": f"Bearer {CLIENT_A}",
                        "x-compand-session-id": "session-1",
                    },
                )
                self.assertEqual(recovered.content, output.encode())
                denied = client.get(
                    f"/compand/v1/artifacts/{capability}",
                    headers={
                        "authorization": f"Bearer {CLIENT_B}",
                        "x-compand-session-id": "session-1",
                    },
                )
                self.assertEqual(denied.status_code, 404)
                self.assertNotIn(output.encode(), db_path.read_bytes())

    def test_scan_measures_but_forwards_original_bytes(self) -> None:
        output = "private\nprivate\n"
        body = request_body([output_item("call-scan", output)])
        provider = ProviderFixture()
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(
                str(Path(temp) / "state.sqlite"), provider, mode=GatewayMode.SCAN
            )
            with TestClient(app) as client:
                response = self.post(
                    client, body, command_receipt("call-scan", output)
                )
                self.assertEqual(response.headers["x-compand-outcome"], "scan_cheaper")
                self.assertEqual(response.headers["x-compand-transformed"], "false")
                self.assertEqual(provider.forwarded[-1], body)
                state = app.state.compand_gateway_runtime.repository.debug_serialized_state()
                self.assertNotIn("private", state)

    def test_retry_and_continuation_reuse_frozen_provider_history(self) -> None:
        first = "alpha\nalpha\n"
        second = "beta\nbeta\n"
        first_item = output_item("call-1", first)
        first_body = request_body([first_item])
        provider = ProviderFixture()
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(str(Path(temp) / "state.sqlite"), provider)
            with TestClient(app) as client:
                initial = self.post(
                    client, first_body, command_receipt("call-1", first)
                )
                self.assertEqual(initial.status_code, 200)
                retry = self.post(client, first_body, None)
                self.assertEqual(retry.headers["x-compand-outcome"], "frozen_retry")
                self.assertEqual(provider.forwarded[-1], provider.forwarded[-2])

                second_item = output_item("call-2", second)
                continuation = self.post(
                    client,
                    request_body([first_item, second_item]),
                    command_receipt("call-2", second),
                )
                self.assertEqual(continuation.headers["x-compand-transformed"], "true")
                forwarded = json.loads(provider.forwarded[-1])["input"]
                self.assertEqual(forwarded[0]["output"], "alpha [repeated 2 times]\n")
                self.assertEqual(forwarded[1]["output"], "beta [repeated 2 times]\n")

    def test_failed_or_untrusted_receipt_is_byte_preserving(self) -> None:
        output = "failure\nfailure\n"
        body = request_body([output_item("call-fail", output)])
        provider = ProviderFixture()
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(str(Path(temp) / "state.sqlite"), provider)
            with TestClient(app) as client:
                response = self.post(
                    client,
                    body,
                    command_receipt("call-fail", output, exit_status=1),
                )
                self.assertEqual(response.headers["x-compand-outcome"], "ineligible_receipt")
                self.assertEqual(provider.forwarded[-1], body)

    def test_zero_retention_disables_recovery_without_blocking_transform(self) -> None:
        output = "zero\nzero\n"
        provider = ProviderFixture()
        with tempfile.TemporaryDirectory() as temp:
            app = self.make_app(
                str(Path(temp) / "state.sqlite"),
                provider,
                artifact_retention_seconds=0,
            )
            with TestClient(app) as client:
                response = self.post(
                    client,
                    request_body([output_item("call-zero", output)]),
                    command_receipt("call-zero", output),
                )
                self.assertEqual(response.headers["x-compand-transformed"], "true")
                self.assertNotIn("x-compand-recovery-capability", response.headers)

    def test_price_table_rejects_boolean_rates_and_blank_authority(self) -> None:
        common = {
            "provider": "openai",
            "model": "gpt-5.4",
            "effective_date": "2026-08-04",
            "input_usd_per_million_tokens": 10,
            "cached_input_usd_per_million_tokens": 1,
            "source": "dated-provider-table",
        }
        for changes in (
            {"input_usd_per_million_tokens": True},
            {"cached_input_usd_per_million_tokens": False},
            {"source": ""},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                ProviderPriceTable.model_validate({**common, **changes})


if __name__ == "__main__":
    unittest.main()
