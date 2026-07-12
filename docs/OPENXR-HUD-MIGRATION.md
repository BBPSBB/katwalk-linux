# OpenXR-only migration: single-layer HUD + locomotion, drop SteamVR

Status: COMPLETE. P0-P6 done and validated in-headset. The repo has exactly one
VR path: the OpenXR layer + overlay_xr.
Goal: run entirely on WiVRn + XRizer (and any OpenXR-native game on any OpenXR runtime),
with one VR-facing native component instead of three. No SteamVR, no OpenVR driver, no
OpenVR background apps.

## Why

The current build has three VR-facing pieces, two of which are SteamVR-only:

- `openxr-driver/` (the API layer): injects locomotion into the game's thumbstick. Runtime
  agnostic, already works on WiVRn+XRizer. KEEP and extend.
- `openvr-driver/` (treadmill-role driver): SteamVR only. DROP.
- `katwalk/overlay.py` (IVROverlay HUD) + `katwalk/io/openvr_pose.py` (HMD yaw): both are
  OpenVR background apps. They cannot attach on WiVRn+XRizer (no vrserver; XRizer excludes
  overlay/background apps). REPLACE with the layer.

OpenXR is the convergence point: native OpenXR games reach the layer directly, and OpenVR
games reach it through XRizer (which is itself an OpenXR app, so implicit layers apply on
top of it). One implicit API layer therefore rides every path on the target stack. The only
case it cannot reach is a native-OpenVR game running directly on SteamVR, which does not
exist on the WiVRn+XRizer stack we are moving to.

## Target architecture

Left: the two host processes. Middle: the four shm regions that form the bus. Right: the
layer, living inside the game process. Arrows show direction and payload.

```
                                              ┌─────────────────────┐
  ┌───────────────────────┐      stick x,y ┌─▶│ shm  katwalk/input  ├────────────────┐
  │ daemon.py             ├────────────────┘  └─────────────────────┘                │
  │ hardware + locomotion │◀─┐                                                       │
  └───────────────────────┘  │                                                       │
                             │                                                       │ inject locomotion
                     hmd_yaw │                                                       │
                             │                ┌─────────────────────┐                │
                             └────────────────┤ shm  katwalk/poses  │◀─┐             │
                                              └──────────┬──────────┘  │             │  ┌─────────────────────────┐
                                                         │             │             └─▶│ libkatwalk_xr_layer.so  │
                                                         │             └────────────────┤ (in game process,       │
                                                         │                           ┌─▶│ both stacks)            │
                               ┌─────────────────────────┘                           │  └────────────┬────────────┘
                               │                                                     │ composite quad│
                               │              ┌───────────────────┐                  │               │
                   gate / drag │           ┌─▶│ shm  katwalk/hud  ├──────────────────┘               │
                               │           │  └───────────────────┘                                  │
                               │           │ frame + anchor                            ray-cast hits │
  ┌─────────────────────────┐  │           │                                                         │
  │ overlay.py              │◀─┘           │                                                         │
  │ UI brain: render, tabs, ├──────────────┘                                                         │
  │ drag, gate, calib       │◀─┐                                                                     │
  └─────────────────────────┘  │ clicks       ┌─────────────────────┐                                │
                               └──────────────┤ shm  katwalk/laser  │◀───────────────────────────────┘
                                              └─────────────────────┘
```

The layer does, all in one `.so`:
- hook `xrCreateSession`: capture the Vulkan graphics binding
- inject its own action set (grip/aim pose, trigger, grip)
- hook `xrEndFrame`: upload the HUD frame to a swapchain, append an `XrCompositionLayerQuad`,
  ray-cast the pointing hand's aim at the panel to produce a cursor hit
- hook `xrPollEvent`: reference-space-change to recenter
- publish HMD/controller poses + head-yaw
- read `katwalk/input`, inject locomotion (EXISTING)

overlay.py keeps its brain. Only its three SteamVR touch points change:
`IVROverlay` output, `getDeviceToAbsoluteTrackingPose` pose reads, and
`pollNextOverlayEvent` click reads all become shared-memory reads/writes.

## The shm contract

All little-endian, packed, mirrored between a C header and a Python `struct` layout (same
pattern as the existing `KatInput`). Paths resolved with the same fallback list the layer
already uses (`$XDG_RUNTIME_DIR/katwalk/...`, `/run/user/<uid>/katwalk/...`, `/tmp/katwalk/...`).

- `katwalk/input`  (EXISTING, unchanged): `{ float x,y; uint32 buttons,seq; }`
  daemon -> layer. The locomotion bus. Untouched by this work.

- `katwalk/hud`  (overlay -> layer): header + RGBA framebuffer.
  `{ uint32 seq_start; uint32 w,h; uint32 visible; uint32 anchor;  // 0=off 1=left 2=right`
  `  float width_m; float transform[12];  // 3x4 quad pose relative to the anchor controller`
  `  uint32 seq_end; /* then w*h*4 RGBA bytes */ }`
  Torn-read guard: layer copies pixels only when `seq_start == seq_end` and seq changed.

- `katwalk/poses`  (layer -> overlay + daemon):
  `{ uint32 seq;`
  `  uint32 hmd_valid;   float hmd[12];`
  `  uint32 lctrl_valid; float lctrl[12];`
  `  uint32 rctrl_valid; float rctrl[12];`
  `  float  hmd_yaw_deg;`
  `  uint32 recenter_seq; }`
  All poses in one stable reference space (STAGE if available, else LOCAL). Daemon reads
  `hmd_yaw_deg` (replaces `HmdYaw`); overlay reads the matrices for the facing gate, hide
  distance, and drag math (same code it runs today, different source).

- `katwalk/laser`  (layer -> overlay):
  `{ uint32 seq; uint32 event; // 0=move 1=down 2=up`
  `  uint32 button; // 0=trigger 1=grip`
  `  float x,y; // image coords 0..w, 0..h`
  `  uint32 on_panel; }`
  overlay feeds these into its EXISTING click handlers (tabs, steppers, lock, drag). The
  image coords match what SteamVR's mouse events delivered, so the handlers are unchanged.

## Component changes

### openxr-driver/src/layer.cpp (the real work; Vulkan-first)

1. Hook `xrCreateSession`: walk the `next` chain for `XrGraphicsBindingVulkanKHR`; store
   VkInstance/PhysicalDevice/Device/queue. If the binding is GL or D3D, log and disable the
   HUD (locomotion still works). Create the reference space here.
2. Inject an action set: hook `xrAttachSessionActionSets`, append our action set (left/right
   grip pose, left/right aim pose, trigger bool, grip bool) to the app's array before calling
   down (the spec allows many sets in the array; only the call is once-per-session). Suggest
   bindings for khr/simple_controller and oculus/touch. Create action spaces for the pose
   actions. Hook `xrSyncActions` to append our set to `activeActionSets` each frame.
3. Create a Vulkan HUD swapchain (RGBA8, w*h). On `hud.seq` change, upload the framebuffer
   via a staging buffer + `vkCmdCopyBufferToImage` with layout transitions.
4. Hook `xrEndFrame`: locate controller poses + read trigger/grip at `frameEndInfo->displayTime`;
   compute quad world pose = controller_pose * hud.transform; append an `XrCompositionLayerQuad`
   (our swapchain, size {width_m, width_m*h/w}) to the app's layer array when `visible`.
5. Cursor: ray-cast the pointing hand's aim pose against the quad plane to get the hit point,
   publish it plus edge-detected trigger clicks to `katwalk/laser`. The overlay draws the hit as a
   cursor dot ON the panel (a mouse-pointer on the surface - there is NO rendered beam in space).
   Gated on `on_panel` so in-game trigger
   presses off-panel never toggle tabs.
6. Publish HMD + controller poses and `hmd_yaw_deg` to `katwalk/poses` each frame. Extend the
   existing reference-space-change hook to also bump `recenter_seq` (keep the `/tmp` recenter
   file for the daemon during transition, then retire it).
7. Reading our own actions does not consume the game's, so no input is stolen.

### katwalk/overlay.py (small; UI untouched)

- Add `--backend {openxr,openvr}`, default `openxr`. `openvr` kept only as a temporary escape
  hatch during the transition, removed at teardown.
- openxr mode: drop `openvr`/`glfw`/`OpenGL`; open the shm regions; render the existing Pillow
  frame to `katwalk/hud`; read poses + laser from shm and feed the existing gate/drag/click
  code. `--render-test` and `--demo` stay fully headless.

### katwalk/daemon.py

- Replace `HmdYaw` with a small reader of `katwalk/poses.hmd_yaw_deg`. Absent shm (no game
  running) -> `hmd_yaw=None` -> body-heading fallback, the same graceful degradation as today.
- Reword the `--output vr` log line (it now feeds the OpenXR layer, same shm).

### Teardown (last, gated on the above working)

- Delete `openvr-driver/`, `katwalk/io/openvr_pose.py`, the `openvr` backend in overlay.py.
- Remove `openvr`, `glfw`, `PyOpenGL` from `requirements.txt` (and `numpy` if tests confirm it
  is unused). Update `README.md`, `CHANGELOG.md`, and `./run` if needed.

## Phased sequence (each phase leaves a working system)

- P0  Shared shm headers (C + Python), design doc (this file).  [DONE: openxr-driver/src/katwalk_shm.h]
- P1  Layer: Vulkan binding capture + swapchain + quad from a STATIC test image, head-locked.
      Proves the hardest part (swapchain + xrEndFrame injection) on WiVRn before anything else.
      [DONE - validated in-headset on WiVRn + umu/Proton]
- P2  Layer reads `katwalk/hud`; overlay `--backend openxr` writes live frames, head-locked.
      Live HUD, no interaction yet.
      [DONE - implemented as `katwalk/overlay_xr.py` (a slim runner reusing overlay.py's render
      brain) rather than a --backend flag; frames upload only on seq change, never per-frame]
- P3  Layer injects the action set + publishes poses; overlay reads them -> forearm anchor,
      facing gate, hide distance restored.
      [DONE, with a design change: the LAYER owns anchor + facing gate directly (not the Python
      overlay via a poses shm). Placement/gate params live in /tmp/katwalk/hud.conf, live-reloaded
      ~1x/s so they are tunable from the host while the game runs. The quad is submitted in LOCAL
      (world) space - VIEW-space quads get warped away by reprojection during gameplay.]
- P4  Layer ray-cast -> cursor dot + `katwalk/laser`; overlay interaction (tabs, steppers, drag).
      [DONE. Pointing model: the layer ray-casts the pointing hand's aim pose against the panel
      and publishes the hit point; overlay_xr draws it as a CURSOR DOT on the panel surface (like
      a mouse pointer). There is intentionally NO rendered laser beam in space - Pouya confirmed
      the dot alone is the desired UX. Clicks = the trigger. Drag: hold GRIP on the panel to move
      it; release re-anchors to the wrist and persists offset/rot into hud.conf. The SETUP lock
      button persists `locked` to the same conf; the layer refuses drags while locked. The layer's
      own action set (grip+aim poses, trigger/squeeze booleans) does not consume game input.
      Validated in-headset: cursor dot, click, drag, lock, facing gate, distance hide.]
- P5  Head-yaw + recenter via shm; daemon reads yaw from shm; delete `openvr_pose.py`.
      [DONE - layer publishes HMD pose/yaw (OpenVR yaw convention) + recenter seq to
      katwalk/poses; daemon reads it via katwalk/io/xr_pose.py; openvr_pose.py deleted.]
- P6  Teardown: OpenVR driver, deps, docs.
      [OVERLAY SIDE DONE: overlay.py stripped to the pure renderer (no OpenVR/GL/main),
      overlay_xr is the single runner; openvr/glfw/PyOpenGL/numpy dropped from requirements;
      run script + docs updated; dead tests removed. Settings moved to
      ~/.config/katwalk/hud.conf (persistent) - /tmp/katwalk holds only per-session shm pipes.
      openvr-driver/ (the treadmill-role driver) deleted too - one maintained path; OpenVR
      games reach the layer via xrizer/OpenComposite.]

Locomotion never breaks across any phase; the HUD only ever degrades gracefully.

## Behavioral changes (explicit, per repo policy)

1. The in-VR HUD only appears while an OpenXR game is running (was: could float on bare
   SteamVR). `--demo` / `--render-test` unaffected.
2. HUD requires a Vulkan session initially. GL/D3D sessions get locomotion but no HUD (logged).
3. Native-OpenVR-on-SteamVR loses both HUD and the locomotion driver (dropped by design; does
   not exist on the WiVRn+XRizer stack).
4. `--output vr` now feeds the OpenXR layer, not a SteamVR driver. Same shm, same effect.
5. Head-yaw source moves from an OpenVR background app to the in-game layer. Head-relative
   steering now needs a game running rather than SteamVR running; same body-heading fallback.
6. Panel interaction is reimplemented with our own action set + ray-cast (instead of SteamVR's
   overlay laser). The user sees a cursor DOT on the panel surface, not a laser beam; clicks come
   from the trigger. Same image coords and same click handlers as before.

## Risks / open questions

- Vulkan swapchain upload + `xrEndFrame` layer injection is the main technical risk; P1 exists
  to retire it first. N=1 hardware (Quest over WiVRn); findings will be caveated as such.
- Co-existing implicit layers (OpenXR-Toolkit, MotionCompensation, OpenKneeboard): our hooks
  must pass through defensively and not assume layer order.
- Real-world game coverage is bounded by XRizer's own OpenVR compatibility, not by this layer.
- Interaction-profile bindings: start with simple_controller + oculus/touch; add others if a
  controller reports no pose.
