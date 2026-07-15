"""Local entrypoint: ``python -m switchboard.services.auth``."""
from __future__ import annotations

import uvicorn

from switchboard.services.auth.app import create_app
from switchboard.services.auth.settings import AuthServiceSettings


def main() -> None:
    settings = AuthServiceSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
