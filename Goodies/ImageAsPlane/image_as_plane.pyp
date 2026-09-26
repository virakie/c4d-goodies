"""
Image As Plane  -  Cinema 4D 2026 Plugin
Pick images; each becomes a plane sized to the image with a matching material.
Lives in plugins/Goodies/ImageAsPlane. Restart C4D after changes.
"""

import c4d
import c4d.plugins
import c4d.gui
import maxon
import sys
import os

PLUGIN_ID   = 1064371
PLUGIN_NAME = "Image As Plane"
PLUGIN_HELP = "Import images as planes sized to the image, with a material"

# ── Settings ──────────────────────────────────────────────────────────────────
PPU       = 1.0    # pixels per unit (increase to shrink planes)
PLANE_GAP = 50.0   # gap between planes when importing multiple

# ── RS node IDs (confirmed working in C4D 2026) ───────────────────────────────
RS_NODESPACE   = "com.redshift3d.redshift4c4d.class.nodespace"
RS_TEXSAMPLER  = "com.redshift3d.redshift4c4d.nodes.core.texturesampler"
RS_MATERIAL    = "com.redshift3d.redshift4c4d.nodes.core.material"
RS_OUTPUT      = "com.redshift3d.redshift4c4d.node.output"
TEX_OUT        = "com.redshift3d.redshift4c4d.nodes.core.texturesampler.outcolor"
MAT_DIFFUSE    = "com.redshift3d.redshift4c4d.nodes.core.material.diffuse_color"
MAT_EMISSION   = "com.redshift3d.redshift4c4d.nodes.core.material.emission_color"
MAT_EMIS_W     = "com.redshift3d.redshift4c4d.nodes.core.material.emission_weight"
MAT_REFL_W     = "com.redshift3d.redshift4c4d.nodes.core.material.refl_weight"
MAT_REFL_ROUGH = "com.redshift3d.redshift4c4d.nodes.core.material.refl_roughness"
MAT_OUT        = "com.redshift3d.redshift4c4d.nodes.core.material.outcolor"
OUTPUT_SURFACE = "com.redshift3d.redshift4c4d.node.output.surface"


# ── File picker ───────────────────────────────────────────────────────────────

def pick_files_windows(title):
    import ctypes, ctypes.wintypes

    class OPENFILENAME(ctypes.Structure):
        _fields_ = [
            ("lStructSize",       ctypes.c_uint32),
            ("hwndOwner",         ctypes.wintypes.HWND),
            ("hInstance",         ctypes.wintypes.HINSTANCE),
            ("lpstrFilter",       ctypes.c_wchar_p),
            ("lpstrCustomFilter", ctypes.c_wchar_p),
            ("nMaxCustFilter",    ctypes.c_uint32),
            ("nFilterIndex",      ctypes.c_uint32),
            ("lpstrFile",         ctypes.c_void_p),
            ("nMaxFile",          ctypes.c_uint32),
            ("lpstrFileTitle",    ctypes.c_wchar_p),
            ("nMaxFileTitle",     ctypes.c_uint32),
            ("lpstrInitialDir",   ctypes.c_wchar_p),
            ("lpstrTitle",        ctypes.c_wchar_p),
            ("Flags",             ctypes.c_uint32),
            ("nFileOffset",       ctypes.c_uint16),
            ("nFileExtension",    ctypes.c_uint16),
            ("lpstrDefExt",       ctypes.c_wchar_p),
            ("lCustData",         ctypes.c_long),
            ("lpfnHook",          ctypes.c_void_p),
            ("lpTemplateName",    ctypes.c_wchar_p),
            ("pvReserved",        ctypes.c_void_p),
            ("dwReserved",        ctypes.c_uint32),
            ("FlagsEx",           ctypes.c_uint32),
        ]

    buf_size = 65536
    buf      = ctypes.create_unicode_buffer(buf_size)
    ofn      = OPENFILENAME()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAME)
    ofn.lpstrFilter = "Images\0*.jpg;*.jpeg;*.png;*.tif;*.tiff;*.exr;*.hdr;*.bmp\0All Files\0*.*\0\0"
    ofn.lpstrFile   = ctypes.cast(buf, ctypes.c_void_p)
    ofn.nMaxFile    = buf_size
    ofn.lpstrTitle  = title
    ofn.Flags       = 0x00000200 | 0x00080000 | 0x00001000

    if not ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn)):
        return []

    tokens, i = [], 0
    while i < buf_size:
        end = i
        while end < buf_size and buf[end] != "\0":
            end += 1
        tok = "".join(buf[i:end])
        if not tok:
            break
        tokens.append(tok)
        i = end + 1

    if len(tokens) == 1:
        return [tokens[0]]
    d = tokens[0].rstrip("\\")
    return [d + "\\" + f for f in tokens[1:]]


def pick_files_mac(title):
    try:
        from AppKit import NSOpenPanel
        panel = NSOpenPanel.openPanel()
        panel.setTitle_(title)
        panel.setAllowsMultipleSelection_(True)
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowedFileTypes_(["jpg","jpeg","png","tif","tiff","exr","hdr","bmp"])
        if panel.runModal() == 1:
            return [url.path() for url in panel.URLs()]
        return []
    except Exception:
        paths, last_dir = [], ""
        while True:
            path = c4d.storage.LoadDialog(
                title="Add image " + str(len(paths)+1) + " — Cancel when done",
                flags=c4d.FILESELECT_LOAD, force_suffix="", def_path=last_dir, def_file=""
            )
            if not path:
                break
            if path not in paths:
                paths.append(path)
                last_dir = "/".join(path.replace("\\","/").split("/")[:-1])
        return paths


def pick_files(title="Select Images  (Shift / Ctrl for multi-select)"):
    if sys.platform == "win32":
        return pick_files_windows(title)
    if sys.platform == "darwin":
        return pick_files_mac(title)
    paths = []
    while True:
        path = c4d.storage.LoadDialog(
            title="Add image " + str(len(paths)+1) + " — Cancel when done",
            flags=c4d.FILESELECT_LOAD, force_suffix="", def_path="", def_file=""
        )
        if not path:
            break
        if path not in paths:
            paths.append(path)
    return paths


# ── Helpers ───────────────────────────────────────────────────────────────────

def get_render_engine(doc):
    rd = doc.GetActiveRenderData()
    if not rd:
        return "standard"
    vid = rd[c4d.RDATA_RENDERENGINE]
    if vid == 1036219: return "redshift"
    if vid == 1029525: return "octane"
    return "standard"


def get_image_size(path):
    bmp    = c4d.bitmaps.BaseBitmap()
    result = bmp.InitWith(path)
    ok     = result[0] if isinstance(result, tuple) else result
    if ok != c4d.IMAGERESULT_OK:
        return None, None
    return bmp.GetBw(), bmp.GetBh()


def basename(path):
    name = path.replace("\\", "/").split("/")[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


# ── Materials ─────────────────────────────────────────────────────────────────

def set_float(port, value):
    """Set a float input; tries Float32 first (Redshift's port type), then a plain float."""
    if port is None or port.IsNullValue():
        print("[Image As Plane] port not found, left at default")
        return
    for v in (maxon.Float32(value), maxon.Float64(value), value):
        try:
            port.SetPortValue(v)
            return
        except Exception:
            continue
    print("[Image As Plane] could not set", port)


def make_redshift_material(doc, name, tex_path):
    mat = c4d.BaseMaterial(c4d.Mmaterial)
    mat.SetName(name)
    doc.InsertMaterial(mat)
    try:
        nmr = mat.GetNodeMaterialReference()
        rs_ns = maxon.Id(RS_NODESPACE)
        nmr.AddGraph(rs_ns)
        g = nmr.GetGraph(rs_ns)
        if g.IsNullValue():
            raise RuntimeError("RS graph is null")

        with g.BeginTransaction() as t:
            # Reuse existing default RS Material node
            rs_mat_nodes = maxon.GraphModelHelper.FindNodesByAssetId(
                g, maxon.Id(RS_MATERIAL), True)
            rs_node = rs_mat_nodes[0] if rs_mat_nodes else g.AddChild(
                maxon.Id(), maxon.Id(RS_MATERIAL), maxon.DataDictionary())

            # Texture sampler
            tex = g.AddChild(maxon.Id(), maxon.Id(RS_TEXSAMPLER), maxon.DataDictionary())
            tex0 = tex.GetInputs().FindChild(
                "com.redshift3d.redshift4c4d.nodes.core.texturesampler.tex0")
            pp = tex0.FindChild("path")
            if not pp.IsNullValue():
                pp.SetDefaultValue(maxon.Url(tex_path))

            # Output node
            out_nodes = maxon.GraphModelHelper.FindNodesByAssetId(
                g, maxon.Id(RS_OUTPUT), True)
            out_node = out_nodes[0] if out_nodes else None

            tex_out = tex.GetOutputs().FindChild(TEX_OUT)

            # Texture -> diffuse
            d = rs_node.GetInputs().FindChild(MAT_DIFFUSE)
            if not tex_out.IsNullValue() and not d.IsNullValue():
                tex_out.Connect(d)

            # Texture -> emission
            e = rs_node.GetInputs().FindChild(MAT_EMISSION)
            if not tex_out.IsNullValue() and not e.IsNullValue():
                tex_out.Connect(e)

            # No reflection, emission at full strength (a flat, unlit-looking card)
            for pid, val in ((MAT_REFL_W, 0.0), (MAT_REFL_ROUGH, 0.0), (MAT_EMIS_W, 1.0)):
                set_float(rs_node.GetInputs().FindChild(pid), val)

            # RS Material -> Output
            if out_node:
                mo  = rs_node.GetOutputs().FindChild(MAT_OUT)
                osu = out_node.GetInputs().FindChild(OUTPUT_SURFACE)
                if not mo.IsNullValue() and not osu.IsNullValue():
                    mo.Connect(osu)

            t.Commit()
        mat.Update(True, True)
    except Exception as e:
        print("[Image As Plane] RS error:", e)
        mat.Remove()
        mat = make_standard_material(doc, name, tex_path)
    return mat


def make_octane_material(doc, name, tex_path):
    try:
        mat = c4d.BaseMaterial(1029501)
        mat.SetName(name)
        sh = c4d.BaseShader(1029508)
        sh[c4d.IMAGETEXTURE_FILE] = tex_path
        mat[800] = sh
        mat.InsertShader(sh)
        mat.Update(True, True)
        doc.InsertMaterial(mat)
        return mat
    except Exception as e:
        print("[Image As Plane] Octane error:", e)
        return make_standard_material(doc, name, tex_path)


def make_standard_material(doc, name, tex_path):
    mat = c4d.BaseMaterial(c4d.Mmaterial)
    mat.SetName(name)
    mat[c4d.MATERIAL_USE_COLOR] = True
    sh = c4d.BaseShader(c4d.Xbitmap)
    sh[c4d.BITMAPSHADER_FILENAME] = tex_path
    mat[c4d.MATERIAL_COLOR_SHADER] = sh
    mat.InsertShader(sh)
    mat.Update(True, True)
    doc.InsertMaterial(mat)
    return mat


# ── Plane creator ─────────────────────────────────────────────────────────────

def create_plane_for_image(doc, path, engine, x_offset):
    w_px, h_px = get_image_size(path)
    if not w_px:
        return None, 0

    name = basename(path)
    w_u  = w_px / PPU
    h_u  = h_px / PPU

    plane = c4d.BaseObject(c4d.Oplane)
    plane.SetName(name)
    plane[c4d.PRIM_PLANE_WIDTH]  = w_u
    plane[c4d.PRIM_PLANE_HEIGHT] = h_u
    plane[c4d.PRIM_PLANE_SUBW]   = 1
    plane[c4d.PRIM_PLANE_SUBH]   = 1
    plane[c4d.PRIM_AXIS]         = 5
    plane.SetAbsPos(c4d.Vector(x_offset + w_u * 0.5, 0, 0))
    doc.InsertObject(plane)
    doc.AddUndo(c4d.UNDOTYPE_NEW, plane)

    mat_name = name + "_mat"
    if engine == "redshift":
        mat = make_redshift_material(doc, mat_name, path)
    elif engine == "octane":
        mat = make_octane_material(doc, mat_name, path)
    else:
        mat = make_standard_material(doc, mat_name, path)
    doc.AddUndo(c4d.UNDOTYPE_NEW, mat)

    tag = plane.MakeTag(c4d.Ttexture)
    tag[c4d.TEXTURETAG_MATERIAL]   = mat
    tag[c4d.TEXTURETAG_PROJECTION] = c4d.TEXTURETAG_PROJECTION_UVW
    doc.AddUndo(c4d.UNDOTYPE_NEW, tag)

    return plane, w_u


# ── Plugin class ──────────────────────────────────────────────────────────────

class ImageAsPlaneCmd(c4d.plugins.CommandData):

    def Execute(self, doc):
        paths = pick_files("Select Images  (Shift / Ctrl for multi-select)")
        if not paths:
            return True

        engine = get_render_engine(doc)
        c4d.StopAllThreads()
        doc.StartUndo()

        x_offset        = 0.0
        imported, failed = [], []

        for path in paths:
            plane, w = create_plane_for_image(doc, path, engine, x_offset)
            if plane:
                imported.append(plane)
                x_offset += w + PLANE_GAP
            else:
                failed.append(basename(path))

        doc.EndUndo()

        for i, plane in enumerate(imported):
            doc.SetActiveObject(plane, c4d.SELECTION_NEW if i == 0 else c4d.SELECTION_ADD)

        c4d.EventAdd()

        msg = "Image As Plane: imported %d plane(s) (%s)" % (len(imported), engine.capitalize())
        if failed:
            msg += "  -  could not read %d: %s" % (len(failed), ", ".join(failed))
        print("[Image As Plane]", msg)
        c4d.StatusSetText(msg)
        return True

    def GetState(self, doc):
        return c4d.CMD_ENABLED


# ── Register (must be at module level for .pyp files) ─────────────────────────

def _load_icon():
    try:
        p = os.path.join(os.path.dirname(__file__), "res", "icon.png")
        if not os.path.isfile(p):
            return None
        bmp = c4d.bitmaps.BaseBitmap()
        ok  = bmp.InitWith(p)
        ok  = ok[0] if isinstance(ok, tuple) else ok
        return bmp if ok == c4d.IMAGERESULT_OK else None
    except Exception:
        return None


_registered = c4d.plugins.RegisterCommandPlugin(
    id   = PLUGIN_ID,
    str  = PLUGIN_NAME,
    info = 0,
    icon = _load_icon(),
    help = PLUGIN_HELP,
    dat  = ImageAsPlaneCmd()
)
print("[Image As Plane]", "Registered OK" if _registered else "FAILED to register")
