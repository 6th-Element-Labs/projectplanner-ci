"""Local entrypoint: ``python -m switchboard.services._skeleton``."""
from __future__ import annotations

import uvicorn

from switchboard.services._skeleton.app import app
from switchboard.services._skeleton.settings import SkeletonSettings


def main() -> None:
    settings = SkeletonSettings.from_env()
    uvicorn.run(app, host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
