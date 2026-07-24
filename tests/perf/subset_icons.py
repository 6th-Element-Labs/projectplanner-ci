#!/usr/bin/env python3
"""Generate a subsetted Tabler icon font + trimmed CSS for only-used glyphs.

Safe by construction: the glyph set is the superset from icon_usage.py
(literal classes + every quoted string that is a real icon name). Emits
woff2/woff into a candidate dir; does NOT overwrite the shipped font. The
perf suite's runtime check must confirm DOM coverage before the swap ships.
"""
from __future__ import annotations
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from icon_usage import ROOT, STATIC, load_icon_map, used_icons  # noqa: E402

FONTS = STATIC / "vendor" / "tabler" / "fonts"
SRC_TTF = FONTS / "tabler-icons.ttf"
OUT_DIR = FONTS / "subset"


def build() -> dict:
    icon_map = load_icon_map()
    names = sorted(used_icons(icon_map)["all"])
    codepoints = sorted({icon_map[n] for n in names})
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    unicodes = ",".join(f"U+{cp:04X}" for cp in codepoints)
    results = {}
    for flavor in ("woff2", "woff"):
        out = OUT_DIR / f"tabler-icons.subset.{flavor}"
        cmd = [
            sys.executable, "-m", "fontTools.subset", str(SRC_TTF),
            f"--unicodes={unicodes}",
            f"--flavor={flavor}",
            "--no-hinting", "--desubroutinize",
            f"--output-file={out}",
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)
        results[flavor] = out.stat().st_size

    # trimmed CSS: font-face pointing at the subset + only used glyph rules
    css_lines = [
        '@font-face{font-family:"tabler-icons";font-style:normal;'
        'font-weight:400;font-display:block;'
        'src:url("../fonts/subset/tabler-icons.subset.woff2") format("woff2"),'
        'url("../fonts/subset/tabler-icons.subset.woff") format("woff")}',
        '.ti{font-family:"tabler-icons"!important;speak:none;font-style:normal;'
        'font-weight:normal;font-variant:normal;text-transform:none;'
        'line-height:1;-webkit-font-smoothing:antialiased;'
        '-moz-osx-font-smoothing:grayscale}',
    ]
    for n in names:
        css_lines.append(f'.ti-{n}:before{{content:"\\{icon_map[n]:x}"}}')
    css_out = STATIC / "vendor" / "tabler" / "css" / "tabler-icons.subset.css"
    css_text = "".join(css_lines) + "\n"
    css_out.write_text(css_text, encoding="utf-8")
    results["css_bytes"] = len(css_text.encode())
    results["glyphs"] = len(codepoints)
    results["css_path"] = str(css_out.relative_to(ROOT))
    return results


def verify_coverage() -> dict:
    """Deterministic proof: every used icon's codepoint has a glyph in the
    subset font's cmap. This is the build-time gate for the font swap."""
    from fontTools.ttLib import TTFont
    icon_map = load_icon_map()
    used = sorted(used_icons(icon_map)["all"])
    subset = OUT_DIR / "tabler-icons.subset.woff2"
    if not subset.is_file():
        return {"ok": False, "reason": "subset font not built", "missing": used}
    font = TTFont(str(subset))
    have = set().union(*[t.cmap.keys() for t in font["cmap"].tables])
    missing = [n for n in used if icon_map[n] not in have]
    return {"ok": not missing, "used": len(used), "missing": missing}


if __name__ == "__main__":
    orig = (FONTS / "tabler-icons.woff2").stat().st_size
    r = build()
    print(f"glyphs subset      : {r['glyphs']}")
    print(f"woff2  {orig:>8} -> {r['woff2']:>7} bytes  "
          f"({100 - r['woff2'] * 100 // orig}% smaller)")
    print(f"woff   subset       : {r['woff']} bytes")
    print(f"css    subset       : {r['css_bytes']} bytes ({r['css_path']})")
