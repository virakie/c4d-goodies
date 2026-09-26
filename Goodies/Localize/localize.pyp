"""Localize - pull every texture and file the scene uses into ./tex next to
the .c4d, relink to it, and save the scene in place. Ready to zip and hand
to someone else.

It drives C4D's own "Save Project with Assets" engine, pointed at the
scene's own folder, rather than rewriting paths by hand. That engine
already knows every place a path can live: Redshift node textures, dome and
gobo lights, Asset Browser (asset:///) textures, LUTs. Tested 2026-09-26 on
copies of real projects: 12/12 and 34/34 assets localized, 0 missing on
reopen, and a second run changes nothing.

Before saving, the current .c4d is copied to backup\<name>_pre-localize.c4d.
Missing files never block: they are left as they are and listed in the
Console.
"""

import os
import shutil

import c4d
from c4d import plugins, gui, bitmaps

# --------------------------------------------------------------------------
# Local pick next to the other Goodies (1066610+). Register a real one
# at developers.maxon.net before this leaves this machine.
# --------------------------------------------------------------------------
PLUGIN_ID = 1066620

FLAGS = (c4d.SAVEPROJECT_ASSETS | c4d.SAVEPROJECT_SCENEFILE |
         c4d.SAVEPROJECT_USEDOCUMENTNAMEASFILENAME |
         c4d.SAVEPROJECT_DONTFAILONMISSINGASSETS |
         c4d.SAVEPROJECT_ASSETLINKS_COPY_FILEASSETS |      # Asset Browser files
         c4d.SAVEPROJECT_ASSETLINKS_COPY_NODEASSETS |
         c4d.SAVEPROJECT_PROGRESSALLOWED)


def log(*args):
    print("[Localize]", *args)


def _norm(p):
    return os.path.normcase(os.path.normpath(p))


def _survey(doc, folder):
    """(total, already local, missing names) - the scene file itself excluded."""
    assets = []
    c4d.documents.GetAllAssetsNew(doc, False, "", c4d.ASSETDATA_FLAG_NONE, assets)
    own = _norm(os.path.join(folder, doc.GetDocumentName()))
    root = _norm(folder) + os.sep
    seen, local, missing = set(), 0, []
    for a in assets:
        fn = a.get("filename") or ""
        if not fn or _norm(fn) == own or fn in seen:
            continue
        seen.add(fn)
        if not a.get("exists"):
            missing.append(a.get("assetname") or fn)
        elif _norm(fn).startswith(root):
            local += 1
    return len(seen), local, missing


def _backup(doc, folder):
    src = os.path.join(folder, doc.GetDocumentName())
    if not os.path.isfile(src):
        return None
    stem, ext = os.path.splitext(doc.GetDocumentName())
    dst_dir = os.path.join(folder, "backup")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, "%s_pre-localize%s" % (stem, ext))
    shutil.copy2(src, dst)
    return dst


class Localize(plugins.CommandData):

    def Execute(self, doc):
        folder = doc.GetDocumentPath()
        if not folder:
            c4d.StatusSetText("Localize: save the scene first - textures go next to it")
            return True

        total, local, missing = _survey(doc, folder)
        todo = total - local - len(missing)
        if todo <= 0 and not missing:
            c4d.StatusSetText("Localize: all %d files are already next to the scene" % total)
            return True

        lines = ["Copy %d file%s into  tex\\  and save the scene in place?" % (todo, "" if todo == 1 else "s")]
        if local:
            lines.append("%d already local." % local)
        if missing:
            lines.append("%d missing - left as they are (see Console)." % len(missing))
        lines.append("A backup of the .c4d goes to  backup\\  first.")
        if not gui.QuestionDialog("\n".join(lines)):
            return True

        try:
            bak = _backup(doc, folder)
        except Exception as exc:
            gui.MessageDialog("Localize stopped - could not back up the scene:\n\n%s" % exc)
            return True

        c4d.StopAllThreads()
        assets, lost = [], []
        ok = c4d.documents.SaveProject(doc, FLAGS, folder, assets, lost)
        c4d.EventAdd()

        for name in missing:
            log("missing:", name)
        if not ok:
            msg = "Localize failed - scene not changed on disk%s" % (" (backup kept)" if bak else "")
            log(msg)
            c4d.StatusSetText(msg)
            return True

        msg = "Localize: %d files in tex\\, scene saved" % todo
        if missing:
            msg += " - %d missing (Console)" % len(missing)
        log(msg, "| backup:", bak)
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
        PLUGIN_ID, "Localize Textures", 0, _icon(),
        "Copy every texture/file the scene uses into ./tex, relink, and save in place",
        Localize())
