#include "capture_core.h"
#include <string.h>
#include <limits.h>

void owner_clear(void *p, size_t n) {
    volatile uint8_t *v = p;
    while (n--) *v++ = 0;
}

int owner_parse_event(const uint8_t *r, size_t n, enum owner_event *out) {
    if (!out) return -1;
    *out = OWNER_EVENT_NONE;
    if (!r || n == 0) return -1;
    size_t offset;
    if (r[0] == 0xfe && n == OWNER_REPORT_BYTES) offset = 2;
    else if (r[0] == 0xfd && n == 20) offset = 17;
    else return -1;
    unsigned event = (unsigned)r[offset] * 256u + r[offset + 1];
    if (event != OWNER_EVENT_NONE && event != OWNER_VOICE_START &&
        event != OWNER_VOICE_STOP && event != OWNER_VOICE_HELD) return -1;
    *out = (enum owner_event)event;
    return 0;
}
int owner_parse(const uint8_t *r, size_t n, struct owner_packet *out) {
    if (!out) return -1;
    owner_clear(out, sizeof(*out));
    if (!r || n != OWNER_REPORT_BYTES || r[0] != 0xfe) return -1;
    enum owner_event event;
    if (owner_parse_event(r, n, &event) != 0) return -1;
    /* H2 sequence byte and trailer are opaque until the exact remote profile is
       read. We only use invariants checked by the saved provider: 01 .. AD. */
    for (unsigned i = 0; i < 2; i++) {
        const uint8_t *p = r + 4 + 60 * i;
        if (p[0] != 0x01 || p[2] != 0xad) return -1;
    }
    out->event = event;
    for (unsigned i = 0; i < 2; i++) memcpy(out->frame[i], r + 6 + 60 * i, 57);
    return 0;
}

int owner_decoder_init(struct owner_decoder *d) {
    if (!d) return -1;
    memset(d, 0, sizeof(*d));
    if (sbc_init_msbc(&d->codec, 0) != 0) return -1;
    d->codec.endian = SBC_LE;
    if (sbc_get_codesize(&d->codec) != 240 || sbc_get_frame_length(&d->codec) != 57) {
        sbc_finish(&d->codec); owner_clear(d, sizeof(*d)); return -1;
    }
    d->ready = true;
    return 0;
}

int owner_decode(struct owner_decoder *d, const struct owner_packet *p,
                 uint8_t output[OWNER_PCM_BYTES]) {
    if (!output) return -1;
    owner_clear(output, OWNER_PCM_BYTES);
    if (!d || !d->ready || !p) return -1;
    for (unsigned i = 0; i < 2; i++) {
        size_t written = 0;
        ssize_t consumed = sbc_decode(&d->codec, p->frame[i], 57,
                                     output + i * 240, 240, &written);
        if (consumed != 57 || written != 240) {
            owner_clear(output, OWNER_PCM_BYTES);
            /* Failure invalidates decoder history. Explicit re-init is needed. */
            owner_decoder_finish(d);
            return -1;
        }
    }
    return 0;
}

void owner_decoder_finish(struct owner_decoder *d) {
    if (!d) return;
    if (d->ready) sbc_finish(&d->codec);
    owner_clear(d, sizeof(*d));
}

void owner_lifecycle_init(struct owner_lifecycle *s) {
    memset(s, 0, sizeof(*s));
    s->state = OWNER_DISARMED;
}
int owner_arm(struct owner_lifecycle *s, const struct owner_qualification *q) {
    if (!s || !q || s->state != OWNER_DISARMED || !q->earliest_key_guard ||
        !q->original_provider_exclusive || !q->exact_device_identity ||
        !q->fixed_lan_authenticated_sink || !q->physical_stop_contract) return -1;
    s->state = OWNER_READY;
    return 0;
}
static bool clock_valid(struct owner_lifecycle *s, uint64_t now) {
    if (now < s->last_ms || now > UINT64_MAX - 20000) return false;
    s->last_ms = now;
    return true;
}
static enum owner_action stop(struct owner_lifecycle *s, uint64_t now) {
    if (!s->stop_required) return OWNER_NO_ACTION;
    if (s->state == OWNER_STOP_PENDING || s->state == OWNER_RECOVERY_REQUIRED)
        return OWNER_NO_ACTION;
    s->state = OWNER_STOP_PENDING;
    s->deadline_ms = now > UINT64_MAX - 750 ? UINT64_MAX : now + 750;
    s->stop_transport_ack = s->stop_marker = false;
    return OWNER_SEND_STOP;
}
enum owner_action owner_press(struct owner_lifecycle *s, uint64_t now) {
    if (!s || s->state != OWNER_READY) return OWNER_NO_ACTION;
    if (!clock_valid(s, now)) { s->state = OWNER_RECOVERY_REQUIRED; return OWNER_NO_ACTION; }
    s->state = OWNER_START_PENDING;
    /* Set before issuing command: a lost or rejected ack never excuses stop. */
    s->stop_required = true;
    s->start_transport_ack = s->start_marker = false;
    s->deadline_ms = now + 750;
    s->capture_end_ms = now + 20000;
    return OWNER_SEND_START;
}
enum owner_action owner_release(struct owner_lifecycle *s, uint64_t now) {
    if (!s) return OWNER_NO_ACTION;
    if (!clock_valid(s, now)) now = s->last_ms;
    return stop(s, now);
}
enum owner_action owner_tick(struct owner_lifecycle *s, uint64_t now) {
    if (!s) return OWNER_NO_ACTION;
    if (!clock_valid(s, now)) {
        enum owner_action action = stop(s, s->last_ms);
        if (action == OWNER_NO_ACTION) s->state = OWNER_RECOVERY_REQUIRED;
        return action;
    }
    if (s->state == OWNER_START_PENDING && now >= s->deadline_ms) return stop(s, now);
    if (s->state == OWNER_CAPTURING && now >= s->capture_end_ms) return stop(s, now);
    if (s->state == OWNER_STOP_PENDING && now >= s->deadline_ms)
        s->state = OWNER_RECOVERY_REQUIRED;
    return OWNER_NO_ACTION;
}
enum owner_action owner_transport_ack(struct owner_lifecycle *s, bool start,
                                      bool accepted, uint64_t now) {
    if (!s) return OWNER_NO_ACTION;
    enum owner_action action = owner_tick(s, now);
    if (action != OWNER_NO_ACTION) return action;
    if (start && s->state == OWNER_START_PENDING) {
        if (!accepted) return stop(s, now);
        s->start_transport_ack = true;
        if (s->start_marker) s->state = OWNER_CAPTURING;
    } else if (!start && s->state == OWNER_STOP_PENDING) {
        if (!accepted) s->state = OWNER_RECOVERY_REQUIRED;
        else s->stop_transport_ack = true;
    }
    return OWNER_NO_ACTION;
}
enum owner_action owner_observe_event(struct owner_lifecycle *s,
                                      enum owner_event event, uint64_t now) {
    if (!s) return OWNER_NO_ACTION;
    enum owner_action action = owner_tick(s, now);
    if (action != OWNER_NO_ACTION) return action;
    if (event == OWNER_VOICE_START && s->state == OWNER_START_PENDING) {
        s->start_marker = true;
        if (s->start_transport_ack) s->state = OWNER_CAPTURING;
    } else if (event == OWNER_VOICE_STOP) {
        action = stop(s, now);
        if (s->state == OWNER_STOP_PENDING) s->stop_marker = true;
    }
    return action;
}
int owner_confirm_physical_stop(struct owner_lifecycle *s, uint64_t now) {
    if (!s) return -1;
    (void)owner_tick(s, now);
    if (s->state != OWNER_STOP_PENDING || !s->stop_transport_ack || !s->stop_marker)
        return -1;
    owner_lifecycle_init(s);
    s->state = OWNER_READY;
    s->last_ms = now;
    return 0;
}
bool owner_may_forward(const struct owner_lifecycle *s) {
    return s && s->state == OWNER_CAPTURING && s->stop_required;
}
int owner_feature_command(enum owner_action action, uint8_t out[2]) {
    if (!out) return -1;
    out[0] = out[1] = 0;
    if (action != OWNER_SEND_START && action != OWNER_SEND_STOP) return -1;
    out[0] = 0xf9;
    out[1] = action == OWNER_SEND_START ? 0x03 : 0x04;
    return 0;
}
