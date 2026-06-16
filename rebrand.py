#!/usr/bin/env python3
"""Deck rebrand — turn ANY .pptx into the Taikun black/red/white theme, in-memory
and LOSSLESS (no python-pptx round-trip; every chart/image/embed is copied through).

This is opinionated and generic — it does not rely on a fixed list of the source
deck's hexes. For every slide it:
  * forces a WHITE background (slide + layout + master bg, and any full-bleed
    background shape);
  * reclassifies every color by HSL — any saturated hue -> brand red, near-blacks
    -> ink, near-whites -> white, neutrals -> a slate/gray ramp;
  * flips fonts to Poppins (headings) / Roboto (body);
  * fixes text contrast both ways — light text that lands on the new white
    background becomes ink; text on a dark/red fill becomes white; text sitting on
    a photo is left alone.

Photos/charts/embeds are untouched (raster + ppt/charts/* are not restyled).
"""
import io
import zipfile
from lxml import etree

A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
def _a(t): return '{%s}%s' % (A, t)
def _p(t): return '{%s}%s' % (P, t)

# Brand palette
RED='C0392B'; RED2='E04434'; INK='0B1020'; SLATE='5B6472'; SOFT='F5F6F8'
HAIR='E8EAEE'; LGRAY='D7DBE0'; WHITE='FFFFFF'

BODY_REPLACE = {'Calibri','Arial','Work Sans','Times New Roman','Calibri Light',
                'Helvetica','Helvetica Neue','Verdana','Tahoma','Segoe UI'}
# never recolor: white + the macOS traffic-light dots (window-chrome mockups)
PRESERVE = {'FFFFFF','FF5F57','FEBC2E','28C840'}
# theme color scheme -> brand (white canvas, ink text, red accent)
THEME_CLR = {'dk1':INK,'dk2':INK,'lt1':WHITE,'lt2':SOFT,'accent1':RED,'accent2':INK,
             'accent3':SLATE,'accent4':RED2,'accent5':SLATE,'accent6':INK,
             'hlink':RED,'folHlink':'8A2A20'}


def _sl(hexv):
    """(saturation, lightness) in 0..1 from a hex color."""
    h = hexv.lstrip('#')
    try:
        r, g, b = (int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:
        return (0.0, 0.5)
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    if mx == mn:
        s = 0.0
    else:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    return (s, l)

def _luma(hexv):
    h = hexv.lstrip('#')
    try:
        r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        return 128
    return 0.299 * r + 0.587 * g + 0.114 * b

def classify(hexv):
    """Map an arbitrary color to the Taikun black/red/white(/slate) palette by HSL."""
    v = hexv.upper()
    if v in PRESERVE:
        return v
    s, l = _sl(v)
    if l <= 0.22:                       # near-black (incl. dark navies) -> ink
        return INK
    if l >= 0.94:                       # near-white -> white
        return WHITE
    if s >= 0.30 and l <= 0.82:         # any saturated hue -> brand red
        return RED
    # low-saturation neutrals -> gray ramp
    if l >= 0.84: return SOFT
    if l >= 0.60: return LGRAY
    if l >= 0.34: return SLATE
    return INK


def _remap_colors(root):
    for el in root.iter(_a('srgbClr')):
        v = (el.get('val') or '').upper()
        if not v:
            continue
        nv = classify(v)
        if nv != v:
            el.set('val', nv)


def _remap_fonts(root):
    for tag in ('latin', 'cs', 'ea'):
        for el in root.iter(_a(tag)):
            tf = el.get('typeface')
            if not tf:
                continue
            rpr = el.getparent()
            if rpr is None:
                continue
            bold = rpr.get('b') == '1'
            caps = rpr.get('cap') == 'all'
            try: szi = int(rpr.get('sz') or 0)
            except Exception: szi = 0
            head = (szi >= 1800) or (bold and (szi == 0 or szi >= 1300)) or (bold and caps)
            if head:
                if tf != 'Poppins':
                    el.set('typeface', 'Poppins')
                if tag == 'latin' and szi >= 2000 and not caps:
                    r = rpr.getparent()
                    t = r.find(_a('t')) if r is not None else None
                    txt = (t.text or '') if t is not None else ''
                    if 0 < len(txt) <= 34:
                        rpr.set('cap', 'all')
            elif tf in BODY_REPLACE:
                el.set('typeface', 'Roboto')


def _fix_theme(root):
    cs = root.find('.//' + _a('clrScheme'))
    if cs is not None:
        for child in cs:
            nm = etree.QName(child).localname
            if nm in THEME_CLR:
                for c in list(child):
                    child.remove(c)
                etree.SubElement(child, _a('srgbClr')).set('val', THEME_CLR[nm])
    fs = root.find('.//' + _a('fontScheme'))
    if fs is not None:
        maj = fs.find(_a('majorFont') + '/' + _a('latin'))
        mnr = fs.find(_a('minorFont') + '/' + _a('latin'))
        if maj is not None: maj.set('typeface', 'Poppins')
        if mnr is not None: mnr.set('typeface', 'Roboto')


def _white_bg(cSld):
    """Force a solid white background on a cSld (slide/layout/master)."""
    for bg in cSld.findall(_p('bg')):
        cSld.remove(bg)
    bg = etree.Element(_p('bg'))
    bgPr = etree.SubElement(bg, _p('bgPr'))
    sf = etree.SubElement(bgPr, _a('solidFill'))
    etree.SubElement(sf, _a('srgbClr')).set('val', WHITE)
    etree.SubElement(bgPr, _a('effectLst'))
    cSld.insert(0, bg)   # bg must precede spTree


def _shape_rect(sp):
    spPr = sp.find(_p('spPr'))
    xfrm = spPr.find(_a('xfrm')) if spPr is not None else sp.find(_p('xfrm'))
    if xfrm is None:
        return None
    off, ext = xfrm.find(_a('off')), xfrm.find(_a('ext'))
    if off is None or ext is None:
        return None
    try:
        x, y = int(off.get('x')), int(off.get('y'))
        cx, cy = int(ext.get('cx')), int(ext.get('cy'))
    except (TypeError, ValueError):
        return None
    return (x, y, x + cx, y + cy)

def _shape_fill(sp):
    """The shape's explicit srgb solid fill (hex), for background-darkness checks."""
    spPr = sp.find(_p('spPr'))
    if spPr is None:
        return None
    sf = spPr.find(_a('solidFill'))
    if sf is None:
        return None
    c = sf.find(_a('srgbClr'))
    return c.get('val').upper() if (c is not None and c.get('val')) else None

def _has_solid_fill(sp):
    """True if the shape has ANY solid fill (srgb or theme/scheme color)."""
    spPr = sp.find(_p('spPr'))
    if spPr is None:
        return False
    sf = spPr.find(_a('solidFill'))
    return sf is not None and len(sf) > 0

def _set_shape_fill_white(sp):
    spPr = sp.find(_p('spPr'))
    sf = spPr.find(_a('solidFill')) if spPr is not None else None
    if sf is None:
        return
    for c in list(sf):
        sf.remove(c)
    etree.SubElement(sf, _a('srgbClr')).set('val', WHITE)

# theme color names by tone (after _fix_theme: lt*/bg* are light, dk*/tx* dark, accent1 red)
_LIGHT_SCHEME = {'lt1', 'lt2', 'bg1', 'bg2'}
_DARK_SCHEME = {'dk1', 'dk2', 'tx1', 'tx2'}

def _run_tone(rpr):
    """'light' | 'dark' | 'red' | None for a run's color (srgb or scheme)."""
    if rpr is None:
        return None
    sf = rpr.find(_a('solidFill'))
    if sf is None:
        return None
    sc = sf.find(_a('srgbClr'))
    if sc is not None and sc.get('val'):
        v = sc.get('val').upper()
        if v == RED or v == RED2:
            return 'red'
        return 'light' if _luma(v) > 150 else 'dark'
    sch = sf.find(_a('schemeClr'))
    if sch is not None:
        nm = sch.get('val')
        if nm in _LIGHT_SCHEME: return 'light'
        if nm in _DARK_SCHEME: return 'dark'
        if nm == 'accent1': return 'red'
    return None

def _shape_text(sp):
    tx = sp.find(_p('txBody'))
    return "".join(t.text or '' for t in tx.iter(_a('t'))) if tx is not None else ""

def _set_run(r_el, hexval):
    rpr = r_el.find(_a('rPr'))
    if rpr is None:
        rpr = etree.Element(_a('rPr'))
        r_el.insert(0, rpr)
    for fe in (rpr.findall(_a('solidFill')) + rpr.findall(_a('noFill'))
               + rpr.findall(_a('gradFill')) + rpr.findall(_a('pattFill'))):
        rpr.remove(fe)
    sf = etree.Element(_a('solidFill'))
    etree.SubElement(sf, _a('srgbClr')).set('val', hexval)
    ln = rpr.find(_a('ln'))
    if ln is not None:
        ln.addnext(sf)
    else:
        rpr.insert(0, sf)


def _fix_slide(root, slide_area):
    spTree = root.find('.//' + _p('cSld') + '/' + _p('spTree'))
    if spTree is None:
        return
    shapes = [c for c in spTree if etree.QName(c).localname in ('sp', 'cxnSp', 'pic')]

    # 1) full-bleed background shapes -> white
    if slide_area:
        for sp in shapes:
            if etree.QName(sp).localname == 'pic':
                continue
            r = _shape_rect(sp)
            if r and _has_solid_fill(sp):
                if (r[2]-r[0]) * (r[3]-r[1]) >= 0.82 * slide_area:
                    _set_shape_fill_white(sp)

    # 2) gather fills (backgrounds) + image rects
    fills, pics = [], []
    for sp in shapes:
        r = _shape_rect(sp)
        if not r:
            continue
        kind = etree.QName(sp).localname
        if kind == 'pic':
            pics.append(r)
        else:
            fh = _shape_fill(sp)
            if fh:
                area = (r[2]-r[0]) * (r[3]-r[1])
                fills.append((area, r, fh, sp))
    fills.sort(key=lambda x: x[0])

    # 3) bidirectional contrast
    def inside(rect, x, y):
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]
    for sp in shapes:
        if etree.QName(sp).localname == 'pic' or not _shape_text(sp).strip():
            continue
        r = _shape_rect(sp)
        if not r:
            continue
        cx, cy = (r[0]+r[2]) / 2, (r[1]+r[3]) / 2
        eff = _shape_fill(sp)
        over_image = False
        if eff is None:
            for area, fr, fh, fs in fills:
                if fs is not sp and inside(fr, cx, cy):
                    eff = fh
                    break
        if eff is None:
            if any(inside(pr, cx, cy) for pr in pics):
                over_image = True
            else:
                eff = WHITE      # bare slide background (now white)
        if over_image or eff is None:
            continue
        bg_dark = _luma(eff) < 128
        tx = sp.find(_p('txBody'))
        for r_el in tx.iter(_a('r')):
            tone = _run_tone(r_el.find(_a('rPr')))
            if bg_dark:
                if tone in (None, 'dark', 'red'):   # ensure light text on dark/red
                    _set_run(r_el, WHITE)
            elif tone == 'light':                   # light text on the new white -> ink
                _set_run(r_el, INK)


def _slide_size(zin):
    try:
        pres = etree.fromstring(zin.read('ppt/presentation.xml'))
        sz = pres.find(_p('sldSz'))
        return int(sz.get('cx')) * int(sz.get('cy'))
    except Exception:
        return 0


def _xml_pass(data: bytes) -> bytes:
    zin = zipfile.ZipFile(io.BytesIO(data), 'r')
    slide_area = _slide_size(zin)
    out = io.BytesIO()
    zout = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED)
    for info in zin.infolist():
        raw = zin.read(info.filename)
        fn = info.filename
        styleable = fn.endswith('.xml') and (
            fn.startswith('ppt/slides/') or fn.startswith('ppt/slideLayouts/')
            or fn.startswith('ppt/slideMasters/') or fn.startswith('ppt/theme/')
            or fn.startswith('ppt/notesSlides/') or fn.startswith('ppt/notesMasters/'))
        if styleable:
            root = etree.fromstring(raw)
            _remap_fonts(root)
            _remap_colors(root)
            if fn.startswith('ppt/theme/'):
                _fix_theme(root)
            if (fn.startswith('ppt/slides/slide') or fn.startswith('ppt/slideLayouts/')
                    or fn.startswith('ppt/slideMasters/')):
                cSld = root.find('.//' + _p('cSld'))
                if cSld is not None:
                    _white_bg(cSld)
            if fn.startswith('ppt/slides/slide'):
                _fix_slide(root, slide_area)
            raw = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
        zout.writestr(info, raw)
    zin.close(); zout.close()
    return out.getvalue()


def looks_like_pptx(data: bytes) -> bool:
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        return any(n.startswith('ppt/slides/slide') for n in names)
    except Exception:
        return False


def rebrand_bytes(data: bytes) -> bytes:
    """Rebrand a .pptx (bytes in) -> branded .pptx (bytes out). Lossless: every
    non-styling part (media, charts, embeds) is copied through untouched."""
    if not looks_like_pptx(data):
        raise ValueError("That doesn't look like a PowerPoint (.pptx) file.")
    return _xml_pass(data)


if __name__ == '__main__':
    import sys
    src, dst = sys.argv[1], sys.argv[2]
    with open(src, 'rb') as f:
        out = rebrand_bytes(f.read())
    with open(dst, 'wb') as f:
        f.write(out)
    print(f"rebranded {src} -> {dst} ({len(out)} bytes)")
