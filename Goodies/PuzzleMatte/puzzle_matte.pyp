"""PuzzleMatte - Redshift puzzle mattes from a list you drag objects into.

Drag objects from the Object Manager into the list (or Add Selected). Two
modes, saved with the scene:

    Per object   one AOV per object, R = G = B = its ID, so each pass is a
                 plain white matte:  Chair -> MATTE_Chair
    RGB packs    three objects per AOV, one per channel:
                 Chair, Table, Lamp -> PM_Chair_Table_Lamp

Drag rows to reorder, Delete to remove, Build to write it to the scene.
Output: Direct (own files, the default), Multi-Pass, or Both. AOV Manager
opens Redshift's.

Build:
- gives each object an RS Object tag with an Object ID override (an existing
  RS Object tag is reused, never doubled up), IDs chosen to avoid any ID the
  scene already uses;
- replaces the AOVs it manages (named PM_...) and leaves every other AOV
  alone.

Conflicts, measured with a real Redshift render (2026-09-26): children inherit
the parent's Object ID, and a child's RS Object tag WITHOUT an ID override
does not interfere - but a child tag WITH its own ID override punches that
child out of the parent's matte. Those are flagged on the row, and Fix
switches their override off.

The list is stored in the scene (object links), so it survives save/reopen.
"""

import os
import re
import traceback

import c4d
from c4d import plugins, gui, bitmaps, storage

# --------------------------------------------------------------------------
# Local pick next to the other Goodies (1066610+). Register a real one
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
ID_PANEL = 1066621
STATE_KEY = ID_PANEL             # sub-container of object links in the document
MARK_MANAGED = ID_PANEL + 1      # bool in a tag's container: PuzzleMatte owns its ID
MARK_CREATED = ID_PANEL + 2      # bool: PuzzleMatte created the tag (delete on removal)
ROW_COUNT = 1                    # in the STATE_KEY container: number of rows
ROW_FIRST = 1000                 # links at 1000, 1001, ...
MODE_KEY = 2                     # in the STATE_KEY container: 1 = RGB packs, else per object
OUTPUT_KEY = 3                   # in the STATE_KEY container: 0 direct, 1 multi-pass, 2 both
RS_AOV_MANAGER = 1038693         # command id of Redshift's AOV Manager

RS_RENDERER = 1036219
RS_OBJECT_TAG = 1036222
ID_OVERRIDE = 1999               # REDSHIFT_OBJECT_OBJECTID_OVERRIDE
ID_VALUE = 1000                  # REDSHIFT_OBJECT_OBJECTID_ID
AOV_PREFIX = "PM_"               # RGB packs
MATTE_PREFIX = "MATTE_"          # per-object white mattes
MANAGED = (AOV_PREFIX, MATTE_PREFIX)
AOV_PUZZLE = 2                   # REDSHIFT_AOV_TYPE_PUZZLE_MATTE
MODE_OBJECT_ID = 1

# gadgets
G_TREE = 3000
B_ADD = 3001
B_FIX = 3002
B_CLEAR = 3003
B_BUILD = 3004
T_STATUS = 3005
CB_MODE = 3006
CB_OUTPUT = 3007
B_AOVMGR = 3008
PER_OBJECT, RGB_PACKS = 0, 1
DIRECT, MULTIPASS, BOTH = 0, 1, 2

# tree columns
COL_CH, COL_NAME, COL_NOTE = 1, 2, 3
CHANNELS = (("R", c4d.Vector(0.85, 0.2, 0.15)),
            ("G", c4d.Vector(0.25, 0.75, 0.3)),
            ("B", c4d.Vector(0.25, 0.45, 0.95)))


def log(*args):
    print("[PuzzleMatte]", *args)


def _log_error(where):
    """UI callbacks fail silently in the Console-less case; keep a trail."""
    try:
        d = os.path.join(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS), "goodies_puzzlematte")
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "errors.log"), "a", encoding="utf-8") as fh:
            fh.write("--- %s\n%s\n" % (where, traceback.format_exc()))
    except Exception:
        pass
    log("error in", where, "- see goodies_puzzlematte/errors.log")


# ═════════════════════════════════════════════════════════════ scene helpers

def _walk(first):
    op = first
    while op:
        yield op
        nxt = op.GetDown()
        if nxt is None:
            while op and op.GetNext() is None:
                op = op.GetUp()
            nxt = op.GetNext() if op else None
        op = nxt


def _descendants(op):
    child = op.GetDown()
    while child:
        yield child
        for o in _descendants(child):
            yield o
        child = child.GetNext()


def load_rows(doc):
    # An explicit count, never "probe until the end": a missing id reads as
    # DA_NIL, not NOTOK, and an open-ended loop froze C4D once.
    bc = doc.GetDataInstance().GetContainer(STATE_KEY)
    rows = []
    for i in range(max(0, min(bc.GetInt32(ROW_COUNT), 10000))):
        op = bc.GetObjectLink(ROW_FIRST + i, doc)
        if op is not None and op not in rows:
            rows.append(op)
    return rows


def load_mode(doc):
    bc = doc.GetDataInstance().GetContainer(STATE_KEY)
    return RGB_PACKS if bc.GetInt32(MODE_KEY) == RGB_PACKS else PER_OBJECT


def load_output(doc):
    v = doc.GetDataInstance().GetContainer(STATE_KEY).GetInt32(OUTPUT_KEY)
    return v if v in (DIRECT, MULTIPASS, BOTH) else DIRECT


def group_size(mode):
    return 3 if mode == RGB_PACKS else 1


def save_rows(doc, rows, mode=None, output=None):
    if mode is None:
        mode = load_mode(doc)
    if output is None:
        output = load_output(doc)
    bc = c4d.BaseContainer()
    bc.SetInt32(MODE_KEY, mode)
    bc.SetInt32(OUTPUT_KEY, output)
    bc.SetInt32(ROW_COUNT, len(rows))
    for i, op in enumerate(rows):
        bc.SetLink(ROW_FIRST + i, op)
    doc.GetDataInstance().SetContainer(STATE_KEY, bc)


def conflicts(op, rows):
    """Children whose own RS tag overrides the Object ID (and are not rows
    themselves - a nested row is a deliberate, separate matte)."""
    out = []
    stack = [op.GetDown()] if op.GetDown() else []
    while stack:
        o = stack.pop()
        while o:
            if o in rows:
                o = o.GetNext()
                continue                      # skip a nested row and its subtree
            t = o.GetTag(RS_OBJECT_TAG)
            if t is not None and t[ID_OVERRIDE]:
                out.append(t)
            if o.GetDown():
                stack.append(o.GetDown())
            o = o.GetNext()
    return out


def clean_name(name):
    s = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_")
    return s or "obj"


# ═════════════════════════════════════════════════════════════ build

def _rs_videopost(doc):
    rd = doc.GetActiveRenderData()
    if rd is None or rd[c4d.RDATA_RENDERENGINE] != RS_RENDERER:
        return None
    return c4d.redshift.FindAddVideoPost(rd, c4d.redshift.VPrsrenderer)


def build(doc, rows, mode=PER_OBJECT, output=DIRECT):
    vp = _rs_videopost(doc)
    if vp is None:
        return "Redshift is not the render engine - nothing built"

    doc.StartUndo()
    # 1. tags on objects that left the list
    for op in _walk(doc.GetFirstObject()):
        t = op.GetTag(RS_OBJECT_TAG)
        if t is None or op in rows or not t.GetDataInstance().GetBool(MARK_MANAGED):
            continue
        if t.GetDataInstance().GetBool(MARK_CREATED):
            doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, t)
            t.Remove()
        else:
            doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, t)
            t[ID_OVERRIDE] = False
            t.GetDataInstance().RemoveData(MARK_MANAGED)

    # 2. IDs nobody else uses
    taken = set()
    for op in _walk(doc.GetFirstObject()):
        t = op.GetTag(RS_OBJECT_TAG)
        if t is not None and t[ID_OVERRIDE] and op not in rows:
            taken.add(t[ID_VALUE])
    ids, n = [], 1
    while len(ids) < len(rows):
        if n not in taken:
            ids.append(n)
        n += 1
    unused = max(taken | set(ids) | {0}) + 1000   # for the empty channels of a last, short matte

    # 3. tag every row
    for op, oid in zip(rows, ids):
        t = op.GetTag(RS_OBJECT_TAG)
        if t is None:
            t = op.MakeTag(RS_OBJECT_TAG)
            doc.AddUndo(c4d.UNDOTYPE_NEW, t)
            t.GetDataInstance().SetBool(MARK_CREATED, True)
        else:
            doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, t)
        t.GetDataInstance().SetBool(MARK_MANAGED, True)
        t[ID_OVERRIDE] = True
        t[ID_VALUE] = oid
    doc.EndUndo()

    # 4. AOVs - ours replaced, everyone else's kept
    aovs = [a for a in c4d.redshift.RendererGetAOVs(vp)
            if not (a.GetParameter(c4d.REDSHIFT_AOV_TYPE) == AOV_PUZZLE and
                    (a.GetParameter(c4d.REDSHIFT_AOV_NAME) or "").startswith(MANAGED))]
    names = set()
    size = group_size(mode)
    for g in range(0, len(rows), size):
        trio = list(zip(rows[g:g + size], ids[g:g + size]))
        prefix = MATTE_PREFIX if size == 1 else AOV_PREFIX
        name = prefix + "_".join(clean_name(op.GetName()) for op, _ in trio)
        base, k = name, 2
        while name in names:
            name = "%s_%d" % (base, k)
            k += 1
        names.add(name)
        if size == 1:
            ch = [trio[0][1]] * 3                    # white matte: every channel the same ID
        else:
            ch = [oid for _, oid in trio] + [unused] * (3 - len(trio))
        aov = c4d.redshift.RSAOV()
        aov.SetParameter(c4d.REDSHIFT_AOV_TYPE, AOV_PUZZLE)
        aov.SetParameter(c4d.REDSHIFT_AOV_NAME, name)
        aov.SetParameter(c4d.REDSHIFT_AOV_ENABLED, True)
        aov.SetParameter(c4d.REDSHIFT_AOV_FILE_ENABLED, output in (DIRECT, BOTH))
        aov.SetParameter(c4d.REDSHIFT_AOV_MULTIPASS_ENABLED, output in (MULTIPASS, BOTH))
        aov.SetParameter(c4d.REDSHIFT_AOV_PUZZLE_MATTE_MODE, MODE_OBJECT_ID)
        aov.SetParameter(c4d.REDSHIFT_AOV_PUZZLE_MATTE_RED_ID, ch[0])
        aov.SetParameter(c4d.REDSHIFT_AOV_PUZZLE_MATTE_GREEN_ID, ch[1])
        aov.SetParameter(c4d.REDSHIFT_AOV_PUZZLE_MATTE_BLUE_ID, ch[2])
        aovs.append(aov)
    c4d.redshift.RendererSetAOVs(vp, aovs)
    c4d.EventAdd()

    mattes = (len(rows) + size - 1) // size
    if not rows:
        return "Cleared - PuzzleMatte AOVs and tags removed"
    return "Built %d matte%s from %d objects (IDs %d-%d)" % (
        mattes, "" if mattes == 1 else "s", len(rows), ids[0], ids[-1])


# ═════════════════════════════════════════════════════════════ tree

class Rows(gui.TreeViewFunctions):
    """The rows are the scene objects themselves, so a drag from the Object
    Manager and a drag within the list are the same DRAGTYPE_ATOMARRAY."""

    def __init__(self, panel):
        self.p = panel

    # -- structure
    def GetFirst(self, root, ud):
        return self.p.rows[0] if self.p.rows else None

    def GetNext(self, root, ud, obj):
        i = self._index(obj)
        return self.p.rows[i + 1] if 0 <= i < len(self.p.rows) - 1 else None

    def GetPred(self, root, ud, obj):
        i = self._index(obj)
        return self.p.rows[i - 1] if i > 0 else None

    def GetDown(self, root, ud, obj):
        return None

    def IsOpened(self, root, ud, obj):
        return False

    def _index(self, obj):
        for i, o in enumerate(self.p.rows):
            if o == obj:
                return i
        return -1

    # -- selection
    def IsSelected(self, root, ud, obj):
        return any(o == obj for o in self.p.selected)

    def Select(self, root, ud, obj, mode):
        if mode == c4d.SELECTION_NEW:
            self.p.selected = [obj]
        elif mode == c4d.SELECTION_ADD and not self.IsSelected(root, ud, obj):
            self.p.selected.append(obj)
        elif mode == c4d.SELECTION_SUB:
            self.p.selected = [o for o in self.p.selected if o != obj]

    # -- look
    def GetName(self, root, ud, obj):
        return obj.GetName()

    def GetColumnWidth(self, root, ud, obj, col, area):
        return {COL_CH: 44, COL_NOTE: 150}.get(col, 120)

    def GetBackgroundColor(self, root, ud, obj, line, col):
        try:
            if (self._index(obj) // group_size(self.p.mode)) % 2:
                return c4d.Vector(0.20, 0.20, 0.21)      # every other matte, faintly lighter
        except Exception:
            _log_error("GetBackgroundColor")
        return None

    def DrawCell(self, root, ud, obj, col, drawinfo, bgColor):
        try:
            ua = drawinfo["frame"]
            x, y = drawinfo["xpos"], drawinfo["ypos"]
            h = drawinfo.get("height", 16)
            i = self._index(obj)
            if col == COL_CH:
                if self.p.mode == RGB_PACKS:
                    label, colour = CHANNELS[i % 3]
                    text = "%d%s" % (i // 3 + 1, label)
                else:
                    colour, text = c4d.Vector(0.92), "%d" % (i + 1)
                ua.DrawSetPen(colour)
                ua.DrawRectangle(x + 4, y + 3, x + 14, y + h - 4)
                ua.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
                ua.DrawText(text, x + 18, y + 1)
            elif col == COL_NOTE:
                bad = self.p.conflicts.get(i, [])
                if bad:
                    ua.DrawSetTextCol(c4d.Vector(1.0, 0.55, 0.2), c4d.COLOR_TRANS)
                    ua.DrawText("%d child ID override%s" % (len(bad), "" if len(bad) == 1 else "s"), x + 2, y + 1)
        except Exception:
            _log_error("DrawCell")

    def EmptyText(self, root, ud):
        return "Drag objects here from the Object Manager"

    # -- drag & drop
    def DragStart(self, root, ud, obj):
        return c4d.TREEVIEW_DRAGSTART_ALLOW | c4d.TREEVIEW_DRAGSTART_SELECT

    def GetDragType(self, root, ud, obj):
        return c4d.DRAGTYPE_ATOMARRAY

    def GenerateDragArray(self, root, ud, obj):
        return list(self.p.selected) or [obj]

    def AcceptDragObject(self, root, ud, obj, dragtype, dragobject):
        try:
            if dragtype != c4d.DRAGTYPE_ATOMARRAY or not dragobject:
                return 0, False
            if not all(isinstance(o, c4d.BaseObject) for o in dragobject):
                return 0, False
            if obj is None:
                return c4d.INSERT_UNDER, False       # empty list / below the last row
            return c4d.INSERT_BEFORE | c4d.INSERT_AFTER, False
        except Exception:
            _log_error("AcceptDragObject")
            return 0, False

    def InsertObject(self, root, ud, obj, dragtype, dragobject, insertmode, bCopy):
        try:
            moving = [o for o in dragobject if isinstance(o, c4d.BaseObject)]
            rows = [o for o in self.p.rows if not any(o == m for m in moving)]
            if obj is None or insertmode == c4d.INSERT_UNDER:
                at = len(rows)
            else:
                at = next((i for i, o in enumerate(rows) if o == obj), len(rows))
                if insertmode == c4d.INSERT_AFTER:
                    at += 1
            self.p.set_rows(rows[:at] + moving + rows[at:])
        except Exception:
            _log_error("InsertObject")

    def DeletePressed(self, root, ud):
        self.p.set_rows([o for o in self.p.rows if not self.IsSelected(root, ud, o)])
        self.p.selected = []


# ═════════════════════════════════════════════════════════════ panel

class Panel(gui.GeDialog):

    def __init__(self):
        self.rows, self.selected, self.conflicts = [], [], {}
        self.mode = PER_OBJECT
        self.output = DIRECT
        self.doc = None
        self.tree = None
        self.funcs = Rows(self)

    def CreateLayout(self):
        self.SetTitle("PuzzleMatte")
        self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0, "", 0)
        self.GroupBorderSpace(6, 6, 6, 6)
        self.GroupSpace(4, 4)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 0, "", 0)
        self.AddComboBox(CB_MODE, c4d.BFH_SCALEFIT, 0, 0)
        self.AddChild(CB_MODE, PER_OBJECT, "Per object - one white matte each")
        self.AddChild(CB_MODE, RGB_PACKS, "RGB packs - three objects per AOV")
        self.AddComboBox(CB_OUTPUT, c4d.BFH_RIGHT, 110, 0)
        self.AddChild(CB_OUTPUT, DIRECT, "Direct")
        self.AddChild(CB_OUTPUT, MULTIPASS, "Multi-Pass")
        self.AddChild(CB_OUTPUT, BOTH, "Both")
        self.AddButton(B_AOVMGR, c4d.BFH_RIGHT, 0, 0, "AOV Manager")
        self.GroupEnd()
        bc = c4d.BaseContainer()
        bc.SetBool(c4d.TREEVIEW_BORDER, c4d.BORDER_THIN_IN)
        bc.SetBool(c4d.TREEVIEW_OUTSIDE_DROP, True)
        bc.SetBool(c4d.TREEVIEW_HAS_HEADER, False)
        bc.SetBool(c4d.TREEVIEW_HIDE_LINES, True)
        bc.SetBool(c4d.TREEVIEW_HIDE_HIERARCHY_LINES, True)
        bc.SetBool(c4d.TREEVIEW_NOENTERRENAME, True)
        bc.SetBool(c4d.TREEVIEW_EMPTY_TEXT_GREYED_OUT, True)
        self.tree = self.AddCustomGui(G_TREE, c4d.CUSTOMGUI_TREEVIEW, "",
                                      c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 300, 180, bc)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 5, 0, "", 0)
        self.AddButton(B_ADD, c4d.BFH_LEFT, 0, 0, "Add Selected")
        self.AddButton(B_FIX, c4d.BFH_LEFT, 0, 0, "Fix Conflicts")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "", 0)
        self.AddButton(B_CLEAR, c4d.BFH_RIGHT, 0, 0, "Clear")
        self.AddButton(B_BUILD, c4d.BFH_RIGHT, 0, 0, "Build")
        self.GroupEnd()
        self.AddStaticText(T_STATUS, c4d.BFH_SCALEFIT, 0, 0, "", c4d.BORDER_THIN_IN)
        self.GroupEnd()
        return True

    def InitValues(self):
        layout = c4d.BaseContainer()
        layout.SetInt32(COL_CH, c4d.LV_USER)
        layout.SetInt32(COL_NAME, c4d.LV_TREE)
        layout.SetInt32(COL_NOTE, c4d.LV_USER)
        self.tree.SetLayout(3, layout)
        self.tree.SetRoot(self, self.funcs, None)
        self._sync(force=True)
        return True

    # ------------------------------------------------------------ state

    def set_rows(self, rows):
        self.rows = rows
        doc = c4d.documents.GetActiveDocument()
        save_rows(doc, rows, self.mode, self.output)
        self._refresh()

    def _sync(self, force=False):
        """Follow the active document; drop rows whose object was deleted."""
        doc = c4d.documents.GetActiveDocument()
        if force or doc != self.doc:
            self.doc = doc
            self.selected = []
        self.rows = load_rows(doc)
        self.mode = load_mode(doc)
        self.output = load_output(doc)
        self.SetInt32(CB_MODE, self.mode)
        self.SetInt32(CB_OUTPUT, self.output)
        self._refresh()

    def _refresh(self):
        self.conflicts = {}
        for i, op in enumerate(self.rows):
            bad = conflicts(op, self.rows)
            if bad:
                self.conflicts[i] = bad
        n_bad = sum(len(v) for v in self.conflicts.values())
        self.Enable(B_FIX, n_bad > 0)
        if self.tree:
            self.tree.Refresh()
        if not self.rows:
            self._status("Drag objects in - " + ("every 3 rows become one R/G/B matte"
                         if self.mode == RGB_PACKS else "each becomes its own white matte"))
        elif n_bad:
            self._status("%d child tag%s override the Object ID - Fix Conflicts" % (n_bad, "" if n_bad == 1 else "s"))
        else:
            size = group_size(self.mode)
            mattes = (len(self.rows) + size - 1) // size
            self._status("%d object%s -> %d matte%s" % (len(self.rows), "" if len(self.rows) == 1 else "s",
                                                        mattes, "" if mattes == 1 else "s"))

    def _status(self, msg):
        self.SetString(T_STATUS, msg)

    # ------------------------------------------------------------ events

    def Command(self, cid, msg):
        try:
            doc = c4d.documents.GetActiveDocument()
            if cid == B_ADD:
                add = [o for o in doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE) if o not in self.rows]
                self.set_rows(self.rows + add)
            elif cid == B_FIX:
                doc.StartUndo()
                n = 0
                for tags in self.conflicts.values():
                    for t in tags:
                        doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, t)
                        t[ID_OVERRIDE] = False
                        n += 1
                doc.EndUndo()
                c4d.EventAdd()
                self._refresh()
                self._status("Fixed %d child tag%s" % (n, "" if n == 1 else "s"))
            elif cid == CB_MODE:
                self.mode = self.GetInt32(CB_MODE)
                self.set_rows(self.rows)             # saves the mode with the scene
            elif cid == CB_OUTPUT:
                self.output = self.GetInt32(CB_OUTPUT)
                self.set_rows(self.rows)
            elif cid == B_AOVMGR:
                c4d.CallCommand(RS_AOV_MANAGER)
            elif cid == B_CLEAR:
                self.set_rows([])
            elif cid == B_BUILD:
                msg_ = build(doc, self.rows, self.mode, self.output)
                log(msg_)
                self._refresh()
                self._status(msg_)
                c4d.StatusSetText(msg_)
        except Exception:
            _log_error("Command")
        return True

    def CoreMessage(self, id, msg):
        if id == c4d.EVMSG_CHANGE:
            try:
                self._sync()
            except Exception:
                _log_error("CoreMessage")
        return gui.GeDialog.CoreMessage(self, id, msg)


PANEL = Panel()


class PanelCommand(plugins.CommandData):
    def Execute(self, doc):
        return PANEL.Open(c4d.DLG_TYPE_ASYNC, ID_PANEL, defaultw=380, defaulth=320)

    def RestoreLayout(self, secret):
        return PANEL.Restore(ID_PANEL, secret)


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(
        ID_PANEL, "PuzzleMatte", 0, _icon(),
        "Redshift puzzle mattes: drag objects in; one white matte each, or R/G/B packs of three",
        PanelCommand())
