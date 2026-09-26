"""HDRI - browse your HDRI folders as thumbnails, click one to light
the scene with it.

    click a thumbnail    that HDRI goes on the current dome (made if missing):
                         the managed dome selected in the Object Manager,
                         else the newest one
    Shift+click          add it as an extra dome instead of swapping
    right-click          Add as New Dome, Add to / Remove from Favorites,
                         Show in Explorer
    All                  every collection at once - search it to find any HDRI
    < >                  previous / next in the collection
    drop .hdr/.exr       onto the panel from Explorer: applied straight away
    Size                 thumbnail size; the grid always fills the panel width
    Refresh              pick up files added since the panel opened
    Rotation, Exposure   the dome's Rotate on Horizon and Exposure (EV)
    Background           whether the HDRI shows behind the scene (camera
                         visibility) or only lights it

Domes made here are marked and named "HDRI - <name>", so a plain click swaps
the texture instead of piling up lights; Shift+click is the way to stack them. Redshift for now; Octane
is deferred until Octane for C4D is installed.

What counts as an HDRI: every .hdr/.exr is checked by its shape, read from
the file header (no decoding - 1,621 files in 0.14 s). Only 2:1 panoramas
are kept, so square light maps and gobo textures drop out wherever they live,
and so do empty (0-byte, failed-download) files. A header that can't be read
is kept rather than hidden. Results are cached in goodies_hdri/index.json by
size + date, so each file is only ever read once; Refresh re-lists the
folders (milliseconds) and only new or changed files get read or thumbnailed.

Thumbnails: no HDRI pack here ships previews, and decoding a 40 MB EXR in C4D
would freeze the UI, so a background ffmpeg child makes them (0.2-0.4 s each,
EXR through its sRGB transfer, .hdr with a 2.2 gamma), cached in
goodies_hdri/thumbs. Only the collection on screen is generated, three at a
time, on demand: only thumbnails that come on screen are made, newest
request first, so scrolling All or searching fills in what you look at.

Folders, favourites, size: <C4D prefs>/goodies_hdri/config.json.
"""

import collections
import hashlib
import math
import json
import os
import re
import struct
import subprocess
import traceback

import c4d
from c4d import plugins, gui, bitmaps, storage

# --------------------------------------------------------------------------
# Local pick next to the other Goodies (1066610+). Register a real one
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
ID_PANEL = 1066624
MARK_DOME = ID_PANEL             # bool in the dome's container: managed by the HDRI panel

RS_RENDERER = 1036219
RS_LIGHT = 1036751
RS_TYPE, RS_TYPE_DOME = 10000, 4
RS_DOME_TEX = 12000
RS_DOME_EXPOSURE = 12015
RS_DOME_ROTATE = 12026           # radians
RS_CAMERA_VIS = 10042
DOME_PATH = c4d.DescID(c4d.DescLevel(RS_DOME_TEX, 1036765, 0),
                       c4d.DescLevel(c4d.REDSHIFT_FILE_PATH, c4d.DTYPE_STRING, 0))

EXTS = (".hdr", ".exr")
PANO_MIN, PANO_MAX = 1.9, 2.1    # equirectangular panoramas are 2:1
THUMB_W = 320                    # generated width; the grid scales down from it
SIZE_MIN, SIZE_MAX, SIZE_DEFAULT = 100, 320, 180
PAD, GAP, LABEL_H = 6, 8, 16
LABEL_W = 100                    # left column of the controls; "Thumbnails" must fit
BITMAP_CACHE = 240               # decoded thumbnails kept in memory
WORKERS = 3
CREATE_NO_WINDOW = 0x08000000
FAVORITES = "*favorites*"        # the Favorites pseudo-collection
ALL = "*all*"                    # every collection combined

# gadgets
CB_COLL, E_SEARCH, B_ADDROOT, B_REFRESH = 4000, 4001, 4002, 4011
G_SCROLL, UA_GRID = 4003, 4004
B_PREV, B_NEXT = 4005, 4006
SL_ROT, SL_EXP, CK_BG, SL_SIZE = 4007, 4008, 4009, 4012
T_STATUS = 4010

# popup menu entries
M_FAV, M_UNFAV, M_EXPLORER = c4d.FIRST_POPUP_ID, c4d.FIRST_POPUP_ID + 1, c4d.FIRST_POPUP_ID + 2
M_NEWDOME = c4d.FIRST_POPUP_ID + 3

MARK = c4d.Vector(1.0)           # white: grid frames, favourite flag, the dome's icon
ROT_STEP = math.radians(1.0)


def _wrap_pi(a):
    """Any angle into -180..180 degrees (radians in, radians out). Only used
    when reading the dome - the slider's own value is never wrapped, and
    remainder() keeps +180 as +180 instead of flipping it to -180."""
    return math.remainder(a, 2.0 * math.pi)


def log(*args):
    print("[HDRI]", *args)


def prefs_dir(*sub):
    d = os.path.join(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS), "goodies_hdri", *sub)
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:
        pass
    return d


def _log_error(where):
    try:
        with open(os.path.join(prefs_dir(), "errors.log"), "a", encoding="utf-8") as fh:
            fh.write("--- %s\n%s\n" % (where, traceback.format_exc()))
    except Exception:
        pass
    log("error in", where, "- see goodies_hdri/errors.log")


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def _atomic_json(path, data):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except Exception as exc:
        log("could not write %s: %s" % (os.path.basename(path), exc))


# ═════════════════════════════════════════════════════════════ config

DEFAULT_ROOTS = []             # add HDRI folders with + Folder; they are remembered


class Config(object):
    def __init__(self):
        self.path = os.path.join(prefs_dir(), "config.json")
        self.data = {"roots": list(DEFAULT_ROOTS), "collection": "", "ffmpeg": "",
                     "favorites": [], "size": SIZE_DEFAULT}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log("could not read config:", exc)

    def save(self):
        _atomic_json(self.path, self.data)

    def is_favorite(self, path):
        n = _norm(path)
        return any(_norm(f) == n for f in self.data["favorites"])

    def set_favorite(self, path, on):
        n = _norm(path)
        favs = [f for f in self.data["favorites"] if _norm(f) != n]
        if on:
            favs.append(path)
        self.data["favorites"] = favs
        self.save()


CONFIG = Config()


def find_ffmpeg():
    override = CONFIG.data.get("ffmpeg") or ""
    if override and os.path.isfile(override):
        return override
    cand = os.path.expanduser(r"~\scoop\apps\ffmpeg\current\bin\ffmpeg.exe")
    if os.path.isfile(cand):
        return cand
    import shutil
    return shutil.which("ffmpeg") or ""


# ═════════════════════════════════════════════════════════════ library

_HDR_RES = re.compile(rb"([-+][XY])\s+(\d+)\s+([-+][XY])\s+(\d+)")


def image_size(path):
    """(w, h) read from the file header only, or None."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(65536)
    except OSError:
        return None
    if path.lower().endswith(".hdr"):
        m = _HDR_RES.search(head)
        if not m:
            return None
        n1, n2 = int(m.group(2)), int(m.group(4))
        return (n2, n1) if m.group(1)[1:] == b"Y" else (n1, n2)
    if head[:4] != b"\x76\x2f\x31\x01":             # EXR magic
        return None
    i, end = 8, len(head)
    for _ in range(512):                             # bounded: headers hold few attributes
        z = head.find(b"\0", i)
        if z <= i:
            return None                              # end of header, or garbage
        z2 = head.find(b"\0", z + 1)
        if z2 < 0 or z2 + 5 > end:
            return None
        size = struct.unpack_from("<i", head, z2 + 1)[0]
        val = z2 + 5
        if head[i:z] == b"dataWindow" and val + 16 <= end:
            x0, y0, x1, y1 = struct.unpack_from("<4i", head, val)
            return (x1 - x0 + 1, y1 - y0 + 1)
        if size < 0:
            return None
        i = val + size
    return None


class Library(object):
    """Which files are HDRIs, per folder. The per-file verdict is cached by
    path + size + mtime, so a rescan only reads headers of new files."""

    def __init__(self):
        self.path = os.path.join(prefs_dir(), "index.json")
        self.index = {}                 # norm path -> [size, mtime, w, h]
        self.folders = {}               # folder -> [hdri paths]
        self.collections = []           # [(label, folder)]
        self.dirty = False
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.index = json.load(fh)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log("could not read index:", exc)

    def is_hdri(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return False
        if st.st_size == 0:
            return False                              # failed download
        key = _norm(path)
        rec = self.index.get(key)
        if not rec or rec[0] != st.st_size or rec[1] != int(st.st_mtime):
            wh = image_size(path) or (0, 0)
            rec = [st.st_size, int(st.st_mtime), wh[0], wh[1]]
            self.index[key] = rec
            self.dirty = True
        w, h = rec[2], rec[3]
        if not w or not h:
            return True                               # unreadable header: keep, don't hide
        return PANO_MIN <= w / float(h) <= PANO_MAX

    def scan(self, roots):
        """Re-list every root. Returns how many HDRIs are new since last scan."""
        before = set(p for items in self.folders.values() for p in items)
        folders, cols = {}, []
        for root in roots:
            if not os.path.isdir(root):
                continue
            base = root.rstrip("\\/").count(os.sep)
            for folder, dirs, files in os.walk(root):
                if folder.count(os.sep) - base >= 3:
                    dirs[:] = []
                dirs[:] = sorted(d for d in dirs if not d.startswith((".", "_")))
                cands = sorted((f for f in files if f.lower().endswith(EXTS)), key=str.lower)
                items = [os.path.join(folder, f) for f in cands]
                items = [p for p in items if self.is_hdri(p)]
                if not items:
                    continue
                folders[folder] = items
                rel = os.path.relpath(folder, root)
                label = os.path.basename(root) if rel == "." else rel.replace(os.sep, " / ")
                if len(roots) > 1 and rel != ".":
                    label = "%s / %s" % (os.path.basename(root), label)
                cols.append((label, folder))
        cols.sort(key=lambda x: x[0].lower())
        self.folders, self.collections = folders, cols
        if self.dirty:
            _atomic_json(self.path, self.index)
            self.dirty = False
        after = set(p for items in folders.values() for p in items)
        return len(after - before) if before else 0


LIBRARY = Library()


def thumb_path(src):
    try:
        st = os.stat(src)
        key = "%s|%d|%d|%d" % (os.path.normcase(src), st.st_size, int(st.st_mtime), THUMB_W)
    except OSError:
        key = os.path.normcase(src)
    return os.path.join(prefs_dir("thumbs"), hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg")


def thumb_cmd(ffmpeg, src, dst):
    if src.lower().endswith(".exr"):
        return [ffmpeg, "-v", "error", "-y", "-apply_trc", "iec61966_2_1", "-i", src,
                "-vf", "scale=%d:-2" % THUMB_W, "-frames:v", "1", dst]
    g = "gammaval(0.4545)"
    return [ffmpeg, "-v", "error", "-y", "-i", src,
            "-vf", "scale=%d:-2,lutrgb=r=%s:g=%s:b=%s" % (THUMB_W, g, g, g), "-frames:v", "1", dst]


# ═════════════════════════════════════════════════════════════ dome

def _rs_active(doc):
    rd = doc.GetActiveRenderData()
    return rd is not None and rd[c4d.RDATA_RENDERENGINE] == RS_RENDERER


def find_domes(doc):
    """Every dome made by this panel, in Object Manager order."""
    out = []
    op = doc.GetFirstObject()
    guard = 0
    while op and guard < 1000000:
        guard += 1
        if op.GetType() == RS_LIGHT and op.GetDataInstance().GetBool(MARK_DOME):
            out.append(op)
        nxt = op.GetDown()
        if nxt is None:
            while op and op.GetNext() is None:
                op = op.GetUp()
            nxt = op.GetNext() if op else None
        op = nxt
    return out


def find_dome(doc):
    """The current dome: a managed dome selected in the Object Manager,
    else the first (newest - new domes go to the top)."""
    domes = find_domes(doc)
    for d in domes:
        if d.GetBit(c4d.BIT_ACTIVE):
            return d
    return domes[0] if domes else None


def apply_hdri(doc, path, new=False):
    if not _rs_active(doc):
        return "HDRI: Redshift isn't the render engine (Octane support comes later)"
    dome = None if new else find_dome(doc)
    doc.StartUndo()
    if dome is None:
        dome = c4d.BaseObject(RS_LIGHT)
        dome[RS_TYPE] = RS_TYPE_DOME
        dome.GetDataInstance().SetBool(MARK_DOME, True)
        doc.InsertObject(dome)
        doc.AddUndo(c4d.UNDOTYPE_NEW, dome)
        if new:
            doc.SetActiveObject(dome, c4d.SELECTION_NEW)   # becomes the current dome
    else:
        doc.AddUndo(c4d.UNDOTYPE_CHANGE, dome)
    dome.SetParameter(DOME_PATH, path, c4d.DESCFLAGS_SET_NONE)
    dome[c4d.ID_BASELIST_ICON_COLORIZE_MODE] = c4d.ID_BASELIST_ICON_COLORIZE_MODE_CUSTOM
    dome[c4d.ID_BASELIST_ICON_COLOR] = MARK
    dome.SetName("HDRI - %s" % os.path.splitext(os.path.basename(path))[0])
    doc.EndUndo()
    c4d.EventAdd()
    return ("Added dome: %s" if new else "HDRI: %s") % os.path.basename(path)


def dome_path(dome):
    if dome is None:
        return ""
    p = dome.GetParameter(DOME_PATH, c4d.DESCFLAGS_GET_NONE) or ""
    if p.startswith("file:///"):
        p = p[8:]
    return _norm(p.replace("/", os.sep)) if p else ""


# ═════════════════════════════════════════════════════════════ grid

class Grid(gui.GeUserArea):
    """Cells stretch so a row always fills the width exactly: the size slider
    sets the target thumbnail width, the column count follows from it."""

    def __init__(self, panel):
        self.p = panel
        self.width = 400

    def layout(self):
        target = CONFIG.data.get("size", SIZE_DEFAULT)
        usable = max(1, self.width - PAD)
        # round, not floor: cells land nearest the chosen size instead of
        # snapping to one huge (upscaled) column when the panel is narrow
        cols = max(1, int(round(usable / float(target + GAP))))
        cell_w = usable // cols
        draw_w = max(20, cell_w - GAP)
        draw_h = draw_w // 2
        return cols, cell_w, draw_w, draw_h, draw_h + LABEL_H + GAP

    def GetMinSize(self):
        cols, _, _, _, cell_h = self.layout()
        rows = (len(self.p.items) + cols - 1) // cols
        return 0, max(cell_h, rows * cell_h + PAD)

    def Sized(self, w, h):
        if w != self.width:
            self.width = w
            self.LayoutChanged()

    def DrawMsg(self, x1, y1, x2, y2, msg):
        try:
            self.OffScreenOn()
            self.DrawSetPen(c4d.COLOR_BG)
            self.DrawRectangle(x1, y1, x2, y2)
            if not self.p.items:
                self.DrawSetTextCol(c4d.COLOR_TEXT_DISABLED, c4d.COLOR_TRANS)
                if self.p.GetString(E_SEARCH).strip():
                    empty = "Nothing matches the search"
                elif self.p.current == FAVORITES:
                    empty = "No favourites yet - right-click a thumbnail"
                else:
                    empty = "No HDRIs here - pick a collection or + Folder"
                self.DrawText(empty, PAD, PAD)
                return
            cols, cell_w, draw_w, draw_h, cell_h = self.layout()
            first = max(0, (y1 // cell_h) * cols)
            last = min(len(self.p.items), ((y2 // cell_h) + 1) * cols)
            for i in range(first, last):
                src = self.p.items[i]
                x = PAD + (i % cols) * cell_w
                y = PAD + (i // cols) * cell_h
                bmp = self.p.thumb(src)
                if bmp is not None:
                    self.DrawBitmap(bmp, x, y, draw_w, draw_h, 0, 0,
                                    bmp.GetBw(), bmp.GetBh(), c4d.BMP_NORMALSCALED)
                else:
                    self.DrawSetPen(c4d.COLOR_BGEDIT)
                    self.DrawRectangle(x, y, x + draw_w, y + draw_h)
                n = _norm(src)
                if n in self.p.used_paths:
                    self.DrawSetPen(MARK)            # thick: the current dome; thin: other domes
                    for k in range(3 if n == self.p.active_path else 1):
                        self.DrawFrame(x - 1 - k, y - 1 - k, x + draw_w + k, y + draw_h + k)
                if CONFIG.is_favorite(src):
                    self.DrawSetPen(MARK)            # favourite: a corner flag
                    self.DrawRectangle(x + draw_w - 12, y + 3, x + draw_w - 3, y + 12)
                name = os.path.splitext(os.path.basename(src))[0]
                guard = 0
                while name and self.DrawGetTextWidth(name) > draw_w and guard < 200:
                    name = name[:-2]
                    guard += 1
                self.DrawSetTextCol(c4d.COLOR_TEXT, c4d.COLOR_TRANS)
                self.DrawText(name, x, y + draw_h + 2)
        except Exception:
            _log_error("DrawMsg")

    def _hit(self, msg):
        loc = self.Global2Local()
        x = msg.GetInt32(c4d.BFM_INPUT_X) + loc["x"]
        y = msg.GetInt32(c4d.BFM_INPUT_Y) + loc["y"]
        cols, cell_w, _, _, cell_h = self.layout()
        c, r = (x - PAD) // cell_w, (y - PAD) // cell_h
        if 0 <= c < cols and r >= 0:
            i = r * cols + c
            if 0 <= i < len(self.p.items):
                return self.p.items[i]
        return None

    def InputEvent(self, msg):
        try:
            if msg.GetInt32(c4d.BFM_INPUT_DEVICE) != c4d.BFM_INPUT_MOUSE:
                return False
            ch = msg.GetInt32(c4d.BFM_INPUT_CHANNEL)
            if ch == c4d.BFM_INPUT_MOUSELEFT:
                src = self._hit(msg)
                if src:
                    shift = bool(msg.GetInt32(c4d.BFM_INPUT_QUALIFIER) & c4d.QSHIFT)
                    self.p.apply(src, new=shift)
                return True
            if ch == c4d.BFM_INPUT_MOUSERIGHT:
                src = self._hit(msg)
                if src:
                    self.p.context_menu(src)
                return True
            return False
        except Exception:
            _log_error("InputEvent")
            return True


# ═════════════════════════════════════════════════════════════ panel

class Panel(gui.GeDialog):

    def __init__(self):
        self.grid = Grid(self)
        self.choices = []                # combo index -> collection key (folder or FAVORITES)
        self.current = ""
        self.all_items, self.items = [], []
        self.bitmaps = collections.OrderedDict()
        self.queue, self.running = [], []
        self.pending = set()             # sources whose thumbnail is queued or running
        self.ffmpeg = ""
        self.active_path = ""
        self.used_paths = set()          # every HDRI on a managed dome
        self.tpaths = {}                 # src -> thumbnail path (saves a stat per draw)
        self.in_undo = False

    # ------------------------------------------------------------ layout

    def CreateLayout(self):
        self.SetTitle("HDRI")
        self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0, "", 0)
        self.GroupBorderSpace(6, 6, 6, 6)
        self.GroupSpace(4, 4)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 4, 0, "", 0)
        self.AddComboBox(CB_COLL, c4d.BFH_SCALEFIT, 0, 0)
        self.AddEditText(E_SEARCH, c4d.BFH_RIGHT, 170, 0,
                         c4d.EDITTEXT_SEARCHLOOK | c4d.EDITTEXT_ENABLECLEARBUTTON)
        self.AddButton(B_REFRESH, c4d.BFH_RIGHT, 0, 0, "Refresh")
        self.AddButton(B_ADDROOT, c4d.BFH_RIGHT, 0, 0, "+ Folder")
        self.GroupEnd()

        self.ScrollGroupBegin(G_SCROLL, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                              c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT, 0, 220)
        self.AddUserArea(UA_GRID, c4d.BFH_SCALEFIT | c4d.BFV_TOP)
        self.AttachUserArea(self.grid, UA_GRID)
        self.GroupEnd()

        # the dome
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "Rotation")
        self.AddEditSlider(SL_ROT, c4d.BFH_SCALEFIT)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "Exposure")
        self.AddEditSlider(SL_EXP, c4d.BFH_SCALEFIT)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "")
        self.AddCheckbox(CK_BG, c4d.BFH_LEFT, 0, 0, "Show as Background")
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # the browser
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 4, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "Thumbnails")
        self.AddSlider(SL_SIZE, c4d.BFH_SCALEFIT, 80, 0)
        self.AddButton(B_PREV, c4d.BFH_RIGHT, 30, 0, "<")
        self.AddButton(B_NEXT, c4d.BFH_RIGHT, 30, 0, ">")
        self.GroupEnd()

        self.AddStaticText(T_STATUS, c4d.BFH_SCALEFIT, 0, 0, "", c4d.BORDER_THIN_IN)
        self.GroupEnd()
        return True

    def InitValues(self):
        self.SetFloat(SL_ROT, 0.0, -math.pi, math.pi, ROT_STEP, c4d.FORMAT_DEGREE)
        self.SetFloat(SL_EXP, 0.0, -5.0, 5.0, 0.1)
        self.SetInt32(SL_SIZE, int(CONFIG.data.get("size", SIZE_DEFAULT)), SIZE_MIN, SIZE_MAX, 10)
        try:
            self.SetString(E_SEARCH, "Search", False, c4d.EDITTEXT_HELPTEXT)   # grey placeholder
        except Exception:
            _log_error("placeholder")
        self.ffmpeg = find_ffmpeg()
        LIBRARY.scan(CONFIG.data.get("roots", []))
        self._fill_combo()
        self._pull_dome()
        self.SetTimer(100)
        return True

    # ------------------------------------------------------------ library

    def _fill_combo(self, keep=None):
        want = keep if keep is not None else CONFIG.data.get("collection", "")
        self.FreeChildren(CB_COLL)
        self.choices = [FAVORITES, ALL] + [folder for _, folder in LIBRARY.collections]
        total = sum(len(v) for v in LIBRARY.folders.values())
        self.AddChild(CB_COLL, 0, "\u2605 Favorites (%d)" % len(CONFIG.data["favorites"]))
        self.AddChild(CB_COLL, 1, "All (%d)" % total)
        pick = {FAVORITES: 0, ALL: 1}.get(want)
        for i, (label, folder) in enumerate(LIBRARY.collections):
            self.AddChild(CB_COLL, i + 2, label)
            if folder == want:
                pick = i + 2
        if pick is None:
            pick = 2 if len(self.choices) > 2 else 1
        self.SetInt32(CB_COLL, pick)
        self._open(self.choices[pick])

    def _items_for(self, key):
        if key == FAVORITES:
            return [p for p in CONFIG.data["favorites"] if os.path.isfile(p)]
        if key == ALL:
            seen, out = set(), []
            for items in LIBRARY.folders.values():
                for p in items:
                    if _norm(p) not in seen:
                        seen.add(_norm(p))
                        out.append(p)
            return sorted(out, key=lambda p: os.path.basename(p).lower())
        return list(LIBRARY.folders.get(key, []))

    def _open(self, key, keep_bitmaps=False):
        self.current = key
        CONFIG.data["collection"] = key
        CONFIG.save()
        if not keep_bitmaps:
            self.bitmaps.clear()
        # Drop requests from the previous view; running ffmpegs just finish.
        self.queue = []
        self.pending = set(s for _, s in self.running)
        self.all_items = self._items_for(key)
        self._filter()
        self._status()

    def _thumb_path(self, src):
        tp = self.tpaths.get(src)
        if tp is None:
            tp = self.tpaths[src] = thumb_path(src)
        return tp

    def _filter(self):
        q = self.GetString(E_SEARCH).strip().lower()
        self.items = [s for s in self.all_items if q in os.path.basename(s).lower()] if q else list(self.all_items)
        self.grid.LayoutChanged()
        self.grid.Redraw()

    def _refresh_library(self):
        new = LIBRARY.scan(CONFIG.data.get("roots", []))
        self._fill_combo(keep=self.current)
        self._status("%d new HDRI%s" % (new, "" if new == 1 else "s") if new else "No new HDRIs")

    def thumb(self, src):
        """Cached bitmap, or None while its thumbnail is still being made."""
        if src in self.bitmaps:
            self.bitmaps.move_to_end(src)
            got = self.bitmaps[src]
            return None if got is False else got
        if src in self.pending:
            return None
        tp = self._thumb_path(src)
        if not os.path.isfile(tp):
            if self.ffmpeg:
                self.queue.append(src)                # made on demand: it is on screen
                self.pending.add(src)
            return None
        bmp = bitmaps.BaseBitmap()
        ok = bmp.InitWith(tp)
        ok = ok[0] if isinstance(ok, tuple) else ok
        self.bitmaps[src] = bmp if ok == c4d.IMAGERESULT_OK else False
        while len(self.bitmaps) > BITMAP_CACHE:
            self.bitmaps.popitem(last=False)
        return None if self.bitmaps[src] is False else bmp

    # ------------------------------------------------------------ thumbnails

    def Timer(self, msg):
        try:
            done = [r for r in self.running if r[0].poll() is not None]
            for proc, src in done:
                self.running.remove((proc, src))
                self.pending.discard(src)
            if not self.ffmpeg:
                self.queue = []
                self.pending = set()
            while self.queue and len(self.running) < WORKERS:
                src = self.queue.pop()                # newest request first = what is on screen now
                try:
                    proc = subprocess.Popen(thumb_cmd(self.ffmpeg, src, self._thumb_path(src)),
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
                    self.running.append((proc, src))
                except Exception as exc:
                    log("ffmpeg failed:", exc)
                    self.queue = []
                    self.pending = set()
            if done:
                self.grid.Redraw()
                self._status()
        except Exception:
            _log_error("Timer")

    # ------------------------------------------------------------ favourites

    def context_menu(self, src):
        bc = c4d.BaseContainer()
        bc.InsData(M_NEWDOME, "Add as New Dome")
        bc.InsData(0, "")                               # separator
        if CONFIG.is_favorite(src):
            bc.InsData(M_UNFAV, "Remove from Favorites")
        else:
            bc.InsData(M_FAV, "Add to Favorites")
        bc.InsData(M_EXPLORER, "Show in Explorer")
        res = gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        if res == M_NEWDOME:
            self.apply(src, new=True)
        elif res in (M_FAV, M_UNFAV):
            CONFIG.set_favorite(src, res == M_FAV)
            self._fill_combo(keep=self.current)       # favourites count in the combo
            self._status("%s %s" % ("Added" if res == M_FAV else "Removed",
                                    os.path.basename(src)))
        elif res == M_EXPLORER:
            storage.ShowInFinder(src, False)

    # ------------------------------------------------------------ dome

    def apply(self, path, new=False):
        doc = c4d.documents.GetActiveDocument()
        msg = apply_hdri(doc, path, new)
        self._pull_dome()
        self.grid.Redraw()
        self._status(msg)

    def _pull_dome(self):
        doc = c4d.documents.GetActiveDocument()
        dome = find_dome(doc)
        self.active_path = dome_path(dome)
        self.used_paths = set(dome_path(d) for d in find_domes(doc))
        on = dome is not None
        for g in (SL_ROT, SL_EXP, CK_BG):
            self.Enable(g, on)
        if on:
            self.SetFloat(SL_ROT, _wrap_pi(dome[RS_DOME_ROTATE] or 0.0), -math.pi, math.pi, ROT_STEP, c4d.FORMAT_DEGREE)
            self.SetFloat(SL_EXP, dome[RS_DOME_EXPOSURE] or 0.0, -5.0, 5.0, 0.1)
            self.SetBool(CK_BG, bool(dome[RS_CAMERA_VIS]))

    def _tweak(self, pid, value, msg):
        """One undo step per slider drag, not one per pixel."""
        doc = c4d.documents.GetActiveDocument()
        dome = find_dome(doc)
        if dome is None:
            return
        dragging = msg.GetBool(c4d.BFM_ACTION_INDRAG)
        if not self.in_undo:
            doc.StartUndo()
            doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, dome)
            self.in_undo = True
        dome[pid] = value
        if not dragging:
            doc.EndUndo()
            self.in_undo = False
        c4d.EventAdd()

    def _step(self, d):
        if not self.items:
            return
        idx = next((i for i, s in enumerate(self.items) if _norm(s) == self.active_path), -1)
        self.apply(self.items[(idx + d) % len(self.items)])

    # ------------------------------------------------------------ events

    def _status(self, msg=None):
        parts = []
        if msg:
            parts.append(msg)
        shown = len(self.items)
        parts.append("%d HDRI%s" % (shown, "" if shown == 1 else "s") if shown == len(self.all_items)
                     else "%d of %d" % (shown, len(self.all_items)))
        if not self.ffmpeg:
            parts.append("ffmpeg not found - no thumbnails")
        elif self.pending:
            parts.append("making %d thumbnail%s" % (len(self.pending), "" if len(self.pending) == 1 else "s"))
        self.SetString(T_STATUS, "  \u00b7  ".join(parts))

    def Command(self, cid, msg):
        try:
            if cid == CB_COLL:
                i = self.GetInt32(CB_COLL)
                if 0 <= i < len(self.choices):
                    self._open(self.choices[i])
            elif cid == E_SEARCH:
                self._filter()
                self._status()
            elif cid == B_REFRESH:
                self._refresh_library()
            elif cid == B_ADDROOT:
                folder = storage.LoadDialog(flags=c4d.FILESELECT_DIRECTORY, title="Add an HDRI folder")
                if folder and folder not in CONFIG.data["roots"]:
                    CONFIG.data["roots"].append(folder)
                    CONFIG.save()
                    self._refresh_library()
            elif cid == SL_SIZE:
                CONFIG.data["size"] = self.GetInt32(SL_SIZE)
                if not msg.GetBool(c4d.BFM_ACTION_INDRAG):
                    CONFIG.save()
                self.grid.LayoutChanged()
                self.grid.Redraw()
            elif cid == B_PREV:
                self._step(-1)
            elif cid == B_NEXT:
                self._step(1)
            elif cid == SL_ROT:
                self._tweak(RS_DOME_ROTATE, self.GetFloat(SL_ROT), msg)
            elif cid == SL_EXP:
                self._tweak(RS_DOME_EXPOSURE, self.GetFloat(SL_EXP), msg)
            elif cid == CK_BG:
                self._tweak(RS_CAMERA_VIS, self.GetBool(CK_BG), msg)
        except Exception:
            _log_error("Command")
        return True

    def Message(self, msg, result):
        if msg.GetId() == c4d.BFM_DRAGRECEIVE:
            try:
                info = self.GetDragObject(msg)
                files = []
                if info and info.get("type") == c4d.DRAGTYPE_FILES:
                    obj = info.get("object")
                    files = [obj] if isinstance(obj, str) else list(obj or [])
                files = [f for f in files if isinstance(f, str) and f.lower().endswith(EXTS)]
                if not files:
                    return self.SetDragDestination(c4d.MOUSE_FORBIDDEN)
                if msg.GetInt32(c4d.BFM_DRAG_FINISHED):
                    self.apply(files[0])
                    return True
                return self.SetDragDestination(c4d.MOUSE_POINT_HAND)
            except Exception:
                _log_error("Message/drag")
        return gui.GeDialog.Message(self, msg, result)

    def CoreMessage(self, id, msg):
        if id == c4d.EVMSG_CHANGE and not self.in_undo:
            try:
                before = (self.active_path, frozenset(self.used_paths))
                self._pull_dome()
                if before != (self.active_path, frozenset(self.used_paths)):
                    self.grid.Redraw()
            except Exception:
                _log_error("CoreMessage")
        return gui.GeDialog.CoreMessage(self, id, msg)

    def DestroyWindow(self):
        for proc, _ in self.running:
            try:
                proc.kill()
            except Exception:
                pass
        self.running, self.queue = [], []
        self.pending = set()


PANEL = Panel()


class PanelCommand(plugins.CommandData):
    def Execute(self, doc):
        return PANEL.Open(c4d.DLG_TYPE_ASYNC, ID_PANEL, defaultw=620, defaulth=520)

    def RestoreLayout(self, secret):
        return PANEL.Restore(ID_PANEL, secret)


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(
        ID_PANEL, "HDRI", 0, _icon(),
        "Browse HDRI folders as thumbnails; click to put one on the scene's dome",
        PanelCommand())
