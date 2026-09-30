"""Tune's image judge - runs in the system Python (numpy, scipy, pillow), talks JSON lines over stdin/stdout.

Images arrive as raw little-endian float32 RGB files (linear, straight from Redshift) plus their size.

    {"cmd": "flip", "ref": p, "test": p, "w": w, "h": h, "exposure": 0, "out": prefix}
        -> {"mean", "p99", "p999"}; with "out": prefix_ref.png, prefix_test.png, prefix_err.png
    {"cmd": "pick", "a": p, "b": p, "w": w, "h": h, "n": 3, "tile": px}
        -> {"tiles": [[x, y], ...]}  top-left corners of the n tiles where a and b differ most (in a's pixels)
    {"cmd": "quit"}

The metric is LDR-FLIP (Andersson et al. 2020, "FLIP: A Difference Evaluator for Alternating Images"),
written from the paper: 0 = identical, 1 = as different as it gets. Renders are first turned into what C4D
shows on screen (see tonemap), so only differences you would see count.
"""
import json
import sys
import traceback

import numpy as np
from scipy.signal import fftconvolve

PPD = 67.0                                     # pixels per degree: a 4K 0.7 m monitor at 0.7 m (FLIP's default)
QC, PC, PT, QF, GW = 0.7, 0.4, 0.95, 0.5, 0.082

# ------------------------------------------------------------------ colour spaces
M_RGB2XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                      [0.2126729, 0.7151522, 0.0721750],
                      [0.0193339, 0.1191920, 0.9503041]])
M_XYZ2RGB = np.linalg.inv(M_RGB2XYZ)
WHITE = M_RGB2XYZ @ np.ones(3)


def load(path, w, h):
    return np.fromfile(path, dtype="<f4").reshape(h, w, 3)


def tonemap(lin, exposure=0.0, view="auto"):
    """Linear render -> display [0, 1] sRGB-encoded, the way C4D shows it.
    view "srgb": the render is already tone-mapped (Basic colour management, Redshift post-effects) - just the
    sRGB curve, like the Picture Viewer. "filmic": scene-linear (OCIO) - an ACES-style curve (Narkowicz fit) as a
    stand-in for the view transform. "auto": srgb when nothing is above 1 (already tone-mapped), else filmic."""
    x = np.maximum(np.nan_to_num(lin, nan=0.0, posinf=64.0), 0.0) * (2.0 ** exposure)
    if view == "display":                          # already finished screen colours (what Commandline returns)
        return np.clip(x, 0.0, 1.0)
    if view == "auto":
        view = "srgb" if float(x.max()) <= 1.0001 else "filmic"
    if view == "filmic":
        x = x * 0.6
        x = (x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14)
    x = np.clip(x, 0.0, 1.0)
    return np.where(x <= 0.0031308, 12.92 * x, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def srgb_to_lin(c):
    return np.where(c <= 0.04045, c / 12.92, np.power((c + 0.055) / 1.055, 2.4))


def lin_to_ycxcz(lin):
    xyz = (lin @ M_RGB2XYZ.T) / WHITE
    y = 116.0 * xyz[..., 1] - 16.0
    return np.stack([y, 500.0 * (xyz[..., 0] - xyz[..., 1]), 200.0 * (xyz[..., 1] - xyz[..., 2])], -1)


def ycxcz_to_lin(ycc):
    yy = (ycc[..., 0] + 16.0) / 116.0
    x = ycc[..., 1] / 500.0 + yy
    z = yy - ycc[..., 2] / 200.0
    return (np.stack([x, yy, z], -1) * WHITE) @ M_XYZ2RGB.T


def lin_to_lab(lin):
    t = (lin @ M_RGB2XYZ.T) / WHITE
    d = 6.0 / 29.0
    f = np.where(t > d ** 3, np.cbrt(t), t / (3 * d * d) + 4.0 / 29.0)
    return np.stack([116.0 * f[..., 1] - 16.0, 500.0 * (f[..., 0] - f[..., 1]), 200.0 * (f[..., 1] - f[..., 2])], -1)


def hunt(lab):
    return np.stack([lab[..., 0], 0.01 * lab[..., 0] * lab[..., 1], 0.01 * lab[..., 0] * lab[..., 2]], -1)


def hyab(a, b):
    d = a - b
    return np.abs(d[..., 0]) + np.sqrt(d[..., 1] ** 2 + d[..., 2] ** 2)


# ------------------------------------------------------------------ filters
def _conv(img, k):
    r = k.shape[0] // 2
    return fftconvolve(np.pad(img, r, mode="symmetric"), k, mode="valid")


def _csf_kernels():
    params = [(1.0, 0.0047, 0.0, 1e-5), (1.0, 0.0053, 0.0, 1e-5), (34.1, 0.04, 13.5, 0.025)]
    r = int(np.ceil(3 * np.sqrt(max(max(p[1], p[3]) for p in params) / (2 * np.pi ** 2)) * PPD))
    x, y = np.meshgrid(np.arange(-r, r + 1), np.arange(-r, r + 1))
    z = (x / PPD) ** 2 + (y / PPD) ** 2
    out = []
    for a1, b1, a2, b2 in params:
        g = a1 * np.sqrt(np.pi / b1) * np.exp(-np.pi ** 2 * z / b1) + a2 * np.sqrt(np.pi / b2) * np.exp(-np.pi ** 2 * z / b2)
        out.append(g / g.sum())
    return out


def _feature_kernels():
    sd = 0.5 * GW * PPD
    r = int(np.ceil(3 * sd))
    x, y = np.meshgrid(np.arange(-r, r + 1), np.arange(-r, r + 1))
    g = np.exp(-(x ** 2 + y ** 2) / (2 * sd * sd))
    out = []
    for k in (-x * g, (x ** 2 / (sd * sd) - 1) * g):
        k = np.where(k < 0, k / -k[k < 0].sum(), k / k[k > 0].sum())
        out.append(k)
    return out


CSF = _csf_kernels()
EDGE, POINT = _feature_kernels()


def _cmax():
    g, b = lin_to_lab(np.array([[0.0, 1.0, 0.0]])), lin_to_lab(np.array([[0.0, 0.0, 1.0]]))
    return float(hyab(hunt(g), hunt(b))[0]) ** QC


CMAX = _cmax()


def _features(y, k):
    return np.hypot(_conv(y, k), _conv(y, k.T))


def flip(ref_srgb, test_srgb):
    """Per-pixel LDR-FLIP of two display images (sRGB-encoded, [0, 1])."""
    ref_ycc, test_ycc = lin_to_ycxcz(srgb_to_lin(ref_srgb)), lin_to_ycxcz(srgb_to_lin(test_srgb))

    def colour(ycc):
        f = np.stack([_conv(ycc[..., i], CSF[i]) for i in range(3)], -1)
        return hunt(lin_to_lab(np.clip(ycxcz_to_lin(f), 0.0, 1.0)))

    dc = hyab(colour(ref_ycc), colour(test_ycc)) ** QC
    lo = PC * CMAX
    dc = np.where(dc < lo, PT / lo * dc, PT + (dc - lo) / (CMAX - lo) * (1.0 - PT))

    ry, ty = (ref_ycc[..., 0] + 16.0) / 116.0, (test_ycc[..., 0] + 16.0) / 116.0
    df = np.maximum(np.abs(_features(ry, EDGE) - _features(ty, EDGE)),
                    np.abs(_features(ry, POINT) - _features(ty, POINT)))
    df = (df / np.sqrt(2.0)) ** QF
    return np.power(dc, 1.0 - df)


# ------------------------------------------------------------------ output images
def _magma(v):
    """Black -> purple -> orange -> pale yellow; like FLIP's own error maps."""
    stops = np.array([[0, 0, 4], [81, 18, 124], [183, 55, 121], [252, 137, 97], [252, 253, 191]], float) / 255
    t = np.clip(v, 0, 1) * (len(stops) - 1)
    i = np.minimum(t.astype(int), len(stops) - 2)
    f = (t - i)[..., None]
    return stops[i] * (1 - f) + stops[i + 1] * f


def _save(img01, path):
    from PIL import Image
    Image.fromarray((np.clip(img01, 0, 1) * 255 + 0.5).astype(np.uint8)).save(path)


# ------------------------------------------------------------------ commands
def cmd_flip(c):
    view = c.get("view", "auto")
    ref = tonemap(load(c["ref"], c["w"], c["h"]), c.get("exposure", 0.0), view)
    test = tonemap(load(c["test"], c["w"], c["h"]), c.get("exposure", 0.0), view)
    e = flip(ref, test)
    out = {"mean": float(e.mean()), "p99": float(np.percentile(e, 99)), "p999": float(np.percentile(e, 99.9))}
    if c.get("out"):
        _save(ref, c["out"] + "_ref.png")
        _save(test, c["out"] + "_test.png")
        _save(_magma(np.sqrt(np.clip(e * 4.0, 0, 1))), c["out"] + "_err.png")   # boosted: small errors show
    return out


def cmd_pick(c):
    """Where do a quick noisy render and a clean one differ most? Those tiles are where the noise lives."""
    a = tonemap(load(c["a"], c["w"], c["h"]), 0.0, c.get("view", "auto"))
    b = tonemap(load(c["b"], c["w"], c["h"]), 0.0, c.get("view", "auto"))
    e = flip(a, b)
    t = max(4, int(c["tile"]))
    n = int(c.get("n", 3))
    h, w = e.shape
    # score every tile position on a quarter-tile grid, then take the best ones that don't overlap
    ii = np.cumsum(np.cumsum(np.pad(e, ((1, 0), (1, 0))), 0), 1)
    step = max(1, t // 4)
    cand = []
    for y in range(0, max(1, h - t + 1), step):
        for x in range(0, max(1, w - t + 1), step):
            s = ii[y + t, x + t] - ii[y, x + t] - ii[y + t, x] + ii[y, x]
            cand.append((s, x, y))
    cand.sort(reverse=True)
    tiles = []
    for s, x, y in cand:
        if all(abs(x - tx) >= t or abs(y - ty) >= t for tx, ty in tiles):
            tiles.append((x, y))
            if len(tiles) == n:
                break
    return {"tiles": [list(p) for p in tiles], "mean": float(e.mean())}


def cmd_flicker(c):
    """How much a render changes from one frame to the next beyond what really changed (the clean reference's
    change): grain that boils in motion. Mean absolute display-luminance difference, 0 = steady."""
    w, h, v = c["w"], c["h"], c.get("view", "auto")
    lum = np.array([0.2126, 0.7152, 0.0722])
    r0, r1, a0, a1 = (tonemap(load(c[k], w, h), 0.0, v) @ lum for k in ("r0", "r1", "a0", "a1"))
    return {"flicker": float(np.abs((a1 - a0) - (r1 - r0)).mean())}


CMDS = {"flip": cmd_flip, "pick": cmd_pick, "flicker": cmd_flicker}


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            c = json.loads(line)
            if c.get("cmd") == "quit":
                break
            res = {"ok": True, **CMDS[c["cmd"]](c)}
        except Exception as e:
            res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e), "trace": traceback.format_exc()}
        sys.stdout.write(json.dumps(res) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
