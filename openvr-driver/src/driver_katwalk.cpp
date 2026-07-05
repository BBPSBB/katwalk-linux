// katwalk-linux SteamVR driver - a virtual device with role = Treadmill that exposes a
// joystick (+ buttons) via IVRDriverInput. SteamVR combines it with the real
// controllers by "greatest absolute value", so it drives locomotion when you walk
// and the real sticks still work when you don't.
//
// Live input comes from katwalkd over a tiny shared-memory file:
//   $XDG_RUNTIME_DIR/katwalk/input  ->  struct { float x, y; uint32 buttons, seq; }
//   buttons bit0 = sprint, bit1 = jump.   (see katwalk/io/driverlink.py)
//
// Build: make   (needs a C++ toolchain)  -> katwalk/bin/linux64/driver_katwalk.so

#include <openvr_driver.h>

#include <cstdint>
#include <cstring>
#include <cstdlib>
#include <string>

#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

using namespace vr;

#if defined(_WIN32)
#define KAT_EXPORT extern "C" __declspec(dllexport)
#else
#define KAT_EXPORT extern "C" __attribute__((visibility("default")))
#endif

struct KatInput { float x; float y; uint32_t buttons; uint32_t seq; };

static std::string input_path() {
    const char *xdg = std::getenv("XDG_RUNTIME_DIR");
    std::string base = (xdg && *xdg) ? xdg : "/tmp";
    return base + "/katwalk/input";
}

class TreadmillDevice : public ITrackedDeviceServerDriver {
public:
    const char *serial() const { return "katwalk-linux-Treadmill"; }

    EVRInitError Activate(uint32_t unObjectId) override {
        m_id = unObjectId;
        PropertyContainerHandle_t c = VRProperties()->TrackedDeviceToPropertyContainer(m_id);
        VRProperties()->SetStringProperty(c, Prop_ModelNumber_String, "katwalk-linux C2+");
        VRProperties()->SetStringProperty(c, Prop_SerialNumber_String, serial());
        VRProperties()->SetStringProperty(c, Prop_ManufacturerName_String, "katwalk-linux");
        VRProperties()->SetInt32Property(c, Prop_ControllerRoleHint_Int32, TrackedControllerRole_Treadmill);
        VRProperties()->SetStringProperty(c, Prop_ControllerType_String, "katwalk_treadmill");
        VRProperties()->SetStringProperty(c, Prop_InputProfilePath_String,
                                          "{katwalk}/input/katwalk_treadmill_profile.json");
        VRProperties()->SetBoolProperty(c, Prop_NeverTracked_Bool, true);

        VRDriverInput()->CreateScalarComponent(c, "/input/joystick/x", &m_jx,
            VRScalarType_Absolute, VRScalarUnits_NormalizedTwoSided);
        VRDriverInput()->CreateScalarComponent(c, "/input/joystick/y", &m_jy,
            VRScalarType_Absolute, VRScalarUnits_NormalizedTwoSided);
        VRDriverInput()->CreateScalarComponent(c, "/input/trackpad/x", &m_tx,
            VRScalarType_Absolute, VRScalarUnits_NormalizedTwoSided);
        VRDriverInput()->CreateScalarComponent(c, "/input/trackpad/y", &m_ty,
            VRScalarType_Absolute, VRScalarUnits_NormalizedTwoSided);
        // trackpad "touch": some (older, Vive-era) games gate locomotion on the pad
        // being touched. We assert it while walking so those games accept movement too.
        VRDriverInput()->CreateBooleanComponent(c, "/input/trackpad/touch", &m_ttouch);
        VRDriverInput()->CreateBooleanComponent(c, "/input/joystick/click", &m_jclick);
        VRDriverInput()->CreateBooleanComponent(c, "/input/trigger/click", &m_trig);
        VRDriverInput()->CreateBooleanComponent(c, "/input/a/click", &m_a);

        open_shared();
        return VRInitError_None;
    }

    void Deactivate() override { close_shared(); m_id = k_unTrackedDeviceIndexInvalid; }
    void EnterStandby() override {}
    void *GetComponent(const char *) override { return nullptr; }
    void DebugRequest(const char *, char *buf, uint32_t n) override { if (n) buf[0] = '\0'; }

    DriverPose_t GetPose() override {
        DriverPose_t p = {};
        // A treadmill has NO spatial pose - it's an input-only device. poseIsValid=false keeps
        // it from being rendered anywhere (otherwise the controller-class device drew its
        // default Steam Controller model at the origin / the user's feet). deviceIsConnected
        // stays true and we keep pushing this every frame in RunFrame, so the joystick input
        // stays live; it's just not drawn. (KAT's KAT_Treadmill device is invisible the same way.)
        p.poseIsValid = false;
        p.result = TrackingResult_Running_OK;
        p.deviceIsConnected = true;
        // all three quaternions MUST be valid units - a zero qRotation is a non-unit quaternion
        // and produces a NaN device transform that crashes the compositor/Home renderer.
        p.qWorldFromDriverRotation.w = 1.0;
        p.qDriverFromHeadRotation.w = 1.0;
        p.qRotation.w = 1.0;
        return p;
    }

    void RunFrame() {
        if (m_id == k_unTrackedDeviceIndexInvalid) return;
        // Push a pose every frame so SteamVR keeps the device ACTIVE. Without this it
        // drops to standby ("idle, like a controller on the table") and ignores our
        // input axes - which is why the joystick was being read as zero.
        VRServerDriverHost()->TrackedDevicePoseUpdated(m_id, GetPose(), sizeof(DriverPose_t));
        if (!m_map) open_shared();
        float x = 0.f, y = 0.f; uint32_t btn = 0;
        if (m_map) { x = m_map->x; y = m_map->y; btn = m_map->buttons; }
        x = x < -1.f ? -1.f : (x > 1.f ? 1.f : x);
        y = y < -1.f ? -1.f : (y > 1.f ? 1.f : y);
        VRDriverInput()->UpdateScalarComponent(m_jx, x, 0.0);
        VRDriverInput()->UpdateScalarComponent(m_jy, y, 0.0);
        VRDriverInput()->UpdateScalarComponent(m_tx, x, 0.0);
        VRDriverInput()->UpdateScalarComponent(m_ty, y, 0.0);
        // "finger on the pad" while there's any deflection (i.e. while walking).
        VRDriverInput()->UpdateBooleanComponent(m_ttouch, (x * x + y * y) > 1e-4f, 0.0);
        bool sprint = btn & 1u, jump = btn & 2u;
        VRDriverInput()->UpdateBooleanComponent(m_jclick, sprint, 0.0);
        VRDriverInput()->UpdateBooleanComponent(m_trig, sprint, 0.0);
        VRDriverInput()->UpdateBooleanComponent(m_a, jump, 0.0);
    }

private:
    void open_shared() {
        std::string p = input_path();
        m_fd = ::open(p.c_str(), O_RDONLY);
        if (m_fd >= 0) {
            void *m = mmap(nullptr, sizeof(KatInput), PROT_READ, MAP_SHARED, m_fd, 0);
            m_map = (m == MAP_FAILED) ? nullptr : static_cast<volatile KatInput *>(m);
        }
    }
    void close_shared() {
        if (m_map) { munmap(const_cast<KatInput *>(m_map), sizeof(KatInput)); m_map = nullptr; }
        if (m_fd >= 0) { ::close(m_fd); m_fd = -1; }
    }

    uint32_t m_id = k_unTrackedDeviceIndexInvalid;
    VRInputComponentHandle_t m_jx = 0, m_jy = 0, m_tx = 0, m_ty = 0, m_ttouch = 0, m_jclick = 0, m_trig = 0, m_a = 0;
    int m_fd = -1;
    volatile KatInput *m_map = nullptr;
};

class ServerProvider : public IServerTrackedDeviceProvider {
public:
    EVRInitError Init(IVRDriverContext *ctx) override {
        VR_INIT_SERVER_DRIVER_CONTEXT(ctx);
        VRServerDriverHost()->TrackedDeviceAdded(
            m_dev.serial(), TrackedDeviceClass_Controller, &m_dev);
        return VRInitError_None;
    }
    void Cleanup() override { VR_CLEANUP_SERVER_DRIVER_CONTEXT(); }
    const char *const *GetInterfaceVersions() override { return k_InterfaceVersions; }
    void RunFrame() override { m_dev.RunFrame(); }
    bool ShouldBlockStandbyMode() override { return false; }
    void EnterStandby() override {}
    void LeaveStandby() override {}

private:
    TreadmillDevice m_dev;
};

static ServerProvider g_provider;

KAT_EXPORT void *HmdDriverFactory(const char *pInterfaceName, int *pReturnCode) {
    if (pInterfaceName && std::strcmp(pInterfaceName, IServerTrackedDeviceProvider_Version) == 0)
        return &g_provider;
    if (pReturnCode) *pReturnCode = VRInitError_Init_InterfaceNotFound;
    return nullptr;
}
