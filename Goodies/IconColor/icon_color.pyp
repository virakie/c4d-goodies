"""Icon Color - tint the Object Manager icons of the selected objects.

    click        C4D's own colour picker, applied to every selected object
    Shift+click  same, and the viewport display colour follows
    Ctrl+click   back to the default icon (and display colour), no picker

The picker opens on the first selected object's current colour, so a
second click on the same selection is a tweak, not a restart.
"""

import os

import c4d
from c4d import plugins, bitmaps

# --------------------------------------------------------------------------
# Local pick next to the other Goodies (1066610+). Register a real one
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
PLUGIN_ID = 1066616

MODE = c4d.ID_BASELIST_ICON_COLORIZE_MODE
COLOR = c4d.ID_BASELIST_ICON_COLOR
DEFAULT_SEED = c4d.Vector(0xC7, 0x2C, 0x07) / 255.0     # #c72c07


def _qualifier():
    bc = c4d.BaseContainer()
    if c4d.gui.GetInputState(c4d.BFM_INPUT_KEYBOARD, c4d.BFM_INPUT_CHANNEL, bc):
        return bc.GetInt32(c4d.BFM_INPUT_QUALIFIER)
    return 0


def _seed(op):
    if op[MODE] == c4d.ID_BASELIST_ICON_COLORIZE_MODE_CUSTOM and op[COLOR] is not None:
        return op[COLOR]
    return DEFAULT_SEED


class IconColor(plugins.CommandData):

    def Execute(self, doc):
        targets = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE)
        if not targets:
            c4d.StatusSetText("Icon Color: select objects first")
            return True

        qual = _qualifier()
        reset = bool(qual & c4d.QCTRL)
        display = bool(qual & c4d.QSHIFT)

        col = None
        if not reset:
            col = c4d.gui.ColorDialog(0, _seed(targets[0]))
            if not isinstance(col, c4d.Vector):
                return True                 # cancelled

        doc.StartUndo()
        for op in targets:
            doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, op)
            if reset:
                op[MODE] = c4d.ID_BASELIST_ICON_COLORIZE_MODE_NONE
                op[c4d.ID_BASEOBJECT_USECOLOR] = c4d.ID_BASEOBJECT_USECOLOR_OFF
            else:
                op[MODE] = c4d.ID_BASELIST_ICON_COLORIZE_MODE_CUSTOM
                op[COLOR] = col
                if display:
                    op[c4d.ID_BASEOBJECT_USECOLOR] = c4d.ID_BASEOBJECT_USECOLOR_ALWAYS
                    op[c4d.ID_BASEOBJECT_COLOR] = col
        doc.EndUndo()
        c4d.EventAdd()

        if reset:
            msg = "Icon Color: reset %d" % len(targets)
        else:
            msg = "Icon Color: #%02x%02x%02x on %d%s" % (
                int(round(col.x * 255)), int(round(col.y * 255)), int(round(col.z * 255)),
                len(targets), " (+ display colour)" if display else "")
        c4d.StatusSetText(msg)
        return True

    def GetState(self, doc):
        return c4d.CMD_ENABLED


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(
        PLUGIN_ID, "Icon Color", 0, _icon(),
        "Pick an icon colour for the selected objects (Shift: display colour too, Ctrl: reset)",
        IconColor())
