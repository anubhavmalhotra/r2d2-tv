#ifndef OWNER_CAPTURE_CORE_H
#define OWNER_CAPTURE_CORE_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "sbc/sbc.h"

/* Offline, exact saved-firmware candidate. No device, LS2, network or file I/O. */
enum { OWNER_REPORT_BYTES = 124, OWNER_PCM_BYTES = 480,
       OWNER_RATE = 16000, OWNER_PCM_CHANNELS = 1 };
enum owner_event { OWNER_EVENT_NONE = 0, OWNER_VOICE_START = 0x800a,
                   OWNER_VOICE_STOP = 0x800d, OWNER_VOICE_HELD = 0x808b };
struct owner_packet { enum owner_event event; uint8_t frame[2][57]; };
int owner_parse(const uint8_t *report, size_t bytes, struct owner_packet *out);
int owner_parse_event(const uint8_t *report, size_t bytes, enum owner_event *out);
struct owner_decoder { sbc_t codec; bool ready; };
int owner_decoder_init(struct owner_decoder *decoder);
int owner_decode(struct owner_decoder *decoder, const struct owner_packet *packet,
                 uint8_t output[OWNER_PCM_BYTES]);
void owner_decoder_finish(struct owner_decoder *decoder);
void owner_clear(void *data, size_t bytes);

enum owner_state { OWNER_DISARMED, OWNER_READY, OWNER_START_PENDING,
                   OWNER_CAPTURING, OWNER_STOP_PENDING, OWNER_RECOVERY_REQUIRED };
enum owner_action { OWNER_NO_ACTION, OWNER_SEND_START, OWNER_SEND_STOP };
struct owner_qualification {
    bool earliest_key_guard, original_provider_exclusive,
         exact_device_identity, fixed_lan_authenticated_sink,
         physical_stop_contract;
};
struct owner_lifecycle {
    enum owner_state state;
    bool start_transport_ack, start_marker, stop_transport_ack, stop_marker;
    bool stop_required;
    uint64_t deadline_ms, capture_end_ms, last_ms;
};
void owner_lifecycle_init(struct owner_lifecycle *s);
int owner_arm(struct owner_lifecycle *s, const struct owner_qualification *q);
enum owner_action owner_press(struct owner_lifecycle *s, uint64_t now_ms);
enum owner_action owner_release(struct owner_lifecycle *s, uint64_t now_ms);
enum owner_action owner_transport_ack(struct owner_lifecycle *s, bool is_start,
                                      bool accepted, uint64_t now_ms);
enum owner_action owner_observe_event(struct owner_lifecycle *s,
                                      enum owner_event event, uint64_t now_ms);
enum owner_action owner_tick(struct owner_lifecycle *s, uint64_t now_ms);
/* Called only by a future qualified adapter, never inferred from LS2 returnValue. */
int owner_confirm_physical_stop(struct owner_lifecycle *s, uint64_t now_ms);
bool owner_may_forward(const struct owner_lifecycle *s);
int owner_feature_command(enum owner_action action, uint8_t out[2]);
#endif
