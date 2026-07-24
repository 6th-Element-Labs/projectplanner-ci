#!/usr/bin/env python3
"""Determine which Tabler icons the app actually uses.

Icons are referenced three ways in the source:
  1. literal class:      <i class="ti ti-alert-circle">
  2. inline ternary:     ti-${paused ? 'player-play' : 'player-pause'}
  3. data-driven maps:   icons = {open: 'folder'}; `ti-${icons[status]}`

A subset built only from (1) would drop (2) and (3) and render blank
squares. To stay safe we take a *superset*: every quoted token anywhere in
the JS/HTML that is a real Tabler icon name (validated against the full
class->codepoint map parsed from the shipped CSS). Over-including a few
glyphs costs bytes; under-including breaks the UI, so we bias to include.
"""
from __future__ import annotations
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "static"
ICON_CSS = STATIC / "vendor" / "tabler" / "css" / "tabler-icons.min.css"

# .ti-<name>:before{content:"\eXXX"}  (some names carry an alias list)
_RULE = re.compile(r"\.ti-([a-z0-9-]+):before\{content:\"\\([0-9a-fA-F]+)\"")
# any single/double/back-quoted token that looks like an icon name
_QUOTED = re.compile(r"""['"`]([a-z][a-z0-9-]{1,40})['"`]""")
_TI_LITERAL = re.compile(r"\bti-([a-z0-9-]+)\b")


def load_icon_map() -> dict[str, int]:
    """class name -> codepoint (int), parsed from the shipped icon CSS."""
    text = ICON_CSS.read_text(encoding="utf-8", errors="replace")
    return {name: int(cp, 16) for name, cp in _RULE.findall(text)}


def _source_files() -> list[Path]:
    files: list[Path] = []
    for pat in ("*.js", "*.html"):
        files += [p for p in STATIC.rglob(pat) if "/vendor/" not in p.as_posix()]
    return files


def used_icons(icon_map: dict[str, int] | None = None) -> dict[str, set[str]]:
    """Return {'literal': set, 'quoted': set, 'all': set} of used icon names.

    'all' is the safe superset to subset the font to.
    """
    icon_map = icon_map or load_icon_map()
    valid = set(icon_map)
    literal: set[str] = set()
    quoted: set[str] = set()
    for f in _source_files():
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in _TI_LITERAL.findall(text):
            if m in valid:
                literal.add(m)
        for tok in _QUOTED.findall(text):
            if tok in valid:
                quoted.add(tok)
    return {"literal": literal, "quoted": quoted, "all": literal | quoted}


def summary() -> dict:
    icon_map = load_icon_map()
    used = used_icons(icon_map)
    return {
        "defined": len(icon_map),
        "used_literal": len(used["literal"]),
        "used_total": len(used["all"]),
        "quoted_only": sorted(used["quoted"] - used["literal"]),
        "used_names": sorted(used["all"]),
        "codepoints": sorted(icon_map[n] for n in used["all"]),
    }


if __name__ == "__main__":
    import json
    s = summary()
    print(f"defined in CSS : {s['defined']}")
    print(f"used (literal) : {s['used_literal']}")
    print(f"used (safe set): {s['used_total']}  <- subset target")
    print(f"caught only via quoted-string scan (would break if omitted): "
          f"{len(s['quoted_only'])}")
    print("  e.g.", ", ".join(s["quoted_only"][:12]))
    print(json.dumps({"used_total": s["used_total"], "defined": s["defined"]}))
