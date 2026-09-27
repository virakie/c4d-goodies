"""Library - one thumbnail browser for your own asset folders: PBR materials,
HDRIs, imperfection maps, light maps, gobos, IES profiles. Redshift.

    Kind / collection    pick what to browse; All shows every collection
    click                Materials: build it (once) and put it on the selected
                         objects. HDRI: on the current dome. Imperfection:
                         into the selected material's roughness. Light map /
                         gobo / IES: onto the selected light, else a new one
    Shift+click          the same, but always new (new material, dome, light)
    drag a material      onto objects in the viewport or Object Manager
    right-click          the other options, Favorites, Show in Explorer
    drop .hdr/.exr       onto the panel from Explorer: goes on the dome
    right-click a slider resets it

Nothing happens at C4D startup. The first time the panel opens it lists
your folders in a background thread (the UI never waits); after that the
index is cached in goodies_library/index.json with each folder's date, so a
rescan only stats folders and re-reads the ones that changed. Refresh
rescans; Shift+Refresh re-reads everything.

What is what, by file name and folder:
    material    a folder (or a group of files sharing a name) holding at least
                two PBR maps, one of them normal or roughness. Tokens like
                basecolor/albedo/diffuse, roughness/gloss, metallic, normal,
                height/displacement, opacity, transmission; 1k/2k/4k/8k
                variants collapse to one (4K preferred). A "preview" image
                becomes the thumbnail.
    HDRI        a 2:1 .hdr/.exr (read from the file header only)
    by folder   imperfection/grunge/scratch/smudge/dust -> Imperfections,
                lightmap/lighthit -> Light Maps, gobo -> Gobos, bokeh -> Bokeh
                (onto the scene camera's RS Camera tag)
    IES         .ies files; a same-named .png next to one is its thumbnail
    LUT         .cube files, plus the ones Redshift ships with C4D; clicked, they go
                on the camera (RS Camera: Color Correction > LUT) with Strength and
                Log controls. Thumbnails: a Poly Haven (CC0) photo through the LUT
    anything else is left out. When the guess is wrong: + Folder asks what
    the folder holds, and right-click > "This folder is" re-sorts one.
Image sequences (name_000, _001 ... 24+ frames, not HDR/EXR) show as one item.

Materials are OpenPBR: colour maps through a Color Correct, grey maps
through a gradient Ramp, one UV Context Projection on the output for tiling.

Thumbnails are made by a background ffmpeg child, on demand, only for what
is on screen, and cached in goodies_library/thumbs.

Settings: <C4D prefs>/goodies_library/config.json.
"""

import collections
import ctypes
import ctypes.wintypes
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import traceback

import c4d
import maxon
from c4d import plugins, gui, bitmaps, storage

# --------------------------------------------------------------------------
# Local picks next to the other Goodies (1066610+). Register real ones at
# developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
ID_LIBRARY = 1066639
ID_HDRI = 1066624                # the old HDRI panel's command: opens Library on HDRI
MARK_DOME = ID_HDRI              # bool on domes made here (kept from the HDRI panel)
MARK = ID_LIBRARY                # string on materials (asset id) / bool on lights and tags

NS = maxon.Id("com.redshift3d.redshift4c4d.class.nodespace")
RSN = "com.redshift3d.redshift4c4d.nodes.core."
RS_RENDERER = 1036219
RS_LIGHT = 1036751
RS_TYPE = 10000
RS_SPOT, RS_AREA, RS_DOME, RS_IES = 2, 3, 4, 5
RS_LIGHT_TEX, RS_DOME_TEX, RS_IES_FILE = 11001, 12000, 13000
RS_DOME_EXPOSURE, RS_DOME_ROTATE, RS_CAMERA_VIS = 12015, 12026, 10042
RS_FILE_TYPE = 1036765


def rs_path_id(pid):
    return c4d.DescID(c4d.DescLevel(pid, RS_FILE_TYPE, 0),
                      c4d.DescLevel(c4d.REDSHIFT_FILE_PATH, c4d.DTYPE_STRING, 0))


# kinds, in combo order
MATERIAL, HDRI, IMPERF, LIGHTMAP, GOBO, BOKEH, IES, LUT = "material", "hdri", "imperfection", "lightmap", "gobo", "bokeh", "ies", "lut"
TEXTURE = "texture"              # an image nothing claimed: indexed, not shown unless a folder is set to a kind
KINDS = [(MATERIAL, "Materials"), (HDRI, "HDRI"), (IMPERF, "Imperfections"), (LIGHTMAP, "Light Maps"),
         (GOBO, "Gobos"), (BOKEH, "Bokeh"), (IES, "IES"), (LUT, "LUTs")]
KIND_NAME = dict(KINDS)
ARROW_KINDS = (HDRI, LUT, BOKEH, LIGHTMAP, GOBO, IES)           # swapped in place, so arrow keys can step them
IMAGE_KINDS = (HDRI, IMPERF, LIGHTMAP, GOBO, BOKEH, TEXTURE)   # single images: a folder setting can re-sort them
RS_CAMTAG, RS_BOKEH_USE, RS_BOKEH_IMAGE = 1036760, 11008, 11010   # RS Camera tag on a C4D camera
RS_CAMERA, RS_CAM_BOKEH, RS_CAM_BOKEH_IMAGE = 1057516, 8002, 8003  # the RS Camera object
RS_CAM_DIAPHRAGM, RS_DIAPHRAGM_CIRCULAR, RS_DIAPHRAGM_IMAGE = 1300, 0, 2   # Circular / Bladed / Image
# LUT: shared ids on the RS Camera object (Color Correction > LUT) and the RS Camera tag
LUT_FILE, LUT_LOG, LUT_STRENGTH = 12401, 12402, 12404
LUT_CAM_MODE, LUT_CAM_OFF, LUT_CAM_OVERRIDE = 12407, 1, 2            # object: Off / Render Settings / Override
LUT_TAG_ENABLED, LUT_TAG_OVERRIDE = 12400, 12405                      # tag: two checkboxes
LUT_EXTS = (".cube",)
LUT_SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "res", "lut_sample.jpg")  # Poly Haven "autumn_park" (CC0)


def builtin_roots():
    """Folders every user has: the LUTs that ship with Redshift inside C4D."""
    d = os.path.join(storage.GeGetStartupPath(), "Redshift", "res", "core", "Data", "LUT")
    return [d] if os.path.isdir(d) else []

IMG_EXTS = (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".exr", ".hdr", ".tga", ".webp", ".bmp")
HDR_EXTS = (".hdr", ".exr")
PANO_MIN, PANO_MAX = 1.9, 2.1
MAX_DEPTH = 8
INDEX_VERSION = 4                # bump whenever classify_folder changes: cached folders are otherwise never re-read

# file-name token -> PBR channel
CHANNELS = {}
for _ch, _toks in {
    "color": "basecolor albedo diffuse diff col color basecol",
    "rough": "roughness rough rgh",
    "gloss": "glossiness gloss",
    "metal": "metallic metalness metal met",
    "normal": "normal nrm nor normalgl nrml",
    "normaldx": "normaldx",
    "height": "height displacement disp displace",
    "ao": "ambientocclusion ao occlusion",
    "opacity": "opacity alpha",
    "transw": "transmissionweight transmission",
    "transc": "transmissioncolor",
    "preview": "preview thumb thumbnail sphere cover",
}.items():
    for _t in _toks.split():
        CHANNELS[_t] = _ch
RES_TOKEN = re.compile(r"^(\d{1,2})k$")
SPLIT = re.compile(r"[\s_\-.]+")
SEQ = re.compile(r"^(.*?)[_\-.]?(\d{3,})$")
SEQ_MIN = 24                     # frames before a numbered run counts as an animation (a second at 24 fps)

FOLDER_KINDS = [(IMPERF, ("imperfection", "grunge", "scratch", "smudge", "dust", "fingerprint", "stain")),
                (LIGHTMAP, ("lightmap", "light map", "light_map", "lighthit")),
                (GOBO, ("gobo",)),
                (BOKEH, ("bokeh",))]

THUMB_W = 320
SIZE_MIN, SIZE_MAX, SIZE_DEFAULT = 80, 320, 150
PAD, GAP, LABEL_H = 6, 8, 16
LABEL_W = 100
BITMAP_CACHE = 300
WORKERS = 3
CREATE_NO_WINDOW = 0x08000000
FAVORITES, ALL = "*favorites*", "*all*"
WHITE = c4d.Vector(1.0)
ROT_STEP = math.radians(1.0)

# gadgets
CB_KIND, CB_COLL, E_SEARCH, B_ADDROOT, B_REFRESH = 4000, 4013, 4001, 4002, 4011
G_SCROLL, UA_GRID = 4003, 4004
G_HDRI, B_PREV, B_NEXT = 4020, 4005, 4006
SL_ROT, SL_EXP, CK_BG, SL_SIZE = 4007, 4008, 4009, 4012
G_MAT, CK_DISP = 4021, 4014
G_IMP, SL_IMP = 4022, 4015
G_LUT, SL_LUT, CK_LOG, B_NOLUT = 4023, 4016, 4017, 4018
RESETS = {SL_ROT: 0.0, SL_EXP: 0.0, SL_IMP: 0.6, SL_LUT: 1.0}     # right-click a slider: back to this
T_STATUS = 4010
G_MAIN = 4030

P = c4d.FIRST_POPUP_ID
M_APPLY, M_NEW, M_FAV, M_UNFAV, M_EXPLORER = P, P + 1, P + 2, P + 3, P + 4
M_AUTO, M_KIND0 = P + 10, P + 11            # "This folder is" submenu: Auto, then one per sortable kind
SORTABLE = [HDRI, IMPERF, LIGHTMAP, GOBO, BOKEH]


def log(*args):
    print("[Library]", *args)


def prefs_dir(*sub):
    d = os.path.join(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS), "goodies_library", *sub)
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
    log("error in", where, "- see goodies_library/errors.log")


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def _atomic_json(path, data, indent=1):
    try:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=indent)
        os.replace(tmp, path)
    except Exception as exc:
        log("could not write %s: %s" % (os.path.basename(path), exc))


def _url(path):
    return maxon.Url("file:///" + path.replace("\\", "/"))


# ═════════════════════════════════════════════════════════════ config

class Config(object):
    def __init__(self):
        self.path = os.path.join(prefs_dir(), "config.json")
        self.data = {"roots": [], "kind": MATERIAL, "collection": {}, "favorites": [],
                     "size": SIZE_DEFAULT, "ffmpeg": "", "displacement": False, "imperfection": 0.6,
                     "folder_kinds": {}}          # folder -> kind for its single images (and below it)
        self._migrate_hdri()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except FileNotFoundError:
            pass
        except Exception as exc:
            log("could not read config:", exc)

    def _migrate_hdri(self):
        """First run: take the HDRI panel's folders, favourites and thumbnails."""
        if os.path.exists(self.path):
            return
        old = os.path.join(storage.GeGetC4DPath(c4d.C4D_PATH_PREFS), "goodies_hdri")
        try:
            with open(os.path.join(old, "config.json"), "r", encoding="utf-8") as fh:
                o = json.load(fh)
            self.data["roots"] = list(o.get("roots", []))
            self.data["favorites"] = list(o.get("favorites", []))
            self.data["ffmpeg"] = o.get("ffmpeg", "")
            thumbs = os.path.join(old, "thumbs")
            if os.path.isdir(thumbs) and not os.path.exists(os.path.join(prefs_dir(), "thumbs")):
                os.replace(thumbs, os.path.join(prefs_dir(), "thumbs"))   # same key scheme: reused as is
            self.save()
            log("took over the HDRI panel's folders and thumbnails")
        except FileNotFoundError:
            pass
        except Exception as exc:
            log("HDRI settings not migrated:", exc)

    def save(self):
        _atomic_json(self.path, self.data)

    def is_favorite(self, aid):
        n = _norm(aid)
        return any(_norm(f) == n for f in self.data["favorites"])

    def set_favorite(self, aid, on):
        n = _norm(aid)
        favs = [f for f in self.data["favorites"] if _norm(f) != n]
        if on:
            favs.append(aid)
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
    return shutil.which("ffmpeg") or ""


# ═════════════════════════════════════════════════════════════ scanning
# Everything in this section is plain Python (no c4d calls): it runs in a
# background thread.

_HDR_RES = re.compile(rb"([-+][XY])\s+(\d+)\s+([-+][XY])\s+(\d+)")


def image_size(path):
    """(w, h) of an .hdr/.exr read from the file header only, or None."""
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
    if head[:4] != b"\x76\x2f\x31\x01":
        return None
    i, end = 8, len(head)
    for _ in range(512):
        z = head.find(b"\0", i)
        if z <= i:
            return None
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


def _channel(stem):
    """(channel or None, group key, resolution in K or 0) for a file stem."""
    toks = [t for t in SPLIT.split(stem.lower()) if t]
    res = 0
    rest = []
    for t in toks:
        m = RES_TOKEN.match(t)
        if m:
            res = int(m.group(1))
        else:
            rest.append(t)
    ch = None
    for i in range(len(rest) - 1, -1, -1):          # the last channel word wins
        if rest[i] in CHANNELS:
            ch = CHANNELS[rest[i]]
            del rest[i]
            break
    return ch, " ".join(rest), res


def _pretty(s):
    s = SPLIT.sub(" ", s).strip()
    return s[:1].upper() + s[1:] if s else s


def _folder_kind(relpath):
    low = relpath.lower()
    for kind, words in FOLDER_KINDS:
        if any(w in low for w in words):
            return kind
    return None


def _res_pick(cands):
    """Among [(res, path)], 4K if there is one, else the largest."""
    for r, p in cands:
        if r == 4:
            return p
    return max(cands, key=lambda c: c[0])[1]


def classify_folder(folder, files, rel, hdr_cache):
    """Assets in one folder: a list of [kind, id, name, main, thumb, channels]."""
    out = []
    imgs, ies = [], []
    for f in files:
        low = f.lower()
        if low.endswith(".ies"):
            ies.append(f)
        elif low.endswith(IMG_EXTS):
            imgs.append(f)
        elif low.endswith(LUT_EXTS):
            p = os.path.join(folder, f)
            out.append([LUT, p, os.path.splitext(f)[0], p, p, None])      # thumb: the sample through this LUT
    claimed = set()

    # IES and their preview images
    stems = {os.path.splitext(f)[0].lower(): f for f in imgs}
    for f in sorted(ies, key=str.lower):
        stem = os.path.splitext(f)[0]
        prev = stems.get(stem.lower())
        if prev:
            claimed.add(prev)
        p = os.path.join(folder, f)
        out.append([IES, p, _pretty(stem), p, os.path.join(folder, prev) if prev else "", None])

    # PBR sets
    groups = collections.defaultdict(lambda: collections.defaultdict(list))
    for f in imgs:
        if f in claimed:
            continue
        ch, key, res = _channel(os.path.splitext(f)[0])
        if ch:
            groups[key][ch].append((res, f))
    mats = []
    for key, chans in groups.items():
        maps = set(chans) - {"preview"}
        if len(maps) >= 2 and maps & {"normal", "normaldx", "rough", "gloss"}:
            mats.append((key, chans))
    for key, chans in mats:
        picked = {}
        for ch, cands in chans.items():
            picked[ch] = os.path.join(folder, _res_pick(cands))
            for _, f in cands:
                claimed.add(f)
        thumb = picked.get("preview") or picked.get("color") or next(iter(picked.values()))
        name = _pretty(key) or os.path.basename(folder)
        aid = folder + "|" + key
        out.append([MATERIAL, aid, name, folder, thumb, {k: v for k, v in picked.items() if k != "preview"}])
    if len(mats) == 1:                               # a material folder: every other image is part of it
        claimed.update(imgs)                         # (mask, groutmask, anisotropic... maps)
    elif mats:
        keys = set(k for k, _ in mats)
        for f in imgs:
            toks = [t for t in SPLIT.split(os.path.splitext(f)[0].lower())
                    if t and not RES_TOKEN.match(t)]
            if " ".join(toks[:-1]) in keys:          # same name, a channel word we don't use
                claimed.add(f)

    # image sequences collapse to their first frame. Not .hdr/.exr: numbered HDRI
    # packs (PRO_STUDIOS_001..046) are separate stills, not frames
    seqs = collections.defaultdict(list)
    for f in imgs:
        if f in claimed or f.lower().endswith(HDR_EXTS):
            continue
        stem, ext = os.path.splitext(f)
        m = SEQ.match(stem)
        if m:
            seqs[(m.group(1).lower(), ext.lower())].append(f)
    for (_, _), frames in seqs.items():
        if len(frames) >= SEQ_MIN:
            frames.sort(key=str.lower)
            for f in frames[1:]:
                claimed.add(f)

    # single images
    fk = _folder_kind(rel)
    for f in sorted(imgs, key=str.lower):
        if f in claimed:
            continue
        p = os.path.join(folder, f)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if st.st_size == 0:
            continue                                  # failed download
        low = f.lower()
        kind = fk
        if low.endswith(HDR_EXTS):
            rec = hdr_cache.get(_norm(p))
            if not rec or rec[0] != st.st_size or rec[1] != int(st.st_mtime):
                wh = image_size(p) or (0, 0)
                rec = hdr_cache[_norm(p)] = [st.st_size, int(st.st_mtime), wh[0], wh[1]]
            w, h = rec[2], rec[3]
            if not w or not h or PANO_MIN <= w / float(h) <= PANO_MAX:
                kind = HDRI
            elif not kind:
                kind = LIGHTMAP
        stem = os.path.splitext(f)[0]
        out.append([kind or TEXTURE, p, stem, p, p, None])
    return out


def scan(roots, cached, full=False):
    """Walk the roots. A folder whose date is unchanged is taken from the
    cache without listing it. Returns the new index."""
    old_dirs = {} if full else cached.get("dirs", {})
    hdr = cached.get("hdr", {})
    dirs = {}
    for root in roots:
        if not os.path.isdir(root):
            continue
        stack = [(root, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                mtime = int(os.stat(folder).st_mtime)
            except OSError:
                continue
            rec = old_dirs.get(folder)
            if rec is None or rec["m"] != mtime:
                files, subs = [], []
                try:
                    with os.scandir(folder) as it:
                        for e in it:
                            if e.name.startswith((".", "_")):
                                continue
                            if e.is_dir():
                                subs.append(e.name)
                            elif e.is_file():
                                files.append(e.name)
                except OSError:
                    continue
                rel = os.path.relpath(folder, os.path.dirname(root.rstrip("\\/")))
                rec = {"m": mtime, "subs": sorted(subs, key=str.lower),
                       "assets": classify_folder(folder, files, rel, hdr)}
            dirs[folder] = rec
            if depth < MAX_DEPTH:
                for s in reversed(rec["subs"]):
                    stack.append((os.path.join(folder, s), depth + 1))
    return {"v": INDEX_VERSION, "roots": list(roots), "dirs": dirs, "hdr": hdr}


class Library(object):
    """The cached index plus the views the panel needs, per kind:
    collections [(label, folder)] and folder -> [asset]."""

    def __init__(self):
        self.path = os.path.join(prefs_dir(), "index.json")
        self.index = {"v": INDEX_VERSION, "dirs": {}, "hdr": {}}
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if data.get("v") == INDEX_VERSION:
                self.index = data
        except FileNotFoundError:
            pass
        except Exception as exc:
            log("could not read index:", exc)
        self.by_id = {}
        self.kinds = {}                 # kind -> {"cols": [(label, folder)], "items": {folder: [asset]}}
        self.thread = None
        self.result = None
        self.scanned = False            # this session
        self._build_views()

    def start_scan(self, full=False):
        if self.thread and self.thread.is_alive():
            return False
        roots = builtin_roots() + list(CONFIG.data.get("roots", []))
        cached = self.index

        def run():
            try:
                self.result = scan(roots, cached, full)
            except Exception:
                self.result = "error"
                _log_error("scan")
        self.result = None
        self.thread = threading.Thread(target=run, name="goodies-library-scan", daemon=True)
        self.thread.start()
        return True

    def busy(self):
        return bool(self.thread and self.thread.is_alive())

    def collect(self):
        """Main thread: take a finished scan. Returns how many assets are new, or None."""
        if self.result is None or self.busy():
            return None
        res, self.result = self.result, None
        self.scanned = True
        if res == "error":
            return 0
        before = set(self.by_id)
        self.index = res
        _atomic_json(self.path, res, indent=None)
        self._build_views()
        return len(set(self.by_id) - before) if before else 0

    def _build_views(self):
        builtin = builtin_roots()
        roots = builtin + [r for r in CONFIG.data.get("roots", [])]
        seen_luts = set()                               # installers ship the same LUT 3 times: show it once
        forced = sorted(((_norm(f), k) for f, k in CONFIG.data.get("folder_kinds", {}).items()),
                        key=lambda x: -len(x[0]))       # deepest setting wins
        kinds, by_id = {}, {}
        for folder, rec in self.index.get("dirs", {}).items():
            root = next((r for r in roots if _norm(folder).startswith(_norm(r))), None)
            if root is None:
                continue
            nf = _norm(folder)
            force = next((k for f, k in forced if nf == f or nf.startswith(f + os.sep)), None)
            for a in rec["assets"]:
                if force and a[0] in IMAGE_KINDS and a[0] != force:
                    a = [force] + a[1:]
                kind = a[0]
                if kind == TEXTURE:
                    continue
                if kind == LUT:
                    key = os.path.basename(a[3]).lower()
                    if key in seen_luts:
                        continue
                    seen_luts.add(key)
                coll = folder
                if len(rec["assets"]) == 1 and _norm(folder) != _norm(root):
                    coll = os.path.dirname(folder)   # one asset per folder (a material, a sequence): group by the parent
                v = kinds.setdefault(kind, {"cols": {}, "items": collections.defaultdict(list)})
                if coll not in v["cols"]:
                    rel = os.path.relpath(coll, root)
                    top = "Redshift" if root in builtin else os.path.basename(root.rstrip("\\/"))
                    label = top if rel == "." else rel.replace(os.sep, " / ")
                    if len(roots) > 1 and rel != ".":
                        label = "%s / %s" % (top, label)
                    v["cols"][coll] = label
                v["items"][coll].append(a)
                by_id[_norm(a[1])] = a
        for v in kinds.values():
            v["cols"] = sorted(((lab, f) for f, lab in v["cols"].items()), key=lambda x: x[0].lower())
            for items in v["items"].values():
                items.sort(key=lambda a: a[2].lower())
        self.kinds, self.by_id = kinds, by_id

    def rebuild(self):
        """Folder settings changed: re-sort from the cached index, no disk access."""
        self._build_views()

    def count(self, kind):
        v = self.kinds.get(kind)
        return sum(len(i) for i in v["items"].values()) if v else 0


LIBRARY = Library()


def thumb_path(src):
    """Same key scheme as the old HDRI panel, so its thumbnails carry over."""
    try:
        st = os.stat(src)
        key = "%s|%d|%d|%d" % (os.path.normcase(src), st.st_size, int(st.st_mtime), THUMB_W)
    except OSError:
        key = os.path.normcase(src)
    return os.path.join(prefs_dir("thumbs"), hashlib.sha1(key.encode("utf-8")).hexdigest() + ".jpg")


def thumb_cmd(ffmpeg, src, dst):
    """(command, working folder) or None when there is nothing to make it from."""
    low = src.lower()
    if low.endswith(LUT_EXTS):
        if not os.path.isfile(LUT_SAMPLE):
            return None
        name = os.path.basename(src).replace("'", "'\\''")
        return [ffmpeg, "-v", "error", "-y", "-i", LUT_SAMPLE, "-vf", "lut3d=file='%s'" % name,
                "-frames:v", "1", "-q:v", "3", dst], os.path.dirname(src)
    return _thumb_cmd(ffmpeg, src, dst), None


def _thumb_cmd(ffmpeg, src, dst):
    low = src.lower()
    scale = "scale='min(%d,iw)':-2" % THUMB_W
    if low.endswith(".exr"):
        return [ffmpeg, "-v", "error", "-y", "-apply_trc", "iec61966_2_1", "-i", src,
                "-vf", "scale=%d:-2" % THUMB_W, "-frames:v", "1", dst]
    if low.endswith(".hdr"):
        g = "gammaval(0.4545)"
        return [ffmpeg, "-v", "error", "-y", "-i", src,
                "-vf", "scale=%d:-2,lutrgb=r=%s:g=%s:b=%s" % (THUMB_W, g, g, g), "-frames:v", "1", dst]
    return [ffmpeg, "-v", "error", "-y", "-i", src, "-vf", scale, "-frames:v", "1", "-q:v", "3", dst]


# ═════════════════════════════════════════════════════════════ scene: shared

def _rs_active(doc):
    rd = doc.GetActiveRenderData()
    return rd is not None and rd[c4d.RDATA_RENDERENGINE] == RS_RENDERER


def _walk(doc):
    op = doc.GetFirstObject()
    guard = 0
    while op and guard < 1000000:
        guard += 1
        yield op
        nxt = op.GetDown()
        if nxt is None:
            while op and op.GetNext() is None:
                op = op.GetUp()
            nxt = op.GetNext() if op else None
        op = nxt


# ═════════════════════════════════════════════════════════════ scene: HDRI

def find_domes(doc):
    return [op for op in _walk(doc) if op.GetType() == RS_LIGHT and op.GetDataInstance().GetBool(MARK_DOME)]


def find_dome(doc):
    domes = find_domes(doc)
    for d in domes:
        if d.GetBit(c4d.BIT_ACTIVE):
            return d
    return domes[0] if domes else None


def apply_hdri(doc, path, new=False):
    dome = None if new else find_dome(doc)
    doc.StartUndo()
    if dome is None:
        dome = c4d.BaseObject(RS_LIGHT)
        dome[RS_TYPE] = RS_DOME
        dome.GetDataInstance().SetBool(MARK_DOME, True)
        doc.InsertObject(dome)
        doc.AddUndo(c4d.UNDOTYPE_NEW, dome)
        if new:
            doc.SetActiveObject(dome, c4d.SELECTION_NEW)
    else:
        doc.AddUndo(c4d.UNDOTYPE_CHANGE, dome)
    dome.SetParameter(rs_path_id(RS_DOME_TEX), path, c4d.DESCFLAGS_SET_NONE)
    dome[c4d.ID_BASELIST_ICON_COLORIZE_MODE] = c4d.ID_BASELIST_ICON_COLORIZE_MODE_CUSTOM
    dome[c4d.ID_BASELIST_ICON_COLOR] = WHITE
    dome.SetName("HDRI - %s" % os.path.splitext(os.path.basename(path))[0])
    doc.EndUndo()
    c4d.EventAdd()
    return ("Added dome: %s" if new else "HDRI: %s") % os.path.basename(path)


def light_path(light, pid):
    if light is None:
        return ""
    p = light.GetParameter(rs_path_id(pid), c4d.DESCFLAGS_GET_NONE) or ""
    if p.startswith("file:///"):
        p = p[8:]
    return _norm(p.replace("/", os.sep)) if p else ""


# ═════════════════════════════════════════════════════════════ scene: lights

LIGHT_SETUP = {LIGHTMAP: (RS_AREA, RS_LIGHT_TEX, "Light Map"),
               GOBO: (RS_SPOT, RS_LIGHT_TEX, "Gobo"),
               IES: (RS_IES, RS_IES_FILE, "IES")}


def apply_light(doc, kind, path, new=False):
    ltype, pid, label = LIGHT_SETUP[kind]
    name = os.path.splitext(os.path.basename(path))[0]
    targets = [] if new else [o for o in doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE)
                              if o.GetType() == RS_LIGHT and o[RS_TYPE] != RS_DOME]
    doc.StartUndo()
    if targets:
        for lt in targets:
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, lt)
            if kind == IES:
                lt[RS_TYPE] = RS_IES
            lt.SetParameter(rs_path_id(pid), path, c4d.DESCFLAGS_SET_NONE)
            if lt.GetDataInstance().GetBool(MARK):
                lt.SetName("%s - %s" % (label, name))
        msg = "%s on %d light%s: %s" % (label, len(targets), "" if len(targets) == 1 else "s", name)
    else:
        lt = c4d.BaseObject(RS_LIGHT)
        lt[RS_TYPE] = ltype
        lt.SetParameter(rs_path_id(pid), path, c4d.DESCFLAGS_SET_NONE)
        lt.SetName("%s - %s" % (label, name))
        lt.GetDataInstance().SetBool(MARK, True)
        doc.InsertObject(lt)
        doc.AddUndo(c4d.UNDOTYPE_NEW, lt)
        doc.SetActiveObject(lt, c4d.SELECTION_NEW)
        msg = "New %s light: %s" % (label.lower() if kind != IES else "IES", name)
    doc.EndUndo()
    c4d.EventAdd()
    return msg


# ═════════════════════════════════════════════════════════════ scene: materials

def _tex(path, raw=True):
    d = {"$type": "Texture", "Image/Filename/Path": _url(path)}
    if raw:
        d["Image/Filename/Color Space"] = "RS_INPUT_COLORSPACE_RAW"
    return d


def _cc(path):
    """Colour maps go through a Color Correct, so hue/saturation/level are one click away."""
    return {"$type": "Color Correct", "Input": _tex(path, raw=False)}


def _ramp(path):
    """Grey maps (roughness, metalness...) go through a gradient Ramp for contrast/remapping."""
    return {"$type": "Ramp", "Input": _tex(path)}


def _nodes(graph, asset):
    found = []

    def visit(n):
        a = n.GetValue("net.maxon.node.attribute.assetid")
        if a and str(a[0]) == asset:
            found.append(n)
        return True
    graph.GetViewRoot().GetChildren(visit, maxon.NODE_KIND.NODE)
    return found


def build_material(asset, displacement=False):
    """A new (not yet inserted) RS OpenPBR material from a PBR set. Colour maps
    get a Color Correct, grey maps a gradient Ramp, and one UV Context Projection
    on the output drives the tiling of every texture."""
    ch = asset[5] or {}
    surf = {"$type": "OpenPBR Material"}
    if "color" in ch:
        surf["Base/Color"] = _cc(ch["color"])
    if "metal" in ch:
        surf["Base/Metalness"] = _ramp(ch["metal"])
    if "rough" in ch or "gloss" in ch:
        surf["Specular/Roughness"] = _ramp(ch.get("rough") or ch["gloss"])
    normal = ch.get("normal") or ch.get("normaldx")
    if normal:
        surf["Geometry/Bump Map"] = {"$type": "Bump Map", "Input Map Type": 1, "Input": _tex(normal)}
    elif "height" in ch:
        surf["Geometry/Bump Map"] = {"$type": "Bump Map", "Input Map Type": 0, "Input": _tex(ch["height"])}
    if "opacity" in ch:
        surf["Geometry/Opacity"] = _ramp(ch["opacity"])
    if "transw" in ch:
        surf["Transmission/Weight"] = _ramp(ch["transw"])
    if "transc" in ch:
        surf["Transmission/Color"] = _cc(ch["transc"])
    desc = {"$type": "Output", "Surface": surf, "UV Context": {"$type": "UV Context Projection"}}
    if displacement and "height" in ch:
        desc["Displacement"] = {"$type": "Displacement", "Map": _tex(ch["height"])}

    mat = c4d.BaseMaterial(c4d.Mmaterial)
    mat.SetName(asset[2])
    graph = mat.GetNodeMaterialReference().CreateEmptyGraph(NS)
    maxon.GraphDescription.ApplyDescription(graph, desc)
    if "gloss" in ch and "rough" not in ch:                  # glossiness map: flip it in the ramp
        with graph.BeginTransaction() as t:
            for n in _nodes(graph, RSN + "rsramp"):
                dst = []
                n.GetOutputs().FindChild(RSN + "rsramp.outcolor").GetConnections(maxon.PORT_DIR.OUTPUT, dst)
                if any("specular_roughness" in str(p.GetId()) for p, _ in dst):
                    n.GetInputs().FindChild(RSN + "rsramp.inputinvert").SetPortValue(True)
            t.Commit()
    mat.GetDataInstance().SetString(MARK, asset[1])
    return mat


def find_material(doc, aid):
    n = _norm(aid)
    m = doc.GetFirstMaterial()
    while m:
        s = m.GetDataInstance().GetString(MARK)
        if s and _norm(s) == n:
            return m
        m = m.GetNext()
    return None


def assign(doc, mat, objs):
    """Put mat on objs: reuse a texture tag this panel made, else add one last (so it wins)."""
    for op in objs:
        tag = None
        for t in op.GetTags():
            if t.GetType() == c4d.Ttexture and t.GetDataInstance().GetBool(MARK):
                tag = t
                break
        if tag:
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)
        else:
            tag = c4d.TextureTag()
            tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_UVW
            tag.GetDataInstance().SetBool(MARK, True)
            op.InsertTag(tag, op.GetLastTag())
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
        tag.SetMaterial(mat)


def get_material(doc, asset, new=False):
    """(material, created) - the one already in the scene unless new."""
    mat = None if new else find_material(doc, asset[1])
    if mat:
        return mat, False
    mat = build_material(asset, CONFIG.data.get("displacement", False))
    doc.InsertMaterial(mat)
    doc.AddUndo(c4d.UNDOTYPE_NEW, mat)
    return mat, True


def apply_material(doc, asset, new=False, assign_to_selection=True):
    objs = doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN) if assign_to_selection else []
    objs = [o for o in objs if o.GetType() != RS_LIGHT]
    doc.StartUndo()
    try:
        mat, created = get_material(doc, asset, new)
        doc.SetActiveMaterial(mat, c4d.SELECTION_NEW)
        if objs:
            assign(doc, mat, objs)
    finally:
        doc.EndUndo()
    c4d.EventAdd()
    what = "New material" if created else "Material"
    if objs:
        return "%s: %s on %d object%s" % (what, asset[2], len(objs), "" if len(objs) == 1 else "s")
    return "%s: %s (select objects to assign)" % (what, asset[2]) if created else "%s: %s" % (what, asset[2])


def texture_material(doc, path, name):
    """An OpenPBR material with the (imperfection) image driving roughness through a ramp."""
    mat = c4d.BaseMaterial(c4d.Mmaterial)
    mat.SetName(name)
    graph = mat.GetNodeMaterialReference().CreateEmptyGraph(NS)
    maxon.GraphDescription.ApplyDescription(graph, {
        "$type": "Output", "UV Context": {"$type": "UV Context Projection"},
        "Surface": {"$type": "OpenPBR Material", "Specular/Roughness": _ramp(path)}})
    mat.GetDataInstance().SetString(MARK, path)
    doc.StartUndo()
    doc.InsertMaterial(mat)
    doc.AddUndo(c4d.UNDOTYPE_NEW, mat)
    doc.SetActiveMaterial(mat, c4d.SELECTION_NEW)
    objs = [o for o in doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_CHILDREN) if o.GetType() != RS_LIGHT]
    if objs:
        assign(doc, mat, objs)
    doc.EndUndo()
    c4d.EventAdd()
    return "New material: %s" % name


def apply_imperfection(doc, path, new=False):
    """Blend the map into the selected RS material's roughness: a Math Mix
    between what was there (input 1) and the target roughness (input 2),
    with the map as the mix amount."""
    name = os.path.splitext(os.path.basename(path))[0]
    mat = None if new else doc.GetActiveMaterial()
    graph = None
    if mat is not None:
        nm = mat.GetNodeMaterialReference()
        if nm and nm.HasSpace(NS):
            graph = nm.GetGraph(NS)
    if graph is None or graph.IsNullValue():
        if mat is not None and not new:
            return "Imperfection: %s isn't a Redshift node material" % mat.GetName()
        return texture_material(doc, path, "Imperfection - %s" % name)
    port = None
    for asset, pid in (("openpbrmaterial", "specular_roughness"), ("standardmaterial", "refl_roughness"),
                       ("material", "refl_roughness")):      # OpenPBR, Standard, then the older RS Material
        for n in _nodes(graph, RSN + asset):
            port = n.GetInputs().FindChild(RSN + asset + "." + pid)
            if port and not port.IsNullValue():
                break
            port = None
        if port:
            break
    if port is None:
        return "Imperfection: no OpenPBR/Standard Material in %s" % mat.GetName()
    target = float(CONFIG.data.get("imperfection", 0.6))
    doc.StartUndo()
    doc.AddUndo(c4d.UNDOTYPE_CHANGE, mat)
    src = []
    port.GetConnections(maxon.PORT_DIR.INPUT, src)
    with graph.BeginTransaction() as t:
        tex = graph.AddChild(maxon.Id(), maxon.Id(RSN + "texturesampler"))
        tex.SetValue(maxon.NODE.BASE.NAME, maxon.String(name))
        tex0 = tex.GetInputs().FindChild(RSN + "texturesampler.tex0")
        tex0.FindChild("path").SetPortValue(_url(path))
        tex0.FindChild("colorspace").SetPortValue("RS_INPUT_COLORSPACE_RAW")
        mix = graph.AddChild(maxon.Id(), maxon.Id(RSN + "rsmathmix"))
        mix.SetValue(maxon.NODE.BASE.NAME, maxon.String("Imperfection"))
        mi = mix.GetInputs()
        if src:
            src[0][0].Connect(mi.FindChild(RSN + "rsmathmix.input1"))
        else:
            v = port.GetPortValue()
            if v is not None:
                mi.FindChild(RSN + "rsmathmix.input1").SetPortValue(v)
        mi.FindChild(RSN + "rsmathmix.input2").SetPortValue(target)
        ramp = graph.AddChild(maxon.Id(), maxon.Id(RSN + "rsramp"))            # contrast / strength of the map
        tex.GetOutputs().FindChild(RSN + "texturesampler.outcolor").Connect(ramp.GetInputs().FindChild(RSN + "rsramp.input"))
        ramp.GetOutputs().FindChild(RSN + "rsramp.outcolor").Connect(mi.FindChild(RSN + "rsmathmix.mixamount"))
        port.RemoveConnections(maxon.PORT_DIR.INPUT, maxon.Wires.All())
        mix.GetOutputs().FindChild(RSN + "rsmathmix.out").Connect(port)
        t.Commit()
    doc.EndUndo()
    c4d.EventAdd()
    return "Imperfection %s -> %s roughness" % (name, mat.GetName())


def target_cameras(doc):
    """Selected cameras (C4D or RS Camera), else the one the view looks through."""
    kinds = (c4d.Ocamera, RS_CAMERA)
    cams = [o for o in doc.GetActiveObjects(c4d.GETACTIVEOBJECTFLAGS_NONE) if o.GetType() in kinds]
    if cams:
        return cams
    bd = doc.GetActiveBaseDraw()
    cam = bd.GetSceneCamera(doc) if bd else None
    if cam is None or cam.GetType() not in kinds or cam == bd.GetEditorCamera():
        return []
    return [cam]


def lut_holder(doc, cam, make=False):
    """Where a camera's LUT lives: the RS Camera object itself, or a C4D camera's RS Camera tag."""
    if cam.GetType() == RS_CAMERA:
        return cam
    tag = cam.GetTag(RS_CAMTAG)
    if tag is None and make:
        tag = cam.MakeTag(RS_CAMTAG)
        doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
    return tag


def lut_on(holder, on):
    if holder.GetType() == RS_CAMERA:
        holder[LUT_CAM_MODE] = LUT_CAM_OVERRIDE if on else LUT_CAM_OFF
    else:
        holder[LUT_TAG_OVERRIDE] = on
        holder[LUT_TAG_ENABLED] = on


def lut_path(holder):
    """The LUT a camera shows, or "" when its LUT is off."""
    if holder is None:
        return ""
    if holder.GetType() == RS_CAMERA:
        if holder[LUT_CAM_MODE] != LUT_CAM_OVERRIDE:
            return ""
    elif not (holder[LUT_TAG_OVERRIDE] and holder[LUT_TAG_ENABLED]):
        return ""
    p = holder[LUT_FILE] or ""
    return _norm(str(p)) if p else ""


def apply_lut(doc, path, strength, log_space):
    cams = target_cameras(doc)
    if not cams:
        return "LUT: select a camera (or look through one)"
    doc.StartUndo()
    for cam in cams:
        h = lut_holder(doc, cam, make=True)
        doc.AddUndo(c4d.UNDOTYPE_CHANGE, h)
        lut_on(h, True)
        h[LUT_FILE] = path
        h[LUT_STRENGTH] = strength
        h[LUT_LOG] = log_space
    doc.EndUndo()
    c4d.EventAdd()
    return "LUT %s on %s" % (os.path.splitext(os.path.basename(path))[0], ", ".join(c.GetName() for c in cams))


def remove_lut(doc):
    cams = [c for c in target_cameras(doc) if lut_holder(doc, c)]
    if not cams:
        return "LUT: no camera with a LUT selected or in view"
    doc.StartUndo()
    for cam in cams:
        h = lut_holder(doc, cam)
        doc.AddUndo(c4d.UNDOTYPE_CHANGE, h)
        lut_on(h, False)
    doc.EndUndo()
    c4d.EventAdd()
    return "LUT off on %s" % ", ".join(c.GetName() for c in cams)


def _rig_controls(cam):
    """A camera inside a rig whose parent has "Bokeh Texture" user data (CAM_RIG):
    (rig, {name: DescID}). The rig pushes its values onto the camera, so write there."""
    root = cam.GetUp()
    if root is None:
        return None
    ids = {bc[c4d.DESC_NAME]: did for did, bc in root.GetUserDataContainer()}
    return (root, ids) if "Bokeh Texture" in ids else None


def apply_bokeh(doc, path):
    """Bokeh image on the selected cameras, else the view's scene camera. An RS Camera
    object holds it itself (Optical > Bokeh); a C4D camera through its RS Camera tag."""
    cams = target_cameras(doc)
    if not cams:
        return "Bokeh: select a camera (or look through one)"
    doc.StartUndo()
    for cam in cams:
        rig = _rig_controls(cam)
        if rig:                                           # a camera rig drives the camera: set its controls
            root, ids = rig
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, root)
            for key, value in (("DOF Toggle", True), ("Custom Bokeh", True), ("Bokeh Texture", path)):
                if key in ids:
                    root[ids[key]] = value
            continue
        if cam.GetType() == RS_CAMERA:
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, cam)
            cam[RS_CAM_BOKEH] = True                          # depth of field on
            cam[RS_CAM_DIAPHRAGM] = RS_DIAPHRAGM_IMAGE        # without this the image is ignored
            cam.SetParameter(rs_path_id(RS_CAM_BOKEH_IMAGE), path, c4d.DESCFLAGS_SET_NONE)
            continue
        tag = cam.GetTag(RS_CAMTAG)
        if tag is None:
            tag = cam.MakeTag(RS_CAMTAG)
            doc.AddUndo(c4d.UNDOTYPE_NEW, tag)
        else:
            doc.AddUndo(c4d.UNDOTYPE_CHANGE, tag)
        tag[RS_BOKEH_USE] = True
        tag.SetParameter(rs_path_id(RS_BOKEH_IMAGE), path, c4d.DESCFLAGS_SET_NONE)
    doc.EndUndo()
    c4d.EventAdd()
    return "Bokeh %s on %s (depth of field must be on)" % (
        os.path.splitext(os.path.basename(path))[0], ", ".join(c.GetName() for c in cams))


# ═════════════════════════════════════════════════════════════ grid

class Grid(gui.GeUserArea):
    """Cells stretch so a row always fills the width exactly."""

    def __init__(self, panel):
        self.p = panel
        self.width = 400

    def layout(self):
        target = CONFIG.data.get("size", SIZE_DEFAULT)
        usable = max(1, self.width - PAD)
        cols = max(1, int(round(usable / float(target + GAP))))
        cell_w = usable // cols
        draw_w = max(20, cell_w - GAP)
        draw_h = draw_w // 2 if self.p.kind == HDRI else draw_w
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
                if not CONFIG.data.get("roots"):
                    empty = "Add your asset folders with + Folder"
                elif LIBRARY.busy() and not LIBRARY.scanned:
                    empty = "Scanning your folders..."
                elif self.p.GetString(E_SEARCH).strip():
                    empty = "Nothing matches the search"
                elif self.p.current == FAVORITES:
                    empty = "No favourites here yet - right-click a thumbnail"
                else:
                    empty = "Nothing here"
                self.DrawText(empty, PAD, PAD)
                return
            cols, cell_w, draw_w, draw_h, cell_h = self.layout()
            first = max(0, (y1 // cell_h) * cols)
            last = min(len(self.p.items), ((y2 // cell_h) + 1) * cols)
            for i in range(first, last):
                a = self.p.items[i]
                x = PAD + (i % cols) * cell_w
                y = PAD + (i // cols) * cell_h
                bmp = self.p.thumb(a[4])
                if bmp is not None:
                    bw, bh = bmp.GetBw(), bmp.GetBh()
                    s = min(draw_w / float(bw), draw_h / float(bh))
                    w, h = max(1, int(bw * s)), max(1, int(bh * s))
                    ox, oy = x + (draw_w - w) // 2, y + (draw_h - h) // 2
                    if w < draw_w or h < draw_h:
                        self.DrawSetPen(c4d.COLOR_BGEDIT)
                        self.DrawRectangle(x, y, x + draw_w, y + draw_h)
                    self.DrawBitmap(bmp, ox, oy, w, h, 0, 0, bw, bh, c4d.BMP_NORMALSCALED | c4d.BMP_ALLOWALPHA)
                else:
                    self.DrawSetPen(c4d.COLOR_BGEDIT)
                    self.DrawRectangle(x, y, x + draw_w, y + draw_h)
                    if a[0] == IES:
                        self.DrawSetTextCol(c4d.COLOR_TEXT_DISABLED, c4d.COLOR_TRANS)
                        self.DrawText("IES", x + 4, y + 4)
                n = _norm(a[1])
                if n in self.p.used:
                    self.DrawSetPen(WHITE)           # thick: the current dome; thin: in the scene
                    for k in range(3 if n == self.p.active else 1):
                        self.DrawFrame(x - 1 - k, y - 1 - k, x + draw_w + k, y + draw_h + k)
                elif n == self.p.cursor and self.p.kind in ARROW_KINDS:
                    self.DrawSetPen(WHITE)           # last one applied (lights, bokeh): where the arrows go on from
                    for k in range(2):
                        self.DrawFrame(x - 1 - k, y - 1 - k, x + draw_w + k, y + draw_h + k)
                if CONFIG.is_favorite(a[1]):
                    self.DrawSetPen(WHITE)
                    self.DrawRectangle(x + draw_w - 12, y + 3, x + draw_w - 3, y + 12)
                name = a[2]
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

    def _dragged(self, msg):
        """Mouse held and moved a few pixels: a drag, not a click."""
        x = msg.GetInt32(c4d.BFM_INPUT_X)
        y = msg.GetInt32(c4d.BFM_INPUT_Y)
        self.MouseDragStart(c4d.KEY_MLEFT, x, y, c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE | c4d.MOUSEDRAGFLAGS_NOMOVE)
        dx = dy = 0
        moved = False
        while True:
            res, mx, my, channels = self.MouseDrag()
            if res != c4d.MOUSEDRAGRESULT_CONTINUE:
                break
            dx += mx
            dy += my
            if abs(dx) + abs(dy) > 6:
                moved = True
                break
        self.MouseDragEnd()
        return moved

    def InputEvent(self, msg):
        try:
            if msg.GetInt32(c4d.BFM_INPUT_DEVICE) == c4d.BFM_INPUT_KEYBOARD:
                return self.p.arrow_key(msg.GetInt32(c4d.BFM_INPUT_CHANNEL))
            if msg.GetInt32(c4d.BFM_INPUT_DEVICE) != c4d.BFM_INPUT_MOUSE:
                return False
            ch = msg.GetInt32(c4d.BFM_INPUT_CHANNEL)
            if ch == c4d.BFM_INPUT_MOUSELEFT:
                self.p.Activate(UA_GRID)                # keyboard focus: arrow keys step through the grid
                a = self._hit(msg)
                if not a:
                    return True
                shift = bool(msg.GetInt32(c4d.BFM_INPUT_QUALIFIER) & c4d.QSHIFT)
                if a[0] == MATERIAL and self._dragged(msg):
                    self.p.drag_material(self, msg, a)
                else:
                    self.p.apply(a, new=shift)
                return True
            if ch == c4d.BFM_INPUT_MOUSERIGHT:
                a = self._hit(msg)
                if a:
                    self.p.context_menu(a)
                return True
            return False
        except Exception:
            _log_error("InputEvent")
            return True


# ═════════════════════════════════════════════════════════════ panel

class Panel(gui.GeDialog):

    def __init__(self):
        self.grid = Grid(self)
        self.kind = CONFIG.data.get("kind", MATERIAL)
        self.kind_choices, self.choices = [], []
        self.current = ""
        self.all_items, self.items = [], []
        self.bitmaps = collections.OrderedDict()
        self.queue, self.running = [], []
        self.pending = set()
        self.ffmpeg = ""
        self.active = ""                 # current dome's HDRI
        self.used = set()                # asset ids in the scene (domes, materials)
        self.tpaths = {}
        self.in_undo = False
        self.drag_dome = None            # the dome (or LUT holder) a slider drag is changing
        self.rdown = False               # right mouse button, for right-click reset
        self.cursor = ""                 # the item last applied: arrow keys go on from here
        self.scan_msg = ""

    # ------------------------------------------------------------ layout

    def CreateLayout(self):
        self.SetTitle("Library")
        self.GroupBegin(G_MAIN, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 1, 0, "", 0)
        self.GroupBorderSpace(6, 6, 6, 6)
        self.GroupSpace(4, 4)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 5, 0, "", 0)
        self.AddComboBox(CB_KIND, c4d.BFH_LEFT, 140, 0)
        self.AddComboBox(CB_COLL, c4d.BFH_SCALEFIT, 0, 0)
        self.AddEditText(E_SEARCH, c4d.BFH_RIGHT, 150, 0,
                         c4d.EDITTEXT_SEARCHLOOK | c4d.EDITTEXT_ENABLECLEARBUTTON)
        self.AddButton(B_REFRESH, c4d.BFH_RIGHT, 0, 0, "Refresh")
        self.AddButton(B_ADDROOT, c4d.BFH_RIGHT, 0, 0, "+ Folder")
        self.GroupEnd()

        self.ScrollGroupBegin(G_SCROLL, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT,
                              c4d.SCROLLGROUP_VERT | c4d.SCROLLGROUP_AUTOVERT, 0, 220)
        self.AddUserArea(UA_GRID, c4d.BFH_SCALEFIT | c4d.BFV_TOP)
        self.AttachUserArea(self.grid, UA_GRID)
        self.GroupEnd()

        # HDRI: the dome
        self.GroupBegin(G_HDRI, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "Rotation")
        self.AddEditSlider(SL_ROT, c4d.BFH_SCALEFIT)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "Exposure")
        self.AddEditSlider(SL_EXP, c4d.BFH_SCALEFIT)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "")
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 0, "", 0)
        self.AddCheckbox(CK_BG, c4d.BFH_SCALEFIT, 0, 0, "Show as Background")
        self.AddButton(B_PREV, c4d.BFH_RIGHT, 30, 0, "<")
        self.AddButton(B_NEXT, c4d.BFH_RIGHT, 30, 0, ">")
        self.GroupEnd()
        self.GroupEnd()

        # Materials
        self.GroupBegin(G_MAT, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "")
        self.AddCheckbox(CK_DISP, c4d.BFH_LEFT, 0, 0, "Height as Displacement")
        self.GroupEnd()

        # Imperfections
        self.GroupBegin(G_IMP, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "Roughness")
        self.AddEditSlider(SL_IMP, c4d.BFH_SCALEFIT)
        self.GroupEnd()

        # LUTs
        self.GroupBegin(G_LUT, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "Strength")
        self.AddEditSlider(SL_LUT, c4d.BFH_SCALEFIT)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "")
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddCheckbox(CK_LOG, c4d.BFH_SCALEFIT, 0, 0, "Convert to Log first (for log/camera LUTs)")
        self.AddButton(B_NOLUT, c4d.BFH_RIGHT, 0, 0, "No LUT")
        self.GroupEnd()
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, LABEL_W, 0, "Thumbnails")
        self.AddSlider(SL_SIZE, c4d.BFH_SCALEFIT, 80, 0)
        self.GroupEnd()

        self.AddStaticText(T_STATUS, c4d.BFH_SCALEFIT, 0, 0, "", c4d.BORDER_THIN_IN)
        self.GroupEnd()
        return True

    def InitValues(self):
        self.SetFloat(SL_ROT, 0.0, -math.pi, math.pi, ROT_STEP, c4d.FORMAT_DEGREE)
        self.SetFloat(SL_EXP, 0.0, -5.0, 5.0, 0.1)
        self.SetFloat(SL_IMP, float(CONFIG.data.get("imperfection", 0.6)), 0.0, 1.0, 0.01)
        self.SetFloat(SL_LUT, 1.0, 0.0, 1.0, 0.01, c4d.FORMAT_PERCENT)
        self.SetBool(CK_LOG, False)
        self.SetBool(CK_DISP, bool(CONFIG.data.get("displacement", False)))
        self.SetInt32(SL_SIZE, int(CONFIG.data.get("size", SIZE_DEFAULT)), SIZE_MIN, SIZE_MAX, 10)
        try:
            self.SetString(E_SEARCH, "Search", False, c4d.EDITTEXT_HELPTEXT)
        except Exception:
            pass
        self.ffmpeg = find_ffmpeg()
        self._fill_kinds()
        self._pull_scene()
        if not LIBRARY.scanned:
            LIBRARY.start_scan()                     # background: the cached index shows meanwhile
            self.scan_msg = "Scanning..."
        self._status()
        self.SetTimer(100)
        return True

    def show_kind(self, kind):
        """Open on a kind (the HDRI command uses this)."""
        self.kind = kind
        CONFIG.data["kind"] = kind
        if self.IsOpen():
            self._fill_kinds()

    # ------------------------------------------------------------ library

    def _fill_kinds(self):
        self.FreeChildren(CB_KIND)
        self.kind_choices = [k for k, _ in KINDS if LIBRARY.count(k) or k in (MATERIAL, HDRI) or k == self.kind]
        pick = 0
        for i, k in enumerate(self.kind_choices):
            self.AddChild(CB_KIND, i, "%s (%d)" % (KIND_NAME[k], LIBRARY.count(k)))
            if k == self.kind:
                pick = i
        self.SetInt32(CB_KIND, pick)
        self._set_kind(self.kind_choices[pick])

    def _set_kind(self, kind):
        self.kind = kind
        CONFIG.data["kind"] = kind
        for gid, k in ((G_HDRI, HDRI), (G_MAT, MATERIAL), (G_IMP, IMPERF), (G_LUT, LUT)):
            self.HideElement(gid, kind != k)
        self.LayoutChanged(G_MAIN)
        self._fill_combo()

    def _fill_combo(self, keep=None):
        v = LIBRARY.kinds.get(self.kind, {"cols": [], "items": {}})
        want = keep if keep is not None else CONFIG.data.get("collection", {}).get(self.kind, "")
        self.FreeChildren(CB_COLL)
        self.choices = [FAVORITES, ALL] + [folder for _, folder in v["cols"]]
        favs = sum(1 for f in CONFIG.data["favorites"] if (LIBRARY.by_id.get(_norm(f)) or [None])[0] == self.kind)
        self.AddChild(CB_COLL, 0, "★ Favorites (%d)" % favs)
        self.AddChild(CB_COLL, 1, "All (%d)" % LIBRARY.count(self.kind))
        pick = {FAVORITES: 0, ALL: 1}.get(want)
        for i, (label, folder) in enumerate(v["cols"]):
            self.AddChild(CB_COLL, i + 2, "%s (%d)" % (label, len(v["items"].get(folder, []))))
            if folder == want:
                pick = i + 2
        if pick is None:
            pick = 1
        self.SetInt32(CB_COLL, pick)
        self._open(self.choices[pick])

    def _items_for(self, key):
        v = LIBRARY.kinds.get(self.kind)
        if key == FAVORITES:
            out = []
            for f in CONFIG.data["favorites"]:
                a = LIBRARY.by_id.get(_norm(f))
                if a and a[0] == self.kind:
                    out.append(a)
            return out
        if not v:
            return []
        if key == ALL:
            return sorted((a for items in v["items"].values() for a in items), key=lambda a: a[2].lower())
        return list(v["items"].get(key, []))

    def _open(self, key):
        self.current = key
        CONFIG.data.setdefault("collection", {})[self.kind] = key
        CONFIG.save()
        self.bitmaps.clear()
        self.queue = []
        self.pending = set(s for _, s in self.running)
        self.all_items = self._items_for(key)
        self._filter()
        self._status()

    def _filter(self):
        q = self.GetString(E_SEARCH).strip().lower()
        if q:
            words = q.split()
            self.items = [a for a in self.all_items
                          if all(w in (a[2] + " " + os.path.basename(a[3])).lower() for w in words)]
        else:
            self.items = list(self.all_items)
        self.grid.LayoutChanged()
        self.grid.Redraw()

    def _thumb_path(self, src):
        tp = self.tpaths.get(src)
        if tp is None:
            tp = self.tpaths[src] = thumb_path(src)
        return tp

    def thumb(self, src):
        """Cached bitmap, or None while its thumbnail is still being made."""
        if not src:
            return None
        if src in self.bitmaps:
            self.bitmaps.move_to_end(src)
            got = self.bitmaps[src]
            return None if got is False else got
        if src in self.pending:
            return None
        tp = self._thumb_path(src)
        if not os.path.isfile(tp):
            if self.ffmpeg:
                self.queue.append(src)
                self.pending.add(src)
                return None
            tp = src if src.lower().endswith((".jpg", ".jpeg", ".png")) else ""   # no ffmpeg: load small files as is
            try:
                if not tp or os.path.getsize(tp) > 3000000:
                    self.bitmaps[src] = False
                    return None
            except OSError:
                return None
        bmp = bitmaps.BaseBitmap()
        ok = bmp.InitWith(tp)
        ok = ok[0] if isinstance(ok, tuple) else ok
        self.bitmaps[src] = bmp if ok == c4d.IMAGERESULT_OK else False
        while len(self.bitmaps) > BITMAP_CACHE:
            self.bitmaps.popitem(last=False)
        return None if self.bitmaps[src] is False else bmp

    def Timer(self, msg):
        try:
            self._poll_reset()
            new = LIBRARY.collect()
            if new is not None:
                self.scan_msg = "%d new" % new if new else ""
                self._fill_kinds()
                self._pull_scene()
            done = [r for r in self.running if r[0].poll() is not None]
            for proc, src in done:
                self.running.remove((proc, src))
                self.pending.discard(src)
                self.bitmaps.pop(src, None)
            if not self.ffmpeg:
                self.queue = []
                self.pending = set()
            while self.queue and len(self.running) < WORKERS:
                src = self.queue.pop()
                job = thumb_cmd(self.ffmpeg, src, self._thumb_path(src))
                if job is None:
                    self.pending.discard(src)
                    self.bitmaps[src] = False
                    continue
                try:
                    proc = subprocess.Popen(job[0], cwd=job[1],
                                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
                    self.running.append((proc, src))
                except Exception as exc:
                    log("ffmpeg failed:", exc)
                    self.queue = []
                    self.pending = set()
            if done or new is not None:
                self.grid.Redraw()
                self._status()
        except Exception:
            _log_error("Timer")

    # ------------------------------------------------------------ actions

    def apply(self, a, new=False):
        doc = c4d.documents.GetActiveDocument()
        if not _rs_active(doc):
            self._status("Library: Redshift isn't the render engine")
            return
        kind = a[0]
        self.cursor = _norm(a[1])                        # where the arrow keys continue from
        try:
            if kind == MATERIAL:
                msg = apply_material(doc, a, new)
            elif kind == HDRI:
                msg = apply_hdri(doc, a[3], new)
            elif kind == IMPERF:
                msg = apply_imperfection(doc, a[3], new)
            elif kind in LIGHT_SETUP:
                msg = apply_light(doc, kind, a[3], new)
            elif kind == BOKEH:
                msg = apply_bokeh(doc, a[3])
            elif kind == LUT:
                msg = apply_lut(doc, a[3], self.GetFloat(SL_LUT), self.GetBool(CK_LOG))
            else:
                msg = "Library: nothing to do with %s" % a[2]
        except Exception:
            _log_error("apply")
            msg = "Library: that failed - see goodies_library/errors.log"
        self._pull_scene()
        self.grid.Redraw()
        self._status(msg)

    def drag_material(self, ua, msg, a):
        """Build the material, then hand it to C4D's own drag: dropping on an
        object assigns it. A drag that lands nowhere is undone."""
        doc = c4d.documents.GetActiveDocument()
        if not _rs_active(doc):
            self._status("Library: Redshift isn't the render engine")
            return
        doc.StartUndo()
        mat, created = get_material(doc, a)
        doc.EndUndo()
        c4d.EventAdd()
        ua.HandleMouseDrag(msg, c4d.DRAGTYPE_ATOMARRAY, [mat], 0)
        if created and not self._material_used(doc, mat):
            doc.DoUndo(False)
            c4d.EventAdd()
            self._status("Drop a material onto an object to assign it")
            return
        self._pull_scene()
        self.grid.Redraw()
        self._status("Material: %s" % a[2])

    @staticmethod
    def _material_used(doc, mat):
        for op in _walk(doc):
            for t in op.GetTags():
                if t.GetType() == c4d.Ttexture and t.GetMaterial() == mat:
                    return True
        return False

    def context_menu(self, a):
        kind = a[0]
        bc = c4d.BaseContainer()
        if kind == MATERIAL:
            bc.InsData(M_APPLY, "Apply to Selection")
            bc.InsData(M_NEW, "New Material Only")
        elif kind == HDRI:
            bc.InsData(M_NEW, "Add as New Dome")
        elif kind == IMPERF:
            bc.InsData(M_APPLY, "Into Selected Material's Roughness")
            bc.InsData(M_NEW, "New Texture Material")
        elif kind in LIGHT_SETUP:
            bc.InsData(M_APPLY, "On Selected Light")
            bc.InsData(M_NEW, "New Light")
        elif kind in (BOKEH, LUT):
            bc.InsData(M_APPLY, "On Camera")
        bc.InsData(0, "")
        bc.InsData(M_UNFAV if CONFIG.is_favorite(a[1]) else M_FAV,
                   "Remove from Favorites" if CONFIG.is_favorite(a[1]) else "Add to Favorites")
        bc.InsData(M_EXPLORER, "Show in Explorer")
        folder = os.path.dirname(a[3]) if kind != MATERIAL else None
        if kind in IMAGE_KINDS:                          # the guess was wrong: re-sort this folder
            sub = c4d.BaseContainer()
            sub.InsData(1, "This Folder Is")
            forced = CONFIG.data.get("folder_kinds", {}).get(folder)
            sub.InsData(M_AUTO, "Auto (by name)" + ("&c&" if not forced else ""))
            for i, k in enumerate(SORTABLE):
                sub.InsData(M_KIND0 + i, KIND_NAME[k] + ("&c&" if forced == k else ""))
            bc.InsData(0, "")
            bc.SetContainer(M_AUTO - 1, sub)
        res = gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        if res == M_AUTO or M_KIND0 <= res < M_KIND0 + len(SORTABLE):
            self.set_folder_kind(folder, None if res == M_AUTO else SORTABLE[res - M_KIND0])
            return
        if res == M_APPLY:
            self.apply(a)
        elif res == M_NEW:
            if kind == MATERIAL:
                doc = c4d.documents.GetActiveDocument()
                if _rs_active(doc):
                    self._status(apply_material(doc, a, new=True, assign_to_selection=False))
            else:
                self.apply(a, new=True)
        elif res in (M_FAV, M_UNFAV):
            CONFIG.set_favorite(a[1], res == M_FAV)
            self._fill_combo(keep=self.current)
            self._status("%s %s" % ("Added" if res == M_FAV else "Removed", a[2]))
        elif res == M_EXPLORER:
            storage.ShowInFinder((a[4] or a[3]) if kind == MATERIAL else a[3], False)

    def _poll_reset(self):
        """Right-click on a slider resets it (like the Attribute Manager's arrows). Dialog
        gadgets don't report right-clicks, so the timer watches the button: a fresh press
        with the cursor over a visible slider of this panel."""
        user32 = ctypes.windll.user32
        down = bool(user32.GetAsyncKeyState(0x02) & 0x8000)
        fresh, self.rdown = down and not self.rdown, down
        if not fresh or not self.IsVisible():
            return
        pt = ctypes.wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        o = self.Local2Screen()
        x, y = pt.x - o["x"], pt.y - o["y"]
        for gid, value in RESETS.items():
            d = self.GetItemDim(gid)
            if d and d["w"] > 0 and d["x"] <= x < d["x"] + d["w"] and d["y"] <= y < d["y"] + d["h"]:
                if not self.IsEnabled(gid):
                    return
                if gid == SL_ROT:
                    self.SetFloat(gid, value, -math.pi, math.pi, ROT_STEP, c4d.FORMAT_DEGREE)
                elif gid == SL_EXP:
                    self.SetFloat(gid, value, -5.0, 5.0, 0.1)
                elif gid == SL_LUT:
                    self.SetFloat(gid, value, 0.0, 1.0, 0.01, c4d.FORMAT_PERCENT)
                else:
                    self.SetFloat(gid, value, 0.0, 1.0, 0.01)
                self.Command(gid, c4d.BaseContainer())          # applied as one plain (undoable) change
                return

    def set_folder_kind(self, folder, kind):
        """Pin what a folder's single images are (None = back to guessing by name)."""
        fk = CONFIG.data.setdefault("folder_kinds", {})
        if kind:
            fk[folder] = kind
        else:
            fk.pop(folder, None)
        CONFIG.save()
        LIBRARY.rebuild()
        self._fill_kinds()
        self._status("%s: %s" % (os.path.basename(folder), KIND_NAME[kind] if kind else "sorted by name"))

    def ask_folder_kind(self):
        """+ Folder: what does it hold? Auto keeps the name-based guess."""
        bc = c4d.BaseContainer()
        bc.InsData(M_AUTO, "Mixed / detect by name")
        bc.InsData(0, "")
        for i, k in enumerate(SORTABLE):
            bc.InsData(M_KIND0 + i, "All " + KIND_NAME[k])
        res = gui.ShowPopupDialog(cd=None, bc=bc, x=c4d.MOUSEPOS, y=c4d.MOUSEPOS)
        return SORTABLE[res - M_KIND0] if M_KIND0 <= res < M_KIND0 + len(SORTABLE) else None

    # ------------------------------------------------------------ scene state

    def _pull_scene(self):
        doc = c4d.documents.GetActiveDocument()
        used = set()
        if self.kind == HDRI:
            dome = find_dome(doc)
            self.active = light_path(dome, RS_DOME_TEX)
            used = set(light_path(d, RS_DOME_TEX) for d in find_domes(doc))
            on = dome is not None
            for g in (SL_ROT, SL_EXP, CK_BG):
                self.Enable(g, on)
            if on:
                self.SetFloat(SL_ROT, math.remainder(dome[RS_DOME_ROTATE] or 0.0, 2.0 * math.pi),
                              -math.pi, math.pi, ROT_STEP, c4d.FORMAT_DEGREE)
                self.SetFloat(SL_EXP, dome[RS_DOME_EXPOSURE] or 0.0, -5.0, 5.0, 0.1)
                self.SetBool(CK_BG, bool(dome[RS_CAMERA_VIS]))
        elif self.kind == LUT:
            cams = target_cameras(doc)
            h = lut_holder(doc, cams[0]) if cams else None
            self.active = lut_path(h)
            used = {self.active} if self.active else set()
            for g in (SL_LUT, CK_LOG, B_NOLUT):
                self.Enable(g, bool(cams))
            if h is not None and self.active:
                self.SetFloat(SL_LUT, h[LUT_STRENGTH] or 0.0, 0.0, 1.0, 0.01, c4d.FORMAT_PERCENT)
                self.SetBool(CK_LOG, bool(h[LUT_LOG]))
        elif self.kind == MATERIAL:
            m = doc.GetFirstMaterial()
            while m:
                s = m.GetDataInstance().GetString(MARK)
                if s:
                    used.add(_norm(s))
                m = m.GetNext()
        self.used = used

    @staticmethod
    def _lut_target(doc):
        cams = target_cameras(doc)
        h = lut_holder(doc, cams[0]) if cams else None
        return h if lut_path(h) else None

    def _tweak(self, pid, value, msg, find=None):
        doc = c4d.documents.GetActiveDocument()
        dragging = msg.GetBool(c4d.BFM_ACTION_INDRAG)
        find = find or find_dome
        # the target is looked up once per drag, not per tick (finding a dome walks the scene)
        dome = self.drag_dome if self.in_undo and self.drag_dome and self.drag_dome.IsAlive() else find(doc)
        if dome is None:
            return
        if not self.in_undo:
            doc.StartUndo()
            doc.AddUndo(c4d.UNDOTYPE_CHANGE_SMALL, dome)
            self.in_undo = True
            self.drag_dome = dome
        dome[pid] = value
        if not dragging:
            doc.EndUndo()
            self.in_undo = False
            self.drag_dome = None
            c4d.EventAdd()
            return
        # Mid-drag the slider runs its own loop, so a queued EventAdd only lands on
        # release. Push the change through now: the viewport redraws synchronously and
        # a synchronous EVMSG_CHANGE reaches whoever listens (Redshift IPR) right away.
        dome.SetDirty(c4d.DIRTYFLAGS_DATA)
        c4d.DrawViews(c4d.DRAWFLAGS_ONLY_ACTIVE_VIEW | c4d.DRAWFLAGS_NO_THREAD | c4d.DRAWFLAGS_NO_ANIMATION)
        c4d.GeSyncMessage(c4d.EVMSG_CHANGE)

    def _step(self, d, rows=0):
        """Apply the item d places (or rows) away from the last one used. Left/right wrap
        around the collection; up/down stop at the ends."""
        if not self.items:
            return
        cur = self.cursor if any(_norm(a[1]) == self.cursor for a in self.items) else self.active
        idx = next((i for i, a in enumerate(self.items) if _norm(a[1]) == cur), -1)
        if rows:
            cols = self.grid.layout()[0]
            new = idx + rows * cols if idx >= 0 else 0
            if not 0 <= new < len(self.items):
                return
        else:
            new = (idx + d) % len(self.items)
        self.apply(self.items[new])
        self._reveal(new)

    def _reveal(self, i):
        """Scroll the grid so item i is in view."""
        cols, cell_w, draw_w, draw_h, cell_h = self.grid.layout()
        y = PAD + (i // cols) * cell_h
        x = PAD + (i % cols) * cell_w
        try:
            self.SetVisibleArea(G_SCROLL, x, max(0, y - GAP), x + draw_w, y + cell_h)
        except Exception:
            pass

    def arrow_key(self, key):
        """Arrow keys over the grid: step through looks you swap in place (HDRI, LUT,
        bokeh, light maps, gobos, IES). Materials and imperfections would pile up, so no."""
        if self.kind not in ARROW_KINDS:
            return False
        step = {c4d.KEY_LEFT: (-1, 0), c4d.KEY_RIGHT: (1, 0), c4d.KEY_UP: (0, -1), c4d.KEY_DOWN: (0, 1)}.get(key)
        if step is None:
            return False
        self._step(step[0], step[1])
        return True

    # ------------------------------------------------------------ events

    def _status(self, msg=None):
        parts = [msg] if msg else []
        if self.scan_msg and LIBRARY.busy():
            parts.append(self.scan_msg)
        shown = len(self.items)
        parts.append("%d item%s" % (shown, "" if shown == 1 else "s") if shown == len(self.all_items)
                     else "%d of %d" % (shown, len(self.all_items)))
        if not self.ffmpeg:
            parts.append("ffmpeg not found - fewer thumbnails")
        elif self.pending:
            parts.append("making %d thumbnail%s" % (len(self.pending), "" if len(self.pending) == 1 else "s"))
        self.SetString(T_STATUS, "  ·  ".join(parts))

    def Command(self, cid, msg):
        try:
            if cid == CB_KIND:
                i = self.GetInt32(CB_KIND)
                if 0 <= i < len(self.kind_choices):
                    self._set_kind(self.kind_choices[i])
                    self._pull_scene()
            elif cid == CB_COLL:
                i = self.GetInt32(CB_COLL)
                if 0 <= i < len(self.choices):
                    self._open(self.choices[i])
            elif cid == E_SEARCH:
                self._filter()
                self._status()
            elif cid == B_REFRESH:
                full = bool(msg.GetInt32(c4d.BFM_ACTION_QUAL) & c4d.QSHIFT)
                if LIBRARY.start_scan(full):
                    self.scan_msg = "Re-reading everything..." if full else "Scanning..."
                    self._status()
            elif cid == B_ADDROOT:
                folder = storage.LoadDialog(flags=c4d.FILESELECT_DIRECTORY, title="Add an asset folder")
                if folder and folder not in CONFIG.data["roots"]:
                    CONFIG.data["roots"].append(folder)
                    kind = self.ask_folder_kind()
                    if kind:
                        CONFIG.data.setdefault("folder_kinds", {})[folder] = kind
                    CONFIG.save()
                    LIBRARY.start_scan()
                    self.scan_msg = "Scanning..."
                    self._status()
            elif cid == SL_SIZE:
                CONFIG.data["size"] = self.GetInt32(SL_SIZE)
                if not msg.GetBool(c4d.BFM_ACTION_INDRAG):
                    CONFIG.save()
                self.grid.LayoutChanged()
                self.grid.Redraw()
            elif cid == CK_DISP:
                CONFIG.data["displacement"] = self.GetBool(CK_DISP)
                CONFIG.save()
            elif cid == SL_IMP:
                CONFIG.data["imperfection"] = round(self.GetFloat(SL_IMP), 3)
                if not msg.GetBool(c4d.BFM_ACTION_INDRAG):
                    CONFIG.save()
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
            elif cid == SL_LUT:
                self._tweak(LUT_STRENGTH, self.GetFloat(SL_LUT), msg, self._lut_target)
            elif cid == CK_LOG:
                self._tweak(LUT_LOG, self.GetBool(CK_LOG), msg, self._lut_target)
            elif cid == B_NOLUT:
                self._status(remove_lut(c4d.documents.GetActiveDocument()))
                self._pull_scene()
                self.grid.Redraw()
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
                files = [f for f in files if isinstance(f, str) and f.lower().endswith(HDR_EXTS)]
                if not files:
                    return self.SetDragDestination(c4d.MOUSE_FORBIDDEN)
                if msg.GetInt32(c4d.BFM_DRAG_FINISHED):
                    self.apply([HDRI, files[0], os.path.basename(files[0]), files[0], files[0], None])
                    return True
                return self.SetDragDestination(c4d.MOUSE_POINT_HAND)
            except Exception:
                _log_error("Message/drag")
        return gui.GeDialog.Message(self, msg, result)

    def CoreMessage(self, id, msg):
        if id == c4d.EVMSG_CHANGE and not self.in_undo:
            try:
                before = (self.active, frozenset(self.used))
                self._pull_scene()
                if before != (self.active, frozenset(self.used)):
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
    def __init__(self, kind=None):
        self.kind = kind

    def Execute(self, doc):
        if self.kind:
            PANEL.show_kind(self.kind)
        return PANEL.Open(c4d.DLG_TYPE_ASYNC, ID_LIBRARY, defaultw=680, defaulth=560)

    def RestoreLayout(self, secret):
        return PANEL.Restore(ID_LIBRARY, secret)


def _icon():
    bmp = bitmaps.BaseBitmap()
    ok = bmp.InitWith(os.path.join(os.path.dirname(__file__), "res", "icon.png"))
    ok = ok[0] if isinstance(ok, tuple) else ok
    return bmp if ok == c4d.IMAGERESULT_OK else None


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(
        ID_LIBRARY, "Library", 0, _icon(),
        "Browse your materials, HDRIs, imperfections, light maps, gobos and IES as thumbnails",
        PanelCommand())
    # the old HDRI command: shortcuts and layouts bound to it open Library on HDRI
    plugins.RegisterCommandPlugin(
        ID_HDRI, "HDRI", c4d.PLUGINFLAG_HIDEPLUGINMENU, _icon(),
        "Library, on HDRIs", PanelCommand(HDRI))
