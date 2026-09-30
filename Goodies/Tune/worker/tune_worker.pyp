"""Tune's render worker - runs inside a hidden Commandline.exe, never in the C4D you work in.

Tune starts:  Commandline.exe -tune_worker <port>   (with g_additionalModulePath pointing at this folder)
The worker connects to Tune on 127.0.0.1:<port> and takes JSON-line commands:
    {"cmd": "load", "path": scene}                          -> {"ok", "rds": [names]}
    {"cmd": "render", "rd": index, "over": {id: value}, "frame": f, "w", "h", "region": [x, y, s] | null, "out": path}
                                                            -> {"progress": p} ... then {"ok", "secs", "w", "h"}
    {"cmd": "quit"}
If Redshift crashes, only this process dies; Tune starts a new one and retries.

In the normal C4D this file loads too, but does nothing: it only acts on the -tune_worker argument.
"""
import json
import socket
import sys
import time
import traceback

import c4d
from c4d import bitmaps, documents

RS_VP = 1036219


class Worker:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=30)
        self.sock.settimeout(None)
        self.rfile = self.sock.makefile("r", encoding="utf-8")
        self.doc = None
        self.base = {}

    def send(self, **msg):
        self.sock.sendall((json.dumps(msg) + "\n").encode("utf-8"))

    def run(self):
        self.send(hello=True, pid=__import__("os").getpid())
        for line in self.rfile:
            line = line.strip()
            if not line:
                continue
            try:
                c = json.loads(line)
                if c["cmd"] == "quit":
                    break
                self.send(**getattr(self, "cmd_" + c["cmd"])(c))
            except Exception as e:
                self.send(ok=False, error="%s: %s" % (type(e).__name__, e), trace=traceback.format_exc())

    def rds(self):
        out = []

        def walk(rd):
            while rd:
                out.append(rd)
                walk(rd.GetDown())
                rd = rd.GetNext()
        walk(self.doc.GetFirstRenderData())
        return out

    def cmd_load(self, c):
        t = time.perf_counter()
        doc = documents.LoadDocument(c["path"], c4d.SCENEFILTER_OBJECTS | c4d.SCENEFILTER_MATERIALS, None)
        if doc is None:
            raise RuntimeError("couldn't load " + c["path"])
        self.doc = doc
        self.base = {}
        return dict(ok=True, rds=[rd.GetName() for rd in self.rds()], secs=time.perf_counter() - t)

    def cmd_py(self, c):
        """Diagnostics: run a snippet against the loaded scene (local socket only). Set `result`."""
        ns = {"c4d": c4d, "doc": self.doc, "worker": self}
        exec(c["code"], ns)
        return dict(ok=True, result=ns.get("result"))

    def cmd_render(self, c):
        rd = self.rds()[c["rd"]]
        self.doc.SetActiveRenderData(rd)
        vp = rd.GetFirstVideoPost()
        while vp and vp.GetType() != RS_VP:
            vp = vp.GetNext()
        key = c["rd"]
        if key not in self.base:                       # the render setting as saved, to reset between renders
            self.base[key] = {int(k): vp[int(k)] for k in c.get("keys", [])}
        for k, v in self.base[key].items():
            vp[k] = v
        for k, v in c["over"].items():
            vp[int(k)] = v
        fps = self.doc.GetFps()
        self.doc.SetTime(c4d.BaseTime(c["frame"], fps))

        bc = rd.GetDataInstance().GetClone(c4d.COPYFLAGS_NONE)
        bc[c4d.RDATA_SAVEIMAGE] = False
        bc[c4d.RDATA_MULTIPASS_SAVEIMAGE] = False
        bc[c4d.RDATA_FRAMESEQUENCE] = c4d.RDATA_FRAMESEQUENCE_CURRENTFRAME     # never the whole range
        bc[c4d.RDATA_RENDERREGION] = False
        W, H = int(rd[c4d.RDATA_XRES]), int(rd[c4d.RDATA_YRES])
        region = c.get("region")
        if region:
            x, y, s = region
            bc[c4d.RDATA_RENDERREGION] = True
            bc[c4d.RDATA_RENDERREGION_LEFT] = x
            bc[c4d.RDATA_RENDERREGION_TOP] = y
            bc[c4d.RDATA_RENDERREGION_RIGHT] = W - x - s
            bc[c4d.RDATA_RENDERREGION_BOTTOM] = H - y - s
        else:
            W, H = int(c["w"]), int(c["h"])
            bc[c4d.RDATA_XRES] = float(W)
            bc[c4d.RDATA_YRES] = float(H)
        bmp = bitmaps.MultipassBitmap(W, H, c4d.COLORMODE_RGBf)
        last = [0.0]

        def prog(p, kind):
            if kind == 1 and p - last[0] >= 0.05:
                last[0] = p
                try:
                    self.send(progress=p)
                except Exception:
                    pass
        t = time.perf_counter()
        res = documents.RenderDocument(self.doc, bc, bmp, c4d.RENDERFLAGS_EXTERNAL, None, prog)
        secs = time.perf_counter() - t
        if res != c4d.RENDERRESULT_OK:
            raise RuntimeError("Redshift render failed (result %d)" % res)
        x0, y0, cw, ch = (region[0], region[1], region[2], region[2]) if region else (0, 0, W, H)
        buf = bytearray(cw * ch * 12)
        mv = memoryview(buf)
        row = cw * 12
        for j in range(ch):
            bmp.GetPixelCnt(x0, y0 + j, cw, mv[j * row:(j + 1) * row], 12, c4d.COLORMODE_RGBf, c4d.PIXELCNT_0)
        with open(c["out"], "wb") as f:
            f.write(buf)
        return dict(ok=True, secs=secs, w=cw, h=ch)

def PluginMessage(id, data):
    if id == c4d.C4DPL_COMMANDLINEARGS and "-tune_worker" in sys.argv:
        if getattr(sys, "_tune_worker_ran", False):     # loaded twice (plugins folder + module path): run once
            return True
        sys._tune_worker_ran = True
        try:
            port = int(sys.argv[sys.argv.index("-tune_worker") + 1])
            Worker(port).run()
        except Exception:
            traceback.print_exc()
        return True
    return False
