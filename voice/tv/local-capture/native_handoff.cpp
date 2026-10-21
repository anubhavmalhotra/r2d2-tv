#include "native_handoff.hpp"
#include "ui_authority.hpp"
#include <array>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <fcntl.h>
#include <limits>
#include <mutex>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <thread>
#include <unistd.h>

namespace r2d2::local_capture {
namespace {
using Clock = std::chrono::steady_clock;
void erase(void* ptr, std::size_t n) {
    auto* p = static_cast<volatile unsigned char*>(ptr);
    while (n--) *p++ = 0;
}
bool device_name(const std::string& s) {
    if (s.size() < 7 || s.size() > 31 || s.compare(0, 6, "remote")) return false;
    for (std::size_t i = 6; i < s.size(); ++i) if (s[i] < '0' || s[i] > '9') return false;
    return true;
}
bool peer(int fd, pid_t pid, uid_t uid) {
    if (pid <= 1 || uid == static_cast<uid_t>(-1)) return false;
#ifdef __linux__
    ucred credentials{};
    socklen_t size = sizeof(credentials);
    return !getsockopt(fd, SOL_SOCKET, SO_PEERCRED, &credentials, &size) &&
        size == sizeof(credentials) && credentials.pid == pid && credentials.uid == uid;
#elif defined(__APPLE__)
    uid_t actual_uid = static_cast<uid_t>(-1); gid_t actual_gid{};
    pid_t actual_pid = -1; socklen_t size = sizeof(actual_pid);
    return !getpeereid(fd, &actual_uid, &actual_gid) &&
        !getsockopt(fd, SOL_LOCAL, LOCAL_PEERPID, &actual_pid, &size) &&
        size == sizeof(actual_pid) && actual_pid == pid && actual_uid == uid;
#else
    (void)fd; return false; // No identity downgrade on an unsupported host.
#endif
}
bool unnamed(const sockaddr_un& address, socklen_t size) {
    if (size < offsetof(sockaddr_un, sun_path) || size > sizeof(address)) return false;
    for (std::size_t i = 0; i < size - offsetof(sockaddr_un, sun_path); ++i)
        if (address.sun_path[i] != 0) return false;
    return true;
}
int send_flags() {
#ifdef MSG_NOSIGNAL
    return MSG_DONTWAIT | MSG_NOSIGNAL;
#else
    return MSG_DONTWAIT;
#endif
}
}
struct Session::Impl {
    Profile profile;
    NativeOps native;
    Send send;
    int fd = -1;
    struct stat identity{};
    mutable std::mutex mutex;
    std::mutex hardware;
    std::thread watchdog;
    bool shutdown = false;
    bool stop_requested = false;
    bool start_running = false;
    std::array<unsigned char, kMaxFrameBytes> staging{};
    std::size_t staged = 0;
    Status result;
    Clock::time_point deadline{};

    Impl(int inherited, Profile p, NativeOps ops, Send writer)
        : profile(std::move(p)), native(std::move(ops)), send(std::move(writer)), fd(inherited >= 3 ? inherited : -1) {
        if (!send) send = [](int socket, const void* data, std::size_t bytes, int flags) {
            return ::send(socket, data, bytes, flags);
        };
        if (profile.backend != kBackend || profile.provider_sha256 != kProviderSha256 ||
            !profile.lease_ms || profile.lease_ms > kMaxLeaseMs || !native.start || !native.stop) {
            reject(Error::Profile); return;
        }
        if (!(device_name(profile.selected_device) ||
            (profile.defer_device_selection && profile.selected_device.empty()))) { reject(Error::Device); return; }
        if (!descriptor(true)) { reject(Error::Peer); return; }
        const int flags = fcntl(fd, F_GETFL), file_flags = fcntl(fd, F_GETFD);
        if (flags < 0 || file_flags < 0 || fcntl(fd, F_SETFL, flags | O_NONBLOCK) ||
            fcntl(fd, F_SETFD, file_flags | FD_CLOEXEC)) { reject(Error::Descriptor); return; }
#ifdef SO_NOSIGPIPE
        int one = 1;
        if (setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof(one))) { reject(Error::Descriptor); return; }
#endif
        watchdog = std::thread([this] { watch(); });
    }
    ~Impl() {
        retire(Error::Cancelled);
        {
            std::lock_guard<std::mutex> lock(mutex);
            shutdown = true;
        }
        if (watchdog.joinable()) watchdog.join();
        erase(profile.selected_device.data(), profile.selected_device.size());
    }
    void close_transport() {
        erase(staging.data(), staging.size()); staged = 0;
        if (fd >= 0) { ::close(fd); fd = -1; }
        result.transport_closed = true;
    }
    void reject(Error error) {
        result.error = error; result.phase = Phase::Failed; close_transport();
    }
    bool descriptor(bool first = false) {
        if (fd < 3) return false;
        struct stat now{}; int type = 0; socklen_t size = sizeof(type);
        sockaddr_un local{}, remote{}; socklen_t local_size = sizeof(local), remote_size = sizeof(remote);
        if (fstat(fd, &now) || !S_ISSOCK(now.st_mode) ||
            getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &size) || type != SOCK_STREAM ||
            getsockname(fd, reinterpret_cast<sockaddr*>(&local), &local_size) ||
            getpeername(fd, reinterpret_cast<sockaddr*>(&remote), &remote_size) ||
            local.sun_family != AF_UNIX || remote.sun_family != AF_UNIX ||
            !unnamed(local, local_size) || !unnamed(remote, remote_size) ||
            !peer(fd, profile.peer_pid, profile.peer_uid)) return false;
        if (first) identity = now;
        return now.st_dev == identity.st_dev && now.st_ino == identity.st_ino && now.st_mode == identity.st_mode;
    }
    bool valid_time() const { return Clock::now() < deadline; }
    Error write_locked(const unsigned char* data, std::size_t bytes) {
        std::size_t offset = 0;
        for (unsigned attempts = 0; offset < bytes && attempts < 32; ++attempts) {
            if (!valid_time()) return Error::Deadline;
            ssize_t count;
            try { count = send(fd, data + offset, bytes - offset, send_flags()); }
            catch (...) { return Error::Transport; }
            if (count > 0 && std::size_t(count) <= bytes - offset) {
                offset += std::size_t(count); result.bytes += std::uint32_t(count);
            } else if (count < 0 && errno == EINTR) continue;
            else return count < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) ? Error::Backpressure : Error::Transport;
        }
        if (offset != bytes) return Error::Backpressure;
        ++result.frames; return Error::None;
    }
    void stop_hardware() {
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (!result.start_attempted || result.stop_attempted || start_running) return;
            result.stop_attempted = true;
        }
        bool confirmed = false;
        {
            std::lock_guard<std::mutex> lock(hardware);
            try { confirmed = native.stop(); } catch (...) {}
        }
        std::lock_guard<std::mutex> lock(mutex);
        result.stop_confirmed = confirmed;
        if (!confirmed && result.error == Error::None) result.error = Error::StopFailed;
        result.phase = confirmed && result.error == Error::None ? Phase::Stopped : Phase::Failed;
        result.complete = confirmed && result.start_confirmed && result.error == Error::None && result.bytes > 0 && result.frames > 0;
    }
    void retire(Error error) {
        bool should_stop;
        {
            std::lock_guard<std::mutex> lock(mutex);
            if (result.phase == Phase::Stopped || result.phase == Phase::Failed) return;
            if (error != Error::None && result.error == Error::None) result.error = error;
            stop_requested = true; close_transport();
            should_stop = result.start_attempted;
            result.phase = should_stop ? Phase::Retiring : (error == Error::None ? Phase::Stopped : Phase::Failed);
        }
        if (should_stop) stop_hardware();
    }
    void watch() {
        // A monotonic bounded check avoids importing GCC 12's newer
        // condition_variable::wait (GLIBCXX_3.4.30) into the older TV runtime.
        // Native STOP may block; the separate tracer retains its kill bound.
        for (;;) {
            bool expired = false;
            {
                std::lock_guard<std::mutex> lock(mutex);
                if (shutdown) return;
                expired = result.start_attempted && !result.stop_attempted &&
                    !(stop_requested && start_running) && !valid_time();
            }
            if (expired) retire(Error::Deadline);
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }
};
Session::Session(int fd, Profile profile, NativeOps ops, Send writer)
    : impl_(std::make_unique<Impl>(fd, std::move(profile), std::move(ops), std::move(writer))) {}
Session::~Session() = default;
bool Session::ready() const {
    std::lock_guard<std::mutex> lock(impl_->mutex);
    return impl_->result.phase == Phase::Idle && impl_->result.error == Error::None;
}
bool Session::reserve(const std::string& caller, const std::string& device) {
    auto& p = *impl_; Error error = Error::None;
    {
        std::lock_guard<std::mutex> lock(p.mutex);
        if (p.result.phase != Phase::Idle) return false;
        if (caller != (p.profile.ui_capture ? kUiCaller : kCaller) || !device_name(device) ||
            (!p.profile.selected_device.empty() && device != p.profile.selected_device)) error = Error::Device;
        else if (!p.descriptor()) error = Error::Peer;
        else { p.profile.selected_device = device; p.result.phase = Phase::Reserved; return true; }
    }
    p.retire(error); return false;
}
bool Session::begin(const std::string& device) {
    auto& p = *impl_;
    {
        std::lock_guard<std::mutex> lock(p.mutex);
        if (p.result.phase != Phase::Reserved || device != p.profile.selected_device || !p.descriptor()) return false;
        p.result.start_attempted = true; p.start_running = true; p.result.phase = Phase::Starting;
        p.deadline = Clock::now() + std::chrono::milliseconds(p.profile.lease_ms);
    }
    bool started = false;
    {
        std::lock_guard<std::mutex> lock(p.hardware);
        try { started = p.native.start(); } catch (...) {}
    }
    bool retire = false; Error reason = Error::None;
    {
        std::lock_guard<std::mutex> lock(p.mutex);
        p.start_running = false; p.result.start_confirmed = started;
        retire = p.stop_requested || !started || !p.valid_time();
        reason = !started ? Error::StartFailed : !p.valid_time() ? Error::Deadline : Error::None;
        if (!retire && p.staged) {
            reason = p.write_locked(p.staging.data(), p.staged);
            erase(p.staging.data(), p.staging.size()); p.staged = 0;
            retire = reason != Error::None;
        }
        if (!retire) p.result.phase = Phase::Capturing;
    }
    if (retire) { p.retire(reason); return false; }
    return true;
}
bool Session::pcm(const std::string& device, const void* first, const void* second, int bytes) {
    auto& p = *impl_; Error error = Error::None;
    std::array<unsigned char, kMaxFrameBytes> buffer{};
    {
        std::lock_guard<std::mutex> lock(p.mutex);
        if (p.result.phase != Phase::Capturing && p.result.phase != Phase::Starting) return false;
        if (!p.valid_time()) error = Error::Deadline;
        else if (device != p.profile.selected_device) error = Error::Device;
        else if (!first || second || bytes <= 0 || bytes % 2 || std::size_t(bytes) > kMaxFrameBytes) error = Error::Format;
        else if (std::uint32_t(bytes) > kMaxUtteranceBytes - p.result.bytes - p.staged) error = Error::Limit;
        else if (!p.descriptor()) error = Error::Peer;
        else {
            // This exact profile certifies FIRST as already normalized PCM.
            // A second pointer is never treated as a guessed alternative.
            if (p.result.phase == Phase::Starting) {
                if (std::size_t(bytes) > p.staging.size() - p.staged) error = Error::Backpressure;
                else { std::memcpy(p.staging.data() + p.staged, first, std::size_t(bytes)); p.staged += std::size_t(bytes); }
            } else {
                std::memcpy(buffer.data(), first, std::size_t(bytes));
                error = p.write_locked(buffer.data(), std::size_t(bytes));
            }
        }
    }
    erase(buffer.data(), buffer.size());
    if (error != Error::None) { p.retire(error); return false; }
    return true;
}
void Session::release() { impl_->retire(Error::None); }
void Session::cancel() { impl_->retire(Error::Cancelled); }
void Session::parent_lost() { impl_->retire(Error::Transport); }
Status Session::status() const { std::lock_guard<std::mutex> lock(impl_->mutex); return impl_->result; }
} // namespace r2d2::local_capture
