"""Render Region - snip a render region straight in the viewport, keep named
ones per scene, and fit one around objects over the frame range.

    Alt+R (Snip Region)   drag a box in the viewport: it becomes the render
                          region. A click without dragging turns it off.
                          Esc while dragging cancels. Back to your tool after.
    Render Regions panel  region on/off, saved regions (pick to apply,
                          Save As, Delete), Fit to Selection for the current
                          frame or the whole frame range, with a border.

Facts it relies on (verified 2026-09-26):
- Redshift honours C4D's own render region (RDATA_RENDERREGION + the four
  border insets in pixels) exactly - render test, L20 R10 T5 B15.
- The RS RenderView crop is not stored anywhere a plugin can read, so the
  viewport is the place to draw regions.
- The region borders cannot be keyframed (DESC_ANIMATE_OFF), so fitting
  over a frame range gives the union of every frame's box - one region
  that covers the objects the whole way through.

The region is always visible: a small "Render Region Frame" helper object
(created on first use, renders nothing) draws it in the camera view. Hide it
with its green check. Saved regions are stored as fractions of the frame in
the document, so they survive a resolution change. Fit to Selection projects
the corners of each object's bounding box, not every point (MW's approach) -
conservative and fast on heavy meshes.
"""

import json
import os
import traceback

import c4d
from c4d import plugins, gui, bitmaps, storage

# --------------------------------------------------------------------------
# Local picks next to the other Goodies (1066610+). Register real ones
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
TOOL_ID = 1066631
CMD_SNIP = 1066632
PANEL_ID = 1066633
FRAME_ID = 1066634               # the overlay helper object
STATE_KEY = 1066635              # JSON string in the document: saved regions

MIN_DRAG = 4                     # pixels: a smaller drag counts as a click (= region off)
DEFAULT_BORDER = 30              # render pixels around fitted objects

C_FRAME = c4d.Vector(1.0, 0.78, 0.16)        # region frame (yellow, like a crop marker)
C_DRAG = c4d.Vector(1.0)

# panel gadgets
CK_ON, B_SNIP = 5000, 5001
CB_SAVED, B_SAVE, B_DELETE = 5002, 5003, 5004
E_BORDER, B_FIT_NOW, B_FIT_RANGE = 5005, 5006, 5007
T_INFO = 5008


def log(*args):
    print("[Render Region]", *args)


def log_error(where):
    try:
        d = os.path.join(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS), "goodies_renderregion")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write("--- %s\n%s\n" % (where, traceback.format_exc()))
    except Exception:
        pass
    log("error in", where)


# ═════════════════════════════════════════════════════════════ region maths

def res_of(rd):
    return max(1, int(round(rd[c4d.RDATA_XRES]))), max(1, int(round(rd[c4d.RDATA_YRES])))


def get_region(doc):
    """(on, (u0, v0, u1, v1)) - the current region as fractions of the frame."""
    rd = doc.GetActiveRenderData()
    w, h = res_of(rd)
    l, t = rd[c4d.RDATA_RENDERREGION_LEFT], rd[c4d.RDATA_RENDERREGION_TOP]
    r, b = rd[c4d.RDATA_RENDERREGION_RIGHT], rd[c4d.RDATA_RENDERREGION_BOTTOM]
    return bool(rd[c4d.RDATA_RENDERREGION]), (l / float(w), t / float(h), 1.0 - r / float(w), 1.0 - b / float(h))


def set_region(doc, uv, on=True, undo=True):
    """uv = (u0, v0, u1, v1) fractions of the frame -> render settings."""
    rd = doc.GetActiveRenderData()
    w, h = res_of(rd)
    u0, v0, u1, v1 = uv
    u0, u1 = sorted((max(0.0, min(1.0, u0)), max(0.0, min(1.0, u1))))
    v0, v1 = sorted((max(0.0, min(1.0, v0)), max(0.0, min(1.0, v1))))
    if undo:
        doc.StartUndo()
        doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, rd)
    rd[c4d.RDATA_RENDERREGION] = on
    rd[c4d.RDATA_RENDERREGION_LEFT] = int(round(u0 * w))
    rd[c4d.RDATA_RENDERREGION_TOP] = int(round(v0 * h))
    rd[c4d.RDATA_RENDERREGION_RIGHT] = int(round((1.0 - u1) * w))
    rd[c4d.RDATA_RENDERREGION_BOTTOM] = int(round((1.0 - v1) * h))
    if on:
        ensure_frame_object(doc, undo)
    if undo:
        doc.EndUndo()
    c4d.EventAdd()


def set_region_on(doc, on):
    rd = doc.GetActiveRenderData()
    doc.StartUndo()
    doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, rd)
    rd[c4d.RDATA_RENDERREGION] = on
    if on:
        ensure_frame_object(doc, True)
    doc.EndUndo()
    c4d.EventAdd()


def describe(doc):
    on, (u0, v0, u1, v1) = get_region(doc)
    w, h = res_of(doc.GetActiveRenderData())
    if not on:
        return "Region off - renders the full %d x %d" % (w, h)
    rw, rh = int(round((u1 - u0) * w)), int(round((v1 - v0) * h))
    pct = 100.0 * (rw * rh) / float(w * h)
    return "%d x %d px at %d, %d  (%.0f%% of the frame)" % (rw, rh, int(round(u0 * w)), int(round(v0 * h)), pct)


def view_to_uv(bd, x, y):
    sf = bd.GetSafeFrame()
    fw, fh = float(max(1, sf["cr"] - sf["cl"])), float(max(1, sf["cb"] - sf["ct"]))
    return (x - sf["cl"]) / fw, (y - sf["ct"]) / fh


def uv_to_view(bd, u, v):
    sf = bd.GetSafeFrame()
    return sf["cl"] + u * (sf["cr"] - sf["cl"]), sf["ct"] + v * (sf["cb"] - sf["ct"])


# ═════════════════════════════════════════════════════════════ saved regions

def load_saved(doc):
    raw = doc.GetDataInstance().GetString(STATE_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def store_saved(doc, data):
    doc.GetDataInstance().SetString(STATE_KEY, json.dumps(data))
    doc.SetChanged()


# ═════════════════════════════════════════════════════════════ overlay object

def find_frame_object(doc):
    op = doc.GetFirstObject()
    guard = 0
    while op and guard < 1000000:
        guard += 1
        if op.GetType() == FRAME_ID:
            return op
        nxt = op.GetDown()
        if nxt is None:
            while op and op.GetNext() is None:
                op = op.GetUp()
            nxt = op.GetNext() if op else None
        op = nxt
    return None


def ensure_frame_object(doc, undo):
    if find_frame_object(doc) is not None:
        return
    if c4d.plugins.FindPlugin(FRAME_ID, c4d.PLUGINTYPE_OBJECT) is None:
        return                                   # not registered (e.g. mid-install): no overlay
    op = c4d.BaseObject(FRAME_ID)
    if op is None:
        return
    op.SetName("Render Region Frame")
    doc.InsertObject(op)
    if undo:
        doc.AddUndo(c4d.UNDOTYPE_NEW, op)


def draw_rect(bd, x0, y0, x1, y1, colour):
    bd.SetPen(colour)
    bd.DrawLine2D(c4d.Vector(x0, y0, 0), c4d.Vector(x1, y0, 0))
    bd.DrawLine2D(c4d.Vector(x1, y0, 0), c4d.Vector(x1, y1, 0))
    bd.DrawLine2D(c4d.Vector(x1, y1, 0), c4d.Vector(x0, y1, 0))
    bd.DrawLine2D(c4d.Vector(x0, y1, 0), c4d.Vector(x0, y0, 0))


class RegionFrame(plugins.ObjectData):
    """Draws the active render region in the camera view. Renders nothing."""

    def Draw(self, op, drawpass, bd, bh):
        if drawpass != c4d.DRAWPASS_OBJECT:
            return c4d.DRAWRESULT_SKIP
        try:
            doc = op.GetDocument()
            if doc is None or TOOL.dragging:
                return c4d.DRAWRESULT_OK
            on, (u0, v0, u1, v1) = get_region(doc)
            if not on:
                return c4d.DRAWRESULT_OK
            bd.SetMatrix_Screen()
            x0, y0 = uv_to_view(bd, u0, v0)
            x1, y1 = uv_to_view(bd, u1, v1)
            draw_rect(bd, x0, y0, x1, y1, C_FRAME)
            draw_rect(bd, x0 - 1, y0 - 1, x1 + 1, y1 + 1, C_FRAME)      # 2 px so it reads
            w, h = res_of(doc.GetActiveRenderData())
            bd.DrawHUDText(int(x0) + 4, int(y0) + 4, "Render Region  %d x %d" % (
                int(round((u1 - u0) * w)), int(round((v1 - v0) * h))))
        except Exception:
            log_error("RegionFrame.Draw")
        return c4d.DRAWRESULT_OK


# ═════════════════════════════════════════════════════════════ snip tool

class SnipTool(plugins.ToolData):
    """Drag a box in the viewport -> render region. One snip, then back to
    the tool you were using."""

    def __init__(self):
        self.dragging = False
        self.rect = None
        self.prev_tool = 0

    def GetState(self, doc):
        return c4d.CMD_ENABLED

    def GetCursorInfo(self, doc, data, bd, x, y, bc):
        bc.SetInt32(c4d.RESULT_CURSOR, c4d.MOUSE_CROSS)
        bc.SetString(c4d.RESULT_BUBBLEHELP, "Drag a render region  -  click: region off  -  Esc: cancel")
        return True

    def _back(self, doc):
        doc.SetAction(self.prev_tool if self.prev_tool and self.prev_tool != TOOL_ID else c4d.ID_MODELING_MOVE)

    def MouseInput(self, doc, data, bd, win, msg):
        if msg.GetInt32(c4d.BFM_INPUT_CHANNEL) != c4d.BFM_INPUT_MOUSELEFT:
            return False
        x0, y0 = msg.GetInt32(c4d.BFM_INPUT_X), msg.GetInt32(c4d.BFM_INPUT_Y)
        x, y = float(x0), float(y0)
        self.dragging = True
        self.rect = (x0, y0, x, y)
        cancelled = False
        win.MouseDragStart(c4d.KEY_MLEFT, x, y, c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE | c4d.MOUSEDRAGFLAGS_NOMOVE)
        try:
            guard = 0
            while guard < 1000000:
                guard += 1
                result, dx, dy, channels = win.MouseDrag()
                if result == c4d.MOUSEDRAGRESULT_ESCAPE:
                    cancelled = True
                    break
                if result != c4d.MOUSEDRAGRESULT_CONTINUE:
                    break
                if dx or dy:
                    x += dx
                    y += dy
                    self.rect = (x0, y0, x, y)
                    c4d.DrawViews(c4d.DRAWFLAGS_ONLY_ACTIVE_VIEW | c4d.DRAWFLAGS_NO_THREAD |
                                  c4d.DRAWFLAGS_NO_ANIMATION | c4d.DRAWFLAGS_NO_EXPRESSIONS)
        finally:
            win.MouseDragEnd()
            self.dragging = False
        try:
            if cancelled:
                c4d.StatusSetText("Render Region: cancelled")
            elif abs(x - x0) < MIN_DRAG and abs(y - y0) < MIN_DRAG:
                set_region_on(doc, False)
                c4d.StatusSetText("Render Region: off")
            else:
                u0, v0 = view_to_uv(bd, min(x0, x), min(y0, y))
                u1, v1 = view_to_uv(bd, max(x0, x), max(y0, y))
                set_region(doc, (u0, v0, u1, v1))
                c4d.StatusSetText("Render Region: " + describe(doc))
            PANEL.refresh()
        except Exception:
            log_error("SnipTool.apply")
        self.rect = None
        self._back(doc)
        c4d.EventAdd()
        return True

    def KeyboardInput(self, doc, data, bd, win, msg):
        if msg.GetInt32(c4d.BFM_INPUT_CHANNEL) == c4d.KEY_ESC:
            self._back(doc)
            c4d.EventAdd()
            return True
        return False

    def Draw(self, doc, data, bd, bh, bt, flags):
        try:
            if flags & c4d.TOOLDRAWFLAGS_HIGHLIGHT:
                return c4d.TOOLDRAW_NONE
            bd.SetMatrix_Screen()
            sf = bd.GetSafeFrame()
            draw_rect(bd, sf["cl"], sf["ct"], sf["cr"] - 1, sf["cb"] - 1, c4d.Vector(0.45))   # what can be snipped
            if self.rect:
                x0, y0, x1, y1 = self.rect
                draw_rect(bd, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1), C_DRAG)
                u0, v0 = view_to_uv(bd, min(x0, x1), min(y0, y1))
                u1, v1 = view_to_uv(bd, max(x0, x1), max(y0, y1))
                w, h = res_of(doc.GetActiveRenderData())
                bd.DrawHUDText(int(min(x0, x1)) + 4, int(max(y0, y1)) + 6, "%d x %d" % (
                    abs(int(round((min(u1, 1) - max(u0, 0)) * w))), abs(int(round((min(v1, 1) - max(v0, 0)) * h)))))
        except Exception:
            log_error("SnipTool.Draw")
        return c4d.TOOLDRAW_NONE


TOOL = SnipTool()


class SnipCommand(plugins.CommandData):
    def Execute(self, doc):
        TOOL.prev_tool = doc.GetAction()
        doc.SetAction(TOOL_ID)
        c4d.StatusSetText("Render Region: drag a box in the viewport (click = region off, Esc = cancel)")
        c4d.EventAdd()
        return True


# ═════════════════════════════════════════════════════════════ fit to selection

def _poly_boxes(op):
    """(matrix, mp, rad) for every polygon object op draws (caches included)."""
    out = []

    def add(o, mg):
        dc = o.GetDeformCache()
        if dc is not None:
            add(dc, mg)
            return
        cache = o.GetCache()
        if cache is not None:
            c, guard = cache, 0
            while c and guard < 100000:
                guard += 1
                add(c, mg * c.GetMl())
                c = c.GetNext()
            return
        if isinstance(o, c4d.PointObject) and o.GetPointCount():
            out.append((mg, o.GetMp(), o.GetRad()))
        child = o.GetDown()
        while child:
            add(child, mg * child.GetMl())
            child = child.GetNext()

    add(op, op.GetMg())
    return out


def project_box(rbd, objs):
    """Screen box of the objects' bounding boxes in the render view, as uv."""
    lo_u = lo_v = 1e9
    hi_u = hi_v = -1e9
    found = False
    for op in objs:
        for mg, mp, rad in _poly_boxes(op):
            for sx in (-1, 1):
                for sy in (-1, 1):
                    for sz in (-1, 1):
                        p = mg * (mp + c4d.Vector(rad.x * sx, rad.y * sy, rad.z * sz))
                        s = rbd.WS(p)
                        if s.z <= 0:
                            continue                  # behind the camera
                        u, v = view_to_uv(rbd, s.x, s.y)
                        lo_u, lo_v = min(lo_u, u), min(lo_v, v)
                        hi_u, hi_v = max(hi_u, u), max(hi_v, v)
                        found = True
    return (lo_u, lo_v, hi_u, hi_v) if found else None


def fit_to_selection(doc, border_px, whole_range):
    objs = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN)
    objs = [o for o in objs if o.GetType() != FRAME_ID]
    if not objs:
        return "Select the objects to fit the region around"
    rbd = doc.GetRenderBaseDraw()
    fps = doc.GetFps()
    boxes = []
    if whole_range:
        original = doc.GetTime()
        start = doc.GetLoopMinTime().GetFrame(fps)
        end = doc.GetLoopMaxTime().GetFrame(fps)
        try:
            for f in range(start, end + 1):
                doc.SetTime(c4d.BaseTime(f, fps))
                doc.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)
                b = project_box(rbd, objs)
                if b:
                    boxes.append(b)
        finally:
            doc.SetTime(original)
            doc.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)
    else:
        b = project_box(rbd, objs)
        if b:
            boxes.append(b)
    if not boxes:
        return "Nothing of the selection is in front of the render camera"
    w, h = res_of(doc.GetActiveRenderData())
    bu, bv = border_px / float(w), border_px / float(h)
    u0 = min(b[0] for b in boxes) - bu
    v0 = min(b[1] for b in boxes) - bv
    u1 = max(b[2] for b in boxes) + bu
    v1 = max(b[3] for b in boxes) + bv
    set_region(doc, (u0, v0, u1, v1))
    what = "frames %d-%d" % (start, end) if whole_range else "this frame"
    return "Fitted to %d object(s), %s: %s" % (len(objs), what, describe(doc))


# ═════════════════════════════════════════════════════════════ panel

class Panel(gui.GeDialog):

    def __init__(self):
        self.names = []

    def CreateLayout(self):
        self.SetTitle("Render Regions")
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "", 0)
        self.GroupBorderSpace(6, 6, 6, 6)
        self.GroupSpace(4, 6)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddCheckbox(CK_ON, c4d.BFH_SCALEFIT, 0, 0, "Region On")
        self.AddButton(B_SNIP, c4d.BFH_RIGHT, 0, 0, "Snip (Alt+R)")
        self.GroupEnd()
        self.AddStaticText(T_INFO, c4d.BFH_SCALEFIT, 0, 0, "", c4d.BORDER_THIN_IN)

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 0, "", 0)
        self.AddComboBox(CB_SAVED, c4d.BFH_SCALEFIT, 0, 0)
        self.AddButton(B_SAVE, c4d.BFH_RIGHT, 0, 0, "Save As")
        self.AddButton(B_DELETE, c4d.BFH_RIGHT, 0, 0, "Delete")
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 4, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, 0, 0, "Fit to Selection   Border")
        self.AddEditNumberArrows(E_BORDER, c4d.BFH_LEFT, 60, 0)
        self.AddButton(B_FIT_NOW, c4d.BFH_SCALEFIT, 0, 0, "This Frame")
        self.AddButton(B_FIT_RANGE, c4d.BFH_SCALEFIT, 0, 0, "Frame Range")
        self.GroupEnd()
        self.GroupEnd()
        return True

    def InitValues(self):
        self.SetInt32(E_BORDER, DEFAULT_BORDER, 0, 2000, 5)
        self.refresh()
        return True

    def refresh(self):
        if not self.IsOpen():
            return
        doc = c4d.documents.GetActiveDocument()
        on, _ = get_region(doc)
        self.SetBool(CK_ON, on)
        self.SetString(T_INFO, describe(doc))
        saved = load_saved(doc)
        names = sorted(saved, key=str.lower)
        if names != self.names:
            self.names = names
            self.FreeChildren(CB_SAVED)
            self.AddChild(CB_SAVED, 0, "Saved regions (%d)" % len(names) if names else "No saved regions")
            for i, n in enumerate(names):
                self.AddChild(CB_SAVED, i + 1, n)
            self.SetInt32(CB_SAVED, 0)
        self.Enable(B_DELETE, bool(names))

    def Command(self, cid, msg):
        try:
            doc = c4d.documents.GetActiveDocument()
            if cid == CK_ON:
                set_region_on(doc, self.GetBool(CK_ON))
            elif cid == B_SNIP:
                c4d.CallCommand(CMD_SNIP)
            elif cid == CB_SAVED:
                i = self.GetInt32(CB_SAVED)
                if 1 <= i <= len(self.names):
                    uv = load_saved(doc)[self.names[i - 1]]
                    set_region(doc, tuple(uv))
                    c4d.StatusSetText("Render Region: %s - %s" % (self.names[i - 1], describe(doc)))
            elif cid == B_SAVE:
                on, uv = get_region(doc)
                if not on:
                    c4d.StatusSetText("Render Region: set a region first")
                else:
                    name = gui.InputDialog("Save this render region as:", "Region %d" % (len(self.names) + 1))
                    if name:
                        saved = load_saved(doc)
                        saved[name.strip()] = list(uv)
                        store_saved(doc, saved)
            elif cid == B_DELETE:
                i = self.GetInt32(CB_SAVED)
                if 1 <= i <= len(self.names):
                    saved = load_saved(doc)
                    saved.pop(self.names[i - 1], None)
                    store_saved(doc, saved)
                else:
                    c4d.StatusSetText("Render Region: pick the saved region to delete first")
            elif cid in (B_FIT_NOW, B_FIT_RANGE):
                msg_ = fit_to_selection(doc, self.GetInt32(E_BORDER), cid == B_FIT_RANGE)
                c4d.StatusSetText("Render Region: " + msg_)
            self.refresh()
        except Exception:
            log_error("Panel.Command")
        return True

    def CoreMessage(self, id, msg):
        if id == c4d.EVMSG_CHANGE:
            try:
                self.refresh()
            except Exception:
                log_error("Panel.CoreMessage")
        return gui.GeDialog.CoreMessage(self, id, msg)


PANEL = Panel()


class PanelCommand(plugins.CommandData):
    def Execute(self, doc):
        return PANEL.Open(c4d.DLG_TYPE_ASYNC, PANEL_ID, defaultw=360, defaulth=0)

    def RestoreLayout(self, secret):
        return PANEL.Restore(PANEL_ID, secret)


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    icon = _icon()
    ok = [
        plugins.RegisterObjectPlugin(id=FRAME_ID, str="Render Region Frame", g=RegionFrame,
                                     description="Orenderregionframe", info=c4d.PLUGINFLAG_HIDEPLUGINMENU, icon=icon),
        plugins.RegisterToolPlugin(id=TOOL_ID, str="Snip Render Region", info=c4d.PLUGINFLAG_HIDEPLUGINMENU, icon=icon,
                                   help="Drag a render region in the viewport", dat=TOOL),
        plugins.RegisterCommandPlugin(CMD_SNIP, "Snip Render Region", c4d.PLUGINFLAG_HIDEPLUGINMENU, icon,
                                      "Drag a render region in the viewport (click = off)", SnipCommand()),
        plugins.RegisterCommandPlugin(PANEL_ID, "Render Regions", 0, icon,
                                      "Region on/off, saved regions, fit to selection", PanelCommand()),
    ]
    log("registered", ok)
