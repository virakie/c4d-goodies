# -*- coding: utf-8 -*-
"""
C4D Timelapse
=============
Records a timelapse of your working session straight to disk via ffmpeg.

Design notes
------------
* All encoding happens in a child ffmpeg process (NVENC where available), so the
  Cinema 4D process never pays for compression.
* Frames are *pushed* from a MessageData timer tick rather than letting ffmpeg
  free-run. That is what makes "skip idle time" possible at all -- there is no
  way to pause a free-running gdigrab.
* The default capture path (GDI StretchBlt of the C4D window) touches no C4D
  API, so the pixel copy runs entirely on a worker thread.
* Output is Matroska, not MP4. A C4D crash kills the child with a broken pipe,
  and an unfinalised MP4 has no moov atom -- the whole session would be lost.
  MKV survives truncation. A clean stop remuxes to MP4 with -c copy.
* Capture rate and playback rate are decoupled: ffmpeg is told the input is
  30 fps regardless of when frames actually arrive, so N frames always become
  N/30 seconds of video.
"""

import os
import json
import time
import re
import threading
import queue
import subprocess
import ctypes
from ctypes import wintypes

import c4d
from c4d import plugins, gui, bitmaps, storage

# --------------------------------------------------------------------------
# Plugin IDs. Local picks next to Eyeballer (1066610), out of the shared
# 1000001-1000010 dev range other people's test plugins also use. Register
# real ones at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
ID_PANEL = 1066611
ID_MESSAGE = 1066612

TICK_MS = 500


def log(*args):
    print("[timelapse]", *args)


# ==========================================================================
# Config
# ==========================================================================

DEFAULTS = {
    "interval": 4.0,             # seconds between frames
    "source": "window",          # "window" | "viewport"
    "width": 1920,               # output width in px (height follows aspect)
    "quality": 28,               # nvenc cq / x264 crf
    "fps_out": 30,               # playback fps
    "encoder": "",               # "" = probe once and cache
    "require_foreground": True,  # only grab when C4D has focus
    "require_change": True,      # only grab when the scene actually changed
    "autostart": True,           # start recording when an armed doc opens
    "ffmpeg": "",                # explicit path override
    "output_mode": "scene",      # "scene" = beside the .c4d | "custom"
    "output_root": "",           # custom root; "" = <prefs>/timelapse/recordings
    "remux_mp4": True,           # remux to .mp4 on clean stop
}

# Subfolder created beside the scene file in "scene" mode. Recordings go in
# their own folder rather than littering the project directory with .mp4s.
SCENE_SUBFOLDER = "timelapse"


def prefs_dir():
    d = os.path.join(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS), "timelapse")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


class Config(object):
    def __init__(self):
        self.path = os.path.join(prefs_dir(), "index.json")
        self.settings = dict(DEFAULTS)
        self.armed = {}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            s = blob.get("settings", {})
            for k, v in DEFAULTS.items():
                self.settings[k] = s.get(k, v)
            self.armed = blob.get("armed", {})
        except FileNotFoundError:
            pass
        except Exception as exc:
            log("could not read config:", exc)

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"settings": self.settings, "armed": self.armed},
                          fh, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            log("could not write config:", exc)

    def is_armed(self, key):
        return bool(self.armed.get(key))

    def set_armed(self, key, on):
        if on:
            self.armed[key] = True
        else:
            self.armed.pop(key, None)
        self.save()

    def output_root(self):
        root = self.settings.get("output_root") or ""
        if not root:
            root = os.path.join(prefs_dir(), "recordings")
        try:
            os.makedirs(root, exist_ok=True)
        except Exception:
            pass
        return root


# ==========================================================================
# ffmpeg discovery + encoder probe
# ==========================================================================

CREATE_NO_WINDOW = 0x08000000


def find_ffmpeg(cfg):
    override = cfg.settings.get("ffmpeg") or ""
    if override and os.path.isfile(override):
        return override
    # Prefer a real binary over a shim so stdin piping is unambiguous.
    candidates = [
        os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe"),
    ]
    for cand in candidates:
        if os.path.isfile(cand):
            return cand
    import shutil as _sh
    return _sh.which("ffmpeg") or ""


def probe_encoder(ffmpeg):
    """One ~200ms subprocess; the result is cached in the config."""
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20,
                             creationflags=CREATE_NO_WINDOW).stdout
    except Exception as exc:
        log("encoder probe failed:", exc)
        return "libx264"
    for enc in ("hevc_nvenc", "h264_nvenc", "h264_amf", "libx264"):
        if enc in out:
            return enc
    return "libx264"


def encoder_args(enc, quality):
    if enc.endswith("_nvenc"):
        return ["-c:v", enc, "-preset", "p5", "-tune", "hq",
                "-rc", "vbr", "-cq", str(quality), "-b:v", "0"]
    if enc.endswith("_amf"):
        return ["-c:v", enc, "-quality", "quality", "-rc", "cqp",
                "-qp_i", str(quality), "-qp_p", str(quality)]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(quality)]


# ==========================================================================
# Win32 window capture (no C4D API -> safe on a worker thread)
# ==========================================================================

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

SRCCOPY = 0x00CC0020
COLORONCOLOR = 3
BI_RGB = 0
DIB_RGB_COLORS = 0

LPVOID = ctypes.c_void_p


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD),
                ("biWidth", ctypes.c_long),
                ("biHeight", ctypes.c_long),
                ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD),
                ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD),
                ("biXPelsPerMeter", ctypes.c_long),
                ("biYPelsPerMeter", ctypes.c_long),
                ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 3)]


# Handles are pointers. Without explicit restypes ctypes truncates them to
# int32 on 64-bit and you get silent corruption or a crash.
user32.GetDC.restype = LPVOID
user32.GetDC.argtypes = [LPVOID]
user32.ReleaseDC.argtypes = [LPVOID, LPVOID]
user32.GetForegroundWindow.restype = LPVOID
user32.GetWindowRect.argtypes = [LPVOID, ctypes.POINTER(RECT)]
user32.IsIconic.argtypes = [LPVOID]
user32.IsWindowVisible.argtypes = [LPVOID]
user32.GetWindowThreadProcessId.argtypes = [LPVOID,
                                            ctypes.POINTER(wintypes.DWORD)]

gdi32.CreateCompatibleDC.restype = LPVOID
gdi32.CreateCompatibleDC.argtypes = [LPVOID]
gdi32.CreateCompatibleBitmap.restype = LPVOID
gdi32.CreateCompatibleBitmap.argtypes = [LPVOID, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = LPVOID
gdi32.SelectObject.argtypes = [LPVOID, LPVOID]
gdi32.DeleteObject.argtypes = [LPVOID]
gdi32.DeleteDC.argtypes = [LPVOID]
gdi32.SetStretchBltMode.argtypes = [LPVOID, ctypes.c_int]
gdi32.StretchBlt.argtypes = [LPVOID, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, LPVOID, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, wintypes.DWORD]
gdi32.BitBlt.argtypes = [LPVOID, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, LPVOID, ctypes.c_int, ctypes.c_int,
                         wintypes.DWORD]
gdi32.GetDIBits.argtypes = [LPVOID, LPVOID, ctypes.c_uint, ctypes.c_uint,
                            LPVOID, LPVOID, ctypes.c_uint]

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, LPVOID, LPVOID)


def find_main_hwnd():
    """Largest visible top-level window belonging to this process."""
    pid = os.getpid()
    best = [None, 0]

    def cb(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd):
            return True
        r = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return True
        area = (r.right - r.left) * (r.bottom - r.top)
        if area > best[1]:
            best[0], best[1] = hwnd, area
        return True

    proc = WNDENUMPROC(cb)
    user32.EnumWindows(proc, None)
    return best[0]


def window_rect(hwnd):
    r = RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r


def foreground_is_ours():
    """True if the focused window belongs to this process.

    Deliberately not `fg == main_hwnd`: torn-off managers and floating
    palettes are separate top-level windows, and focusing one of those is
    still very much working in C4D.
    """
    fg = user32.GetForegroundWindow()
    if not fg:
        return False
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(fg, ctypes.byref(owner))
    return owner.value == os.getpid()


def even(n):
    n = int(n)
    return n if n % 2 == 0 else n - 1


class WindowCapture(object):
    """Cached GDI resources; one blit + GetDIBits per frame.

    Captures at the window's size *as of session start* and hands the raw
    frame straight to ffmpeg, which does the downscale with a real bicubic
    filter in the other process. That is both faster and better-looking than
    doing it here: the ~20ms cost of a screen readback is fixed, but GDI's
    HALFTONE stretch adds ~14ms on top of it for a worse result.

    Frame size must stay constant for the life of a rawvideo stream, so if
    the window is resized mid-session we fall back to a stretch into the
    original dimensions for that frame rather than desyncing the pipe.
    """

    def __init__(self, hwnd, cap_w, cap_h):
        self.hwnd = hwnd
        self.w = cap_w
        self.h = cap_h
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        self.bmp = gdi32.CreateCompatibleBitmap(self.screen_dc, cap_w, cap_h)
        self.old = gdi32.SelectObject(self.mem_dc, self.bmp)
        gdi32.SetStretchBltMode(self.mem_dc, COLORONCOLOR)

        self.bi = BITMAPINFO()
        self.bi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        self.bi.bmiHeader.biWidth = cap_w
        self.bi.bmiHeader.biHeight = -cap_h          # negative = top-down
        self.bi.bmiHeader.biPlanes = 1
        self.bi.bmiHeader.biBitCount = 32
        self.bi.bmiHeader.biCompression = BI_RGB

        self.nbytes = cap_w * cap_h * 4
        self.buf = ctypes.create_string_buffer(self.nbytes)

    def grab(self):
        r = window_rect(self.hwnd)
        if r is None:
            return None
        sw = r.right - r.left
        sh = r.bottom - r.top
        if sw <= 0 or sh <= 0:
            return None
        if sw == self.w and sh == self.h:
            ok = gdi32.BitBlt(self.mem_dc, 0, 0, self.w, self.h,
                              self.screen_dc, r.left, r.top, SRCCOPY)
        else:
            ok = gdi32.StretchBlt(self.mem_dc, 0, 0, self.w, self.h,
                                  self.screen_dc, r.left, r.top, sw, sh,
                                  SRCCOPY)
        if not ok:
            return None
        got = gdi32.GetDIBits(self.mem_dc, self.bmp, 0, self.h,
                              self.buf, ctypes.byref(self.bi), DIB_RGB_COLORS)
        if got == 0:
            return None
        return bytes(self.buf)

    def close(self):
        try:
            if self.old:
                gdi32.SelectObject(self.mem_dc, self.old)
            if self.bmp:
                gdi32.DeleteObject(self.bmp)
            if self.mem_dc:
                gdi32.DeleteDC(self.mem_dc)
            if self.screen_dc:
                user32.ReleaseDC(None, self.screen_dc)
        except Exception:
            pass
        self.old = self.bmp = self.mem_dc = self.screen_dc = None


# ==========================================================================
# Viewport capture (offscreen hardware preview -- main thread only)
# ==========================================================================

def grab_viewport_png(doc, w, h):
    """Returns PNG bytes, or None. MUST be called on the main thread."""
    rdata = doc.GetActiveRenderData()
    if rdata is None:
        return None
    bc = rdata.GetDataInstance().GetClone(c4d.COPYFLAGS_NONE)
    bc[c4d.RDATA_RENDERENGINE] = c4d.RDATA_RENDERENGINE_PREVIEWHARDWARE
    bc[c4d.RDATA_XRES] = float(w)
    bc[c4d.RDATA_YRES] = float(h)
    bc[c4d.RDATA_FRAMESEQUENCE] = c4d.RDATA_FRAMESEQUENCE_CURRENTFRAME

    bmp = bitmaps.BaseBitmap()
    if bmp.Init(w, h, 24) != c4d.IMAGERESULT_OK:
        return None

    # NODOCUMENTCLONE skips cloning the scene, which on a heavy file is most
    # of the cost. It requires main thread + safe context, which a MSG_TIMER
    # tick is.
    res = c4d.documents.RenderDocument(
        doc, bc, bmp,
        c4d.RENDERFLAGS_EXTERNAL | c4d.RENDERFLAGS_NODOCUMENTCLONE)
    if res != c4d.RENDERRESULT_OK:
        return None

    mfs = storage.MemoryFileStruct()
    mfs.SetMemoryWriteMode()
    if bmp.Save(mfs, c4d.FILTER_PNG, None,
                c4d.SAVEBIT_NONE) != c4d.IMAGERESULT_OK:
        return None
    data = mfs.GetData()
    if not data:
        return None
    return bytes(data[0])


# ==========================================================================
# Encoder child process
# ==========================================================================

class Encoder(object):
    QUEUE_MAX = 8   # a dropped timelapse frame costs nothing; an OOM does

    def __init__(self, ffmpeg, out_path, log_path, in_args, enc_args):
        self.out_path = out_path
        self.frames = 0
        self.dropped = 0
        self.error = None

        cmd = ([ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
               + in_args + ["-an"] + enc_args
               + ["-pix_fmt", "yuv420p", out_path])
        log("spawn:", " ".join(cmd))

        # stderr goes to a file, not a pipe: an undrained stderr pipe can fill
        # and deadlock ffmpeg.
        self.logfh = open(log_path, "ab")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=self.logfh, creationflags=CREATE_NO_WINDOW)

        self.q = queue.Queue(maxsize=self.QUEUE_MAX)
        self.thread = threading.Thread(target=self._run, name="timelapse-enc",
                                       daemon=True)
        self.thread.start()

    def _run(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            try:
                self.proc.stdin.write(item)
                self.frames += 1
            except Exception as exc:
                self.error = str(exc)
                log("encoder write failed:", exc)
                break

    def alive(self):
        return self.error is None and self.proc.poll() is None

    def push(self, payload):
        try:
            self.q.put_nowait(payload)
            return True
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.q.put_nowait(payload)
            except queue.Full:
                pass
            self.dropped += 1
            return False

    def close(self):
        try:
            self.q.put(None, timeout=2)
        except Exception:
            pass
        self.thread.join(timeout=10)
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=30)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        try:
            self.logfh.close()
        except Exception:
            pass


# ==========================================================================
# Recorder -- one live session
# ==========================================================================

def slugify(name):
    base = os.path.splitext(name or "untitled")[0]
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")
    return slug or "untitled"


def output_folder(cfg, doc):
    """Where this document's recordings go.

    In "scene" mode that is <scene dir>/timelapse. An unsaved document has no
    scene dir, so it falls back to the custom/prefs root -- the dialog shows
    the resolved path so this is never a surprise.
    """
    if doc is not None and cfg.settings.get("output_mode", "scene") == "scene":
        path = doc.GetDocumentPath() or ""
        if path and os.path.isdir(path):
            return os.path.join(path, SCENE_SUBFOLDER)
    name = doc.GetDocumentName() if doc is not None else "untitled"
    return os.path.join(cfg.output_root(), slugify(name))


def last_recording(cfg, doc):
    folder = output_folder(cfg, doc)
    try:
        clips = [os.path.join(folder, f) for f in os.listdir(folder)
                 if f.lower().endswith((".mp4", ".mkv"))]
    except OSError:
        return None
    return max(clips, key=os.path.getmtime) if clips else None


class Recorder(object):
    def __init__(self, cfg, doc, key):
        self.cfg = cfg
        self.key = key
        self.slug = slugify(doc.GetDocumentName())
        self.source = cfg.settings["source"]
        self.interval = float(cfg.settings["interval"])
        self.started = time.time()
        self.last_frame_at = 0.0
        self.last_dirty = None
        self.enc = None
        self.cap = None
        self.hwnd = None
        self.path = None
        self.dims = (0, 0)
        self.status = "starting"

        ffmpeg = find_ffmpeg(cfg)
        if not ffmpeg:
            self.status = "ffmpeg not found (set an explicit path in index.json)"
            log(self.status)
            return

        enc_name = cfg.settings.get("encoder") or ""
        if not enc_name:
            enc_name = probe_encoder(ffmpeg)
            cfg.settings["encoder"] = enc_name
            cfg.save()
            log("using encoder:", enc_name)

        folder = output_folder(cfg, doc)
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception as exc:
            self.status = "cannot create output folder: %s" % exc
            log(self.status)
            return

        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.path = os.path.join(folder, "%s_%s.mkv" % (self.slug, stamp))
        log_path = os.path.join(folder, "ffmpeg.log")

        fps = int(cfg.settings["fps_out"])
        want_w = even(int(cfg.settings["width"]))

        if self.source == "window":
            self.hwnd = find_main_hwnd()
            if not self.hwnd:
                self.status = "C4D window not found"
                log(self.status)
                return
            r = window_rect(self.hwnd)
            if r is None:
                self.status = "window rect unavailable"
                log(self.status)
                return
            cap_w, cap_h = even(r.right - r.left), even(r.bottom - r.top)
            if cap_w <= 0 or cap_h <= 0:
                self.status = "window has zero size"
                log(self.status)
                return
            self.cap = WindowCapture(self.hwnd, cap_w, cap_h)
            in_args = ["-f", "rawvideo", "-pix_fmt", "bgra",
                       "-s", "%dx%d" % (cap_w, cap_h),
                       "-r", str(fps), "-i", "pipe:0"]
            # Downscale in ffmpeg, not in GDI: better filter, other process.
            if want_w < cap_w:
                out_w = want_w
                out_h = even(round(out_w * cap_h / float(cap_w)))
                vf = ["-vf", "scale=%d:%d:flags=bicubic" % (out_w, out_h)]
            else:
                out_w, out_h = cap_w, cap_h
                vf = []
        else:
            bd = doc.GetActiveBaseDraw()
            frame = bd.GetFrame() if bd else None
            if frame:
                sw = max(1, frame["cr"] - frame["cl"])
                sh = max(1, frame["cb"] - frame["ct"])
            else:
                sw, sh = 16, 9
            out_w = even(want_w)
            out_h = even(round(out_w * sh / float(sw)))
            in_args = ["-f", "image2pipe", "-c:v", "png",
                       "-r", str(fps), "-i", "pipe:0"]
            vf = []

        self.dims = (out_w, out_h)

        try:
            self.enc = Encoder(
                ffmpeg, self.path, log_path, in_args,
                vf + encoder_args(enc_name, int(cfg.settings["quality"])))
        except Exception as exc:
            self.status = "ffmpeg failed to start: %s" % exc
            log(self.status)
            return

        self.status = "recording"
        log("recording %s (%dx%d, %s)"
            % (self.path, out_w, out_h, self.source))

    def ok(self):
        return self.enc is not None and self.enc.alive()

    def maybe_capture(self, doc, now):
        """Gate chain, cheapest test first -- bails in microseconds."""
        if self.enc is None:
            return
        if not self.enc.alive():
            if self.status == "recording":
                self.status = "encoder died (see ffmpeg.log)"
                log(self.status)
            return

        if now - self.last_frame_at < self.interval:
            return

        if self.cfg.settings["require_foreground"]:
            if not foreground_is_ours():
                return
            if self.hwnd is None:
                self.hwnd = find_main_hwnd()
            if self.hwnd and user32.IsIconic(self.hwnd):
                return

        if self.cfg.settings["require_change"]:
            try:
                dirty = doc.GetDirty(c4d.DIRTYFLAGS_ALL)
            except Exception:
                dirty = None
            if dirty is not None:
                if self.last_dirty is not None and dirty == self.last_dirty:
                    return
                self.last_dirty = dirty

        payload = None
        if self.source == "window":
            if self.cap is not None:
                payload = self.cap.grab()
        else:
            payload = grab_viewport_png(doc, self.dims[0], self.dims[1])

        if payload:
            self.enc.push(payload)
            self.last_frame_at = now

    def stop(self):
        frames = dropped = 0
        if self.enc is not None:
            self.enc.close()
            frames, dropped = self.enc.frames, self.enc.dropped
            self.enc = None
        if self.cap is not None:
            self.cap.close()
            self.cap = None

        final = self.path
        if final and frames > 0 and self.cfg.settings.get("remux_mp4"):
            final = self._remux(final) or final

        if final and frames > 0:
            self._manifest(final, frames, dropped)
        elif final and frames == 0 and os.path.isfile(final):
            try:
                os.remove(final)   # empty session, don't litter
            except Exception:
                pass

        log("stopped: %d frames, %d dropped -> %s" % (frames, dropped, final))
        self.status = "idle"
        return frames

    def _remux(self, mkv):
        ffmpeg = find_ffmpeg(self.cfg)
        if not ffmpeg or not os.path.isfile(mkv):
            return None
        mp4 = os.path.splitext(mkv)[0] + ".mp4"
        try:
            rc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error",
                                 "-y", "-i", mkv, "-c", "copy",
                                 "-movflags", "+faststart", mp4],
                                timeout=300,
                                creationflags=CREATE_NO_WINDOW).returncode
            if rc == 0 and os.path.isfile(mp4):
                os.remove(mkv)
                return mp4
        except Exception as exc:
            log("remux failed:", exc)
        return None

    def _manifest(self, path, frames, dropped):
        try:
            rec = {
                "file": os.path.basename(path),
                "started": self.started,
                "ended": time.time(),
                "frames": frames,
                "dropped": dropped,
                "source": self.source,
                "interval": self.interval,
            }
            with open(os.path.join(os.path.dirname(path), "sessions.jsonl"),
                      "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec) + "\n")
        except Exception as exc:
            log("manifest write failed:", exc)


# ==========================================================================
# Manager
# ==========================================================================

def doc_key(doc):
    if doc is None:
        return None
    path = doc.GetDocumentPath() or ""
    name = doc.GetDocumentName() or "untitled"
    if path:
        return os.path.normcase(os.path.join(path, name))
    return "untitled:" + name


class Manager(object):
    def __init__(self):
        self.cfg = Config()
        self.rec = None
        self.cur_key = None
        self.merge_status = ""
        self.errored = False

    # -- lifecycle ---------------------------------------------------------
    def start(self, doc, key, quiet=False):
        self.stop()
        rec = Recorder(self.cfg, doc, key)
        if not rec.ok():
            # `quiet` matters: autostart runs from the timer tick, and a modal
            # dialog there would fire on every document switch.
            if not quiet:
                gui.MessageDialog("Timelapse could not start:\n\n%s"
                                  % rec.status)
            try:
                rec.stop()
            except Exception:
                pass
            return False
        self.rec = rec
        return True

    def stop(self):
        if self.rec is not None:
            rec, self.rec = self.rec, None
            try:
                rec.stop()
            except Exception as exc:
                log("stop failed:", exc)

    def shutdown(self):
        self.stop()
        self.cfg.save()

    # -- called from the timer --------------------------------------------
    def tick(self):
        doc = c4d.documents.GetActiveDocument()
        if doc is None:
            return
        key = doc_key(doc)

        if key != self.cur_key:
            self.cur_key = key
            self.stop()
            if self.cfg.settings["autostart"] and self.cfg.is_armed(key):
                self.start(doc, key, quiet=True)

        if self.rec is not None:
            self.rec.maybe_capture(doc, time.time())

    def toggle(self, doc):
        key = doc_key(doc)
        if self.rec is not None and self.cur_key == key:
            self.stop()
            self.cfg.set_armed(key, False)
            return False
        self.cur_key = key
        self.cfg.set_armed(key, True)
        if not self.start(doc, key):
            self.cfg.set_armed(key, False)
            return False
        return True

    def is_recording(self, doc):
        return self.rec is not None and self.cur_key == doc_key(doc)

    def status_line(self):
        if self.rec is None:
            return "Not recording"
        enc = self.rec.enc
        if enc is None:
            return self.rec.status
        elapsed = int(time.time() - self.rec.started)
        secs = enc.frames / float(max(1, int(self.cfg.settings["fps_out"])))
        line = ("REC  %d:%02d:%02d   %d frames = %.1f s of video"
                % (elapsed // 3600, elapsed // 60 % 60, elapsed % 60, enc.frames, secs))
        if enc.dropped:
            line += "   (%d dropped)" % enc.dropped
        return line

    # -- merge -------------------------------------------------------------
    def merge(self, doc):
        folder = output_folder(self.cfg, doc)
        if not os.path.isdir(folder):
            self.merge_status = "Nothing recorded for this project yet."
            return
        self.merge_status = "Merging..."
        threading.Thread(target=self._merge_worker, args=(folder,),
                         daemon=True).start()

    def _merge_worker(self, folder):
        try:
            clips = sorted(f for f in os.listdir(folder)
                           if f.lower().endswith((".mp4", ".mkv"))
                           and not f.startswith("ALL_"))
            if not clips:
                self.merge_status = "No sessions found."
                return
            listfile = os.path.join(folder, "_concat.txt")
            with open(listfile, "w", encoding="utf-8") as fh:
                for clip in clips:
                    fh.write("file '%s'\n" % clip.replace("'", "'\\''"))
            out = os.path.join(folder,
                               "ALL_%s.mp4" % os.path.basename(folder))
            ffmpeg = find_ffmpeg(self.cfg)
            rc = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error",
                                 "-y", "-f", "concat", "-safe", "0",
                                 "-i", listfile, "-c", "copy",
                                 "-movflags", "+faststart", out],
                                timeout=1800,
                                creationflags=CREATE_NO_WINDOW).returncode
            try:
                os.remove(listfile)
            except Exception:
                pass
            self.merge_status = (
                "Merged %d sessions -> %s" % (len(clips), os.path.basename(out))
                if rc == 0 else "Merge failed (rc=%d)" % rc)
        except Exception as exc:
            self.merge_status = "Merge failed: %s" % exc


MANAGER = Manager()


# ==========================================================================
# Panel -- one dockable window: record button, status, settings
# ==========================================================================

B_RECORD = 2000
T_STATUS = 2001
T_WHERE = 2002
E_INTERVAL = 2010
CB_SOURCE = 2011
CK_FG = 2012
CK_CHANGE = 2013
E_WIDTH = 2020
E_QUALITY = 2021
E_FPS = 2022
T_ENCODER = 2023
CB_OUTMODE = 2030
E_OUTPATH = 2031
B_BROWSE = 2032
CK_REMUX = 2033
CK_AUTO = 2034
M_FOLDER, M_LAST, M_MERGE, M_HELP = 2100, 2101, 2102, 2103

# Changing these mid-session would desync the running ffmpeg stream, so they
# lock while recording and apply to the next session.
LOCKED_WHILE_RECORDING = (CB_SOURCE, E_WIDTH, E_QUALITY, E_FPS, CB_OUTMODE,
                          E_OUTPATH, B_BROWSE)

HELP = ("Record makes a timelapse of your work on this project.\n\n"
        "- It keeps recording until you press Stop, and picks up again by itself "
        "whenever you reopen this project (untick 'Resume when the project "
        "reopens' to stop that).\n"
        "- Each session is its own video. Recordings > Merge sessions joins them "
        "into ALL_<project>.mp4.\n"
        "- Frames are only taken while C4D is in front and something in the "
        "scene changed, so breaks don't make it into the video.\n"
        "- Files go into a 'timelapse' folder next to the scene. Unsaved "
        "scenes use the custom folder instead.")


class Panel(gui.GeDialog):
    def _section(self, title, cols=2):
        self.GroupBegin(0, c4d.BFH_SCALEFIT, cols, 0, title, 0)
        self.GroupBorder(c4d.BORDER_GROUP_IN | c4d.BORDER_WITH_TITLE_BOLD)
        self.GroupBorderSpace(6, 4, 6, 6)
        self.GroupSpace(8, 4)

    def _label(self, text):
        self.AddStaticText(0, c4d.BFH_LEFT, 150, 0, text)

    def CreateLayout(self):
        self.SetTitle("Timelapse")
        self.MenuFlushAll()
        self.MenuSubBegin("Recordings")
        self.MenuAddString(M_FOLDER, "Open folder")
        self.MenuAddString(M_LAST, "Play last recording")
        self.MenuAddSeparator()
        self.MenuAddString(M_MERGE, "Merge sessions into one video")
        self.MenuSubEnd()
        self.MenuSubBegin("Help")
        self.MenuAddString(M_HELP, "How it works...")
        self.MenuSubEnd()
        self.MenuFinished()

        self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0, "", 0)
        self.GroupBorderSpace(6, 6, 6, 6)
        self.GroupSpace(0, 6)

        self.AddButton(B_RECORD, c4d.BFH_SCALEFIT, 0, 30, "Record")
        self.AddStaticText(T_STATUS, c4d.BFH_SCALEFIT, 0, 0, "Not recording",
                           c4d.BORDER_THIN_IN)
        self.AddStaticText(T_WHERE, c4d.BFH_SCALEFIT, 0, 0, "")

        self._section("Capture")
        self._label("Take a frame every (s)")
        self.AddEditNumberArrows(E_INTERVAL, c4d.BFH_LEFT, 80)
        self._label("Capture")
        self.AddComboBox(CB_SOURCE, c4d.BFH_SCALEFIT)
        self.AddChild(CB_SOURCE, 0, "Whole C4D window")
        self.AddChild(CB_SOURCE, 1, "Viewport only")
        self.GroupEnd()
        self.AddCheckbox(CK_FG, c4d.BFH_SCALEFIT, 0, 0, "Only while C4D is in front")
        self.AddCheckbox(CK_CHANGE, c4d.BFH_SCALEFIT, 0, 0, "Skip when nothing in the scene changes")

        self._section("Video")
        self._label("Width (px)")
        self.AddEditNumberArrows(E_WIDTH, c4d.BFH_LEFT, 80)
        self._label("Quality (lower = better)")
        self.AddEditNumberArrows(E_QUALITY, c4d.BFH_LEFT, 80)
        self._label("Playback fps")
        self.AddEditNumberArrows(E_FPS, c4d.BFH_LEFT, 80)
        self._label("Encoder")
        self.AddStaticText(T_ENCODER, c4d.BFH_SCALEFIT, 0, 0, "-")
        self.GroupEnd()

        self._section("Save")
        self._label("Save to")
        self.AddComboBox(CB_OUTMODE, c4d.BFH_SCALEFIT)
        self.AddChild(CB_OUTMODE, 0, "Folder next to the scene")
        self.AddChild(CB_OUTMODE, 1, "Custom folder")
        self._label("Custom folder")
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddEditText(E_OUTPATH, c4d.BFH_SCALEFIT)
        self.AddButton(B_BROWSE, c4d.BFH_RIGHT, 30, 0, "...")
        self.GroupEnd()
        self.GroupEnd()
        self.AddCheckbox(CK_REMUX, c4d.BFH_SCALEFIT, 0, 0, "Convert to .mp4 when stopped")
        self.AddCheckbox(CK_AUTO, c4d.BFH_SCALEFIT, 0, 0, "Resume when the project reopens")

        self.GroupEnd()
        return True

    def InitValues(self):
        s = MANAGER.cfg.settings
        self.SetFloat(E_INTERVAL, float(s["interval"]), 0.5, 600.0, 0.5)
        self.SetInt32(CB_SOURCE, 0 if s["source"] == "window" else 1)
        self.SetBool(CK_FG, s["require_foreground"])
        self.SetBool(CK_CHANGE, s["require_change"])
        self.SetInt32(E_WIDTH, int(s["width"]), 320, 3840, 16)
        self.SetInt32(E_QUALITY, int(s["quality"]), 1, 51, 1)
        self.SetInt32(E_FPS, int(s["fps_out"]), 1, 120, 1)
        self.SetString(T_ENCODER, s.get("encoder") or "picked on first recording")
        self.SetInt32(CB_OUTMODE, 0 if s.get("output_mode") == "scene" else 1)
        self.SetString(E_OUTPATH, s.get("output_root") or "")
        self.SetBool(CK_REMUX, s["remux_mp4"])
        self.SetBool(CK_AUTO, s["autostart"])
        self._refresh()
        self.SetTimer(500)
        return True

    def _refresh(self):
        doc = c4d.documents.GetActiveDocument()
        rec = MANAGER.is_recording(doc)
        self.SetString(B_RECORD, "Stop recording" if rec else "Record this project")
        status = MANAGER.status_line()
        if MANAGER.merge_status:
            status += "      " + MANAGER.merge_status
        self.SetString(T_STATUS, status)
        try:
            self.SetString(T_WHERE, "Saves to  " + output_folder(MANAGER.cfg, doc))
        except Exception:
            pass
        for gid in LOCKED_WHILE_RECORDING:
            self.Enable(gid, not rec)
        if not rec:
            custom = self.GetInt32(CB_OUTMODE) == 1
            self.Enable(E_OUTPATH, custom)
            self.Enable(B_BROWSE, custom)

    def Timer(self, msg):
        self._refresh()

    def _pull(self):
        s = MANAGER.cfg.settings
        s["interval"] = max(0.5, float(self.GetFloat(E_INTERVAL)))
        s["source"] = "window" if self.GetInt32(CB_SOURCE) == 0 else "viewport"
        s["require_foreground"] = bool(self.GetBool(CK_FG))
        s["require_change"] = bool(self.GetBool(CK_CHANGE))
        s["width"] = even(max(320, self.GetInt32(E_WIDTH)))
        s["quality"] = int(self.GetInt32(E_QUALITY))
        s["fps_out"] = int(self.GetInt32(E_FPS))
        s["output_mode"] = "scene" if self.GetInt32(CB_OUTMODE) == 0 else "custom"
        s["output_root"] = (self.GetString(E_OUTPATH) or "").strip()
        s["remux_mp4"] = bool(self.GetBool(CK_REMUX))
        s["autostart"] = bool(self.GetBool(CK_AUTO))
        MANAGER.cfg.save()
        # interval and the two gates are read live by the running session
        if MANAGER.rec is not None:
            MANAGER.rec.interval = s["interval"]

    def Command(self, cid, msg):
        doc = c4d.documents.GetActiveDocument()
        if cid == B_RECORD:
            self._pull()
            MANAGER.merge_status = ""
            MANAGER.toggle(doc)
            c4d.EventAdd()                      # refresh the command icon's pressed state
        elif cid == M_FOLDER:
            folder = output_folder(MANAGER.cfg, doc)
            if not os.path.isdir(folder):
                folder = MANAGER.cfg.output_root()
            try:
                os.startfile(folder)
            except Exception as exc:
                log("open folder failed:", exc)
        elif cid == M_LAST:
            clip = last_recording(MANAGER.cfg, doc)
            if clip:
                os.startfile(clip)
            else:
                c4d.StatusSetText("Timelapse: nothing recorded for this project yet")
        elif cid == M_MERGE:
            MANAGER.merge(doc)
        elif cid == M_HELP:
            gui.MessageDialog(HELP)
        elif cid == B_BROWSE:
            picked = storage.LoadDialog(
                c4d.FILESELECTTYPE_ANYTHING, "Timelapse output folder",
                c4d.FILESELECT_DIRECTORY, "",
                self.GetString(E_OUTPATH) or "")
            if picked:
                self.SetString(E_OUTPATH, picked)
                self.SetInt32(CB_OUTMODE, 1)
                self._pull()
        else:
            self._pull()
        self._refresh()
        return True

    def AskClose(self):
        self._pull()
        return False


PANEL = Panel()


# ==========================================================================
# Command -- opens the panel; its icon shows pressed while recording
# ==========================================================================

class PanelCommand(plugins.CommandData):
    def Execute(self, doc):
        return PANEL.Open(c4d.DLG_TYPE_ASYNC, ID_PANEL, defaultw=380, defaulth=0)

    def RestoreLayout(self, secret):
        return PANEL.Restore(ID_PANEL, secret)

    def GetState(self, doc):
        state = c4d.CMD_ENABLED
        if MANAGER.is_recording(doc):
            state |= c4d.CMD_VALUE
        return state


# ==========================================================================
# Message plugin -- the scheduler
# ==========================================================================

class TimelapseMessage(plugins.MessageData):
    def GetTimer(self):
        return TICK_MS

    def CoreMessage(self, mid, bc):
        if mid == c4d.MSG_TIMER:
            try:
                was = MANAGER.rec is not None
                MANAGER.tick()
                if (MANAGER.rec is not None) != was:
                    c4d.EventAdd()              # auto-start/stop: update the icon
            except Exception as exc:
                if not MANAGER.errored:
                    MANAGER.errored = True
                    log("tick error (further errors suppressed):", exc)
                    import traceback
                    traceback.print_exc()
        elif mid == c4d.C4DPL_ENDPROGRAM:
            try:
                MANAGER.shutdown()
            except Exception:
                pass
        return True


# ==========================================================================
# Registration
# ==========================================================================

def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterMessagePlugin(ID_MESSAGE, "Timelapse Scheduler", 0,
                                  TimelapseMessage())
    plugins.RegisterCommandPlugin(
        ID_PANEL, "Timelapse", 0, _icon(),
        "Record a timelapse of your work on this project", PanelCommand())
