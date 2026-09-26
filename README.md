# Goodies

## Install
[Download](https://github.com/virakie/c4d-goodies/archive/refs/heads/main.zip), put the **Goodies** folder in your C4D `plugins` folder (Preferences → Open Preferences Folder), restart. Tools are under **Extensions → Goodies**.

## Tools

### ![](img/icons/Axis.png) Axis
Axis to the centre of the geometry. **Shift:** bottom centre.

### ![](img/icons/SnapFloor.png) Snap to Floor
Drops the selection onto the floor. **Shift:** live tag that keeps it there.

### ![](img/icons/Solo.png) Solo
Solos the selection in viewport and render. Click again to restore. Can also be used with lighting workflow for solo-ing lights.

### ![](img/icons/CameraToggle.png) Camera Toggle
Flips between the scene camera and the editor camera.

### ![](img/icons/RenderRegion.png) Render Regions
Drag a render region in the viewport, save named regions, or fit one to the selection.

![](img/screens/render_region_viewport.png)

### ![](img/icons/Renamer.png) Rename
Blender-style rename popup. `Leg_##` numbers, `*_L` adds a suffix, `old>new` replaces.

![](img/screens/rename_popup.png)

### ![](img/icons/IconColor.png) Icon Color
Tints the selected objects' icons. **Shift:** display colour too. **Ctrl:** reset.

### ![](img/icons/ImageAsPlane.png) Image As Plane
Images to planes with matching materials (Redshift, Octane or Standard).

### ![](img/icons/HDRI.png) HDRI _(Redshift)_
Thumbnail browser for your HDRI folders. Click to light the scene. Thumbnails need [ffmpeg](https://ffmpeg.org/).

![](img/screens/hdri_panel.png)

### ![](img/icons/PuzzleMatte.png) PuzzleMatte _(Redshift)_
Drag objects in, Build: one matte AOV per object, or RGB packs.

![](img/screens/puzzlematte_panel.png)

### ![](img/icons/Localize.png) Localize Textures
Copies all textures next to the scene and relinks them.

## License
[MIT](LICENSE)
