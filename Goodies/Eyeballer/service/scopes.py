"""DaVinci Resolve-style scopes for Eyeballer.

Every scope takes a float32 HxWx3 image in 0..1 (display-referred, small - ~320 px wide is plenty), an optional
reference image (drawn in orange, like a second trace), and returns a PIL image of exactly (w, h).
Look: near-black background, faint 10-bit graticule (0-1023) with dim labels, soft traces.
"""
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont

BG = (13, 13, 14)
GRID = (40, 40, 44)
LABEL = (105, 105, 112)
TRACE = np.array([0.80, 0.95, 0.85], np.float32)      # Resolve's soft whitish-green
REF = np.array([1.0, 0.58, 0.16], np.float32)
try: FONT = ImageFont.truetype("segoeui.ttf", 10); FONT_M = ImageFont.truetype("segoeui.ttf", 12); FONT_B = ImageFont.truetype("segoeuib.ttf", 12)
except OSError: FONT = FONT_M = FONT_B = ImageFont.load_default()

SCOPES = ["Waveform", "RGB Parade", "Vectorscope", "Histogram", "CIE Chromaticity", "Match"]

def luma(a): return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722

def _density(h, gain):
    h = h.astype(np.float32)
    m = np.percentile(h[h > 0], 99) if (h > 0).any() else 1
    return np.clip((h / max(m, 1)) ** 0.45 * gain, 0, 1)

def _trace(values, W, H, x_index):
    """2-D histogram of (x column, value) -> H x W counts."""
    ys = (H - 1 - np.clip(values * (H - 1), 0, H - 1)).astype(np.int32)
    return np.bincount((ys * W + x_index).ravel(), minlength=W * H).reshape(H, W)

LEFT = 30                                                  # room for the 10-bit scale labels

def _graticule(d, x0, x1, H, labels=True):
    for v in (0, 128, 256, 384, 512, 640, 768, 896, 1023):
        y = int((1 - v / 1023) * (H - 7)) + 3
        d.line([x0, y, x1, y], fill=GRID)
        if labels: d.text((3, y - 7), str(v), fill=LABEL, font=FONT)

def waveform(a, w, h, ref=None, gain=1.0):
    im = Image.new("RGB", (w, h), BG); d = ImageDraw.Draw(im)
    pw, ph = w - LEFT - 4, h - 6
    _graticule(d, LEFT, w - 2, h)
    out = np.zeros((ph, pw, 3), np.float32)
    for img, col in ((a, TRACE), (ref, REF)):
        if img is None: continue
        y = luma(img); hh, ww = y.shape
        xs = np.broadcast_to((np.arange(ww) * pw // ww)[None, :], (hh, ww))
        out += _density(_trace(y, pw, ph, xs), gain)[..., None] * col
    base = np.asarray(im, np.float32) / 255
    base[3:3 + ph, LEFT:LEFT + pw] = np.clip(base[3:3 + ph, LEFT:LEFT + pw] + out, 0, 1)
    return Image.fromarray((base * 255).astype(np.uint8))

def parade(a, w, h, ref=None, gain=1.0):
    im = Image.new("RGB", (w, h), BG); d = ImageDraw.Draw(im)
    _graticule(d, LEFT, w - 2, h)
    gap = 6; pw = (w - LEFT - 4 - 2 * gap) // 3; ph = h - 6
    base = np.asarray(im, np.float32) / 255
    cols = [np.array([1.0, 0.32, 0.30]), np.array([0.35, 1.0, 0.40]), np.array([0.35, 0.55, 1.0])]
    for c in range(3):
        x0 = LEFT + c * (pw + gap); out = np.zeros((ph, pw, 3), np.float32)
        for img, col in ((a, cols[c]), (ref, REF)):
            if img is None: continue
            v = img[..., c]; hh, ww = v.shape
            xs = np.broadcast_to((np.arange(ww) * pw // ww)[None, :], (hh, ww))
            out += _density(_trace(v, pw, ph, xs), gain)[..., None] * col
        base[3:3 + ph, x0:x0 + pw] = np.clip(base[3:3 + ph, x0:x0 + pw] + out, 0, 1)
    return Image.fromarray((base * 255).astype(np.uint8))

VEC_TARGETS = [("R", (0.75, 0, 0)), ("Yl", (0.75, 0.75, 0)), ("G", (0, 0.75, 0)), ("Cy", (0, 0.75, 0.75)),
               ("B", (0, 0, 0.75)), ("Mg", (0.75, 0, 0.75))]
def _cbcr(a):
    y = luma(a)
    return (a[..., 2] - y) / 1.8556, (a[..., 0] - y) / 1.5748

def vectorscope(a, w, h, ref=None, gain=1.0, zoom=1.0):
    im = Image.new("RGB", (w, h), BG); d = ImageDraw.Draw(im)
    S = min(w, h) - 8; cx, cy = w // 2, h // 2
    R = S / 2
    scale = R / 0.5 * zoom * 0.95                          # |CbCr| 0.5 = edge at 1x
    d.ellipse([cx - R, cy - R, cx + R, cy + R], outline=GRID)
    d.ellipse([cx - R * 0.5, cy - R * 0.5, cx + R * 0.5, cy + R * 0.5], outline=(30, 30, 34))
    d.line([cx - R, cy, cx + R, cy], fill=(30, 30, 34)); d.line([cx, cy - R, cx, cy + R], fill=(30, 30, 34))
    for k in range(0, 360, 10):                            # tick ring like Resolve
        t = math.radians(k); r0 = R - (5 if k % 30 == 0 else 2)
        d.line([cx + r0 * math.cos(t), cy + r0 * math.sin(t), cx + R * math.cos(t), cy + R * math.sin(t)], fill=GRID)
    cb, cr = _cbcr(np.array([[(0.87, 0.67, 0.55)]], np.float32))       # skin-tone line
    ang = math.atan2(-cr[0, 0], cb[0, 0])
    d.line([cx, cy, cx + R * math.cos(ang), cy + R * math.sin(ang)], fill=(95, 78, 62))
    out = np.zeros((h, w, 3), np.float32)
    for img, col in ((a, TRACE), (ref, REF)):
        if img is None: continue
        cb, cr = _cbcr(img)
        px = np.clip(cx + cb * scale, 0, w - 1).astype(np.int32); py = np.clip(cy - cr * scale, 0, h - 1).astype(np.int32)
        hist = np.bincount((py * w + px).ravel(), minlength=w * h).reshape(h, w)
        out += _density(hist, gain)[..., None] * col
    base = np.clip(np.asarray(im, np.float32) / 255 + out, 0, 1)
    im = Image.fromarray((base * 255).astype(np.uint8)); d = ImageDraw.Draw(im)
    for name, t in VEC_TARGETS:
        cb, cr = _cbcr(np.array([[t]], np.float32))
        x, y = cx + cb[0, 0] * scale, cy - cr[0, 0] * scale
        if abs(x - cx) < R and abs(y - cy) < R:
            col = tuple(int(v / 0.75 * 200) for v in t)
            d.rectangle([x - 4, y - 4, x + 4, y + 4], outline=col)
            d.text((x + (6 if x >= cx else -18), y - 6), name, fill=col, font=FONT)
    if zoom != 1: d.text((w - 26, h - 16), "%gx" % zoom, fill=LABEL, font=FONT)
    return im

def histogram(a, w, h, ref=None, gain=1.0):
    im = Image.new("RGB", (w, h), BG); d = ImageDraw.Draw(im)
    pw, ph = w - 8, h - 18; x0 = 4
    for v in (0, 256, 512, 768, 1023):
        x = x0 + int(v / 1023 * (pw - 1)); d.line([x, 3, x, 3 + ph], fill=GRID)
        d.text((min(x - 6, w - 24), h - 14), str(v), fill=LABEL, font=FONT)
    base = np.asarray(im, np.float32) / 255
    rows = np.arange(ph)[:, None]
    def curve(v):
        # 256 bins (the source is 8-bit), lightly smoothed, then resampled to the plot width - no comb spikes
        hb = np.bincount(np.clip(v * 255 + 0.5, 0, 255).astype(np.int32).ravel(), minlength=256).astype(np.float32)
        hb = np.convolve(hb, np.array([1, 2, 3, 2, 1], np.float32) / 9, mode="same")
        hb = np.interp(np.linspace(0, 255, pw), np.arange(256), hb)
        hb = np.sqrt(hb); return hb / (np.percentile(hb, 99.5) or 1) * (ph - 2)
    for c, col in enumerate(([0.95, 0.25, 0.25], [0.25, 0.9, 0.3], [0.3, 0.45, 1.0])):
        hist = curve(a[..., c]) * gain
        fill = (rows >= (ph - hist[None, :])).astype(np.float32)          # filled area, additive: overlaps go white
        base[3:3 + ph, x0:x0 + pw] += fill[..., None] * np.array(col, np.float32) * 0.55
    if ref is not None:
        hist = curve(luma(ref))
        im2 = Image.fromarray((np.clip(base, 0, 1) * 255).astype(np.uint8)); d2 = ImageDraw.Draw(im2)
        d2.line([(x0 + x, 3 + ph - min(ph, hist[x])) for x in range(pw)], fill=tuple(int(v * 255) for v in REF), width=1)
        return im2
    return Image.fromarray((np.clip(base, 0, 1) * 255).astype(np.uint8))

# CIE 1931 2-degree spectral locus, 380-700 nm in 10 nm steps (x, y)
LOCUS = [(0.1741, 0.0050), (0.1738, 0.0049), (0.1733, 0.0048), (0.1726, 0.0048), (0.1714, 0.0051), (0.1689, 0.0069),
         (0.1644, 0.0109), (0.1566, 0.0177), (0.1440, 0.0297), (0.1241, 0.0578), (0.0913, 0.1327), (0.0454, 0.2950),
         (0.0082, 0.5384), (0.0139, 0.7502), (0.0743, 0.8338), (0.1547, 0.8059), (0.2296, 0.7543), (0.3016, 0.6923),
         (0.3731, 0.6245), (0.4441, 0.5547), (0.5125, 0.4866), (0.5752, 0.4242), (0.6270, 0.3725), (0.6658, 0.3340),
         (0.6915, 0.3083), (0.7079, 0.2920), (0.7190, 0.2809), (0.7260, 0.2740), (0.7300, 0.2700), (0.7320, 0.2680),
         (0.7334, 0.2666), (0.7347, 0.2653), (0.7347, 0.2653)]
M709 = np.array([[0.4124, 0.3576, 0.1805], [0.2126, 0.7152, 0.0722], [0.0193, 0.1192, 0.9505]], np.float32)

def cie(a, w, h, ref=None, gain=1.0):
    im = Image.new("RGB", (w, h), BG); d = ImageDraw.Draw(im)
    S = min(w - 10, h - 10); ox, oy = (w - S) // 2, (h - S) // 2 + S
    def P(x, y): return ox + x / 0.8 * S, oy - y / 0.9 * S
    d.polygon([P(x, y) for x, y in LOCUS], outline=(70, 70, 76))
    d.polygon([P(0.64, 0.33), P(0.30, 0.60), P(0.15, 0.06)], outline=(120, 120, 128))    # Rec.709 gamut
    wx, wy = P(0.3127, 0.3290); d.ellipse([wx - 2, wy - 2, wx + 2, wy + 2], outline=LABEL)
    d.text((ox + 2, oy - S + 2), "Rec.709", fill=LABEL, font=FONT)
    out = np.zeros((h, w, 3), np.float32)
    for img, col in ((a, TRACE), (ref, REF)):
        if img is None: continue
        lin = np.where(img <= 0.04045, img / 12.92, ((img + 0.055) / 1.055) ** 2.4).reshape(-1, 3)
        XYZ = lin @ M709.T; s = XYZ.sum(1); ok = s > 1e-4
        x, y = XYZ[ok, 0] / s[ok], XYZ[ok, 1] / s[ok]
        px = np.clip(ox + x / 0.8 * S, 0, w - 1).astype(np.int32); py = np.clip(oy - y / 0.9 * S, 0, h - 1).astype(np.int32)
        hist = np.bincount(py * w + px, minlength=w * h).reshape(h, w)
        out += _density(hist, gain)[..., None] * col
    base = np.clip(np.asarray(im, np.float32) / 255 + out, 0, 1)
    return Image.fromarray((base * 255).astype(np.uint8))

# ---------------------------------------------------------------- match (yours vs reference)
def stats(a):
    y = luma(a).ravel(); c = (a.max(-1) - a.min(-1)).ravel()
    p5, p95 = np.percentile(y, (5, 95))
    hb = np.zeros(12, np.float32); warm = 0.0
    col = c > 0.12
    if col.any():
        px = a.reshape(-1, 3)[col]; mx, mn = px.max(1), px.min(1); cc = mx - mn
        r, g, b = px[:, 0], px[:, 1], px[:, 2]
        hue = np.where(mx == r, ((g - b) / cc) % 6, np.where(mx == g, (b - r) / cc + 2, (r - g) / cc + 4)) * 60
        hb = np.bincount((hue // 30).astype(int) % 12, weights=cc, minlength=12).astype(np.float32)
        warm = float(cc[(hue < 65) | (hue > 325)].sum() / cc.sum())
    return {"bright": float(y.mean()), "contrast": float(p95 - p5), "blacks": float((y < 0.08).mean()),
            "highs": float((y > 0.85).mean()), "sat": float(c.mean()), "accent": float((c > 0.35).mean()),
            "warm": warm, "hue": hb / (hb.sum() or 1)}

def compare(yours, ref):
    rows = []
    def row(label, key, scale, more, less, tol):
        d = yours[key] - ref[key]
        rows.append((label, yours[key], ref[key], scale, max(0.0, 1 - abs(d) / (tol * 3)), "on target" if abs(d) <= tol else (more if d > 0 else less)))
    row("Brightness", "bright", 1.0, "brighter", "darker", 0.04)
    row("Contrast", "contrast", 1.0, "more contrast", "flatter", 0.06)
    row("Deep blacks", "blacks", 1.0, "more black", "less black", 0.05)
    row("Highlights", "highs", 0.3, "more highlights", "fewer highlights", 0.02)
    row("Saturation", "sat", 0.6, "more saturated", "less saturated", 0.03)
    a, b = yours["accent"], ref["accent"]
    if a < 0.003 and b < 0.003: rows.append(("Accent area", a, b, 0.2, 1.0, "on target"))
    elif b < 0.003: rows.append(("Accent area", a, b, max(0.2, a), 0.2, "reference has almost none"))
    elif a < 0.003: rows.append(("Accent area", a, b, max(0.2, b), 0.2, "yours has almost none"))
    else:
        ratio = a / b
        rows.append(("Accent area", a, b, max(0.2, a, b), max(0.0, 1 - abs(math.log2(ratio)) / 2.5),
                     "on target" if 0.66 < ratio < 1.5 else ("%.1fx the reference" % ratio if ratio > 1 else "%.0f%% of the reference" % (ratio * 100))))
    pal = float(np.minimum(yours["hue"], ref["hue"]).sum())
    rows.append(("Colour palette", pal, 1.0, 1.0, pal, "on target" if pal > 0.7 else "%d%% shared colours" % (pal * 100)))
    row("Warm share", "warm", 1.0, "warmer", "cooler", 0.1)
    wts = [1.2, 1.2, 1.0, 0.8, 1.0, 1.2, 1.4, 0.8]
    return sum(w * r[4] for w, r in zip(wts, rows)) / sum(wts) * 100, rows

def match(a, w, h, ref=None, gain=1.0, cache=None):
    im = Image.new("RGB", (w, h), BG); d = ImageDraw.Draw(im)
    if ref is None:
        d.text((12, 10), "Match", fill=(200, 200, 205), font=FONT_B)
        d.text((12, 30), "Load a reference (Reference menu) to compare.", fill=LABEL, font=FONT_M); return im
    overall, rows = cache if cache else compare(stats(a), stats(ref))
    col = (110, 205, 120) if overall >= 80 else ((230, 190, 80) if overall >= 60 else (230, 120, 90))
    d.text((12, 6), "Match", fill=(200, 200, 205), font=FONT_B); d.text((62, 6), "%.0f%%" % overall, fill=col, font=FONT_B)
    rh = max(13, (h - 30) // len(rows)); bx0 = 112; bx1 = min(w - 150, bx0 + 110)
    for i, (label, yv, rv, scale, sim, verdict) in enumerate(rows):
        y = 28 + i * rh
        if y + 12 > h: break
        d.text((12, y - 2), label, fill=(185, 185, 190), font=FONT_M)
        if bx1 > bx0 + 20:
            d.line([bx0, y + 6, bx1, y + 6], fill=GRID, width=2)
            for v, c in ((rv, tuple(int(x * 255) for x in REF)), (yv, (235, 235, 235))):
                x = bx0 + min(1.0, v / scale if scale else 0) * (bx1 - bx0); d.rectangle([x - 1, y + 1, x + 1, y + 11], fill=c)
        d.text((bx1 + 10, y - 2), verdict, fill=(120, 200, 130) if verdict == "on target" else (225, 180, 100), font=FONT_M)
    return im

RENDER = {"Waveform": waveform, "RGB Parade": parade, "Vectorscope": vectorscope, "Histogram": histogram,
          "CIE Chromaticity": cie, "Match": match}
