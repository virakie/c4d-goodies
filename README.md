
https://github.com/user-attachments/assets/4dac5d5d-c4f4-4ed1-8ef8-1cd6c9bb7e72
# C4D Goodies

I hate paywalls, especially subscriptions. So I made a few quality-of-life plugins for my own work, and I'm sharing them here in case they help someone else :) Feel free to dig through the source, fork it, and make it your own.

Built with the help of LLMs. I test everything in my own work, but if you avoid AI-assisted tools, these probably aren't for you and I completely understand.

## Install
[Download](https://github.com/virakie/c4d-goodies/archive/refs/heads/main.zip), put the **Goodies** folder in your C4D `plugins` folder (Preferences → Open Preferences Folder), restart. Tools are under **Extensions → Goodies**.

## Tools

### ![](img/icons/Library.png) Library _(Redshift)_
Asset browser for your own folders: PBR materials, HDRIs, imperfections, light maps, gobos, bokeh, IES and LUTs (Redshift's own included). Click to apply, drag materials onto objects, arrow keys to flip through looks. Add folders with **+ Folder**. Thumbnails need [ffmpeg](https://ffmpeg.org/).

No assets are included: Library browses folders/subfolders you already own, and cleverly filters into their category.

https://github.com/user-attachments/assets/c69558ff-a72f-41e8-b965-dd5750cdcdcf

---

### ![](img/icons/Ease.png) Ease
A curve editor for keyframe easing, you know the deal lol.

![](img/screens/ease_panel.png)

---

### ![](img/icons/PuzzleMatte.png) PuzzleMatte _(Redshift)_
Drag objects in, Build: one matte AOV per object, or RGB packs.


https://github.com/user-attachments/assets/2c26a72c-e96e-4404-8ba1-a0890b0e31f8


---

### ![](img/icons/RenderRegion.png) Render Regions
Drag a render region in the viewport, save named regions, or fit one to the selection.

![](img/screens/render_region_viewport.png)

---

### ![](img/icons/Renamer.png) Rename
Blender-style rename popup. `Name_##` numbers, `*_suffix` adds a suffix, `old>new` replaces.

![](img/screens/rename_demo.gif)

---

### ![](img/icons/Eyeballer.png) Eyeballer _(Windows)_
Live look-dev views of the viewport or Redshift RenderView (brightness, light & shadow, exposure zones, saturation…) with DaVinci-style scopes. Needs Python 3 with `pip install numpy pillow pywin32`. Good if you don't trust your eyes.

https://github.com/user-attachments/assets/23fb22d4-fa0d-4fe6-a2d7-0b1ebfffed84

---

### ![](img/icons/Axis.png) Axis
Axis to the centre of the geometry. **Shift:** bottom centre.

---

### ![](img/icons/SnapFloor.png) Snap to Floor
Drops the selection onto the floor. **Shift:** live tag that keeps it there.

---

### ![](img/icons/Solo.png) Solo
Solos the selection in viewport and render. Click again to restore. Can also be used with lighting workflow for solo-ing lights.

---

### ![](img/icons/CameraToggle.png) Camera Toggle
Flips between the scene camera and the editor camera.

---

### ![](img/icons/IconColor.png) Icon Color
Tints the selected objects' icons. **Shift:** display colour too. **Ctrl:** reset.

---

### ![](img/icons/ImageAsPlane.png) Image As Plane
Images to planes with matching materials (Redshift, Octane or Standard).

---

### ![](img/icons/Localize.png) Localize Textures
Copies all textures next to the scene and relinks them.

---

### ![](img/icons/Timelapse.png) Timelapse _(Windows)_
Records your working session as a timelapse, skipping idle time. Needs [ffmpeg](https://ffmpeg.org/).

---

## License
[MIT](LICENSE)
