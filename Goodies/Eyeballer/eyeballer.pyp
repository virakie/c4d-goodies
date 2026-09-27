"""Eyeballer - dockable C4D panel that shows the viewport or the Redshift RenderView live through
analysis views (Brightness, Light & Shadow, Exposure Zones, Saturation, ...) with DaVinci-style scopes,
reference compare, probes and a match scope.

The heavy lifting runs in an external Python process (eyeballer_service.pyw, numpy); this plugin starts it, pings it,
sends it commands over UDP and draws the frames it publishes in shared memory.

In the image:  click = pin a probe (max 4) - right-click a probe = remove it - hover = live readout
               keys 0-8 = view, R = reference on/off
"""
import ctypes, json, mmap, os, socket, struct, subprocess, time
import c4d
from c4d import gui, plugins

# --- paths: the engine ships next to this file and runs in a normal Python 3
# with numpy, pillow and pywin32 (set EYEBALLER_PYTHON to pick a specific pythonw.exe)
HERE = os.path.dirname(os.path.abspath(__file__))
SERVICE = os.path.join(HERE, "service", "eyeballer_service.pyw")
GUIDE = os.path.join(HERE, "guide", "index.html")         # optional field guide


def _find_pythonw():
    import shutil
    for cand in (os.environ.get("EYEBALLER_PYTHON", ""), shutil.which("pythonw") or "", shutil.which("pyw") or ""):
        if cand and os.path.exists(cand) and "windowsapps" not in cand.lower():
            return cand
    return ""


PYTHONW = _find_pythonw()
ICON = os.path.join(os.path.dirname(__file__), "res", "icon.png")

PORT = 47819
SHM_NAME, SHM_SIZE, HDR, MAGIC = "EyeballerFrame", 40 * 1024 * 1024, 4096, b"BDM3"
SOURCES = ["auto", "viewport", "renderview", "octane", "area"]
SOURCE_NAMES = ["Auto", "Viewport", "Redshift RenderView", "Octane Live Viewer", "Screen area"]
RESOLVED = ["viewport", "renderview", "octane", "area"]
RESOLVED_NAMES = {"viewport": "Viewport", "renderview": "RenderView", "octane": "Octane", "area": "Screen area"}
SCOPES = ["Waveform", "RGB Parade", "Vectorscope", "Histogram", "CIE Chromaticity", "Match"]
SCOPE_HEIGHTS = [("Small", 150), ("Medium", 210), ("Large", 300)]
STATE_OK, STATE_NO_VIEWPORT, STATE_NO_RV, STATE_NO_AREA, STATE_PAUSED, STATE_NO_OCTANE = 0, 1, 2, 3, 4, 5
IPR_START, RS_RENDERVIEW = 1040206, 1038666            # Redshift "Start RenderView IPR" / "RS RenderView"
PLUGIN_ID = 1066610                                    # local id, kept from v1 so docked layouts still restore

# --- gadget ids
G_TOP, G_ADJ, G_VIEW, G_SCOPES, G_SCOPEBAR, G_SCOPEUA = 1000, 1001, 1002, 1003, 1004, 1005
CB_SOURCE, CB_VIEW, CK_REF, TX_STATUS, TX_HINT = 1100, 1101, 1102, 1103, 1104
CB_SCOPECOUNT, CB_SLOT0 = 1200, 1210                   # 1210..1213
SL = {1300: ("notan_lo", "Shadow level", 0.18), 1301: ("notan_hi", "Highlight level", 0.55),
      1302: ("accent", "Accent strength", 0.35), 1303: ("exposure", "Exposure offset", 0.0),
      1304: ("scope_gain", "Scope brightness", 1.0)}
CB_VECZOOM = 1310
# --- menu ids
M_SRC0 = 2000                                          # 2000..2004
M_TRIM, M_TRIMRESET, M_IPR = 2010, 2011, 2012
M_REFPASTE, M_REFLOAD, M_REFSHOW, M_REFCLEAR = 2020, 2021, 2022, 2023
M_PINSCLEAR = 2030
M_ADJ, M_ADJRESET = 2040, 2041
M_HEIGHT0, M_FPS10, M_FPS20, M_FPS30 = 2050, 2060, 2061, 2062
M_LEGEND, M_HOWTO = 2070, 2071
M_UNDO, M_REDO = 2080, 2081

_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
_proc, _last_start = None, 0.0


_local = {}          # settings sent but not yet confirmed by a frame: key -> (value, time)

def send(cmd):
    try: _sock.sendto(cmd.encode("utf-8"), ("127.0.0.1", PORT))
    except OSError: pass
    p = cmd.split(" ", 2); now = time.time()
    if p[0] == "mode": _local["mode"] = (int(p[1]), now)
    elif p[0] == "source": _local["source"] = (p[1], now)
    elif p[0] == "scopes": _local["scope_count"] = (int(p[1]), now)
    elif p[0] == "slot": _local["slot%s" % p[1]] = (p[2], now)
    elif p[0] == "param": _local["param:" + p[1]] = (float(p[2]), now)
    elif p[0] == "ref" and p[1] in ("show", "hide"): _local["ref_shown"] = (p[1] == "show", now)


def ensure_service():
    """Start eyeballer_service.pyw unless it is running (it exits on its own 10 s after the last ping)."""
    global _proc, _last_start
    if _proc is not None and _proc.poll() is None: return True
    if time.time() - _last_start < 3: return True
    if not (PYTHONW and os.path.exists(SERVICE)):
        msg = "Eyeballer needs Python 3 with numpy, pillow and pywin32 (pip install numpy pillow pywin32)"
        print("[Eyeballer]", msg, "- python:", PYTHONW or "not found", "- service:", SERVICE)
        c4d.StatusSetText(msg); return False
    _last_start = time.time()
    _proc = subprocess.Popen([PYTHONW, SERVICE, "--service"], cwd=os.path.dirname(SERVICE), creationflags=0x08000000)
    return True


def local_mouse(ua):
    """Cursor position in the user area's local coordinates, or None when outside it."""
    class P(ctypes.Structure): _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
    try: o = ua.Local2Screen()
    except Exception: return None
    p = P(); ctypes.windll.user32.GetCursorPos(ctypes.byref(p))
    x, y = p.x - o["x"], p.y - o["y"]
    return (x, y) if 0 <= x < ua.GetWidth() and 0 <= y < ua.GetHeight() else None


class Frame(object):
    """Reader for the service's shared-memory frame (seqlock: odd seq = being written)."""
    FMT = "<IIdIIIIIfIIIII"

    def __init__(self):
        self.mm = mmap.mmap(-1, SHM_SIZE, tagname=SHM_NAME)
        self.seq = -1

    def read(self):
        mm = self.mm
        if mm[:4] != MAGIC: return None
        s1 = struct.unpack_from("<I", mm, 4)[0]
        if s1 == self.seq or s1 & 1: return None
        (w, h, stamp, state, mode, source, resolved, flags, fps, vx, vy, vw, vh, nmodes) = struct.unpack_from(self.FMT, mm, 8)
        if w == 0 or w * h * 3 + HDR > SHM_SIZE: return None
        meta = mm[128:HDR].split(b"\0", 1)[0]
        px = bytearray(mm[HDR:HDR + w * h * 3])                   # SetPixelCnt rejects read-only buffers
        if struct.unpack_from("<I", mm, 4)[0] != s1: return None  # torn read, try next tick
        self.seq = s1
        try: meta = json.loads(meta.decode("utf-8"))
        except Exception: meta = {}
        info = dict(meta, state=state, mode=mode, source=SOURCES[source] if source < len(SOURCES) else "auto",
                    resolved=RESOLVED[resolved] if resolved < len(RESOLVED) else "viewport",
                    ref=bool(flags & 2), ref_shown=bool(flags & 4), idle=bool(flags & 8), fps=fps, view=(vx, vy, vw, vh))
        return w, h, px, info


class ImageArea(gui.GeUserArea):
    """Draws one horizontal slice of the published frame (the view, or the scope strip)."""
    def __init__(self, panel, is_view):
        self.panel, self.is_view = panel, is_view
        self.bmp = None
        self.last_hover = None

    def set_pixels(self, w, y0, y1, px):
        h = max(1, y1 - y0)
        if self.bmp is None or self.bmp.GetBw() != w or self.bmp.GetBh() != h:
            self.bmp = c4d.bitmaps.BaseBitmap(); self.bmp.Init(w, h, 24)
        mv, stride = memoryview(px), w * 3
        for y in range(h):
            r = y0 + y
            self.bmp.SetPixelCnt(0, y, w, mv[r * stride:(r + 1) * stride], 3, c4d.COLORMODE_RGB, c4d.PIXELCNT_0)
        self.Redraw()

    def GetMinSize(self):
        return (240, 160) if self.is_view else (240, self.panel.scope_height())

    def Sized(self, w, h):
        self.panel.send_size()

    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        self.DrawSetPen(c4d.COLOR_BG); self.DrawRectangle(x1, y1, x2, y2)
        info = self.panel.info
        if self.bmp and info and info["state"] == STATE_OK:
            bw, bh = self.bmp.GetBw(), self.bmp.GetBh()
            self.DrawBitmap(self.bmp, 0, 0, bw, bh, 0, 0, bw, bh, c4d.BMP_NORMAL)
            return
        if not self.is_view: return
        state = info["state"] if info else -1
        lines = {-1: ("Starting...", ""),
                 STATE_NO_VIEWPORT: ("No viewport found", "Switch Source to RenderView, or show a viewport in this layout."),
                 STATE_NO_RV: ("Nothing in the RenderView yet", "Start a Redshift IPR (Source > Start Redshift IPR), or switch Source to Viewport."),
                 STATE_NO_AREA: ("No screen area picked", "Source > Screen area... and drag over the part of the screen to analyse."),
                 STATE_PAUSED: ("Paused", "Cinema 4D is minimised."),
                 STATE_NO_OCTANE: ("Octane Live Viewer not found", "Open it from the Octane menu. If it is docked, use Source > Trim / pick area... once and drag over it.")}.get(state, ("", ""))
        W, H = self.GetWidth(), self.GetHeight()
        self.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
        self.DrawText(lines[0], (W - self.DrawGetTextWidth(lines[0])) // 2, H // 2 - 18)
        self.DrawSetTextCol(c4d.COLOR_TEXT_DISABLED, c4d.COLOR_TRANS)
        self.DrawText(lines[1], (W - self.DrawGetTextWidth(lines[1])) // 2, H // 2 + 2)

    def poll_hover(self):
        if not self.is_view: return
        info = self.panel.info
        m = local_mouse(self) if info and info["state"] == STATE_OK else None
        if m != self.last_hover:
            send("hover %d %d" % m if m else "hover off"); self.last_hover = m

    def InputEvent(self, msg):
        if msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_KEYBOARD:
            if self.panel.undo_key(msg): return True
            ch = (msg[c4d.BFM_INPUT_ASC] or "").lower()
            if ch.isdigit() and int(ch) <= 8: self.panel.record("mode"); self.panel.set_view(int(ch)); return True
            if ch == "r": self.panel.record("ref"); send("ref toggle"); return True
            return False
        if not self.is_view or msg[c4d.BFM_INPUT_DEVICE] != c4d.BFM_INPUT_MOUSE: return False
        m = local_mouse(self)
        if m:
            ch = msg[c4d.BFM_INPUT_CHANNEL]
            if ch == c4d.BFM_INPUT_MOUSELEFT: send("pin %d %d" % m)
            elif ch == c4d.BFM_INPUT_MOUSERIGHT: send("unpin %d %d" % m)
        return True


class Panel(gui.GeDialog):
    def __init__(self):
        self.view = ImageArea(self, True)
        self.scope = ImageArea(self, False)
        self.frame = None
        self.info = None
        self.names = []
        self.adj_open = False
        self.scope_count = -1
        self.height_idx = 1
        self.last_ping = 0.0
        self.last_size = None
        self.shown = {}
        self.history, self.future, self._undo_tag, self._undo_t = [], [], None, 0.0

    # ---- layout
    def CreateLayout(self):
        self.SetTitle("Eyeballer")
        # Docking/undocking re-runs CreateLayout on this same instance: forget what the old
        # widgets showed, or sync() thinks the fresh ones are already filled in.
        self.names, self.shown = [], {}
        self.scope_count, self.last_size, self.adj_open = -1, None, False
        self.MenuFlushAll()
        self.MenuSubBegin("Edit")
        self.MenuAddString(M_UNDO, "Undo  (Ctrl+Z)")
        self.MenuAddString(M_REDO, "Redo  (Ctrl+Shift+Z)")
        self.MenuSubEnd()
        self.MenuSubBegin("Source")
        for i, n in enumerate(SOURCE_NAMES): self.MenuAddString(M_SRC0 + i, n + ("..." if n == "Screen area" else ""))
        self.MenuAddSeparator()
        self.MenuAddString(M_TRIM, "Trim / pick area...")
        self.MenuAddString(M_TRIMRESET, "Reset trim")
        self.MenuAddSeparator()
        self.MenuAddString(M_IPR, "Start Redshift IPR")
        self.MenuSubEnd()
        self.MenuSubBegin("Reference")
        self.MenuAddString(M_REFPASTE, "Paste from clipboard")
        self.MenuAddString(M_REFLOAD, "Load image...")
        self.MenuAddSeparator()
        self.MenuAddString(M_REFSHOW, "Show reference")
        self.MenuAddString(M_REFCLEAR, "Remove reference")
        self.MenuSubEnd()
        self.MenuSubBegin("Options")
        self.MenuAddString(M_ADJ, "Adjustments")
        self.MenuAddString(M_PINSCLEAR, "Clear probes")
        self.MenuAddSeparator()
        self.MenuSubBegin("Scope height")
        for i, (n, _) in enumerate(SCOPE_HEIGHTS): self.MenuAddString(M_HEIGHT0 + i, n)
        self.MenuSubEnd()
        self.MenuSubBegin("Frame rate")
        for mid, n in ((M_FPS10, "10 fps"), (M_FPS20, "20 fps"), (M_FPS30, "30 fps")): self.MenuAddString(mid, n)
        self.MenuSubEnd()
        self.MenuAddSeparator()
        self.MenuAddString(M_ADJRESET, "Reset adjustments")
        self.MenuSubEnd()
        self.MenuSubBegin("Help")
        self.MenuAddString(M_HOWTO, "How to use...")
        self.MenuAddString(M_LEGEND, "Field guide...")
        self.MenuSubEnd()
        self.MenuFinished()

        self.GroupBorderSpace(4, 4, 4, 4)
        if self.GroupBegin(G_TOP, c4d.BFH_SCALEFIT, 0, 1):
            self.GroupSpace(6, 0)
            self.AddStaticText(0, c4d.BFH_LEFT, name="Source")
            self.AddComboBox(CB_SOURCE, c4d.BFH_LEFT, 150)
            for i, n in enumerate(SOURCE_NAMES): self.AddChild(CB_SOURCE, i, n)
            self.AddStaticText(0, c4d.BFH_LEFT, name="  View")
            self.AddComboBox(CB_VIEW, c4d.BFH_LEFT, 150)
            self.AddChild(CB_VIEW, 0, "Original")
            self.AddStaticText(0, c4d.BFH_LEFT, name="  Scopes")
            self.AddComboBox(CB_SCOPECOUNT, c4d.BFH_LEFT, 60)
            for n, label in enumerate(("Off", "1", "2", "3", "4")): self.AddChild(CB_SCOPECOUNT, n, label)
            self.AddCheckbox(CK_REF, c4d.BFH_LEFT, 0, 0, "Reference")
            self.AddStaticText(TX_STATUS, c4d.BFH_SCALEFIT, name="")
            self.GroupEnd()
        self.AddStaticText(TX_HINT, c4d.BFH_SCALEFIT, name="")
        if self.GroupBegin(G_ADJ, c4d.BFH_SCALEFIT, 4, 0):
            self.GroupEnd()
        self.AddUserArea(G_VIEW, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 320, 200)
        self.AttachUserArea(self.view, G_VIEW)
        if self.GroupBegin(G_SCOPES, c4d.BFH_SCALEFIT, 1, 0):
            self.GroupEnd()
        return True

    def build_adjustments(self, params=None):
        self.LayoutFlushGroup(G_ADJ)
        if self.adj_open:
            self.GroupBorderSpace(4, 4, 4, 4)
            p = params if params is not None else (self.info or {}).get("params", {})
            for gid, (key, label, default) in SL.items():
                self.AddStaticText(0, c4d.BFH_LEFT, name=label)
                self.AddEditSlider(gid, c4d.BFH_SCALEFIT, 120)
                self.set_slider(gid, p.get(key, default))
            self.AddStaticText(0, c4d.BFH_LEFT, name="Vectorscope zoom")
            self.AddComboBox(CB_VECZOOM, c4d.BFH_LEFT, 80)
            self.AddChild(CB_VECZOOM, 1, "1x"); self.AddChild(CB_VECZOOM, 2, "2x")
            self.SetInt32(CB_VECZOOM, 2 if p.get("vec_zoom", 1.0) > 1.5 else 1)
        self.LayoutChanged(G_ADJ)

    def set_slider(self, gid, v):
        key = SL[gid][0]
        if key == "exposure": self.SetFloat(gid, v, -3.0, 3.0, 0.1, c4d.FORMAT_FLOAT)
        elif key == "scope_gain": self.SetFloat(gid, v, 0.3, 3.0, 0.05, c4d.FORMAT_FLOAT)
        else: self.SetFloat(gid, v, 0.0, 1.0, 0.01, c4d.FORMAT_PERCENT)

    def scope_height(self):
        return SCOPE_HEIGHTS[self.height_idx][1] if self.scope_count > 0 else 0

    def build_scopes(self, count, slots):
        self.scope_count = count
        self.LayoutFlushGroup(G_SCOPES)
        self.SetInt32(CB_SCOPECOUNT, count)
        if count > 0:
            # one menu per scope, equal widths, so each sits exactly above its scope
            if self.GroupBegin(G_SCOPEBAR, c4d.BFH_SCALEFIT, count, 1):
                self.GroupSpace(4, 0)
                for i in range(count):
                    self.AddComboBox(CB_SLOT0 + i, c4d.BFH_SCALEFIT, 60)
                    for k, n in enumerate(SCOPES): self.AddChild(CB_SLOT0 + i, k, n)
                    self.SetInt32(CB_SLOT0 + i, SCOPES.index(slots[i]) if i < len(slots) and slots[i] in SCOPES else 0)
                self.GroupEnd()
            self.AddUserArea(G_SCOPEUA, c4d.BFH_SCALEFIT, 320, self.scope_height())
            self.AttachUserArea(self.scope, G_SCOPEUA)
        self.LayoutChanged(G_SCOPES)
        self.send_size()

    def InitValues(self):
        ensure_service()
        try: self.frame = Frame()
        except Exception as e: print("[Eyeballer] shared memory:", e)
        self.build_scopes(2, ["Waveform", "Vectorscope"])
        self.SetTimer(33)
        return True

    # ---- helpers
    def send_bg(self):
        try:
            v = gui.GetGuiWorldColor(c4d.COLOR_BG)
            send("bg %d %d %d" % (v.x * 255, v.y * 255, v.z * 255))
        except Exception: pass

    def send_size(self):
        size = (self.view.GetWidth(), self.view.GetHeight(), self.scope_height())
        if size != self.last_size and size[0] > 0:
            send("size %d %d %d" % size); self.last_size = size

    def set_view(self, n):
        send("mode %d" % n); self.SetInt32(CB_VIEW, n)

    def update_names(self, names):
        if names == self.names: return
        self.names = names
        self.FreeChildren(CB_VIEW)
        for i, (n, _) in enumerate(names): self.AddChild(CB_VIEW, i, "%d  %s" % (i, n))
        self.LayoutChanged(G_TOP)

    def sync(self, info):
        """Reflect service state in the widgets (only when it changed, so nothing fights the user)."""
        def changed(key, value):
            if self.shown.get(key) == value: return False
            self.shown[key] = value; return True
        self.update_names([tuple(n) for n in info.get("names", [])])
        if changed("source", info["source"]): self.SetInt32(CB_SOURCE, SOURCES.index(info["source"]))
        if changed("mode", info["mode"]): self.SetInt32(CB_VIEW, info["mode"])
        if changed("hint", info["mode"]) or changed("names_n", len(self.names)):
            if info["mode"] < len(self.names): self.SetString(TX_HINT, "  " + self.names[info["mode"]][1])
        if changed("ref", (info["ref"], info["ref_shown"])):
            self.Enable(CK_REF, info["ref"]); self.SetBool(CK_REF, info["ref"] and info["ref_shown"])
        status = SOURCE_NAMES[SOURCES.index(info["source"])]
        if info["source"] == "auto": status += " > " + RESOLVED_NAMES.get(info["resolved"], "")
        if info.get("pins"): status += "   |   %d probe%s" % (info["pins"], "s" if info["pins"] > 1 else "")
        if info["state"] == STATE_OK and not info["idle"] and info["fps"] < 14: status += "   |   %.0f fps" % info["fps"]
        if info.get("msg"): status += "   |   " + info["msg"]
        if changed("status", status): self.SetString(TX_STATUS, status)
        # the service owns the scope count: rebuild whenever the panel shows something else
        # (after a re-layout InitValues guesses 2, which may not be what the service has)
        want = info.get("scope_count", 2)
        loc = _local.get("scope_count")
        if loc and time.time() - loc[1] < 1.5: want = loc[0]     # just changed here; the service hasn't caught up
        if want != self.scope_count:
            self.build_scopes(want, info.get("slots", []))
        p = info.get("params", {})
        for mid, fps in ((M_FPS10, 10), (M_FPS20, 20), (M_FPS30, 30)): self.MenuInitString(mid, True, int(p.get("max_fps", 30)) == fps)
        for i, s in enumerate(SOURCES): self.MenuInitString(M_SRC0 + i, True, info["source"] == s)
        self.MenuInitString(M_REFSHOW, info["ref"], info["ref"] and info["ref_shown"])
        self.MenuInitString(M_REFCLEAR, info["ref"], False)
        self.MenuInitString(M_ADJ, True, self.adj_open)
        self.MenuInitString(M_UNDO, bool(self.history), False)
        self.MenuInitString(M_REDO, bool(self.future), False)
        for i in range(len(SCOPE_HEIGHTS)): self.MenuInitString(M_HEIGHT0 + i, True, i == self.height_idx)

    # ---- events
    # ---- undo / redo: panel settings only; the scene's own undo stack is never touched
    def snapshot(self):
        """Current panel settings: the last frame's state, overlaid with changes sent since (up to 1.5 s old)."""
        i = self.info or {}
        s = {"mode": i.get("mode", 0), "source": i.get("source", "auto"), "scope_count": i.get("scope_count", 2),
             "slots": list(i.get("slots", [])), "params": dict(i.get("params", {})),
             "ref_shown": i.get("ref_shown", True), "height_idx": self.height_idx}
        now = time.time()
        for k, (v, t) in list(_local.items()):
            if now - t > 1.5: _local.pop(k, None); continue
            if k.startswith("param:"): s["params"][k[6:]] = v
            elif k.startswith("slot"):
                n = int(k[4:])
                while len(s["slots"]) <= n: s["slots"].append("Waveform")
                s["slots"][n] = v
            else: s[k] = v
        return s

    def record(self, tag):
        now = time.time()
        if tag == self._undo_tag and tag.startswith("param") and now - self._undo_t < 0.8:
            self._undo_t = now; return                       # a whole slider drag is one undo step
        self.history.append(self.snapshot()); del self.history[:-50]
        self.future.clear(); self._undo_tag, self._undo_t = tag, now

    def restore(self, s):
        cur = self.snapshot()
        if s["source"] != cur["source"]: send("source " + s["source"])
        if s["mode"] != cur["mode"]: send("mode %d" % s["mode"])
        if s["scope_count"] != cur["scope_count"]: send("scopes %d" % s["scope_count"])
        for i, n in enumerate(s["slots"]):
            if i >= len(cur["slots"]) or cur["slots"][i] != n: send("slot %d %s" % (i, n))
        for k, v in s["params"].items():
            if cur["params"].get(k) != v: send("param %s %f" % (k, v))
        if s["ref_shown"] != cur["ref_shown"]: send("ref show" if s["ref_shown"] else "ref hide")
        self.height_idx = s["height_idx"]
        self.SetInt32(CB_SOURCE, SOURCES.index(s["source"])); self.SetInt32(CB_VIEW, s["mode"])
        self.build_scopes(s["scope_count"], s["slots"])
        if self.adj_open: self.build_adjustments(s["params"])
        self.shown.clear(); self._undo_tag = None

    def undo(self):
        if self.history: self.future.append(self.snapshot()); self.restore(self.history.pop())

    def redo(self):
        if self.future: self.history.append(self.snapshot()); self.restore(self.future.pop())

    def undo_key(self, msg):
        """Ctrl+Z / Ctrl+Shift+Z / Ctrl+Y while the panel has focus."""
        q, ch = msg[c4d.BFM_INPUT_QUALIFIER], msg[c4d.BFM_INPUT_CHANNEL]
        if not (q & c4d.QCTRL) or ch not in (ord("Z"), ord("Y")): return False
        (self.redo if ch == ord("Y") or q & c4d.QSHIFT else self.undo)()
        return True

    def Message(self, msg, result):
        if msg.GetId() == c4d.BFM_INPUT and msg[c4d.BFM_INPUT_DEVICE] == c4d.BFM_INPUT_KEYBOARD and self.undo_key(msg):
            return True
        return gui.GeDialog.Message(self, msg, result)

    def Command(self, id, msg):
        undoable = {CB_SOURCE: "source", CB_VIEW: "mode", CK_REF: "ref", CB_SCOPECOUNT: "scopes", CB_VECZOOM: "veczoom",
                    M_REFSHOW: "ref", M_ADJRESET: "adjreset", M_FPS10: "fps", M_FPS20: "fps", M_FPS30: "fps"}
        if id in undoable: self.record(undoable[id])
        elif id in SL: self.record("param%d" % id)
        elif CB_SLOT0 <= id < CB_SLOT0 + 4 or M_SRC0 <= id < M_SRC0 + len(SOURCES) or M_HEIGHT0 <= id < M_HEIGHT0 + len(SCOPE_HEIGHTS):
            self.record("layout%d" % id)
        if id == M_UNDO: self.undo(); return True
        if id == M_REDO: self.redo(); return True
        if id == CB_SOURCE:
            s = SOURCES[self.GetInt32(CB_SOURCE)]; send("source " + s)
            if s == "area": send("pick")
        elif id == CB_VIEW: send("mode %d" % self.GetInt32(CB_VIEW))
        elif id == CK_REF: send("ref show" if self.GetBool(CK_REF) else "ref hide")
        elif id == CB_SCOPECOUNT:
            n = self.GetInt32(CB_SCOPECOUNT); send("scopes %d" % n)
            self.build_scopes(n, (self.info or {}).get("slots", SCOPES))
        elif CB_SLOT0 <= id < CB_SLOT0 + 4: send("slot %d %s" % (id - CB_SLOT0, SCOPES[self.GetInt32(id)]))
        elif id in SL: send("param %s %f" % (SL[id][0], self.GetFloat(id)))
        elif id == CB_VECZOOM: send("param vec_zoom %d" % self.GetInt32(CB_VECZOOM))
        elif M_SRC0 <= id < M_SRC0 + len(SOURCES):
            s = SOURCES[id - M_SRC0]; send("source " + s)
            if s == "area": send("pick")
        elif id == M_TRIM: send("pick")
        elif id == M_TRIMRESET: send("trim reset")
        elif id == M_IPR: c4d.CallCommand(RS_RENDERVIEW); c4d.CallCommand(IPR_START); send("source auto")
        elif id == M_REFPASTE: send("ref paste")
        elif id == M_REFLOAD:
            path = c4d.storage.LoadDialog(c4d.FILESELECTTYPE_IMAGES, "Reference image")
            if path: send("ref load " + path)
        elif id == M_REFSHOW: send("ref toggle")
        elif id == M_REFCLEAR: send("ref clear")
        elif id == M_PINSCLEAR: send("pins clear")
        elif id == M_ADJ: self.adj_open = not self.adj_open; self.build_adjustments()
        elif id == M_ADJRESET:
            for gid, (key, label, default) in SL.items(): send("param %s %f" % (key, default))
            send("param vec_zoom 1")
            if self.adj_open: self.build_adjustments()
        elif M_HEIGHT0 <= id < M_HEIGHT0 + len(SCOPE_HEIGHTS):
            self.height_idx = id - M_HEIGHT0
            self.build_scopes(self.scope_count, (self.info or {}).get("slots", SCOPES))
        elif id in (M_FPS10, M_FPS20, M_FPS30): send("param max_fps %d" % {M_FPS10: 10, M_FPS20: 20, M_FPS30: 30}[id])
        elif id == M_LEGEND:
            if os.path.exists(GUIDE): c4d.storage.GeExecuteFile(GUIDE)
        elif id == M_HOWTO:
            gui.MessageDialog("Eyeballer\n\n"
                              "Source: what to analyse - Auto follows the Redshift RenderView or Octane Live Viewer while it shows a render, else the viewport.\n"
                              "View: how to look at it (Brightness, Light & Shadow, Exposure Zones, ...). Keys 0-8 switch views.\n"
                              "Scopes: pick 1-4 and choose each one from its menu. Match compares yours with the reference.\n\n"
                              "In the image: hover for a readout, click to pin a probe (up to 4), right-click a probe to remove it.\n"
                              "Reference: paste or load a still to see it side by side and in the scopes (orange). R toggles it.\n\n"
                              "Ctrl+Z / Ctrl+Shift+Z undo and redo panel changes (views, scopes, adjustments) while the panel has focus.")
        return True

    def Timer(self, msg):
        now = time.time()
        if now - self.last_ping > 1.0:
            ensure_service(); send("ping"); self.send_bg(); self.last_size = None; self.send_size(); self.last_ping = now
        self.view.poll_hover()
        if not self.frame: return
        f = self.frame.read()
        if not f: return
        w, h, px, info = f
        old = self.info; self.info = info
        self.sync(info)
        if info["state"] == STATE_OK:
            split = min(h, int(info.get("split", h)))
            self.view.set_pixels(w, 0, split, px)
            if self.scope_count > 0 and h > split: self.scope.set_pixels(w, split, h, px)
        elif old is None or old["state"] != info["state"]:
            self.view.Redraw()

    def DestroyWindow(self):
        self.SetTimer(0); send("hover off")


_panel = None

def panel():
    global _panel
    if _panel is None: _panel = Panel()
    return _panel


class OpenPanel(plugins.CommandData):
    def Execute(self, doc):
        return panel().Open(c4d.DLG_TYPE_ASYNC, PLUGIN_ID, defaultw=900, defaulth=720)

    def RestoreLayout(self, sec_ref):
        return panel().Restore(PLUGIN_ID, sec_ref)


def PluginMessage(id, data):
    if id == c4d.C4DPL_ENDPROGRAM:
        send("quit")
    return True


if __name__ == "__main__":
    bmp = c4d.bitmaps.BaseBitmap()
    icon = bmp if bmp.InitWith(ICON)[0] == c4d.IMAGERESULT_OK else None
    plugins.RegisterCommandPlugin(PLUGIN_ID, "Eyeballer", 0, icon,
                                  "Live look-development analysis of the viewport or RenderView", OpenPanel())
