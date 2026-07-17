"""Server-side terminal screen model for snapshot-on-attach (UI-25).

UI-24 shipped the browser PTY relay with byte-replay only: a bounded ring of
the most recent output frames is replayed to each newly-attached browser. That
works for streaming/log output but blanks on a full-screen TUI (Codex, vim,
top): once the ring rolls past the app's last full-screen paint, and the app is
idle (emitting nothing), a new viewer receives no complete frame and sees a
blank screen. The app only repaints on its own state changes, not on attach or
resize, so nothing fills the gap.

This module reconstructs the *current screen* from the PTY byte stream with a
headless terminal emulator (pyte), then reserializes it to a single ANSI frame.
The relay feeds every output byte into a per-session ``ScreenModel`` and, on
attach, hands the new browser that full frame instead of the raw ring — the
same technique tmux/ttyd/gotty use for instant reattach.

pyte is an optional dependency: if it is not importable the model degrades to
``ok=False`` and the relay transparently falls back to byte-replay, so the relay
never hard-depends on it.
"""
from __future__ import annotations

try:  # optional dependency; relay falls back to byte-replay when absent
    import pyte

    _HAVE_PYTE = True
except Exception:  # pragma: no cover - exercised only where pyte is missing
    _HAVE_PYTE = False

DEFAULT_COLS = 80
DEFAULT_ROWS = 24
_MAX_DIM = 1000

# pyte stores the 8 base colours by name; map them to SGR offsets.
_BASE_COLORS = {
    "black": 0, "red": 1, "green": 2, "brown": 3, "yellow": 3,
    "blue": 4, "magenta": 5, "cyan": 6, "white": 7,
}


def have_pyte() -> bool:
    return _HAVE_PYTE


def _color_codes(value: str, base: int) -> list[str]:
    """Map a pyte colour string to SGR codes. base=30 fg, 40 bg."""
    if not value or value == "default":
        return [str(base + 9)]  # 39 / 49
    if value in _BASE_COLORS:
        return [str(base + _BASE_COLORS[value])]  # 30-37 / 40-47
    if value.startswith("bright") and value[6:] in _BASE_COLORS:
        return [str(base + 60 + _BASE_COLORS[value[6:]])]  # 90-97 / 100-107
    if len(value) == 6:  # pyte encodes 256/true-colour as 6 hex digits
        try:
            r = int(value[0:2], 16)
            g = int(value[2:4], 16)
            b = int(value[4:6], 16)
            return [str(base + 8), "2", str(r), str(g), str(b)]  # 38;2;r;g;b
        except ValueError:
            pass
    return [str(base + 9)]


def _sgr(char) -> str:
    codes = ["0"]
    if getattr(char, "bold", False):
        codes.append("1")
    if getattr(char, "italics", False):
        codes.append("3")
    if getattr(char, "underscore", False):
        codes.append("4")
    if getattr(char, "blink", False):
        codes.append("5")
    if getattr(char, "reverse", False):
        codes.append("7")
    if getattr(char, "strikethrough", False):
        codes.append("9")
    codes += _color_codes(getattr(char, "fg", "default"), 30)
    codes += _color_codes(getattr(char, "bg", "default"), 40)
    return "\x1b[" + ";".join(codes) + "m"


def serialize_screen(screen) -> str:
    """Reserialize a pyte screen to a self-contained ANSI full-frame repaint.

    Uses absolute cursor positioning and resets attributes per row, so it
    renders correctly on a fresh xterm regardless of prior state, and restores
    the cursor to the app's position so subsequent live (relative) updates from
    the app still align.
    """
    out = ["\x1b[?25l\x1b[0m\x1b[2J\x1b[H"]  # hide cursor, reset attrs, clear, home
    last_key = None
    for y in range(screen.lines):
        out.append(f"\x1b[{y + 1};1H")
        row = screen.buffer[y]
        for x in range(screen.columns):
            char = row[x]
            key = (
                char.fg, char.bg, char.bold, char.italics,
                char.underscore, char.reverse, char.strikethrough, char.blink,
            )
            if key != last_key:
                out.append(_sgr(char))
                last_key = key
            out.append(char.data or " ")
        out.append("\x1b[0m")
        last_key = None
    cx = min(max(0, screen.cursor.x), screen.columns - 1)
    cy = min(max(0, screen.cursor.y), screen.lines - 1)
    out.append(f"\x1b[{cy + 1};{cx + 1}H")
    if not getattr(screen.cursor, "hidden", False):
        out.append("\x1b[?25h")
    return "".join(out)


class ScreenModel:
    """Per-session headless screen fed by the PTY byte stream.

    All methods are exception-safe: a malformed escape sequence can never break
    the relay. When pyte is unavailable every method is a no-op and
    ``has_content()`` stays False so callers fall back to byte-replay.
    """

    def __init__(self, cols: int = DEFAULT_COLS, rows: int = DEFAULT_ROWS):
        self.ok = _HAVE_PYTE
        self._fed_bytes = 0
        self._screen = None
        self._stream = None
        if self.ok:
            try:
                cols = self._clamp(cols, DEFAULT_COLS)
                rows = self._clamp(rows, DEFAULT_ROWS)
                self._screen = pyte.Screen(cols, rows)
                self._stream = pyte.ByteStream(self._screen)
            except Exception:
                self.ok = False

    @staticmethod
    def _clamp(value, fallback: int) -> int:
        try:
            v = int(value)
        except (TypeError, ValueError):
            return fallback
        return max(1, min(v, _MAX_DIM))

    def feed(self, data: bytes) -> None:
        if not self.ok or not data:
            return
        try:
            self._stream.feed(bytes(data))
            self._fed_bytes += len(data)
        except Exception:
            pass

    def resize(self, rows, cols) -> None:
        if not self.ok:
            return
        try:
            r = self._clamp(rows, self._screen.lines)
            c = self._clamp(cols, self._screen.columns)
            if (r, c) != (self._screen.lines, self._screen.columns):
                self._screen.resize(r, c)
        except Exception:
            pass

    def has_content(self) -> bool:
        return bool(self.ok and self._fed_bytes > 0)

    def snapshot_bytes(self) -> bytes:
        """Full-frame ANSI repaint of the current screen, or b'' if unavailable."""
        if not self.has_content():
            return b""
        try:
            return serialize_screen(self._screen).encode("utf-8")
        except Exception:
            return b""
