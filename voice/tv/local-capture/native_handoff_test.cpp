#include "native_handoff.hpp"
#include "ble_callback.hpp"
#include "owner_retirement.h"
#include "native_retirement.hpp"
#include <array>
#include <chrono>
#include <atomic>
#include <fcntl.h>
#include <iostream>
#include <stdexcept>
#include <sys/socket.h>
#include <thread>
#include <unistd.h>
#include <vector>
using namespace r2d2::local_capture;
namespace {
void need(bool x, const char* code) { if (!x) throw std::runtime_error(code); }
struct Pair { int fds[2]; Pair() { need(!socketpair(AF_UNIX, SOCK_STREAM, 0, fds), "SOCKETPAIR"); }
    ~Pair() { for (int fd : fds) if (fd >= 0) close(fd); }
    int give() { int fd = fds[0]; fds[0] = -1; return fd; }
    std::vector<unsigned char> read() { std::vector<unsigned char> v; unsigned char b[1024]; ssize_t n;
        while ((n = recv(fds[1], b, sizeof(b), 0)) > 0) v.insert(v.end(), b, b + n);
        need(n == 0, "READ"); return v; }
};
Profile profile() { return {kBackend, kProviderSha256, "remote19", getpid(), getuid(), 1000}; }
NativeOps ops(int& starts, int& stops, bool started = true, bool stopped = true) {
    return {[&starts, started] { ++starts; return started; }, [&stops, stopped] { ++stops; return stopped; }};
}
void open(Session& s) { need(s.ready(), "READY"); need(s.reserve(kCaller, "remote19"), "RESERVE"); need(s.begin("remote19"), "BEGIN"); }
unsigned groups = 0;
template <class F> void test(F f) { f(); ++groups; }
}
int main() {
try {
 test([] { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops));
    open(s); const unsigned char pcm[] = {1, 2, 3, 4}; need(s.pcm("remote19", pcm, nullptr, 4), "FIRST_BUFFER");
    s.release(); s.release(); auto r = s.status(); need(r.complete && r.stop_confirmed && r.transport_closed && r.bytes == 4 && r.frames == 1, "STATUS");
    need(starts == 1 && stops == 1 && p.read() == std::vector<unsigned char>({1,2,3,4}), "EXACT_PCM"); });
 test([] { for (int bad = 0; bad < 6; ++bad) { Pair p; int starts = 0, stops = 0; auto cfg = profile();
    if (bad == 0) cfg.backend.clear();
    if (bad == 1) cfg.provider_sha256[0] ^= 1;
    if (bad == 2) cfg.peer_pid += 10000;
    if (bad == 3) cfg.peer_uid += 1;
    if (bad == 4) cfg.lease_ms = 10001;
    if (bad == 5) cfg.selected_device = "remote19/../other";
    Session s(p.give(), cfg, ops(starts, stops)); need(!s.ready() && s.status().transport_closed, "BAD_PROFILE_PEER");
    need(starts == 0 && stops == 0 && p.read().empty(), "NO_START"); }});
 test([] { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops));
    need(!s.begin("remote19"), "NO_RESERVATION"); need(!s.reserve("com.webos.service.voiceconductor", "remote19"), "CALLER");
    need(starts == 0 && stops == 0 && s.status().transport_closed, "CALLER_CLOSED"); });
 test([] { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops, false));
    need(s.reserve(kCaller, "remote19") && !s.begin("remote19"), "START_FAIL"); auto r = s.status();
    need(r.start_attempted && !r.start_confirmed && r.stop_attempted && r.stop_confirmed && !r.complete, "AMBIGUOUS_STOP"); need(starts == 1 && stops == 1, "ONCE"); });
 test([] { Pair p; int stops = 0; Session s(p.give(), profile(), {[]()->bool { throw std::runtime_error("PRIVATE"); }, [&] { ++stops; return true; }});
    need(s.reserve(kCaller, "remote19") && !s.begin("remote19"), "THROW_START"); need(stops == 1 && s.status().error == Error::StartFailed, "THROW_STOP"); });
 test([] { for (int bad = 0; bad < 5; ++bad) { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops)); open(s);
    std::array<unsigned char, 8194> pcm{}; const void* first = pcm.data(); const void* second = nullptr; int n = 4;
    if (bad == 0) first = nullptr;
    if (bad == 1) second = pcm.data();
    if (bad == 2) n = 3;
    if (bad == 3) n = 8194;
    if (bad == 4) n = 0;
    need(!s.pcm("remote19", first, second, n), "FORMAT_REFUSE"); need(s.status().error == Error::Format && stops == 1 && p.read().empty(), "FORMAT_STOP"); }});
 test([] { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops)); open(s); unsigned char pcm[2]{};
    need(!s.pcm("remote20", pcm, nullptr, 2), "DEVICE_MISMATCH"); need(s.status().error == Error::Device && stops == 1 && p.read().empty(), "DEVICE_STOP"); });
 test([] { Pair p; int starts = 0, stops = 0; std::vector<std::size_t> requested;
    Session s(p.give(), profile(), ops(starts, stops), [&](int fd, const void* data, std::size_t bytes, int flags) {
        requested.push_back(bytes); return ::send(fd, data, std::min<std::size_t>(17, bytes), flags);
    }); open(s); std::array<unsigned char, 64> pcm{}; for (unsigned i = 0; i < pcm.size(); ++i) pcm[i] = i;
    need(s.pcm("remote19", pcm.data(), nullptr, pcm.size()), "SHORT_SEND"); s.release();
    need(requested == std::vector<std::size_t>({64,47,30,13}), "REMAINING_LENGTH");
    need(p.read() == std::vector<unsigned char>(pcm.begin(), pcm.end()), "NO_OVERREAD_REORDER"); });
 test([] { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops), [](int, const void*, std::size_t, int)->ssize_t { errno = EAGAIN; return -1; });
    open(s); unsigned char pcm[2]{}; need(!s.pcm("remote19", pcm, nullptr, 2), "BACKPRESSURE");
    need(s.status().error == Error::Backpressure && !s.status().complete && stops == 1 && p.read().empty(), "NO_DROP_AS_SUCCESS"); });
 test([] { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops)); open(s); close(p.fds[1]); p.fds[1] = -1; unsigned char pcm[2]{};
    need(!s.pcm("remote19", pcm, nullptr, 2), "PEER_DISCONNECT"); need(stops == 1 && s.status().transport_closed && !s.status().complete, "DISCONNECT_STOP"); });
 test([] { Pair p; int starts = 0, stops = 0; auto cfg = profile(); cfg.lease_ms = 35; Session s(p.give(), cfg, ops(starts, stops)); open(s);
    for (int i = 0; i < 100 && !s.status().stop_attempted; ++i) std::this_thread::sleep_for(std::chrono::milliseconds(5));
    auto r = s.status(); need(r.error == Error::Deadline && r.stop_confirmed && r.transport_closed && stops == 1, "NATIVE_WATCHDOG"); s.release(); need(stops == 1, "WATCHDOG_NO_REPLAY"); });
 test([] { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops, true, false)); open(s); unsigned char pcm[2]{};
    need(s.pcm("remote19", pcm, nullptr, 2), "PCM"); s.release(); auto r = s.status(); need(!r.complete && !r.stop_confirmed && r.error == Error::StopFailed && stops == 1, "STOP_FAILURE_TRUTH"); });
 test([] { Pair p; int starts = 0, stops = 0; Session s(p.give(), profile(), ops(starts, stops)); open(s); s.parent_lost();
    need(stops == 1 && s.status().error == Error::Transport && !s.status().complete, "PARENT_LOSS"); });
 test([] { Pair p; int starts = 0, stops = 0; { Session s(p.give(), profile(), ops(starts, stops)); open(s); }
    need(stops == 1 && p.read().empty(), "DESTRUCTOR_STOP"); });
 test([] { Pair p; int starts = 0, stops = 0; auto cfg=profile(); cfg.selected_device.clear(); cfg.defer_device_selection=true;
    Session s(p.give(),cfg,ops(starts,stops)); need(s.ready(),"DEFERRED_INVENTORY_READY");
    need(!s.begin("remote0") && s.reserve(kCaller,"remote23") && !s.begin("remote19") && s.begin("remote23"),"ACTUAL_SELECTED_ID");
    unsigned char pcm[2]{1,2}; need(s.pcm("remote23",pcm,nullptr,2),"SELECTED_PCM"); s.release();
    need(s.status().complete && starts==1 && stops==1,"DEFERRED_ONCE"); });
 test([] { Pair p; int stops=0; Session *current=nullptr; unsigned char early[4]{1,2,3,4};
    Session s(p.give(),profile(),{[&]{need(current->pcm("remote19",early,nullptr,4),"SYNC_EARLY_ACCEPTED");
        unsigned char byte; need(recv(p.fds[1],&byte,1,MSG_DONTWAIT)<0 && (errno==EAGAIN||errno==EWOULDBLOCK),"NO_PCM_BEFORE_START_ACK"); return true;},[&]{++stops;return true;}});
    current=&s; open(s); unsigned char later[2]{5,6}; need(s.pcm("remote19",later,nullptr,2),"AFTER_START_PCM"); s.release();
    need(p.read()==std::vector<unsigned char>({1,2,3,4,5,6})&&s.status().complete&&stops==1,"STAGED_ORDER"); });
 test([] { Pair p; int stops=0; Session *current=nullptr; std::atomic<bool> accepted{false}; unsigned char early[2]{7,8};
    Session s(p.give(),profile(),{[&]{std::thread producer([&]{accepted=current->pcm("remote19",early,nullptr,2);});producer.join();return true;},[&]{++stops;return true;}});
    current=&s;open(s);s.release();need(accepted&&stops==1&&s.status().complete&&p.read()==std::vector<unsigned char>({7,8}),"CONCURRENT_START_PCM"); });
 test([] { Pair p; int stops=0; Session *current=nullptr; unsigned char early[2]{7,8};
    Session s(p.give(),profile(),{[&]{need(current->pcm("remote19",early,nullptr,2),"FAILED_START_STAGED");return false;},[&]{++stops;return true;}});
    current=&s;need(s.reserve(kCaller,"remote19")&&!s.begin("remote19"),"FAILED_ACK");
    need(stops==1&&s.status().error==Error::StartFailed&&!s.status().complete&&p.read().empty(),"FAILED_START_ERASED"); });
 test([] { for(int invalid=0;invalid<2;++invalid){Pair p;int stops=0;Session *current=nullptr;
    Session s(p.give(),profile(),{[&]{std::thread producer([&]{if(invalid){unsigned char pcm[2]{};need(!current->pcm("remote19",pcm,pcm,2),"EARLY_FORMAT");}else current->release();});producer.join();return true;},[&]{++stops;return true;}});
    current=&s;need(s.reserve(kCaller,"remote19")&&!s.begin("remote19"),"RETIRED_DURING_START");
    need(stops==1&&!s.status().complete&&s.status().stop_confirmed&&p.read().empty(),"EARLY_STOP_NO_DEADLOCK");}});
 test([] { Pair p;int starts=0,stops=0;Session s(p.give(),profile(),ops(starts,stops));open(s);unsigned char pcm[2]{4,9};
    BleFeCallback::Image image{0x71000000,0x71060ba9};
    need(BleFeCallback::dispatch(image,0x7105771d,pcm,nullptr,[&]{need(handoff_ble_pcm(s,"remote19",pcm,nullptr,2),"REAL_CALLBACK_CORE");}),"ASLR_THUMB_CALLSITE");
    s.release();need(s.status().complete&&p.read()==std::vector<unsigned char>({4,9}),"BLE_FIRST_BUFFER_EXACT"); });
 test([] { unsigned char pcm[2]{};int called=0;BleFeCallback::Image image{0x71000000,0x71060ba9};
    need(!BleFeCallback::dispatch(image,0x7205771d,pcm,nullptr,[&]{++called;}),"WRONG_MODULE_RETURN");
    need(!BleFeCallback::dispatch({0x71000000,0x72060ba9},0x7105771d,pcm,nullptr,[&]{++called;}),"WRONG_MODULE_NOTIFY");
    need(!BleFeCallback::dispatch(image,0x71057721,pcm,nullptr,[&]{++called;}),"WRONG_CALLSITE");
    need(!BleFeCallback::dispatch(image,0x7105771d,pcm,pcm,[&]{++called;})&&called==0,"NO_SECOND_BUFFER_GUESS");
    need(BleFeCallback::dispatch(image,0x7105771d,pcm,nullptr,[&]{++called;need(!BleFeCallback::dispatch(image,0x7105771d,pcm,nullptr,[&]{++called;}),"REENTRANT_DENIED");}),"OUTER_CALLBACK");
    need(called==1&&!BleFeCallback::active(),"CALLBACK_SCOPE_RESET"); });
 test([] { owner_retirement state{};need(!owner_retirement_step(&state,1000,0),"LIVE_OWNER_INERT");
    need(owner_retirement_step(&state,1000,1)==OWNER_REQUEST_STOP&&state.deadline==1750,"OWNER_LOSS_REQUEST");
    need(!owner_retirement_step(&state,1749,1),"FULL_STOP_GRACE");
    need(owner_retirement_step(&state,1750,1)==OWNER_GRACE_EXPIRED,"GRACE_BOUNDARY");
    need(owner_retirement_step(&state,999,1)==OWNER_GRACE_EXPIRED,"CLOCK_REGRESSION");
    owner_retirement overflow{};need(owner_retirement_step(&overflow,UINT64_MAX,1)==(OWNER_REQUEST_STOP|OWNER_GRACE_EXPIRED),"GRACE_OVERFLOW"); });
 test([] { Pair p;int starts=0;std::atomic<bool> stopping{false};Session s(p.give(),profile(),{[&]{++starts;return true;},[&]{stopping=true;std::this_thread::sleep_for(std::chrono::milliseconds(35));return true;}});
    open(s);unsigned char pcm[2]{3,4};need(s.pcm("remote19",pcm,nullptr,2),"RELEASE_PCM");
    std::thread releaser([&]{s.release();});while(!stopping)std::this_thread::yield();
    auto final=finish_status(s,100);releaser.join();need(final.complete&&final.stop_confirmed&&final.error==Error::None&&starts==1,"WATCHER_PRESERVES_CLEAN_RETIREMENT"); });
 std::cout << "{\"groups\":" << groups << ",\"deviceCalls\":false}\n"; return 0;
} catch (const std::exception& e) { std::cerr << e.what() << '\n'; return 1; }
}
