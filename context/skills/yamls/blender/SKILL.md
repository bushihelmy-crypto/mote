---
name: blender
description: >-
  Drive Blender headlessly for 3D scene building, geometry/material edits, and
  rendering via the official `blender --background --python` interface using the
  bpy Python API. Produces real renders (PNG/EXR) and .blend files without a GUI.
when_to_use: >-
  When you need to create/modify a 3D scene or render an image/animation from
  Blender — build geometry, set materials/cameras/lights, then render. Author a
  bpy script and run it with the official blender binary via the Bash tool.
---

# Blender headless (official binary + bpy, via Bash)

The correct way to drive Blender from an agent is the **official `blender`
binary in background mode running a bpy Python script** — not a reimplementation.
You write a `.py` script using the `bpy` API, Blender executes it and does the
real rendering.

## Prerequisites

- Blender installed (>= 4.2 recommended): https://www.blender.org/download/
- Verify: `blender --version`

## The core pattern

Write a bpy script (use the Write tool), then run it headlessly:

```bash
blender --background --python scene.py
# --background (-b): no GUI
# --python: run this script against the loaded/empty scene
```

Run against an existing .blend file:
```bash
blender --background my_scene.blend --python edit_and_render.py
```

Pass args to the script after `--`:
```bash
blender --background --python render.py -- --out /abs/path/out.png --frame 1
```

## Example bpy script (build + render)

```python
import bpy, sys, math

# Clean slate
bpy.ops.object.select_all(action="SELECT")
bpy.ops.object.delete()

# Add a cube
bpy.ops.mesh.primitive_cube_add(location=(0, 0, 0))
cube = bpy.context.active_object

# Material
mat = bpy.data.materials.new("Red")
mat.use_nodes = True
bsdf = mat.node_tree.nodes["Principled BSDF"]
bsdf.inputs["Base Color"].default_value = (0.8, 0.1, 0.1, 1.0)
cube.data.materials.append(mat)

# Camera + light
bpy.ops.object.camera_add(location=(5, -5, 4), rotation=(math.radians(60), 0, math.radians(45)))
bpy.context.scene.camera = bpy.context.active_object
bpy.ops.object.light_add(type="SUN", location=(3, -3, 6))

# Render settings and output (ALWAYS absolute paths)
scene = bpy.context.scene
scene.render.engine = "BLENDER_EEVEE_NEXT"   # or "CYCLES"
scene.render.image_settings.file_format = "PNG"
scene.render.filepath = "/abs/path/out.png"
scene.render.resolution_x, scene.render.resolution_y = 1920, 1080

bpy.ops.render.render(write_still=True)
```

Common render tweaks:
```python
scene.render.engine = "CYCLES"
scene.cycles.samples = 128
scene.frame_set(24)                     # a specific frame
# Animation: bpy.ops.render.render(animation=True) with frame_start/frame_end
```

## Rendering an existing .blend without editing

```bash
blender -b my_scene.blend -o /abs/out_#### -f 1        # render frame 1
blender -b my_scene.blend -o /abs/anim_#### -a          # render whole animation
```

## Agent guidance

1. **Use absolute paths** for `render.filepath` and outputs — relative paths
   fail unpredictably in background mode.
2. Blender is a **hard dependency**; if `blender --version` fails, stop and tell
   the user to install it. Do not fake renders in Python (PIL etc.).
3. After rendering, **verify the output file exists and size > 0**; the render
   "running without error" is not proof it produced an image.
4. `BLENDER_EEVEE_NEXT` is fast for previews; `CYCLES` for final quality (slower,
   needs sample count).
5. Keep the bpy script alongside outputs so the scene stays reproducible.
6. Blender's Python is its own bundled interpreter — import only `bpy` and the
   stdlib inside these scripts, not your project's packages.
