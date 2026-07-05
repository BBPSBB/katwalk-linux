# katwalk-linux - plan and roadmap

Native Linux locomotion for the KAT Walk C2+ ("plusE") VR treadmill: read the sensors directly,
turn walking into a thumbstick, and drive SteamVR and OpenXR on Linux. No Wine, no Windows, no
KAT Gateway.

This started as a phased plan (recon, decode the protocol, fusion, output, native driver). Those
phases are done. What follows is where things actually stand and what is left.

## Where it is now

The whole pipeline works end to end. The feel is still rough, see the README status.

- **Sensor decode.** The USB HID protocol was worked out by watching the traffic on one unit. The
  receiver device (`3f12`) is the aggregator: it carries the body heading quaternion and both
  feet. `katwalk/core/parser.py` decodes the 32-byte frames (body quaternion, per-foot optical
  slip, battery, firmware). Connect, keepalive, and clean sleep-on-exit are all worked out
  (`docs/PLUSE-PROTOCOL.md`).
- **Locomotion model** (`katwalk/core/locomotion.py`). Speed is the grounded foot's optical slip
  velocity, low-pass smoothed. It is not step-cadence based; cadence is tracked separately for run
  detection. Direction is body relative: you move where your body faces and can look around while
  walking. Also has per-direction sensitivity, a linear-vs-constant speed mode, and an optional
  cruise (auto-walk).
- **Head decoupling** (`katwalk/core/fusion.py`). HMD yaw is read from OpenVR and subtracted, so
  movement follows your body instead of your gaze. Validated in a real game: recenter facing
  forward, then look anywhere and walking still goes where your body points.
- **Outputs.** A native OpenVR driver (`openvr-driver/`) that registers a treadmill-role device exposing
  a joystick, fed by the daemon over shared memory. Also an OpenXR API-layer driver (`openxr-driver/`) and
  a uinput virtual gamepad as alternative paths.
- **Daemon** (`katwalk/daemon.py`). Reads the device over hidraw, runs the model, feeds the
  outputs, and serves the web tuner. Tuned params persist to a config file with named profiles.
- **In-VR overlay** (`katwalk/overlay.py`). A wrist HUD with the foot-sensor pads, body heading,
  the final stick output, live tuning sliders, and recenter.

The code is organized as: `katwalk/core/` (the logic: parsing, the model, fusion), `katwalk/io/`
(device and output adapters), `katwalk/daemon.py` and `katwalk/overlay.py` (the two apps), `openvr-driver/`
and `openxr-driver/` (the C++ pieces), and `dev/` (capture and debug scripts). `./run` starts the
daemon and overlay together.

## What is left

Near term, the things that make it actually good:
- Speed and feel calibration that is not per-user guesswork.
- Tidy up the crude web tuner UI (it is debugging scaffolding right now).
- Wider hardware testing. Everything so far is N=1 (one unit, one machine). Other units, firmware
  versions, and distros need checking.
- Confirm the body-relative direction holds up across more games (only tested in one so far).

Later, the nice-to-haves:
- Haptics: the vibration command (`0xA1`) is known and could be wired up for feedback.
- LED brightness control: the command to drive it has not been found yet.
- Armband input.
- Per-user gait calibration: watch a few seconds of walking a straight line and learn each
  foot's slip-angle bias (people do not slide their feet perfectly parallel, some swipe at an
  angle), then correct it so walking straight moves you forward instead of drifting off at an
  angle. This is the real "calibration" - it replaces manual tuning and the current capture
  tool (`/capture`), which only records labeled movements for offline analysis. Bigger change,
  planned for later.
- Package as a systemd user service with proper install and uninstall.

## Known open problems

- Body-relative direction is validated in one game (head-relative locomotion). Games that handle
  locomotion differently (controller-relative) may need a different mapping; not tested yet.
- SteamVR can crash during wireless headset streaming on the test rig. That is a streaming and
  Vulkan issue on the host, not this driver, but it makes testing annoying.
- The waist sensor sits on a loose belt and under-rotates on small turns, so the body heading is
  not a perfect measure of actual body yaw.

## How to help

If you have a C2+ and a Linux VR setup, the most useful thing is testing on your hardware and
reporting what matches and what does not (include `lsusb` and your distro). Direction and speed
tuning across games is the other big one. See the README for how to run it.
