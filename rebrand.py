#!/usr/bin/env python3
"""Deck rebrand — turn ANY .pptx into the Taikun black/red/white theme, in-memory
and LOSSLESS (no python-pptx round-trip; every chart/image/embed is copied through).

For every slide it:
  * forces a WHITE background (slide + layout + master bg, and full-bleed bg shapes,
    incl. theme-colored ones);
  * reclassifies color by HSL+HUE — reds/blues/teals/purples -> brand red, but
    GREEN and AMBER survive (status colors a matrix/dashboard needs), near-black ->
    ink, near-white -> white, neutrals -> a slate/gray ramp;
  * flips fonts to Poppins (headings) / Roboto (body);
  * fixes contrast both ways (light text on the new white bg -> ink; text on a
    dark/red fill -> white; text over a photo left alone);
  * stamps the Taikun logo bottom-left on every slide.

Photos/charts/embeds are untouched (raster + ppt/charts/* are not restyled).
"""
import io
import os
import zipfile
from lxml import etree

A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
PKGREL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
IMGREL = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships/image'
def _a(t): return '{%s}%s' % (A, t)
def _p(t): return '{%s}%s' % (P, t)

# Brand palette
RED='C0392B'; RED2='E04434'; INK='0B1020'; SLATE='5B6472'; SOFT='F5F6F8'
HAIR='E8EAEE'; LGRAY='D7DBE0'; WHITE='FFFFFF'; GREEN='1F7A4D'; AMBER='C77D11'

BODY_REPLACE = {'Calibri','Arial','Work Sans','Times New Roman','Calibri Light',
                'Helvetica','Helvetica Neue','Verdana','Tahoma','Segoe UI'}
PRESERVE = {'FFFFFF','FF5F57','FEBC2E','28C840'}   # white + macOS traffic-light dots
THEME_CLR = {'dk1':INK,'dk2':INK,'lt1':WHITE,'lt2':SOFT,'accent1':RED,'accent2':INK,
             'accent3':SLATE,'accent4':RED2,'accent5':SLATE,'accent6':INK,
             'hlink':RED,'folHlink':'8A2A20'}
DARK_BG = {'0B1020','000000','C0392B','E04434','5B6472','0B2545','07182E','1F2937','1A1A1A',
           '13335C','13315C','1C3678'}

_LOGO_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'assets', 'taikun-logo.png')
try:
    with open(_LOGO_PATH, 'rb') as _f:
        _LOGO_BYTES = _f.read()
    _LOGO_ASPECT = 4.077   # 1476x362
except Exception:
    _LOGO_BYTES = None
    _LOGO_ASPECT = 4.077
_LOGO_MEDIA = 'ppt/media/taikun-brand-logo.png'
_LOGO_RID = 'rIdTaikunLogo'
_LOGO_NAME = 'Taikun Brand Logo'


def _hsl(hexv):
    h = hexv.lstrip('#')
    try:
        r, g, b = (int(h[i:i+2], 16) / 255.0 for i in (0, 2, 4))
    except Exception:
        return (0.0, 0.0, 0.5)
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2
    d = mx - mn
    if d == 0:
        return (0.0, 0.0, l)
    s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
    if mx == r:   hue = ((g - b) / d) % 6
    elif mx == g: hue = (b - r) / d + 2
    else:         hue = (r - g) / d + 4
    return (hue * 60, s, l)

def _luma(hexv):
    h = hexv.lstrip('#')
    try:
        r, g, b = (int(h[i:i+2], 16) for i in (0, 2, 4))
    except Exception:
        return 128
    return 0.299 * r + 0.587 * g + 0.114 * b

def classify(hexv):
    """Map a color to the Taikun palette. Greens/ambers survive (status colors);
    all other saturated hues -> brand red; darks -> ink; lights -> white; neutrals -> gray."""
    v = hexv.upper()
    if v in PRESERVE:
        return v
    hue, s, l = _hsl(v)
    if l <= 0.20:
        return INK
    if l >= 0.94:
        return WHITE
    if s >= 0.28 and l <= 0.86:
        if 80 <= hue <= 168:   return GREEN     # status green
        if 38 <= hue < 80:     return AMBER     # status amber / yellow
        return RED                              # red/orange/blue/teal/purple -> brand red
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
            try: szi = int(rpr.get('sz') or 0)
            except Exception: szi = 0
            head = (szi >= 1800) or (bold and (szi == 0 or szi >= 1300)) or (bold and rpr.get('cap') == 'all')
            if head:
                if tf != 'Poppins':
                    el.set('typeface', 'Poppins')      # no forced uppercase -> avoids overflow
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
    for bg in cSld.findall(_p('bg')):
        cSld.remove(bg)
    bg = etree.Element(_p('bg'))
    bgPr = etree.SubElement(bg, _p('bgPr'))
    etree.SubElement(etree.SubElement(bgPr, _a('solidFill')), _a('srgbClr')).set('val', WHITE)
    etree.SubElement(bgPr, _a('effectLst'))
    cSld.insert(0, bg)


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
    spPr = sp.find(_p('spPr'))
    sf = spPr.find(_a('solidFill')) if spPr is not None else None
    if sf is None:
        return None
    c = sf.find(_a('srgbClr'))
    return c.get('val').upper() if (c is not None and c.get('val')) else None

def _has_solid_fill(sp):
    spPr = sp.find(_p('spPr'))
    sf = spPr.find(_a('solidFill')) if spPr is not None else None
    return sf is not None and len(sf) > 0

def _set_shape_fill_white(sp):
    spPr = sp.find(_p('spPr'))
    sf = spPr.find(_a('solidFill')) if spPr is not None else None
    if sf is None:
        return
    for c in list(sf):
        sf.remove(c)
    etree.SubElement(sf, _a('srgbClr')).set('val', WHITE)

_LIGHT_SCHEME = {'lt1', 'lt2', 'bg1', 'bg2'}
_DARK_SCHEME = {'dk1', 'dk2', 'tx1', 'tx2'}

def _run_tone(rpr):
    if rpr is None:
        return None
    sf = rpr.find(_a('solidFill'))
    if sf is None:
        return None
    sc = sf.find(_a('srgbClr'))
    if sc is not None and sc.get('val'):
        v = sc.get('val').upper()
        if v in (RED, RED2): return 'red'
        return 'light' if _luma(v) > 150 else 'dark'
    sch = sf.find(_a('schemeClr'))
    if sch is not None:
        nm = sch.get('val')
        if nm in _LIGHT_SCHEME: return 'light'
        if nm in _DARK_SCHEME: return 'dark'
        if nm == 'accent1': return 'red'
    return None

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


def _add_logo(root, slide_w, slide_h):
    """Append the Taikun logo (bottom-left) to the slide spTree, once."""
    if not _LOGO_BYTES or not slide_w:
        return False
    spTree = root.find('.//' + _p('cSld') + '/' + _p('spTree'))
    if spTree is None:
        return False
    for cNvPr in spTree.iter(_p('cNvPr')):
        if cNvPr.get('name') == _LOGO_NAME:
            return False   # already stamped (idempotent)
    # skip if the slide already carries a TAIKUN wordmark/footer (a short text shape
    # mentioning TAIKUN). Geometry-independent so it catches placeholder footers too.
    for sp in spTree:
        if etree.QName(sp).localname != 'sp':
            continue
        tx = sp.find(_p('txBody'))
        if tx is None:
            continue
        txt = "".join(t.text or '' for t in tx.iter(_a('t'))).strip()
        if 'TAIKUN' in txt.upper() and len(txt) < 55:
            return False
    h = int(0.26 * 914400)
    w = int(0.26 * _LOGO_ASPECT * 914400)
    x = int(0.5 * 914400)
    y = slide_h - int(0.5 * 914400)
    pic = etree.SubElement(spTree, _p('pic'))
    nv = etree.SubElement(pic, _p('nvPicPr'))
    cn = etree.SubElement(nv, _p('cNvPr')); cn.set('id', '99001'); cn.set('name', _LOGO_NAME)
    cp = etree.SubElement(nv, _p('cNvPicPr')); etree.SubElement(cp, _a('picLocks')).set('noChangeAspect', '1')
    etree.SubElement(nv, _p('nvPr'))
    bf = etree.SubElement(pic, _p('blipFill'))
    etree.SubElement(bf, _a('blip')).set('{%s}embed' % R, _LOGO_RID)
    etree.SubElement(etree.SubElement(bf, _a('stretch')), _a('fillRect'))
    spPr = etree.SubElement(pic, _p('spPr'))
    xfrm = etree.SubElement(spPr, _a('xfrm'))
    etree.SubElement(xfrm, _a('off')).attrib.update({'x': str(x), 'y': str(y)})
    etree.SubElement(xfrm, _a('ext')).attrib.update({'cx': str(w), 'cy': str(h)})
    g = etree.SubElement(spPr, _a('prstGeom')); g.set('prst', 'rect'); etree.SubElement(g, _a('avLst'))
    return True

def _add_logo_rel(rels_bytes):
    """Add the logo image relationship to a slide's .rels (creating root if needed)."""
    if rels_bytes:
        root = etree.fromstring(rels_bytes)
    else:
        root = etree.Element('{%s}Relationships' % PKGREL)
    for rel in root:
        if rel.get('Id') == _LOGO_RID:
            return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
    rel = etree.SubElement(root, '{%s}Relationship' % PKGREL)
    rel.set('Id', _LOGO_RID); rel.set('Type', IMGREL); rel.set('Target', '../media/taikun-brand-logo.png')
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)

def _ensure_png_ct(ct_bytes):
    root = etree.fromstring(ct_bytes)
    for d in root.findall('{%s}Default' % CT):
        if (d.get('Extension') or '').lower() == 'png':
            return ct_bytes
    d = etree.Element('{%s}Default' % CT); d.set('Extension', 'png'); d.set('ContentType', 'image/png')
    root.insert(0, d)
    return etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)


def _fix_slide(root, slide_area):
    spTree = root.find('.//' + _p('cSld') + '/' + _p('spTree'))
    if spTree is None:
        return
    shapes = [c for c in spTree if etree.QName(c).localname in ('sp', 'cxnSp', 'pic')]
    if slide_area:
        for sp in shapes:
            if etree.QName(sp).localname == 'pic':
                continue
            r = _shape_rect(sp)
            if r and _has_solid_fill(sp) and (r[2]-r[0]) * (r[3]-r[1]) >= 0.82 * slide_area:
                _set_shape_fill_white(sp)
    fills, pics = [], []
    for sp in shapes:
        r = _shape_rect(sp)
        if not r:
            continue
        if etree.QName(sp).localname == 'pic':
            pics.append(r)
        else:
            fh = _shape_fill(sp)
            if fh:
                fills.append(((r[2]-r[0]) * (r[3]-r[1]), r, fh, sp))
    fills.sort(key=lambda x: x[0])
    def inside(rect, x, y):
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]
    for sp in shapes:
        if etree.QName(sp).localname == 'pic':
            continue
        tx = sp.find(_p('txBody'))
        if tx is None or not "".join(t.text or '' for t in tx.iter(_a('t'))).strip():
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
                eff = WHITE
        if over_image or eff is None:
            continue
        bg_dark = _luma(eff) < 128
        for r_el in tx.iter(_a('r')):
            tone = _run_tone(r_el.find(_a('rPr')))
            if bg_dark:
                if tone in (None, 'dark', 'red'):
                    _set_run(r_el, WHITE)
            elif tone == 'light':
                _set_run(r_el, INK)


def _slide_size(zin):
    try:
        sz = etree.fromstring(zin.read('ppt/presentation.xml')).find(_p('sldSz'))
        w, h = int(sz.get('cx')), int(sz.get('cy'))
        return (w * h, w, h)
    except Exception:
        return (0, 0, 0)


def _xml_pass(data: bytes) -> bytes:
    zin = zipfile.ZipFile(io.BytesIO(data), 'r')
    area, sw, sh = _slide_size(zin)
    names = set(zin.namelist())
    out = io.BytesIO()
    zout = zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED)
    logo_added_to = []
    for info in zin.infolist():
        raw = zin.read(info.filename)
        fn = info.filename
        styleable = fn.endswith('.xml') and (
            fn.startswith('ppt/slides/') or fn.startswith('ppt/slideLayouts/')
            or fn.startswith('ppt/slideMasters/') or fn.startswith('ppt/theme/')
            or fn.startswith('ppt/notesSlides/') or fn.startswith('ppt/notesMasters/'))
        is_slide = fn.startswith('ppt/slides/slide') and fn.endswith('.xml')
        if styleable:
            root = etree.fromstring(raw)
            _remap_fonts(root)
            _remap_colors(root)
            if fn.startswith('ppt/theme/'):
                _fix_theme(root)
            if (is_slide or fn.startswith('ppt/slideLayouts/') or fn.startswith('ppt/slideMasters/')):
                cSld = root.find('.//' + _p('cSld'))
                if cSld is not None:
                    _white_bg(cSld)
            if is_slide:
                _fix_slide(root, area)
                if _add_logo(root, sw, sh):
                    logo_added_to.append(fn)
            raw = etree.tostring(root, xml_declaration=True, encoding='UTF-8', standalone=True)
        elif fn.startswith('ppt/slides/_rels/') and fn.endswith('.xml.rels') and _LOGO_BYTES:
            slide_xml = 'ppt/slides/' + os.path.basename(fn)[:-5]   # strip .rels
            if slide_xml in names:
                raw = _add_logo_rel(raw)
        elif fn == '[Content_Types].xml' and _LOGO_BYTES:
            raw = _ensure_png_ct(raw)
        zout.writestr(info, raw)
    # any slide that lacked a .rels file entirely -> create one
    if _LOGO_BYTES:
        for sfn in logo_added_to:
            relfn = 'ppt/slides/_rels/' + os.path.basename(sfn) + '.rels'
            if relfn not in names:
                zout.writestr(relfn, _add_logo_rel(None))
        zout.writestr(_LOGO_MEDIA, _LOGO_BYTES)
    zin.close(); zout.close()
    return out.getvalue()


def looks_like_pptx(data: bytes) -> bool:
    try:
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        return any(n.startswith('ppt/slides/slide') for n in names)
    except Exception:
        return False


def rebrand_bytes(data: bytes) -> bytes:
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
