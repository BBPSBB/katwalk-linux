# katwalk-linux

Native Linux locomotion for the KAT Walk C2+ ("plus Enhanced" / "plusE") VR treadmill. It reads
the shoe and body sensors over USB, turns your walking into a thumbstick, and feeds that into
your OpenXR games. No Wine, no Windows, no KAT Gateway.

> **Status: pre-alpha, and genuinely rough.** The core works: it connects to the treadmill,
> decodes the sensors, and drives locomotion in real games. But this is a work in progress held
> together with developer scaffolding. The web tuner UI is crude and mostly for my own debugging,
> half of `tools/` is dev and capture leftovers, and direction and speed feel still need dialing
> in. It has only been built and tested against a single unit on one machine. If you have a C2+
> and a Linux VR setup, testing and issues are very welcome.

## Heads up: tested on one unit (N=1)

Everything here about the hardware (the USB vendor and product ids, the frame format, the connect
and keepalive sequences, the sleep behavior) was observed on one KAT Walk C2+ plusE, on one
machine (Bazzite / Fedora), worked out by watching the USB traffic. It is not an official spec.
Your unit, firmware, or distro may differ. If something does not match, please open an issue with
your `lsusb` output and what you see. That is how this gets more general.

## What you need

- A KAT Walk C2+ (the "plusE" variant) plugged in over USB.
- Linux with any OpenXR runtime driving your headset (WiVRn, Monado, or SteamVR's OpenXR).
  This was built on Bazzite/Fedora with a Quest over WiVRn. Other setups should work but are
  untested.
- Python 3.
- A C++ toolchain (`g++`, `make`) to build the OpenXR layer (the game injector). On an
  immutable distro like Bazzite, do the builds inside a `distrobox`/`toolbox` that has them,
  not by layering packages onto the host.

## Setup (one time)

Do these in order. Steps 1 to 3 are always needed. Step 4 is how movement actually reaches your
games, so pick the path(s) that match them.

**1. Get the code**
```sh
git clone https://github.com/BBPSBB/katwalk-linux
cd katwalk-linux
```

**2. Python environment.** The daemon core is stdlib only; the venv adds Pillow (HUD rendering)
and pyusb (device wake). The overlay is pure Python, nothing to compile.
```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

**3. udev rules** so the tools reach the device without sudo (HID + uinput). Read the files first:
the device id in `70-katvr.rules` is from my one unit and may not match yours.
```sh
sudo install -m644 udev/70-katvr.rules udev/71-katvr-uinput.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

**4. Get walking into your games.** The daemon computes a thumbstick vector and publishes it over
shared memory; the **OpenXR layer** (an implicit API layer that loads into every OpenXR game,
Proton included) reads it and injects it into the left thumbstick - no in-game binding needed. The
same layer draws the in-VR HUD. It compiles with the C++ toolchain from above (plus the
`vulkan-headers` and `openxr` dev headers; see the toolchain note about building inside a
distrobox/toolbox on immutable distros) and statically links libstdc++/libgcc:
```sh
cd openxr-driver && make   # builds bin/libkatwalk_xr_layer.so
./install.sh               # installs it as a per-user implicit layer
cd ..
```
`make` refuses to build without those headers and tells you what to install. It attaches when the
game launches. Turn it off without uninstalling with `export DISABLE_KATWALK_XR_LAYER=1`.

(An experimental OpenVR treadmill-role driver used to live in this repo as an alternative path; it
was removed to keep one maintained path. OpenVR games work through the same OpenXR layer when run
via an OpenVR-to-OpenXR shim such as xrizer or OpenComposite.)

## Running it

With the base on, and the shoes and rotating base powered so they stream:
```sh
./run
```
That starts both pieces: the daemon (treadmill to the OpenXR layer over shared memory) and the
overlay runner (the wrist HUD where you recenter and tune - it appears inside whatever OpenXR
game you launch). Stand facing forward, recenter, and walk. Ctrl-C stops both and sleeps the
device cleanly. HUD placement/gating lives in `~/.config/katwalk/hud.conf`: drag the panel
in-VR (point the other hand, hold grip) or edit the file - it reloads live.

To run them separately in two terminals instead:
```sh
.venv/bin/python -m katwalk.daemon     # reads the treadmill, feeds the OpenXR layer
.venv/bin/python -m katwalk.overlay_xr # the in-VR overlay (frames + clicks via the layer)
```
Publishing the stick to the OpenXR layer over shared memory (and reading your head yaw so movement follows your
body, not your gaze) is the default, so the daemon needs no flags for normal use. The only knobs:
`--output {vr,gamepad,none}` (default `vr`; `gamepad` drives a virtual Xbox pad, `none` disables
output) and `--port`.

### Getting it into your game (native vs. Proton)

`./run` starts the daemon and the overlay, but how they reach a *game* depends on how that game is
sandboxed:

- **Native Linux OpenXR games**: nothing extra. The implicit layer loads automatically and reads
  katwalk's shared memory directly. Launch the game and the wrist overlay appears; walking drives
  the thumbstick.
- **Proton games (Steam, Heroic/umu, ...)**: these run inside the Steam Linux Runtime container
  (pressure-vessel), which by default hides two things the game needs from the host:
  1. **your headset's OpenXR runtime** (WiVRn, Monado, ...) - without it the game can't enter VR
     at all, and usually opens flatscreen on the desktop;
  2. **katwalk's runtime dir `/tmp/katwalk`** - the channel the overlay frames, the pointer
     clicks, and the locomotion stick travel through into the sandbox.

  You expose both in the game's launch options. With Steam's `%command%` placeholder:
  ```
  PRESSURE_VESSEL_IMPORT_OPENXR_1_RUNTIMES=1 PRESSURE_VESSEL_FILESYSTEMS_RW=/tmp/katwalk %command%
  ```
  `IMPORT_OPENXR_1_RUNTIMES` pulls your active OpenXR runtime into the container;
  `FILESYSTEMS_RW` bind-mounts paths in (colon-separated). If your runtime is itself a flatpak
  (WiVRn is), its socket lives in the flatpak's data dir, so add that dir to the list too:
  ```
  PRESSURE_VESSEL_FILESYSTEMS_RW=/path/to/your/runtime/dir:/tmp/katwalk
  ```
  Your runtime's own setup guide usually gives you the exact import string to paste; treat the
  above as the shape of it. Heroic/umu use the same pressure-vessel variables; set them as
  environment variables on the game rather than via `%command%`.

If a Proton game launches flatscreen, or into VR but with no overlay and no walking, it is almost
always one of these two exports missing (the container hiding the runtime or `/tmp/katwalk`), not a
katwalk bug.

### Device detection (any C2 variant)

The daemon finds the treadmill by role name, not a fixed USB id, so it works across C2 variants
whose receiver has a different product id (C2 core `3f37`, C2+ `2f37`, this author's unit `3f12`,
and so on). If it can't find your device, or you have several units attached and want to pin a
specific one, run the setup wizard, which lists what it sees and lets you pick:
```sh
.venv/bin/python -m katwalk.configure       # interactive; or --auto to just accept detection
```
It saves your choice (pinned by serial) to `~/.config/katwalk/device.json`. If your unit doesn't
show up at all, check `lsusb | grep c4f4` and open an issue with the output.

### Just poking at the sensors (no VR)

Run with `--output none` and open the web page in a browser. Heads up, this web UI is crude
developer scaffolding, not a real interface:
```sh
.venv/bin/python -m katwalk.daemon --output none    # then open http://localhost:8770
```

## How it works

```
katwalk/daemon.py (the daemon)
  hidraw reader -> frame parser -> locomotion model -> stick vector (+ HMD yaw)
        |
        +-- web tuner + live sliders (katwalk/web/tuner.html)
        +-- output (--output, default vr):
              vr       -> shared-memory stick file, read by the OpenXR layer (openxr-driver/)
              gamepad  -> a virtual Xbox pad (uinput)
              none     -> nothing; just the web tuner

  katwalk/overlay_xr.py : the in-VR overlay runner. Polls /state, ships rendered frames to the
                        OpenXR layer over shared memory, and feeds laser clicks back into the
                        tabs/steppers. katwalk/overlay.py is the pure-Pillow renderer it uses.
```

- Speed comes from how fast the grounded foot slides on the deck (optical slip velocity), low-pass
  smoothed. It is not step-cadence based. Sprint is decided by speed crossing a threshold, not by
  cadence; cadence is measured only as a readout on the HUD for now - probably buggy
- Direction is body relative: you move where your body faces (waist sensor heading plus the foot
  slip angle), so you can look around freely while walking. Recenter from the overlay to set your
  forward.

For the driver internals see [openxr-driver/README.md](openxr-driver/README.md) and

## What works, what is rough

- Works: connect and sensor decode, speed, body-relative direction (look around freely), the OpenXR
  layer injecting locomotion into real games, and the in-VR wrist overlay - live tuning, recenter,
  drag-to-place, a cursor-dot pointer for clicking, and a look-at-wrist show/hide gesture. Cruise
  mode and per-direction sensitivity too.
- Rough: the web tuner UI is crude debugging scaffolding, and `dev/` is a pile of capture and
  one-off debug scripts. Speed feel needs per-user tuning, and it has only been exercised on one
  unit.

## Layout

```
katwalk/          the product code
  core/           the logic: parser (sensor decode), locomotion (speed + direction), fusion (heading + stick)
  io/             adapters: gamepad, xr_pose (HMD yaw from the layer), driverlink (shared-mem), sleeper
  daemon.py       the runtime: reads the treadmill, runs the model, feeds the outputs + web tuner
  overlay.py      the HUD renderer (pure Pillow)
  overlay_xr.py   the in-VR HUD runner (shared memory to/from the OpenXR layer)
  config.py       tuning params + named profiles
  web/            web tuner UI
openxr-driver/    game injector: OpenXR API-layer (C++) - the one used in practice
dev/              capture + debug scripts (not needed to run it)
udev/             HID + uinput access rules
docs/             hardware, protocol, USB-noise notes (all N=1)
run               start the daemon + overlay together
```

## Contributing

Issues and pull requests are welcome, especially:
- Testing on other C2+ units, firmware versions, and distros, and reporting what matches or does not.
- Direction and speed tuning across different games.
- Smoothing off the rough edges.

For a hardware issue, include `lsusb` and your distro. For in-game behavior, say which game and its
locomotion setting (smooth head-relative vs controller-relative).

## License

GPLv3, see [LICENSE](LICENSE). A few permissively-licensed third-party headers (OpenXR)
are bundled for a self-contained build, see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
