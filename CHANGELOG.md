# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/). Versions are pre-1.0 - the whole project is
still pre-alpha; these are internal milestones, not stable releases.

## [0.2.0] - 2026-07-12

Dropped SteamVR entirely: the in-VR overlay is now drawn by the OpenXR API layer itself, so the
whole stack runs on any OpenXR runtime (WiVRn, Monado, SteamVR's OpenXR) with no SteamVR/OpenVR
background app. One VR-facing native component, one path to maintain.

### Added
- In-VR wrist HUD interaction, all through the OpenXR layer: point the off-hand at the panel to
  get a cursor dot on its surface, pull the trigger to click the tabs and steppers; drag-to-place
  (hold grip while pointing, release to re-anchor to the wrist); a SETUP lock button; and a
  look-at-wrist show/hide gate with a configurable visibility distance.
- Persistent HUD settings in `~/.config/katwalk/hud.conf` (placement, gate, lock), live-reloaded
  by the layer while a game runs - editable by hand or by dragging in VR.
- `katwalk/overlay_xr.py`: the overlay runner. Renders HUD frames and ships them to the layer over
  shared memory, and feeds the layer's cursor/click events back into the tab and stepper handlers.
- Head-relative yaw is now published by the OpenXR layer to shared memory (`katwalk/poses`) and
  read by the daemon (`katwalk/io/xr_pose.py`); the recenter now zeroes the overlay's body-heading
  readout so drift and failed recalibration are visible at a glance.

### Changed
- The in-VR HUD is an OpenXR composition-layer quad drawn by the API layer, instead of a SteamVR
  `IVROverlay`. OpenVR games reach it through an OpenVR-to-OpenXR shim (xrizer / OpenComposite).
- `katwalk/overlay.py` is reduced to the pure Pillow HUD renderer (no VR APIs), shared by the
  runner and the tests.
- The daemon reads HMD yaw from the OpenXR layer over shared memory instead of from OpenVR.
- Recenter confirmation buzz reduced from vibration level 5 to level 1 (it was shaking the floor).
- Python dependencies trimmed to `pyusb` + `Pillow`.

### Removed
- The experimental OpenVR/SteamVR treadmill-role driver (`openvr-driver/`) - one maintained VR path.
- `katwalk/io/openvr_pose.py` (the OpenVR HMD-yaw reader), replaced by the shared-memory reader.
- The SteamVR/OpenVR runtime dependency, and the `openvr` / `glfw` / `PyOpenGL` / `numpy` Python
  packages and the vendored OpenVR header that went with it.

## [0.1.1] - 2026-07-06

### Added
- Variant-agnostic device detection: finds the treadmill by role name (`receiver` / `position` /
  `armband` in the HID name) rather than a fixed USB product id, so it works across C2 variants
  (C2 core `3f37`, C2+ `2f37`, etc.). A `python -m katwalk.configure` wizard lists what it sees and
  pins a specific unit (by serial) when detection needs help.

## [0.1.0] - 2026-07-05

First public pre-alpha: a from-scratch native-Linux driver and stack for the KAT Walk C2+
"plusE" VR treadmill. No Wine, no KAT Gateway. Tested on a single unit.

### Added
- USB sensor decoding for the receiver: body-orientation quaternion, per-foot optical slip
  position, per-sensor battery and firmware, and armband heart rate.
- Locomotion model: speed from the grounded foot's optical slip velocity, body-relative
  direction (look around freely while walking), per-direction sensitivity, linear and constant
  speed modes, and an optional cruise auto-walk.
- Daemon: reads the treadmill, serves a live web tuner, records motion captures, stores named
  tuning profiles, and reads HMD yaw from SteamVR for head-relative walking.
- Self-healing init: detects a sensor that woke in the wrong mode (streaming orientation
  instead of optical position) and recovers it with a sleep + re-wake, plus a clean device
  sleep handshake on exit.
- Reconnection: the HMD-yaw reader and the in-VR overlay reconnect on their own if SteamVR is
  restarted, and wait patiently if started before SteamVR is up.
- Two interchangeable game injectors: an OpenXR API-layer driver (the tested path, Proton
  included) and an experimental OpenVR/SteamVR driver. A virtual Xbox gamepad output is also
  available.
- In-VR forearm HUD overlay with live tuning, recenter, and sensor debug views.
- udev rules for HID and uinput access, and vendored OpenVR/OpenXR headers for self-contained
  driver builds.
