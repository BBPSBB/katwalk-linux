# katwalk-linux OpenVR driver (`openvr-driver/`)

One of the two interchangeable game injectors (the other is `../openxr-driver/`). A small OpenVR
driver that registers a virtual device with role **Treadmill**
(`/user/treadmill`), exposing a joystick + buttons via `IVRDriverInput`. SteamVR
combines it with the real controllers by "greatest absolute value", so it drives
locomotion when you walk and your real thumbsticks still work when you don't.

Live input comes from `katwalkd` over a shared-memory file
(`$XDG_RUNTIME_DIR/katwalk/input`, see `../katwalk/io/driverlink.py`).

## Build
Needs a C++ toolchain (g++, make) and the OpenVR driver headers. Run:
```sh
make
```
Output: `katwalk/bin/linux64/driver_katwalk.so` (statically links libstdc++/libgcc so it
loads cleanly into SteamVR's `vrserver` regardless of host toolchain). The one OpenVR header
it needs is vendored in `openvr/headers/openvr_driver.h` (see ../THIRD_PARTY_LICENSES.md).

## Install / uninstall
```sh
./install.sh      # vrpathreg adddriver  (restart SteamVR after)
./uninstall.sh    # vrpathreg removedriver
```

## Run
```sh
python3 -m katwalk.daemon   # from the repo root; default output is the VR driver + head-relative
```
Then in SteamVR bind **katwalk-linux** (the treadmill source) to your game's Walk action if it
isn't picked up automatically.

## Status
Experimental. Compiles to a valid, self-contained `.so` exporting `HmdDriverFactory` and registers
with `vrpathreg`, but **its behaviour in a live SteamVR session is unverified** - and SteamVR has
been observed safe-mode blocking it (`Not loading driver katwalk because it was blocked by a
previous safe mode event`) after a crash. The **OpenXR driver** in `../openxr-driver/` is the
injector that has actually been used to drive games; prefer it. Keep this one only for the case
of older titles that use OpenVR input directly and never OpenXR.

## Layout
```
src/driver_katwalk.cpp     driver source (provider + treadmill device)
Makefile                   g++ build (static libstdc++/libgcc)
katwalk/
  driver.vrdrivermanifest
  resources/input/katwalk_treadmill_profile.json
  bin/linux64/driver_katwalk.so   (build output, git-ignored)
openvr/headers/             vendored OpenVR header (openvr_driver.h)
```
