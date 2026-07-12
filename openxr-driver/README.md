# katwalk-linux OpenXR driver (`openxr-driver/`)

The game injector: the single VR-facing native component of katwalk-linux. It injects locomotion
into games AND displays the in-VR wrist HUD (rendered by `katwalk/overlay_xr.py`, shipped over
shared memory). Works on any OpenXR runtime - WiVRn, Monado, or SteamVR's OpenXR; OpenVR games
reach it through an OpenVR-to-OpenXR shim (xrizer / OpenComposite).

Technically this is an OpenXR **implicit API layer** ("driver" here just means the thing that feeds
locomotion into a game). It loads into every OpenXR app, native and Proton, and injects the walk
vector into the left thumbstick the game already reads, so there is no in-game binding to set up.

Live input comes from `katwalkd` over a shared-memory file (`/tmp/katwalk/input`, with
`$XDG_RUNTIME_DIR/katwalk/input` as the non-sandbox path; see `../katwalk/io/driverlink.py`).

## Build
Needs a C++ toolchain (g++, make) and the OpenXR headers (vendored in `openxr/headers/`, see
`../THIRD_PARTY_LICENSES.md`). Run:
```sh
make
```
Output: `bin/libkatwalk_xr_layer.so` (statically links libstdc++/libgcc).

## Install / uninstall
```sh
./install.sh      # installs it as a per-user implicit layer
./uninstall.sh    # removes it
```
It attaches when the game launches. Turn it off without uninstalling with
`export DISABLE_KATWALK_XR_LAYER=1`. Verify it loaded with
`grep -l libkatwalk_xr_layer /proc/*/maps`.

## Run
```sh
python3 -m katwalk.daemon   # from the repo root; default output feeds this driver + head-relative
```

## Layout
```
src/layer.cpp          layer source (injects the stick into the left thumbstick action)
Makefile               g++ build (static libstdc++/libgcc)
bin/libkatwalk_xr_layer.so   (build output, git-ignored)
openxr/headers/        vendored OpenXR headers
install.sh / uninstall.sh
```
