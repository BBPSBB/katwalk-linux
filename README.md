# katwalk-linux

Native Linux locomotion for the KAT Walk C2+ ("plus Enhanced" / "plusE") VR treadmill. It reads
the shoe and body sensors over USB, turns your walking into a thumbstick, and feeds that into
SteamVR and OpenXR. No Wine, no Windows, no KAT Gateway.

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
- Linux with SteamVR working in your headset. This was built on Bazzite/Fedora with a Quest over
  wireless streaming. Other setups should work but are untested.
- Python 3.
- A C++ toolchain (`g++`, `make`) to build a driver (the OpenXR or OpenVR game injector). On an
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

**2. Python environment.** The daemon core is stdlib only; this venv covers the VR-facing pieces
(the in-VR overlay, HMD pose, the driver feed). The overlay is pure Python, nothing to compile.
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
shared memory; a *driver* (a game injector) reads it and feeds the game. There are two
interchangeable drivers doing the same job. The **OpenXR driver** is the one actually developed and
tested against, so use it; the **OpenVR driver** is an experimental alternative. Build only the one
you need (each compiles with the C++ toolchain from above and statically links libstdc++/libgcc so
the `.so` loads cleanly).

*The OpenXR driver* (an implicit OpenXR API layer that injects the left thumbstick into every
OpenXR game, Proton included, with no in-game binding):
```sh
cd openxr-driver && make   # builds bin/libkatwalk_xr_layer.so
./install.sh               # installs it as a per-user implicit layer
cd ..
```
It attaches when the game launches. Turn it off without uninstalling with
`export DISABLE_KATWALK_XR_LAYER=1`.

*The OpenVR driver (experimental, optional)* registers a treadmill-role device inside SteamVR. It
builds and registers, but it has not been verified driving a live session and SteamVR can safe-mode
block it after any crash. Only bother with it for older games that use OpenVR input directly and
never OpenXR:
```sh
cd openvr-driver && make   # builds katwalk/bin/linux64/driver_katwalk.so
./install.sh               # registers it with SteamVR via vrpathreg
cd ..
```
Restart SteamVR afterwards, then bind **katwalk-linux** (the treadmill source) to your game's walk
action in the SteamVR controller bindings. You do not need this if the OpenXR driver covers your
games.

## Running it

With SteamVR running, the base on, and the shoes and rotating base powered so they stream:
```sh
./run
```
That starts both pieces: the daemon (treadmill to the injector over shared memory) and the in-VR
overlay (the wrist HUD where you recenter and tune). Launch your game, stand facing forward,
recenter from the overlay, and walk. Ctrl-C stops both and sleeps the device cleanly.

To run them separately in two terminals instead:
```sh
.venv/bin/python -m katwalk.daemon     # reads the treadmill, feeds the installed driver
.venv/bin/python -m katwalk.overlay    # the in-VR overlay
```
Publishing to the driver over shared memory (and reading your head yaw so movement follows your
body, not your gaze) is the default, so the daemon needs no flags for normal use. The only knobs:
`--output {vr,gamepad,none}` (default `vr`; `gamepad` drives a virtual Xbox pad, `none` disables
output) and `--port`.

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
              vr       -> shared-memory stick file, read by whichever driver you installed:
                            openxr-driver/  an OpenXR implicit layer (the tested path; Proton too)
                            openvr-driver/  an OpenVR treadmill-role device (experimental)
              gamepad  -> a virtual Xbox pad (uinput)
              none     -> nothing; just the web tuner

  katwalk/overlay.py : separate in-VR overlay. Polls /state, draws the two foot pads, the body
                        heading, and the final stick output, plus recenter and live tuning.
```

- Speed comes from how fast the grounded foot slides on the deck (optical slip velocity), low-pass
  smoothed. It is not step-cadence based. Sprint is decided by speed crossing a threshold, not by
  cadence; cadence is measured only as a readout on the HUD for now - probably buggy
- Direction is body relative: you move where your body faces (waist sensor heading plus the foot
  slip angle), so you can look around freely while walking. Recenter from the overlay to set your
  forward.

For the driver internals see [openxr-driver/README.md](openxr-driver/README.md) and
[openvr-driver/README.md](openvr-driver/README.md).

## What works, what is rough

- Works: connect and sensor decode, speed, body-relative direction (look around freely), the in-VR
  overlay with live tuning, the OpenXR driver injecting locomotion into real games, cruise mode,
  per-direction sensitivity.
- Experimental: the OpenVR driver builds and registers but is unverified in a live session, and
  SteamVR can safe-mode block it after a crash. The OpenXR driver is the path to use.
- Rough: the web tuner UI is crude debugging scaffolding, and `dev/` is a pile of capture and
  one-off debug scripts. Speed feel needs per-user tuning, and it has only been exercised on one
  unit. If SteamVR crashes during wireless streaming, that is a streaming/Vulkan issue on the
  host, not this project.

## Layout

```
katwalk/          the product code
  core/           the logic: parser (sensor decode), locomotion (speed + direction), fusion (heading + stick)
  io/             adapters: gamepad, openvr_pose, driverlink (shared-mem to the driver), sleeper
  daemon.py       the runtime: reads the treadmill, runs the model, feeds the outputs + web tuner
  overlay.py      the in-VR HUD
  config.py       tuning params + named profiles
  web/            web tuner UI
openxr-driver/    game injector: OpenXR API-layer (C++) - the one used in practice
openvr-driver/    game injector: OpenVR treadmill-role driver (C++) - experimental alternative
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

GPLv3, see [LICENSE](LICENSE). A few permissively-licensed third-party headers (OpenVR, OpenXR)
are bundled for a self-contained build, see [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md).
