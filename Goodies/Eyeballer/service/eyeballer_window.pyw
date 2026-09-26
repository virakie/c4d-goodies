"""Eyeballer - live colour/value analysis of any screen area (C4D viewport, Redshift RenderView, ...).

Floating always-on-top window. Pick an area of the screen; it is shown live through the same
"Breakdown" LUTs used in Resolve (every Breakdown*.cube in the Resolve MCP LUT folder becomes a mode),
with optional scopes and a reference image processed side by side.

Hotkeys (global): Ctrl+Alt+0 original, Ctrl+Alt+1..9 modes, Ctrl+Alt+S scopes, Ctrl+Alt+F reference on/off,
                  Ctrl+Alt+P pick area, Ctrl+Alt+B hide/show.   (R and H are taken by another app here)
Run:  pythonw eyeballer_window.pyw
"""
import ctypes, glob, json, os, queue, re, threading, time
from ctypes import wintypes
import tkinter as tk
from tkinter import filedialog
import numpy as np
from PIL import Image, ImageDraw, ImageGrab, ImageTk
import win32con, win32gui, win32ui

ctypes.windll.shcore.SetProcessDpiAwareness(2)          # physical pixels everywhere
user32 = ctypes.windll.user32

LUT_DIRS = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "modes"),     # shipped with the plugin
            r"C:\ProgramData\Blackmagic Design\DaVinci Resolve\Support\LUT\MCP"]
CFG = os.path.join(os.environ["APPDATA"], "Eyeballer", "window.json")
BG, FG, ACC, REF = "#16161a", "#e8e8e8", "#ffd23c", (255, 150, 40)

# ---------------------------------------------------------------- LUTs
def load_cube(path):
    n, rows = None, []
    for line in open(path, encoding="utf-8", errors="ignore"):
        s = line.strip()
        if not s or s.startswith("#"): continue
        if s.upper().startswith("LUT_3D_SIZE"): n = int(s.split()[1]); continue
        if s[0].isdigit() or s[0] in "-.":
            rows.append([float(v) for v in s.split()[:3]])
    return np.asarray(rows, np.float32).reshape(n, n, n, 3)      # [b][g][r] (red fastest in .cube)

def apply_lut(img, lut):
    """img float32 HxWx3 in 0..1 -> trilinear lookup."""
    n = lut.shape[0]
    x = np.clip(img, 0, 1) * (n - 1)
    i = np.minimum(x.astype(np.int32), n - 2)
    f = x - i
    r, g, b = i[..., 0], i[..., 1], i[..., 2]
    fr, fg, fb = f[..., 0:1], f[..., 1:2], f[..., 2:3]
    def L(db, dg, dr): return lut[b + db, g + dg, r + dr]
    c00 = L(0, 0, 0) * (1 - fr) + L(0, 0, 1) * fr
    c01 = L(0, 1, 0) * (1 - fr) + L(0, 1, 1) * fr
    c10 = L(1, 0, 0) * (1 - fr) + L(1, 0, 1) * fr
    c11 = L(1, 1, 0) * (1 - fr) + L(1, 1, 1) * fr
    c0 = c00 * (1 - fg) + c01 * fg
    c1 = c10 * (1 - fg) + c11 * fg
    return c0 * (1 - fb) + c1 * fb

BITS = 7                                                   # 128 levels/channel lookup table, built once per mode
def build_table(lut):
    v = (np.arange(1 << BITS, dtype=np.float32) + 0.5) / (1 << BITS)
    r, g, b = np.meshgrid(v, v, v, indexing="ij")          # index order r, g, b
    rgb = np.stack([r, g, b], -1).reshape(-1, 1, 3)
    return (np.clip(apply_lut(rgb, lut), 0, 1) * 255 + 0.5).astype(np.uint8).reshape(-1, 3)

def apply_table(img8, table):
    s = 8 - BITS
    i = ((img8[..., 0].astype(np.int32) >> s) << (2 * BITS)) | ((img8[..., 1].astype(np.int32) >> s) << BITS) | (img8[..., 2].astype(np.int32) >> s)
    return table[i]

def find_modes():
    files = []
    for d in LUT_DIRS:
        files += glob.glob(os.path.join(d, "Breakdown*.cube"))
    modes, seen = [], set()
    for p in sorted(files, key=os.path.basename):
        name = re.sub(r"^Breakdown\s*", "", os.path.splitext(os.path.basename(p))[0])
        if name.lower() in seen:
            continue                      # same LUT in more than one folder: first folder wins
        try: modes.append((name, load_cube(p), p)); seen.add(name.lower())
        except Exception: pass
    return modes

# ---------------------------------------------------------------- capture (GDI BitBlt, virtual-desktop coords)
class Grabber:
    def __init__(self): self.size = None
    def grab(self, x, y, w, h):
        if self.size != (w, h):
            self.release()
            self.hdc = win32gui.GetDC(0)
            self.src = win32ui.CreateDCFromHandle(self.hdc)
            self.mem = self.src.CreateCompatibleDC()
            self.bmp = win32ui.CreateBitmap(); self.bmp.CreateCompatibleBitmap(self.src, w, h)
            self.mem.SelectObject(self.bmp); self.size = (w, h)
        self.mem.BitBlt((0, 0), (w, h), self.src, (x, y), win32con.SRCCOPY)
        a = np.frombuffer(self.bmp.GetBitmapBits(True), np.uint8).reshape(h, w, 4)
        return a[..., 2::-1]                                               # BGRA -> RGB
    def release(self):
        if self.size:
            win32gui.DeleteObject(self.bmp.GetHandle()); self.mem.DeleteDC(); self.src.DeleteDC()
            win32gui.ReleaseDC(0, self.hdc); self.size = None

# ---------------------------------------------------------------- scopes (on the ORIGINAL image)
def luma(a): return a[..., 0] * 0.2126 + a[..., 1] * 0.7152 + a[..., 2] * 0.0722

def _tone(h, gain=1.0):
    h = np.log1p(h); m = h.max()
    return (h / m * gain) if m > 0 else h

def scope_waveform(a, W, H, ref=None):
    out = np.zeros((H, W, 3), np.float32)
    for img, col in ((a, (0.35, 1.0, 0.45)), (ref, tuple(c / 255 for c in REF))):
        if img is None: continue
        y = luma(img); h_, w_ = y.shape
        xs = (np.arange(w_) * W // w_)[None, :].repeat(h_, 0)
        ys = (H - 1 - np.clip(y * (H - 1), 0, H - 1)).astype(np.int32)
        hist = np.bincount((ys * W + xs).ravel(), minlength=W * H).reshape(H, W).astype(np.float32)
        out += _tone(hist, 1.2)[..., None] * np.array(col, np.float32)
    im = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for v in (0.1, 0.5, 0.9):
        yy = int((1 - v) * (H - 1)); d.line([0, yy, W, yy], fill=(60, 60, 70))
    return im

VEC_TARGETS = [(0.75, 0, 0), (0.75, 0.75, 0), (0, 0.75, 0), (0, 0.75, 0.75), (0, 0, 0.75), (0.75, 0, 0.75)]
def _cbcr(a):
    y = luma(a)
    return (a[..., 2] - y) / 1.8556, (a[..., 0] - y) / 1.5748

def scope_vector(a, S, ref=None):
    out = np.zeros((S, S, 3), np.float32)
    zoom = 1.6                                             # most CG sits well inside the 75% targets
    for img, col in ((a, (0.9, 0.9, 0.9)), (ref, tuple(c / 255 for c in REF))):
        if img is None: continue
        cb, cr = _cbcr(img)
        px = np.clip((cb * zoom + 0.5) * (S - 1), 0, S - 1).astype(np.int32)
        py = np.clip((0.5 - cr * zoom) * (S - 1), 0, S - 1).astype(np.int32)
        hist = np.bincount((py * S + px).ravel(), minlength=S * S).reshape(S, S).astype(np.float32)
        out += _tone(hist, 1.3)[..., None] * np.array(col, np.float32)
    im = Image.fromarray((np.clip(out, 0, 1) * 255).astype(np.uint8))
    d = ImageDraw.Draw(im); c = S / 2
    d.ellipse([c - S * 0.45, c - S * 0.45, c + S * 0.45, c + S * 0.45], outline=(55, 55, 65))
    d.line([c, 0, c, S], fill=(40, 40, 48)); d.line([0, c, S, c], fill=(40, 40, 48))
    for t in VEC_TARGETS:
        cb, cr = _cbcr(np.array([[t]], np.float32))
        x, y = (cb[0, 0] * zoom + 0.5) * S, (0.5 - cr[0, 0] * zoom) * S
        d.rectangle([x - 3, y - 3, x + 3, y + 3], outline=tuple(int(v * 255 / 0.75) for v in t))
    cb, cr = _cbcr(np.array([[(0.87, 0.67, 0.55)]], np.float32))              # skin-tone line
    d.line([c, c, c + cb[0, 0] * zoom * S * 3, c - cr[0, 0] * zoom * S * 3], fill=(120, 90, 70))
    return im

def scope_hist(a, W, H, ref=None):
    im = Image.new("RGB", (W, H), (0, 0, 0)); d = ImageDraw.Draw(im)
    def curve(v, col):
        h = np.bincount(np.clip(v * (W - 1), 0, W - 1).astype(np.int32).ravel(), minlength=W).astype(np.float32)
        h = np.sqrt(h); h = h / (h.max() or 1) * (H - 4)
        d.line([(x, H - 1 - h[x]) for x in range(W)], fill=col, width=1)
    for ch, col in ((0, (255, 70, 70)), (1, (70, 255, 90)), (2, (80, 130, 255))): curve(a[..., ch], col)
    if ref is not None: curve(luma(ref), REF)
    return im

# ---------------------------------------------------------------- global hotkeys
HK = {1: ("mode", 0)}
for n in range(1, 10): HK[n + 1] = ("mode", n)
HK.update({20: ("scopes",), 21: ("ref",), 22: ("pick",), 23: ("hide",)})
VK = {1: 0x30, **{n + 1: 0x30 + n for n in range(1, 10)}, 20: ord("S"), 21: ord("F"), 22: ord("P"), 23: ord("B")}

def hotkey_thread(q, failed):
    MOD = 0x0001 | 0x0002 | 0x4000                          # ALT | CTRL | NOREPEAT
    for i, vk in VK.items():
        if not user32.RegisterHotKey(None, i, MOD, vk): failed.append(i)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == 0x0312: q.put(HK.get(msg.wParam))

# ---------------------------------------------------------------- app
class App:
    def __init__(self, selftest=None):
        self.cfg = {"region": None, "mode": 0, "scopes": True, "geom": "760x620+80+80", "ref": None}
        try: self.cfg.update(json.load(open(CFG)))
        except Exception: pass
        self.selftest, self.frames = selftest, 0          # {"out": png, "region": [..], "mode": n, "ref": path}
        if selftest: self.cfg.update({k: v for k, v in selftest.items() if k in ("region", "mode", "ref")}, scopes=True)
        self.modes = find_modes()
        self.grabber = Grabber(); self.tables = {}; self.ref = None; self.ref_cache = {}; self.show_ref = True
        self.q = queue.Queue(); self.hk_failed = []
        threading.Thread(target=hotkey_thread, args=(self.q, self.hk_failed), daemon=True).start()

        r = self.root = tk.Tk()
        r.title("Eyeballer"); r.configure(bg=BG); r.geometry(self.cfg["geom"])
        r.attributes("-topmost", True); r.protocol("WM_DELETE_WINDOW", self.quit)
        bar = tk.Frame(r, bg=BG); bar.pack(fill="x", padx=6, pady=(6, 2))
        def btn(parent, text, cmd, w=None):
            b = tk.Button(parent, text=text, command=cmd, bg="#26262c", fg=FG, activebackground="#3a3a44",
                          activeforeground=FG, relief="flat", bd=0, padx=7, pady=3, width=w)
            b.pack(side="left", padx=2); return b
        btn(bar, "Pick area", self.pick)
        self.mode_btns = [btn(bar, "Off", lambda: self.set_mode(0))]
        for i in range(len(self.modes)):
            self.mode_btns.append(btn(bar, str(i + 1), lambda i=i: self.set_mode(i + 1), 2))
        tk.Frame(bar, bg=BG, width=10).pack(side="left")
        btn(bar, "Ref…", self.load_ref); btn(bar, "Paste ref", self.paste_ref); btn(bar, "✕ ref", self.clear_ref)
        self.scope_btn = btn(bar, "Scopes", self.toggle_scopes)
        self.label = tk.Label(r, bg=BG, fg=ACC, anchor="w", font=("Segoe UI", 10, "bold")); self.label.pack(fill="x", padx=10)
        self.view = tk.Label(r, bg="black"); self.view.pack(fill="both", expand=True, padx=6, pady=4)
        self.scopes = tk.Label(r, bg="black"); self.scopes.pack(fill="x", padx=6, pady=(0, 6))
        self.status = tk.Label(r, bg=BG, fg="#8a8a96", anchor="w", font=("Segoe UI", 8)); self.status.pack(fill="x", padx=10, pady=(0, 4))
        r.update()
        hwnd = user32.GetParent(r.winfo_id())
        self.excluded = bool(user32.SetWindowDisplayAffinity(hwnd, 0x11))    # WDA_EXCLUDEFROMCAPTURE
        if self.cfg.get("ref") and os.path.exists(self.cfg["ref"]): self.set_ref(Image.open(self.cfg["ref"]), self.cfg["ref"])
        self.set_mode(self.cfg["mode"] if self.cfg["mode"] <= len(self.modes) else 0)
        self.fps_t, self.fps = time.time(), 0.0
        r.after(50, self.tick)

    # ---- state
    def set_mode(self, m):
        if m > len(self.modes): return
        self.cfg["mode"] = m
        for i, b in enumerate(self.mode_btns): b.configure(bg=("#5a4a10" if i == m else "#26262c"))
        self.label.configure(text=("Original" if m == 0 else self.modes[m - 1][0]))
        self.ref_cache = {}

    def toggle_scopes(self):
        self.cfg["scopes"] = not self.cfg["scopes"]
        if not self.cfg["scopes"]: self.scopes.configure(image=""); self.scopes.image = None

    def set_ref(self, im, path=None):
        self.ref = np.asarray(im.convert("RGB"), np.uint8); self.ref_cache = {}; self.show_ref = True
        self.cfg["ref"] = path

    def load_ref(self):
        p = filedialog.askopenfilename(filetypes=[("Images", "*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.webp *.exr"), ("All", "*.*")])
        if p: self.set_ref(Image.open(p), p)

    def paste_ref(self):
        c = ImageGrab.grabclipboard()
        if isinstance(c, list) and c: c = Image.open(c[0])
        if isinstance(c, Image.Image): self.set_ref(c)
        else: self.status.configure(text="Clipboard has no image")

    def clear_ref(self): self.ref = None; self.ref_cache = {}; self.cfg["ref"] = None

    def pick(self):
        vx, vy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        vw, vh = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        t = tk.Toplevel(self.root); t.overrideredirect(True); t.geometry(f"{vw}x{vh}+{vx}+{vy}")
        t.attributes("-alpha", 0.25); t.attributes("-topmost", True); t.configure(bg="black")
        c = tk.Canvas(t, bg="black", highlightthickness=0, cursor="crosshair"); c.pack(fill="both", expand=True)
        c.create_text(vw // 2, 40, text="Drag over the viewport / RenderView.  Esc = cancel", fill="white", font=("Segoe UI", 16))
        s = {}
        def down(e): s["p"] = (e.x_root, e.y_root); s["r"] = c.create_rectangle(e.x, e.y, e.x, e.y, outline=ACC, width=3)
        def move(e):
            if "r" in s: x0, y0 = s["p"]; c.coords(s["r"], x0 - vx, y0 - vy, e.x, e.y)
        def up(e):
            x0, y0 = s["p"]; x1, y1 = e.x_root, e.y_root
            x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
            if w > 20 and h > 20: self.cfg["region"] = [x, y, w, h]
            t.destroy()
        c.bind("<ButtonPress-1>", down); c.bind("<B1-Motion>", move); c.bind("<ButtonRelease-1>", up)
        t.bind("<Escape>", lambda e: t.destroy()); t.focus_force()

    # ---- frame loop
    def process(self, a8):
        m = self.cfg["mode"]
        if m == 0: return a8
        if m not in self.tables:
            name, lut, path = self.modes[m - 1]
            cache = os.path.join(os.path.dirname(CFG), "tables", f"{name[:40]}.{int(os.path.getmtime(path))}.b{BITS}.npy")
            if os.path.exists(cache): self.tables[m] = np.load(cache)
            else:
                self.status.configure(text="building mode table (first time only)…"); self.status.update()
                self.tables[m] = build_table(lut)
                os.makedirs(os.path.dirname(cache), exist_ok=True); np.save(cache, self.tables[m])
        return apply_table(a8, self.tables[m])

    def fit(self, a8, W, H):
        h, w = a8.shape[:2]; s = min(W / w, H / h)
        return Image.fromarray(a8).resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR)

    @staticmethod
    def scope_src(a8, maxw=320):
        h, w = a8.shape[:2]; s = min(1.0, maxw / w)
        return np.asarray(Image.fromarray(a8).resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR), np.float32) / 255

    def tick(self):
        t0 = time.time()
        while not self.q.empty():
            act = self.q.get()
            if not act: continue
            if act[0] == "mode": self.set_mode(act[1])
            elif act[0] == "scopes": self.toggle_scopes()
            elif act[0] == "ref": self.show_ref = not self.show_ref
            elif act[0] == "pick": self.pick()
            elif act[0] == "hide":
                (self.root.deiconify if self.root.state() == "withdrawn" else self.root.withdraw)()
        reg = self.cfg.get("region")
        if reg and self.root.state() != "withdrawn":
            try:
                x, y, w, h = reg
                raw = self.grabber.grab(x, y, w, h)
                VW, VH = max(50, self.view.winfo_width()), max(50, self.view.winfo_height())
                ref_on = self.ref is not None and self.show_ref
                cellW = VW // 2 if ref_on else VW
                s = min(cellW / w, VH / h, 1.0)
                small = np.asarray(Image.fromarray(raw).resize((max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR))
                out = self.fit(self.process(small), cellW, VH)
                refsmall = None
                if ref_on:
                    rh, rw = self.ref.shape[:2]; rs = min(cellW / rw, VH / rh, 1.0)
                    key = (self.cfg["mode"], int(rw * rs), int(rh * rs))
                    if key not in self.ref_cache:
                        rsm = np.asarray(Image.fromarray(self.ref).resize((key[1], key[2]), Image.BILINEAR))
                        self.ref_cache = {key: (self.scope_src(rsm), self.fit(self.process(rsm), cellW, VH))}
                    refsmall, rimg = self.ref_cache[key]
                    canvas = Image.new("RGB", (VW, max(out.height, rimg.height)), (0, 0, 0))
                    canvas.paste(out, ((cellW - out.width) // 2, 0)); canvas.paste(rimg, (cellW + (cellW - rimg.width) // 2, 0))
                    d = ImageDraw.Draw(canvas); d.text((6, 4), "YOURS", fill=(230, 230, 230)); d.text((cellW + 6, 4), "REF", fill=REF)
                    out = canvas
                ph = ImageTk.PhotoImage(out); self.view.configure(image=ph); self.view.image = ph
                shot = out
                if self.cfg["scopes"]:
                    SW = max(300, self.scopes.winfo_width()); sh = 150
                    ss = self.scope_src(small)
                    wv = scope_waveform(ss, SW // 3 - 4, sh, refsmall)
                    vs = scope_vector(ss, sh, refsmall)
                    hs = scope_hist(ss, SW - wv.width - vs.width - 8, sh, refsmall)
                    strip = Image.new("RGB", (SW, sh), BG)
                    strip.paste(wv, (0, 0)); strip.paste(vs, (wv.width + 4, 0)); strip.paste(hs, (wv.width + vs.width + 8, 0))
                    sp = ImageTk.PhotoImage(strip); self.scopes.configure(image=sp); self.scopes.image = sp
                    both = Image.new("RGB", (max(shot.width, strip.width), shot.height + strip.height + 4), BG)
                    both.paste(shot, (0, 0)); both.paste(strip, (0, shot.height + 4)); shot = both
                self.frames += 1
                if self.selftest and self.frames >= 40:
                    shot.save(self.selftest["out"]); print(f"selftest: {self.fps:.0f} fps, modes={len(self.modes)}, excluded={self.excluded}, hotkeys_failed={self.hk_failed}")
                    self.root.destroy(); return
                dt = time.time() - t0; self.fps = self.fps * 0.9 + (1 / max(dt, 1e-3)) * 0.1
                self.status.configure(text=f"area {w}x{h} at {x},{y}   ~{self.fps:.0f} fps"
                    + ("" if self.excluded else "   (window can't be hidden from capture - keep it off the area)")
                    + (f"   hotkeys taken by another app: {len(self.hk_failed)}" if self.hk_failed else ""))
            except Exception as e:
                self.status.configure(text=f"capture error: {e}")
        elif not reg:
            self.status.configure(text="Click 'Pick area' (Ctrl+Alt+P) and drag over your C4D viewport or Redshift RenderView")
        self.root.after(max(5, 33 - int((time.time() - t0) * 1000)), self.tick)

    def quit(self):
        self.cfg["geom"] = self.root.geometry()
        os.makedirs(os.path.dirname(CFG), exist_ok=True); json.dump(self.cfg, open(CFG, "w"))
        self.grabber.release(); self.root.destroy()

if __name__ == "__main__":
    import sys
    st = None
    if "--selftest" in sys.argv:
        a = sys.argv; st = {"out": a[a.index("--selftest") + 1]}
        if "--region" in a: st["region"] = [int(v) for v in a[a.index("--region") + 1].split(",")]
        if "--mode" in a: st["mode"] = int(a[a.index("--mode") + 1])
        if "--ref" in a: st["ref"] = a[a.index("--ref") + 1]
    App(st).root.mainloop()
