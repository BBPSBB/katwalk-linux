// katwalk-linux shared-memory contract.
//
// One header, four regions, shared between three processes:
//   - katwalkd            (Python daemon)  writes katwalk/input, reads katwalk/poses
//   - the OpenXR layer     (this .so)       reads  katwalk/input + katwalk/hud,
//                                           writes  katwalk/poses + katwalk/laser
//   - the overlay          (Python)         writes katwalk/hud, reads katwalk/poses + katwalk/laser
//
// Layout rules (so the Python `struct` mirror in katwalk/io/shm.py stays in sync):
//   - every field is 4 bytes (uint32_t or float); no padding, no 64-bit fields.
//   - append-only: never reorder or resize an existing field. Add new fields at the end
//     and bump the region's own seq semantics if a consumer must detect the change.
//
// All regions live under one of these dirs (first that works, matching the layer's existing
// shm path search): $XDG_RUNTIME_DIR/katwalk/, /run/user/<uid>/katwalk/, /tmp/katwalk/.

#ifndef KATWALK_SHM_H
#define KATWALK_SHM_H

#include <stdint.h>

// HUD framebuffer geometry. Matches W, H in katwalk/overlay.py. If those change, change here
// and rebuild the layer; the overlay validates w/h against the header at attach time.
#define KATWALK_HUD_W       512
#define KATWALK_HUD_H       304
#define KATWALK_HUD_BYTES   (KATWALK_HUD_W * KATWALK_HUD_H * 4)  // RGBA8

// HUD anchor space (which tracked thing the quad rides).
#define KATWALK_ANCHOR_OFF   0u
#define KATWALK_ANCHOR_LEFT  1u  // left controller (grip pose)
#define KATWALK_ANCHOR_RIGHT 2u  // right controller (grip pose)
#define KATWALK_ANCHOR_VIEW  3u  // head-locked (VIEW space) - used before poses land (P1/P2)

// katwalk/laser event kinds.
#define KATWALK_LASER_MOVE   0u
#define KATWALK_LASER_DOWN   1u
#define KATWALK_LASER_UP     2u
// katwalk/laser buttons.
#define KATWALK_BTN_TRIGGER  0u  // taps tabs / steppers (was SteamVR left mouse)
#define KATWALK_BTN_GRIP     1u  // drag-reanchor (was SteamVR the other mouse button)

// katwalk/input : daemon -> layer. EXISTING region; layout frozen (retired consumers
// read the same struct). x,y in [-1,1] stick space; seq bumps every write.
struct KatInput {
    float    x;
    float    y;
    uint32_t buttons;
    uint32_t seq;
};

// katwalk/hud : overlay -> layer. This header is immediately followed by KATWALK_HUD_BYTES of
// RGBA8 pixels in the same mapping. Torn-read guard: the overlay writes seq_start, then the
// pixels + fields, then sets seq_end == seq_start; the layer copies pixels only when it sees a
// new seq with seq_start == seq_end.
struct KatHud {
    uint32_t seq_start;
    uint32_t w;            // == KATWALK_HUD_W
    uint32_t h;            // == KATWALK_HUD_H
    uint32_t visible;      // 0/1: append the quad this frame
    uint32_t anchor;       // KATWALK_ANCHOR_*
    float    width_m;      // quad width in meters (height derived from w/h aspect)
    float    transform[12];// 3x4 [R|t] quad pose relative to the anchor space, row-major
    uint32_t seq_end;
    // uint8_t rgba[KATWALK_HUD_BYTES] follows here in the mapping.
};

#define KATWALK_HUD_MAP_BYTES ((uint32_t)sizeof(struct KatHud) + KATWALK_HUD_BYTES)

// katwalk/poses : layer -> overlay + daemon. All poses in one stable reference space (STAGE if
// available, else LOCAL), 3x4 [R|t] row-major. *_valid is 0 when that device has no valid pose
// this frame. hmd_yaw_deg is the convenience the daemon reads (replaces the old OpenVR HmdYaw).
struct KatPoses {
    uint32_t seq;
    uint32_t hmd_valid;
    float    hmd[12];
    uint32_t lctrl_valid;
    float    lctrl[12];
    uint32_t rctrl_valid;
    float    rctrl[12];
    float    hmd_yaw_deg;
    uint32_t recenter_seq;  // bumps on an OpenXR reference-space change (recenter)
};

// katwalk/laser : layer -> overlay. One event per seq bump; x,y are image coords in [0,w]x[0,h]
// from the ray/quad intersection, matching what SteamVR's overlay mouse events delivered.
struct KatLaser {
    uint32_t seq;
    uint32_t event;    // KATWALK_LASER_*
    uint32_t button;   // KATWALK_BTN_*
    float    x;
    float    y;
    uint32_t on_panel; // 1 while the ray currently intersects the quad
};

#endif // KATWALK_SHM_H
