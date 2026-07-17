"""Local entrypoint: ``python -m switchboard.services.deliverables``."""
from __future__ import annotations

import uvicorn

from switchboard.services.deliverables.app import create_app
from switchboard.services.deliverables.settings import DeliverablesServiceSettings


def main() -> None:
    settings = DeliverablesServiceSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
