from __future__ import annotations

import uvicorn
from .app import create_app
from .settings import IngestServiceSettings


def main() -> None:
    cfg = IngestServiceSettings.from_env()
    uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port)


if __name__ == "__main__":
    main()
