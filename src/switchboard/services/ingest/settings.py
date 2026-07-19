from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class IngestServiceSettings:
    service_name: str
    host: str
    port: int

    @classmethod
    def from_env(cls) -> "IngestServiceSettings":
        return cls(
            (os.environ.get("SWITCHBOARD_INGEST_SERVICE_NAME") or "switchboard-ingest").strip(),
            (os.environ.get("SWITCHBOARD_INGEST_HOST") or "127.0.0.1").strip(),
            int(os.environ.get("SWITCHBOARD_INGEST_PORT") or "8126"),
        )
