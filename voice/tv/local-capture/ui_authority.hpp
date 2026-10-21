#pragma once
#include <cstdint>
#include <cstring>
namespace r2d2::local_capture {
inline constexpr char kUiCaller[] = "local.owner.r2d2mic";
inline constexpr char kUiCategory[] = "/r2d2-ui";
// These values travel over the existing inherited, parent-bound control pipe.
// No LS2 payload or web setting chooses the native process mode.
inline bool native_route_allowed(std::uint32_t mode, const char *category,
                                 const char *sender, const char *method) {
    if (!category || !sender || !method) return false;
    const bool inventory = !std::strcmp(method, "getDevices");
    const bool capture = !std::strcmp(method, "startStreaming") || !std::strcmp(method, "stopStreaming");
    if (mode == 2)
        return !std::strcmp(category, kUiCategory) && !std::strcmp(sender, kUiCaller) && (inventory || capture);
    if (mode > 1 || std::strcmp(category, "/")) return false;
    const bool keyfilter = !std::strcmp(sender, "com.webos.starfish.keyfilters");
    const bool self_probe = mode == 0 && !std::strcmp(sender, "com.webos.service.voiceinput.hidraw");
    return (keyfilter || self_probe) && (inventory || (mode == 1 && keyfilter && capture));
}
}
