#!/usr/bin/env python3
"""Deck rebrand — turn any .pptx into the Taikun brand, in-memory and LOSSLESS.

Self-contained: no asset files, no LibreOffice, no network, and (deliberately) no
python-pptx round-trip — that round-trip silently drops parts it doesn't model
(charts, embeddings, notesMasters, some media). Instead this rewrites the .pptx zip
member-for-member with lxml, so every chart/image/embed survives untouched.

Three edits, all at the XML layer:
  * colors  — remap off-brand hexes -> the Taikun palette (teal/navy -> ink, accents
              -> red, grays -> slate, soft bg -> brand soft). Preserves white, the
              macOS traffic-light dots, and amber.
  * fonts   — bold/heading text -> Poppins, body -> Roboto; short headlines get
              UPPERCASE via the cap attribute (long narrative headlines left as-is).
  * theme   — rewrite the theme color + font scheme. This is what de-tints decks
              whose "dark text" (dk1) scheme color is actually a brand-foreign hue.
  * contrast— after recolor, whiten text whose INNERMOST background is dark, so a
              recolored label never ends up dark-on-dark.

This is the automatic, generic ~80%-there pass. Deck-specific touch-ups (divider
rebuilds, per-slide nudges) are intentionally out of scope.
"""
import io
import zipfile
from lxml import etree

A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
def _a(t): return '{%s}%s' % (A, t)
def _p(t): return '{%s}%s' % (P, t)

# Brand palette
RED='C0392B'; RED2='E04434'; INK='0B1020'; SLATE='5B6472'; SOFT='F5F6F8'; HAIR='E8EAEE'
GREEN='1F7A4D'; GREEN_BG='E3F4E9'; EYE_BG='FAEFEE'; EYE_BD='F0CFCC'; WHITE='FFFFFF'

COLORMAP = {
    '1F2937':INK,'0B2545':INK,'000000':INK,'0D0D0D':INK,'07182E':INK,
    '13335C':INK,'13315C':INK,'1A1A1A':INK,'1C3678':INK,'0F4A8A':INK,
    '27A39B':RED,'1B7F79':RED,'0E5C58':RED,'1A9988':INK,
    'C00000':RED,'B23A48':RED,'EB5600':RED2,'6AA4C8':RED,
    '5B6470':SLATE,'8896A8':SLATE,'6B7280':SLATE,'8FAEC6':SLATE,'595959':SLATE,
    'B7C2CF':'BFC3CB','BCC8DA':'C7CBD2',
    'F2F4F7':SOFT,'F9F9F9':SOFT,'F8F8F8':SOFT,'E8F1F0':SOFT,'EFF2FF':SOFT,'E9EDEE':SOFT,
    'DEE2E6':HAIR,'E8ECEF':HAIR,
    '2F9E44':GREEN,'C3F0D0':GREEN_BG,
    'FAF3F4':EYE_BG,'A2FFE8':EYE_BG,'FFB8A2':EYE_BD,
}
PRESERVE = {'FFFFFF','FF5F57','FEBC2E','28C840','E8B53A'}
BODY_REPLACE = {'Calibri','Arial','Work Sans','Times New Roman','Calibri Light','Helvetica','Helvetica Neue'}
THEME_CLR = {'dk1':INK,'dk2':INK,'lt1':WHITE,'lt2':SOFT,'accent1':SLATE,'accent2':RED,
             'accent3':RED2,'accent4':EYE_BG,'accent5':INK,'accent6':EYE_BD,'hlink':RED,'folHlink':'8A2A20'}
DARK_BG = {'0B1020','000000','C0392B','E04434','5B6472','0B2545','07182E','1F2937','1A1A1A','13335C','13315C','1C3678'}


def _remap_colors(root):
    for el in root.iter(_a('srgbClr')):
        v = (el.get('val') or '').upper()
        if v in PRESERVE:
            continue
        if v in COLORMAP and COLORMAP[v] != v:
            el.set('val', COLORMAP[v])


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


# ---- contrast (pure-XML, geometry parsed from the slide) ----
def _luma(h):
    h = h.lstrip('#'); r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b

def _is_light(h):
    try: return _luma(h) > 140
    except Exception: return False

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
    if spPr is None:
        return None
    sf = spPr.find(_a('solidFill'))
    if sf is None:
        return None
    c = sf.find(_a('srgbClr'))
    return c.get('val').upper() if (c is not None and c.get('val')) else None

def _shape_text(sp):
    tx = sp.find(_p('txBody'))
    if tx is None:
        return ""
    return "".join(t.text or '' for t in tx.iter(_a('t')))

def _whiten_run(r_el):
    rpr = r_el.find(_a('rPr'))
    if rpr is None:
        rpr = etree.Element(_a('rPr'))
        r_el.insert(0, rpr)
    # already light? leave it
    sc = rpr.find(_a('solidFill') + '/' + _a('srgbClr'))
    if sc is not None and sc.get('val') and _is_light(sc.get('val')):
        return
    for fe in (rpr.findall(_a('solidFill')) + rpr.findall(_a('noFill'))
               + rpr.findall(_a('gradFill')) + rpr.findall(_a('pattFill'))):
        rpr.remove(fe)
    sf = etree.Element(_a('solidFill'))
    etree.SubElement(sf, _a('srgbClr')).set('val', WHITE)
    ln = rpr.find(_a('ln'))
    if ln is not None:
        ln.addnext(sf)          # fill follows ln in the schema
    else:
        rpr.insert(0, sf)

def _fix_contrast(slide_root):
    spTree = slide_root.find('.//' + _p('cSld') + '/' + _p('spTree'))
    if spTree is None:
        return
    shapes = [c for c in spTree if etree.QName(c).localname in ('sp', 'cxnSp', 'pic')]
    fills = []
    for sp in shapes:
        fh, r = _shape_fill(sp), _shape_rect(sp)
        if fh and r:
            area = (r[2]-r[0]) * (r[3]-r[1]) / 914400.0 / 914400.0
            if area > 0.05:
                fills.append((area, r, fh, sp))
    fills.sort(key=lambda x: x[0])     # smallest (innermost) first
    for sp in shapes:
        if not _shape_text(sp).strip():
            continue
        r = _shape_rect(sp)
        if not r:
            continue
        cx, cy = (r[0]+r[2]) / 2, (r[1]+r[3]) / 2
        eff = _shape_fill(sp)
        if eff is None:
            for area, fr, fh, fs in fills:
                if fs is sp:
                    continue
                if fr[0] <= cx <= fr[2] and fr[1] <= cy <= fr[3]:
                    eff = fh
                    break
        if eff is None or eff not in DARK_BG:
            continue
        tx = sp.find(_p('txBody'))
        for r_el in tx.iter(_a('r')):
            _whiten_run(r_el)


def _xml_pass(data: bytes) -> bytes:
    zin = zipfile.ZipFile(io.BytesIO(data), 'r')
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
            if fn.startswith('ppt/slides/slide'):
                _fix_contrast(root)
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
