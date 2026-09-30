"""Tune - finds faster Redshift render settings that look the same as the ones you have.

How a run works (one button):
    1. Reference   the current frame at half size with a 4x stricter sampling threshold and no denoiser -
                   what your settings are trying to reach, nearly noise-free.
    2. Current     your settings, same size: how far they are from the reference (FLIP, 0..1) and how long they take.
    3. Sampling    the threshold is loosened step by step, with the denoiser off, OptiX and OIDN; each family stops
                   at the first step that looks worse than your current settings (times the quality level).
    4. Depths      from the fastest passing sampling setting, each trace depth is lowered until the picture changes.
    5. Frames      the pick is checked on the start, middle and end of the render setting's frame range (the
                   sweep only saw one); if it looks worse on one, the next-best setting is tried.
    6. Flicker     the next frame is rendered too: the pick's grain must not change frame to frame (boil) more
                   than yours does.
    7. Check       the winner and your current settings render full-size crops of the noisiest spots and are
                   compared again, so half size can't hide anything.
The ★ recommendation: of every setting that passed, the fastest - or, among those within ~8% of it (render
times wobble that much), the one closest to the reference.
Results: a list of every passing step with its time, an A/B compare (current | candidate, drag the split),
the error map, and Apply = a new Render Setting next to yours (yours is never changed).

Every render runs in a hidden Commandline.exe (worker/tune_worker.pyp), on a packaged copy of the scene: C4D stays
usable, and if Redshift crashes only the worker dies - Tune restarts it and retries. Stop is instant. The picture comparison runs in the system Python (service/tune_metric.py).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback

import c4d
from c4d import bitmaps, gui

HERE = os.path.dirname(os.path.abspath(__file__))
METRIC = os.path.join(HERE, "service", "tune_metric.py")
RS_VP = 1036219

# --- Redshift render-setting ids (read from the 2026 install)
AUTO_SAMPLING, THRESHOLD, MIN_SAMPLES, MAX_SAMPLES = 1107, 1103, 1101, 1102
DENOISE, DENOISE_ENGINE = 2006, 2007
ENGINE_OPTIX, ENGINE_OIDN = 3, 4
DEPTH_COMBINED, DEPTH_REFL, DEPTH_REFR, DEPTH_TRANSP = 5003, 5001, 5002, 5004
GI_ENABLED, GI_DEPTH = 7005, 7006
NAMES = {THRESHOLD: "Threshold", MAX_SAMPLES: "Samples Max", DENOISE: "Denoising", DENOISE_ENGINE: "Denoiser",
         DEPTH_COMBINED: "Combined depth", DEPTH_REFL: "Reflection depth", DEPTH_REFR: "Refraction depth",
         DEPTH_TRANSP: "Transparency depth", GI_DEPTH: "GI depth"}
SHORT = {DEPTH_COMBINED: "Combined", DEPTH_REFL: "Refl", DEPTH_REFR: "Refr", DEPTH_TRANSP: "Transp", GI_DEPTH: "GI"}
ENGINE_NAMES = {1: "Altus Single", 2: "Altus Dual", 3: "OptiX", 4: "OIDN"}

# --- quality levels: a candidate passes when its error is at most current x factor (+ a hair for rounding)
LEVELS = [("Same as now", 1.05), ("A little lower", 1.5), ("Draft", 3.0)]
EPS = 0.0005
MIN_GAIN = 0.92                            # render times wobble ~10% run to run: only a real gain counts
THRESH_STEPS = [1.5, 2, 3, 4, 6, 8, 12]
DEPTH_STEPS = [16, 12, 10, 8, 6, 5, 4, 3, 2, 1]
SWEEP_LONG_SIDE = 1280                     # the sweep renders the whole frame with its long side at most this
CROP, CROPS = 256, 3                       # the final check: this many full-size crops of this size

# --- gadget ids
CB_RD, CB_LEVEL, BT_RUN, TX_STATUS, UA_VIEW, BT_APPLY, BT_FULL, CK_ERR = 1000, 1001, 1002, 1003, 1004, 1005, 1006, 1007
M_HOWTO, M_TEMP = 2000, 2001

LOG = []


def log_error(where):
    msg = "[Tune] error in %s:\n%s" % (where, traceback.format_exc())
    print(msg)
    LOG.append(msg)


def log(*a):
    print("[Tune]", *a)


# ================================================================ metric worker
def _find_python():
    for cand in (os.environ.get("TUNE_PYTHON", ""), shutil.which("python") or "", shutil.which("py") or ""):
        if cand and os.path.exists(cand) and "windowsapps" not in cand.lower():
            return cand
    return ""


class Metric:
    """The system-Python picture judge, kept running for the whole job."""

    def __init__(self):
        py = _find_python()
        if not py:
            raise RuntimeError("Tune needs Python 3 with numpy, scipy and pillow on PATH "
                               "(pip install numpy scipy pillow), or TUNE_PYTHON set to its python.exe")
        self.p = subprocess.Popen([py, "-u", METRIC], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, bufsize=1,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def ask(self, **cmd):
        self.p.stdin.write(json.dumps(cmd) + "\n")
        self.p.stdin.flush()
        line = self.p.stdout.readline()
        if not line:
            raise RuntimeError("the picture judge stopped: " + (self.p.stderr.read() or "")[-800:])
        res = json.loads(line)
        if not res.get("ok"):
            raise RuntimeError(res.get("trace") or res.get("error"))
        return res

    def close(self):
        try:
            self.p.stdin.write('{"cmd": "quit"}\n')
            self.p.stdin.flush()
            self.p.wait(2)
        except Exception:
            try:
                self.p.kill()
            except Exception:
                pass


# ================================================================ render settings helpers
def render_datas(doc):
    out = []

    def walk(rd, depth):
        while rd:
            out.append((rd, depth))
            walk(rd.GetDown(), depth + 1)
            rd = rd.GetNext()
    walk(doc.GetFirstRenderData(), 0)
    return out


def rd_path(doc, target):
    """Index path of a render data, to find the same one in a clone of the document."""
    for i, (rd, _) in enumerate(render_datas(doc)):
        if rd == target:
            return i
    return -1


def rs_videopost(rd):
    vp = rd.GetFirstVideoPost()
    while vp and vp.GetType() != RS_VP:
        vp = vp.GetNext()
    return vp


def read_settings(vp):
    keys = [AUTO_SAMPLING, THRESHOLD, MIN_SAMPLES, MAX_SAMPLES, DENOISE, DENOISE_ENGINE, DEPTH_COMBINED,
            DEPTH_REFL, DEPTH_REFR, DEPTH_TRANSP, GI_ENABLED, GI_DEPTH]
    return {k: vp[k] for k in keys}


def describe(over, base):
    """Human words for what a candidate changes."""
    parts = []
    if DENOISE in over:
        parts.append("Denoise " + (ENGINE_NAMES.get(over.get(DENOISE_ENGINE, base[DENOISE_ENGINE]), "?")
                                   if over[DENOISE] else "off"))
    if THRESHOLD in over:
        parts.append("Threshold %s" % _num(over[THRESHOLD]))
    for k in (GI_DEPTH, DEPTH_REFL, DEPTH_REFR, DEPTH_TRANSP, DEPTH_COMBINED):
        if k in over and over[k] != base[k]:
            parts.append("%s %d" % (SHORT[k], over[k]))
    return " · ".join(parts) or "Your current settings"


def _num(v):
    return ("%.4f" % v).rstrip("0").rstrip(".")


def pick_best(results, current):
    """The recommendation: of every setting that passed, the fastest - unless another one is within the timing
    wobble (~8%) of it and looks closer to the reference, then that one. Current wins if nothing is really faster."""
    ok = [r for r in results if r["ok"] and r is not current]
    if not ok:
        return current
    fastest = min(r["secs"] for r in ok)
    if fastest > current["secs"] * MIN_GAIN:
        return current
    close = [r for r in ok if r["secs"] <= fastest / MIN_GAIN]
    return min(close, key=lambda r: (round(r["mean"], 4), r["secs"]))


def ranked(results, current):
    """Passing sweep results in recommendation order (pick_best, then the next pick_best of the rest, ...)."""
    pool = [r for r in results if r["ok"] and r is not current and r["kind"] in ("sampling", "depth")]
    out = []
    while pool:
        b = pick_best(pool, current)
        if b is current:
            break
        out.append(b)
        pool.remove(b)
    return out


def frame_range(doc, rd):
    """(first, last) frame the render setting renders."""
    fps = doc.GetFps()
    fs = rd[c4d.RDATA_FRAMESEQUENCE]
    if fs == c4d.RDATA_FRAMESEQUENCE_CURRENTFRAME:
        f = doc.GetTime().GetFrame(fps)
        return f, f
    if fs == c4d.RDATA_FRAMESEQUENCE_ALLFRAMES:
        return doc.GetMinTime().GetFrame(fps), doc.GetMaxTime().GetFrame(fps)
    if fs == c4d.RDATA_FRAMESEQUENCE_PREVIEWRANGE:
        return doc.GetLoopMinTime().GetFrame(fps), doc.GetLoopMaxTime().GetFrame(fps)
    return rd[c4d.RDATA_FRAMEFROM].GetFrame(fps), rd[c4d.RDATA_FRAMETO].GetFrame(fps)


def why(j, b):
    """One line on why the recommendation was picked."""
    if b is j.current:
        return "Nothing faster passes at this quality - your settings are already lean."
    return ("★ %s  -  %d%% of the time, error %.3f (yours %.3f)"
            % (b["name"], int(round(j.speedup(b) * 100)), b["mean"], j.current["mean"]))


# ================================================================ the render worker (a separate Commandline.exe)
COMMANDLINE = os.path.join(os.path.dirname(sys.executable), "Commandline.exe")
WORKER_DIR = os.path.join(HERE, "worker")
_HOLD = sys.__dict__.setdefault("_tune_worker_holder", {})     # survives reloads of this module


class WorkerClient:
    """Hidden Commandline.exe that does every render. If Redshift crashes, only it dies - never your C4D.
    Talks JSON lines over a local socket; never blocks: poll() collects whatever has arrived."""

    def __init__(self):
        if not os.path.exists(COMMANDLINE):
            raise RuntimeError("Commandline.exe not found next to Cinema 4D.exe")
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.srv.setblocking(False)
        port = self.srv.getsockname()[1]
        env = dict(os.environ, g_additionalModulePath=WORKER_DIR)
        self.logpath = os.path.join(tempfile.gettempdir(), "Tune", "worker_log.txt")
        os.makedirs(os.path.dirname(self.logpath), exist_ok=True)
        self.proc = subprocess.Popen([COMMANDLINE, "-tune_worker", str(port)], env=env,
                                     stdout=open(self.logpath, "w"), stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.conn = None
        self.buf = b""
        self.t0 = time.time()
        self.scene = None          # the scene file it has loaded

    def alive(self):
        return self.proc.poll() is None

    def connected(self):
        return self.conn is not None

    def poll(self):
        """New messages (list of dicts), without waiting."""
        if self.conn is None:
            try:
                self.conn, _ = self.srv.accept()
                self.conn.setblocking(False)
            except (BlockingIOError, OSError):
                return []
        out = []
        while True:
            try:
                data = self.conn.recv(65536)
            except (BlockingIOError, InterruptedError):
                break
            except OSError:
                data = b""
            if not data:
                break
            self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            if line.strip():
                out.append(json.loads(line.decode("utf-8")))
        return out

    def send(self, **msg):
        self.conn.setblocking(True)
        try:
            self.conn.sendall((json.dumps(msg) + "\n").encode("utf-8"))
        finally:
            self.conn.setblocking(False)

    def why_dead(self):
        """Last lines of its log, for the Console."""
        try:
            with open(self.logpath, encoding="utf-8", errors="replace") as f:
                tail = f.read()[-1500:]
        except Exception:
            tail = ""
        if "Enter the license method" in tail:
            return ("Commandline.exe isn't licensed yet. Run it once in a terminal and pick 1 (Maxon App):\n"
                    '& "%s"' % COMMANDLINE)
        return tail

    def kill(self):
        for x in (self.conn, self.srv):
            try:
                x.close()
            except Exception:
                pass
        try:
            self.proc.kill()
        except Exception:
            pass


def worker():
    """The running worker, or None."""
    w = _HOLD.get("w")
    return w if w and w.alive() else None


def new_worker():
    old = _HOLD.get("w")
    if old:
        old.kill()
    _HOLD["w"] = WorkerClient()
    return _HOLD["w"]


def kill_worker():
    w = _HOLD.pop("w", None)
    if w:
        w.kill()


class Req:
    """A render the job asked for, sent to the worker."""

    def __init__(self, name, kind, W, H, region, over, frame):
        self.name, self.kind, self.W, self.H, self.region, self.over, self.frame = name, kind, W, H, region, over, frame
        self.p = 0.0
        self.t_sent = None
        self.tries = 0

    def elapsed(self):
        return time.time() - self.t_sent if self.t_sent else 0.0


PACKAGE_FLAGS = (c4d.SAVEPROJECT_ASSETS | c4d.SAVEPROJECT_SCENEFILE | c4d.SAVEPROJECT_USEDOCUMENTNAMEASFILENAME |
                 c4d.SAVEPROJECT_DONTFAILONMISSINGASSETS | c4d.SAVEPROJECT_ASSETLINKS_COPY_FILEASSETS |
                 c4d.SAVEPROJECT_ASSETLINKS_COPY_NODEASSETS)


def save_scene_copy(doc):
    """The scene as it is right now (unsaved changes too), packaged with every texture it uses into Tune's temp
    folder - C4D's own Save Project with Assets. The worker runs with its own preferences, so it can't see your
    texture search paths or the Asset Browser: a textures-next-to-the-scene package is the only copy it renders
    exactly like your C4D does (tested: identical pixels). Nothing is written to your project folder."""
    folder = os.path.join(tempfile.gettempdir(), "Tune", "scene")
    if os.path.isdir(folder):
        shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder, exist_ok=True)
    clone = doc.GetClone(c4d.COPYFLAGS_NONE)
    name = doc.GetDocumentName() or "scene.c4d"
    clone.SetDocumentName(name)
    clone.SetDocumentPath(doc.GetDocumentPath())       # so relative texture paths resolve while packaging
    lost = []
    if not c4d.documents.SaveProject(clone, PACKAGE_FLAGS, folder, [], lost):
        raise RuntimeError("couldn't package the scene for the render worker")
    for l in lost:
        log("missing texture (renders without it, in your C4D too):", l)
    path = os.path.join(folder, name if name.lower().endswith(".c4d") else name + ".c4d")
    if not os.path.exists(path):
        raise RuntimeError("the packaged scene isn't where expected: " + path)
    return path


# ================================================================ the job
class Job:
    """State of one run. run() is a generator: it yields a Req for each render and gets back
    (seconds, raw float32 path, width, height). tick() - on the panel timer - drives it without ever blocking."""

    def __init__(self, doc, rd, level):
        self.doc, self.src_rd, self.level = doc, rd, level
        self.factor = LEVELS[level][1]
        self.fps = doc.GetFps()
        a, b = frame_range(doc, rd)
        self.range = (a, b)
        self.frames = sorted({a, (a + b) // 2, b})             # start, middle, end of what this setting renders
        now = doc.GetTime().GetFrame(self.fps)
        self.primary = min(self.frames, key=lambda f: abs(f - now))   # the full sweep runs on this one
        self.frame = c4d.BaseTime(self.primary, self.fps)
        self.notes = []           # plain-words log of the multi-frame / flicker checks
        self.dir = os.path.join(tempfile.gettempdir(), "Tune", time.strftime("%Y%m%d_%H%M%S"))
        os.makedirs(self.dir, exist_ok=True)
        self.rd_index = rd_path(doc, rd)                      # the same render setting in the worker's copy
        self.base = read_settings(rs_videopost(rd))
        self.W, self.H = int(rd[c4d.RDATA_XRES]), int(rd[c4d.RDATA_YRES])
        self.scene = save_scene_copy(doc)                     # what the worker renders (unsaved changes included)
        self.wk = None
        self.boot_state = None
        s = min(0.5, SWEEP_LONG_SIDE / float(max(self.W, self.H)))
        self.sw, self.sh = max(16, int(round(self.W * s))), max(16, int(round(self.H * s)))
        # Basic colour management: the render arrives already tone-mapped (Redshift post-effects) -> plain sRGB,
        # like the Picture Viewer. OCIO: scene-linear -> auto (filmic stand-in unless nothing is above 1)
        self.view = "srgb" if doc[c4d.DOCUMENT_COLOR_MANAGEMENT] != c4d.DOCUMENT_COLOR_MANAGEMENT_OCIO else "auto"
        self.metric = Metric()
        self.results = []         # dicts: name, over, secs, mean, p99, ok, png prefix, kind
        self.current = None
        self.best = None
        self.crops = []           # [(x, y)] full-size
        self.check = None         # final full-size crop check {cur: [...], best: [...]}
        self.full = None          # (current, best) seconds of a full frame, from "Time full frames"
        self.status = "Starting"
        self.done = self.failed = self.stopping = False
        self.error = ""
        self.t0 = time.time()
        self.n = 0
        self.req = None
        self.times = {}           # kind -> [seconds], for the progress estimate
        self.planned = 2          # renders this run may still need, upper bound (grows as phases are planned)
        self.gen = self.run()
        self.send = None

    # ------------------------------------------------------------ rendering (in the worker)
    def render(self, over, name, w=None, h=None, region=None, kind="sweep", frame=None):
        """A render request: base settings + over, full frame at w x h or a full-size region (x, y, s),
        at frame (default: the sweep frame)."""
        if region:
            kind = "crop"
            W, H = self.W, self.H
        else:
            W, H = w, h
        return Req(name, kind, W, H, list(region) if region else None, over, self.primary if frame is None else frame)

    def _send(self, req):
        req.t_sent = time.time()
        req.p = 0.0
        self.wk.send(cmd="render", rd=self.rd_index, keys=list(self.base.keys()),
                     over={str(k): v for k, v in req.over.items()}, frame=req.frame, w=req.W, h=req.H,
                     region=req.region, out=os.path.join(self.dir, req.name + ".f32"))

    def _boot(self):
        """Get a worker with this run's scene loaded, one step per tick. True when ready to render."""
        w = self.wk if (self.wk and self.wk.alive()) else worker()
        if w is None:
            if self.boot_state == "starting":
                raise RuntimeError("the render worker quit while starting:\n" + (self.wk.why_dead() if self.wk else ""))
            self.wk = new_worker()
            self.boot_state = "starting"
            self.status = "Starting the render worker (loads Redshift, about a minute)"
            return False
        self.wk = w
        if self.boot_state in (None, "starting"):
            if not w.connected() or self.boot_state == "starting":
                if not any(m.get("hello") for m in w.poll()):
                    if time.time() - w.t0 > 180:
                        raise RuntimeError("the render worker didn't start:\n" + w.why_dead())
                    return False
            if w.scene == self.scene:
                self.boot_state = "ready"
                return True
            w.send(cmd="load", path=self.scene)
            self.boot_state = "loading"
            self.status = "Loading the scene in the render worker"
            return False
        if self.boot_state == "loading":
            for m in w.poll():
                if "ok" in m:
                    if not m["ok"]:
                        raise RuntimeError("the render worker couldn't load the scene: " + m.get("error", ""))
                    w.scene = self.scene
                    self.boot_state = "ready"
            return self.boot_state == "ready"
        return True

    def tick(self):
        """Called by the panel timer. Never waits: boots the worker, collects the running render, or sends the next."""
        if self.done or self.failed:
            return
        if self.boot_state != "ready":
            if not self._boot():
                return
            if self.req:                                       # a render was interrupted by a worker crash: resend
                self._send(self.req)
                return
        if self.req:
            w = self.wk
            if not w.alive():
                self.req.tries += 1
                if self.req.tries > 2:
                    raise RuntimeError("Redshift kept crashing on '%s' (worker log: %s)" % (self.req.name, w.logpath))
                self.notes.append("Redshift crashed in the worker - restarted it, retrying")
                log("worker died on", self.req.name, "- restarting:\n", w.why_dead()[-600:])
                self.boot_state = None
                self.wk = None
                kill_worker()
                return
            done = None
            for m in w.poll():
                if "progress" in m:
                    self.req.p = m["progress"]
                elif "ok" in m:
                    done = m
            if done is None:
                return
            if not done["ok"]:
                raise RuntimeError(done.get("error") or "render failed")
            w.count = getattr(w, "count", 0) + 1
            if w.count == 1 and self.req.kind != "ref":
                self._send(self.req)                           # warm-up render: its time includes loading, redo it
                return
            secs = done["secs"]
            self.n += 1
            self.times.setdefault(self.req.kind, []).append(secs)
            self.send = (secs, os.path.join(self.dir, self.req.name + ".f32"), done["w"], done["h"])
            self.req = None
        try:
            nxt = self.gen.send(self.send)
        except StopIteration:
            self.done = True
            if getattr(self, "t_end", None) is None:            # the run itself; a later full-frame timing doesn't count
                self.t_end, self.n_run = time.time(), self.n
            self.close()
            return
        self.send = None
        self.req = nxt
        self._send(nxt)

    def stop(self):
        """Stop now: a render in flight is killed with the worker (the next run starts a fresh one)."""
        if self.req:
            kill_worker()
            self.req = None
            self.boot_state = None
        if self.current and self.status.startswith("Full frame"):
            self.done = True                                   # stopping a full-frame timing keeps the results
        else:
            self.failed, self.error = True, "Stopped."
        self.close()

    # ------------------------------------------------------------ progress
    def _expect(self, kind):
        t = self.times.get(kind) or self.times.get("sweep")
        if t:
            return sum(t) / len(t)
        return 90.0 if kind == "ref" else 25.0

    def progress(self):
        """(fraction of this run, fraction of the current render, seconds left at most)."""
        cur_frac, cur_left = 0.0, 0.0
        if self.req and self.req.t_sent:
            exp = self._expect(self.req.kind)
            el = self.req.elapsed()
            cur_frac = min(0.97, max(self.req.p, el / exp if exp else 0))
            cur_left = max(0.0, exp - el)
        total = max(self.planned, self.n + 1)
        frac = min(1.0, (self.n + cur_frac) / float(total))
        left = cur_left + max(0, total - self.n - 1) * self._expect("sweep")
        return frac, cur_frac, left

    def skip(self, n):
        self.planned -= n

    def compare(self, ref, test, w, h, out):
        return self.metric.ask(cmd="flip", ref=ref, test=test, w=w, h=h, out=os.path.join(self.dir, out), view=self.view)

    def passes(self, r, cur):
        k = self.factor
        return r["mean"] <= cur["mean"] * k + EPS and r["p99"] <= cur["p99"] * k + EPS

    # ------------------------------------------------------------ the steps
    def run(self):
        b = self.base
        sw, sh = self.sw, self.sh
        if not b[AUTO_SAMPLING]:
            log("Automatic Sampling is off: only the threshold, denoiser and depths are tuned")

        families = [("off", {DENOISE: 0}), ("OptiX", {DENOISE: 1, DENOISE_ENGINE: ENGINE_OPTIX}),
                    ("OIDN", {DENOISE: 1, DENOISE_ENGINE: ENGINE_OIDN})]
        base_fam = "off" if not b[DENOISE] else {ENGINE_OPTIX: "OptiX", ENGINE_OIDN: "OIDN"}.get(b[DENOISE_ENGINE])
        fam_steps = [(fam, fover, THRESH_STEPS if fam == base_fam else [1.0] + THRESH_STEPS) for fam, fover in families]
        order = ([GI_DEPTH] if b[GI_ENABLED] else []) + [DEPTH_REFL, DEPTH_REFR, DEPTH_TRANSP, DEPTH_COMBINED]
        depth_steps = [(k, [d for d in DEPTH_STEPS if d < b[k]]) for k in order]
        self.planned = (2 + sum(len(s) for _, _, s in fam_steps) + sum(len(s) for _, s in depth_steps)
                        + 4 + 3 * CROPS + 3 * (len(self.frames) - 1) + 3)

        self.status = "Reference (clean, takes longest)"
        ref_over = {THRESHOLD: b[THRESHOLD] / 4.0, DENOISE: 0}
        if not b[AUTO_SAMPLING]:
            ref_over[MAX_SAMPLES] = b[MAX_SAMPLES] * 4
        _, self.ref_path, _, _ = yield self.render(ref_over, "ref", sw, sh, kind="ref")

        self.status = "Your current settings"
        secs, p, _, _ = yield self.render({}, "cur", sw, sh)
        m = self.compare(self.ref_path, p, sw, sh, "cur")
        self.current = dict(name="Your current settings", over={}, secs=secs, mean=m["mean"], p99=m["p99"], ok=True,
                            png=os.path.join(self.dir, "cur"), kind="current")
        self.results.append(self.current)
        self.best = self.current

        # --- sampling: threshold up, per denoiser family, stop at the first failure
        best_sampling = self.current
        for fam, fover, steps in fam_steps:
            mine = fam == base_fam                         # your own denoiser setup: 1x is just "current"
            for i, mult in enumerate(steps):
                over = {} if mine else dict(fover)
                over[THRESHOLD] = b[THRESHOLD] * mult
                if mult == 1.0:
                    over.pop(THRESHOLD)
                self.status = "Sampling: " + describe(over, b)
                r = yield from self.try_candidate(over, "s_%s_%g" % (fam, mult), "sampling")
                if not r["ok"]:
                    self.skip(len(steps) - i - 1)
                    break
                if r["secs"] < best_sampling["secs"] * MIN_GAIN:
                    best_sampling = r
        self.best = best_sampling

        # --- trace depths: lower each until the picture changes
        cur_over = dict(self.best["over"])
        for key, steps in depth_steps:
            for i, v in enumerate(steps):
                over = dict(cur_over)
                over[key] = v
                self.status = "Depths: %s %d" % (NAMES[key], v)
                r = yield from self.try_candidate(over, "d_%d_%d" % (key, v), "depth")
                if not r["ok"]:
                    self.skip(len(steps) - i - 1)
                    break
                cur_over = over
                self.best = r                                       # lower depth that looks the same: keep it
        self.best = pick_best(self.results, self.current)
        yield from self.check_frames(ref_over)
        yield from self.check_flicker(ref_over)
        if self.best is not self.current:
            # times wobble: time current and best again, alternating, and keep the best only if it's really faster
            cur_t, best_t = [self.current["secs"]], [self.best["secs"]]
            for i in range(2):
                self.status = "Timing again: current (%d/2)" % (i + 1)
                cur_t.append((yield self.render({}, "t_cur%d" % i, sw, sh))[0])
                self.status = "Timing again: best (%d/2)" % (i + 1)
                best_t.append((yield self.render(self.best["over"], "t_best%d" % i, sw, sh))[0])
            self.current["secs"] = sorted(cur_t)[1]                 # median of 3
            self.best["secs"] = sorted(best_t)[1]
            if self.best["secs"] > self.current["secs"] * MIN_GAIN:
                self.best = self.current
        else:
            self.skip(4)

        # --- final check at full size on the noisiest spots
        self.status = "Finding the noisiest spots"
        self.crops = self.pick_crops(self.results_noisiest())
        self.skip(3 * (CROPS - len(self.crops)) + (len(self.crops) if self.best is self.current else 0))
        self.check = {"cur": [], "best": [], "ok": True}
        for i, (x, y) in enumerate(self.crops):
            self.status = "Full-size check %d/%d: reference" % (i + 1, len(self.crops))
            _, rp, cw, ch = yield self.render(ref_over, "cref%d" % i, region=(x, y, CROP))
            self.status = "Full-size check %d/%d: current" % (i + 1, len(self.crops))
            _, cp, _, _ = yield self.render({}, "ccur%d" % i, region=(x, y, CROP))
            mc = self.compare(rp, cp, cw, ch, "ccur%d" % i)
            if self.best is self.current:
                mb = mc
            else:
                self.status = "Full-size check %d/%d: best" % (i + 1, len(self.crops))
                _, bp, _, _ = yield self.render(self.best["over"], "cbest%d" % i, region=(x, y, CROP))
                mb = self.compare(rp, bp, cw, ch, "cbest%d" % i)
            self.check["cur"].append(mc)
            self.check["best"].append(mb)
            if not self.passes(mb, mc):
                self.check["ok"] = False
        self.status = "Done"

    def _candidates(self, failed):
        return [r for r in ranked(self.results, self.current) if r is not self.best and r not in failed]

    def check_frames(self, ref_over):
        """The sweep saw one frame. Check the pick on the other test frames; on a failure try the next-best (max 4)."""
        others = [f for f in self.frames if f != self.primary]
        if not others or self.best is self.current:
            self.skip(3 * len(others))
            return
        sw, sh = self.sw, self.sh
        refs, failed = {}, []
        cand, tries = self.best, 0
        while cand is not self.current:
            tries += 1
            bad = None
            for f in others:
                if f not in refs:
                    self.status = "Frame %d: reference" % f
                    _, rp, w, h = yield self.render(ref_over, "f%d_ref" % f, sw, sh, kind="ref", frame=f)
                    self.status = "Frame %d: your settings" % f
                    cs, cp, _, _ = yield self.render({}, "f%d_cur" % f, sw, sh, frame=f)
                    refs[f] = (rp, self.compare(rp, cp, w, h, "f%d_cur" % f), cs)
                rp, mc, cs = refs[f]
                tag = "f%d_c%d" % (f, tries)
                self.status = "Frame %d: checking %s" % (f, cand["name"])
                bs, bp, w, h = yield self.render(cand["over"], tag, sw, sh, frame=f)
                mb = self.compare(rp, bp, w, h, tag)
                row = dict(name="Frame %d: %s" % (f, cand["name"]), over=cand["over"], secs=bs, base_secs=cs,
                           mean=mb["mean"], p99=mb["p99"], png=os.path.join(self.dir, tag),
                           cur_png=os.path.join(self.dir, "f%d_cur" % f), kind="frame", frame=f)
                row["ok"] = self.passes(mb, mc)
                self.results.append(row)
                if not row["ok"]:
                    bad = f
                    break
            if bad is None:
                self.notes.append("frames %s ok" % ", ".join(str(f) for f in self.frames))
                break
            self.notes.append("%s looked worse on frame %d" % (cand["name"], bad))
            failed.append(cand)
            nxt = self._candidates(failed)
            cand = nxt[0] if nxt and tries < 4 else self.current
            self.planned += len(others)
        self.best = cand

    def check_flicker(self, ref_over):
        """Grain that changes every frame boils in motion. Render the next frame too and compare how much the pick
        changes frame to frame (beyond what the clean reference changes) with how much your settings do."""
        a, b = self.range
        f1 = self.primary + 1 if self.primary + 1 <= b else self.primary - 1
        if self.best is self.current or a == b or f1 < a:
            self.skip(3)
            return
        sw, sh = self.sw, self.sh
        self.status = "Flicker: reference, frame %d" % f1
        _, r1, _, _ = yield self.render(ref_over, "fl_ref", sw, sh, kind="ref", frame=f1)
        self.status = "Flicker: your settings, frame %d" % f1
        _, c1, _, _ = yield self.render({}, "fl_cur", sw, sh, frame=f1)
        cur_fl = self.metric.ask(cmd="flicker", r0=self.ref_path, r1=r1, a0=self.current["png"] + ".f32", a1=c1,
                                 w=sw, h=sh, view=self.view)["flicker"]
        self.compare(r1, c1, sw, sh, "fl_cur")                    # pictures for the A/B view
        failed, tries = [], 0
        cand = self.best
        while cand is not self.current:
            tries += 1
            tag = "fl_c%d" % tries
            self.status = "Flicker: checking %s" % cand["name"]
            _, p1, _, _ = yield self.render(cand["over"], tag, sw, sh, frame=f1)
            fl = self.metric.ask(cmd="flicker", r0=self.ref_path, r1=r1, a0=cand["png"] + ".f32", a1=p1,
                                 w=sw, h=sh, view=self.view)["flicker"]
            self.compare(r1, p1, sw, sh, tag)
            ok = fl <= cur_fl * self.factor + EPS
            row = dict(name="Flicker: %s" % cand["name"], over=cand["over"], secs=cand["secs"], mean=fl, p99=fl,
                       png=os.path.join(self.dir, tag), cur_png=os.path.join(self.dir, "fl_cur"), kind="flicker",
                       ok=ok, frame=f1)
            self.results.append(row)
            if ok:
                self.notes.append("flicker ok (%.3f vs yours %.3f)" % (fl, cur_fl))
                break
            self.notes.append("%s flickers more (%.3f vs yours %.3f)" % (cand["name"], fl, cur_fl))
            failed.append(cand)
            # something steadier: a tighter threshold, or a different denoiser choice
            nxt = [r for r in self._candidates(failed) if r["over"].get(DENOISE) != cand["over"].get(DENOISE)
                   or r["over"].get(THRESHOLD, 0) < cand["over"].get(THRESHOLD, 0)]
            cand = nxt[0] if nxt and tries < 3 else self.current
            self.planned += 1
        self.best = cand

    def run_full(self):
        """Time one full frame with the current and the best settings."""
        self.planned, self.n, self.times = 2, 0, {}
        self.status = "Full frame: your current settings"
        a = (yield self.render({}, "full_cur", self.W, self.H, kind="full"))[0]
        self.status = "Full frame: best settings"
        b = (yield self.render(self.best["over"], "full_best", self.W, self.H, kind="full"))[0]
        self.full = (a, b)
        self.status = "Done"

    def try_candidate(self, over, tag, kind):
        secs, p, w, h = yield self.render(over, tag, self.sw, self.sh)
        m = self.compare(self.ref_path, p, w, h, tag)
        r = dict(name=describe(over, self.base), over=over, secs=secs, mean=m["mean"], p99=m["p99"],
                 png=os.path.join(self.dir, tag), kind=kind)
        r["ok"] = self.passes(r, self.current)
        self.results.append(r)
        return r

    def results_noisiest(self):
        """The loosest sampling render we made (most noise) - where it differs from the reference is where noise lives."""
        s = [r for r in self.results if r["kind"] == "sampling" and not r["over"].get(DENOISE)]
        return max(s, key=lambda r: r["over"].get(THRESHOLD, 0)) if s else self.current

    def pick_crops(self, noisy):
        t = max(8, int(round(CROP * self.sw / float(self.W))))
        res = self.metric.ask(cmd="pick", a=noisy["png"] + ".f32", b=self.ref_path, w=self.sw, h=self.sh, n=CROPS, tile=t,
                              view=self.view)
        sx, sy = self.W / float(self.sw), self.H / float(self.sh)
        out = []
        for x, y in res["tiles"]:
            fx = int(round((x + t / 2.0) * sx - CROP / 2.0))
            fy = int(round((y + t / 2.0) * sy - CROP / 2.0))
            out.append((max(0, min(self.W - CROP, fx)), max(0, min(self.H - CROP, fy))))
        return out

    # ------------------------------------------------------------ results
    def speedup(self, r):
        return r["secs"] / max(r.get("base_secs") or self.current["secs"], 1e-6)

    def close(self):
        """Ends the picture judge. The worker stays up (warm) for Time full frames and the next run."""
        try:
            self.metric.close()
        except Exception:
            pass

    def kill(self):
        """New run / panel closing: a render in flight dies with the worker; the scene copy is deleted."""
        if self.req:
            kill_worker()
            self.req = None
        self.close()

    def apply(self, doc):
        """A new Render Setting next to the source one with the winner's changes. The source is never touched."""
        src = self.src_rd
        if not src or not src.IsAlive():
            raise RuntimeError("the render setting this run started from is gone")
        new = src.GetClone(c4d.COPYFLAGS_NONE)
        pct = int(round((1.0 - self.speedup(self.best)) * 100))
        new.SetName("%s - Tune %d%% faster" % (src.GetName(), pct))
        vp = rs_videopost(new)
        for k, v in self.best["over"].items():
            vp[k] = v
        doc.StartUndo()
        doc.InsertRenderData(new, None, src)
        doc.AddUndo(c4d.UNDOTYPE_NEW, new)
        doc.EndUndo()                                   # added only: which setting is active stays your call
        c4d.EventAdd()
        return new.GetName()


# ================================================================ panel
def panel_layout(dlg):
    dlg.SetTitle("Tune")
    dlg.MenuFlushAll()
    dlg.MenuSubBegin("Help")
    dlg.MenuAddString(M_HOWTO, "How Tune works")
    dlg.MenuAddString(M_TEMP, "Open test renders folder")
    dlg.MenuSubEnd()
    dlg.MenuFinished()
    if dlg.GroupBegin(0, c4d.BFH_SCALEFIT, 5, 1, "", 0):
        dlg.GroupBorderSpace(6, 6, 6, 2)
        dlg.AddStaticText(0, c4d.BFH_LEFT, name="Settings")
        dlg.AddComboBox(CB_RD, c4d.BFH_SCALEFIT, 120)
        dlg.AddStaticText(0, c4d.BFH_LEFT, name="  Quality")
        dlg.AddComboBox(CB_LEVEL, c4d.BFH_LEFT, 110)
        dlg.AddButton(BT_RUN, c4d.BFH_RIGHT, 70, name="Run")
    dlg.GroupEnd()
    if dlg.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 1, "", 0):
        dlg.GroupBorderSpace(8, 0, 8, 2)
        dlg.AddStaticText(TX_STATUS, c4d.BFH_SCALEFIT, name="Pick a render setting and press Run.")
    dlg.GroupEnd()
    dlg.AddUserArea(UA_VIEW, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 300, 300)
    dlg.AttachUserArea(dlg.view, UA_VIEW)
    if dlg.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 1, "", 0):
        dlg.GroupBorderSpace(6, 2, 6, 6)
        dlg.AddCheckbox(CK_ERR, c4d.BFH_LEFT, 0, 0, name="Show error")
        dlg.AddButton(BT_FULL, c4d.BFH_SCALEFIT, 0, name="Time full frames")
        dlg.AddButton(BT_APPLY, c4d.BFH_SCALEFIT, 0, name="Apply as new render setting")
    dlg.GroupEnd()
    return True


def panel_init(dlg):
    if not hasattr(dlg, "job"):
        dlg.job = None
        dlg.sel = 0
        dlg.split = 0.5
        dlg.full = None
    fill_rds(dlg)
    dlg.FreeChildren(CB_LEVEL)
    for i, (n, _) in enumerate(LEVELS):
        dlg.AddChild(CB_LEVEL, i, n)
    dlg.SetInt32(CB_LEVEL, getattr(dlg, "level", 0))
    dlg.SetTimer(100)
    refresh(dlg)
    return True


def fill_rds(dlg):
    doc = c4d.documents.GetActiveDocument()
    dlg.rds = [rd for rd, _ in render_datas(doc)]
    dlg.FreeChildren(CB_RD)
    active = doc.GetActiveRenderData()
    sel = 0
    for i, (rd, depth) in enumerate(render_datas(doc)):
        tag = "" if rs_videopost(rd) else "   (not Redshift)"
        dlg.AddChild(CB_RD, i, "    " * depth + rd.GetName() + tag)
        if rd == active:
            sel = i
    dlg.SetInt32(CB_RD, sel)


def panel_command(dlg, id, msg):
    if id == BT_RUN:
        if dlg.job and not dlg.job.done and not dlg.job.failed:
            stop_job(dlg, "Stopped.")
        else:
            start_job(dlg)
    elif id == CB_LEVEL:
        dlg.level = dlg.GetInt32(CB_LEVEL)
    elif id == BT_APPLY:
        j = dlg.job
        if j and j.done and j.best is not j.current:
            name = j.apply(c4d.documents.GetActiveDocument())
            fill_rds(dlg)
            c4d.StatusSetText("Tune: added render setting '%s'" % name)
            dlg.SetString(TX_STATUS, "Added '%s' under your render settings. Nothing else changed (Ctrl+Z removes it)." % name)
    elif id == BT_FULL:
        time_full(dlg)
    elif id == CK_ERR:
        dlg.view.Redraw()
    elif id == M_HOWTO:
        gui.MessageDialog(__doc__.split("\n\n", 1)[1].strip())
    elif id == M_TEMP:
        d = dlg.job.dir if dlg.job else os.path.join(tempfile.gettempdir(), "Tune")
        if os.path.isdir(d):
            c4d.storage.ShowInFinder(d, True)
    return True


def start_job(dlg):
    doc = c4d.documents.GetActiveDocument()
    fill_rds(dlg) if not getattr(dlg, "rds", None) else None
    i = dlg.GetInt32(CB_RD)
    rd = dlg.rds[i] if 0 <= i < len(dlg.rds) else None
    if rd is None or not rd.IsAlive():
        fill_rds(dlg)
        return
    if rs_videopost(rd) is None or rd[c4d.RDATA_RENDERENGINE] != RS_VP:
        gui.MessageDialog("'%s' doesn't render with Redshift. Tune only tunes Redshift settings." % rd.GetName())
        return
    if doc.GetChanged() and gui.QuestionDialog(
            "Tune renders ~25 test frames in a row. Redshift can crash on long runs - save the scene first?"):
        c4d.CallCommand(12098)                      # Save
    if dlg.job:
        dlg.job.kill()
    try:
        dlg.job = Job(doc, rd, dlg.GetInt32(CB_LEVEL))
    except Exception as e:
        log_error("start")
        gui.MessageDialog(str(e))
        dlg.job = None
        return
    dlg.sel, dlg.scroll = 0, None
    refresh(dlg)


def running(j):
    return bool(j and not j.done and not j.failed)


def stop_job(dlg, text):
    j = dlg.job
    if running(j):
        j.stop()
    refresh(dlg)


def panel_timer(dlg, msg):
    j = dlg.job
    if not running(j):
        return
    was = (j.n, j.status, j.done, j.failed, len(j.results))
    try:
        j.tick()
    except Exception as e:
        log_error("job")
        j.failed, j.error = True, "Failed: %s (details in the Console)" % str(e).splitlines()[0][:120]
        j.kill()
    if j.done and j.best and j.best in j.results and not getattr(j, "shown_best", False):
        j.shown_best = True
        dlg.sel = j.results.index(j.best)
        dlg.scroll = max(0, dlg.sel - 2)                   # scroll the recommendation into view
    if was != (j.n, j.status, j.done, j.failed, len(j.results)):
        refresh(dlg)
    else:
        refresh_progress(dlg)


def panel_core_message(dlg, id, msg):
    return True


def panel_ask_close(dlg):
    kill_worker()                                        # frees the GPU memory it holds
    if dlg.job:
        dlg.job.kill()
        if not dlg.job.done:
            dlg.job.failed, dlg.job.error = True, "Stopped (panel closed)."
    return False


def _mins(s):
    s = int(round(s))
    return "%d:%02d" % (s // 60, s % 60)


def refresh_progress(dlg):
    """The cheap per-tick update: status line + progress bar, no layout work."""
    j = dlg.job
    if not running(j):
        return
    frac, cur, left = j.progress()
    txt = "%s   -   %d%%   (up to %s left, usually less)" % (j.status, int(frac * 100), _mins(left))
    if getattr(j, "boot_state", "ready") != "ready":
        txt = "%s   -   %d s" % (j.status, int(time.time() - j.t0))      # no estimate until the worker is up
    if txt != getattr(dlg, "_last_txt", None):
        dlg._last_txt = txt
        dlg.SetString(TX_STATUS, txt)
    if abs(frac - getattr(dlg, "_last_frac", -1)) > 0.002:
        dlg._last_frac = frac
        dlg.view.Redraw()


def refresh(dlg):
    j = dlg.job
    run = running(j)
    dlg.SetString(BT_RUN, "Stop" if run else "Run")
    dlg.Enable(CB_RD, not run)
    dlg.Enable(CB_LEVEL, not run)
    have = bool(j and j.done and j.current)
    dlg.Enable(BT_APPLY, bool(have and j.best is not j.current))
    dlg.Enable(BT_FULL, bool(have))
    dlg._last_txt = None
    if not j:
        pass
    elif run:
        refresh_progress(dlg)
    elif j.failed:
        dlg.SetString(TX_STATUS, j.error)
    elif j.current:
        b = j.best
        txt = why(j, b)
        if b is not j.current:
            if j.check and not j.check["ok"]:
                txt += "  (full-size check: slightly noisier in one spot - look before applying)"
        if getattr(j, "notes", None):
            txt += "   |   " + "; ".join(j.notes[-2:])
        if getattr(j, "t_end", None):
            txt += "   |   Run took %s (%d renders)" % (_mins(j.t_end - j.t0), getattr(j, "n_run", j.n))
        if j.full:
            txt += "   |   full frame: %.1f s -> %.1f s" % j.full
        dlg.SetString(TX_STATUS, txt)
    dlg.view.Redraw()


def time_full(dlg):
    """Render one full frame with the current and the best settings, for real numbers. Runs in the background."""
    j = dlg.job
    if not j or not j.done:
        return
    j.metric = _NoMetric()
    if not os.path.exists(j.scene):                     # replaced by another run meanwhile
        j.scene = save_scene_copy(j.doc)
    j.boot_state = None
    j.done, j.failed, j.stopping = False, False, False
    j.gen, j.send, j.req = j.run_full(), None, None
    refresh(dlg)


class _NoMetric:
    def close(self):
        pass


# ================================================================ the view: results list + A/B compare
ROW_H = 18


def view_min_size(ua):
    return 300, 300


def _bmp(path, cache={}):
    if not path or not os.path.exists(path):
        return None
    key = (path, os.path.getmtime(path))
    if key not in cache:
        b = bitmaps.BaseBitmap()
        ok = b.InitWith(path)
        ok = ok[0] if isinstance(ok, tuple) else ok
        cache[key] = b if ok == c4d.IMAGERESULT_OK else None
    return cache[key]


def _layout(ua):
    w, h = ua.GetWidth(), ua.GetHeight()
    j = ua.dlg.job
    n = len(j.results) if j else 0
    list_h = min(h // 2, (n + 1) * ROW_H + 6)
    return w, h, list_h


SB_W = 8                                  # list scrollbar width


def _rows_visible(list_h):
    return max(1, list_h // ROW_H - 1)


def _first(ua, n, list_h):
    """First row shown. dlg.scroll None = follow the newest rows (while a run adds them)."""
    top = max(0, n - _rows_visible(list_h))
    sc = getattr(ua.dlg, "scroll", None)
    return top if sc is None else max(0, min(sc, top))


def _set_scroll(ua, first, n, list_h):
    top = max(0, n - _rows_visible(list_h))
    first = max(0, min(int(first), top))
    ua.dlg.scroll = None if first >= top else first    # scrolled to the bottom = follow again
    ua.Redraw()


def view_draw(ua, x1, y1, x2, y2, msg):
    ua.OffScreenOn()
    w, h, list_h = _layout(ua)
    ua.DrawSetPen(c4d.Vector(0.16, 0.16, 0.17))
    ua.DrawRectangle(0, 0, w, h)
    j = ua.dlg.job
    bar = 5 if running(j) else 0                                  # progress bar strip, above the picture
    if not j or not j.results:
        ua.DrawSetTextCol(c4d.Vector(0.6), c4d.COLOR_TRANS)
        ua.DrawText("Results show here: each setting tried, its time, and whether it looks the same." if not j
                    else "Rendering the clean reference first - results appear after the next render.", 8, 10 + bar)
    else:
        img_h = h - list_h
        _draw_compare(ua, j, 0, bar, w, img_h - bar)
        _draw_list(ua, j, 0, img_h, w, list_h)
    if bar:
        frac = j.progress()[0]
        ua.DrawSetPen(c4d.Vector(0.1))
        ua.DrawRectangle(0, 0, w, bar - 1)
        ua.DrawSetPen(c4d.Vector(0.78, 0.17, 0.03))                 # accent #c72c07
        ua.DrawRectangle(0, 0, int(w * frac), bar - 1)


def _fit(ua, text, width):
    """Text cut with an ellipsis to fit width pixels."""
    if ua.DrawGetTextWidth(text) <= width:
        return text
    while text and ua.DrawGetTextWidth(text + "…") > width:
        text = text[:-1]
    return text + "…"


def _draw_compare(ua, j, x, y, w, h):
    sel = j.results[min(ua.dlg.sel, len(j.results) - 1)]
    err = ua.dlg.GetBool(CK_ERR)
    left = _bmp((sel.get("cur_png") or j.current["png"]) + ("_err.png" if err else "_test.png"))
    right = _bmp(sel["png"] + ("_err.png" if err else "_test.png"))
    if not left or not right:
        return
    bw, bh = right.GetBw(), right.GetBh()
    s = min(w / float(bw), h / float(bh))
    dw, dh = int(bw * s), int(bh * s)
    ox, oy = x + (w - dw) // 2, y + (h - dh) // 2
    ua._img = (ox, oy, dw, dh)                            # where the picture is, for the split drag
    split = int(dw * ua.dlg.split)
    if split > 0:
        ua.DrawBitmap(left, ox, oy, split, dh, 0, 0, int(split / s), bh, c4d.BMP_NORMALSCALED)
    if split < dw:
        ua.DrawBitmap(right, ox + split, oy, dw - split, dh, int(split / s), 0, bw - int(split / s), bh, c4d.BMP_NORMALSCALED)
    ua.DrawSetPen(c4d.Vector(1))
    ua.DrawLine(ox + split, oy, ox + split, oy + dh)
    ua.DrawSetTextCol(c4d.Vector(0.95), c4d.Vector(0, 0, 0))
    ua.DrawText(" Current%s " % ((" - frame %d" % sel["frame"]) if sel.get("frame") is not None else ""), ox + 4, oy + 4)
    lbl = " %s " % _fit(ua, "error vs reference" if err else sel["name"], max(40, dw // 2 - 70))
    ua.DrawText(lbl, ox + dw - ua.DrawGetTextWidth(lbl) - 4, oy + 4)


def _draw_list(ua, j, x, y, w, h):
    ua.DrawSetPen(c4d.Vector(0.13))
    ua.DrawRectangle(x, y, x + w, y + h)
    cols = [8, w - 175 - SB_W, w - 120 - SB_W, w - 60 - SB_W]
    ua.DrawSetTextCol(c4d.Vector(0.55), c4d.COLOR_TRANS)
    for cx, t in zip(cols, ["Setting", "Time", "Error", "Looks"]):
        ua.DrawText(t, x + cx, y + 3)
    rows = j.results
    vis = _rows_visible(h)
    first = _first(ua, len(rows), h)
    if len(rows) > vis:                                  # scrollbar
        th = y + ROW_H
        track = h - ROW_H - 2
        ua.DrawSetPen(c4d.Vector(0.18))
        ua.DrawRectangle(x + w - SB_W, th, x + w - 1, th + track)
        kh = max(16, int(track * vis / float(len(rows))))
        ky = th + int((track - kh) * first / float(max(1, len(rows) - vis)))
        ua.DrawSetPen(c4d.Vector(0.42))
        ua.DrawRectangle(x + w - SB_W + 1, ky, x + w - 2, ky + kh)
    for i, r in enumerate(rows[first:first + vis], start=first):
        ry = y + (i - first + 1) * ROW_H + 3
        if i == ua.dlg.sel:
            ua.DrawSetPen(c4d.Vector(0.25, 0.25, 0.28))
            ua.DrawRectangle(x, ry - 1, x + w - SB_W - 1, ry + ROW_H - 2)
        best = r is j.best and j.done and r is not j.current
        col = c4d.Vector(0.95, 0.55, 0.3) if best else (c4d.Vector(0.85) if r["ok"] else c4d.Vector(0.45))
        ua.DrawSetTextCol(col, c4d.COLOR_TRANS)
        ua.DrawText(_fit(ua, ("★ " if best else "   ") + r["name"], cols[1] - cols[0] - 8), x + cols[0], ry)
        ua.DrawText("%d%%" % int(round(j.speedup(r) * 100)), x + cols[1], ry)
        ua.DrawText("%.3f" % r["mean"], x + cols[2], ry)
        ua.DrawText("same" if r["kind"] == "current" else ("ok" if r["ok"] else
                    ("flickers" if r["kind"] == "flicker" else "worse")), x + cols[3], ry)


def view_input(ua, msg):
    w, h, list_h = _layout(ua)
    j = ua.dlg.job
    if not j or not j.results or msg[c4d.BFM_INPUT_DEVICE] != c4d.BFM_INPUT_MOUSE:
        return False
    n = len(j.results)
    img_h = h - list_h
    ch = msg[c4d.BFM_INPUT_CHANNEL]
    if ch == c4d.BFM_INPUT_MOUSEWHEEL:                   # wheel anywhere over the panel scrolls the list
        steps = -int(msg[c4d.BFM_INPUT_VALUE] / 120.0 * 3) or (-1 if msg[c4d.BFM_INPUT_VALUE] > 0 else 1)
        _set_scroll(ua, _first(ua, n, list_h) + steps, n, list_h)
        return True
    if ch != c4d.BFM_INPUT_MOUSELEFT:
        return False
    loc = ua.Global2Local()
    mx, my = msg[c4d.BFM_INPUT_X] + loc["x"], msg[c4d.BFM_INPUT_Y] + loc["y"]
    vis = _rows_visible(list_h)
    if my >= img_h and mx >= w - SB_W - 2 and n > vis:  # scrollbar: jump there, then drag
        track = list_h - ROW_H - 2

        def put(yy):
            _set_scroll(ua, round((yy - img_h - ROW_H) / float(max(1, track)) * n - vis / 2.0), n, list_h)
        put(my)
        ua.MouseDragStart(c4d.KEY_MLEFT, mx, my, c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE | c4d.MOUSEDRAGFLAGS_NOMOVE)
        while True:
            res, dx, dy, channels = ua.MouseDrag()
            if res != c4d.MOUSEDRAGRESULT_CONTINUE:
                break
            if dy:
                my -= dy                                 # MouseDrag reports old - new
                put(my)
        ua.MouseDragEnd()
        return True
    if my >= img_h:                                      # a row: show it in the compare
        i = _first(ua, n, list_h) + int((my - img_h - 3) // ROW_H) - 1
        if 0 <= i < n:
            ua.dlg.sel = i
            ua.Redraw()
        return True
    # drag the split: relative to the picture (it's centred, not at the panel's left edge)
    ox, oy, dw, dh = getattr(ua, "_img", (0, 0, w, img_h))

    def put(x):
        ua.dlg.split = max(0.0, min(1.0, (x - ox) / float(max(1, dw))))
        ua.Redraw()
    put(mx)
    ua.MouseDragStart(c4d.KEY_MLEFT, mx, my, c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE | c4d.MOUSEDRAGFLAGS_NOMOVE)
    while True:
        res, dx, dy, channels = ua.MouseDrag()
        if res != c4d.MOUSEDRAGRESULT_CONTINUE:
            break
        if dx:
            mx -= dx                                     # MouseDrag reports old - new
            put(mx)
    ua.MouseDragEnd()
    return True
