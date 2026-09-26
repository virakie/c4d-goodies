"""Rename - Blender-style F2: one line at the mouse, Enter to rename.

    Q (default key)  pops a small field at the mouse, holding the selected
                     object's name, all selected - just type over it
    Enter            rename      Esc / click away   cancel
    first  v         recent names
    second v         Clean Up, Auto-number duplicates, the syntax below

What you type:
    Rock             one object: plain rename. Several objects: numbered,
                     Rock_01, Rock_02 ... in Object Manager order
    Leg_##           numbered (### = 3 digits)
    *_L / Hero_*     * is the current name: suffix / prefix
    old>new          replace text in every name ("old>" deletes it)

Several objects that already share a name (Rock.1, Rock_03, ...) open as
"Rock_##", so Enter alone renumbers them cleanly.

Clean Up: strips C4D's ".1" copy marks, spaces/dashes to "_", doubled or
trailing separators. Selection, or the whole scene when nothing is selected.

Auto-number duplicates (on by default, remembered): when you duplicate
(Ctrl+drag, copy/paste) C4D names the copy "Cube.1"; it becomes the lowest
free _## among its siblings instead, Cube -> Cube_01, Cube_03 -> Cube_04.
Only objects carrying that C4D copy mark are touched. It never walks the
scene per change - a fresh duplicate is always selected, so only the
selection is looked at (~0.03 ms per event; each document indexed once, 8 ms
per 10,000 objects). Batches over MAX_BATCH new objects (imports) are left alone.
"""

import json
import os
import re

import c4d
from c4d import plugins, gui, bitmaps, storage

# --------------------------------------------------------------------------
# Local picks next to the other Goodies (1066610+). Register real ones
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
ID_RENAME = 1066618              # was the Renamer panel; same id, now the popup
ID_WATCHER = 1066619

# ── Settings ─────────────────────────────────────────────────────────────
PAD = 2              # digits for auto-numbering when siblings don't say otherwise
MAX_BATCH = 50       # more new objects than this at once = an import, not a duplicate
HISTORY = 10         # recent names kept for the first menu

# ── gadgets ──────────────────────────────────────────────────────────────
E_NAME = 2000
B_RECENT = 2001
B_OPTIONS = 2002

M_CLEAN, M_AUTO = c4d.FIRST_POPUP_ID, c4d.FIRST_POPUP_ID + 1
M_RECENT0 = c4d.FIRST_POPUP_ID + 100

DEFAULTS = {"auto_number": True, "history": []}


def log(*args):
    print("[Renamer]", *args)


# ═════════════════════════════════════════════════════════════════ config

def prefs_dir():
    d = os.path.join(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS), "goodies_renamer")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


class Config(object):
    def __init__(self):
        self.path = os.path.join(prefs_dir(), "config.json")
        self.settings = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                blob = json.load(fh)
            for k, v in DEFAULTS.items():
                self.settings[k] = blob.get(k, v)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log("could not read config:", exc)

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.settings, fh, indent=2)
            os.replace(tmp, self.path)
        except Exception as exc:
            log("could not write config:", exc)


CONFIG = Config()


# ═════════════════════════════════════════════════════════════════ naming

HASHES = re.compile(r"#+")
C4D_COPY = re.compile(r"\.\d+$")                   # C4D's own "Cube.1"
NUMBERED = re.compile(r"^(.*?)(?:[._ ](\d+))?$")   # base + optional number


def apply_pattern(pattern, names):
    """New names for `names` (in order) under `pattern`. Pure function."""
    if not pattern:
        return list(names)
    if ">" in pattern:
        old, new = pattern.split(">", 1)
        return [n.replace(old, new) if old else n for n in names]
    run = HASHES.search(pattern)
    out = []
    for i, name in enumerate(names):
        s = pattern.replace("*", name)
        if run:
            s = HASHES.sub(str(i + 1).zfill(len(run.group(0))), s, count=1)
        out.append(s)
    return out


def clean(name):
    s = C4D_COPY.sub("", name.strip())
    s = re.sub(r"[\s\-]+", "_", s)
    s = re.sub(r"_{2,}", "_", s)
    s = s.strip("_.")
    return s or name


def split_number(name):
    m = NUMBERED.match(name)
    base, num = m.group(1), m.group(2)
    return base, (int(num) if num is not None else None), (len(num) if num else 0)


def next_free(op, base):
    """Lowest free _## for `base` among op's siblings, and the padding they use."""
    used, pad = set(), PAD
    first = op.GetUp().GetDown() if op.GetUp() else op.GetDocument().GetFirstObject()
    sib = first
    while sib:
        if sib != op:
            b, n, width = split_number(sib.GetName())
            if b == base and n is not None:
                used.add(n)
                pad = max(pad, width)
        sib = sib.GetNext()
    n = 1
    while n in used:
        n += 1
    return "%s_%s" % (base, str(n).zfill(pad))


# ═════════════════════════════════════════════════════════════════ watcher

def _walk(doc):
    op = doc.GetFirstObject()
    while op:
        yield op
        nxt = op.GetDown()
        if nxt is None:
            while op and op.GetNext() is None:
                op = op.GetUp()
            nxt = op.GetNext() if op else None
        op = nxt


class Watcher(plugins.MessageData):
    """Numbers fresh duplicates. Known objects are tracked per document by
    GUID: an object is 'new' when its GUID was never seen before."""

    def __init__(self):
        self.known = []            # [(doc, set(guid))] - documents compare with ==

    def _known_for(self, doc):
        for d, s in self.known:
            if d == doc:
                return s, False
        s = set(op.GetGUID() for op in _walk(doc))
        self.known = [(d, k) for d, k in self.known if d.IsAlive()]
        self.known.append((doc, s))
        return s, True

    def CoreMessage(self, id, bc):
        if id != c4d.EVMSG_CHANGE or not CONFIG.settings["auto_number"]:
            return True
        doc = c4d.documents.GetActiveDocument()
        if doc is None:
            return True
        try:
            known, fresh_index = self._known_for(doc)
            if fresh_index:
                return True                         # first sight: everything is "old"
            act = doc.GetActiveObject()
            if act is None or act.GetGUID() in known:
                return True                         # the common case, ~0.03 ms
            sel = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE)
            new = [op for op in sel if op.GetGUID() not in known]
            # Children of a pasted hierarchy are new too, but keep their names.
            for op in new:
                for child in _subtree(op):
                    known.add(child.GetGUID())
            if len(new) > MAX_BATCH:
                return True
            renamed = 0
            for op in new:
                name = op.GetName()
                # Only C4D's own copy mark counts. A plain name clash is not
                # proof of a duplicate: scenes are full of identical names,
                # and objects created unselected are never indexed.
                if C4D_COPY.search(name):
                    base, _, _ = split_number(C4D_COPY.sub("", name))
                    op.SetName(next_free(op, base))
                    renamed += 1
            if renamed:
                c4d.EventAdd()
        except Exception as exc:
            log("watcher:", exc)
        return True


def _subtree(op):
    yield op
    child = op.GetDown()
    while child:
        for o in _subtree(child):
            yield o
        child = child.GetNext()


# ═════════════════════════════════════════════════════════════════ popup

def _cursor_pos():
    """Screen position of the mouse (Windows); None elsewhere."""
    try:
        import ctypes
        from ctypes import wintypes
        pt = wintypes.POINT()
        if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
            return pt.x, pt.y
    except Exception:
        pass
    return None


def suggestion(objs):
    """What the field opens with: the name, or Base_## when the selection
    already shares one base name (Rock.1, Rock_03 ...)."""
    if len(objs) == 1:
        return objs[0].GetName()
    bases = set(split_number(C4D_COPY.sub("", o.GetName()))[0] for o in objs)
    if len(bases) == 1:
        return "%s_##" % bases.pop()
    return objs[-1].GetName()


def resolve(text, objs):
    """New names. A plain name typed for several objects gets numbered -
    renaming them all identically is never what you want."""
    if len(objs) > 1 and not any(t in text for t in ("#", "*", ">")):
        text += "_##"
    return apply_pattern(text, [o.GetName() for o in objs])


def rename(doc, objs, names):
    pairs = [(o, n) for o, n in zip(objs, names) if n and n != o.GetName()]
    if not pairs:
        return 0
    doc.StartUndo()
    for op, name in pairs:
        doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, op)
        op.SetName(name)
    doc.EndUndo()
    c4d.EventAdd()
    return len(pairs)


def _key_down(key):
    bc = c4d.BaseContainer()
    if c4d.gui.GetInputState(c4d.BFM_INPUT_KEYBOARD, key, bc):
        return bool(bc.GetInt32(c4d.BFM_INPUT_VALUE))
    return False


_trace = []


def _debug(*parts):
    """First few hundred dialog events of a session, to diagnose key
    handling from outside (goodies_renamer/popup_debug.log)."""
    if len(_trace) >= 300:
        return
    _trace.append(" ".join(str(p) for p in parts))
    try:
        with open(os.path.join(prefs_dir(), "popup_debug.log"), "a", encoding="utf-8") as fh:
            fh.write(_trace[-1] + "\n")
    except Exception:
        pass


class Popup(gui.GeDialog):
    """Enter/Esc are polled, not received: the edit field swallows them and
    they never reach the dialog's Message (tested 2026-09-26). A key counts
    only on an up->down edge, so the key that opened the popup is ignored."""

    def __init__(self):
        self.objs = []
        self.text = ""
        self.prev = {}
        self.had_focus = False
        self.in_menu = False
        self.done = False

    def CreateLayout(self):
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 0, "", 0)
        self.GroupBorderSpace(4, 4, 4, 4)
        self.GroupSpace(3, 0)
        self.AddEditText(E_NAME, c4d.BFH_SCALEFIT, 280, 0)
        self.AddArrowButton(B_RECENT, c4d.BFH_RIGHT, 0, 0, c4d.ARROW_SMALL_DOWN)
        self.AddArrowButton(B_OPTIONS, c4d.BFH_RIGHT, 0, 0, c4d.ARROW_SMALL_DOWN)
        self.GroupEnd()
        return True

    def InitValues(self):
        self.SetString(E_NAME, self.text)
        self.Activate(E_NAME)
        # Select the whole name so typing replaces it (Blender's F2).
        try:
            bc = c4d.BaseContainer(c4d.BFM_EDITFIELD_SETCURSORPOS)
            bc.SetInt32(c4d.BFM_EDITFIELD_SETCURSORPOS, len(self.text))
            bc.SetInt32(c4d.BFM_EDITFIELD_GETBLOCKSTART, 0)
            self.SendMessage(E_NAME, bc)
        except Exception as exc:
            log("select-all:", exc)
        self.prev = {k: _key_down(k) for k in (c4d.KEY_ENTER, c4d.KEY_ESC)}
        self.SetTimer(30)
        return True

    def _edge(self, key):
        down = _key_down(key)
        hit = down and not self.prev.get(key, False)
        self.prev[key] = down
        return hit

    def Timer(self, msg):
        if self.done:
            return
        if self._edge(c4d.KEY_ENTER):
            _debug("timer: enter")
            self._commit()
        elif self._edge(c4d.KEY_ESC):
            _debug("timer: esc")
            self._cancel()

    def _cancel(self):
        self.done = True
        self.SetTimer(0)
        self.Close()

    def _commit(self):
        if self.done:
            return
        self.done = True
        self.SetTimer(0)
        doc = c4d.documents.GetActiveDocument()
        text = self.GetString(E_NAME).strip()
        objs = [o for o in self.objs if o.IsAlive()]
        self.Close()
        if not text or not objs:
            return
        n = rename(doc, objs, resolve(text, objs))
        hist = [h for h in CONFIG.settings["history"] if h != text]
        CONFIG.settings["history"] = [text] + hist[:HISTORY - 1]
        CONFIG.save()
        c4d.StatusSetText("Renamed %d" % n if n else "Names unchanged")

    def _menu(self, bc):
        self.in_menu = True                    # the menu takes focus: not a click-away
        try:
            return gui.ShowPopupDialog(cd=self, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        finally:
            self.in_menu = False

    def Command(self, cid, msg):
        _debug("command", cid)
        if cid == E_NAME and _key_down(c4d.KEY_ENTER):
            self._commit()                        # Enter delivered as the field's commit
            return True
        if cid == B_RECENT:
            bc = c4d.BaseContainer()
            hist = CONFIG.settings["history"]
            if not hist:
                bc.InsData(M_RECENT0, "No recent names&d&")
            for i, h in enumerate(hist):
                bc.InsData(M_RECENT0 + i, h)
            res = self._menu(bc)
            if res >= M_RECENT0 and res - M_RECENT0 < len(hist):
                self.SetString(E_NAME, hist[res - M_RECENT0])
                self.Activate(E_NAME)
        elif cid == B_OPTIONS:
            bc = c4d.BaseContainer()
            bc.InsData(M_CLEAN, "Clean Up Names")
            bc.InsData(M_AUTO, "Auto-number Duplicates" + ("&c&" if CONFIG.settings["auto_number"] else ""))
            bc.InsData(0, "")
            for i, line in enumerate(("Rock  -  several: Rock_01, Rock_02",
                                      "Leg_##  -  numbered",
                                      "*_L  /  Hero_*  -  suffix / prefix",
                                      "old>new  -  replace")):
                bc.InsData(M_RECENT0 + 50 + i, line + "&d&")
            res = self._menu(bc)
            if res == M_CLEAN:
                doc = c4d.documents.GetActiveDocument()
                objs = [o for o in self.objs if o.IsAlive()] or list(_walk(doc))
                self.Close()
                n = rename(doc, objs, [clean(o.GetName()) for o in objs])
                c4d.StatusSetText("Cleaned %d name%s" % (n, "" if n == 1 else "s"))
            elif res == M_AUTO:
                CONFIG.settings["auto_number"] = not CONFIG.settings["auto_number"]
                CONFIG.save()
                c4d.StatusSetText("Auto-number duplicates %s" % ("on" if CONFIG.settings["auto_number"] else "off"))
        return True

    def Message(self, msg, result):
        mid = msg.GetId()
        if mid in (c4d.BFM_GOTFOCUS, c4d.BFM_LOSTFOCUS, c4d.BFM_INPUT):
            _debug("message", mid, msg.GetInt32(c4d.BFM_INPUT_CHANNEL) if mid == c4d.BFM_INPUT else "")
        if mid == c4d.BFM_GOTFOCUS:
            self.had_focus = True
        elif mid == c4d.BFM_LOSTFOCUS and self.had_focus and not self.in_menu and not self.done:
            self._cancel()                        # clicked away: cancel, like Blender
            return True
        if mid == c4d.BFM_INPUT and msg.GetInt32(c4d.BFM_INPUT_DEVICE) == c4d.BFM_INPUT_KEYBOARD:
            ch = msg.GetInt32(c4d.BFM_INPUT_CHANNEL)
            if ch == c4d.KEY_ENTER:
                self._commit()
                return True
            if ch == c4d.KEY_ESC:
                self._cancel()
                return True
        return gui.GeDialog.Message(self, msg, result)


_popup = None


class RenameCommand(plugins.CommandData):

    def Execute(self, doc):
        global _popup
        objs = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE)
        if not objs:
            c4d.StatusSetText("Rename: select objects first")
            return True
        if _popup is not None and _popup.IsOpen():
            _popup.Close()
        # A fresh dialog every time, opened with pluginid 0: passing the
        # command's own id made C4D restore a remembered, empty layout and
        # skip InitValues (tested 2026-09-26).
        _popup = Popup()
        _popup.objs = objs
        _popup.text = suggestion(objs)
        pos = _cursor_pos()
        x, y = (pos[0] - 24, pos[1] - 14) if pos else (-1, -1)
        return _popup.Open(c4d.DLG_TYPE_ASYNC_POPUPEDIT, 0, xpos=x, ypos=y, defaultw=340, defaulth=0)


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterMessagePlugin(ID_WATCHER, "Rename Auto-number", 0, Watcher())
    plugins.RegisterCommandPlugin(
        ID_RENAME, "Rename", 0, _icon(),
        "Rename the selection from one line at the mouse (Name, Name_##, *_L, old>new)",
        RenameCommand())
