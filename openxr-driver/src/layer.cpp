// katwalk-linux OpenXR API layer - injects treadmill locomotion into ANY OpenXR game.
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
// Input comes from katwalkd via the same shared-memory file the OpenVR driver reads:
//   $XDG_RUNTIME_DIR/katwalk/input  ->  struct { float x, y; uint32 buttons, seq; }
//
// Safe by construction: if the treadmill isn't running the shm file is absent, readWalk()
// returns false, and every call is a pure pass-through. Set DISABLE_KATWALK_XR_LAYER=1 to
// turn the layer off entirely (also honored by the loader via the manifest).
//
// Build: make   (needs a C++ toolchain)  ->  bin/libkatwalk_xr_layer.so

#include <openxr.h>
#include <openxr_loader_negotiation.h>

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <cstdlib>
#include <mutex>
#include <set>
#include <string>

#include <fcntl.h>
#include <sys/mman.h>
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
        char p_xdg[256] = {0}, p_run[64] = {0};
        const char *xdg = std::getenv("XDG_RUNTIME_DIR");
        if (xdg && *xdg) std::snprintf(p_xdg, sizeof(p_xdg), "%s/katwalk/xr-layer.log", xdg);
        std::snprintf(p_run, sizeof(p_run), "/run/user/%u/katwalk/xr-layer.log", (unsigned)getuid());
        const char *cands[] = { p_xdg[0] ? p_xdg : nullptr, p_run, "/tmp/katwalk-xr-layer.log" };
        for (const char *c : cands) { if (c && !f) f = std::fopen(c, "a"); }
    }
    if (f) { std::fprintf(f, "%s\n", msg.c_str()); std::fflush(f); }
}

// ---- shared-memory input (written by katwalkd; same struct as the OpenVR driver) ----
struct KatInput { float x; float y; uint32_t buttons; uint32_t seq; };

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
    XrPath rightHand = XR_NULL_PATH;
};
Dispatch              g;
std::mutex            g_mtx;
std::set<XrAction>    g_leftThumb;    // actions bound to the LEFT hand thumbstick/trackpad
std::set<XrAction>    g_rightThumb;   // ... and the RIGHT (so we never inject into turning)

// Ask the daemon (native; shares /tmp with the sandbox) to recenter walk-forward. Fired
// when the runtime recenters (e.g. the Quest Meta-button hold emits a reference-space
// change), so locomotion-forward re-aligns with the new view-forward - no extra gesture.
void requestRecenter() {
    int fd = ::open("/tmp/katwalk/recenter", O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd >= 0) { ssize_t n = ::write(fd, "1", 1); (void)n; ::close(fd); }
    layer_log("recenter requested (OpenXR reference space changed)");
}

// ------------------------------- hooked functions -------------------------------

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
    return g.nextSuggest ? g.nextSuggest(instance, sb) : XR_ERROR_FUNCTION_UNSUPPORTED;
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

    if (g.nextStringToPath)
        g.nextStringToPath(*instance, "/user/hand/right", &g.rightHand);

    layer_log("layer attached to OpenXR instance");
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
