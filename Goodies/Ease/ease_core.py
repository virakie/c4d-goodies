"""Ease core - curve maths, key editing and the whole panel. ease.pyp reloads
this file whenever it changes, so edits apply without restarting Cinema 4D
(a layout change needs the panel closed and reopened).

A curve is a list of nodes in a 0..1 box, from (0, 0) to (1, 1):
    [t, v, lt, lv, rt, rv]        position, left handle, right handle
    [t, v, lt, lv, rt, rv, 1]     ...and the segment after it is a step (hold)
Handles are relative to their node, like C4D key tangents. A classic ease
"x1, y1, x2, y2" (AE / CSS cubic-bezier) is two nodes:
    [0, 0, 0, 0, x1, y1] and [1, 1, x2 - 1, y2 - 1, 0, 0]
More nodes = a multi-point curve: the extra nodes become in-between keys,
marked as Breakdown keys so C4D keeps them proportional when you move the
main keys - and so Ease knows to replace them next time.

Modes (tabs in the panel):
    Bezier    two handles - the classic ease
    Custom    any number of points (double-click adds, right-click deletes)
    Bounce    generated: Bounces / Bounciness / Decay. Exact: a bounce arc is
              a parabola, and a parabola is exactly one cubic bezier.
    Elastic   generated: Wiggles / Decay (fitted, within ~0.6 %)
    Steps     generated: Steps (optionally following the Bezier tab's curve).
              Applied as Step-interpolation keys - real held frames.

Apply: every pair of neighbouring selected keys (breakdowns between them
don't count) on every track gets the curve, scaled to that pair's time and
value change. Copy: the run from the first to the last selected key of the
first track that has two, read back as a curve - Linear keys copy as
straight, Step keys as holds. Paste Reversed mirrors in time
(ease-in <-> ease-out).

Facts it relies on (verified live 2026-09-27, C4D 2026):
- Keys set to Tangent Preset Custom, Auto off, Clamp off, Weighted on,
  Break on follow a literal cubic bezier (<= 0.07 % off, C4D's own sampling).
- A key's interpolation governs the segment AFTER it. So the end key of an
  eased segment keeps its interpolation (else a Linear next segment turns
  into a curve); only its left tangent is set.
- Auto tangents store stale numbers; CCurve.GetTangents(i) -> (vl, vr, xl, xr)
  returns what C4D evaluates. Linear / Step keys still carry tangents, which
  C4D ignores - so Copy must read the interpolation first.
- Undo = AddUndo(UNDOTYPE_CHANGE, track). Key bits via ChangeNBit.
- gui.InputDialog returns the preset text on Cancel; gui.RenameDialog
  returns None - use that one.
- GeUserArea: DrawBezierLine needs a plain list; MouseDrag's deltas carry a
  coordinate offset on the first call, so drags read GetInputState instead.
"""

import json
import math
import os
import shutil
import time
import traceback

import c4d
from c4d import gui, storage

# key selection: NBIT_TLn_SELECT in the Dope Sheet, NBIT_TLn_SELECT2 in F-Curve mode (keys
# picked on a curve), for each of the four Timeline windows
SEL_BITS = (c4d.NBIT_TL1_SELECT, c4d.NBIT_TL2_SELECT, c4d.NBIT_TL3_SELECT, c4d.NBIT_TL4_SELECT,
            c4d.NBIT_TL1_SELECT2, c4d.NBIT_TL2_SELECT2, c4d.NBIT_TL3_SELECT2, c4d.NBIT_TL4_SELECT2)
EPS = 1e-9


def log(*args):
    print("[Ease]", *args)


def prefs_dir():
    d = os.path.join(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS), "goodies_ease")
    os.makedirs(d, exist_ok=True)
    return d


def log_error(where):
    try:
        with open(os.path.join(prefs_dir(), "errors.log"), "a", encoding="utf-8") as fh:
            fh.write("--- %s\n%s\n" % (where, traceback.format_exc()))
    except Exception:
        pass
    log("error in", where)


# ═════════════════════════════════════════════════════════════ curves

def from_bezier(x1, y1, x2, y2):
    return [[0.0, 0.0, 0.0, 0.0, x1, y1], [1.0, 1.0, x2 - 1.0, y2 - 1.0, 0.0, 0.0]]


DEFAULT = from_bezier(0.42, 0.0, 0.58, 1.0)


def is_step(node):
    return len(node) > 6 and bool(node[6])


def is_simple(curve):
    """A plain two-handle ease (fits the Bezier tab and 'x1, y1, x2, y2')."""
    return len(curve) == 2 and not is_step(curve[0])


def to_text(curve):
    if is_simple(curve):
        a, b = curve
        return "%.2f, %.2f, %.2f, %.2f" % tuple(round(x, 2) + 0.0 for x in (a[4], a[5], 1.0 + b[2], 1.0 + b[3]))
    return json.dumps([[round(x, 4) for x in n] for n in curve])


def parse(text):
    """'0.63,0.03,0.58,1', 'cubic-bezier(0.63, 0.03, 0.58, 1)' or node JSON."""
    text = (text or "").strip()
    if text.startswith("[["):
        nodes = json.loads(text)
        if len(nodes) >= 2 and all(len(n) in (6, 7) for n in nodes):
            return [[float(x) for x in n] for n in nodes]
        raise ValueError("expected a list of [t, v, lt, lv, rt, rv] nodes")
    inner = text[text.find("(") + 1:text.rfind(")")] if "(" in text else text
    nums = [float(x) for x in inner.replace(";", ",").split(",") if x.strip()]
    if len(nums) != 4:
        raise ValueError("expected four numbers: x1, y1, x2, y2")
    x1, y1, x2, y2 = nums
    return from_bezier(min(1.0, max(0.0, x1)), y1, min(1.0, max(0.0, x2)), y2)


def reverse(curve):
    """Mirror in time and value: an ease-in becomes the matching ease-out.
    A step segment stays a step (a staircase mirrors into a staircase)."""
    n = len(curve)
    out = []
    for j, (t, v, lt, lv, rt, rv, *rest) in enumerate(reversed(curve)):
        node = [1.0 - t, 1.0 - v, -rt, -rv, -lt, -lv]
        if j < n - 1 and is_step(curve[n - 2 - j]):
            node.append(1)
        out.append(node)
    return out


def _bezier_segment(p0, p1, p2, p3, x):
    """y at x on one cubic bezier (x monotonic), by bisection."""
    lo, hi = 0.0, 1.0
    for _ in range(40):
        s = 0.5 * (lo + hi)
        u = 1.0 - s
        bx = u * u * u * p0[0] + 3 * u * u * s * p1[0] + 3 * u * s * s * p2[0] + s * s * s * p3[0]
        lo, hi = (s, hi) if bx < x else (lo, s)
    s = 0.5 * (lo + hi)
    u = 1.0 - s
    return u * u * u * p0[1] + 3 * u * u * s * p1[1] + 3 * u * s * s * p2[1] + s * s * s * p3[1]


def evaluate(curve, x):
    """The curve's value at x (0..1)."""
    for a, c in zip(curve[:-1], curve[1:]):
        if x < c[0] or c is curve[-1]:
            if is_step(a):
                return a[1] if x < c[0] else c[1]
            return _bezier_segment((a[0], a[1]), (a[0] + a[4], a[1] + a[5]), (c[0] + c[2], c[1] + c[3]),
                                   (c[0], c[1]), x)
    return curve[-1][1]


# ═════════════════════════════════════════════════════════════ generators

def _fit(f, cuts=(), split=True):
    """A multi-point curve through f (0..1 -> value): nodes at f's peaks and
    valleys (flat handles there), at the given cuts (corners: each side keeps
    its own slope), at the ends, and (split) once more mid-span - that takes
    elastic / wave from ~10 % to ~0.6 % off. Handles: a third of the gap,
    along f's slope on that side."""
    n = 2000
    ts = [i / float(n) for i in range(n + 1)]
    vs = [f(t) for t in ts]
    keep = {0.0, 1.0}
    for i in range(1, n):
        if (vs[i] - vs[i - 1]) * (vs[i + 1] - vs[i]) < 0:
            keep.add(ts[i])
    keep.update(cuts)
    knots = sorted(keep)
    if split:
        knots = sorted(set(knots) | {0.5 * (a + b) for a, b in zip(knots[:-1], knots[1:])})
    h = 1e-5

    def slope(t, side):
        if side < 0:
            return (f(t) - f(max(0.0, t - h))) / max(h, t - max(0.0, t - h))
        return (f(min(1.0, t + h)) - f(t)) / max(h, min(1.0, t + h) - t)

    nodes = []
    for k, t in enumerate(knots):
        lt = -(t - knots[k - 1]) / 3.0 if k > 0 else 0.0
        rt = (knots[k + 1] - t) / 3.0 if k < len(knots) - 1 else 0.0
        nodes.append([t, f(t), lt, lt * slope(t, -1) if k else 0.0, rt, rt * slope(t, 1) if rt else 0.0])
    nodes[0][0:2] = [0.0, 0.0]
    nodes[-1][0:2] = [1.0, 1.0]
    return nodes


def gen_bounce(p):
    """Falls from rest, hits the target (the floor), bounces back Bounces
    times. Heights: Bounciness, then x Decay each time. Timing from gravity
    (an arc of height h lasts 2 * sqrt(h) of the first fall), so it reads as
    a real bounce. Each arc is a parabola = one exact cubic segment."""
    heights = [p["bounciness"] * p["decay"] ** k for k in range(int(p["bounces"]))]
    durs = [2.0 * math.sqrt(h) for h in heights]
    total = 1.0 + sum(durs)
    fall = 1.0 / total
    nodes = [[0.0, 0.0, 0.0, 0.0, fall / 3.0, 0.0]]
    t = fall
    left = (-fall / 3.0, -2.0 / 3.0)                  # end of the first fall (quadratic from rest)
    for h, d in zip(heights, durs):
        d /= total
        nodes.append([t, 1.0, left[0], left[1], d / 3.0, -4.0 * h / 3.0])
        t += d
        left = (-d / 3.0, -4.0 * h / 3.0)
    nodes.append([1.0, 1.0, left[0], left[1], 0.0, 0.0])
    return nodes


def gen_elastic(p):
    w, k = max(1, int(p["wiggles"])), 2.0 + 10.0 * p["decay"]
    return _fit(lambda t: 1.0 - math.exp(-k * t) * (1.0 - t) * math.cos(w * math.pi * t) if t < 1 else 1.0)


STEPS_EASE = from_bezier(0.33, 0.33, 0.67, 0.67)     # linear, with handles you can grab


def gen_steps(p, bezier=None):
    """Held steps whose heights follow p["ease"] - drag its handles in the
    Steps tab (faint curve behind the staircase)."""
    n = max(2, int(p["steps"]))
    ease = p.get("ease") or STEPS_EASE
    nodes = []
    for i in range(n):
        t = i / float(n)
        v = evaluate(ease, t)
        nodes.append([t, v, 0.0, 0.0, 0.0, 0.0, 1])
    nodes.append([1.0, 1.0, 0.0, 0.0, 0.0, 0.0])
    return nodes


MODES = ["Bezier", "Custom", "Bounce", "Elastic", "Steps"]
GENERATED = ("Bounce", "Elastic", "Steps")
# (key, label, kind, min, max, step, default)
PARAMS = {
    "Bounce": [("bounces", "Bounces", "int", 1, 12, 1, 3), ("bounciness", "Bounciness", "pct", 0.05, 0.95, 0.01, 0.45),
               ("decay", "Decay", "pct", 0.1, 0.95, 0.01, 0.5)],
    "Elastic": [("wiggles", "Wiggles", "int", 1, 16, 1, 5), ("decay", "Decay", "pct", 0.0, 1.0, 0.01, 0.5)],
    "Steps": [("steps", "Steps", "int", 2, 60, 1, 6)],
}


def default_params():
    out = {m: {k: d for k, _, _, _, _, _, d in spec} for m, spec in PARAMS.items()}
    for m in out:
        out[m]["reversed"] = False
    out["Steps"]["ease"] = [list(n) for n in STEPS_EASE]
    return out


def generate(mode, params, bezier):
    p = params[mode]
    curve = {"Bounce": gen_bounce, "Elastic": gen_elastic}[mode](p) if mode != "Steps" \
        else gen_steps(p, bezier)
    return reverse(curve) if p.get("reversed") else curve


# ═════════════════════════════════════════════════════════════ keys

def _objects(op):
    while op:
        yield op
        for o in _objects(op.GetDown()):
            yield o
        op = op.GetNext()


def tracks(doc):
    """Every value track in the scene: objects, their tags, materials."""
    owners = []
    for o in _objects(doc.GetFirstObject()):
        owners.append(o)
        owners.extend(o.GetTags())
    m = doc.GetFirstMaterial()
    while m:
        owners.append(m)
        m = m.GetNext()
    for owner in owners:
        for tr in owner.GetCTracks():
            if tr.GetTrackCategory() == c4d.CTRACK_CATEGORY_VALUE:
                yield tr


def is_selected(key):
    return any(key.GetNBit(b) for b in SEL_BITS)


def is_breakdown(key):
    try:
        return bool(key[c4d.ID_CKEY_BREAKDOWN])
    except Exception:
        return False


def tangents(cv, i):
    """(lt, lv, rt, rv) as C4D evaluates them (right even for auto keys)."""
    vl, vr, xl, xr = cv.GetTangents(i)
    return xl, vl, xr, vr


def make_exact(cv, i, starts_segment):
    """Key i to exact bezier tangents without changing its other side.
    starts_segment: it starts an eased segment, so it becomes Spline; an end
    key keeps its interpolation (that belongs to the segment after it)."""
    lt, lv, rt, rv = tangents(cv, i)
    k = cv.GetKey(i)
    if starts_segment:
        k.SetInterpolation(cv, c4d.CINTERPOLATION_SPLINE)
    k[c4d.ID_CKEY_PRESET] = c4d.ID_CKEY_PRESET_CUSTOM
    k[c4d.ID_CKEY_AUTO] = False
    k[c4d.ID_CKEY_CLAMP] = False
    k[c4d.ID_CKEY_WEIGHTEDTANGENT] = True
    k[c4d.ID_CKEY_AUTOWEIGHT] = False
    k[c4d.ID_CKEY_BREAK] = True
    k.SetTimeLeft(cv, c4d.BaseTime(lt))
    k.SetValueLeft(cv, lv)
    k.SetTimeRight(cv, c4d.BaseTime(rt))
    k.SetValueRight(cv, rv)
    return k


def pairs(cv):
    """(i, j) for neighbouring selected keys; only breakdowns may sit between."""
    out, prev = [], None
    for i in range(cv.GetKeyCount()):
        k = cv.GetKey(i)
        if is_breakdown(k):
            continue
        if is_selected(k):
            if prev is not None:
                out.append((prev, i))
            prev = i
        else:
            prev = None
    return out


def _start(cv, i, node, dt, dv):
    """Key i starts the segment of node: step, or bezier with its right handle."""
    if is_step(node):
        cv.GetKey(i).SetInterpolation(cv, c4d.CINTERPOLATION_STEP)
    else:
        k = make_exact(cv, i, True)
        k.SetTimeRight(cv, c4d.BaseTime(node[4] * dt))
        k.SetValueRight(cv, node[5] * dv)


def apply_segment(doc, cv, i, j, curve):
    """Put curve between keys i and j. Returns how many keys it added."""
    a, b = cv.GetKey(i), cv.GetKey(j)
    ta, tb = a.GetTime().Get(), b.GetTime().Get()
    va, vb = a.GetValue(), b.GetValue()
    dt, dv = tb - ta, vb - va
    if dt <= EPS:
        return 0
    for k in range(j - 1, i, -1):                 # replace the in-betweens a previous ease left
        if is_breakdown(cv.GetKey(k)):
            cv.DelKey(k)
            j -= 1
    _start(cv, i, curve[0], dt, dv)
    if not is_step(curve[-2]):
        end = make_exact(cv, j, False)
        end.SetTimeLeft(cv, c4d.BaseTime(curve[-1][2] * dt))
        end.SetValueLeft(cv, curve[-1][3] * dv)
    added = 0
    for node in curve[1:-1]:
        res = cv.AddKey(c4d.BaseTime(ta + node[0] * dt))
        if not res:
            continue
        k, idx = res["key"], res["nidx"]
        k.SetValue(cv, va + node[1] * dv)
        make_exact(cv, idx, True)
        k.SetTimeLeft(cv, c4d.BaseTime(node[2] * dt))
        k.SetValueLeft(cv, node[3] * dv)
        _start(cv, idx, node, dt, dv)
        k[c4d.ID_CKEY_BREAKDOWN] = True
        for bit in SEL_BITS:
            k.ChangeNBit(bit, c4d.NBITCONTROL_CLEAR)
        added += 1
    return added


def apply(doc, curve):
    """curve onto every selected key pair. Returns a status line."""
    segs, added, flat_multi = 0, 0, 0
    doc.StartUndo()
    try:
        for tr in list(tracks(doc)):
            cv = tr.GetCurve()
            todo = pairs(cv)
            if not todo:
                continue
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, tr)
            for i, j in reversed(todo):              # right to left: in-betweens don't shift what's left to do
                a, b = cv.GetKey(i), cv.GetKey(j)
                if not is_simple(curve) and abs(b.GetValue() - a.GetValue()) <= EPS:
                    flat_multi += 1                  # a bounce on a hold has nothing to scale
                    continue
                added += apply_segment(doc, cv, i, j, curve)
                segs += 1
            tr.GetCurve().SetKeyDirty()
    finally:
        doc.EndUndo()
    c4d.EventAdd()
    if not segs and not flat_multi:
        return "Select at least two neighbouring keys in the Timeline"
    text = "eased %d segment%s" % (segs, "" if segs == 1 else "s")
    if added:
        text += " (+%d in-between keys)" % added
    if flat_multi:
        text += " - skipped %d hold%s (no value change to shape)" % (flat_multi, "" if flat_multi == 1 else "s")
    return text


def copy(doc):
    """The selected run on the first track that has one, as a curve. Reads
    each key's interpolation: Linear copies straight, Step as a hold."""
    for tr in tracks(doc):
        cv = tr.GetCurve()
        sel = [i for i in range(cv.GetKeyCount()) if is_selected(cv.GetKey(i)) and not is_breakdown(cv.GetKey(i))]
        if len(sel) < 2:
            continue
        i, j = sel[0], sel[-1]
        a, b = cv.GetKey(i), cv.GetKey(j)
        t0, v0 = a.GetTime().Get(), a.GetValue()
        dt, dv = b.GetTime().Get() - t0, b.GetValue() - v0
        if abs(dv) <= EPS:
            return None, "Those keys hold one value - there is no ease to copy (pick keys that change)"
        nodes = []
        for k in range(i, j + 1):
            key = cv.GetKey(k)
            lt, lv, rt, rv = tangents(cv, k)
            nodes.append([(key.GetTime().Get() - t0) / dt, (key.GetValue() - v0) / dv,
                          lt / dt, lv / dv, rt / dt, rv / dv])
        for k in range(len(nodes) - 1):              # the key starting each segment decides its shape
            inter = cv.GetKey(i + k).GetInterpolation()
            a_, b_ = nodes[k], nodes[k + 1]
            if inter == c4d.CINTERPOLATION_LINEAR:
                st, sv = (b_[0] - a_[0]) / 3.0, (b_[1] - a_[1]) / 3.0
                a_[4:6] = [st, sv]
                b_[2:4] = [-st, -sv]
            elif inter == c4d.CINTERPOLATION_STEP:
                a_[4:6] = [0.0, 0.0]
                b_[2:4] = [0.0, 0.0]
                a_.append(1)
        nodes[0][2:4] = [0.0, 0.0]
        nodes[-1][4:6] = [0.0, 0.0]
        return nodes, "copied %s (%d keys)" % (tr.GetName(), len(nodes))
    return None, "Select two or more keys to copy their ease"


# ═════════════════════════════════════════════════════════════ clipboard

def _clip_path():
    return os.path.join(prefs_dir(), "clipboard.json")


def store(curve):
    try:
        with open(_clip_path(), "w", encoding="utf-8") as fh:
            json.dump(curve, fh)
    except Exception:
        log_error("store clipboard")
    if is_simple(curve):
        try:
            c4d.CopyStringToClipboard(to_text(curve))     # paste straight into After Effects
        except Exception:
            pass


def stored():
    try:
        with open(_clip_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════ presets

BEZIERS = [
    ("Linear", (0.0, 0.0, 1.0, 1.0)), ("Ease", (0.25, 0.1, 0.25, 1.0)),
    ("Ease In", (0.42, 0.0, 1.0, 1.0)), ("Ease Out", (0.0, 0.0, 0.58, 1.0)), ("Ease In Out", (0.42, 0.0, 0.58, 1.0)),
    ("Sine In", (0.12, 0.0, 0.39, 0.0)), ("Sine Out", (0.61, 1.0, 0.88, 1.0)), ("Sine In Out", (0.37, 0.0, 0.63, 1.0)),
    ("Quad In", (0.11, 0.0, 0.5, 0.0)), ("Quad Out", (0.5, 1.0, 0.89, 1.0)), ("Quad In Out", (0.45, 0.0, 0.55, 1.0)),
    ("Cubic In", (0.32, 0.0, 0.67, 0.0)), ("Cubic Out", (0.33, 1.0, 0.68, 1.0)), ("Cubic In Out", (0.65, 0.0, 0.35, 1.0)),
    ("Quart In", (0.5, 0.0, 0.75, 0.0)), ("Quart Out", (0.25, 1.0, 0.5, 1.0)), ("Quart In Out", (0.76, 0.0, 0.24, 1.0)),
    ("Quint In", (0.64, 0.0, 0.78, 0.0)), ("Quint Out", (0.22, 1.0, 0.36, 1.0)), ("Quint In Out", (0.83, 0.0, 0.17, 1.0)),
    ("Expo In", (0.7, 0.0, 0.84, 0.0)), ("Expo Out", (0.16, 1.0, 0.3, 1.0)), ("Expo In Out", (0.87, 0.0, 0.13, 1.0)),
    ("Circ In", (0.55, 0.0, 1.0, 0.45)), ("Circ Out", (0.0, 0.55, 0.45, 1.0)), ("Circ In Out", (0.85, 0.0, 0.15, 1.0)),
    ("Back In", (0.36, 0.0, 0.66, -0.56)), ("Back Out", (0.34, 1.56, 0.64, 1.0)), ("Back In Out", (0.68, -0.6, 0.32, 1.6)),
]

_builtin_cache = []


def builtin_presets():
    """(name, curve, meta). The easings.net set opens in Bezier; Bounce /
    Elastic open their own tab with the settings (meta)."""
    if not _builtin_cache:
        p = default_params()
        out = [(name, from_bezier(*b), None) for name, b in BEZIERS]
        for mode, gen in (("Bounce", gen_bounce), ("Elastic", gen_elastic)):
            c = gen(p[mode])
            for suffix, rev in (("Out", False), ("In", True)):
                params = dict(p[mode], reversed=rev)
                out.append(("%s %s" % (mode, suffix), reverse(c) if rev else c, {"mode": mode, "params": params}))
        _builtin_cache.extend(out)
    return _builtin_cache


def _profiles_dir():
    """One JSON file per profile: [{"name", "curve", "meta"}]. The old single
    presets.json becomes the "My Presets" profile."""
    d = os.path.join(prefs_dir(), "profiles")
    os.makedirs(d, exist_ok=True)
    old = os.path.join(prefs_dir(), "presets.json")
    if os.path.exists(old) and not any(f.endswith(".json") for f in os.listdir(d)):
        shutil.move(old, os.path.join(d, "My Presets.json"))
    return d


def _safe(name):
    return "".join("_" if c in '<>:"/\\|?*' else c for c in name).strip() or "Presets"


def profile_names():
    d = _profiles_dir()
    names = sorted(f[:-5] for f in os.listdir(d) if f.lower().endswith(".json"))
    if not names:
        with open(os.path.join(d, "My Presets.json"), "w", encoding="utf-8") as fh:
            fh.write("[]")
        names = ["My Presets"]
    return names


def active_profile():
    names = profile_names()
    name = ui_prefs().get("profile")
    return name if name in names else names[0]


def _profile_path(name):
    return os.path.join(_profiles_dir(), _safe(name) + ".json")


def _presets_path():
    return _profile_path(active_profile())


_user_cache = {"key": None, "items": []}


def user_presets():
    """Your presets, re-read only when presets.json changes (draws call this a lot)."""
    path = _presets_path()
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return []
    if key != _user_cache["key"]:
        try:
            with open(_presets_path(), encoding="utf-8") as fh:
                data = json.load(fh)
            _user_cache["items"] = [(p["name"], p["curve"], p.get("meta")) for p in data if p.get("name") and p.get("curve")]
        except Exception:
            _user_cache["items"] = []
        _user_cache["key"] = key
    return list(_user_cache["items"])


def save_user_presets(items):
    with open(_presets_path(), "w", encoding="utf-8") as fh:
        json.dump([{"name": n, "curve": c, "meta": m} for n, c, m in items], fh, indent=1)


def library_items():
    """[(is_user, name, curve)] - yours first."""
    mine = [(True, n, c, m) for n, c, m in user_presets()]
    if not ui_prefs().get("builtins", True):
        return mine
    return mine + [(False, n, c, m) for n, c, m in builtin_presets()]


# ═════════════════════════════════════════════════════════════ panel state
# Runs from ease.pyp's forwarding Panel / Editor classes. State on the dialog:
#   dlg.mode       the tab
#   dlg.params     generator settings, per tab
#   dlg.saved      the Bezier and Custom tabs' own curves
#   dlg.curve      what the editor shows and Apply uses

QT_MODE, UA_EDITOR, E_CURVE, B_PLAY, B_REVERSE, B_RESET = 2000, 2001, 2002, 2003, 2004, 2005
B_APPLY, B_COPY, B_PASTE, B_PASTE_REV, T_STATUS, B_TO_CUSTOM = 2006, 2007, 2008, 2009, 2010, 2011
G_PARAMS, UA_LIB, G_LIB, B_SAVE, P_BASE = 2012, 2013, 2014, 2015, 2200
B_LIBTOGGLE, G_LIBCOL, G_MAIN, B_SPEED = 2016, 2017, 2018, 2019
C_PROFILE, B_LIBMENU = 2020, 2021

C_BG = c4d.Vector(0.067)
C_GRID = c4d.Vector(0.16)
C_BOX = c4d.Vector(0.62)
C_CURVE = c4d.Vector(0.93, 0.27, 0.2)         # fallback only - the theme's colour is used (_curve_col)


def _theme(cid, fallback):
    try:
        v = gui.GetGuiWorldColor(cid)
        return v if isinstance(v, c4d.Vector) else fallback
    except Exception:
        return fallback


def _curve_col():
    """The C4D scheme's focus colour (blue by default): the curve and its dots."""
    return _theme(c4d.COLOR_SLIDER_BAR_FOCUS, C_CURVE)


def _accent():
    """The scheme's active-tab colour: APPLY and 'on' toggles match the lit tab."""
    return _theme(c4d.COLOR_QUICKTAB_BG_ACTIVE, c4d.Vector(0.33, 0.35, 0.6))
C_HANDLE = c4d.Vector(0.92)
C_HANDLE_HOT = c4d.Vector(1.0, 0.72, 0.25)
C_LABEL = c4d.Vector(0.45)
C_PLAYHEAD = c4d.Vector(0.3)
PAD = 18
STRIP = 26                                 # the motion preview track, down the graph's right side
HIT = 9                                    # px: grab radius for handles and points
PLAY_S, HOLD_S = 1.0, 0.35                 # preview: one second of motion, a short hold


def _status(dlg, text):
    c4d.StatusSetText("Ease: " + text)
    dlg.status_text = text
    try:
        if dlg.IsOpen():
            dlg.SetString(T_STATUS, text)
    except Exception:
        pass


def _hint(dlg, text):
    """A hover description in the panel's status line (C4D's own tooltips
    depend on a preference and a delay); None puts the last message back."""
    try:
        dlg.SetString(T_STATUS, text if text is not None else getattr(dlg, "status_text", ""))
    except Exception:
        pass


def _ensure(dlg):
    if not isinstance(getattr(dlg, "params", None), dict):
        dlg.params = default_params()
    for m, d in default_params().items():            # panels from before a setting existed
        dlg.params.setdefault(m, d)
        for k, v in d.items():
            dlg.params[m].setdefault(k, v)
    if not isinstance(getattr(dlg, "saved", None), dict):
        start = [list(n) for n in (getattr(dlg, "curve", None) or stored() or DEFAULT)]
        dlg.saved = {"Bezier": start if is_simple(start) else [list(n) for n in DEFAULT],
                     "Custom": start if not is_simple(start) else [list(n) for n in DEFAULT]}
    if getattr(dlg, "mode", None) not in MODES:           # (also a panel left on the removed Wave tab)
        dlg.mode = "Bezier"
    if not isinstance(getattr(dlg, "history", None), list):
        dlg.history, dlg.future = [], []


def _state(dlg):
    return {"mode": dlg.mode, "params": json.loads(json.dumps(dlg.params)),
            "saved": json.loads(json.dumps(dlg.saved)), "curve": [list(n) for n in dlg.curve]}


def refresh(dlg, text=True, rebuild=False):
    """Recompute dlg.curve for the tab, then update the editor / field / tabs."""
    if dlg.mode in GENERATED:
        dlg.curve = generate(dlg.mode, dlg.params, dlg.saved["Bezier"])
    else:
        dlg.curve = [list(n) for n in dlg.saved[dlg.mode]]
    try:
        if text:
            dlg.SetString(E_CURVE, to_text(dlg.curve))
        if rebuild:
            _select_tab(dlg)
            _build_params(dlg)
        dlg.editor.Redraw()
    except Exception:
        pass


def set_curve(dlg, curve):
    """Show a curve as your own: two handles -> the Bezier tab, more -> Custom."""
    dlg.sel = set()
    curve = [list(n) for n in curve]
    mode = "Bezier" if is_simple(curve) else "Custom"
    dlg.saved[mode] = curve
    dlg.mode = mode
    refresh(dlg, rebuild=True)


# ---- undo: the panel's own history (C4D's covers only the scene).
# ("state", snapshot) for edits, ("keys", snapshot) for Apply / Paste -
# undoing that one hands over to C4D's undo.

def checkpoint(dlg, kind="state", snapshot=None):
    _ensure(dlg)
    dlg.history.append((kind, snapshot or _state(dlg)))
    del dlg.history[:-200]
    dlg.future = []
    dlg.last_action = None


def _restore(dlg, st):
    dlg.sel = set()
    dlg.mode, dlg.params, dlg.saved = st["mode"] if st["mode"] in MODES else "Bezier", st["params"], st["saved"]
    refresh(dlg, rebuild=True)
    dlg.curve = [list(n) for n in st["curve"]]
    dlg.SetString(E_CURVE, to_text(dlg.curve))
    dlg.editor.Redraw()


def undo(dlg, redo=False):
    _ensure(dlg)
    src, dst = (dlg.future, dlg.history) if redo else (dlg.history, dlg.future)
    if not src:
        _status(dlg, "nothing to redo" if redo else "nothing to undo")
        return
    kind, snap = src.pop()
    dst.append((kind, _state(dlg)))
    if kind == "keys":
        doc = c4d.documents.GetActiveDocument()
        doc.DoRedo() if redo else doc.DoUndo()
        c4d.EventAdd()
    _restore(dlg, snap)
    dlg.last_action = None
    _status(dlg, ("redo" if redo else "undo") + (" (keys too)" if kind == "keys" else ""))


def undo_key(dlg, msg):
    """Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y from a keyboard InputEvent. True if used."""
    if msg.GetInt32(c4d.BFM_INPUT_DEVICE) != c4d.BFM_INPUT_KEYBOARD:
        return False
    ch, qual = msg.GetInt32(c4d.BFM_INPUT_CHANNEL), msg.GetInt32(c4d.BFM_INPUT_QUALIFIER)
    if not qual & c4d.QCTRL:
        return False
    if ch == ord("Z"):
        undo(dlg, redo=bool(qual & c4d.QSHIFT))
        return True
    if ch == ord("Y"):
        undo(dlg, redo=True)
        return True
    return False


# ═════════════════════════════════════════════════════════════ panel layout

def _button(dlg, bid, flags, label="", icon=None, primary=False, w=0, h=26, tip=""):
    b = ButtonArea(dlg, bid, label, icon, primary, w, h, tip)
    dlg.buttons[bid] = b
    dlg.AddUserArea(bid, flags, w, h)
    dlg.AttachUserArea(b, bid, c4d.USERAREAFLAGS_COREMESSAGE)
    return b


def panel_layout(dlg):
    _ensure(dlg)
    dlg.buttons = {}
    dlg.show_library = ui_prefs().get("library", True)
    dlg.SetTitle("Ease")
    dlg.GroupBorderSpace(4, 4, 4, 4)
    dlg.GroupBegin(G_MAIN, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 2, 1)
    dlg.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0)
    dlg.GroupSpace(0, 4)

    if dlg.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 1):
        bc = c4d.BaseContainer()
        bc[c4d.QUICKTAB_BAR] = False
        bc[c4d.QUICKTAB_SHOWSINGLE] = True
        bc[c4d.QUICKTAB_NOMULTISELECT] = True
        dlg.tabs = dlg.AddCustomGui(QT_MODE, c4d.CUSTOMGUI_QUICKTAB, "", c4d.BFH_SCALEFIT, 0, 0, bc)
        for i, name in enumerate(MODES):
            dlg.tabs.AppendString(i, name, name == dlg.mode)
        _button(dlg, B_LIBTOGGLE, c4d.BFH_RIGHT, icon="library", w=30, tip="Show / hide the library")
    dlg.GroupEnd()

    dlg.AddUserArea(UA_EDITOR, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 260, 260)
    dlg.AttachUserArea(dlg.editor, UA_EDITOR)
    dlg.GroupBegin(G_PARAMS, c4d.BFH_SCALEFIT, 2, 0)
    dlg.GroupEnd()

    if dlg.GroupBegin(0, c4d.BFH_SCALEFIT, 4, 1):
        _button(dlg, B_PLAY, c4d.BFH_LEFT, icon="play", w=34, tip="Preview the timing (loops)")
        dlg.AddEditText(E_CURVE, c4d.BFH_SCALEFIT, initw=120)
        _button(dlg, B_REVERSE, c4d.BFH_RIGHT, icon="reverse", w=34, tip="Reverse: ease-in <-> ease-out")
        _button(dlg, B_RESET, c4d.BFH_RIGHT, icon="reset", w=34, tip="Reset this tab")
    dlg.GroupEnd()

    _button(dlg, B_APPLY, c4d.BFH_SCALEFIT, "APPLY", primary=True, h=40,
            tip="Apply to every pair of selected keys")

    if dlg.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 1):
        _button(dlg, B_COPY, c4d.BFH_SCALEFIT, "Copy", "copy", tip="Copy the selected keys' ease")
        _button(dlg, B_PASTE, c4d.BFH_SCALEFIT, "Paste", "paste", tip="Paste the copied ease onto the selected keys")
        _button(dlg, B_PASTE_REV, c4d.BFH_SCALEFIT, "Reversed", "paste_rev",
                tip="Paste the copied ease mirrored (ease-in <-> ease-out)")
    dlg.GroupEnd()
    dlg.AddStaticText(T_STATUS, c4d.BFH_SCALEFIT, name="")
    dlg.GroupEnd()

    dlg.GroupBegin(G_LIBCOL, c4d.BFH_RIGHT | c4d.BFV_SCALEFIT, 1, 0)
    if dlg.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 1):
        dlg.AddComboBox(C_PROFILE, c4d.BFH_SCALEFIT, 100)
        _button(dlg, B_SAVE, c4d.BFH_RIGHT, "Save", "save", tip="Save the current curve into this profile")
        _button(dlg, B_LIBMENU, c4d.BFH_RIGHT, icon="more", w=26,
                tip="Profiles: new, rename, delete, import, export - and built-in presets on / off")
    dlg.GroupEnd()
    dlg.ScrollGroupBegin(G_LIB, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT,
                         LIB_W + 16, 200)
    dlg.library = LibraryArea(dlg)
    dlg.AddUserArea(UA_LIB, c4d.BFH_SCALEFIT | c4d.BFV_TOP, LIB_W, 200)
    dlg.AttachUserArea(dlg.library, UA_LIB, c4d.USERAREAFLAGS_COREMESSAGE)
    dlg.GroupEnd()
    dlg.GroupEnd()
    dlg.GroupEnd()
    return True


def _select_tab(dlg):
    """Light the current tab. Select() alone didn't repaint it, so the
    strings are rebuilt with the right one selected."""
    tabs = getattr(dlg, "tabs", None)
    if tabs:
        tabs.ClearStrings()
        for i, name in enumerate(MODES):
            tabs.AppendString(i, name, name == dlg.mode)
        tabs.DoLayoutChange()


HINTS = {"Bezier": "Drag the handles (Shift: one axis, Ctrl: snap)",
         "Custom": "Double-click adds a point, right-click deletes one. Alt breaks a point's handles.",
         "Steps": "Drag the handles to ease the steps"}


def _build_params(dlg):
    """The tab's controls under the editor."""
    dlg.LayoutFlushGroup(G_PARAMS)
    spec = PARAMS.get(dlg.mode)
    if dlg.mode == "Bezier":
        dlg.AddStaticText(0, c4d.BFH_SCALEFIT, name=HINTS["Bezier"])
        _button(dlg, B_SPEED, c4d.BFH_RIGHT, "Speed Graph", "speed",
                tip="Show speed over time instead of value (drag: influence and speed, like AE)")
    elif not spec:
        dlg.AddStaticText(0, c4d.BFH_SCALEFIT, name=HINTS[dlg.mode])
        dlg.AddStaticText(0, c4d.BFH_LEFT, name="")
    else:
        p = dlg.params[dlg.mode]
        for i, (key, label, kind, lo, hi, step, _) in enumerate(spec):
            gid = P_BASE + i
            if kind == "bool":
                dlg.AddStaticText(0, c4d.BFH_LEFT, name="")
                dlg.AddCheckbox(gid, c4d.BFH_LEFT, 0, 0, label)
                dlg.SetBool(gid, bool(p[key]))
                continue
            dlg.AddStaticText(0, c4d.BFH_LEFT, initw=80, name=label)
            dlg.AddEditSlider(gid, c4d.BFH_SCALEFIT)
            if kind == "int":
                dlg.SetInt32(gid, int(p[key]), lo, hi, step)
            else:
                dlg.SetFloat(gid, float(p[key]), lo, hi, step, c4d.FORMAT_PERCENT if kind == "pct" else c4d.FORMAT_FLOAT)
        dlg.AddStaticText(0, c4d.BFH_LEFT, name="")
        if dlg.mode == "Steps":
            dlg.AddStaticText(0, c4d.BFH_SCALEFIT, name=HINTS["Steps"])
        else:
            dlg.AddButton(B_TO_CUSTOM, c4d.BFH_LEFT, name="Edit as Custom")
    dlg.LayoutChanged(G_PARAMS)


def panel_init(dlg):
    _ensure(dlg)
    dlg.playing = getattr(dlg, "playing", False)
    dlg.timer_on = 0
    dlg.lib_hover = None
    dlg.hovered = None
    _update_timer(dlg)
    refresh(dlg, rebuild=True)
    dlg.HideElement(G_LIBCOL, not getattr(dlg, "show_library", True))
    dlg.LayoutChanged(G_MAIN)
    fill_profiles(dlg)
    _status(dlg, "Ctrl+Z / Ctrl+Shift+Z undo and redo while the editor or library is focused")
    return True


def _play(dlg, on):
    dlg.playing = on
    dlg.play_t0 = time.time()
    _update_timer(dlg)
    b = getattr(dlg, "buttons", {}).get(B_PLAY)
    if b:
        b.Redraw()
    dlg.editor.Redraw()


def panel_timer(dlg, msg):
    _watch_hover(dlg)
    if getattr(dlg, "playing", False):
        dlg.editor.Redraw()
    if getattr(dlg, "lib_hover", None) is not None:
        dlg.library.Redraw()


def panel_core_message(dlg, id, msg):
    return True


def panel_command(dlg, id, msg):
    _ensure(dlg)
    if id == E_CURVE:
        try:
            new = parse(dlg.GetString(E_CURVE))
        except Exception:
            return True                              # half-typed: keep the last good curve
        if getattr(dlg, "last_action", None) != "text":
            checkpoint(dlg)                          # one undo step per bout of typing
        dlg.last_action = "text"
        mode = "Bezier" if is_simple(new) else "Custom"
        dlg.saved[mode] = new
        changed = mode != dlg.mode
        dlg.mode = mode
        refresh(dlg, text=False, rebuild=changed)
        return True
    if P_BASE <= id < P_BASE + 10 and dlg.mode in PARAMS:
        key, _, kind = PARAMS[dlg.mode][id - P_BASE][:3]
        if getattr(dlg, "last_action", None) != ("param", id):
            checkpoint(dlg)                          # one undo step per slider drag
        dlg.last_action = ("param", id)
        dlg.params[dlg.mode][key] = dlg.GetBool(id) if kind == "bool" else (
            dlg.GetInt32(id) if kind == "int" else dlg.GetFloat(id))
        refresh(dlg)
        return True
    dlg.last_action = None
    if id == QT_MODE:
        # the tab just clicked = the selected one that isn't the current tab
        picked = [m for i, m in enumerate(MODES) if dlg.tabs.IsSelected(i)]
        new = next((m for m in picked if m != dlg.mode), dlg.mode)
        if new != dlg.mode:
            checkpoint(dlg)
            dlg.sel = set()
            dlg.mode = new
            refresh(dlg)
            _build_params(dlg)
        if len(picked) != 1:
            _select_tab(dlg)                         # keep exactly one lit
    elif id == B_TO_CUSTOM:
        checkpoint(dlg)
        dlg.saved["Custom"] = [list(n) for n in dlg.curve]
        dlg.mode = "Custom"
        refresh(dlg, rebuild=True)
        _status(dlg, "now editable point by point")
    elif id == B_PLAY:
        _play(dlg, not getattr(dlg, "playing", False))
    elif id == B_LIBTOGGLE:
        show_library(dlg, not getattr(dlg, "show_library", True))
    elif id == B_SPEED:
        dlg.speed = not getattr(dlg, "speed", False)
        b = dlg.buttons.get(B_SPEED)
        if b:
            b.Redraw()
        dlg.editor.Redraw()
    elif id == B_REVERSE:
        checkpoint(dlg)
        if dlg.mode in GENERATED:
            p = dlg.params[dlg.mode]
            p["reversed"] = not p.get("reversed")
        else:
            dlg.saved[dlg.mode] = reverse(dlg.saved[dlg.mode])
        refresh(dlg)
    elif id == B_RESET:
        checkpoint(dlg)
        if dlg.mode in GENERATED:
            dlg.params[dlg.mode] = default_params()[dlg.mode]
            refresh(dlg, rebuild=True)
        else:
            dlg.saved[dlg.mode] = [list(n) for n in DEFAULT]
            refresh(dlg)
    elif id == B_SAVE:
        save_preset(dlg)
    elif id == C_PROFILE:
        names = profile_names()
        k = dlg.GetInt32(C_PROFILE)
        if 0 <= k < len(names):
            switch_profile(dlg, names[k])
    elif id == B_LIBMENU:
        profile_menu(dlg)
    elif id == B_APPLY:
        _apply_current(dlg)
    elif id == B_COPY:
        cmd_copy(dlg)
    elif id == B_PASTE:
        cmd_paste(dlg)
    elif id == B_PASTE_REV:
        cmd_paste_reversed(dlg)
    return True


def _apply_current(dlg, reverse_it=False):
    before = _state(dlg)
    text = apply(c4d.documents.GetActiveDocument(), reverse(dlg.curve) if reverse_it else dlg.curve)
    if text.startswith("eased"):
        checkpoint(dlg, "keys", before)
    _status(dlg, ("reversed: " if reverse_it else "") + text)


# ---- the commands (also bound to Ease: Copy / Paste / Paste Reversed)

def cmd_copy(dlg):
    curve, text = copy(c4d.documents.GetActiveDocument())
    if curve:
        store(curve)
        if dlg.IsOpen():
            _ensure(dlg)
            checkpoint(dlg)
            set_curve(dlg, curve)
    _status(dlg, text)


def _paste(dlg, rev):
    curve = stored()
    if not curve:
        _status(dlg, "nothing copied yet - select keys and Copy first")
        return
    if rev:
        curve = reverse(curve)
    before = _state(dlg) if dlg.IsOpen() else None
    text = apply(c4d.documents.GetActiveDocument(), curve)
    if dlg.IsOpen():
        if text.startswith("eased"):
            checkpoint(dlg, "keys", before)
        set_curve(dlg, curve)
    _status(dlg, ("reversed: " if rev else "") + text)


def cmd_paste(dlg):
    _paste(dlg, False)


def cmd_paste_reversed(dlg):
    _paste(dlg, True)


def save_preset(dlg):
    name = gui.RenameDialog("My Ease")               # None on Cancel (InputDialog can't tell)
    if not name or not name.strip():
        return
    name = name.strip()
    items = [it for it in user_presets() if it[0] != name]
    meta = {"mode": dlg.mode, "params": json.loads(json.dumps(dlg.params[dlg.mode]))} if dlg.mode in GENERATED else None
    items.insert(0, (name, [list(n) for n in dlg.curve], meta))
    save_user_presets(items)
    dlg.picked = (True, name)
    dlg.library.LayoutChanged()
    dlg.library.Redraw()
    _status(dlg, "saved %s in %s" % (name, active_profile()))


# ═════════════════════════════════════════════════════════════ editor

def editor_min_size(ua):
    return 260, 240


def _frame(ua):
    """(left, top, right, bottom of the graph, vmin, vmax)."""
    w, h = ua.GetWidth(), ua.GetHeight()
    curve = ua.dlg.curve
    lock = getattr(ua, "range_lock", None)
    if lock:
        vmin, vmax = lock
    elif _speed_view(ua.dlg):
        sp = [v for _, v in _speed_samples(curve)] + [v for _, v in _speed_handles(curve)]
        top_v = max(1.25, max(sp) * 1.08)
        vmin = min(0.0, min(sp)) - 0.04 * top_v        # the baseline sits right at the bottom
        vmax = top_v
    else:
        vals = [n[1] for n in curve] + [n[1] + n[3] for n in curve] + [n[1] + n[5] for n in curve]
        vmin = min(-0.12, min(vals) - 0.08)
        vmax = max(1.12, max(vals) + 0.08)
    aw, ah = max(1, w - 2 * PAD - STRIP), max(1, h - 2 * PAD)
    if _speed_view(ua.dlg):                          # speed has its own units: fill the area
        return PAD, PAD, PAD + aw, PAD + ah, vmin, vmax
    # value graph: square units - the 0..1 box is square. Spare height shows
    # more value range (room for overshoot), spare width centres the box.
    k = min(float(aw), ah / (vmax - vmin))
    if ah > k * (vmax - vmin) + 0.5:
        extra = (ah / k - (vmax - vmin)) / 2.0
        vmin, vmax = vmin - extra, vmax + extra
    left = int(round(PAD + (aw - k) / 2.0))          # whole pixels: DrawLine takes ints
    return left, PAD, left + int(round(k)), PAD + ah, vmin, vmax


def _to_px(fr, t, v):
    l, top, r, b, vmin, vmax = fr
    return l + t * (r - l), b - (v - vmin) / (vmax - vmin) * (b - top)


def _to_curve(fr, x, y):
    l, top, r, b, vmin, vmax = fr
    return (x - l) / float(max(1, r - l)), vmin + (b - y) / float(max(1, b - top)) * (vmax - vmin)


def _edit_curve(dlg):
    """The curve your mouse edits in this tab (None: generated, use the sliders)."""
    if dlg.mode in ("Bezier", "Custom"):
        return dlg.saved[dlg.mode]
    if dlg.mode == "Steps":
        return dlg.params["Steps"]["ease"]
    return None


def _editable(dlg):
    return _edit_curve(dlg) is not None


def _speed_view(dlg):
    return dlg.mode == "Bezier" and getattr(dlg, "speed", False)


def _speed_at_s(curve, s):
    """(x, speed) at bezier parameter s. Speed = value change per time,
    so 1 = the average (linear) speed."""
    a, b = curve[0], curve[1]
    p1x, p1y, p2x, p2y = a[4], a[5], 1 + b[2], 1 + b[3]
    u = 1 - s
    x = 3 * u * u * s * p1x + 3 * u * s * s * p2x + s * s * s
    dx = 3 * u * u * p1x + 6 * u * s * (p2x - p1x) + 3 * s * s * (1 - p2x)
    dy = 3 * u * u * p1y + 6 * u * s * (p2y - p1y) + 3 * s * s * (1 - p2y)
    return x, max(-20.0, min(20.0, dy / max(dx, 1e-4)))


def _speed_samples(curve, n=120):
    return [_speed_at_s(curve, i / float(n)) for i in range(n + 1)]


def _speed_handles(curve):
    """(where the handle sits, speed) at the start and the end.
    The mapping: a handle sits at HALF its bezier handle's reach -
    x1 / 2 from the start, (1 - x2) / 2 from the end - so the in-handle lives
    in the left half, the out-handle in the right half, and they can't
    cross. Both at the middle = x1 1, x2 0: the sharp spike. The height is
    the real speed at that key (1 = linear's speed)."""
    a, b = curve[0], curve[1]
    x1, y1, x2, y2 = a[4], a[5], 1 + b[2], 1 + b[3]
    return (x1 / 2.0, y1 / max(x1, 1e-4)), (1.0 - (1.0 - x2) / 2.0, (1 - y2) / max(1 - x2, 1e-4))


def _speed_move(curve, kind, t, v):
    a, b = curve[0], curve[1]
    if kind == "sp_in":                              # left half only: handle at x1 / 2
        x1 = min(max(2.0 * t, 0.01), 1.0)
        a[4], a[5] = x1, v * x1
    else:                                            # right half only: handle at 1 - (1 - x2) / 2
        x2 = min(max(1.0 - 2.0 * (1.0 - t), 0.0), 0.99)
        b[2], b[3] = x2 - 1.0, -v * (1.0 - x2)


def _points(dlg):
    """Everything grabbable: (kind, node index, t, v). Handles first, so they
    win over their own node. None in the generated tabs."""
    if not _editable(dlg):
        return []
    if _speed_view(dlg):
        (x1, s0), (x2, s1) = _speed_handles(dlg.saved["Bezier"])
        return [("sp_in", 0, x1, s0), ("sp_out", 1, x2, s1)]
    curve = _edit_curve(dlg)
    out, last = [], len(curve) - 1
    for i, n in enumerate(curve):
        t, v, lt, lv, rt, rv = n[:6]
        if i > 0 and not is_step(curve[i - 1]):
            out.append(("left", i, t + lt, v + lv))
        if i < last and not is_step(n):
            out.append(("right", i, t + rt, v + rv))
    if dlg.mode == "Custom":
        for i, n in enumerate(curve):
            if 0 < i < last:
                out.append(("node", i, n[0], n[1]))
    return out


def _draw_curve(ua, curve, to_px, width):
    for a, c in zip(curve[:-1], curve[1:]):
        x0, y0 = to_px(a[0], a[1])
        if is_step(a):
            x1, y1 = to_px(c[0], a[1])
            x2, y2 = to_px(c[0], c[1])
            ua.DrawPolyLine([x0, y0, x1, y1, x2, y2], False, width)
            continue
        pts = []
        for t, v in ((a[0] + a[4], a[1] + a[5]), (c[0] + c[2], c[1] + c[3]), (c[0], c[1])):
            pts += list(to_px(t, v))
        ua.DrawBezierLine([x0, y0], pts, False, width)


def editor_draw(ua, x1, y1, x2, y2, msg):
    ua.OffScreenOn()
    ua.DrawSetPen(C_BG)
    ua.DrawRectangle(x1, y1, x2, y2)
    dlg = ua.dlg
    curve = getattr(dlg, "curve", None) or DEFAULT
    fr = _frame(ua)
    l, top, r, b, vmin, vmax = fr
    ua.DrawSetPen(C_GRID)
    for k in range(1, 4):
        x = int(l + (r - l) * k / 4.0)
        ua.DrawLine(x, top, x, b)
    for k in range(-8, 13):
        v = k / 4.0
        if vmin <= v <= vmax and k not in (0, 4):
            y = int(_to_px(fr, 0, v)[1])
            ua.DrawLine(l, y, r, y)
    ua.DrawSetPen(C_BOX)
    for v in (0.0, 1.0):
        y = int(_to_px(fr, 0, v)[1])
        ua.DrawLine(l, y, r, y)
    ua.DrawLine(int(l), top, int(l), b)
    ua.DrawLine(int(r), top, int(r), b)
    ua.DrawSetTextCol(C_LABEL, c4d.COLOR_TRANS)
    ua.DrawText("0", int(l) + 3, int(_to_px(fr, 0, 0)[1]) + 3)
    ua.DrawText("1", int(r) - 10, int(_to_px(fr, 0, 1)[1]) - 15)

    p = None
    if getattr(dlg, "playing", False):
        el = (time.time() - getattr(dlg, "play_t0", time.time())) % (PLAY_S + HOLD_S)
        p = min(1.0, el / PLAY_S)
        if _speed_view(dlg):                         # speed graph: a time line through the dot
            px = int(l + p * (r - l))
            ua.DrawSetPen(C_PLAYHEAD)
            ua.DrawLine(px, top, px, b)

    if _speed_view(dlg):
        _draw_speed(ua, dlg, fr, p)
        return
    if dlg.mode == "Steps":                          # the ease the steps follow, faint behind them
        ua.DrawSetPen(c4d.Vector(0.4))
        _draw_curve(ua, _edit_curve(dlg), lambda t, v: _to_px(fr, t, v), 1.5)
    ua.DrawSetPen(_curve_col())
    _draw_curve(ua, curve, lambda t, v: _to_px(fr, t, v), 2.5)

    hot = getattr(ua, "hot", None)
    ec = _edit_curve(dlg) or curve
    sel = getattr(dlg, "sel", set())
    for kind, i, t, v in _points(dlg):
        X, Y = _to_px(fr, t, v)
        if (kind, i) in sel:                         # selected: a ring in the curve colour
            ua.DrawSetPen(_curve_col())
            ua.DrawEllipseFill([X, Y], [7.5, 7.5])
        if kind != "node":
            nx, ny = _to_px(fr, ec[i][0], ec[i][1])
            ua.DrawSetPen(C_HANDLE)
            ua.DrawPolyLine([nx, ny, X, Y], False, 1.2)
            ua.DrawSetPen(C_HANDLE_HOT if hot == (kind, i) else C_HANDLE)
            ua.DrawEllipseFill([X, Y], [4.0, 4.0])      # handles: a touch smaller than points
        else:
            ua.DrawSetPen(C_HANDLE_HOT if hot == (kind, i) else C_HANDLE)
            ua.DrawEllipseFill([X, Y], [5.0, 5.0])
            ua.DrawSetPen(_curve_col())
            ua.DrawEllipseFill([X, Y], [3.0, 3.0])

    band = getattr(ua, "band", None)
    if band:
        bx0, by0, bx1, by1 = band
        ua.DrawSetOpacity(0.12)
        ua.DrawSetPen(_curve_col())
        ua.DrawRectangle(int(min(bx0, bx1)), int(min(by0, by1)), int(max(bx0, bx1)), int(max(by0, by1)))
        ua.DrawSetOpacity(1.0)
        ua.DrawSetPen(_curve_col())
        ua.DrawPolyLine([bx0, by0, bx1, by0, bx1, by1, bx0, by1], True, 1.0)
    if p is not None:
        # the dot on the curve, a guide line at its height, and the thing
        # being animated on a track down the right side - all one height
        v = evaluate(curve, p)
        X, Y = _to_px(fr, p, v)
        sx = r + STRIP * 0.6
        ua.DrawSetPen(C_GRID)
        ua.DrawLine(int(sx), int(top), int(sx), int(b))
        ua.DrawSetPen(C_PLAYHEAD)
        ua.DrawLine(int(l), int(Y), int(sx), int(Y))
        ua.DrawSetPen(_curve_col())
        ua.DrawEllipseFill([X, Y], [5.0, 5.0])
        ua.DrawEllipseFill([sx, Y], [6.0, 6.0])


def _hit(ua, x, y):
    fr = _frame(ua)
    best, dist = None, HIT
    for kind, i, t, v in _points(ua.dlg):
        X, Y = _to_px(fr, t, v)
        d = math.hypot(X - x, Y - y)
        if d <= dist:
            best, dist = (kind, i), d
    return best


def _snap(val, on, step=0.05):
    return round(val / step) * step if on else val


def _move(curve, kind, i, t, v, snap, free):
    """Put handle / node (kind, i) at (t, v), keeping time moving forward:
    a handle can't reach past its neighbouring node, a node stays between
    its neighbours. Inner nodes keep their two handles in line unless
    free (Alt)."""
    n = curve[i]
    t, v = _snap(t, snap), _snap(v, snap)
    last = len(curve) - 1
    if kind == "right":
        span = curve[i + 1][0] - n[0]
        n[4] = min(max(t - n[0], 0.0), span)
        n[5] = v - n[1]
        if 0 < i < last and not free:
            _align(n, "left")
    elif kind == "left":
        span = n[0] - curve[i - 1][0]
        n[2] = max(min(t - n[0], 0.0), -span)
        n[3] = v - n[1]
        if 0 < i < last and not free:
            _align(n, "right")
    else:
        lo, hi = curve[i - 1][0] + 0.01, curve[i + 1][0] - 0.01
        n[0] = min(max(t, lo), hi)
        n[1] = v
        n[2] = max(n[2], -(n[0] - curve[i - 1][0]))
        n[4] = min(n[4], curve[i + 1][0] - n[0])
        curve[i - 1][4] = min(curve[i - 1][4], n[0] - curve[i - 1][0])
        curve[i + 1][2] = max(curve[i + 1][2], -(curve[i + 1][0] - n[0]))


def _align(n, side):
    """Point the other handle opposite the one just moved, keeping its length."""
    if side == "left":
        dx, dy, length = n[4], n[5], math.hypot(n[2], n[3])
    else:
        dx, dy, length = n[2], n[3], math.hypot(n[4], n[5])
    d = math.hypot(dx, dy)
    if d < 1e-9:
        return
    ux, uy = -dx / d, -dy / d
    if side == "left":
        n[2], n[3] = ux * length, uy * length
    else:
        n[4], n[5] = ux * length, uy * length


def _add_node(curve, t, v):
    """A new point at (t, v), handles along the local slope."""
    if not (0.0 < t < 1.0):
        return None
    for i in range(len(curve) - 1):
        a, c = curve[i], curve[i + 1]
        if a[0] < t < c[0]:
            if t - a[0] < 0.02 or c[0] - t < 0.02:
                return None
            slope = (evaluate(curve, min(1.0, t + 0.01)) - evaluate(curve, max(0.0, t - 0.01))) / 0.02
            left, right = (t - a[0]) / 3.0, (c[0] - t) / 3.0
            node = [t, v, -left, -left * slope, right, right * slope]
            if is_step(a):
                node.append(1)                        # splitting a hold keeps both halves holds
            a[4] = min(a[4], t - a[0])
            c[2] = max(c[2], -(c[0] - t))
            curve.insert(i + 1, node)
            return i + 1
    return None


def editor_input(ua, msg):
    dlg = ua.dlg
    _ensure(dlg)
    if undo_key(dlg, msg):
        return True
    if msg.GetInt32(c4d.BFM_INPUT_DEVICE) == c4d.BFM_INPUT_KEYBOARD and \
            msg.GetInt32(c4d.BFM_INPUT_CHANNEL) in (c4d.KEY_DELETE, c4d.KEY_BACKSPACE):
        return _delete_selected(dlg)
    if msg.GetInt32(c4d.BFM_INPUT_DEVICE) != c4d.BFM_INPUT_MOUSE:
        return False
    dlg.Activate(UA_EDITOR)                            # so Ctrl+Z reaches the editor
    ch = msg.GetInt32(c4d.BFM_INPUT_CHANNEL)
    loc = ua.Global2Local()
    x = msg.GetInt32(c4d.BFM_INPUT_X) + loc["x"]
    y = msg.GetInt32(c4d.BFM_INPUT_Y) + loc["y"]

    if ch == c4d.BFM_INPUT_MOUSERIGHT and not _editable(dlg):
        editor_menu(ua, x, y)
        return True
    if not _editable(dlg):
        if ch == c4d.BFM_INPUT_MOUSELEFT:
            _status(dlg, "%s is generated - use its sliders, or Edit as Custom to shape it by hand" % dlg.mode)
        return True
    hit = _hit(ua, x, y)
    curve = _edit_curve(dlg)

    if ch == c4d.BFM_INPUT_MOUSERIGHT:
        if hit and hit[0] == "node" and hit in _selection(dlg) and len(dlg.sel) > 1:
            _delete_selected(dlg)
        elif hit and hit[0] == "node":
            checkpoint(dlg)
            del curve[hit[1]]
            dlg.sel = set()
            refresh(dlg)
            _status(dlg, "point removed")
        else:
            editor_menu(ua, x, y)
        return True
    if ch != c4d.BFM_INPUT_MOUSELEFT:
        return False

    fr = _frame(ua)
    if msg.GetBool(c4d.BFM_INPUT_DOUBLECLICK) and not hit:
        if dlg.mode != "Custom":
            _status(dlg, "%s has two handles - the Custom tab takes more points" % dlg.mode)
            return True
        before = _state(dlg)
        t, v = _to_curve(fr, x, y)
        if _add_node(curve, t, v) is not None:
            checkpoint(dlg, snapshot=before)
            refresh(dlg)
            _status(dlg, "point added - right-click it to remove")
        return True
    qual0 = msg.GetInt32(c4d.BFM_INPUT_QUALIFIER)
    sel = _selection(dlg)
    if not hit:
        if _speed_view(dlg):
            return True
        _band_select(ua, msg, x, y, add=bool(qual0 & c4d.QSHIFT))
        return True
    if hit[0] in ("sp_in", "sp_out"):
        group = [hit]
    elif qual0 & c4d.QSHIFT and hit not in sel:
        sel.add(hit)                               # Shift-click adds to the selection
        group = sorted(sel)
    elif hit in sel:
        group = sorted(sel)                        # drag the whole selection
    else:
        dlg.sel = sel = {hit}
        group = [hit]
    # a handle rides along with its point: don't move it twice
    nodes = {i for kind, i in group if kind == "node"}
    group = [g for g in group if g[0] == "node" or g[1] not in nodes or g == hit]

    ua.range_lock = fr[4:6]                        # the graph doesn't rescale under the mouse
    ua.hot = hit
    mx, my = float(msg.GetInt32(c4d.BFM_INPUT_X)), float(msg.GetInt32(c4d.BFM_INPUT_Y))
    ua.MouseDragStart(c4d.KEY_MLEFT, mx, my, c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE | c4d.MOUSEDRAGFLAGS_NOMOVE)
    mouse = lambda: _mouse_state(ua)
    first = mouse()
    pos = {(kind, i): (t, v) for kind, i, t, v in _points(dlg)}
    hx, hy = _to_px(fr, *pos[hit])
    before, moved = _state(dlg), False
    try:
        while True:
            res, dx, dy, channels = ua.MouseDrag()
            if res != c4d.MOUSEDRAGRESULT_CONTINUE:
                break
            now = mouse()
            if first is None or now is None:
                continue
            ddx, ddy = now[0] - first[0], now[1] - first[1]
            if not (ddx or ddy):
                continue                           # a click is not a drag
            qual = now[2]
            if qual & c4d.QSHIFT:                  # Shift: only along the axis you're moving most
                if abs(ddx) >= abs(ddy):
                    ddy = 0
                else:
                    ddx = 0
            if not moved:
                checkpoint(dlg, snapshot=before)     # one undo step per drag
                moved = True
            frn = _frame(ua)
            t, v = _to_curve(frn, hx + ddx, hy + ddy)
            if hit[0] in ("sp_in", "sp_out"):
                y0 = _to_px(frn, 0.0, 0.0)[1]
                if abs((hy + ddy) - y0) < 8:           # rests on the baseline (speed 0) unless pulled off it
                    v = 0.0
                _speed_move(curve, hit[0], _snap(t, qual & c4d.QCTRL), _snap(v, qual & c4d.QCTRL))
            else:
                t, v = _snap(t, qual & c4d.QCTRL), _snap(v, qual & c4d.QCTRL)
                d_t, d_v = t - pos[hit][0], v - pos[hit][1]
                for kind, i in sorted(group, key=lambda g: g[0] != "node"):   # points first, then handles
                    bt, bv = pos[(kind, i)]
                    _move(curve, kind, i, bt + d_t, bv + d_v, False, bool(qual & c4d.QALT) or len(group) > 1)
            refresh(dlg)
    finally:
        ua.MouseDragEnd()
        ua.range_lock = None
        ua.hot = None
        ua.Redraw()
    return True


def _mouse_state(ua):
    """GetInputState, measured against its own first reading - MouseDrag's
    deltas carry a coordinate offset (a plain click nudged the handle)."""
    bc = c4d.BaseContainer()
    if ua.GetInputState(c4d.BFM_INPUT_MOUSE, c4d.BFM_INPUT_MOUSELEFT, bc):
        return bc.GetInt32(c4d.BFM_INPUT_X), bc.GetInt32(c4d.BFM_INPUT_Y), bc.GetInt32(c4d.BFM_INPUT_QUALIFIER)
    return None


def _selection(dlg):
    """dlg.sel, minus anything that no longer exists (after an undo, a delete...)."""
    valid = {(kind, i) for kind, i, t, v in _points(dlg)}
    dlg.sel = {g for g in getattr(dlg, "sel", set()) if g in valid}
    return dlg.sel


def _band_select(ua, msg, x, y, add):
    """Drag on empty space: a rubber band; what's inside gets selected
    (Shift: added). A click without dragging clears the selection."""
    dlg = ua.dlg
    mx, my = float(msg.GetInt32(c4d.BFM_INPUT_X)), float(msg.GetInt32(c4d.BFM_INPUT_Y))
    ua.MouseDragStart(c4d.KEY_MLEFT, mx, my, c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE | c4d.MOUSEDRAGFLAGS_NOMOVE)
    first = _mouse_state(ua)
    x1, y1 = x, y
    try:
        while True:
            res, dx, dy, ch = ua.MouseDrag()
            if res != c4d.MOUSEDRAGRESULT_CONTINUE:
                break
            now = _mouse_state(ua)
            if first is None or now is None:
                continue
            x1, y1 = x + now[0] - first[0], y + now[1] - first[1]
            ua.band = (x, y, x1, y1)
            ua.Redraw()
    finally:
        ua.MouseDragEnd()
        ua.band = None
    lo_x, hi_x, lo_y, hi_y = min(x, x1), max(x, x1), min(y, y1), max(y, y1)
    fr = _frame(ua)
    inside = set()
    if hi_x - lo_x > 3 or hi_y - lo_y > 3:
        for kind, i, t, v in _points(dlg):
            X, Y = _to_px(fr, t, v)
            if lo_x <= X <= hi_x and lo_y <= Y <= hi_y:
                inside.add((kind, i))
    dlg.sel = (_selection(dlg) | inside) if add else inside
    ua.Redraw()
    if dlg.sel:
        _status(dlg, "%d selected - drag one to move them all%s" % (
            len(dlg.sel), ", Delete removes points" if dlg.mode == "Custom" else ""))


def _delete_selected(dlg):
    curve = _edit_curve(dlg)
    doomed = sorted({i for kind, i in _selection(dlg) if kind == "node"}, reverse=True)
    if dlg.mode != "Custom" or not doomed:
        return False
    checkpoint(dlg)
    for i in doomed:
        del curve[i]
    dlg.sel = set()
    refresh(dlg)
    _status(dlg, "removed %d point%s" % (len(doomed), "" if len(doomed) == 1 else "s"))
    return True


# ═════════════════════════════════════════════════════════════ library

CELL_W, CELL_H, THUMB_H = 68, 66, 44
LIB_W = 3 * CELL_W + 8
C_CELL = c4d.Vector(0.1)
C_THUMB = c4d.Vector(0.72)
M_RENAME, M_DELETE, M_APPLY = c4d.FIRST_POPUP_ID + 1, c4d.FIRST_POPUP_ID + 2, c4d.FIRST_POPUP_ID + 3
M_APPLY_REV, M_REVERSE, M_RESET, M_COPY_TEXT, M_PASTE_TEXT = (c4d.FIRST_POPUP_ID + k for k in range(4, 9))
M_SAVE, M_SPEED, M_TO_CUSTOM, M_ADD_POINT = (c4d.FIRST_POPUP_ID + k for k in range(9, 13))


class LibraryArea(gui.GeUserArea):
    """The preset grid. Methods look the functions up at call time, so a
    reloaded ease_core takes over without reopening the panel."""

    def __init__(self, dlg):
        self.dlg = dlg
        self.width = LIB_W

    def GetMinSize(self):
        return library_min_size(self)

    def Sized(self, w, h):
        library_sized(self, w, h)

    def DrawMsg(self, x1, y1, x2, y2, msg):
        try:
            library_draw(self, x1, y1, x2, y2)
        except Exception:
            log_error("library draw")

    def InputEvent(self, msg):
        try:
            return library_input(self, msg)
        except Exception:
            log_error("library input")
            return True

    def Message(self, msg, result):
        try:
            if hover_message(self, msg, result):
                return True
        except Exception:
            log_error("library hover")
        return gui.GeUserArea.Message(self, msg, result)


LibraryArea.is_library = True


def _cols(width):
    return max(1, int(width) // CELL_W)


def library_min_size(ua):
    n = len(library_items())
    rows = (n + _cols(ua.width) - 1) // _cols(ua.width)
    return CELL_W, rows * CELL_H + 4


def library_sized(ua, w, h):
    if _cols(w) != _cols(ua.width):
        ua.width = w
        ua.LayoutChanged()
    ua.width = w


def library_draw(ua, x1, y1, x2, y2):
    ua.OffScreenOn()
    ua.DrawSetPen(c4d.COLOR_BG)
    ua.DrawRectangle(x1, y1, x2, y2)
    cols = _cols(ua.GetWidth())
    picked = getattr(ua.dlg, "picked", None)
    hov = getattr(ua.dlg, "lib_hover", None)
    for i, (mine, name, curve, meta) in enumerate(library_items()):
        cx, cy = (i % cols) * CELL_W + 2, (i // cols) * CELL_H + 2
        ua.DrawSetPen(C_CELL)
        ua.DrawRectangle(cx, cy, cx + CELL_W - 5, cy + THUMB_H)
        vals = [n[1] for n in curve]
        lo, hi = min(-0.05, min(vals)), max(1.05, max(vals))
        tx, ty, tw, th = cx + 7, cy + 5, CELL_W - 19, THUMB_H - 10
        hovering = hov is not None and hov[:2] == (mine, name)
        if hovering:
            ua.DrawSetPen(c4d.Vector(0.16))
            ua.DrawRectangle(cx, cy, cx + CELL_W - 5, cy + THUMB_H)
        to_px = lambda t, v: (tx + t * tw, ty + th - (v - lo) / (hi - lo) * th)
        ua.DrawSetPen(_curve_col() if picked == (mine, name) or hovering else C_THUMB)
        _draw_curve(ua, curve, to_px, 1.5)
        if hovering:                                 # the timing: a playhead line and a dot riding the curve
            gp = min(1.0, ((time.time() - getattr(ua.dlg, "play_t0", time.time())) % (PLAY_S + HOLD_S)) / PLAY_S)
            gx, gy = to_px(gp, evaluate(curve, gp))
            ua.DrawSetPen(c4d.Vector(0.45))              # a guide at the dot's height, like the editor
            ua.DrawLine(cx + 2, int(gy), cx + CELL_W - 7, int(gy))
            ua.DrawSetPen(c4d.Vector(1.0))
            ua.DrawEllipseFill([gx, gy], [3.0, 3.0])
        if mine:
            ua.DrawSetPen(_curve_col())
            ua.DrawRectangle(cx + CELL_W - 10, cy + 2, cx + CELL_W - 7, cy + 5)
        label = name
        while label and ua.DrawGetTextWidth(label) > CELL_W - 6:
            label = label[:-1]
        if label != name:
            label = label[:-1] + "."
        ua.DrawSetTextCol(c4d.COLOR_TEXT if picked == (mine, name) else C_LABEL, c4d.COLOR_TRANS)
        ua.DrawText(label, cx, cy + THUMB_H + 2)


def _lib_hit(ua, msg):
    loc = ua.Global2Local()
    return _lib_item_at(ua, msg.GetInt32(c4d.BFM_INPUT_X) + loc["x"], msg.GetInt32(c4d.BFM_INPUT_Y) + loc["y"])


def _lib_item_at(ua, x, y):
    cols = _cols(ua.GetWidth())
    c, r = int(x) // CELL_W, int(y) // CELL_H
    items = library_items()
    i = r * cols + c
    if 0 <= c < cols and r >= 0 and i < len(items):
        return items[i]
    return None


def load_preset(dlg, name, curve, mine, meta=None):
    """Opens the preset's own tab: Bezier / Custom by shape, or the generator
    tab (with its settings) it came from."""
    _ensure(dlg)
    checkpoint(dlg)
    dlg.picked = (mine, name)
    if meta and meta.get("mode") in GENERATED:
        dlg.mode = meta["mode"]
        dlg.params[dlg.mode].update(json.loads(json.dumps(meta.get("params") or {})))
        refresh(dlg, rebuild=True)
    else:
        set_curve(dlg, curve)
    try:
        dlg.library.Redraw()
    except Exception:
        pass


def library_input(ua, msg):
    dlg = ua.dlg
    _ensure(dlg)
    if undo_key(dlg, msg):
        return True
    if msg.GetInt32(c4d.BFM_INPUT_DEVICE) != c4d.BFM_INPUT_MOUSE:
        return False
    ch = msg.GetInt32(c4d.BFM_INPUT_CHANNEL)
    item = _lib_hit(ua, msg)
    if item is None:
        return True
    mine, name, curve, meta = item
    if ch == c4d.BFM_INPUT_MOUSELEFT:
        dlg.Activate(UA_LIB)
        load_preset(dlg, name, curve, mine, meta)
        if msg.GetBool(c4d.BFM_INPUT_DOUBLECLICK):
            _apply_current(dlg)
        else:
            _status(dlg, "%s - Apply, or double-click a preset to apply it straight away" % name)
        return True
    if ch == c4d.BFM_INPUT_MOUSERIGHT:
        bc = c4d.BaseContainer()
        bc.InsData(M_APPLY, "Apply to Selected Keys")
        bc.InsData(M_APPLY_REV, "Apply Reversed")
        if mine:
            bc.InsData(0, "")
            bc.InsData(M_RENAME, "Rename...")
            bc.InsData(M_DELETE, "Delete")
        res = gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        if res in (M_APPLY, M_APPLY_REV):
            load_preset(dlg, name, curve, mine, meta)
            _apply_current(dlg, reverse_it=res == M_APPLY_REV)
        elif res in (M_RENAME, M_DELETE):
            items = user_presets()
            idx = next((k for k, it in enumerate(items) if it[0] == name), None)
            if idx is None:
                return True
            if res == M_DELETE:
                del items[idx]
                _status(dlg, "deleted %s" % name)
            else:
                new = gui.RenameDialog(name)            # None on Cancel
                if not new or not new.strip():
                    return True
                items[idx] = (new.strip(),) + tuple(items[idx][1:])
                _status(dlg, "renamed to %s" % new.strip())
            save_user_presets(items)
            ua.LayoutChanged()
            ua.Redraw()
        return True
    return False


# ═════════════════════════════════════════════════════════════ buttons (M4)
# Drawn buttons: hover, an icon, and one big accent APPLY. A click calls
# panel_command with the button's id, like a normal gadget would.

C_ACCENT = c4d.Vector(0.78, 0.17, 0.03)            # the Goodies red, #c72c07
C_BTN = c4d.Vector(0.235)
C_BTN_HOVER = c4d.Vector(0.275)                     # one subtle shade up
C_BTN_DOWN = c4d.Vector(0.18)
C_ICON = c4d.Vector(0.88)


class ButtonArea(gui.GeUserArea):
    """Looks the functions up at call time: a reloaded core takes over."""

    def __init__(self, dlg, bid, label="", icon=None, primary=False, w=0, h=26, tip=""):
        self.dlg, self.bid, self.label, self.icon = dlg, bid, label, icon
        self.primary, self.w, self.h, self.tip = primary, w, h, tip
        self.hover = self.down = False

    def GetMinSize(self):
        return button_min_size(self)

    def DrawMsg(self, x1, y1, x2, y2, msg):
        try:
            button_draw(self, x1, y1, x2, y2)
        except Exception:
            log_error("button draw")

    def InputEvent(self, msg):
        try:
            return button_input(self, msg)
        except Exception:
            log_error("button input")
            return True

    def Message(self, msg, result):
        try:
            if hover_message(self, msg, result):
                return True
        except Exception:
            log_error("button hover")
        return gui.GeUserArea.Message(self, msg, result)


def button_min_size(b):
    w = b.w
    if not w:
        w = 30 + (b.DrawGetTextWidth(b.label) + 16 if b.label else 0)
    return w, b.h


def _tip(ua):
    """The hover description; toggles say what a click will do."""
    dlg, bid = ua.dlg, getattr(ua, "bid", None)
    if bid == B_LIBTOGGLE:
        return "Hide the library" if getattr(dlg, "show_library", True) else "Show the library"
    if bid == B_SPEED:
        return "Back to the value graph" if getattr(dlg, "speed", False) else             "Speed graph: speed over time - drag the handles for influence and speed, like AE"
    if bid == B_PLAY:
        return "Stop the preview" if getattr(dlg, "playing", False) else "Preview the timing (loops)"
    return getattr(ua, "tip", "")


def hover_message(ua, msg, result):
    """Shared by buttons and the library: pointer cursor, hover on / off."""
    mid = msg.GetId()
    if mid == c4d.BFM_GETCURSORINFO:
        result.SetInt32(c4d.RESULT_CURSOR, c4d.MOUSE_POINT_HAND)
        tip = _tip(ua) if not getattr(ua, "is_library", False) else ""
        if tip:
            result.SetString(c4d.RESULT_BUBBLEHELP, tip)
        if getattr(ua, "is_library", False):          # duck-typed: survives a reload
            library_hover(ua, msg)
        elif not ua.hover:
            prev = getattr(ua.dlg, "hovered", None)
            if prev is not None and prev is not ua:      # moved straight from one button to the next
                prev.hover = False
                prev.Redraw()
            ua.hover = True
            ua.dlg.hovered = ua
            _update_timer(ua.dlg)
            ua.Redraw()
            if tip:
                _hint(ua.dlg, tip)
        return True
    if mid == c4d.BFM_CURSORINFO_REMOVE:
        if getattr(ua, "is_library", False):
            library_hover(ua, None)
        elif ua.hover:
            ua.hover = False
            ua.Redraw()
            _hint(ua.dlg, None)
            if getattr(ua.dlg, "hovered", None) is ua:
                ua.dlg.hovered = None
                _update_timer(ua.dlg)
    return False


C4D_ICONS = {"play": 12412,                          # Play Forwards
             "copy": 12107, "paste": 12108,
             "reset": 1019940,                       # Reset Transform (a circular arrow)
             "reverse": 12134,                       # Mirror Horizontally: mirrored in time
             "library": 1054225,                     # Asset Browser
             "save": c4d.RESOURCEIMAGE_PLUS}
_bitmaps = {}


def _c4d_icon(ua, cid, cx, cy, size):
    """Draw one of C4D's own icons (cached), alpha-blended, size px."""
    bmp = _bitmaps.get(cid)
    if bmp is None:
        bmp = _bitmaps[cid] = c4d.bitmaps.InitResourceBitmap(cid) or False
    if not bmp:
        return False
    size = int(size)
    ua.DrawBitmap(bmp, int(cx - size / 2.0), int(cy - size / 2.0), size, size, 0, 0, bmp.GetBw(), bmp.GetBh(),
                  c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA)
    return True


def _icon(ua, name, cx, cy, s, col):
    """C4D's icon when there is one, else a tiny flat drawn one."""
    if name in C4D_ICONS and _c4d_icon(ua, C4D_ICONS[name], cx, cy, s + 4):
        return
    ua.DrawSetPen(col)
    if name == "play":
        ua.DrawPolyFill([cx - s * 0.32, cy - s * 0.42, cx + s * 0.42, cy, cx - s * 0.32, cy + s * 0.42], True)
    elif name == "stop":
        ua.DrawRectangle(int(cx - s * 0.32), int(cy - s * 0.32), int(cx + s * 0.32), int(cy + s * 0.32))
    elif name == "reverse":
        for dy, d in ((-0.2, 1), (0.2, -1)):
            y = cy + dy * s
            ua.DrawPolyLine([cx - s * 0.4, y, cx + s * 0.4, y], False, 1.6)
            tip = cx + d * s * 0.45
            ua.DrawPolyFill([tip, y, tip - d * s * 0.22, y - s * 0.14, tip - d * s * 0.22, y + s * 0.14], True)
    elif name == "reset":
        ua.DrawEllipseLine([cx, cy], [s * 0.34, s * 0.34], 1.6)
        ua.DrawSetPen(C_BTN_HOVER if getattr(ua, "hover", False) else C_BTN)
        ua.DrawRectangle(int(cx), int(cy - s * 0.45), int(cx + s * 0.45), int(cy))
        ua.DrawSetPen(col)
        ua.DrawPolyFill([cx - s * 0.02, cy - s * 0.52, cx + s * 0.2, cy - s * 0.34, cx - s * 0.02, cy - s * 0.16], True)
    elif name in ("copy", "paste", "paste_rev"):
        if name == "copy":
            ua.DrawPolyLine([cx - s * 0.36, cy - s * 0.18, cx - s * 0.36, cy + s * 0.42, cx + s * 0.14, cy + s * 0.42,
                             cx + s * 0.14, cy - s * 0.18], True, 1.4)
            ua.DrawPolyLine([cx - s * 0.14, cy - s * 0.18, cx - s * 0.14, cy - s * 0.42, cx + s * 0.36, cy - s * 0.42,
                             cx + s * 0.36, cy + s * 0.2, cx + s * 0.14, cy + s * 0.2], False, 1.4)
        else:
            ua.DrawPolyLine([cx - s * 0.34, cy - s * 0.32, cx - s * 0.34, cy + s * 0.44, cx + s * 0.34, cy + s * 0.44,
                             cx + s * 0.34, cy - s * 0.32], True, 1.4)
            ua.DrawRectangle(int(cx - s * 0.16), int(cy - s * 0.46), int(cx + s * 0.16), int(cy - s * 0.24))
            if name == "paste_rev":
                y = cy + s * 0.1
                ua.DrawPolyLine([cx - s * 0.18, y, cx + s * 0.2, y], False, 1.4)
                ua.DrawPolyFill([cx - s * 0.22, y, cx - s * 0.06, y - s * 0.12, cx - s * 0.06, y + s * 0.12], True)
    elif name == "save":
        ua.DrawRectangle(int(cx - s * 0.36), int(cy - 1), int(cx + s * 0.36), int(cy + 1))
        ua.DrawRectangle(int(cx - 1), int(cy - s * 0.36), int(cx + 1), int(cy + s * 0.36))
    elif name == "more":                             # three dots
        for dx in (-0.32, 0.0, 0.32):
            ua.DrawEllipseFill([cx + dx * s, cy], [1.6, 1.6])
    elif name == "menu":                             # hamburger
        for dy in (-0.3, 0.0, 0.3):
            y = cy + dy * s
            ua.DrawRectangle(int(cx - s * 0.38), int(y - 1), int(cx + s * 0.38), int(y + 1))
    elif name == "speed":                            # a little hill: speed rising and falling
        ua.DrawBezierLine([cx - s * 0.45, cy + s * 0.35], [cx - s * 0.1, cy + s * 0.35, cx - s * 0.2, cy - s * 0.45,
                                                          cx, cy - s * 0.45], False, 1.6)
        ua.DrawBezierLine([cx, cy - s * 0.45], [cx + s * 0.2, cy - s * 0.45, cx + s * 0.1, cy + s * 0.35,
                                                cx + s * 0.45, cy + s * 0.35], False, 1.6)
    elif name == "library":
        q = s * 0.17
        for ox in (-1, 1):
            for oy in (-1, 1):
                x, y = cx + ox * s * 0.2, cy + oy * s * 0.2
                ua.DrawRectangle(int(x - q), int(y - q), int(x + q), int(y + q))


def button_draw(b, x1, y1, x2, y2):
    b.OffScreenOn()
    b.DrawSetPen(c4d.COLOR_BG)
    b.DrawRectangle(x1, y1, x2, y2)
    w, h = b.GetWidth(), b.GetHeight()
    if b.primary:
        base = _accent()
        col = base * (0.85 if b.down else (1.1 if b.hover else 1.0))
    else:
        col = C_BTN_DOWN if b.down else (C_BTN_HOVER if b.hover else C_BTN)
    active = (b.bid == B_LIBTOGGLE and getattr(b.dlg, "show_library", True)) or \
             (b.bid == B_SPEED and getattr(b.dlg, "speed", False))
    b.DrawSetPen(col)
    b.DrawRoundedRectangle(1, 1, w - 2, h - 2, 4, 4)
    if active and not b.primary:                     # on: just an accent bar underneath; off: plain
        b.DrawSetPen(_accent())
        b.DrawRectangle(5, h - 4, w - 6, h - 3)
    icon = b.icon
    if b.bid == B_PLAY:
        icon = "stop" if getattr(b.dlg, "playing", False) else "play"
    b.DrawSetFont(c4d.FONT_BIG_BOLD if b.primary else c4d.FONT_DEFAULT)
    tw = b.DrawGetTextWidth(b.label) if b.label else 0
    s = 14.0
    gap = 6 if (icon and b.label) else 0
    total = (s if icon else 0) + gap + tw
    x = (w - total) / 2.0
    if icon:
        _icon(b, icon, x + s / 2.0, h / 2.0, s, C_ICON)
    if b.label:
        b.DrawSetTextCol(c4d.Vector(1.0) if b.primary else C_ICON, c4d.COLOR_TRANS)
        b.DrawText(b.label, int(x + (s if icon else 0) + gap), int((h - b.DrawGetFontHeight()) / 2))


def button_input(b, msg):
    if msg.GetInt32(c4d.BFM_INPUT_DEVICE) != c4d.BFM_INPUT_MOUSE:
        return False
    if msg.GetInt32(c4d.BFM_INPUT_CHANNEL) == c4d.BFM_INPUT_MOUSERIGHT and b.bid == B_APPLY:
        bc = c4d.BaseContainer()
        bc.InsData(M_APPLY, "Apply")
        bc.InsData(M_APPLY_REV, "Apply Reversed")
        res = gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        if res in (M_APPLY, M_APPLY_REV):
            _apply_current(b.dlg, reverse_it=res == M_APPLY_REV)
        return True
    if msg.GetInt32(c4d.BFM_INPUT_CHANNEL) != c4d.BFM_INPUT_MOUSELEFT:
        return False
    b.down = True
    b.Redraw()
    mx, my = float(msg.GetInt32(c4d.BFM_INPUT_X)), float(msg.GetInt32(c4d.BFM_INPUT_Y))
    b.MouseDragStart(c4d.KEY_MLEFT, mx, my, c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE | c4d.MOUSEDRAGFLAGS_NOMOVE)
    while True:
        res, dx, dy, ch = b.MouseDrag()
        if res != c4d.MOUSEDRAGRESULT_CONTINUE:
            break
    b.MouseDragEnd()
    b.down = False
    b.Redraw()
    panel_command(b.dlg, b.bid, None)             # fires on release, like a button
    try:
        b.dlg.Activate(UA_EDITOR)                 # don't keep focus: a focused area gets C4D's focus tint
    except Exception:
        pass
    return True


def _update_timer(dlg):
    """16 ms while something animates, 60 ms while only a hover is being watched."""
    anim = getattr(dlg, "playing", False) or getattr(dlg, "lib_hover", None) is not None
    rate = 16 if anim else (60 if getattr(dlg, "hovered", None) is not None else 0)
    if rate != getattr(dlg, "timer_on", 0):
        if anim and not getattr(dlg, "timer_on", 0) == 16:
            dlg.play_t0 = time.time()
        dlg.timer_on = rate
        dlg.SetTimer(rate)


def _cursor_over(ua):
    """Is the mouse over this user area right now? (screen cursor vs its box)"""
    try:
        import ctypes

        class _P(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = _P()
        ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
        off = ua.Screen2Local()
        x, y = pt.x + off["x"], pt.y + off["y"]
        return 0 <= x < ua.GetWidth() and 0 <= y < ua.GetHeight()
    except Exception:
        return True                                # can't tell: leave it to C4D's own 'left' message


def _watch_hover(dlg):
    """C4D doesn't always say when the mouse leaves a user area - check."""
    b = getattr(dlg, "hovered", None)
    if b is not None and not _cursor_over(b):
        b.hover = False
        b.Redraw()
        _hint(dlg, None)
        dlg.hovered = None
        _update_timer(dlg)
    lib = getattr(dlg, "library", None)
    if getattr(dlg, "lib_hover", None) is not None and lib is not None and not _cursor_over(lib):
        library_hover(lib, None)


# ---- library hover: an animated dot on the thumbnail + a ghost in the editor

def library_hover(ua, msg):
    dlg = ua.dlg
    item = None
    if msg is not None:
        sx, sy = msg.GetInt32(c4d.BFM_DRAG_SCREENX), msg.GetInt32(c4d.BFM_DRAG_SCREENY)
        off = ua.Screen2Local()
        item = _lib_item_at(ua, sx + off["x"], sy + off["y"])
    if item != getattr(dlg, "lib_hover", None):
        dlg.lib_hover = item
        _update_timer(dlg)
        ua.Redraw()


def _ui_path():
    return os.path.join(prefs_dir(), "ui.json")


_ui_cache = {"mtime": None, "data": {}}


def ui_prefs():
    try:
        mtime = os.path.getmtime(_ui_path())
    except OSError:
        return {}
    if mtime != _ui_cache["mtime"]:
        try:
            with open(_ui_path(), encoding="utf-8") as fh:
                _ui_cache["data"] = json.load(fh)
        except Exception:
            _ui_cache["data"] = {}
        _ui_cache["mtime"] = mtime
    return dict(_ui_cache["data"])


def save_ui_prefs(**kw):
    data = ui_prefs()
    data.update(kw)
    try:
        with open(_ui_path(), "w", encoding="utf-8") as fh:
            json.dump(data, fh)
    except Exception:
        log_error("save ui prefs")


def show_library(dlg, on):
    dlg.show_library = on
    dlg.HideElement(G_LIBCOL, not on)
    dlg.LayoutChanged(G_MAIN)
    save_ui_prefs(library=on)
    b = dlg.buttons.get(B_LIBTOGGLE) if hasattr(dlg, "buttons") else None
    if b:
        b.hover = False                            # it just moved: no 'mouse left' message comes
        b.Redraw()


# ═════════════════════════════════════════════════════════════ speed graph

def _speed_value_at(curve, p):
    """Speed at time p (bisection on the bezier parameter)."""
    lo, hi = 0.0, 1.0
    for _ in range(30):
        m = 0.5 * (lo + hi)
        lo, hi = (m, hi) if _speed_at_s(curve, m)[0] < p else (lo, m)
    return _speed_at_s(curve, 0.5 * (lo + hi))[1]


def _draw_speed(ua, dlg, fr, p):
    """Speed over time, like AE's speed graph. 1 = linear's (average) speed.
    Handles: horizontal influence lines at the start and the end - drag
    sideways for influence, up and down for speed."""
    l, top, r, b, vmin, vmax = fr
    curve = dlg.saved["Bezier"]
    y1 = _to_px(fr, 0, 1.0)[1]                       # linear's speed: faint, labelled at the edge
    ua.DrawSetPen(c4d.Vector(0.24))
    for x in range(int(l), int(r), 8):
        ua.DrawLine(x, int(y1), min(int(r), x + 4), int(y1))
    ua.DrawSetTextCol(C_LABEL, c4d.COLOR_TRANS)
    ua.DrawText("avg", int(r) - 26, int(y1) - 15)
    pts = []
    for x, v in _speed_samples(curve):
        pts += list(_to_px(fr, x, v))
    x0, yb = _to_px(fr, 0.0, 0.0)
    ua.DrawSetPen(_curve_col() * 0.28)          # the area under it
    ua.DrawPolyFill(pts + [_to_px(fr, 1.0, 0.0)[0], yb, x0, yb], True)
    ua.DrawSetPen(C_HANDLE)
    ua.DrawLine(int(l), int(yb), int(r), int(yb))       # the baseline: speed 0
    ua.DrawSetPen(_curve_col())
    ua.DrawPolyLine(pts, False, 2.5)
    hot = getattr(ua, "hot", None)
    (x1, s0), (x2, s1) = _speed_handles(curve)
    for kind, (hx, hv), ax in (("sp_in", (x1, s0), 0.0), ("sp_out", (x2, s1), 1.0)):
        X, Y = _to_px(fr, hx, hv)
        AX, AY = _to_px(fr, ax, hv)
        ua.DrawSetPen(C_HANDLE)
        ua.DrawPolyLine([AX, AY, X, Y], False, 1.2)
        ua.DrawEllipseFill([AX, AY], [5.0, 5.0])     # the key
        ua.DrawSetPen(C_HANDLE_HOT if hot == (kind, 0 if kind == "sp_in" else 1) else C_HANDLE)
        ua.DrawEllipseFill([X, Y], [4.0, 4.0])      # its influence handle, smaller
    if p is not None:
        X, Y = _to_px(fr, p, _speed_value_at(curve, p))
        ua.DrawSetPen(_curve_col())
        ua.DrawEllipseFill([X, Y], [5.0, 5.0])


# ═════════════════════════════════════════════════════════════ editor menu

def editor_menu(ua, x, y):
    """Right-click in the graph (not on a point)."""
    dlg = ua.dlg
    bc = c4d.BaseContainer()
    bc.InsData(M_APPLY, "Apply to Selected Keys")
    bc.InsData(M_APPLY_REV, "Apply Reversed")
    bc.InsData(0, "")
    bc.InsData(M_REVERSE, "Reverse Curve")
    bc.InsData(M_RESET, "Reset %s" % dlg.mode)
    if dlg.mode == "Custom":
        bc.InsData(M_ADD_POINT, "Add Point Here")
    bc.InsData(0, "")
    bc.InsData(M_COPY_TEXT, "Copy as Text")
    bc.InsData(M_PASTE_TEXT, "Paste Text")
    bc.InsData(M_SAVE, "Save as Preset...")
    if dlg.mode == "Bezier":
        bc.InsData(0, "")
        bc.InsData(M_SPEED, "Speed Graph" + ("&c&" if getattr(dlg, "speed", False) else ""))
    elif dlg.mode in GENERATED and dlg.mode != "Steps":
        bc.InsData(0, "")
        bc.InsData(M_TO_CUSTOM, "Edit as Custom")
    res = gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
    if res in (M_APPLY, M_APPLY_REV):
        _apply_current(dlg, reverse_it=res == M_APPLY_REV)
    elif res == M_REVERSE:
        panel_command(dlg, B_REVERSE, None)
    elif res == M_RESET:
        panel_command(dlg, B_RESET, None)
    elif res == M_ADD_POINT:
        before = _state(dlg)
        t, v = _to_curve(_frame(ua), x, y)
        if _add_node(dlg.saved["Custom"], t, v) is not None:
            checkpoint(dlg, snapshot=before)
            refresh(dlg)
    elif res == M_COPY_TEXT:
        c4d.CopyStringToClipboard(to_text(dlg.curve))
        _status(dlg, "copied the curve as text - paste it into After Effects, or back here")
    elif res == M_PASTE_TEXT:
        try:
            curve = parse(c4d.GetStringFromClipboard())
        except Exception:
            _status(dlg, "the clipboard doesn't hold a curve (x1, y1, x2, y2 or node JSON)")
            return
        checkpoint(dlg)
        set_curve(dlg, curve)
        _status(dlg, "pasted a curve from the clipboard")
    elif res == M_SAVE:
        save_preset(dlg)
    elif res == M_SPEED:
        panel_command(dlg, B_SPEED, None)
    elif res == M_TO_CUSTOM:
        panel_command(dlg, B_TO_CUSTOM, None)


# ═════════════════════════════════════════════════════════════ profiles

M_NEW_PROF, M_RENAME_PROF, M_DELETE_PROF, M_IMPORT, M_EXPORT, M_BUILTINS = (c4d.FIRST_POPUP_ID + k for k in range(20, 26))


def fill_profiles(dlg):
    """The dropdown: every profile, the active one picked."""
    names = profile_names()
    dlg.FreeChildren(C_PROFILE)
    for k, n in enumerate(names):
        dlg.AddChild(C_PROFILE, k, n)
    dlg.SetInt32(C_PROFILE, names.index(active_profile()))


def switch_profile(dlg, name):
    save_ui_prefs(profile=name)
    dlg.picked = None
    fill_profiles(dlg)
    dlg.library.LayoutChanged()
    dlg.library.Redraw()
    _status(dlg, "profile: %s (%d presets)" % (name, len(user_presets())))


def _unique(name):
    names = set(profile_names())
    base, k = name, 2
    while name in names:
        name = "%s %d" % (base, k)
        k += 1
    return name


def _read_presets(path):
    """[{"name", "curve", "meta"}] from a file, checked."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    out = []
    for p in data if isinstance(data, list) else []:
        if not isinstance(p, dict):
            continue                                 # junk entries are skipped, not fatal
        c = p.get("curve")
        if p.get("name") and isinstance(c, list) and len(c) >= 2 and                 all(isinstance(n, list) and len(n) in (6, 7) for n in c):
            out.append({"name": str(p["name"]), "curve": c, "meta": p.get("meta")})
    return out


def profile_menu(dlg):
    current = active_profile()
    builtins = ui_prefs().get("builtins", True)
    bc = c4d.BaseContainer()
    bc.InsData(M_NEW_PROF, "New Profile...")
    bc.InsData(M_RENAME_PROF, "Rename \"%s\"..." % current)
    bc.InsData(M_DELETE_PROF, "Delete \"%s\"..." % current)
    bc.InsData(0, "")
    bc.InsData(M_IMPORT, "Import Profile...")
    bc.InsData(M_EXPORT, "Export \"%s\"..." % current)
    bc.InsData(0, "")
    bc.InsData(M_BUILTINS, "Show Built-in Presets" + ("&c&" if builtins else ""))
    res = gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
    if res == M_NEW_PROF:
        name = gui.RenameDialog("New Profile")
        if name and name.strip():
            name = _unique(_safe(name.strip()))
            with open(_profile_path(name), "w", encoding="utf-8") as fh:
                fh.write("[]")
            switch_profile(dlg, name)
    elif res == M_RENAME_PROF:
        name = gui.RenameDialog(current)
        if name and name.strip() and _safe(name.strip()) != current:
            name = _unique(_safe(name.strip()))
            os.rename(_profile_path(current), _profile_path(name))
            switch_profile(dlg, name)
    elif res == M_DELETE_PROF:
        n = len(user_presets())
        if gui.QuestionDialog("Delete the profile \"%s\" and its %d preset%s?\n(Export it first to keep a copy.)"
                              % (current, n, "" if n == 1 else "s")):
            os.remove(_profile_path(current))
            switch_profile(dlg, profile_names()[0])        # an empty "My Presets" comes back if it was the last
    elif res == M_IMPORT:
        path = c4d.storage.LoadDialog(c4d.FILESELECTTYPE_ANYTHING, "Import an Ease profile (.json)",
                                      c4d.FILESELECT_LOAD, "json")
        if not path:
            return
        try:
            items = _read_presets(path)
        except Exception:
            _status(dlg, "that file isn't an Ease profile (JSON list of presets)")
            return
        if not items:
            _status(dlg, "no presets found in %s" % os.path.basename(path))
            return
        name = _unique(_safe(os.path.splitext(os.path.basename(path))[0]))
        with open(_profile_path(name), "w", encoding="utf-8") as fh:
            json.dump(items, fh, indent=1)
        switch_profile(dlg, name)
        _status(dlg, "imported %d presets as the profile %s" % (len(items), name))
    elif res == M_EXPORT:
        path = c4d.storage.SaveDialog(c4d.FILESELECTTYPE_ANYTHING, "Export \"%s\"" % current, "json",
                                      def_file=current + ".json")
        if not path:
            return
        if not path.lower().endswith(".json"):
            path += ".json"
        shutil.copyfile(_profile_path(current), path)
        _status(dlg, "exported %s (%d presets) to %s" % (current, len(user_presets()), path))
    elif res == M_BUILTINS:
        save_ui_prefs(builtins=not builtins)
        dlg.library.LayoutChanged()
        dlg.library.Redraw()
