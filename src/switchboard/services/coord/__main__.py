"""Local entrypoint: ``python -m switchboard.services.coord``."""
from __future__ import annotations

import uvicorn

from switchboard.services.coord.app import create_app
from switchboard.services.coord.settings import CoordServiceSettings


def main() -> None:
    settings = CoordServiceSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
