"""Snap to Floor - drop the selected objects so their lowest point sits on
the floor (world Y = 0).

    click        snap once (one undo)
    Shift+click  add a live Floor tag that keeps them on the floor while they
                 animate or deform; Shift+click again removes it

Design notes
------------
- The lowest point is taken from what you see: deformed points, generator
  caches, and the children's geometry (a group drops as one piece).
- Only the object's position moves, never its rotation or points.
- If a parent and its child are both selected, only the parent moves.
- The tag runs in the Generators priority, after caches are built, so an
  object that deforms this frame is measured this frame, not the last.
"""

import os

import c4d
from c4d import plugins, bitmaps

# --------------------------------------------------------------------------
# Local picks next to the other Goodies (1066610+). Register real ones
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
PLUGIN_ID = 1066637
TAG_ID = 1066638                 # Tgoodiesfloor
FLOOR_HEIGHT = 1000
EPS = 1e-4


def _shift_held():
    bc = c4d.BaseContainer()
    if c4d.gui.GetInputState(c4d.BFM_INPUT_KEYBOARD, c4d.BFM_INPUT_CHANNEL, bc):
        return bool(bc.GetInt32(c4d.BFM_INPUT_QUALIFIER) & c4d.QSHIFT)
    return False


# ---------------------------------------------------------------- lowest point

def _points_low(obj, mg):
    """Lowest world Y of a point object. When the matrix keeps Y separate
    (no tilt) the bounding box is exact and nothing is looped."""
    if obj.GetPointCount() == 0:
        return None
    if abs(mg.v1.y) < 1e-9 and abs(mg.v3.y) < 1e-9:
        mp, rad = obj.GetMp(), obj.GetRad()
        return mg.off.y + min(mg.v2.y * (mp.y - rad.y), mg.v2.y * (mp.y + rad.y))
    a, b, c, d = mg.v1.y, mg.v2.y, mg.v3.y, mg.off.y
    return min(a * p.x + b * p.y + c * p.z for p in obj.GetAllPoints()) + d


def _low(a, b):
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _cache_low(cache, mg):
    """A generated cache hierarchy; matrices are accumulated by hand because
    cache objects are not in the document."""
    low = None
    while cache:
        m = mg * cache.GetMl()
        dc = cache.GetDeformCache()
        sub = cache.GetCache()
        if dc is not None:
            low = _low(low, _points_low(dc, m))
        elif sub is not None:
            low = _low(low, _cache_low(sub, m))
        elif isinstance(cache, c4d.PointObject):
            low = _low(low, _points_low(cache, m))
        low = _low(low, _cache_low(cache.GetDown(), m))
        cache = cache.GetNext()
    return low


def _own_low(op):
    mg = op.GetMg()
    dc = op.GetDeformCache()
    if dc is not None:
        return _points_low(dc, mg)
    cache = op.GetCache()
    if cache is not None:
        return _cache_low(cache, mg)
    if isinstance(op, c4d.PointObject):
        return _points_low(op, mg)
    return None


def tree_low(op):
    """Own geometry plus the children's. Children of an input generator
    (SDS, Boole, Extrude...) are already inside its cache."""
    low = _own_low(op)
    if op.GetInfo() & c4d.OBJECT_INPUT and op.GetCache() is not None:
        return low
    child = op.GetDown()
    while child:
        low = _low(low, tree_low(child))
        child = child.GetNext()
    return low


def lift(op, dy):
    mg = op.GetMg()
    mg.off = mg.off + c4d.Vector(0, dy, 0)
    op.SetMg(mg)


def top_level(objs):
    chosen = set(o for o in objs)
    out = []
    for o in objs:
        p = o.GetUp()
        while p and p not in chosen:
            p = p.GetUp()
        if p is None:
            out.append(o)
    return out


# ---------------------------------------------------------------- live tag

class FloorTag(plugins.TagData):

    def Init(self, node, isCloneInit=False):
        if not isCloneInit:
            node[FLOOR_HEIGHT] = 0.0
            pd = c4d.PriorityData()
            pd.SetPriorityValue(c4d.PRIORITYVALUE_MODE, c4d.CYCLE_GENERATORS)
            pd.SetPriorityValue(c4d.PRIORITYVALUE_PRIORITY, 0)
            node[c4d.EXPRESSION_PRIORITY] = pd
        return True

    def Execute(self, tag, doc, op, bt, priority, flags):
        try:
            low = tree_low(op)
            if low is not None:
                dy = tag[FLOOR_HEIGHT] - low
                if abs(dy) > EPS:
                    lift(op, dy)
        except Exception as exc:
            print("[Snap to Floor] tag:", exc)
        return c4d.EXECUTIONRESULT_OK


# ---------------------------------------------------------------- command

class SnapFloor(plugins.CommandData):

    def Execute(self, doc):
        objs = top_level(doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN))
        if not objs:
            c4d.StatusSetText("Snap to Floor: select an object first")
            return True
        c4d.StopAllThreads()
        if _shift_held():
            msg = self.toggle_tags(doc, objs)
        else:
            msg = self.snap(doc, objs)
        c4d.EventAdd()
        c4d.StatusSetText("Snap to Floor: " + msg)
        return True

    def snap(self, doc, objs):
        doc.StartUndo()
        done, skipped = 0, []
        for op in objs:
            low = tree_low(op)
            if low is None:
                skipped.append(op.GetName())
                continue
            if abs(low) > EPS:
                doc.AddUndo(c4d.UNDOTYPE_CHANGE, op)
                lift(op, -low)
            done += 1
        doc.EndUndo()
        msg = "%d on the floor" % done
        if skipped:
            msg += " - skipped %s (no geometry)" % ", ".join(skipped)
        return msg

    def toggle_tags(self, doc, objs):
        tags = [op.GetTag(TAG_ID) for op in objs]
        doc.StartUndo()
        if all(tags):
            for t in tags:
                doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, t)
                t.Remove()
            msg = "live floor off for %d" % len(tags)
        else:
            n = 0
            for op, t in zip(objs, tags):
                if t is None:
                    t = c4d.BaseTag(TAG_ID)
                    op.InsertTag(t)
                    doc.AddUndo(c4d.UNDOTYPE_NEWOBJ, t)
                    n += 1
            msg = "live floor on for %d (Shift+click again to remove)" % n
        doc.EndUndo()
        return msg

    def GetState(self, doc):
        return c4d.CMD_ENABLED


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    icon = _icon()
    plugins.RegisterTagPlugin(id=TAG_ID, str="Floor", g=FloorTag, description="Tgoodiesfloor",
                              info=c4d.TAG_VISIBLE | c4d.TAG_EXPRESSION | c4d.PLUGINFLAG_HIDEPLUGINMENU,
                              icon=icon)
    plugins.RegisterCommandPlugin(
        PLUGIN_ID, "Snap to Floor", 0, icon,
        "Drop the selection onto the floor (Shift: live Floor tag that keeps it there)",
        SnapFloor())
