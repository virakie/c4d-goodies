"""Axis - move each selected object's axis to the centre of its geometry,
without moving the geometry or its children.

    click        bounding-box centre
    Shift+click  bottom centre

Deliberately left without a keyboard shortcut: a shortcut holding Shift
would always trigger the bottom variant.

Design notes
------------
- The axis keeps its rotation and scale; only its position moves. Rotation
  is a separate decision the user makes, so it is never touched silently.
- Bounds come from the object's own geometry. An object with none of its
  own (a Null, a group) is centred on everything below it instead.
- Editable points are rewritten so the mesh stays put. Generators whose
  result follows their children in world space (Null, Subdivision Surface,
  Boole, Connect, Extrude, Sweep, Loft...) are moved with their children
  pinned. Anything else (a primitive Cube, Symmetry, Lathe, Cloner) would
  change shape if its axis moved, so it is skipped and reported.
- MCOMMAND_AXIS_CENTERPARENT does not exist in 2026, so the maths is here.
"""

import os

import c4d
from c4d import plugins, bitmaps

# --------------------------------------------------------------------------
# Local pick next to the other Goodies (1066610+). Register a real one
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
PLUGIN_ID = 1066614              # kept from "Magic Center"; 1066617 (Axis Bottom) retired

# Generators whose output stays put when the generator moves and its children
# are pinned in world space.
FOLLOW_CHILDREN = {c4d.Onull, c4d.Osds, c4d.Oboole, c4d.Oconnector,
                   c4d.Oextrude, c4d.Osweep, c4d.Oloft, c4d.Ometaball}
for _name in ("Ovolumebuilder", "Ovolumemesher", "Ogroup"):
    if hasattr(c4d, _name):
        FOLLOW_CHILDREN.add(getattr(c4d, _name))


def _shift_held():
    bc = c4d.BaseContainer()
    if c4d.gui.GetInputState(c4d.BFM_INPUT_KEYBOARD, c4d.BFM_INPUT_CHANNEL, bc):
        return bool(bc.GetInt32(c4d.BFM_INPUT_QUALIFIER) & c4d.QSHIFT)
    return False


# ---------------------------------------------------------------- bounds

class Bounds(object):
    def __init__(self):
        self.lo = None
        self.hi = None

    def add(self, p):
        if self.lo is None:
            self.lo, self.hi = c4d.Vector(p), c4d.Vector(p)
            return
        self.lo = c4d.Vector(min(self.lo.x, p.x), min(self.lo.y, p.y), min(self.lo.z, p.z))
        self.hi = c4d.Vector(max(self.hi.x, p.x), max(self.hi.y, p.y), max(self.hi.z, p.z))

    def empty(self):
        return self.lo is None


def _add_points(obj, mg, bounds):
    for p in obj.GetAllPoints():
        bounds.add(mg * p)


def _add_cache(cache, mg, bounds):
    """A generated cache hierarchy; matrices are accumulated by hand because
    cache objects are not in the document."""
    while cache:
        m = mg * cache.GetMl()
        dc = cache.GetDeformCache()
        sub = cache.GetCache()
        if dc is not None:
            _add_points(dc, m, bounds)
        elif sub is not None:
            _add_cache(sub, m, bounds)
        elif isinstance(cache, c4d.PointObject):
            _add_points(cache, m, bounds)
        _add_cache(cache.GetDown(), m, bounds)
        cache = cache.GetNext()


def _own_geometry(op, bounds):
    """The object's own shape: editable points, else its generated cache."""
    mg = op.GetMg()
    if isinstance(op, c4d.PointObject):
        _add_points(op, mg, bounds)          # undeformed, so the axis matches the base mesh
        return
    cache = op.GetCache()
    if cache is not None:
        # Cache matrices are relative to the generator: identity for a
        # primitive, the child's offset for an SDS/Boole result.
        _add_cache(cache, mg, bounds)


def _tree_geometry(op, bounds):
    """Own geometry plus the children's. Children of an input generator
    (SDS, Boole, Extrude...) are already inside its cache, so they are not
    counted twice. BIT_CONTROLOBJECT is no use for this: it is also set on
    plain objects under Nulls."""
    _own_geometry(op, bounds)
    if op.GetInfo() & c4d.OBJECT_INPUT and op.GetCache() is not None:
        return
    child = op.GetDown()
    while child:
        _tree_geometry(child, bounds)
        child = child.GetNext()


def _bounds_for(op):
    b = Bounds()
    _own_geometry(op, b)
    if b.empty():
        child = op.GetDown()
        while child:
            _tree_geometry(child, b)
            child = child.GetNext()
    return b


# ---------------------------------------------------------------- moving

def _children(op):
    out = []
    c = op.GetDown()
    while c:
        out.append(c)
        c = c.GetNext()
    return out


def _move_axis(doc, op, target):
    """Move op's axis to world point `target`; geometry and children stay."""
    mg = op.GetMg()
    new = c4d.Matrix(mg)
    new.off = target
    kids = [(k, k.GetMg()) for k in _children(op)]

    doc.AddUndo(c4d.UNDOTYPE_CHANGE, op)
    for k, _ in kids:
        doc.AddUndo(c4d.UNDOTYPE_CHANGE, k)

    if isinstance(op, c4d.PointObject):
        # Only the offset changes, so tangents (directions) need no rewrite.
        to_local = ~new * mg
        op.SetAllPoints([to_local * p for p in op.GetAllPoints()])
        op.Message(c4d.MSG_UPDATE)

    op.SetMg(new)
    for k, kmg in kids:
        k.SetMg(kmg)


def _movable(op):
    return isinstance(op, c4d.PointObject) or op.GetType() in FOLLOW_CHILDREN


class Axis(plugins.CommandData):

    def Execute(self, doc):
        targets = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE)
        if not targets:
            c4d.StatusSetText("Axis: select an object first")
            return True

        bottom = _shift_held()
        done, skipped = 0, []

        c4d.StopAllThreads()
        doc.StartUndo()
        for op in targets:
            if not _movable(op):
                skipped.append(op.GetName())
                continue
            b = _bounds_for(op)
            if b.empty():
                skipped.append(op.GetName())
                continue
            centre = (b.lo + b.hi) * 0.5
            if bottom:
                centre.y = b.lo.y             # 0 is a valid bottom - no falsy test
            _move_axis(doc, op, centre)
            done += 1
        doc.EndUndo()
        c4d.EventAdd()

        where = "bottom centre" if bottom else "centre"
        msg = "Axis: %d moved to %s" % (done, where)
        if skipped:
            msg += " - skipped %s (would change shape, or no geometry)" % ", ".join(skipped)
        print("[Axis]", msg)
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
        PLUGIN_ID, "Axis", 0, _icon(),
        "Axis to the centre of the geometry (Shift: bottom centre); geometry and children stay put",
        Axis())
