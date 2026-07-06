# Changelog

All notable changes to this project are documented here, following
[Keep a Changelog](https://keepachangelog.com/). There are no tagged releases yet - changes
accumulate under **Unreleased** until the first version.

## [Unreleased]

First public pre-alpha: a from-scratch native-Linux driver and stack for the KAT Walk C2+
"plusE" VR treadmill. No Wine, no KAT Gateway. Tested on a single unit.

### Added
- Variant-agnostic device detection: finds the treadmill by role name (`receiver` / `position` /
  `armband` in the HID name) rather than a fixed USB product id, so it works across C2 variants
  (C2 core `3f37`, C2+ `2f37`, etc.). A `python -m katwalk.configure` wizard lists what it sees and
  pins a specific unit (by serial) when detection needs help.
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
