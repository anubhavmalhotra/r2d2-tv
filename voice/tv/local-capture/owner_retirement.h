/* Shared pure owner-loss policy, used by the real supervisor and host tests.
 * Native STOP remains asynchronous; an expired grace is failure, never proof
 * that forcibly killing a process switched off the physical microphone. */
#ifndef R2D2_OWNER_RETIREMENT_H
#define R2D2_OWNER_RETIREMENT_H
#include <stdint.h>
enum { OWNER_REQUEST_STOP=1, OWNER_GRACE_EXPIRED=2 };
struct owner_retirement { uint64_t deadline; int requested; };
static unsigned owner_retirement_step(struct owner_retirement *state,uint64_t now,int owner_lost) {
 if(!state->requested&&!owner_lost)return 0;
 if(!state->requested){state->requested=1;
  if(!now||now>UINT64_MAX-750)return OWNER_REQUEST_STOP|OWNER_GRACE_EXPIRED;
  state->deadline=now+750;return OWNER_REQUEST_STOP;
 }
 if(!now||now>=state->deadline||state->deadline-now>750)return OWNER_GRACE_EXPIRED;
 return 0;
}
#endif
