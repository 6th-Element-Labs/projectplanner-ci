"""Local entrypoint: ``python -m switchboard.services.tasks``."""
from __future__ import annotations

import uvicorn

from switchboard.services.tasks.app import create_app
from switchboard.services.tasks.settings import TasksServiceSettings


def main() -> None:
    settings = TasksServiceSettings.from_env()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
