"""Eyeballer service - the headless engine behind the C4D "Eyeballer" panel.

Started and pinged by the C4D plugin; exits 10 s after the last ping. Captures the C4D viewport
(MAXON-DRAWPORT window) or the Redshift RenderView (redshift Qt window) with PrintWindow, so covered
windows still work, or a fixed screen area. Runs the view modes, DaVinci-style scopes (scopes.py),
reference compare, hover / pinned probes and the match scope, and publishes one image - the view on top,
the scope strip below - plus a header to shared memory.

UDP commands on 127.0.0.1:47819:
  ping | size W VIEW_H SCOPE_H | mode N | ref toggle|paste|clear | ref load <path>
  source auto|viewport|renderview|area | pick | trim reset
  hover X Y | hover off | pin X Y | unpin X Y | pins clear
  scopes N (0 = off, 1-4 slots) | slot I NAME | param <name> <value> | quit
Test by hand:  python eyeballer_service.pyw --selftest out.png [--source=viewport] [--mode=N] [--ref=img] [--scopes=3]
"""
import ctypes, importlib.util, json, math, mmap, os, socket, struct, sys, time
import tkinter as tk
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageGrab
import win32gui, win32process, win32ui

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import scopes
_spec = importlib.util.spec_from_file_location("eyeballer_window", os.path.join(HERE, "eyeballer_window.pyw"))
bm = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(bm)      # LUT loading + lookup tables

PORT = 47819
SHM_NAME, SHM_SIZE, HDR = "EyeballerFrame", 40 * 1024 * 1024, 4096
MAGIC = b"BDM3"
CFG = os.path.join(os.environ["APPDATA"], "Eyeballer", "panel.json")
KEEPALIVE_S = 10
SOURCES = ["auto", "viewport", "renderview", "octane", "area"]
RESOLVED = ["viewport", "renderview", "octane", "area"]
STATE_OK, STATE_NO_VIEWPORT, STATE_NO_RV, STATE_NO_AREA, STATE_PAUSED, STATE_NO_OCTANE = 0, 1, 2, 3, 4, 5
DEFAULTS = {"mode": 0, "source": "auto", "area": None, "trim": {}, "dock": {}, "ref": None, "pins": [],
            "scope_count": 2, "slots": ["Waveform", "Vectorscope", "RGB Parade", "Histogram"],
            "notan_lo": 0.18, "notan_hi": 0.55, "accent": 0.35, "exposure": 0.0, "max_fps": 30,
            "scope_gain": 1.0, "vec_zoom": 1.0}
ZONES = [(0.02, "crushed", (115, 0, 153)), (0.10, "deep shadow", (0, 25, 153)), (0.20, "shadow", (0, 115, 140)),
         (0.40, "low mid", (77, 89, 77)), (0.50, "mid grey", (25, 179, 25)), (0.62, "high mid", (158, 158, 158)),
         (0.75, "bright", (242, 230, 0)), (0.90, "hot", (255, 128, 0)), (0.98, "near clip", (242, 13, 13)),
         (9.99, "clipped", (255, 153, 217))]
# plain-language names for the Breakdown LUTs, matched on words in the .cube file names
FRIENDLY = [("notan", "Light & Shadow", "Three tones - shadow, mid, light - to judge the big shapes of the image"),
            ("false", "Exposure Zones", "Brightness as colour bands - purple crushed, green mid-grey, red clipping"),
            ("saturation", "Saturation", "How colourful each area is - dark blue neutral, red fully saturated"),
            ("hue only", "Colour Only", "Colour with the lighting removed - grey means neutral"),
            ("hue band", "Colour Groups", "Colours snapped to 12 groups - count how many colour families you use"),
            ("hue famil", "Colour Groups", "Colours snapped to 12 groups - count how many colour families you use"),
            ("accent", "Accent Colours", "Only strong colour stays - how little accent does the image need?"),
            ("warm", "Warm / Cool", "Orange = warm, blue = cool - temperature contrast"),
            ("value", "Brightness", "Greyscale - does the lighting still read without colour?")]
ORIGINAL = ("Original", "The image as it is")

try: FONT = ImageFont.truetype("segoeui.ttf", 12); FONT_S = ImageFont.truetype("segoeui.ttf", 11)
except OSError: FONT = FONT_S = ImageFont.load_default()
REF_RGB = (255, 150, 40)

# ---------------------------------------------------------------- window tracking + capture
# (class, title) -> is this the window? Octane's Live Viewer is a C4D dialog: when floating it is its own
# top-level window titled "...Live Viewer..."; when docked it has no window and is read from the main window.
TARGETS = {"viewport": lambda cls, title: cls.startswith("MAXON-DRAWPORT"),
           "renderview": lambda cls, title: "redshift" in cls.lower() and "qwindow" in cls.lower(),
           "octane": lambda cls, title: "live viewer" in title.lower() and not cls.startswith("C4DR_WINA")}

def c4d_main():
    found = []
    win32gui.EnumWindows(lambda h, _: found.append(h) if win32gui.GetClassName(h).startswith("C4DR_WINA") else None, None)
    return found

def c4d_is_foreground():
    fg = win32gui.GetForegroundWindow()
    if not fg: return False
    pid = win32process.GetWindowThreadProcessId(fg)[1]
    return any(win32process.GetWindowThreadProcessId(h)[1] == pid for h in c4d_main())

def find_window(kind):
    match, found = TARGETS[kind], []
    pids = {win32process.GetWindowThreadProcessId(h)[1] for h in c4d_main()}
    def consider(h):
        if win32gui.IsWindowVisible(h) and match(win32gui.GetClassName(h), win32gui.GetWindowText(h)):
            r = win32gui.GetClientRect(h)
            if r[2] * r[3] > 0: found.append((r[2] * r[3], h))
    def top(h, _):
        if win32process.GetWindowThreadProcessId(h)[1] in pids:
            consider(h)
            try: win32gui.EnumChildWindows(h, lambda c, __: consider(c), None)
            except Exception: pass
    win32gui.EnumWindows(top, None)
    return max(found)[1] if found else None

class WindowGrabber:
    """PrintWindow(PW_CLIENTONLY | PW_RENDERFULLCONTENT): reads the window's own DirectX/Qt content,
    so it works while other windows cover it (~25 ms for 1000x1200)."""
    def __init__(self): self.key = None
    def grab(self, hwnd):
        _, _, w, h = win32gui.GetClientRect(hwnd)
        if self.key != (hwnd, w, h):
            self.release()
            self.hdc = win32gui.GetWindowDC(hwnd); self.src = win32ui.CreateDCFromHandle(self.hdc)
            self.mem = self.src.CreateCompatibleDC()
            self.bmp = win32ui.CreateBitmap(); self.bmp.CreateCompatibleBitmap(self.src, w, h)
            self.mem.SelectObject(self.bmp); self.key = (hwnd, w, h)
        ctypes.windll.user32.PrintWindow(hwnd, self.mem.GetSafeHdc(), 3)
        return np.frombuffer(self.bmp.GetBitmapBits(True), np.uint8).reshape(h, w, 4)[..., 2::-1]
    def release(self):
        if self.key:
            try:
                win32gui.DeleteObject(self.bmp.GetHandle()); self.mem.DeleteDC(); self.src.DeleteDC()
                win32gui.ReleaseDC(self.key[0], self.hdc)
            except Exception: pass
            self.key = None

class Capturer:
    """Grabs a window's pixels one frame ahead on a worker thread, so capture overlaps processing.

    While Cinema 4D is the foreground app the window is read straight off the screen (BitBlt, ~14 ms, costs C4D
    nothing); when C4D is covered it falls back to PrintWindow, which still sees the window's own content."""
    def __init__(self):
        from concurrent.futures import ThreadPoolExecutor
        self.ex = ThreadPoolExecutor(max_workers=2)
        self.grabbers, self.pending = {}, {}

    def _grab(self, hwnd, screen_ok):
        g = self.grabbers.setdefault(hwnd, [WindowGrabber(), bm.Grabber()])
        if screen_ok:
            _, _, w, h = win32gui.GetClientRect(hwnd); x, y = win32gui.ClientToScreen(hwnd, (0, 0))
            if w > 0 and h > 0: return np.ascontiguousarray(g[1].grab(x, y, w, h))
        return np.ascontiguousarray(g[0].grab(hwnd))

    def get(self, hwnd, screen_ok=True):
        fut = self.pending.pop(hwnd, None) or self.ex.submit(self._grab, hwnd, screen_ok)
        img = fut.result()
        self.pending[hwnd] = self.ex.submit(self._grab, hwnd, screen_ok)      # next frame, while this one is processed
        if len(self.grabbers) > 6:                                              # windows come and go with layouts
            for h in list(self.grabbers)[:-4]:
                if h not in self.pending: self.grabbers.pop(h, None)
        return img

TOOLBAR_H = 60
def auto_trim_rv(full):
    """The render sits inside the RenderView, letterboxed on UI grey, with a toolbar on top and a status line below.
    UI grey = the most common colour of the toolbar strip (a render with a black background fools edge sampling).
    -> (x0, y0, x1, y1) of the render, or None."""
    h, w = full.shape[:2]
    if h < 120 or w < 80: return None
    # The RenderView uses more than one UI grey (toolbar/status 43, viewport surround 51 here): collect every
    # common neutral grey from the toolbar strip and the outer edges, and treat pixels matching any of them as UI.
    samples = np.concatenate([full[4:40:2, ::3].reshape(-1, 3), full[:, 1:4].reshape(-1, 3),
                              full[:, w - 4:w - 1].reshape(-1, 3), full[h - 6:h - 1, ::3].reshape(-1, 3)])
    vals, counts = np.unique(samples, axis=0, return_counts=True)
    greys = [v for v, n in zip(vals.astype(np.int16), counts)
             if n > len(samples) * 0.04 and v.max() - v.min() <= 2 and 25 <= v.max() <= 90]
    if not greys: return None
    small = full[::2, ::2].astype(np.int16)
    diff = np.ones(small.shape[:2], bool)
    for g in greys: diff &= np.abs(small - g).max(axis=2) > 3
    def longest_run(mask):
        best, start = (0, 0), None
        for i, v in enumerate(np.append(mask, False)):
            if v and start is None: start = i
            elif not v and start is not None:
                if i - start > best[1] - best[0]: best = (start, i)
                start = None
        return best
    r0, r1 = longest_run(diff.mean(axis=1) > 0.5)             # status text / progress bar rows stay below 50 %
    if r1 - r0 < 10: return None
    c0, c1 = longest_run(diff[r0:r1].mean(axis=0) > 0.5)
    if c1 - c0 < 10: return None
    return c0 * 2, r0 * 2, c1 * 2, r1 * 2

# ---------------------------------------------------------------- probe readouts
def luma(a): return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722
def chroma(a): return a.max(axis=-1) - a.min(axis=-1)
def zone(y):
    for t, name, col in ZONES:
        if y < t: return name, col
    return ZONES[-1][1], ZONES[-1][2]
HUE_NAMES = [(15, "red"), (45, "orange"), (70, "yellow"), (160, "green"), (200, "cyan"), (255, "blue"), (290, "purple"), (335, "magenta"), (361, "red")]
def readout(rgb8):
    a = np.asarray(rgb8, np.float32) / 255; y = float(luma(a)); zn, zc = zone(y)
    mx, mn = float(a.max()), float(a.min()); c = mx - mn
    text = "%d%%  %s" % (round(y * 100), zn)
    if c > 0.08:
        r, g, b = a
        h = (((g - b) / c) % 6 if mx == r else ((b - r) / c + 2 if mx == g else (r - g) / c + 4)) * 60
        text += "   %s  sat %d%%" % (next(n for lim, n in HUE_NAMES if h < lim), round(c * 100))
    else: text += "   neutral"
    return text, zc

# ---------------------------------------------------------------- service
class Service:
    def __init__(self, selftest=None):
        self.cfg = json.loads(json.dumps(DEFAULTS))
        try: self.cfg.update(json.load(open(CFG)))
        except Exception: pass
        if "target" in self.cfg:                                                  # v1 settings
            self.cfg.setdefault("source", self.cfg.pop("target"))
        if self.cfg["source"] not in SOURCES: self.cfg["source"] = "auto"
        if isinstance(self.cfg.get("scopes"), bool): self.cfg["scope_count"] = 2 if self.cfg.pop("scopes") else 0   # v2
        self.selftest = selftest
        self.modes = bm.find_modes(); self.tables = {}
        self.kind, self.names = [], [ORIGINAL]
        for n, _, _ in self.modes:
            low = n.lower()
            self.kind.append("notan" if "notan" in low else "false" if "false" in low else "accent" if "accent" in low else "lut")
            self.names.append(next(((nm, hint) for key, nm, hint in FRIENDLY if key in low), (n, "")))
        self.grab, self.cap = bm.Grabber(), Capturer()
        self.crops, self.live, self.auto_choice, self.auto_t = {}, {}, "viewport", 0.0
        self.ref = None; self.ref_cache = {}; self.show_ref = True
        if self.cfg.get("ref") and os.path.exists(self.cfg["ref"]): self.set_ref(Image.open(self.cfg["ref"]), self.cfg["ref"])
        self.size = (960, 540, 220); self.last_ping = time.time(); self.bg = (30, 30, 33)
        self.hwnd, self.hwnd_t = {}, {}
        self.msg = ""; self.fps = 0.0; self.resolved = "viewport"; self.state = STATE_OK
        self.hover = None; self.view_rect = (0, 0, 0, 0); self.src_small = None
        self.last_sig = None; self.dirty = True; self.idle = False; self.match_cache = (None, 0.0)
        self.shm = mmap.mmap(-1, SHM_SIZE, tagname=SHM_NAME + ("_test" if selftest else ""))
        self.seq = struct.unpack_from("<I", self.shm, 4)[0] & ~1 if self.shm[:4] == MAGIC else 0
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0 if selftest else PORT)); self.sock.setblocking(False)   # fails if another service runs
        self.root = tk.Tk(); self.root.withdraw()                                # only for the pick overlay

    def save(self):
        os.makedirs(os.path.dirname(CFG), exist_ok=True); json.dump(self.cfg, open(CFG, "w"))

    def set_ref(self, im, path=None):
        self.ref = np.asarray(im.convert("RGB"), np.uint8); self.ref_cache = {}; self.show_ref = True; self.cfg["ref"] = path

    # ---- modes: Light & Shadow / Exposure Zones / Accent Colours are live-adjustable, the rest are LUT tables
    def process(self, a8):
        m = self.cfg["mode"]
        if m == 0 or m > len(self.modes): return a8
        kind = self.kind[m - 1]
        if kind != "lut":
            # integer maths + 256-entry lookups: ~4x faster than float, and the sliders stay live
            y8 = ((a8[..., 0].astype(np.uint16) * 54 + a8[..., 1].astype(np.uint16) * 183
                   + a8[..., 2].astype(np.uint16) * 19) >> 8).astype(np.uint8)
            if kind == "accent":
                c8 = a8.max(axis=2) - a8.min(axis=2)
                grey = ((y8.astype(np.uint16) * 140) >> 8).astype(np.uint8)
                return np.where((c8 >= int(self.cfg["accent"] * 255))[..., None], a8, grey[..., None].repeat(3, axis=2))
            key = (kind, self.cfg["notan_lo"], self.cfg["notan_hi"], self.cfg["exposure"])
            if getattr(self, "_lut_key", None) != key:
                v = np.arange(256) / 255.0
                if kind == "notan":
                    t = np.where(v < self.cfg["notan_lo"], 10, np.where(v < self.cfg["notan_hi"], 115, 242))
                    lut = np.stack([t, t, t], -1)
                else:
                    y = np.clip(v * 2 ** self.cfg["exposure"], 0, 1)
                    idx = np.minimum(np.searchsorted(np.array([z[0] for z in ZONES]), y, side="right"), len(ZONES) - 1)
                    lut = np.array([z[2] for z in ZONES])[idx]
                self._lut, self._lut_key = lut.astype(np.uint8), key
            return self._lut[y8]
        if m not in self.tables:
            name, lut, path = self.modes[m - 1]
            cache = os.path.join(os.path.dirname(bm.CFG), "tables", f"{name[:40]}.{int(os.path.getmtime(path))}.b{bm.BITS}.npy")
            if os.path.exists(cache): self.tables[m] = np.load(cache)
            else:
                self.tables[m] = bm.build_table(lut)
                os.makedirs(os.path.dirname(cache), exist_ok=True); np.save(cache, self.tables[m])
        return bm.apply_table(a8, self.tables[m])

    # ---- commands
    def command(self, text):
        p = text.strip().split(" ", 2); c = p[0]
        quiet = c in ("ping", "hover", "size", "bg")
        if not quiet: self.dirty = True
        if c == "ping": pass
        elif c == "size":
            v = text.split()[1:]
            s = (max(64, int(v[0])), max(32, int(v[1])), max(0, int(v[2])) if len(v) > 2 else 0)
            if s != self.size: self.size = s; self.dirty = True
        elif c == "mode": self.cfg["mode"] = max(0, min(int(p[1]), len(self.modes))); self.ref_cache = {}
        elif c == "bg":                                        # the panel's theme colour, so the image sits flush
            v = tuple(int(x) for x in text.split()[1:4])
            if v != self.bg: self.bg = v; self.dirty = True
        elif c == "ref":
            sub = p[1] if len(p) > 1 else "toggle"
            if sub == "toggle": self.show_ref = not self.show_ref
            elif sub == "show": self.show_ref = True
            elif sub == "hide": self.show_ref = False
            elif sub == "clear": self.ref = None; self.cfg["ref"] = None
            elif sub == "load" and len(p) == 3 and os.path.exists(p[2]): self.set_ref(Image.open(p[2]), p[2])
            elif sub == "paste":
                clip = ImageGrab.grabclipboard()
                if isinstance(clip, list) and clip: clip = Image.open(clip[0])
                if isinstance(clip, Image.Image): self.set_ref(clip); self.msg = ""
                else: self.msg = "Clipboard has no image"
        elif c == "source" and len(p) > 1 and p[1] in SOURCES: self.cfg["source"] = p[1]; self.cfg["pins"] = []
        elif c == "trim":
            self.cfg["trim"].pop(self.resolved, None); self.cfg["dock"].pop(self.resolved, None); self.crops.pop(self.resolved, None)
        elif c == "pick": self.pick()
        elif c == "hover":
            new = None if p[1] == "off" else (int(p[1]), int(p[2]))
            if new != self.hover: self.hover = new; self.dirty = True
        elif c in ("pin", "unpin") and len(p) == 3:
            uv = self.panel_to_uv(int(p[1]), int(p[2]))
            if uv and c == "pin" and len(self.cfg["pins"]) < 4: self.cfg["pins"].append(list(uv))
            elif uv and c == "unpin" and self.cfg["pins"]:
                vw, vh = self.view_rect[2], self.view_rect[3]
                d = [math.hypot((u - uv[0]) * vw, (v - uv[1]) * vh) for u, v in self.cfg["pins"]]
                i = int(np.argmin(d))
                if d[i] < 16: self.cfg["pins"].pop(i)
        elif c == "pins": self.cfg["pins"] = []
        elif c == "scopes": self.cfg["scope_count"] = max(0, min(4, int(p[1])))
        elif c == "slot" and len(p) == 3 and p[2] in scopes.SCOPES: self.cfg["slots"][max(0, min(3, int(p[1])))] = p[2]
        elif c == "param" and len(p) == 3 and p[1] in DEFAULTS:
            self.cfg[p[1]] = type(DEFAULTS[p[1]])(float(p[2])); self.ref_cache = {}
        elif c == "quit": self.save(); raise SystemExit
        self.last_ping = time.time()
        if not quiet: self.save()

    def panel_to_uv(self, x, y):
        vx, vy, vw, vh = self.view_rect
        if vw <= 0 or not (vx <= x < vx + vw and vy <= y < vy + vh): return None
        return (x - vx) / vw, (y - vy) / vh

    def pick(self):
        user32 = ctypes.windll.user32
        vx, vy, vw, vh = (user32.GetSystemMetrics(i) for i in (76, 77, 78, 79))
        t = tk.Toplevel(self.root); t.overrideredirect(True); t.geometry(f"{vw}x{vh}+{vx}+{vy}")
        t.attributes("-alpha", 0.25); t.attributes("-topmost", True)
        c = tk.Canvas(t, bg="black", highlightthickness=0, cursor="crosshair"); c.pack(fill="both", expand=True)
        tgt = self.resolved
        docked = tgt == "octane" and not self.get_hwnd("octane")
        tip = ("Drag over the docked Octane Live Viewer (its whole panel is fine).  Esc = cancel" if docked else
               "Drag the part of the %s to analyse.  Esc = cancel" % tgt if self.cfg["source"] != "area" else
               "Drag over the area to analyse.  Esc = cancel")
        c.create_text(vw // 2, 40, text=tip, fill="white", font=("Segoe UI", 16))
        s = {}
        def down(e): s["p"] = (e.x_root, e.y_root); s["r"] = c.create_rectangle(e.x, e.y, e.x, e.y, outline="#ffd23c", width=3)
        def move(e):
            if "r" in s: c.coords(s["r"], s["p"][0] - vx, s["p"][1] - vy, e.x, e.y)
        def up(e):
            x0, y0 = s["p"]; x, y = min(x0, e.x_root), min(y0, e.y_root); w, h = abs(e.x_root - x0), abs(e.y_root - y0)
            if w > 20 and h > 20:
                win = self.window_rect(tgt) if self.cfg["source"] != "area" else None
                mains = c4d_main()
                if docked and mains:                     # remember it relative to C4D's main window
                    l, t_, r, b = win32gui.GetClientRect(mains[0]); mx, my = win32gui.ClientToScreen(mains[0], (0, 0))
                    self.cfg["dock"]["octane"] = [(x - mx) / r, (y - my) / b, w / r, h / b]; self.crops.pop("octane", None)
                elif win:
                    wx, wy, ww, wh = win
                    self.cfg["trim"][tgt] = [(x - wx) / ww, (y - wy) / wh, w / ww, h / wh]
                else: self.cfg["source"] = "area"; self.cfg["area"] = [x, y, w, h]
                self.cfg["pins"] = []; self.save(); self.dirty = True
            t.destroy()
        c.bind("<ButtonPress-1>", down); c.bind("<B1-Motion>", move); c.bind("<ButtonRelease-1>", up)
        t.bind("<Escape>", lambda e: t.destroy()); t.focus_force()

    # ---- sources
    def get_hwnd(self, kind):
        now = time.time(); h = self.hwnd.get(kind)
        if not h or not win32gui.IsWindow(h) or now - self.hwnd_t.get(kind, 0) > 1.0:
            h = find_window(kind); self.hwnd[kind] = h; self.hwnd_t[kind] = now
        return h

    def window_rect(self, kind):
        h = self.get_hwnd(kind)
        if not h: return None
        _, _, w, hh = win32gui.GetClientRect(h); x, y = win32gui.ClientToScreen(h, (0, 0))
        return x, y, w, hh

    def crop_box(self, kind, full):
        """Render windows (RenderView / Octane LV): where the render sits inside the UI. Re-detected twice a second."""
        c = self.crops.setdefault(kind, [None, 0.0])
        if time.time() - c[1] > 0.5: c[0], c[1] = auto_trim_rv(full), time.time()
        return c[0]

    def grab_kind(self, kind):
        """Full pixels of a source window, or of the region picked inside C4D's main window for a docked one."""
        fg = c4d_is_foreground()
        h = self.get_hwnd(kind)
        if h: return self.cap.get(h, fg)
        dock = self.cfg["dock"].get(kind)
        mains = c4d_main()
        if dock and mains:
            full = self.cap.get(mains[0], fg); H, W = full.shape[:2]
            fx, fy, fw, fh = dock
            return full[int(fy * H):int((fy + fh) * H), int(fx * W):int((fx + fw) * W)]
        return None

    def has_image(self, kind):
        """Auto source: a render window counts as live if it shows a non-empty render or changed in the last 3 s."""
        full = self.grab_kind(kind)
        if full is None: return False
        box = self.crop_box(kind, full)
        if not box: return False
        x0, y0, x1, y1 = box; img = full[y0:y1:8, x0:x1:8]
        sig = int(img.astype(np.int32).sum()); now = time.time()
        live = self.live.setdefault(kind, [None, 0.0])
        if live[0] is not None and sig != live[0]: live[1] = now
        live[0] = sig
        return img.std() > 3 or now - live[1] < 3

    def auto_kind(self):
        # probing render windows costs a capture each, so the choice is re-made twice a second, not every frame
        if time.time() - self.auto_t > 0.5:
            self.auto_choice = next((k for k in ("renderview", "octane") if self.has_image(k)), "viewport")
            self.auto_t = time.time()
        return self.auto_choice

    def capture(self):
        src = self.cfg["source"]
        if src == "area":
            self.resolved = "area"; a = self.cfg.get("area")
            if not a: self.state = STATE_NO_AREA; return None
            return np.ascontiguousarray(self.grab.grab(*a))
        kind = self.auto_kind() if src == "auto" else src
        self.resolved = kind
        full = self.grab_kind(kind)
        if full is None:
            self.state = {"viewport": STATE_NO_VIEWPORT, "renderview": STATE_NO_RV, "octane": STATE_NO_OCTANE}[kind]; return None
        H, W = full.shape[:2]
        trim = self.cfg["trim"].get(kind)
        box = self.crop_box(kind, full) if kind in ("renderview", "octane") else None
        if box: x0, y0, x1, y1 = box                     # render windows: auto-crop to the render; manual trim is the fallback
        elif kind == "renderview" and not trim: self.state = STATE_NO_RV; return None
        elif trim:
            fx, fy, fw, fh = trim
            x0, y0 = int(fx * W), int(fy * H); x1, y1 = min(W, x0 + max(16, int(fw * W))), min(H, y0 + max(16, int(fh * H)))
        else: x0, y0, x1, y1 = 0, 0, W, H
        return np.ascontiguousarray(full[y0:y1, x0:x1])

    # ---- frame
    def compose(self):
        W, VH, SH = self.size
        out = Image.new("RGB", (W, VH + SH), self.bg); d = ImageDraw.Draw(out)
        raw = self.capture()
        if raw is None: self.view_rect = (0, 0, 0, 0); return out
        self.state = STATE_OK
        h, w = raw.shape[:2]
        ref_on = self.ref is not None and self.show_ref
        cw = W // 2 - 2 if ref_on else W
        s = min(cw / w, VH / h, 1.0)
        small = np.asarray(Image.fromarray(raw).resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR, reducing_gap=2.0))
        self.src_small = small
        view = Image.fromarray(self.process(small))
        vx, vy = (cw - view.width) // 2, (VH - view.height) // 2
        out.paste(view, (vx, vy)); self.view_rect = (vx, vy, view.width, view.height)
        refss = None
        if self.ref is not None:
            rh, rw = self.ref.shape[:2]; rs = min((cw if ref_on else W // 2) / rw, VH / rh, 1.0)
            key = (self.cfg["mode"], int(rw * rs), int(rh * rs), self.cfg["notan_lo"], self.cfg["notan_hi"], self.cfg["accent"], self.cfg["exposure"])
            if key not in self.ref_cache:
                rsm = np.asarray(Image.fromarray(self.ref).resize((max(1, key[1]), max(1, key[2])), Image.BILINEAR))
                self.ref_cache = {key: (self.scope_src(rsm), Image.fromarray(self.process(rsm)))}
            refss, rimg = self.ref_cache[key]
            if ref_on:
                rx = W - cw + (cw - rimg.width) // 2
                out.paste(rimg, (rx, (VH - rimg.height) // 2))
                d.text((vx + 6, vy + 4), "Yours", fill=(220, 220, 224), font=FONT_S)
                d.text((rx + 6, (VH - rimg.height) // 2 + 4), "Reference", fill=REF_RGB, font=FONT_S)
        self.draw_probes(d, small, view, vx, vy, W, VH)
        n = self.cfg["scope_count"]
        if SH > 0 and n > 0:
            ss = self.scope_src(small); gap = 4; sw = (W - gap * (n - 1)) // n
            for i in range(n):
                name = self.cfg["slots"][i]; x0 = i * (sw + gap)
                kw = {"gain": self.cfg["scope_gain"]}
                if name == "Vectorscope": kw["zoom"] = self.cfg["vec_zoom"]
                if name == "Match":
                    now = time.time(); cached, t = self.match_cache
                    if refss is not None and (cached is None or now - t > 0.25):
                        cached = scopes.compare(scopes.stats(ss), scopes.stats(refss)); self.match_cache = (cached, now)
                    kw["cache"] = cached if refss is not None else None
                ref_for = refss if self.show_ref else None
                out.paste(scopes.RENDER[name](ss, sw, SH, ref_for if name != "Match" else refss, **kw), (x0, VH))
        return out

    @staticmethod
    def scope_src(a8, maxw=320):
        h, w = a8.shape[:2]; s = min(1.0, maxw / w)
        return np.asarray(Image.fromarray(a8).resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR, reducing_gap=2.0), np.float32) / 255

    def sample(self, small, u, v):
        sx, sy = min(small.shape[1] - 1, int(u * small.shape[1])), min(small.shape[0] - 1, int(v * small.shape[0]))
        return small[max(0, sy - 1):sy + 2, max(0, sx - 1):sx + 2].reshape(-1, 3).mean(0)

    def draw_probes(self, d, small, view, vx, vy, W, VH):
        lines = []
        for i, (u, v) in enumerate(self.cfg["pins"]):
            px_, py_ = vx + u * view.width, vy + v * view.height
            t, zc = readout(self.sample(small, u, v))
            d.ellipse([px_ - 5, py_ - 5, px_ + 5, py_ + 5], outline=(0, 0, 0), width=3)
            d.ellipse([px_ - 5, py_ - 5, px_ + 5, py_ + 5], outline=(255, 255, 255), width=1)
            d.text((px_ + 7, py_ - 15), str(i + 1), fill=(255, 255, 255), font=FONT)
            lines.append(("%d   %s" % (i + 1, t), zc))
        if lines:
            bh = 18 * len(lines) + 6; y0 = vy + view.height - bh - 4
            d.rectangle([vx + 4, y0, vx + 4 + max(d.textlength(t, font=FONT) for t, _ in lines) + 28, y0 + bh], fill=(24, 24, 26))
            for i, (t, zc) in enumerate(lines):
                d.rectangle([vx + 10, y0 + 7 + i * 18, vx + 18, y0 + 15 + i * 18], fill=zc)
                d.text((vx + 24, y0 + 3 + i * 18), t, fill=(225, 225, 228), font=FONT)
        if self.hover:
            uv = self.panel_to_uv(*self.hover)
            if uv:
                t, zc = readout(self.sample(small, *uv)); hx, hy = self.hover
                tw = d.textlength(t, font=FONT) + 28
                bx = hx + 14 if hx + 14 + tw < W else hx - 14 - tw
                by = hy + 16 if hy + 40 < VH else hy - 36
                d.rectangle([bx, by, bx + tw, by + 22], fill=(24, 24, 26), outline=(64, 64, 70))
                d.rectangle([bx + 7, by + 7, bx + 15, by + 15], fill=zc)
                d.text((bx + 21, by + 4), t, fill=(235, 235, 238), font=FONT)

    # ---- publish: header + pixels (seqlock: odd seq while writing)
    def publish(self, img):
        a = np.asarray(img, np.uint8); h, w = a.shape[:2]
        if HDR + a.nbytes > SHM_SIZE: return
        flags = (2 if self.ref is not None else 0) | (4 if self.show_ref else 0) | (8 if self.idle else 0)
        self.seq += 1; struct.pack_into("<4sI", self.shm, 0, MAGIC, self.seq)
        struct.pack_into("<IIdIIIIIfIIIII", self.shm, 8, w, h, time.time(), self.state, self.cfg["mode"],
                         SOURCES.index(self.cfg["source"]), RESOLVED.index(self.resolved),
                         flags, float(self.fps), *[int(v) for v in self.view_rect], len(self.modes))
        meta = json.dumps({"names": self.names, "msg": self.msg, "pins": len(self.cfg["pins"]), "split": self.size[1],
                           "scope_count": self.cfg["scope_count"], "slots": self.cfg["slots"], "scopes": scopes.SCOPES,
                           "params": {k: self.cfg[k] for k in ("notan_lo", "notan_hi", "accent", "exposure", "max_fps", "scope_gain", "vec_zoom")},
                           "trim": self.resolved in self.cfg["trim"]}).encode("utf-8")[: HDR - 128]
        self.shm[128:128 + len(meta) + 1] = meta + b"\0"
        self.shm[HDR:HDR + a.nbytes] = a.tobytes()
        self.seq += 1; struct.pack_into("<I", self.shm, 4, self.seq)

    def tick(self):
        t0 = time.time()
        try:
            while True:
                data, _ = self.sock.recvfrom(4096); self.command(data.decode("utf-8", "replace"))
        except BlockingIOError: pass
        except SystemExit: self.root.destroy(); return
        if not self.selftest and time.time() - self.last_ping > KEEPALIVE_S:
            self.save(); self.root.destroy(); return
        mains = c4d_main()
        if mains and all(win32gui.IsIconic(h) for h in mains) and not self.selftest:
            self.state = STATE_PAUSED; self.idle = True
            self.publish(Image.new("RGB", (self.size[0], self.size[1] + self.size[2]), (30, 30, 33)))
            self.root.after(300, self.tick); return
        try:
            img = self.compose()
            sig = int(self.src_small[::4, ::4].astype(np.int32).sum()) if self.src_small is not None and self.state == STATE_OK else None
            self.idle = sig is not None and sig == self.last_sig and not self.dirty
            if not self.idle or self.state != STATE_OK: self.publish(img)
            self.last_sig, self.dirty = sig, False
            if self.selftest:
                self.selftest["n"] = self.selftest.get("n", 0) + 1
                if self.selftest["n"] >= 25:
                    img.save(self.selftest["out"])
                    print(f"state={self.state} src={self.cfg['source']}->{self.resolved} mode={self.cfg['mode']} fps={self.fps:.0f} view={self.view_rect} crops={ {k: v[0] for k, v in self.crops.items()} }")
                    self.root.destroy(); return
        except Exception as e:
            import traceback; self.msg = "Error: %s" % e; traceback.print_exc()
        dt = time.time() - t0
        if not self.idle: self.fps = (1 / max(dt, 1e-3)) if self.fps == 0 else self.fps * 0.9 + 0.1 / max(dt, 1e-3)
        frame_ms = 1000 // max(5, int(self.cfg["max_fps"]))
        # an unchanged image is only re-checked, never re-published; a progressive render changes every pass
        self.root.after(60 if self.idle else max(5, frame_ms - int(dt * 1000)), self.tick)

if __name__ == "__main__":
    st = None
    if "--selftest" in sys.argv: st = {"out": sys.argv[sys.argv.index("--selftest") + 1]}
    try: svc = Service(st)
    except OSError: sys.exit(0)                     # another service already owns the port
    for a in sys.argv:
        k, _, v = a.partition("=")
        if k == "--source": svc.cfg["source"] = v
        elif k == "--mode": svc.cfg["mode"] = int(v)
        elif k == "--ref": svc.set_ref(Image.open(v))
        elif k == "--scopes": svc.cfg["scope_count"] = int(v)
        elif k == "--slots": svc.cfg["slots"] = v.split(",")
        elif k == "--pins": svc.cfg["pins"] = [[float(x) for x in p.split(",")] for p in v.split(";")]
        elif k == "--hover": svc.hover = tuple(int(x) for x in v.split(","))
    if st: svc.size = (1100, 520, 230)
    svc.root.after(10, svc.tick); svc.root.mainloop()
