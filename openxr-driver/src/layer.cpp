// katwalk-linux OpenXR API layer - injects treadmill locomotion into ANY OpenXR game, and
// (being migrated) draws the katwalk HUD as an OpenXR composition-layer quad so the wrist
// overlay works without SteamVR/OpenVR. See docs/OPENXR-HUD-MIGRATION.md for the full plan;
// the HUD code lives in namespace `hud` below. P1 = a static test quad; live frames + input
// come in later phases.
//
// Why this exists: OpenXR has no treadmill concept. A game reads its movement from the
// hand controller's thumbstick action (e.g. /actions/.../in/thumbstick bound to
// /user/hand/left/input/thumbstick). SteamVR's OpenVR "treadmill role" combine never
// reaches OpenXR apps (OpenVR issue #937, still unsolved). So instead of fighting the
// binding system, we sit between the game and the runtime as an implicit API layer and
// feed our walk vector straight into the thumbstick the game already reads. This is the
// same technique OpenXR-Toolkit / OpenXR-MotionCompensation use. It works for Proton
// games too: wineopenxr.so forwards through the native Linux OpenXR loader, which applies
// implicit layers.
//
// What we do:
//   1. Hook xrSuggestInteractionProfileBindings - note which XrActions the game binds to
//      the LEFT (movement) hand's thumbstick/trackpad. (Right hand = turning, left alone.)
//   2. Hook xrGetActionStateVector2f - when the game reads one of those actions, overlay
//      our walk vector by greatest-absolute-value per axis. When we're not walking we
//      touch nothing, so the real stick keeps working.
//
// Input comes from katwalkd via a shared-memory file:
//   $XDG_RUNTIME_DIR/katwalk/input  ->  struct { float x, y; uint32 buttons, seq; }
//
// Safe by construction: if the treadmill isn't running the shm file is absent, readWalk()
// returns false, and every call is a pure pass-through. Set DISABLE_KATWALK_XR_LAYER=1 to
// turn the layer off entirely (also honored by the loader via the manifest).
//
// Build: make   (needs a C++ toolchain)  ->  bin/libkatwalk_xr_layer.so

#define XR_USE_GRAPHICS_API_VULKAN
#include <vulkan/vulkan.h>

#include <openxr.h>
#include <openxr_platform.h>
#include <openxr_loader_negotiation.h>

#include "katwalk_shm.h"

#ifndef KATWALK_VERSION
#define KATWALK_VERSION "0.0.0-dev"  // normally injected by the Makefile from katwalk/__init__.py
#endif

// Layout guards: the Python side unpacks these with fixed struct strings - any padding or
// field drift must fail the BUILD, not corrupt the bridge at runtime.
static_assert(sizeof(KatInput) == 16, "KatInput layout drifted");
static_assert(sizeof(KatHud)   == 76, "KatHud layout drifted");
static_assert(sizeof(KatLaser) == 24, "KatLaser layout drifted");
static_assert(sizeof(KatPoses) == 168, "KatPoses layout drifted");

#include <atomic>
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <mutex>
#include <set>
#include <string>
#include <vector>

#include <dlfcn.h>
#include <fcntl.h>
#include <pwd.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>

#if defined(__GNUC__)
#define KAT_EXPORT extern "C" __attribute__((visibility("default")))
#else
#define KAT_EXPORT extern "C"
#endif

namespace {

// ---- one-time diagnostic log (so a Proton-sandboxed run can be debugged afterwards) ----
std::mutex g_logmtx;
void layer_log(const std::string &msg) {
    static FILE *f = nullptr;
    static bool  tried = false;
    std::lock_guard<std::mutex> lk(g_logmtx);
    if (!tried) {
        tried = true;
        // /tmp/katwalk FIRST: it is the one dir we bind-mount RW into the game's pressure-vessel
        // sandbox (via PRESSURE_VESSEL_FILESYSTEMS_RW), so the log reaches the HOST. The sandbox's
        // own $XDG_RUNTIME_DIR is private and invisible from outside, so it is only a fallback.
        ::mkdir("/tmp/katwalk", 0777);
        char p_xdg[256] = {0}, p_run[64] = {0};
        const char *xdg = std::getenv("XDG_RUNTIME_DIR");
        if (xdg && *xdg) std::snprintf(p_xdg, sizeof(p_xdg), "%s/katwalk/xr-layer.log", xdg);
        std::snprintf(p_run, sizeof(p_run), "/run/user/%u/katwalk/xr-layer.log", (unsigned)getuid());
        const char *cands[] = { "/tmp/katwalk/xr-layer.log", p_xdg[0] ? p_xdg : nullptr, p_run };
        for (const char *c : cands) { if (c && !f) f = std::fopen(c, "a"); }
    }
    if (f) { std::fprintf(f, "%s\n", msg.c_str()); std::fflush(f); }
}

// ---- shared-memory input (written by katwalkd) ---- struct KatInput lives in katwalk_shm.h
int                g_fd  = -1;
volatile KatInput *g_map = nullptr;
constexpr float    DEADZONE = 0.05f;   // below this magnitude we pass the real stick through

bool openShm() {
    // pressure-vessel may rewrite XDG_RUNTIME_DIR, so also try the canonical
    // /run/user/<uid> (bind-mounted into the sandbox) and the driver's /tmp fallback.
    std::string cands[3]; int n = 0;
    const char *xdg = std::getenv("XDG_RUNTIME_DIR");
    if (xdg && *xdg) cands[n++] = std::string(xdg) + "/katwalk/input";
    { char b[64]; std::snprintf(b, sizeof(b), "/run/user/%u/katwalk/input", (unsigned)getuid()); cands[n++] = b; }
    cands[n++] = "/tmp/katwalk/input";
    for (int i = 0; i < n; ++i) {
        int fd = ::open(cands[i].c_str(), O_RDONLY);
        if (fd < 0) continue;
        void *m = mmap(nullptr, sizeof(KatInput), PROT_READ, MAP_SHARED, fd, 0);
        if (m != MAP_FAILED) { g_fd = fd; g_map = static_cast<volatile KatInput *>(m);
                               layer_log("shm opened: " + cands[i]); return true; }
        ::close(fd);
    }
    return false;
}

// Returns true and fills x,y in [-1,1] when the treadmill says we're moving.
bool readWalk(float &x, float &y) {
    if (!g_map && !openShm()) return false;
    float vx = g_map->x, vy = g_map->y;
    if (vx < -1.f) vx = -1.f; else if (vx > 1.f) vx = 1.f;
    if (vy < -1.f) vy = -1.f; else if (vy > 1.f) vy = 1.f;
    if (vx * vx + vy * vy < DEADZONE * DEADZONE) return false;
    x = vx; y = vy;
    return true;
}

// ---- per-process dispatch (games use exactly one instance; a single table is enough) ----
struct Dispatch {
    PFN_xrGetInstanceProcAddr               nextGIPA            = nullptr;
    PFN_xrGetActionStateVector2f            nextGetV2f          = nullptr;
    PFN_xrSuggestInteractionProfileBindings nextSuggest         = nullptr;
    PFN_xrPathToString                      nextPathToString    = nullptr;
    PFN_xrStringToPath                      nextStringToPath    = nullptr;
    PFN_xrDestroyInstance                   nextDestroyInstance = nullptr;
    PFN_xrPollEvent                         nextPollEvent       = nullptr;
    // HUD (composition-layer quad) path:
    PFN_xrCreateSession                     nextCreateSession   = nullptr;
    PFN_xrDestroySession                    nextDestroySession  = nullptr;
    PFN_xrEndFrame                          nextEndFrame        = nullptr;
    PFN_xrCreateReferenceSpace              nextCreateRefSpace  = nullptr;
    PFN_xrDestroySpace                      nextDestroySpace    = nullptr;
    PFN_xrEnumerateSwapchainFormats         nextEnumFormats     = nullptr;
    PFN_xrCreateSwapchain                   nextCreateSwapchain = nullptr;
    PFN_xrDestroySwapchain                  nextDestroySwapchain= nullptr;
    PFN_xrEnumerateSwapchainImages          nextEnumSwImages    = nullptr;
    PFN_xrAcquireSwapchainImage             nextAcquireImage    = nullptr;
    PFN_xrWaitSwapchainImage                nextWaitImage       = nullptr;
    PFN_xrReleaseSwapchainImage             nextReleaseImage    = nullptr;
    // hand anchor (own action set):
    PFN_xrCreateActionSet                   nextCreateActionSet = nullptr;
    PFN_xrCreateAction                      nextCreateAction    = nullptr;
    PFN_xrAttachSessionActionSets           nextAttachActionSets= nullptr;
    PFN_xrSyncActions                       nextSyncActions     = nullptr;
    PFN_xrCreateActionSpace                 nextCreateActionSpace = nullptr;
    PFN_xrLocateSpace                       nextLocateSpace     = nullptr;
    PFN_xrGetActionStateBoolean             nextGetBool         = nullptr;
    XrInstance instance  = XR_NULL_HANDLE;
    XrPath rightHand = XR_NULL_PATH;
};
Dispatch              g;
std::mutex            g_mtx;
// HUD is ON by default (the wrist overlay is the product); set KATWALK_NO_HUD=1 to make the
// layer pure locomotion injection with ZERO effect on the game's frames (no hooks, no Vulkan,
// no swapchain, no per-frame work). Decided once at instance creation.
bool                  g_hudEnabled = false;
std::set<XrAction>    g_leftThumb;    // actions bound to the LEFT hand thumbstick/trackpad
std::set<XrAction>    g_rightThumb;   // ... and the RIGHT (so we never inject into turning)

// Ask the daemon (native; shares /tmp with the sandbox) to recenter walk-forward. Fired
// when the runtime recenters (e.g. the Quest Meta-button hold emits a reference-space
// change), so locomotion-forward re-aligns with the new view-forward - no extra gesture.
std::atomic<uint32_t> g_recenterSeq{0};   // bumped on runtime recenter; published via katwalk/poses

void requestRecenter() {
    int fd = ::open("/tmp/katwalk/recenter", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) { ssize_t n = ::write(fd, "1", 1); (void)n; ::close(fd); }
    g_recenterSeq.fetch_add(1, std::memory_order_relaxed);
    layer_log("recenter requested (OpenXR reference space changed)");
}

// ===================== HUD: composition-layer quad (P1: static test image) =====================
// We draw a small quad in front of the view by appending an XrCompositionLayerQuad in xrEndFrame.
// P1 uploads a fixed test pattern so we can confirm the quad shows, is the right way up, and
// composites on WiVRn (native OpenXR and OpenVR-via-XRizer) and other runtimes. P2 swaps the
// pattern for the live overlay framebuffer from katwalk/hud.
//
// Vulkan is loaded dynamically from the app's own libvulkan; we only use the VkInstance/
// PhysicalDevice/Device the app handed us in XrGraphicsBindingVulkanKHR. A non-Vulkan session
// (OpenGL/D3D) leaves the HUD disabled and never touches the frame; locomotion is unaffected.

namespace hud {

struct VkFns {
    PFN_vkGetInstanceProcAddr               GetInstanceProcAddr    = nullptr;
    PFN_vkGetDeviceProcAddr                 GetDeviceProcAddr      = nullptr;
    PFN_vkGetPhysicalDeviceMemoryProperties GetPhysMemProps        = nullptr;
    PFN_vkGetDeviceQueue                    GetDeviceQueue         = nullptr;
    PFN_vkCreateCommandPool                 CreateCommandPool      = nullptr;
    PFN_vkAllocateCommandBuffers            AllocateCommandBuffers = nullptr;
    PFN_vkCreateBuffer                      CreateBuffer           = nullptr;
    PFN_vkGetBufferMemoryRequirements       GetBufferMemReq        = nullptr;
    PFN_vkAllocateMemory                    AllocateMemory         = nullptr;
    PFN_vkBindBufferMemory                  BindBufferMemory       = nullptr;
    PFN_vkMapMemory                         MapMemory              = nullptr;
    PFN_vkUnmapMemory                       UnmapMemory            = nullptr;
    PFN_vkFlushMappedMemoryRanges           FlushMappedRanges      = nullptr;
    PFN_vkBeginCommandBuffer                BeginCommandBuffer     = nullptr;
    PFN_vkEndCommandBuffer                  EndCommandBuffer       = nullptr;
    PFN_vkResetCommandBuffer                ResetCommandBuffer     = nullptr;
    PFN_vkCmdPipelineBarrier                CmdPipelineBarrier     = nullptr;
    PFN_vkCmdCopyBufferToImage              CmdCopyBufferToImage   = nullptr;
    PFN_vkCmdCopyImageToBuffer              CmdCopyImageToBuffer   = nullptr;  // debug readback
    PFN_vkQueueSubmit                       QueueSubmit            = nullptr;
    PFN_vkQueueWaitIdle                     QueueWaitIdle          = nullptr;
};

struct State {
    bool             parsed     = false;  // graphics binding inspected (once per session)
    bool             vkOk       = false;  // a usable Vulkan binding was captured
    bool             inited     = false;  // swapchain + Vulkan objects built (lazy, in endFrame)
    bool             failed     = false;  // init failed once; stop retrying every frame
    XrSession        session    = XR_NULL_HANDLE;
    void*            vkLib      = nullptr;
    VkInstance       vkInstance = VK_NULL_HANDLE;
    VkPhysicalDevice vkPhys     = VK_NULL_HANDLE;
    VkDevice         vkDevice   = VK_NULL_HANDLE;
    uint32_t         queueFamily= 0;
    uint32_t         queueIndex = 0;
    VkQueue          queue      = VK_NULL_HANDLE;
    VkCommandPool    cmdPool    = VK_NULL_HANDLE;
    VkCommandBuffer  cmd        = VK_NULL_HANDLE;
    VkBuffer         staging    = VK_NULL_HANDLE;
    VkDeviceMemory   stagingMem = VK_NULL_HANDLE;
    uint8_t         *stagingMap = nullptr;         // persistently mapped for live-frame uploads
    VkBuffer         readback   = VK_NULL_HANDLE;  // debug: GPU->CPU copy of the composited image
    VkDeviceMemory   readbackMem= VK_NULL_HANDLE;
    bool             dumped     = false;           // one-shot readback dump done
    bool             uploaded   = false;           // static pattern written to the swapchain once
    XrSpace          viewSpace  = XR_NULL_HANDLE;  // head space, used for the facing-gate math
    XrSpace          localSpace = XR_NULL_HANDLE;  // world space; the quad is SUBMITTED here -
    // view-space quads get warped away by reprojection during gameplay (they only survived on
    // static loading screens); LOCAL-space quads are the path every real overlay app uses.
    XrSwapchain      swapchain  = XR_NULL_HANDLE;
    bool             bgra       = false;
    std::vector<VkImage>    images;
    XrCompositionLayerQuad  quad{};
    VkFns            vk;
};

State     g_h;
std::mutex g_hmtx;

// ---- hand anchor: our own action set (reading it does NOT consume the game's input) ----
struct HandState {
    XrActionSet actionSet = XR_NULL_HANDLE;
    XrAction    gripPose  = XR_NULL_HANDLE;   // panel anchor
    XrAction    aimPose   = XR_NULL_HANDLE;   // laser ray (pointing hand)
    XrAction    trigBool  = XR_NULL_HANDLE;   // laser click (taps)
    XrAction    sqzBool   = XR_NULL_HANDLE;   // laser secondary (reserved)
    XrPath      hands[2]  = {XR_NULL_PATH, XR_NULL_PATH};  // /user/hand/left, /user/hand/right
    XrSpace     gripSpace[2] = {XR_NULL_HANDLE, XR_NULL_HANDLE};
    XrSpace     aimSpace[2]  = {XR_NULL_HANDLE, XR_NULL_HANDLE};
    bool        created  = false;   // action set + actions exist
    bool        attached = false;   // rode along on the app's xrAttachSessionActionSets
    bool        visible  = false;   // facing-gate state (with hysteresis)
    int         holdCnt  = 0;       // frames the flipped gate decision has persisted
    bool        lastTrig = false;   // previous button states (edge detection)
    bool        lastSqz  = false;
    bool        dragging = false;   // panel is riding the pointing hand (grip held)
    XrPosef     dragOff{};          // panel pose relative to the pointing hand's aim pose
};
HandState g_hand;
std::atomic<uint32_t> g_syncCalls{0};   // xrSyncActions passing through us (staleness diagnosis)

// ---- live-tunable placement: /tmp/katwalk/hud.conf, re-read when its mtime changes, so the
// wrist anchor can be tuned from the host while the game runs (same host-shared dir as the log).
struct HudCfg {
    int   hand      = 0;                    // 0=left 1=right
    float off[3]    = {0.0f, 0.02f, 0.10f}; // panel origin in grip space (m)
    float rotdeg[3] = {-90.f, 0.f, 0.f};    // panel rotation in grip space (deg, XYZ order)
    float width     = 0.13f;                // panel width (m)
    float faceShow  = 0.50f;                // show when normal·toHead exceeds this...
    float faceHide  = 0.30f;                // ...hide when it falls below this (hysteresis)
    float maxDist   = 0.65f;                // hand farther than this from the face -> hidden
    int   hold      = 6;                    // frames a gate flip must persist (kills flicker)
    int   always    = 0;                    // 1 = ignore the gate (placement tuning aid)
    int   locked    = 0;                    // 1 = refuse drag-to-move (SETUP lock button)
};
HudCfg g_cfg;
time_t g_cfgMtime = 0;
uint32_t g_cfgPoll = 0;

// Persistent settings live in ~/.config/katwalk/hud.conf (NOT /tmp - they must survive
// reboots). The game sandbox (pressure-vessel) shares the home directory, so both the layer
// (in-game) and overlay_xr (host) read/write the same file. XDG-aware, passwd fallback.
const char *confPath() {
    static std::string p = [] {
        std::string base;
        if (const char *x = std::getenv("XDG_CONFIG_HOME"); x && *x) base = x;
        else if (const char *h = std::getenv("HOME"); h && *h) base = std::string(h) + "/.config";
        else if (const passwd *pw = getpwuid(getuid())) base = std::string(pw->pw_dir) + "/.config";
        std::string dir = base + "/katwalk";
        ::mkdir(dir.c_str(), 0755);
        std::string full = dir + "/hud.conf";
        layer_log("HUD: conf path = " + full);
        return full;
    }();
    return p.c_str();
}

void loadCfg() {
    struct stat st{};
    if (::stat(confPath(), &st) != 0 || st.st_mtime == g_cfgMtime) return;
    g_cfgMtime = st.st_mtime;
    FILE *f = std::fopen(confPath(), "r");
    if (!f) return;
    char line[128];
    HudCfg c = g_cfg;
    while (std::fgets(line, sizeof line, f)) {
        char key[32]; float a=0, b=0, d=0;
        if (std::sscanf(line, "%31[a-z_] = %f , %f , %f", key, &a, &b, &d) >= 2) {
            if      (!std::strcmp(key, "hand"))      c.hand = (int)a;
            else if (!std::strcmp(key, "offset"))    { c.off[0]=a; c.off[1]=b; c.off[2]=d; }
            else if (!std::strcmp(key, "rot"))       { c.rotdeg[0]=a; c.rotdeg[1]=b; c.rotdeg[2]=d; }
            else if (!std::strcmp(key, "width"))     c.width = a;
            else if (!std::strcmp(key, "face_show")) c.faceShow = a;
            else if (!std::strcmp(key, "face_hide")) c.faceHide = a;
            else if (!std::strcmp(key, "max_dist"))  c.maxDist = a;
            else if (!std::strcmp(key, "hold"))      c.hold = (int)a;
            else if (!std::strcmp(key, "always"))    c.always = (int)a;
            else if (!std::strcmp(key, "locked"))    c.locked = (int)a;
        }
    }
    std::fclose(f);
    g_cfg = c;
    char m[160]; std::snprintf(m, sizeof m,
        "HUD: conf reloaded  hand=%d off=%.3f,%.3f,%.3f rot=%.0f,%.0f,%.0f w=%.2f face=%.2f/%.2f always=%d",
        c.hand, c.off[0], c.off[1], c.off[2], c.rotdeg[0], c.rotdeg[1], c.rotdeg[2],
        c.width, c.faceShow, c.faceHide, c.always);
    layer_log(m);
}

// ---- minimal quaternion/pose math ----
XrQuaternionf qmul(const XrQuaternionf &a, const XrQuaternionf &b) {
    return { a.w*b.x + a.x*b.w + a.y*b.z - a.z*b.y,
             a.w*b.y - a.x*b.z + a.y*b.w + a.z*b.x,
             a.w*b.z + a.x*b.y - a.y*b.x + a.z*b.w,
             a.w*b.w - a.x*b.x - a.y*b.y - a.z*b.z };
}
XrVector3f qrot(const XrQuaternionf &q, const XrVector3f &v) {
    // v' = v + 2*qv x (qv x v + w*v)
    XrVector3f u{q.x, q.y, q.z};
    XrVector3f c1{u.y*v.z - u.z*v.y + q.w*v.x, u.z*v.x - u.x*v.z + q.w*v.y, u.x*v.y - u.y*v.x + q.w*v.z};
    return { v.x + 2*(u.y*c1.z - u.z*c1.y), v.y + 2*(u.z*c1.x - u.x*c1.z), v.z + 2*(u.x*c1.y - u.y*c1.x) };
}
XrQuaternionf qeulerXYZ(float xd, float yd, float zd) {
    auto ax = [](float deg, float x, float y, float z) {
        float r = deg * 3.14159265f / 180.f / 2.f;
        return XrQuaternionf{ std::sin(r)*x, std::sin(r)*y, std::sin(r)*z, std::cos(r) };
    };
    return qmul(qmul(ax(xd,1,0,0), ax(yd,0,1,0)), ax(zd,0,0,1));
}

XrQuaternionf qconj(const XrQuaternionf &q) { return {-q.x, -q.y, -q.z, q.w}; }

// Decompose a rotation into the XYZ euler order qeulerXYZ() composes (degrees).
void eulerXYZFromQuat(const XrQuaternionf &q, float &xd, float &yd, float &zd) {
    float x=q.x, y=q.y, z=q.z, w=q.w;
    float R00 = 1 - 2*(y*y + z*z), R01 = 2*(x*y - w*z), R02 = 2*(x*z + w*y);
    float R12 = 2*(y*z - w*x),     R22 = 1 - 2*(x*x + y*y);
    if (R02 >  1.f) R02 =  1.f;
    if (R02 < -1.f) R02 = -1.f;
    const float RAD = 180.f / 3.14159265f;
    yd = std::asin(R02) * RAD;
    xd = std::atan2(-R12, R22) * RAD;
    zd = std::atan2(-R01, R00) * RAD;
}

// Persist a dragged placement: rewrite ONLY the offset=/rot= lines of hud.conf (comments and
// the rest survive), and update the in-memory cfg immediately so the panel cannot snap back
// while waiting for the ~1 s conf reload.
void writeConfPose(const XrVector3f &off, float rx, float ry, float rz) {
    g_cfg.off[0]=off.x; g_cfg.off[1]=off.y; g_cfg.off[2]=off.z;
    g_cfg.rotdeg[0]=rx; g_cfg.rotdeg[1]=ry; g_cfg.rotdeg[2]=rz;
    FILE *f = std::fopen(confPath(), "r");
    std::string out; char line[160];
    bool haveOff=false, haveRot=false;
    if (f) {
        while (std::fgets(line, sizeof line, f)) {
            if      (!std::strncmp(line, "offset", 6)) { char b[96];
                std::snprintf(b, sizeof b, "offset = %.3f, %.3f, %.3f\n", off.x, off.y, off.z);
                out += b; haveOff = true; }
            else if (!std::strncmp(line, "rot", 3) && line[3] != 'a') { char b[96];
                std::snprintf(b, sizeof b, "rot = %.1f, %.1f, %.1f\n", rx, ry, rz);
                out += b; haveRot = true; }
            else out += line;
        }
        std::fclose(f);
    }
    if (!haveOff) { char b[96]; std::snprintf(b, sizeof b, "offset = %.3f, %.3f, %.3f\n", off.x, off.y, off.z); out += b; }
    if (!haveRot) { char b[96]; std::snprintf(b, sizeof b, "rot = %.1f, %.1f, %.1f\n", rx, ry, rz); out += b; }
    f = std::fopen(confPath(), "w");
    if (f) { std::fwrite(out.data(), 1, out.size(), f); std::fclose(f); }
    struct stat st{};   // swallow our own rewrite so loadCfg doesn't re-log it
    if (::stat(confPath(), &st) == 0) g_cfgMtime = st.st_mtime;
    char m[128]; std::snprintf(m, sizeof m,
        "HUD: drag saved  offset=%.3f,%.3f,%.3f rot=%.0f,%.0f,%.0f", off.x, off.y, off.z, rx, ry, rz);
    layer_log(m);
}

static inline int iabs(int v) { return v < 0 ? -v : v; }

// A fixed test pattern: cyan border, four distinct corner squares (to expose any flip/mirror),
// and an orange centre cross. RGBA8, top-left origin (OpenXR Vulkan is not V-flipped like GL).
void makeTestPattern(uint8_t *px, bool bgra) {
    const int W = KATWALK_HUD_W, H = KATWALK_HUD_H;
    for (int y = 0; y < H; ++y) for (int x = 0; x < W; ++x) {
        uint8_t r = 12, g = 16, b = 25;  // panel bg
        if (x < 4 || y < 4 || x >= W - 4 || y >= H - 4) { r = 34; g = 211; b = 238; }  // border
        if      (x <  40 && y <  40)          { r = 248; g =  60; b =  60; }  // TL red
        else if (x >= W-40 && y <  40)        { r =  52; g = 211; b = 153; }  // TR green
        else if (x <  40 && y >= H-40)        { r =  60; g = 120; b = 248; }  // BL blue
        else if (x >= W-40 && y >= H-40)      { r = 236; g = 244; b = 252; }  // BR white
        if (iabs(x - W/2) < 2 || iabs(y - H/2) < 2) { r = 251; g = 146; b = 60; }  // centre cross
        uint8_t *p = px + (size_t)(y * W + x) * 4;
        if (bgra) { p[0] = b; p[1] = g; p[2] = r; } else { p[0] = r; p[1] = g; p[2] = b; }
        p[3] = 255;
    }
}

int findMemType(uint32_t typeBits, VkMemoryPropertyFlags want) {
    VkPhysicalDeviceMemoryProperties mp{};
    g_h.vk.GetPhysMemProps(g_h.vkPhys, &mp);
    for (uint32_t i = 0; i < mp.memoryTypeCount; ++i)
        if ((typeBits & (1u << i)) &&
            (mp.memoryTypes[i].propertyFlags & want) == want) return (int)i;
    return -1;
}

// Create our action set + grip-pose action once (idempotent; needs only the instance).
void ensureActions() {
    if (g_hand.created || !g.nextCreateActionSet || !g.nextCreateAction || !g.nextStringToPath) return;
    g.nextStringToPath(g.instance, "/user/hand/left",  &g_hand.hands[0]);
    g.nextStringToPath(g.instance, "/user/hand/right", &g_hand.hands[1]);
    XrActionSetCreateInfo asci{XR_TYPE_ACTION_SET_CREATE_INFO};
    std::strcpy(asci.actionSetName, "katwalk_hud");
    std::strcpy(asci.localizedActionSetName, "katwalk-linux HUD");
    asci.priority = 0;
    if (XR_FAILED(g.nextCreateActionSet(g.instance, &asci, &g_hand.actionSet))) {
        layer_log("HUD: xrCreateActionSet failed"); return;
    }
    auto mk = [&](const char *name, const char *loc, XrActionType type, XrAction *out) {
        XrActionCreateInfo aci{XR_TYPE_ACTION_CREATE_INFO};
        std::strncpy(aci.actionName, name, sizeof(aci.actionName) - 1);
        std::strncpy(aci.localizedActionName, loc, sizeof(aci.localizedActionName) - 1);
        aci.actionType = type;
        aci.countSubactionPaths = 2;
        aci.subactionPaths = g_hand.hands;
        return XR_SUCCEEDED(g.nextCreateAction(g_hand.actionSet, &aci, out));
    };
    if (!mk("katwalk_grip",    "katwalk HUD anchor",  XR_ACTION_TYPE_POSE_INPUT,    &g_hand.gripPose) ||
        !mk("katwalk_aim",     "katwalk HUD laser",   XR_ACTION_TYPE_POSE_INPUT,    &g_hand.aimPose)  ||
        !mk("katwalk_trigger", "katwalk HUD click",   XR_ACTION_TYPE_BOOLEAN_INPUT, &g_hand.trigBool) ||
        !mk("katwalk_squeeze", "katwalk HUD grab",    XR_ACTION_TYPE_BOOLEAN_INPUT, &g_hand.sqzBool)) {
        layer_log("HUD: xrCreateAction failed"); return;
    }
    g_hand.created = true;
    layer_log("HUD: action set created (grip+aim pose, trigger, squeeze; both hands)");
}

// Piggyback our bindings onto every profile the app suggests, so whichever profile the runtime
// picks, our anchor + laser have bindings. Poses exist in every profile; the click/grab paths
// differ per profile, so pick them from the profile name and fall back gracefully.
XrResult suggestWithOurs(XrInstance instance, const XrInteractionProfileSuggestedBinding *sb) {
    ensureActions();
    if (!g_hand.created || !g.nextStringToPath || !g.nextPathToString)
        return g.nextSuggest(instance, sb);

    char prof[XR_MAX_PATH_LENGTH] = {0}; uint32_t plen = 0;
    g.nextPathToString(instance, sb->interactionProfile, sizeof(prof), &plen, prof);
    std::string p(prof);
    // per-profile click/grab input paths (suffix after /user/hand/X/input/)
    const char *click = "trigger/value", *grab = "squeeze/value";      // oculus/index/most
    if (p.find("khr/simple_controller") != std::string::npos) { click = "select/click"; grab = nullptr; }
    else if (p.find("htc/vive_controller") != std::string::npos) { grab = "squeeze/click"; }
    else if (p.find("microsoft/motion_controller") != std::string::npos) { grab = "squeeze/click"; }

    auto path = [&](const char *hand, const char *suffix) {
        XrPath out = XR_NULL_PATH;
        std::string s = std::string("/user/hand/") + hand + "/input/" + suffix;
        g.nextStringToPath(instance, s.c_str(), &out);
        return out;
    };
    std::vector<XrActionSuggestedBinding> ours;
    for (const char *h : {"left", "right"}) {
        ours.push_back({g_hand.gripPose, path(h, "grip/pose")});
        ours.push_back({g_hand.aimPose,  path(h, "aim/pose")});
        ours.push_back({g_hand.trigBool, path(h, click)});
        if (grab) ours.push_back({g_hand.sqzBool, path(h, grab)});
    }
    std::vector<XrActionSuggestedBinding> v(sb->suggestedBindings,
                                            sb->suggestedBindings + sb->countSuggestedBindings);
    v.insert(v.end(), ours.begin(), ours.end());
    XrInteractionProfileSuggestedBinding nsb = *sb;
    nsb.countSuggestedBindings = (uint32_t)v.size();
    nsb.suggestedBindings = v.data();
    XrResult res = g.nextSuggest(instance, &nsb);
    if (XR_FAILED(res)) {
        char m[160]; std::snprintf(m, sizeof m,
            "HUD: suggest with buttons REJECTED for %s (res=%d) - retrying poses-only", prof, (int)res);
        layer_log(m);
        // retry with poses only (some profile rejected our button paths)...
        v.assign(sb->suggestedBindings, sb->suggestedBindings + sb->countSuggestedBindings);
        for (const char *h : {"left", "right"}) {
            v.push_back({g_hand.gripPose, path(h, "grip/pose")});
            v.push_back({g_hand.aimPose,  path(h, "aim/pose")});
        }
        nsb.countSuggestedBindings = (uint32_t)v.size();
        nsb.suggestedBindings = v.data();
        res = g.nextSuggest(instance, &nsb);
        // ...and never take the game down over our additions
        if (XR_FAILED(res)) { layer_log("HUD: poses-only also rejected - no HUD input on this profile");
                              res = g.nextSuggest(instance, sb); }
    } else {
        char m[128]; std::snprintf(m, sizeof m, "HUD: bindings accepted for %s", prof);
        layer_log(m);
    }
    return res;
}

// Ride along on the app's attach (once per session) and create the per-hand grip spaces.
XrResult attachWithOurs(XrSession session, const XrSessionActionSetsAttachInfo *ai) {
    if (!g_hand.created || g_hand.attached || !g.nextAttachActionSets)
        return g.nextAttachActionSets ? g.nextAttachActionSets(session, ai)
                                      : XR_ERROR_FUNCTION_UNSUPPORTED;
    std::vector<XrActionSet> sets(ai->actionSets, ai->actionSets + ai->countActionSets);
    sets.push_back(g_hand.actionSet);
    XrSessionActionSetsAttachInfo nai = *ai;
    nai.countActionSets = (uint32_t)sets.size();
    nai.actionSets = sets.data();
    XrResult res = g.nextAttachActionSets(session, &nai);
    if (XR_FAILED(res)) {  // don't break the game if our set is rejected
        layer_log("HUD: attach with our set failed - passing through (no hand anchor)");
        return g.nextAttachActionSets(session, ai);
    }
    g_hand.attached = true;
    if (g.nextCreateActionSpace) {
        for (int h = 0; h < 2; ++h) {
            XrActionSpaceCreateInfo si{XR_TYPE_ACTION_SPACE_CREATE_INFO};
            si.subactionPath = g_hand.hands[h];
            si.poseInActionSpace.orientation.w = 1.0f;
            si.action = g_hand.gripPose;
            if (XR_FAILED(g.nextCreateActionSpace(session, &si, &g_hand.gripSpace[h])))
                g_hand.gripSpace[h] = XR_NULL_HANDLE;
            si.action = g_hand.aimPose;
            if (XR_FAILED(g.nextCreateActionSpace(session, &si, &g_hand.aimSpace[h])))
                g_hand.aimSpace[h] = XR_NULL_HANDLE;
        }
    }
    layer_log("HUD: action set attached, grip+aim spaces created");
    return res;
}

// Keep our set active on every sync so the runtime updates the grip pose.
XrResult syncWithOurs(XrSession session, const XrActionsSyncInfo *si) {
    g_syncCalls.fetch_add(1, std::memory_order_relaxed);
    if (!g_hand.attached || !g.nextSyncActions)
        return g.nextSyncActions ? g.nextSyncActions(session, si) : XR_ERROR_FUNCTION_UNSUPPORTED;
    std::vector<XrActiveActionSet> act(si->activeActionSets,
                                       si->activeActionSets + si->countActiveActionSets);
    act.push_back({g_hand.actionSet, XR_NULL_PATH});
    XrActionsSyncInfo nsi = *si;
    nsi.countActiveActionSets = (uint32_t)act.size();
    nsi.activeActionSets = act.data();
    XrResult res = g.nextSyncActions(session, &nsi);
    if (XR_FAILED(res)) return g.nextSyncActions(session, si);
    return res;
}

// Inspect xrCreateSession's next chain for a Vulkan graphics binding; dlopen libvulkan and load
// the instance-level entry points. Device-level ones are loaded later in init().
void onSessionCreated(XrSession session, const XrSessionCreateInfo *ci) {
    std::lock_guard<std::mutex> lk(g_hmtx);
    g_h.session = session;
    g_h.parsed  = true;
    const XrBaseInStructure *b = ci ? (const XrBaseInStructure *)ci->next : nullptr;
    const XrGraphicsBindingVulkanKHR *vb = nullptr;
    for (; b; b = b->next)
        if (b->type == XR_TYPE_GRAPHICS_BINDING_VULKAN_KHR)
            vb = (const XrGraphicsBindingVulkanKHR *)b;
    if (!vb) { layer_log("HUD: non-Vulkan session (no XrGraphicsBindingVulkanKHR) - HUD disabled"); return; }

    g_h.vkLib = dlopen("libvulkan.so.1", RTLD_NOW | RTLD_LOCAL);
    if (!g_h.vkLib) g_h.vkLib = dlopen("libvulkan.so", RTLD_NOW | RTLD_LOCAL);
    if (!g_h.vkLib) { layer_log("HUD: dlopen libvulkan failed - HUD disabled"); return; }
    g_h.vk.GetInstanceProcAddr =
        (PFN_vkGetInstanceProcAddr)dlsym(g_h.vkLib, "vkGetInstanceProcAddr");
    if (!g_h.vk.GetInstanceProcAddr) { layer_log("HUD: no vkGetInstanceProcAddr - HUD disabled"); return; }

    g_h.vkInstance  = vb->instance;
    g_h.vkPhys      = vb->physicalDevice;
    g_h.vkDevice    = vb->device;
    g_h.queueFamily = vb->queueFamilyIndex;
    g_h.queueIndex  = vb->queueIndex;

    auto gi = [&](const char *n) { return g_h.vk.GetInstanceProcAddr(g_h.vkInstance, n); };
    g_h.vk.GetDeviceProcAddr = (PFN_vkGetDeviceProcAddr)gi("vkGetDeviceProcAddr");
    g_h.vk.GetPhysMemProps   = (PFN_vkGetPhysicalDeviceMemoryProperties)gi("vkGetPhysicalDeviceMemoryProperties");
    if (!g_h.vk.GetDeviceProcAddr || !g_h.vk.GetPhysMemProps) {
        layer_log("HUD: instance proc load failed - HUD disabled"); return;
    }
    g_h.vkOk = true;
    layer_log("HUD: Vulkan binding captured");
}

bool loadDeviceFns() {
    auto gd = [&](const char *n) { return g_h.vk.GetDeviceProcAddr(g_h.vkDevice, n); };
    g_h.vk.GetDeviceQueue         = (PFN_vkGetDeviceQueue)gd("vkGetDeviceQueue");
    g_h.vk.CreateCommandPool      = (PFN_vkCreateCommandPool)gd("vkCreateCommandPool");
    g_h.vk.AllocateCommandBuffers = (PFN_vkAllocateCommandBuffers)gd("vkAllocateCommandBuffers");
    g_h.vk.CreateBuffer           = (PFN_vkCreateBuffer)gd("vkCreateBuffer");
    g_h.vk.GetBufferMemReq        = (PFN_vkGetBufferMemoryRequirements)gd("vkGetBufferMemoryRequirements");
    g_h.vk.AllocateMemory         = (PFN_vkAllocateMemory)gd("vkAllocateMemory");
    g_h.vk.BindBufferMemory       = (PFN_vkBindBufferMemory)gd("vkBindBufferMemory");
    g_h.vk.MapMemory              = (PFN_vkMapMemory)gd("vkMapMemory");
    g_h.vk.UnmapMemory            = (PFN_vkUnmapMemory)gd("vkUnmapMemory");
    g_h.vk.FlushMappedRanges      = (PFN_vkFlushMappedMemoryRanges)gd("vkFlushMappedMemoryRanges");
    g_h.vk.BeginCommandBuffer     = (PFN_vkBeginCommandBuffer)gd("vkBeginCommandBuffer");
    g_h.vk.EndCommandBuffer       = (PFN_vkEndCommandBuffer)gd("vkEndCommandBuffer");
    g_h.vk.ResetCommandBuffer     = (PFN_vkResetCommandBuffer)gd("vkResetCommandBuffer");
    g_h.vk.CmdPipelineBarrier     = (PFN_vkCmdPipelineBarrier)gd("vkCmdPipelineBarrier");
    g_h.vk.CmdCopyBufferToImage   = (PFN_vkCmdCopyBufferToImage)gd("vkCmdCopyBufferToImage");
    g_h.vk.CmdCopyImageToBuffer   = (PFN_vkCmdCopyImageToBuffer)gd("vkCmdCopyImageToBuffer");
    g_h.vk.QueueSubmit            = (PFN_vkQueueSubmit)gd("vkQueueSubmit");
    g_h.vk.QueueWaitIdle          = (PFN_vkQueueWaitIdle)gd("vkQueueWaitIdle");
    return g_h.vk.GetDeviceQueue && g_h.vk.CreateCommandPool && g_h.vk.AllocateCommandBuffers &&
           g_h.vk.CreateBuffer && g_h.vk.GetBufferMemReq && g_h.vk.AllocateMemory &&
           g_h.vk.BindBufferMemory && g_h.vk.MapMemory && g_h.vk.UnmapMemory &&
           g_h.vk.FlushMappedRanges && g_h.vk.BeginCommandBuffer && g_h.vk.EndCommandBuffer &&
           g_h.vk.ResetCommandBuffer && g_h.vk.CmdPipelineBarrier && g_h.vk.CmdCopyBufferToImage &&
           g_h.vk.CmdCopyImageToBuffer && g_h.vk.QueueSubmit && g_h.vk.QueueWaitIdle;
}

int64_t chooseFormat(XrSession s) {
    uint32_t n = 0;
    if (!g.nextEnumFormats || XR_FAILED(g.nextEnumFormats(s, 0, &n, nullptr)) || n == 0) return 0;
    std::vector<int64_t> fmts(n);
    if (XR_FAILED(g.nextEnumFormats(s, n, &n, fmts.data()))) return 0;
    const int64_t prefer[] = {
        VK_FORMAT_R8G8B8A8_SRGB, VK_FORMAT_B8G8R8A8_SRGB,
        VK_FORMAT_R8G8B8A8_UNORM, VK_FORMAT_B8G8R8A8_UNORM,
    };
    for (int64_t want : prefer)
        for (int64_t have : fmts)
            if (have == want) return want;
    return fmts[0];  // fall back to whatever the runtime offers first
}

void openLaserShm();   // defined with the shm bridge below
void openPosesShm();
void publishPoses(XrTime displayTime);

bool init(XrSession s) {
    if (!loadDeviceFns()) { layer_log("HUD: device proc load failed"); return false; }
    g_h.vk.GetDeviceQueue(g_h.vkDevice, g_h.queueFamily, g_h.queueIndex, &g_h.queue);

    XrReferenceSpaceCreateInfo si{XR_TYPE_REFERENCE_SPACE_CREATE_INFO};
    si.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_VIEW;
    si.poseInReferenceSpace.orientation.w = 1.0f;
    if (XR_FAILED(g.nextCreateRefSpace(s, &si, &g_h.viewSpace))) { layer_log("HUD: view space failed"); return false; }
    si.referenceSpaceType = XR_REFERENCE_SPACE_TYPE_LOCAL;
    if (XR_FAILED(g.nextCreateRefSpace(s, &si, &g_h.localSpace))) { layer_log("HUD: local space failed"); return false; }

    int64_t fmt = chooseFormat(s);
    if (!fmt) { layer_log("HUD: no swapchain format"); return false; }
    g_h.bgra = (fmt == VK_FORMAT_B8G8R8A8_SRGB || fmt == VK_FORMAT_B8G8R8A8_UNORM);
    { char m[160]; std::snprintf(m, sizeof m,
        "HUD: chosen swapchain format=%lld bgra=%d requested %dx%d",
        (long long)fmt, (int)g_h.bgra, KATWALK_HUD_W, KATWALK_HUD_H); layer_log(m); }

    XrSwapchainCreateInfo sci{XR_TYPE_SWAPCHAIN_CREATE_INFO};
    sci.usageFlags = XR_SWAPCHAIN_USAGE_TRANSFER_DST_BIT | XR_SWAPCHAIN_USAGE_SAMPLED_BIT;
    sci.format = fmt; sci.sampleCount = 1;
    sci.width = KATWALK_HUD_W; sci.height = KATWALK_HUD_H;
    sci.faceCount = 1; sci.arraySize = 1; sci.mipCount = 1;
    if (XR_FAILED(g.nextCreateSwapchain(s, &sci, &g_h.swapchain))) { layer_log("HUD: create swapchain failed"); return false; }

    uint32_t ni = 0;
    if (XR_FAILED(g.nextEnumSwImages(g_h.swapchain, 0, &ni, nullptr)) || ni == 0) { layer_log("HUD: enum images failed"); return false; }
    std::vector<XrSwapchainImageVulkanKHR> xi(ni, {XR_TYPE_SWAPCHAIN_IMAGE_VULKAN_KHR});
    if (XR_FAILED(g.nextEnumSwImages(g_h.swapchain, ni, &ni, (XrSwapchainImageBaseHeader *)xi.data()))) { layer_log("HUD: get images failed"); return false; }
    g_h.images.resize(ni);
    for (uint32_t i = 0; i < ni; ++i) g_h.images[i] = xi[i].image;
    { char m[96]; std::snprintf(m, sizeof m, "HUD: swapchain image count=%u", ni); layer_log(m); }

    VkCommandPoolCreateInfo pci{VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO};
    pci.flags = VK_COMMAND_POOL_CREATE_RESET_COMMAND_BUFFER_BIT;
    pci.queueFamilyIndex = g_h.queueFamily;
    if (g_h.vk.CreateCommandPool(g_h.vkDevice, &pci, nullptr, &g_h.cmdPool) != VK_SUCCESS) return false;
    VkCommandBufferAllocateInfo cbi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO};
    cbi.commandPool = g_h.cmdPool; cbi.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY; cbi.commandBufferCount = 1;
    if (g_h.vk.AllocateCommandBuffers(g_h.vkDevice, &cbi, &g_h.cmd) != VK_SUCCESS) return false;

    VkBufferCreateInfo bci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    bci.size = KATWALK_HUD_BYTES; bci.usage = VK_BUFFER_USAGE_TRANSFER_SRC_BIT;
    bci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (g_h.vk.CreateBuffer(g_h.vkDevice, &bci, nullptr, &g_h.staging) != VK_SUCCESS) return false;
    VkMemoryRequirements mr{}; g_h.vk.GetBufferMemReq(g_h.vkDevice, g_h.staging, &mr);
    int mt = findMemType(mr.memoryTypeBits,
                         VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
    if (mt < 0) mt = findMemType(mr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
    if (mt < 0) { layer_log("HUD: no host-visible memory"); return false; }
    VkMemoryAllocateInfo mai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
    mai.allocationSize = mr.size; mai.memoryTypeIndex = (uint32_t)mt;
    if (g_h.vk.AllocateMemory(g_h.vkDevice, &mai, nullptr, &g_h.stagingMem) != VK_SUCCESS) return false;
    if (g_h.vk.BindBufferMemory(g_h.vkDevice, g_h.staging, g_h.stagingMem, 0) != VK_SUCCESS) return false;

    // Persistently map the staging buffer: initial test pattern now, live overlay frames later.
    void *map = nullptr;
    if (g_h.vk.MapMemory(g_h.vkDevice, g_h.stagingMem, 0, KATWALK_HUD_BYTES, 0, &map) != VK_SUCCESS) return false;
    g_h.stagingMap = (uint8_t *)map;
    makeTestPattern(g_h.stagingMap, g_h.bgra);
    VkMappedMemoryRange range{VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE};
    range.memory = g_h.stagingMem; range.offset = 0; range.size = VK_WHOLE_SIZE;
    g_h.vk.FlushMappedRanges(g_h.vkDevice, 1, &range);

    openLaserShm();   // interaction channel out (we own it)
    openPosesShm();   // HMD yaw out for the daemon (P5)

    // Debug readback buffer (host-visible, TRANSFER_DST): one-shot GPU->CPU copy of the
    // composited image so the actual on-GPU result can be dumped to disk and inspected.
    VkBufferCreateInfo rci{VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO};
    rci.size = KATWALK_HUD_BYTES; rci.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
    rci.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
    if (g_h.vk.CreateBuffer(g_h.vkDevice, &rci, nullptr, &g_h.readback) == VK_SUCCESS) {
        VkMemoryRequirements rmr{}; g_h.vk.GetBufferMemReq(g_h.vkDevice, g_h.readback, &rmr);
        int rmt = findMemType(rmr.memoryTypeBits,
                              VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT);
        if (rmt < 0) rmt = findMemType(rmr.memoryTypeBits, VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT);
        VkMemoryAllocateInfo rmai{VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO};
        rmai.allocationSize = rmr.size; rmai.memoryTypeIndex = (uint32_t)(rmt < 0 ? 0 : rmt);
        if (rmt < 0 || g_h.vk.AllocateMemory(g_h.vkDevice, &rmai, nullptr, &g_h.readbackMem) != VK_SUCCESS ||
            g_h.vk.BindBufferMemory(g_h.vkDevice, g_h.readback, g_h.readbackMem, 0) != VK_SUCCESS) {
            g_h.readback = VK_NULL_HANDLE;  // readback disabled; the HUD still works
        }
    }

    g_h.inited = true;
    layer_log("HUD: initialized (swapchain + Vulkan staging ready)");
    return true;
}

// Copy the staging buffer into one swapchain image, transitioning its layout around the copy.
bool uploadTo(VkImage img) {
    g_h.vk.ResetCommandBuffer(g_h.cmd, 0);
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (g_h.vk.BeginCommandBuffer(g_h.cmd, &bi) != VK_SUCCESS) return false;

    VkImageMemoryBarrier toDst{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    toDst.srcAccessMask = 0; toDst.dstAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
    toDst.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED; toDst.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
    toDst.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED; toDst.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    toDst.image = img;
    toDst.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    g_h.vk.CmdPipelineBarrier(g_h.cmd, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                              0, 0, nullptr, 0, nullptr, 1, &toDst);

    VkBufferImageCopy region{};
    region.bufferRowLength   = KATWALK_HUD_W;  // explicit (texels): rule out any stride ambiguity
    region.bufferImageHeight = KATWALK_HUD_H;
    region.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    region.imageExtent = {KATWALK_HUD_W, KATWALK_HUD_H, 1};
    g_h.vk.CmdCopyBufferToImage(g_h.cmd, g_h.staging, img, VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, 1, &region);

    VkImageMemoryBarrier toRead{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    toRead.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT; toRead.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    toRead.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL; toRead.newLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    toRead.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED; toRead.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    toRead.image = img;
    toRead.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    g_h.vk.CmdPipelineBarrier(g_h.cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                              0, 0, nullptr, 0, nullptr, 1, &toRead);

    if (g_h.vk.EndCommandBuffer(g_h.cmd) != VK_SUCCESS) return false;
    VkSubmitInfo su{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    su.commandBufferCount = 1; su.pCommandBuffers = &g_h.cmd;
    if (g_h.vk.QueueSubmit(g_h.queue, 1, &su, VK_NULL_HANDLE) != VK_SUCCESS) return false;
    g_h.vk.QueueWaitIdle(g_h.queue);  // P1: simple sync; a fence-based path can come later
    return true;
}

// --- debug: the host-shared katwalk dir (bind-mounted into the game sandbox, see layer_log) ---
std::string hudDir() {
    ::mkdir("/tmp/katwalk", 0777);
    return "/tmp/katwalk";
}

// ---- shm bridge: live HUD frames in (katwalk/hud), laser events out (katwalk/laser) ----
volatile KatHud  *g_hudShm  = nullptr;   // written by the Python overlay
const uint8_t    *g_hudPx   = nullptr;   // its RGBA pixels (immediately after the header)
volatile KatLaser*g_laser   = nullptr;   // written by us, read by the Python overlay
uint32_t          g_hudSeq  = 0;         // last framebuffer seq we uploaded
uint32_t          g_laserSeq= 0;
uint32_t          g_shmPoll = 0;

void openHudShm() {   // the overlay creates the file; retry cheaply until it appears
    int fd = ::open("/tmp/katwalk/hud", O_RDONLY);
    if (fd < 0) return;
    size_t len = sizeof(KatHud) + KATWALK_HUD_BYTES;
    void *m = mmap(nullptr, len, PROT_READ, MAP_SHARED, fd, 0);
    ::close(fd);
    if (m == MAP_FAILED) return;
    g_hudShm = static_cast<volatile KatHud *>(m);
    g_hudPx  = reinterpret_cast<const uint8_t *>(m) + sizeof(KatHud);
    layer_log("HUD: live framebuffer shm attached (katwalk/hud)");
}

void openLaserShm() {  // we own this one: create + size + map rw
    int fd = ::open("/tmp/katwalk/laser", O_RDWR | O_CREAT, 0666);
    if (fd < 0) return;
    if (ftruncate(fd, sizeof(KatLaser)) != 0) { ::close(fd); return; }
    void *m = mmap(nullptr, sizeof(KatLaser), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    ::close(fd);
    if (m == MAP_FAILED) return;
    g_laser = static_cast<volatile KatLaser *>(m);
    layer_log("HUD: laser shm ready (katwalk/laser)");
}

void laserEvent(uint32_t event, uint32_t button, float px, float py, uint32_t onPanel) {
    if (!g_laser) return;
    g_laser->event = event; g_laser->button = button;
    g_laser->x = px; g_laser->y = py; g_laser->on_panel = onPanel;
    g_laser->seq = ++g_laserSeq;   // seq LAST: the reader treats a new seq as a complete event
}

// ---- katwalk/poses: HMD pose + yaw for the daemon (replaces the OpenVR HmdYaw reader) ----
volatile KatPoses *g_poses = nullptr;
uint32_t           g_posesSeq = 0;

void openPosesShm() {
    int fd = ::open("/tmp/katwalk/poses", O_RDWR | O_CREAT, 0666);
    if (fd < 0) return;
    if (ftruncate(fd, sizeof(KatPoses)) != 0) { ::close(fd); return; }
    void *m = mmap(nullptr, sizeof(KatPoses), PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    ::close(fd);
    if (m == MAP_FAILED) return;
    g_poses = static_cast<volatile KatPoses *>(m);
    layer_log("HUD: poses shm ready (katwalk/poses)");
}

void writeRow34(volatile float *dst, const XrPosef &p) {   // 3x4 [R|t], row-major
    float x = p.orientation.x, y = p.orientation.y, z = p.orientation.z, w = p.orientation.w;
    dst[0] = 1 - 2*(y*y + z*z); dst[1] = 2*(x*y - w*z);     dst[2] = 2*(x*z + w*y);     dst[3]  = p.position.x;
    dst[4] = 2*(x*y + w*z);     dst[5] = 1 - 2*(x*x + z*z); dst[6] = 2*(y*z - w*x);     dst[7]  = p.position.y;
    dst[8] = 2*(x*z - w*y);     dst[9] = 2*(y*z + w*x);     dst[10] = 1 - 2*(x*x + y*y); dst[11] = p.position.z;
}

void publishPoses(XrTime displayTime) {
    if (!g_poses || !g.nextLocateSpace || g_h.viewSpace == XR_NULL_HANDLE) return;
    XrSpaceLocation lv{XR_TYPE_SPACE_LOCATION};
    if (XR_FAILED(g.nextLocateSpace(g_h.viewSpace, g_h.localSpace, displayTime, &lv))) return;
    constexpr XrSpaceLocationFlags REQ =
        XR_SPACE_LOCATION_POSITION_VALID_BIT | XR_SPACE_LOCATION_ORIENTATION_VALID_BIT;
    bool ok = (lv.locationFlags & REQ) == REQ;
    g_poses->hmd_valid = ok ? 1u : 0u;
    if (ok) {
        writeRow34(g_poses->hmd, lv.pose);
        // yaw with the same convention the OpenVR reader used: atan2(R02, R22)
        float x = lv.pose.orientation.x, y = lv.pose.orientation.y,
              z = lv.pose.orientation.z, w = lv.pose.orientation.w;
        g_poses->hmd_yaw_deg = std::atan2(2*(x*z + w*y), 1 - 2*(x*x + y*y)) * 180.f / 3.14159265f;
    }
    g_poses->lctrl_valid = 0;   // controller slots reserved; the daemon only needs yaw today
    g_poses->rctrl_valid = 0;
    g_poses->recenter_seq = g_recenterSeq.load(std::memory_order_relaxed);
    g_poses->seq = ++g_posesSeq;   // seq LAST
}

// Write an RGBA buffer to a binary PPM (P6, RGB) so it can be inspected off-headset.
void writePPM(const std::string &path, const uint8_t *rgba, int w, int h, bool bgra) {
    FILE *f = std::fopen(path.c_str(), "wb");
    if (!f) return;
    std::fprintf(f, "P6\n%d %d\n255\n", w, h);
    for (int i = 0; i < w * h; ++i) {
        const uint8_t *p = rgba + (size_t)i * 4;
        uint8_t rgb[3];
        if (bgra) { rgb[0] = p[2]; rgb[1] = p[1]; rgb[2] = p[0]; }
        else      { rgb[0] = p[0]; rgb[1] = p[1]; rgb[2] = p[2]; }
        std::fwrite(rgb, 1, 3, f);
    }
    std::fclose(f);
}

// One-shot: copy the just-composited image back to the readback buffer and dump it to
// <katwalk>/hud-gpu.ppm, so the ACTUAL on-GPU result (not just our source pattern) can be
// inspected. img is in COLOR_ATTACHMENT_OPTIMAL on entry; we restore that layout for release.
void readbackDump(VkImage img) {
    if (!g_h.readback || !g_h.vk.CmdCopyImageToBuffer) return;
    g_h.vk.ResetCommandBuffer(g_h.cmd, 0);
    VkCommandBufferBeginInfo bi{VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO};
    bi.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (g_h.vk.BeginCommandBuffer(g_h.cmd, &bi) != VK_SUCCESS) return;

    VkImageMemoryBarrier toSrc{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    toSrc.srcAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT; toSrc.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    toSrc.oldLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL; toSrc.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
    toSrc.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED; toSrc.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    toSrc.image = img; toSrc.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    g_h.vk.CmdPipelineBarrier(g_h.cmd, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
                              0, 0, nullptr, 0, nullptr, 1, &toSrc);

    VkBufferImageCopy region{};
    region.bufferRowLength = KATWALK_HUD_W; region.bufferImageHeight = KATWALK_HUD_H;
    region.imageSubresource = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 0, 1};
    region.imageExtent = {KATWALK_HUD_W, KATWALK_HUD_H, 1};
    g_h.vk.CmdCopyImageToBuffer(g_h.cmd, img, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, g_h.readback, 1, &region);

    VkImageMemoryBarrier back{VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER};
    back.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT; back.dstAccessMask = VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT;
    back.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL; back.newLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    back.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED; back.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    back.image = img; back.subresourceRange = {VK_IMAGE_ASPECT_COLOR_BIT, 0, 1, 0, 1};
    g_h.vk.CmdPipelineBarrier(g_h.cmd, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT,
                              0, 0, nullptr, 0, nullptr, 1, &back);

    if (g_h.vk.EndCommandBuffer(g_h.cmd) != VK_SUCCESS) return;
    VkSubmitInfo su{VK_STRUCTURE_TYPE_SUBMIT_INFO};
    su.commandBufferCount = 1; su.pCommandBuffers = &g_h.cmd;
    if (g_h.vk.QueueSubmit(g_h.queue, 1, &su, VK_NULL_HANDLE) != VK_SUCCESS) return;
    g_h.vk.QueueWaitIdle(g_h.queue);

    void *map = nullptr;
    if (g_h.vk.MapMemory(g_h.vkDevice, g_h.readbackMem, 0, KATWALK_HUD_BYTES, 0, &map) == VK_SUCCESS) {
        writePPM(hudDir() + "/hud-gpu.ppm", (const uint8_t *)map, KATWALK_HUD_W, KATWALK_HUD_H, g_h.bgra);
        g_h.vk.UnmapMemory(g_h.vkDevice, g_h.readbackMem);
        layer_log("HUD: wrote GPU readback dump hud-gpu.ppm");
    }
}

// Called from xrEndFrame: returns a quad layer to append this frame, or null to change nothing.
const XrCompositionLayerBaseHeader *prepareFrame(XrSession s, XrTime displayTime) {
    std::lock_guard<std::mutex> lk(g_hmtx);
    if (!g_h.vkOk || g_h.failed || s != g_h.session) return nullptr;
    if (!g_h.inited && !init(s)) { g_h.failed = true; return nullptr; }

    if ((g_cfgPoll++ % 90) == 0) loadCfg();   // live placement tuning (~1 Hz check)

    publishPoses(displayTime);   // HMD pose/yaw for the daemon - even while the panel is hidden

    // --- hand anchor + facing gate ---
    XrSpace grip = g_hand.gripSpace[g_cfg.hand ? 1 : 0];
    if (!g_hand.attached || grip == XR_NULL_HANDLE || !g.nextLocateSpace) return nullptr;
    XrSpaceLocation loc{XR_TYPE_SPACE_LOCATION};
    if (XR_FAILED(g.nextLocateSpace(grip, g_h.viewSpace, displayTime, &loc))) return nullptr;
    constexpr XrSpaceLocationFlags REQ =
        XR_SPACE_LOCATION_POSITION_VALID_BIT | XR_SPACE_LOCATION_ORIENTATION_VALID_BIT;
    bool tracked = (loc.locationFlags & REQ) == REQ;

    bool target = g_hand.visible;
    XrPosef panel{};
    if (tracked) {
        // panel pose = grip pose composed with the configured forearm offset (in VIEW space)
        XrQuaternionf qoff = qeulerXYZ(g_cfg.rotdeg[0], g_cfg.rotdeg[1], g_cfg.rotdeg[2]);
        panel.orientation = qmul(loc.pose.orientation, qoff);
        XrVector3f o = qrot(loc.pose.orientation, {g_cfg.off[0], g_cfg.off[1], g_cfg.off[2]});
        panel.position = {loc.pose.position.x + o.x, loc.pose.position.y + o.y, loc.pose.position.z + o.z};

        // facing gate: show when the panel's front (+Z) points at the face (VIEW-space origin)
        float dist = std::sqrt(panel.position.x*panel.position.x +
                               panel.position.y*panel.position.y +
                               panel.position.z*panel.position.z);
        XrVector3f n = qrot(panel.orientation, {0, 0, 1});
        float facing = 0.f;
        if (dist > 1e-4f)
            facing = -(n.x*panel.position.x + n.y*panel.position.y + n.z*panel.position.z) / dist;
        if      (facing > g_cfg.faceShow && dist < g_cfg.maxDist) target = true;
        else if (facing < g_cfg.faceHide || dist > g_cfg.maxDist + 0.10f) target = false;
        // between the thresholds: keep the current decision (hysteresis)
    }
    // a lost pose keeps the last decision; gate flips only after `hold` consistent frames
    if (target != g_hand.visible) {
        if (++g_hand.holdCnt >= g_cfg.hold) { g_hand.visible = target; g_hand.holdCnt = 0; }
    } else g_hand.holdCnt = 0;
    if (g_hand.dragging) { g_hand.visible = true; g_hand.holdCnt = 0; }  // never hide mid-drag

    // 1/s diagnostic: which branch is deciding, and with what numbers
    static uint32_t dbgN = 0;
    if ((dbgN++ % 90) == 0) {
        char m[192]; std::snprintf(m, sizeof m,
            "HUD: gate flags=0x%llx tracked=%d visible=%d always=%d pos=%.2f,%.2f,%.2f syncs/s=%u",
            (unsigned long long)loc.locationFlags, (int)tracked, (int)g_hand.visible, g_cfg.always,
            panel.position.x, panel.position.y, panel.position.z,
            g_syncCalls.exchange(0, std::memory_order_relaxed));
        layer_log(m);
    }

    if (!(g_hand.visible || g_cfg.always || g_hand.dragging) || (!tracked && !g_hand.dragging))
        return nullptr;

    // The gate above was computed head-relative (VIEW space). For SUBMISSION, express the same
    // panel pose in world space (LOCAL): reprojection then treats it like any world object.
    XrSpaceLocation locW{XR_TYPE_SPACE_LOCATION};
    bool anchorOk = XR_SUCCEEDED(g.nextLocateSpace(grip, g_h.localSpace, displayTime, &locW)) &&
                    (locW.locationFlags & REQ) == REQ;
    if (!anchorOk && !g_hand.dragging) return nullptr;
    XrQuaternionf qoffW = qeulerXYZ(g_cfg.rotdeg[0], g_cfg.rotdeg[1], g_cfg.rotdeg[2]);
    XrPosef panelW{};
    if (anchorOk) {
        panelW.orientation = qmul(locW.pose.orientation, qoffW);
        XrVector3f oW = qrot(locW.pose.orientation, {g_cfg.off[0], g_cfg.off[1], g_cfg.off[2]});
        panelW.position = {locW.pose.position.x + oW.x, locW.pose.position.y + oW.y,
                           locW.pose.position.z + oW.z};
    }
    if (g_hand.dragging) {  // panel rides the pointing hand while grip is held
        XrSpace aimD = g_hand.aimSpace[g_cfg.hand ? 0 : 1];
        XrSpaceLocation lad{XR_TYPE_SPACE_LOCATION};
        if (aimD != XR_NULL_HANDLE &&
            XR_SUCCEEDED(g.nextLocateSpace(aimD, g_h.localSpace, displayTime, &lad)) &&
            (lad.locationFlags & REQ) == REQ) {
            panelW.orientation = qmul(lad.pose.orientation, g_hand.dragOff.orientation);
            XrVector3f dp = qrot(lad.pose.orientation, g_hand.dragOff.position);
            panelW.position = {lad.pose.position.x + dp.x, lad.pose.position.y + dp.y,
                               lad.pose.position.z + dp.z};
        } else if (!anchorOk) return nullptr;  // neither hand located this frame
    }

    // Upload only when the content changes: once for the built-in test pattern, then whenever
    // the Python overlay publishes a new frame in katwalk/hud (seq change with a consistent
    // seq_start==seq_end snapshot). The overlay renders at most ~15 Hz and only on visual
    // change, so the acquire+copy+wait here is rare and bounded - never per-frame (that stalled
    // the game's render thread once before; see git history).
    if ((g_shmPoll++ % 90) == 0 && !g_hudShm) openHudShm();
    bool need = !g_h.uploaded;
    if (g_hudShm && g_h.stagingMap) {
        uint32_t s0 = g_hudShm->seq_start, s1 = g_hudShm->seq_end;
        if (s0 == s1 && s0 != g_hudSeq &&
            g_hudShm->w == KATWALK_HUD_W && g_hudShm->h == KATWALK_HUD_H) {
            std::memcpy(g_h.stagingMap, (const void *)g_hudPx, KATWALK_HUD_BYTES);
            if (g_hudShm->seq_start == s0 && g_hudShm->seq_end == s0) {  // still consistent
                if (g_h.bgra) for (size_t i = 0; i < KATWALK_HUD_BYTES; i += 4)
                    std::swap(g_h.stagingMap[i], g_h.stagingMap[i + 2]);
                VkMappedMemoryRange r{VK_STRUCTURE_TYPE_MAPPED_MEMORY_RANGE};
                r.memory = g_h.stagingMem; r.size = VK_WHOLE_SIZE;
                g_h.vk.FlushMappedRanges(g_h.vkDevice, 1, &r);
                g_hudSeq = s0;
                need = true;
            }
        }
    }
    if (need) {
        uint32_t idx = 0;
        XrSwapchainImageAcquireInfo ai{XR_TYPE_SWAPCHAIN_IMAGE_ACQUIRE_INFO};
        if (XR_FAILED(g.nextAcquireImage(g_h.swapchain, &ai, &idx))) {
            layer_log("HUD: acquireImage FAILED"); return nullptr;
        }
        XrSwapchainImageWaitInfo wi{XR_TYPE_SWAPCHAIN_IMAGE_WAIT_INFO};
        wi.timeout = XR_INFINITE_DURATION;
        if (XR_FAILED(g.nextWaitImage(g_h.swapchain, &wi))) {
            g_h.failed = true; layer_log("HUD: waitImage FAILED - HUD disabled"); return nullptr;
        }
        uploadTo(g_h.images[idx]);
        if (std::getenv("KATWALK_HUD_DUMP")) readbackDump(g_h.images[idx]);
        XrSwapchainImageReleaseInfo ri{XR_TYPE_SWAPCHAIN_IMAGE_RELEASE_INFO};
        g.nextReleaseImage(g_h.swapchain, &ri);
        g_h.uploaded = true;
    }

    // ---- laser: ray-cast the POINTING hand's aim ray against the panel plane (world space) ----
    do {
        XrSpace aim = g_hand.aimSpace[g_cfg.hand ? 0 : 1];   // opposite of the anchor hand
        if (aim == XR_NULL_HANDLE || !g.nextGetBool) break;
        XrSpaceLocation la{XR_TYPE_SPACE_LOCATION};
        if (XR_FAILED(g.nextLocateSpace(aim, g_h.localSpace, displayTime, &la)) ||
            (la.locationFlags & REQ) != REQ) break;
        // panel basis in world space
        XrVector3f ax = qrot(panelW.orientation, {1, 0, 0});
        XrVector3f ay = qrot(panelW.orientation, {0, 1, 0});
        XrVector3f an = qrot(panelW.orientation, {0, 0, 1});
        XrVector3f ro = la.pose.position;
        XrVector3f rd = qrot(la.pose.orientation, {0, 0, -1});   // aim ray = -Z
        XrVector3f rel{panelW.position.x - ro.x, panelW.position.y - ro.y, panelW.position.z - ro.z};
        float denom = rd.x*an.x + rd.y*an.y + rd.z*an.z;
        float px = -1.f, py = -1.f; uint32_t onPanel = 0;
        if (std::fabs(denom) > 1e-5f) {
            float t = (rel.x*an.x + rel.y*an.y + rel.z*an.z) / denom;
            if (t > 0.02f && t < 3.0f) {   // in front of the hand, within reach
                XrVector3f hit{ro.x + rd.x*t - panelW.position.x,
                               ro.y + rd.y*t - panelW.position.y,
                               ro.z + rd.z*t - panelW.position.z};
                float hw = g_cfg.width * 0.5f;
                float hh = hw * (float)KATWALK_HUD_H / (float)KATWALK_HUD_W;
                float u = hit.x*ax.x + hit.y*ax.y + hit.z*ax.z;    // meters along panel X
                float v = hit.x*ay.x + hit.y*ay.y + hit.z*ay.z;    // meters along panel Y (up)
                if (u >= -hw && u <= hw && v >= -hh && v <= hh) {
                    onPanel = 1;
                    px = (u / hw * 0.5f + 0.5f) * KATWALK_HUD_W;
                    py = (0.5f - v / hh * 0.5f) * KATWALK_HUD_H;   // image y grows downward
                }
            }
        }
        // buttons of the pointing hand (reading our own set consumes nothing from the game)
        auto getb = [&](XrAction a) {
            XrActionStateGetInfo gi{XR_TYPE_ACTION_STATE_GET_INFO};
            gi.action = a; gi.subactionPath = g_hand.hands[g_cfg.hand ? 0 : 1];
            XrActionStateBoolean st{XR_TYPE_ACTION_STATE_BOOLEAN};
            return XR_SUCCEEDED(g.nextGetBool(s, &gi, &st)) && st.isActive && st.currentState;
        };
        bool trig = getb(g_hand.trigBool), sqz = getb(g_hand.sqzBool);
        if (trig != g_hand.lastTrig)
            laserEvent(trig ? KATWALK_LASER_DOWN : KATWALK_LASER_UP, KATWALK_BTN_TRIGGER, px, py, onPanel);
        else if (sqz != g_hand.lastSqz)
            laserEvent(sqz ? KATWALK_LASER_DOWN : KATWALK_LASER_UP, KATWALK_BTN_GRIP, px, py, onPanel);
        else if (onPanel)
            laserEvent(KATWALK_LASER_MOVE, KATWALK_BTN_TRIGGER, px, py, onPanel);

        // ---- drag-to-move: hold GRIP while pointing at the panel; release to save ----
        if (sqz && !g_hand.lastSqz && onPanel && !g_hand.dragging && !g_cfg.locked) {
            // capture the panel pose relative to the pointing hand: panel = aim o dragOff
            XrQuaternionf qi = qconj(la.pose.orientation);
            XrVector3f d{panelW.position.x - la.pose.position.x,
                         panelW.position.y - la.pose.position.y,
                         panelW.position.z - la.pose.position.z};
            g_hand.dragOff.orientation = qmul(qi, panelW.orientation);
            g_hand.dragOff.position = qrot(qi, d);
            g_hand.dragging = true;
            layer_log("HUD: drag start");
        } else if (!sqz && g_hand.lastSqz && g_hand.dragging) {
            g_hand.dragging = false;
            if (anchorOk) {  // re-anchor: panel pose relative to the wrist, persisted to conf
                XrQuaternionf qi = qconj(locW.pose.orientation);
                XrVector3f d{panelW.position.x - locW.pose.position.x,
                             panelW.position.y - locW.pose.position.y,
                             panelW.position.z - locW.pose.position.z};
                XrVector3f off = qrot(qi, d);
                float rx, ry, rz;
                eulerXYZFromQuat(qmul(qi, panelW.orientation), rx, ry, rz);
                writeConfPose(off, rx, ry, rz);
            } else layer_log("HUD: drag end but wrist not tracked - placement NOT saved");
        }
        g_hand.lastTrig = trig; g_hand.lastSqz = sqz;
        static uint32_t ldbg = 0;   // 1/s: is the ray/button pipeline alive at all?
        if ((ldbg++ % 90) == 0) {
            char m[128]; std::snprintf(m, sizeof m,
                "HUD: laser aim_ok=1 on_panel=%u px=%.0f,%.0f trig=%d sqz=%d seq=%u",
                onPanel, px, py, (int)trig, (int)sqz, g_laserSeq);
            layer_log(m);
        }
    } while (false);

    g_h.quad = XrCompositionLayerQuad{XR_TYPE_COMPOSITION_LAYER_QUAD};
    g_h.quad.layerFlags = XR_COMPOSITION_LAYER_BLEND_TEXTURE_SOURCE_ALPHA_BIT;
    g_h.quad.space = g_h.localSpace;            // world space (see localSpace comment)
    g_h.quad.eyeVisibility = XR_EYE_VISIBILITY_BOTH;
    g_h.quad.subImage.swapchain = g_h.swapchain;
    g_h.quad.subImage.imageRect.offset = {0, 0};
    g_h.quad.subImage.imageRect.extent = {KATWALK_HUD_W, KATWALK_HUD_H};
    g_h.quad.subImage.imageArrayIndex = 0;
    g_h.quad.pose = panelW;                     // world-space panel pose riding the grip
    g_h.quad.size = {g_cfg.width, g_cfg.width * (float)KATWALK_HUD_H / (float)KATWALK_HUD_W};

    static uint64_t submitted = 0;
    if (submitted == 0) {
        char m[160]; std::snprintf(m, sizeof m,
            "HUD: FIRST quad returned (space=VIEW pos %.2f %.2f %.2f size %.2fx%.2f m)",
            g_h.quad.pose.position.x, g_h.quad.pose.position.y, g_h.quad.pose.position.z,
            g_h.quad.size.width, g_h.quad.size.height);
        layer_log(m);
    }
    if ((++submitted % 600) == 0) {  // ~ every 5-10 s: confirms it keeps submitting
        char m[64]; std::snprintf(m, sizeof m, "HUD: %llu quads submitted so far",
                                  (unsigned long long)submitted);
        layer_log(m);
    }
    return (const XrCompositionLayerBaseHeader *)&g_h.quad;
}

void destroy() {
    std::lock_guard<std::mutex> lk(g_hmtx);
    if (g_h.queue && g_h.vk.QueueWaitIdle) g_h.vk.QueueWaitIdle(g_h.queue);
    if (g_h.swapchain && g.nextDestroySwapchain) g.nextDestroySwapchain(g_h.swapchain);
    if (g_h.viewSpace && g.nextDestroySpace) g.nextDestroySpace(g_h.viewSpace);
    if (g_h.localSpace && g.nextDestroySpace) g.nextDestroySpace(g_h.localSpace);
    // Vulkan objects belong to the app's device; we let process teardown reclaim them rather
    // than risk destroying with a partially-loaded fn table. Reset our state for a clean re-init.
    if (g_h.vkLib) dlclose(g_h.vkLib);
    g_h = State{};
    // grip spaces + attachment die with the session; the action set is instance-level and stays
    g_hand.gripSpace[0] = g_hand.gripSpace[1] = XR_NULL_HANDLE;
    g_hand.attached = false;
    g_hand.visible = false;
    g_hand.holdCnt = 0;
}

} // namespace hud

// ------------------------------- hooked functions -------------------------------

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrCreateSession(
        XrInstance instance, const XrSessionCreateInfo *ci, XrSession *session) {
    XrResult r = g.nextCreateSession ? g.nextCreateSession(instance, ci, session)
                                     : XR_ERROR_FUNCTION_UNSUPPORTED;
    if (XR_SUCCEEDED(r) && session) hud::onSessionCreated(*session, ci);
    return r;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrEndFrame(
        XrSession session, const XrFrameEndInfo *frameEndInfo) {
    const XrCompositionLayerBaseHeader *quad =
        frameEndInfo ? hud::prepareFrame(session, frameEndInfo->displayTime) : nullptr;
    static uint32_t dbgN = 0;
    bool dbg = (dbgN++ % 90) == 0;   // 1/s: what the app submits + whether we appended
    if (quad && frameEndInfo && g.nextEndFrame) {
        std::vector<const XrCompositionLayerBaseHeader *> layers(
            frameEndInfo->layers, frameEndInfo->layers + frameEndInfo->layerCount);
        layers.push_back(quad);
        XrFrameEndInfo ni = *frameEndInfo;
        ni.layerCount = (uint32_t)layers.size();
        ni.layers = layers.data();
        XrResult res = g.nextEndFrame(session, &ni);
        if (dbg) {
            char m[128]; std::snprintf(m, sizeof m,
                "HUD: endFrame app_layers=%u type0=%d + quad -> res=%d",
                frameEndInfo->layerCount,
                frameEndInfo->layerCount ? (int)frameEndInfo->layers[0]->type : -1, (int)res);
            layer_log(m);
        }
        return res;
    }
    if (dbg && frameEndInfo) {
        char m[128]; std::snprintf(m, sizeof m,
            "HUD: endFrame app_layers=%u NO quad appended", frameEndInfo->layerCount);
        layer_log(m);
    }
    return g.nextEndFrame ? g.nextEndFrame(session, frameEndInfo) : XR_ERROR_FUNCTION_UNSUPPORTED;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrDestroySession(XrSession session) {
    hud::destroy();
    return g.nextDestroySession ? g.nextDestroySession(session) : XR_SUCCESS;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrSuggestInteractionProfileBindings(
        XrInstance instance, const XrInteractionProfileSuggestedBinding *sb) {
    if (sb && g.nextPathToString) {
        for (uint32_t i = 0; i < sb->countSuggestedBindings; ++i) {
            XrAction a = sb->suggestedBindings[i].action;
            XrPath   p = sb->suggestedBindings[i].binding;
            char buf[XR_MAX_PATH_LENGTH]; uint32_t len = 0;
            if (g.nextPathToString(instance, p, sizeof(buf), &len, buf) == XR_SUCCESS) {
                std::string s(buf);
                if (s.find("/input/thumbstick") != std::string::npos ||
                    s.find("/input/trackpad")  != std::string::npos) {
                    std::lock_guard<std::mutex> lk(g_mtx);
                    if      (s.rfind("/user/hand/left",  0) == 0) { g_leftThumb.insert(a);  layer_log("bound LEFT  thumb: " + s); }
                    else if (s.rfind("/user/hand/right", 0) == 0) { g_rightThumb.insert(a); layer_log("bound RIGHT thumb: " + s); }
                }
            }
        }
    }
    if (!g.nextSuggest) return XR_ERROR_FUNCTION_UNSUPPORTED;
    // HUD: piggyback our grip-pose bindings onto the app's suggestion for this profile
    if (g_hudEnabled && sb) return hud::suggestWithOurs(instance, sb);
    return g.nextSuggest(instance, sb);
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrAttachSessionActionSets(
        XrSession session, const XrSessionActionSetsAttachInfo *ai) {
    if (g_hudEnabled && ai) return hud::attachWithOurs(session, ai);
    return g.nextAttachActionSets ? g.nextAttachActionSets(session, ai)
                                  : XR_ERROR_FUNCTION_UNSUPPORTED;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrSyncActions(
        XrSession session, const XrActionsSyncInfo *si) {
    if (g_hudEnabled && si) return hud::syncWithOurs(session, si);
    return g.nextSyncActions ? g.nextSyncActions(session, si)
                             : XR_ERROR_FUNCTION_UNSUPPORTED;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrGetActionStateVector2f(
        XrSession session, const XrActionStateGetInfo *gi, XrActionStateVector2f *state) {
    XrResult r = g.nextGetV2f ? g.nextGetV2f(session, gi, state)
                              : XR_ERROR_FUNCTION_UNSUPPORTED;
    if (XR_FAILED(r) || !gi || !state) return r;

    bool isLeft;
    {
        std::lock_guard<std::mutex> lk(g_mtx);
        isLeft = g_leftThumb.count(gi->action) > 0;
    }
    if (!isLeft) return r;                                   // only the movement stick
    if (gi->subactionPath != XR_NULL_PATH &&
        gi->subactionPath == g.rightHand) return r;          // right-hand read = turning

    float wx, wy;
    if (readWalk(wx, wy)) {
        if (std::fabs(wx) > std::fabs(state->currentState.x)) state->currentState.x = wx;
        if (std::fabs(wy) > std::fabs(state->currentState.y)) state->currentState.y = wy;
        state->isActive             = XR_TRUE;
        state->changedSinceLastSync = XR_TRUE;
        static bool first = true;
        if (first) { first = false; layer_log("INJECTING walk vector into left thumbstick"); }
    }
    return r;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrDestroyInstance(XrInstance instance) {
    PFN_xrDestroyInstance n = g.nextDestroyInstance;
    {
        std::lock_guard<std::mutex> lk(g_mtx);
        g_leftThumb.clear();
        g_rightThumb.clear();
    }
    hud::g_hand = hud::HandState{};  // action set/action/spaces die with the instance
    if (g_map) { munmap(const_cast<KatInput *>(g_map), sizeof(KatInput)); g_map = nullptr; }
    if (g_fd >= 0) { ::close(g_fd); g_fd = -1; }
    return n ? n(instance) : XR_SUCCESS;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrPollEvent(
        XrInstance instance, XrEventDataBuffer *ev) {
    XrResult r = g.nextPollEvent ? g.nextPollEvent(instance, ev)
                                 : XR_ERROR_FUNCTION_UNSUPPORTED;
    if (r == XR_SUCCESS && ev &&
        ev->type == XR_TYPE_EVENT_DATA_REFERENCE_SPACE_CHANGE_PENDING) {
        requestRecenter();
    }
    return r;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrGetInstanceProcAddr(
        XrInstance instance, const char *name, PFN_xrVoidFunction *function) {
    if (name && function) {
        if (std::strcmp(name, "xrGetActionStateVector2f") == 0) {
            *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrGetActionStateVector2f);
            return XR_SUCCESS;
        }
        if (std::strcmp(name, "xrSuggestInteractionProfileBindings") == 0) {
            *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrSuggestInteractionProfileBindings);
            return XR_SUCCESS;
        }
        if (std::strcmp(name, "xrDestroyInstance") == 0) {
            *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrDestroyInstance);
            return XR_SUCCESS;
        }
        if (std::strcmp(name, "xrPollEvent") == 0) {
            *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrPollEvent);
            return XR_SUCCESS;
        }
        if (g_hudEnabled) {  // HUD hooks only when enabled; otherwise pure pass-through
            if (std::strcmp(name, "xrCreateSession") == 0) {
                *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrCreateSession);
                return XR_SUCCESS;
            }
            if (std::strcmp(name, "xrEndFrame") == 0) {
                *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrEndFrame);
                return XR_SUCCESS;
            }
            if (std::strcmp(name, "xrDestroySession") == 0) {
                *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrDestroySession);
                return XR_SUCCESS;
            }
            if (std::strcmp(name, "xrAttachSessionActionSets") == 0) {
                *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrAttachSessionActionSets);
                return XR_SUCCESS;
            }
            if (std::strcmp(name, "xrSyncActions") == 0) {
                *function = reinterpret_cast<PFN_xrVoidFunction>(katwalk_xrSyncActions);
                return XR_SUCCESS;
            }
        }
    }
    return g.nextGIPA ? g.nextGIPA(instance, name, function)
                      : XR_ERROR_FUNCTION_UNSUPPORTED;
}

XRAPI_ATTR XrResult XRAPI_CALL katwalk_xrCreateApiLayerInstance(
        const XrInstanceCreateInfo *info, const XrApiLayerCreateInfo *layerInfo,
        XrInstance *instance) {
    XrApiLayerNextInfo *nextInfo = layerInfo->nextInfo;
    if (!nextInfo) return XR_ERROR_INITIALIZATION_FAILED;

    // create the instance further down the chain
    XrApiLayerCreateInfo nextLayerInfo = *layerInfo;
    nextLayerInfo.nextInfo = nextInfo->next;
    XrResult r = nextInfo->nextCreateApiLayerInstance(info, &nextLayerInfo, instance);
    if (XR_FAILED(r)) return r;

    // build our dispatch from the next layer/runtime's resolver
    g.nextGIPA = nextInfo->nextGetInstanceProcAddr;
    auto load = [&](const char *n, PFN_xrVoidFunction *fn) { g.nextGIPA(*instance, n, fn); };
    load("xrGetActionStateVector2f",            reinterpret_cast<PFN_xrVoidFunction *>(&g.nextGetV2f));
    load("xrSuggestInteractionProfileBindings", reinterpret_cast<PFN_xrVoidFunction *>(&g.nextSuggest));
    load("xrPathToString",                      reinterpret_cast<PFN_xrVoidFunction *>(&g.nextPathToString));
    load("xrStringToPath",                      reinterpret_cast<PFN_xrVoidFunction *>(&g.nextStringToPath));
    load("xrDestroyInstance",                   reinterpret_cast<PFN_xrVoidFunction *>(&g.nextDestroyInstance));
    load("xrPollEvent",                         reinterpret_cast<PFN_xrVoidFunction *>(&g.nextPollEvent));
    // HUD composition-layer path
    load("xrCreateSession",                     reinterpret_cast<PFN_xrVoidFunction *>(&g.nextCreateSession));
    load("xrDestroySession",                    reinterpret_cast<PFN_xrVoidFunction *>(&g.nextDestroySession));
    load("xrEndFrame",                          reinterpret_cast<PFN_xrVoidFunction *>(&g.nextEndFrame));
    load("xrCreateReferenceSpace",              reinterpret_cast<PFN_xrVoidFunction *>(&g.nextCreateRefSpace));
    load("xrDestroySpace",                      reinterpret_cast<PFN_xrVoidFunction *>(&g.nextDestroySpace));
    load("xrEnumerateSwapchainFormats",         reinterpret_cast<PFN_xrVoidFunction *>(&g.nextEnumFormats));
    load("xrCreateSwapchain",                   reinterpret_cast<PFN_xrVoidFunction *>(&g.nextCreateSwapchain));
    load("xrDestroySwapchain",                  reinterpret_cast<PFN_xrVoidFunction *>(&g.nextDestroySwapchain));
    load("xrEnumerateSwapchainImages",          reinterpret_cast<PFN_xrVoidFunction *>(&g.nextEnumSwImages));
    load("xrAcquireSwapchainImage",             reinterpret_cast<PFN_xrVoidFunction *>(&g.nextAcquireImage));
    load("xrWaitSwapchainImage",                reinterpret_cast<PFN_xrVoidFunction *>(&g.nextWaitImage));
    load("xrReleaseSwapchainImage",             reinterpret_cast<PFN_xrVoidFunction *>(&g.nextReleaseImage));
    // hand anchor
    load("xrCreateActionSet",                   reinterpret_cast<PFN_xrVoidFunction *>(&g.nextCreateActionSet));
    load("xrCreateAction",                      reinterpret_cast<PFN_xrVoidFunction *>(&g.nextCreateAction));
    load("xrAttachSessionActionSets",           reinterpret_cast<PFN_xrVoidFunction *>(&g.nextAttachActionSets));
    load("xrSyncActions",                       reinterpret_cast<PFN_xrVoidFunction *>(&g.nextSyncActions));
    load("xrCreateActionSpace",                 reinterpret_cast<PFN_xrVoidFunction *>(&g.nextCreateActionSpace));
    load("xrLocateSpace",                       reinterpret_cast<PFN_xrVoidFunction *>(&g.nextLocateSpace));
    load("xrGetActionStateBoolean",             reinterpret_cast<PFN_xrVoidFunction *>(&g.nextGetBool));

    if (g.nextStringToPath)
        g.nextStringToPath(*instance, "/user/hand/right", &g.rightHand);

    g.instance = *instance;
    g_hudEnabled = (std::getenv("KATWALK_NO_HUD") == nullptr);
    layer_log(std::string("layer attached v") + KATWALK_VERSION +
              (g_hudEnabled ? " (locomotion + wrist HUD)"
                            : " (locomotion only, HUD off via KATWALK_NO_HUD)"));
    return XR_SUCCESS;
}

} // namespace

KAT_EXPORT XRAPI_ATTR XrResult XRAPI_CALL xrNegotiateLoaderApiLayerInterface(
        const XrNegotiateLoaderInfo *loaderInfo, const char * /*layerName*/,
        XrNegotiateApiLayerRequest *apiLayerRequest) {
    if (!loaderInfo || !apiLayerRequest) return XR_ERROR_INITIALIZATION_FAILED;
    if (loaderInfo->structType != XR_LOADER_INTERFACE_STRUCT_LOADER_INFO ||
        apiLayerRequest->structType != XR_LOADER_INTERFACE_STRUCT_API_LAYER_REQUEST)
        return XR_ERROR_INITIALIZATION_FAILED;

    // Advertise an API version inside the loader's supported range. Proton ships an
    // older loader (e.g. 1.1.36) than our headers (1.1.60); declaring a version above
    // loaderInfo->maxApiVersion makes the loader silently drop the layer.
    XrVersion api = XR_CURRENT_API_VERSION;
    if (api > loaderInfo->maxApiVersion) api = loaderInfo->maxApiVersion;
    if (api < loaderInfo->minApiVersion) api = loaderInfo->minApiVersion;

    apiLayerRequest->layerInterfaceVersion  = XR_CURRENT_LOADER_API_LAYER_VERSION;
    apiLayerRequest->layerApiVersion        = api;
    apiLayerRequest->getInstanceProcAddr    = katwalk_xrGetInstanceProcAddr;
    apiLayerRequest->createApiLayerInstance = katwalk_xrCreateApiLayerInstance;
    return XR_SUCCESS;
}
