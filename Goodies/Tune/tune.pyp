"""Tune - finds faster Redshift render settings that look the same. See tune_core.py for how it works.

This file only registers and forwards: the panel's layout, drawing and the whole test-render job live in
tune_core.py, which is reloaded whenever it changes.
"""

import importlib
import os
import sys

import c4d
from c4d import plugins, gui, bitmaps

# --------------------------------------------------------------------------
# Local pick next to the other Goodies (1066610+). Register a real one
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
PANEL_ID = 1066650

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import tune_core                                    # noqa: E402

_mod = {"core": tune_core, "mtime": os.path.getmtime(tune_core.__file__)}


def core():
    """tune_core, reloaded if the file changed. A broken edit keeps the last working version and logs the error."""
    try:
        mtime = os.path.getmtime(_mod["core"].__file__)
        if mtime != _mod["mtime"]:
            _mod["mtime"] = mtime
            _mod["core"] = importlib.reload(_mod["core"])
            print("[Tune] reloaded tune_core.py")
    except Exception:
        _mod["core"].log_error("reload tune_core.py")
    return _mod["core"]


def _safe(where, fn, *args, default=True):
    c = core()
    try:
        return fn(c)(*args)
    except Exception:
        c.log_error(where)
        return default


class View(gui.GeUserArea):
    """The compare image. Everything forwards to tune_core.view_*."""

    def __init__(self, dlg):
        self.dlg = dlg

    def GetMinSize(self):
        return _safe("view size", lambda c: c.view_min_size, self, default=(300, 220))

    def DrawMsg(self, x1, y1, x2, y2, msg):
        _safe("view draw", lambda c: c.view_draw, self, x1, y1, x2, y2, msg, default=None)

    def InputEvent(self, msg):
        return _safe("view input", lambda c: c.view_input, self, msg)


class Panel(gui.GeDialog):
    """Forwards to tune_core.panel_*; keeps the state (job, results) on itself."""

    def __init__(self):
        self.view = View(self)

    def CreateLayout(self):
        return _safe("layout", lambda c: c.panel_layout, self)

    def InitValues(self):
        return _safe("init", lambda c: c.panel_init, self)

    def Command(self, id, msg):
        return _safe("command %d" % id, lambda c: c.panel_command, self, id, msg)

    def Timer(self, msg):
        _safe("timer", lambda c: c.panel_timer, self, msg, default=None)

    def CoreMessage(self, id, msg):
        return _safe("core message", lambda c: c.panel_core_message, self, id, msg)

    def AskClose(self):
        return _safe("close", lambda c: c.panel_ask_close, self, default=False)


PANEL = Panel()


class OpenPanel(plugins.CommandData):
    def Execute(self, doc):
        return PANEL.Open(c4d.DLG_TYPE_ASYNC, PANEL_ID, defaultw=460, defaulth=560)

    def RestoreLayout(self, sec_ref):
        return PANEL.Restore(PANEL_ID, sec_ref)


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(HERE, "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(PANEL_ID, "Tune", 0, _icon(),
                                  "Tune: find faster Redshift render settings that look the same", OpenPanel())
