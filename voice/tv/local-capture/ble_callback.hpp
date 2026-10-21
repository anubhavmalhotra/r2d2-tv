#pragma once
#include "native_handoff.hpp"
#include <cstdint>
#include <utility>
namespace r2d2::local_capture {
// The fixed provider SHA identifies an ET_DYN ARM32 image. Its normalized BLE FE
// call to notifyDataCaptured returns at VA0x5771c. A mere non-null first pointer
// never establishes this backend. The actual preload obtains the return address
// from the call stack; this is not a JSON or external caller-supplied field.
class BleFeCallback final {
public:
    struct Image {
        std::uintptr_t load_bias;
        std::uintptr_t notify;
        bool valid() const {
            return load_bias <= UINTPTR_MAX - 0x60ba8 &&
                (notify & ~std::uintptr_t(1)) == load_bias + 0x60ba8;
        }
    };
    template <class Callback>
    static bool dispatch(Image image, std::uintptr_t return_address, const void* first,
            const void* second, Callback&& callback) {
        if (!image.valid() || image.load_bias > UINTPTR_MAX - 0x5771c ||
            (return_address & ~std::uintptr_t(1)) != image.load_bias + 0x5771c ||
            !first || second || active_) return false;
        struct Guard { Guard() { active_ = true; } ~Guard() { active_ = false; } } guard;
        std::forward<Callback>(callback)();
        return true;
    }
    static bool active() { return active_; }
private:
    inline static thread_local bool active_ = false;
};
inline bool handoff_ble_pcm(Session& session, const std::string& device,
        const void* first, const void* second, int bytes) {
    if (!BleFeCallback::active()) { session.cancel(); return false; }
    return session.pcm(device, first, second, bytes);
}
} // namespace r2d2::local_capture
