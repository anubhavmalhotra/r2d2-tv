#pragma once
// No native provider is started by loading this library. No paths or network
// sockets are opened. The qualified provider transfers one inherited socketpair
// endpoint to one Session; the other endpoint belongs to its verified owner.
#include <cstddef>
#include <cstdint>
#include <functional>
#include <memory>
#include <string>
#include <sys/types.h>

namespace r2d2::local_capture {
inline constexpr char kBackend[] = "lg-ble-msbc-fe-first-s16le-16000-mono-v1";
inline constexpr char kProviderSha256[] = "2b9f9e318fba385801e160349159cb3238d1fc370136a385413168170879e28b";
inline constexpr char kCaller[] = "com.webos.starfish.keyfilters";
inline constexpr std::size_t kMaxFrameBytes = 8192;
inline constexpr std::uint32_t kMaxUtteranceBytes = 320000;
inline constexpr std::uint32_t kMaxLeaseMs = 10000;

enum class Phase { Idle, Reserved, Starting, Capturing, Retiring, Stopped, Failed };
enum class Error { None, Profile, Device, Peer, Descriptor, State, StartFailed,
    Format, Limit, Deadline, Backpressure, Transport, Cancelled, StopFailed };

struct Profile {
    // No default profile. The outer exact-build hook must independently verify
    // its loaded image, selected physical device and actual BLE FE call site.
    std::string backend;
    std::string provider_sha256;
    std::string selected_device;
    pid_t peer_pid = -1;
    uid_t peer_uid = static_cast<uid_t>(-1);
    std::uint32_t lease_ms = 0;
    bool defer_device_selection = false; // Explicit inventory-before-reserve mode only.
    bool ui_capture = false; // Separate owner category; physical mode remains the default.
};
struct Status {
    Phase phase = Phase::Idle;
    Error error = Error::None;
    std::uint32_t bytes = 0;       // Actual bytes accepted by the private socket.
    std::uint32_t frames = 0;      // Complete write buffers; early callbacks may coalesce.
    bool start_attempted = false;
    bool start_confirmed = false;
    bool stop_attempted = false;
    bool stop_confirmed = false;  // Original native STOP returned true.
    bool transport_closed = false;
    bool complete = false;        // EOF alone is never a successful utterance.
};
struct NativeOps {
    // These call the original functions with their original provider objects.
    // begin() is invoked only by the intercepted native startStreaming path;
    // the Node controller must not issue an additional hardware START.
    std::function<bool()> start;
    std::function<bool()> stop;
};
using Send = std::function<ssize_t(int, const void*, std::size_t, int)>;

class Session final {
public:
    // Transfers fd ownership even on rejection (valid descriptors >=3 only).
    // send_override is a dependency seam for compiled host tests, not JSON/UI.
    Session(int inherited_fd, Profile profile, NativeOps native, Send send_override = {});
    ~Session();
    Session(const Session&) = delete;
    Session& operator=(const Session&) = delete;
    bool ready() const;
    bool reserve(const std::string& caller, const std::string& device);
    bool begin(const std::string& device);
    bool pcm(const std::string& device, const void* first, const void* second, int bytes);
    void release();
    void cancel();
    void parent_lost();
    Status status() const;
private:
    struct Impl;
    std::unique_ptr<Impl> impl_;
};
} // namespace r2d2::local_capture
