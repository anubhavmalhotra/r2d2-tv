"""How a v5 recognition result would trigger the agent (stub; nothing is wired).

Starting point:
  * v4/v5 receipts are scalar-only and carry no intent or text (no model text or
    audio crosses the capture-integration boundary), and commandDispatched must be
    False. So a saved or relayed receipt can never be the trigger; refuse_receipt()
    makes that explicit.
  * The v4/v5 attempt id is 48 lowercase hex; this stub follows that, not the
    shorter 32-hex form.

Two trigger paths:

P1 (HandoffWindow). A distinct, separately reviewed v5 successor keeps the 4-intent
prompt, maps the parsed intent with legacy_to_call and sends ONE frame
    {"schema":1,"kind":"call","attempt":<48hex>,"parsedAt":<CLOCK_MONOTONIC s>,
     "tool":...,"args":{...}}  (<= 2048 B)
over AF_UNIX SOCK_SEQPACKET at $XDG_RUNTIME_DIR/r2d2-agent/handoff.sock (dir 0700),
SO_PEERCRED uid check, one call per connection. [R] The socket exists only
after `r2d2-agentctl handoff arm --attempt <id>` (audited), accepts exactly one
call for that attempt, browser tools only, and is unlinked after the call or 5
minutes. `attempt`/`parsedAt` are sender-chosen: hygiene, not proof. The socket
transport itself is not implemented in P0 (tests forbid sockets); deliver() is
the frame handler the transport would call with (frame bytes, peer uid).

P2 (P2Bridge). The ingress, the warm worker and the agent share one process; the
RecognitionResult (transcript in memory only) goes straight to
agent.handle_utterance under a lease armed at TLS accept.
"""
from dataclasses import dataclass
import math
import re
import threading
import time

from .errors import Outcome, Refused
from .parse import legacy_to_call, parse_legacy_intent
from .registry import REGISTRY
from .validate import bounded_text, canonical, strict_loads

ATTEMPT = re.compile(r'[a-f0-9]{48}')
ARM_WINDOW_S = 300
FRESH_S = 10
MAX_FRAME = 2048
SEEN_CAP = 4096
# [R] browser tools only. TV-routed variants are excluded by the router (tv_routes=False).
HANDOFF_TOOLS = frozenset(('browser.open_site', 'browser.web_search', 'browser.youtube_search',
                           'browser.close', 'control.back', 'control.play_pause', 'tv.home',
                           'meta.whats_playing', 'meta.unsupported'))


@dataclass(frozen=True, repr=False)
class RecognitionResult:
    """In-memory only. repr=False keeps the transcript out of logs."""
    attempt: str
    source: str
    transcript: str
    asr_ms: int

    def __post_init__(self):
        if (type(self.attempt) is not str or not ATTEMPT.fullmatch(self.attempt) or
                self.source != 'ptt' or not bounded_text(self.transcript, 1024, 4096) or
                type(self.asr_ms) is not int or not 0 <= self.asr_ms <= 8000):
            raise ValueError('RECOGNITION_SCHEMA')


class P2Bridge:
    def __init__(self, agent):
        self.agent = agent

    def on_recognition(self, lease, result):
        if type(result) is not RecognitionResult:
            return Outcome('refused', 'TRANSCRIPT_SCOPE')
        return self.agent.handle_utterance(lease, result.transcript, attempt=result.attempt)


def refuse_receipt(_receipt):
    """Saved/displayed state is never authority (command_join rule)."""
    return Outcome('refused', 'RECEIPT_NOT_AUTHORITY')


def build_call_frame(attempt, parsed_at, legacy_raw):
    """Reference for the v5 successor side: strict legacy parse -> legacy_to_call -> frame."""
    call = legacy_to_call(parse_legacy_intent(legacy_raw))
    frame = canonical({'schema': 1, 'kind': 'call', 'attempt': attempt, 'parsedAt': parsed_at,
                       'tool': call.tool, 'args': dict(call.args)})
    if len(frame) > MAX_FRAME:
        raise Refused('HANDOFF_SCHEMA')
    return frame


class HandoffWindow:
    """P1 only. Per-attempt arm window; one call; browser tools only."""
    def __init__(self, agent, *, uid, clock=time.monotonic, audit=None):
        self.agent, self.uid, self.clock, self.audit = agent, uid, clock, audit
        self.armed = None   # (attempt, opened_at)
        self.arming = False  # audit in progress; never delivery authority
        self.seen = set()
        self.lock = threading.Lock()

    def arm(self, attempt):
        """Run by `r2d2-agentctl handoff arm` from the owner-started v5 attempt."""
        with self.lock:
            if self.arming or type(attempt) is not str or not ATTEMPT.fullmatch(attempt) or attempt in self.seen:
                raise Refused('HANDOFF_ATTEMPT')
            if self.armed is not None and self.clock() - self.armed[1] < ARM_WINDOW_S:
                raise Refused('HANDOFF_ATTEMPT')
            # An attempt id is armable once, even if its window expires unused.
            self.seen.add(attempt)
            opened = self.clock()
            self.armed = None
            self.arming = True
        try:
            if self.audit is not None:
                self.audit.append({'event': 'handoff_arm', 'attempt': attempt, 'phase': self.agent.phase})
        except Exception:
            self.agent.killswitch.pause('AUDIT_WRITE')
            raise Refused('AUDIT_UNAVAILABLE') from None
        else:
            with self.lock:
                # Publish only after the audit succeeds. Its latency does not
                # renew the original arm window.
                if self.clock() - opened >= ARM_WINDOW_S:
                    raise Refused('HANDOFF_NOT_ARMED')
                self.armed = (attempt, opened)
        finally:
            with self.lock:
                self.arming = False
        return True

    def _refused(self, code, attempt):
        if self.audit is not None:
            try:
                self.audit.append({'event': 'handoff_refused', 'attempt': attempt, 'code': code,
                                   'phase': self.agent.phase})
            except Exception:
                self.agent.killswitch.pause('AUDIT_WRITE')
        return Outcome('refused', code)

    def is_open(self):
        with self.lock:
            return self.armed is not None and self.clock() - self.armed[1] < ARM_WINDOW_S

    def deliver(self, frame, *, peer_uid):
        with self.lock:
            if self.armed is None or self.clock() - self.armed[1] >= ARM_WINDOW_S:
                self.armed = None  # transport unlinks the socket path
                return Outcome('refused', 'HANDOFF_NOT_ARMED')
            attempt = self.armed[0]
            if type(peer_uid) is not int or peer_uid != self.uid:
                return self._refused('HANDOFF_PEER', attempt)
            # One frame per armed attempt, whatever it contains.
            self.armed = None
            self.seen.add(attempt)
            if len(self.seen) > SEEN_CAP:
                return self._refused('HANDOFF_REPLAY', attempt)
        try:
            value = strict_loads(frame, MAX_FRAME, 'HANDOFF_SCHEMA')
        except Refused as error:
            return self._refused(error.code, attempt)
        if (type(value) is not dict or
                set(value) != {'schema', 'kind', 'attempt', 'parsedAt', 'tool', 'args'} or
                type(value['schema']) is not int or value['schema'] != 1 or value['kind'] != 'call'):
            return self._refused('HANDOFF_SCHEMA', attempt)
        if value['attempt'] != attempt:
            return self._refused('HANDOFF_ATTEMPT', attempt)
        parsed = value['parsedAt']
        now = self.clock()
        if (type(parsed) not in (int, float) or not math.isfinite(parsed) or
                not now - FRESH_S <= parsed <= now):
            return self._refused('HANDOFF_STALE', attempt)
        tool = value['tool']
        if type(tool) is str and tool in REGISTRY and tool not in HANDOFF_TOOLS:
            return self._refused('TOOL_DISABLED', attempt)
        try:
            lease = self.agent.arm()
        except Refused as error:
            return self._refused(error.code, attempt)
        # Unknown tool names reach the agent's re-parse, which refuses and trips
        # PARSE_MISMATCH (the sender's parse disagreed with ours).
        return self.agent.handle_call(lease, {'tool': value['tool'], 'args': value['args']},
                                      attempt=attempt)
