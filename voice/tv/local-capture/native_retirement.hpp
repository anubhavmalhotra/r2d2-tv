#pragma once
#include "native_handoff.hpp"
#include <chrono>
#include <thread>
namespace r2d2::local_capture {
// Preserve a clean release already executing native STOP. A second cancelling
// watcher must neither rewrite its outcome nor _exit before its acknowledgement.
// Timeout remains failure, not a claim of hardware-off; the tracer owns reaping.
inline Status finish_status(Session& session, std::uint32_t wait_ms) {
    auto state = session.status();
    if (state.phase != Phase::Retiring) session.cancel();
    auto deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(wait_ms);
    while ((state = session.status()).phase == Phase::Retiring &&
            std::chrono::steady_clock::now() < deadline)
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    return state;
}
}
