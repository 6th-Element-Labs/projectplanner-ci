"""Read the composed browser source for static UI contract tests."""
from pathlib import Path


FRONTEND_SOURCES = (
    "static/js/api.js",
    "static/js/state.js",
    "static/js/board.js",
    "static/js/mission.js",
    "static/js/runner-session.js",
    "static/js/proof-console.js",
    "static/js/settings.js",
    "static/app.js",
)


def read_frontend_source(root):
    base = Path(root)
    return "\n".join((base / relative).read_text(encoding="utf-8")
                     for relative in FRONTEND_SOURCES)
