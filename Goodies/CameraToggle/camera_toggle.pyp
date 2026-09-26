"""Camera Toggle - flip the active viewport between its scene camera and the
editor (default) camera, remembering which scene camera to come back to.

Replaces the classic "Toggle Camera View" script.

Why it is fast: the old script ended with a bare c4d.EventAdd(), which makes
C4D re-evaluate the whole scene (XPresso, Python tags, generators, pose
morphs) before redrawing. Switching cameras changes no scene data, so that
evaluation is pure waste - measured 216 ms vs 17 ms on a scene whose
expressions cost 200 ms. Here the view is redrawn with DRAWFLAGS_NO_EXPRESSIONS
and managers are told with EVENT_NOEXPRESSION, which evaluates nothing.

The remembered camera lives in memory rather than the document container, so
toggling never marks the scene as modified.
"""

import os

import c4d
from c4d import plugins, bitmaps

# --------------------------------------------------------------------------
# Local pick next to the other Goodies (1066610+). Register a real one
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
PLUGIN_ID = 1066613

REDRAW = (c4d.DRAWFLAGS_ONLY_ACTIVE_VIEW | c4d.DRAWFLAGS_NO_THREAD |
          c4d.DRAWFLAGS_NO_ANIMATION | c4d.DRAWFLAGS_NO_EXPRESSIONS)

# [(doc, camera)] - a list, because documents are compared with ==, not hashed.
_remembered = []


def _recall(doc):
    for d, cam in _remembered:
        if d == doc:
            if cam.IsAlive() and cam.GetDocument() == doc:
                return cam
            return None
    return None


def _remember(doc, cam):
    _remembered[:] = [(d, c) for d, c in _remembered
                      if d != doc and d.IsAlive()]
    _remembered.append((doc, cam))


def _first_camera(doc):
    """Depth-first, iterative, stops at the first hit. Only runs when nothing
    is remembered and no camera is selected."""
    op = doc.GetFirstObject()
    while op:
        if op.CheckType(c4d.Ocamera):
            return op
        nxt = op.GetDown()
        if nxt is None:
            while op and op.GetNext() is None:
                op = op.GetUp()
            nxt = op.GetNext() if op else None
        op = nxt
    return None


def _on_scene_camera(bd, doc):
    cam = bd.GetSceneCamera(doc)
    return cam is not None and cam != bd.GetEditorCamera()


class CameraToggle(plugins.CommandData):

    def Execute(self, doc):
        bd = doc.GetActiveBaseDraw()
        if bd is None:
            return True

        if _on_scene_camera(bd, doc):
            _remember(doc, bd.GetSceneCamera(doc))
            bd.SetSceneCamera(None)          # None = the editor camera
            msg = "Editor camera"
        else:
            cam = _recall(doc)
            if cam is None:
                act = doc.GetActiveObject()
                cam = act if act and act.CheckType(c4d.Ocamera) else _first_camera(doc)
            if cam is None:
                c4d.StatusSetText("Camera Toggle: no camera in this scene")
                return True
            bd.SetSceneCamera(cam)
            msg = "Camera: %s" % cam.GetName()

        c4d.DrawViews(REDRAW)
        c4d.EventAdd(c4d.EVENT_NOEXPRESSION)
        c4d.StatusSetText(msg)
        return True

    def GetState(self, doc):
        state = c4d.CMD_ENABLED
        bd = doc.GetActiveBaseDraw()
        if bd is not None and _on_scene_camera(bd, doc):
            state |= c4d.CMD_VALUE           # pressed while looking through a scene camera
        return state


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(
        PLUGIN_ID, "Camera Toggle", 0, _icon(),
        "Switch the viewport between its scene camera and the editor camera",
        CameraToggle())
