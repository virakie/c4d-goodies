"""Solo - isolate the selected objects in the viewport and the render;
press again to put everything back.

    click        solo the selection (and everything under it)
    click again  restore
    Ctrl+click   force a full restore, even of objects you moved to another
                 layer while soloed

How it works
------------
It uses C4D's own layer solo, so the Layer Manager shows exactly what is
going on and nothing in the scene is hidden by hand:
  1. a temporary "SOLO" layer is created,
  2. the selection, all of its children, and every light / environment /
     camera go into it (lights stay on - only objects are soloed),
  3. the layer is soloed, and the document's solo flag is raised.

Measured facts this relies on (C4D 2026):
- Layer solo hides non-solo objects in the render only while the document
  carries NBIT_SOLO_LAYER; the Layer Manager sets it, so this does too.
- It is per object, with no inheritance: children must be in the layer
  themselves, and a light outside it goes dark.
- That flag must never outlive the solo: with it set and no layer soloed,
  the render comes out empty. It is recorded with UNDOTYPE_BITS on the
  document, so Ctrl+Z restores it too.
- Undo does NOT restore a layer's own solo checkbox, so other layers are
  never touched: a layer you soloed yourself stays soloed and simply shows
  alongside the selection.
- Generators outside the solo layer drop their cache and do NOT rebuild when
  the solo ends (a Cube stays invisible until touched), so on exit every
  object that was hidden is marked dirty.

Every object's original layer is remembered (by GUID, in the document, so
it survives save/restart) and restored on exit - unless you moved it to
another layer while soloed, which is kept.
"""

import json
import os

import c4d
from c4d import plugins, bitmaps

# --------------------------------------------------------------------------
# Local pick next to the other Goodies (1066610+). Register a real one
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
PLUGIN_ID = 1066615
STATE_KEY = PLUGIN_ID            # slot in the document container

LAYER_NAME = "SOLO"
LAYER_COLOR = c4d.Vector(0xC7, 0x2C, 0x07) / 255.0      # #c72c07

# Always kept visible so the render still looks like the scene.
KEEP_TYPES = {
    c4d.Olight, c4d.Ocamera, c4d.Osky, c4d.Oenvironment,
    c4d.Obackground, c4d.Oforeground,
    1036751,                     # RS Light (point/spot/area/dome/IES/portal/sun)
    1036754,                     # RS Sun & Sky
    1036757,                     # RS Environment
    1057516,                     # RS Camera
    5102, 5105,                  # Octane Light / HDRI Env. - unverified, Octane not installed
    1030424, 1034624,            # Arnold lights
    1036899,                     # GSG Area Light
}


def _qualifier():
    bc = c4d.BaseContainer()
    if c4d.gui.GetInputState(c4d.BFM_INPUT_KEYBOARD, c4d.BFM_INPUT_CHANNEL, bc):
        return bc.GetInt32(c4d.BFM_INPUT_QUALIFIER)
    return 0


def _walk(first):
    """Every node from `first` on, depth-first, iteratively."""
    op = first
    while op:
        yield op
        nxt = op.GetDown()
        if nxt is None:
            while op and op.GetNext() is None:
                op = op.GetUp()
            nxt = op.GetNext() if op else None
        op = nxt


def _subtree(op):
    yield op
    child = op.GetDown()
    while child:
        for o in _subtree(child):
            yield o
        child = child.GetNext()


def _guid(node):
    """Stable id that survives save/reload. Layers have no GetGUID(), so
    everything uses the MAXON_CREATOR_ID marker instead."""
    uid = node.FindUniqueID(c4d.MAXON_CREATOR_ID)
    if uid is not None:
        return bytes(uid).hex()
    return str(node.GetGUID()) if hasattr(node, "GetGUID") else str(id(node))


def _layers(doc):
    return list(_walk(doc.GetLayerObjectRoot().GetDown()))


# ---------------------------------------------------------------- state

def _load(doc):
    raw = doc.GetDataInstance().GetString(STATE_KEY)
    if not raw:
        return None
    try:
        state = json.loads(raw)
    except ValueError:
        return None
    return state if "layer" in state else None


def _save(doc, state):
    bc = doc.GetDataInstance()
    if state is None:
        bc.RemoveData(STATE_KEY)
    else:
        bc.SetString(STATE_KEY, json.dumps(state))


def _sync_doc_flag(doc, undo=True):
    """Raise the document's solo flag exactly when some layer is soloed."""
    on = any(l[c4d.ID_LAYER_SOLO] for l in _layers(doc))
    if undo:
        doc.AddUndo(c4d.UNDOTYPE_BITS, doc)
    doc.ChangeNBit(c4d.NBIT_SOLO_LAYER,
                   c4d.NBITCONTROL_SET if on else c4d.NBITCONTROL_CLEAR)


# ---------------------------------------------------------------- enter

def _targets(doc, selected):
    out = {}
    for sel in selected:
        for o in _subtree(sel):
            out[_guid(o)] = o
        # A mesh inside an SDS/Boole should still show the generator's result.
        up = sel.GetUp()
        while up:
            if up.GetInfo() & c4d.OBJECT_INPUT:
                out[_guid(up)] = up
            up = up.GetUp()
    for o in _walk(doc.GetFirstObject()):
        if o.GetType() in KEEP_TYPES:
            out[_guid(o)] = o
    return out.values()


def _enter(doc, selected):
    layer = c4d.documents.LayerObject()
    layer.SetName(LAYER_NAME)
    layer[c4d.ID_LAYER_COLOR] = LAYER_COLOR
    layer.InsertUnder(doc.GetLayerObjectRoot())
    doc.AddUndo(c4d.UNDOTYPE_NEW, layer)

    prev = {}
    for o in _targets(doc, selected):
        old = o.GetLayerObject(doc)
        prev[_guid(o)] = _guid(old) if old else None
        doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, o)
        o.SetLayerObject(layer)

    layer[c4d.ID_LAYER_SOLO] = True
    _sync_doc_flag(doc)
    _save(doc, {"layer": _guid(layer), "objects": prev})
    return len(prev)


# ---------------------------------------------------------------- exit

def _rebuild_hidden(doc):
    """Generators hidden by a layer solo drop their cache, and nothing
    rebuilds it when the solo ends: a Cube stays invisible until it is
    moved. Marking every object dirty forces the rebuild. Used after a
    Ctrl+Z'd solo, where Solo cannot know what was hidden."""
    for o in _walk(doc.GetFirstObject()):
        o.SetDirty(c4d.DIRTYFLAGS_DATA)


def _find_layer(doc, guid):
    for l in _layers(doc):
        if _guid(l) == guid:
            return l
    return None


def _exit(doc, state, force):
    layer = _find_layer(doc, state["layer"])
    by_guid = dict((_guid(l), l) for l in _layers(doc))
    prev = state.get("objects", {})

    restored = 0
    for o in _walk(doc.GetFirstObject()):
        g = _guid(o)
        if g not in prev:
            o.SetDirty(c4d.DIRTYFLAGS_DATA)         # see _rebuild_hidden
            continue
        if force or (layer is not None and o.GetLayerObject(doc) == layer):
            doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, o)
            o.SetLayerObject(by_guid.get(prev[g]))     # None = no layer
            restored += 1

    if layer is not None:
        doc.AddUndo(c4d.UNDOTYPE_DELETEOBJ, layer)
        layer.Remove()

    _save(doc, None)
    _sync_doc_flag(doc)
    return restored


class Solo(plugins.CommandData):

    def Execute(self, doc):
        force = bool(_qualifier() & c4d.QCTRL)
        state = _load(doc)
        if state is not None and _find_layer(doc, state["layer"]) is None and not force:
            # The SOLO layer is gone (Ctrl+Z, or deleted by hand): stale.
            _save(doc, None)
            _sync_doc_flag(doc, undo=False)
            _rebuild_hidden(doc)
            state = None
        elif doc.GetDataInstance().GetString(STATE_KEY) and state is None:
            _save(doc, None)                         # unreadable / old format

        doc.StartUndo()
        if state is not None:
            n = _exit(doc, state, force)
            msg = "Solo off - %d restored%s" % (n, " (forced)" if force else "")
        else:
            selected = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE)
            if not selected:
                doc.EndUndo()
                c4d.StatusSetText("Solo: select objects first")
                return True
            n = _enter(doc, selected)
            msg = "Solo on - %d objects in the SOLO layer" % n
        doc.EndUndo()

        c4d.EventAdd()
        print("[Solo]", msg)
        c4d.StatusSetText(msg)
        return True

    def GetState(self, doc):
        state = c4d.CMD_ENABLED
        if doc.GetDataInstance().GetString(STATE_KEY):
            state |= c4d.CMD_VALUE           # pressed while soloed
        return state


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(
        PLUGIN_ID, "Solo", 0, _icon(),
        "Solo the selected objects in viewport and render via a SOLO layer; press again to restore",
        Solo())
