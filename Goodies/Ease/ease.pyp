"""Ease - curve-editor easing for C4D keys. See ease_core.py for how it works.

    Ease                   the panel: curve editor + preview, Apply to the
                           selected keys, Copy an ease from keys, Paste /
                           Paste Reversed
    Ease: Copy             the selected keys' ease -> Ease's clipboard
    Ease: Paste            clipboard -> selected keys
    Ease: Paste Reversed   clipboard mirrored in time (ease-in <-> ease-out)

Only the panel shows in the menu; the three commands are hidden from it but
can still be bound to shortcuts (Customize Commands) or AutoHotPie.

This file only registers and forwards: the panel's layout, drawing, mouse
handling and all the key work live in ease_core.py, which is reloaded
whenever it changes - edit it and the next redraw / click runs the new code.
"""

import importlib
import os
import sys

import c4d
from c4d import plugins, gui, bitmaps

# --------------------------------------------------------------------------
# Local picks next to the other Goodies (1066610+). Register real ones
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
PANEL_ID = 1066643
CMD_COPY = 1066644
CMD_PASTE = 1066645
CMD_PASTE_REV = 1066646

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import ease_core                                    # noqa: E402

_mod = {"core": ease_core, "mtime": os.path.getmtime(ease_core.__file__)}


def core():
    """ease_core, reloaded if the file changed. A broken edit keeps the last
    working version and logs the error."""
    try:
        mtime = os.path.getmtime(_mod["core"].__file__)
        if mtime != _mod["mtime"]:
            _mod["mtime"] = mtime
            _mod["core"] = importlib.reload(_mod["core"])
            print("[Ease] reloaded ease_core.py")
    except Exception:
        _mod["core"].log_error("reload ease_core.py")
    return _mod["core"]


def _safe(where, fn, *args, default=True):
    c = core()
    try:
        return fn(c)(*args)
    except Exception:
        c.log_error(where)
        return default


class Editor(gui.GeUserArea):
    """The curve editor. Everything forwards to ease_core.editor_*."""

    def __init__(self, dlg):
        self.dlg = dlg

    def GetMinSize(self):
        return _safe("editor size", lambda c: c.editor_min_size, self, default=(260, 260))

    def DrawMsg(self, x1, y1, x2, y2, msg):
        _safe("editor draw", lambda c: c.editor_draw, self, x1, y1, x2, y2, msg, default=None)

    def InputEvent(self, msg):
        return _safe("editor input", lambda c: c.editor_input, self, msg)


class Panel(gui.GeDialog):
    """Forwards to ease_core.panel_*; keeps the state (curve, playback)."""

    def __init__(self):
        self.editor = Editor(self)

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


PANEL = Panel()


class OpenPanel(plugins.CommandData):
    def Execute(self, doc):
        return PANEL.Open(c4d.DLG_TYPE_ASYNC, PANEL_ID, defaultw=420, defaulth=520)

    def RestoreLayout(self, sec_ref):
        return PANEL.Restore(PANEL_ID, sec_ref)


class Action(plugins.CommandData):
    def __init__(self, name):
        self.name = name

    def Execute(self, doc):
        _safe(self.name, lambda c: getattr(c, self.name), PANEL)
        return True


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(HERE, "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    icon = _icon()
    HIDDEN = c4d.PLUGINFLAG_HIDEPLUGINMENU          # one menu entry (the panel); still bindable in Customize Commands
    plugins.RegisterCommandPlugin(PANEL_ID, "Ease", 0, icon, "Ease panel: curve editor, apply / copy / paste eases on keys",
                                  OpenPanel())
    plugins.RegisterCommandPlugin(CMD_COPY, "Ease: Copy", HIDDEN, icon, "Copy the selected keys' ease", Action("cmd_copy"))
    plugins.RegisterCommandPlugin(CMD_PASTE, "Ease: Paste", HIDDEN, icon, "Paste the copied ease onto the selected keys",
                                  Action("cmd_paste"))
    plugins.RegisterCommandPlugin(CMD_PASTE_REV, "Ease: Paste Reversed", HIDDEN, icon,
                                  "Paste the copied ease mirrored in time (ease-in <-> ease-out)", Action("cmd_paste_reversed"))
