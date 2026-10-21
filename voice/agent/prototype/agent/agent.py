"""Agent core. Order of checks in Agent.handle:

    lease alive -> kill switch -> audit writable -> fast path -> model call (P2) or
    received call (P1) -> parse_tool_call -> phase gate -> router.plan -> rate limit
    -> confirmation rule -> executor.run -> feedback -> audit row

router.plan runs before the rate limit, not after it, because the rate scope
depends on the route (limits are per executor); plan() is pure, so nothing is
dispatched earlier.

Press path (arm): kill switch -> busy -> utterance rate -> warm admit (P2+, when a
warm.WarmController is wired). A warm refusal (VOICE_WARMING_UP / VOICE_UNAVAILABLE)
is shown on the TV where a surface exists, audited, and raised to the ingress, which
relays the code to the TV client: never a silent drop. The warm ticket rides on the
lease and is released when the utterance ends, so the models are never unloaded
under it.

Invariants:
  * One utterance -> at most one plan dispatched. No chaining, no retry.
  * The lease is consumed before the model call and never replayed.
  * After dispatch, any exception or inconsistent result is unknown, never success.
  * The kill switch is read before arming, before the model call and before each
    dispatch (the gate passed to executors).
  * The audit row holds tool, enumerated args, free-text lengths and codes only.
"""
from dataclasses import dataclass, field
import re
import secrets
import threading
import time

from . import fastpath, router
from .audit import AuditUnavailable, audit_args
from .confirm import ConfirmationSlot
from .errors import Outcome, Refused
from .executors.base import checked
from .executors.browser import OWNER_OPEN_WINDOW_S
from .models import check_pinned
from .parse import check_call, parse_tool_call
from .policy import (DEFAULT_VOLUME_CEILING, RATE_HITS_TRIP, RATE_HITS_WINDOW_S, REPEAT_WINDOW_S,
                     UNKNOWN_STREAK_TRIP, RateLimiter, Recent, needs_confirmation)
from .registry import REGISTRY, at_least, phase_index
from .validate import bounded_text

LEASE_S = 30
MODEL_TIMEOUT_S = 5.0
EXEC_SLACK_S = 15
_ATTEMPT = re.compile(r'[a-f0-9]{48}|[a-f0-9]{32}')
# Status-event slots carry closed enum/int args only; free text is a dict in rec.args.
_SLOT_PREFIX = {'tv.open_app': 'app', 'browser.open_site': 'site', 'control.play_pause': 'media',
                'tv.volume': 'volume', 'tv.mute': 'mute', 'tv.open_content': 'content'}
_SLOT_PART = re.compile(r'[a-z0-9_]{1,16}')
_PAGES = {'browser_youtube_watch': 'yt_watch', 'browser_youtube': 'youtube', 'browser_other': 'web'}


@dataclass(repr=False, eq=False)
class Lease:
    nonce: str = field(repr=False)
    expires: float
    consumed: bool = False
    cancelled: bool = False
    ticket: object = field(default=None, repr=False)  # warm admission while in flight


class _Record:
    """Audit row under construction. `echo` and `display` are TV-only and are
    deliberately not part of row()."""
    def __init__(self, phase, origin, attempt):
        self.phase, self.origin, self.attempt = phase, origin, attempt
        self.utterance = secrets.token_hex(16)
        self.asr_chars = None
        self.path, self.model_ms, self.tool, self.args = 'none', None, None, None
        self.rule, self.pending, self.surface = None, None, 'unknown'
        self.executor, self.dispatched, self.shown = None, False, False
        self.echo = None
        self.display = ()
        self.expires = None  # pending confirmation expiry, for the status event only

    def row(self, outcome):
        return {'phase': self.phase, 'utterance': self.utterance, 'origin': self.origin,
                'attempt': self.attempt,
                'asr': None if self.asr_chars is None else {'chars': self.asr_chars},
                'selection': {'path': self.path, 'ms': self.model_ms, 'tool': self.tool,
                              'args': self.args},
                'policy': {'confirm': self.rule, 'pending': self.pending, 'surface': self.surface},
                'execution': {'executor': self.executor, 'state': outcome.state, 'code': outcome.code,
                              'dispatched': self.dispatched},
                'feedback': {'shown': self.shown}}


def _refuse(code):
    return Outcome('refused', code)


def _slot(rec):
    """Closed status slot from enumerated values only: rec.args (never call.args
    or plan params, which carry free text), the route, or the status display."""
    if rec.tool == 'tv.home':
        return 'via:' + rec.executor if rec.executor in ('browser', 'tv') else None
    if rec.tool == 'meta.whats_playing':
        shown = dict(item for item in rec.display if type(item) is tuple and len(item) == 2)
        where, page = shown.get('app'), _PAGES.get(shown.get('surface'))
        if type(where) is str and _SLOT_PART.fullmatch(where):
            return 'on:' + where
        if page == 'yt_watch':
            paused = shown.get('paused')
            return 'page:yt_watch:' + ('paused' if paused is True else 'playing' if paused is False else 'unknown')
        return None if page is None else 'page:' + page
    prefix = _SLOT_PREFIX.get(rec.tool)
    if prefix is None or type(rec.args) is not dict:
        return None
    parts = [prefix]
    for name, _ in REGISTRY[rec.tool].args:
        value = rec.args.get(name)
        value = str(value) if type(value) is int else value
        if type(value) is str:
            if not _SLOT_PART.fullmatch(value):
                return None
            parts.append(value)
    return ':'.join(parts[:3]) if len(parts) > 1 else None


class Agent:
    def __init__(self, *, phase, audit, killswitch, model=None, browser=None, tv=None,
                 feedback=None, clock=time.monotonic, surface_probe=None,
                 volume_ceiling=DEFAULT_VOLUME_CEILING, warm=None, events=None):
        phase_index(phase)
        if model is not None:
            check_pinned(model, phase, legacy_allowed=phase == 'P1a')
        self.phase, self.model, self.audit, self.killswitch = phase, model, audit, killswitch
        self.browser, self.tv, self.feedback = browser, tv, feedback
        self.warm = warm  # warm.WarmController (P2+ host); None keeps P0/P1 behaviour
        self.events = events  # events(lease, kind, fields): enum/int fields only, never text
        self.clock, self.surface_probe = clock, surface_probe
        self.volume_ceiling = volume_ceiling
        self.rates = RateLimiter(clock)
        self.repeats = Recent(clock, REPEAT_WINDOW_S)
        self.rate_hits = Recent(clock, RATE_HITS_WINDOW_S)
        self.confirm = ConfirmationSlot(clock)
        self.unknown_streak = 0
        self.lock = threading.Lock()
        self.active = None
        self.busy = False

    # ------------------------------------------------------------------ lease
    def arm(self):
        """Trusted press-to-talk owner only (ingress at TLS accept, before any PCM
        is read [R], or the armed P1 handoff). Never exposed to ASR, model or
        page data."""
        with self.lock:
            if self.killswitch.paused():
                self.confirm.take()  # a pause also drops any pending plan
                raise Refused('AGENT_PAUSED')
            if self.busy:
                raise Refused('AGENT_BUSY')
            try:
                self.rates.admit(('utterance',))
            except Refused:
                self._rate_hit()
                raise
            ticket, refused = None, None
            if self.warm is not None:
                try:
                    ticket = self.warm.admit()
                except Refused as error:
                    refused = error.code
                except Exception:
                    refused = 'VOICE_UNAVAILABLE'
            if refused is None:
                if self.active is not None:
                    self._release(self.active)  # never ran: its admission ends here
                    self.active.consumed = True
                self.active = Lease(secrets.token_hex(16), self.clock() + LEASE_S, ticket=ticket)
                return self.active
        self._press_refused(refused)
        raise Refused(refused)

    def _press_refused(self, code):
        """Warm refusal at the press: fixed template on the TV where a surface
        exists (overlay, or toast in P4+), plus an audit row (no transcript exists yet)."""
        shown = False
        if self.feedback is not None:
            try:
                shown = self.feedback.show(code, self._surface()) is True
            except Exception:
                shown = False
        try:
            self.audit.append({'event': 'press_refused', 'phase': self.phase, 'code': code,
                               'warm': getattr(self.warm, 'state', None), 'feedback': {'shown': shown}})
        except AuditUnavailable:
            self._trip('AUDIT_WRITE')

    def _release(self, lease):
        """Idempotent. Called with self.lock held."""
        ticket, lease.ticket = lease.ticket, None
        if ticket is not None and self.warm is not None:
            self.warm.release(ticket)

    def cancel(self, lease=None):
        """Identity-bound: a late, old ingress cannot cancel a newer lease."""
        with self.lock:
            if lease is not None and self.active is not lease:
                return False
            if self.active is not None:
                if not self.active.consumed:
                    self._release(self.active)  # a running utterance releases in _run
                self.active.consumed = True
                self.active.cancelled = True
            self.active = None
            return True

    def _alive(self, lease):
        if lease.cancelled or self.clock() >= lease.expires:
            raise Refused('REQUEST_EXPIRED')

    # ------------------------------------------------------------- entry points
    def handle_utterance(self, lease, transcript, *, attempt=None):
        """P2+: transcript held in memory only, never stored or logged."""
        return self._run(lease, 'ptt', attempt, transcript=transcript)

    def handle_call(self, lease, call_value, *, attempt=None):
        """P1: a decoded {tool,args} from the armed handoff. Always re-parsed; any
        disagreement with the sender's parse trips the agent to Paused."""
        return self._run(lease, 'handoff', attempt, call_value=call_value)

    def _run(self, lease, origin, attempt, *, transcript=None, call_value=None):
        with self.lock:
            if (self.busy or type(lease) is not Lease or self.active is not lease or
                    lease.consumed):
                return _refuse('REQUEST_NOT_ARMED')
            lease.consumed = True
            self.busy = True
        # `attempt` reaches the audit row; accept only the v4/v5 48-hex id (or a
        # 32-hex id), never caller text.
        attempt_ok = attempt is None or (type(attempt) is str and _ATTEMPT.fullmatch(attempt) is not None)
        rec = _Record(self.phase, origin, attempt if attempt_ok else None)
        outcome = Outcome('unknown', 'EXECUTION_UNCONFIRMED')
        try:
            try:
                outcome = (self._steps(lease, origin, transcript, call_value, rec) if attempt_ok
                           else _refuse('HANDOFF_ATTEMPT'))
            except Refused as error:
                outcome = (Outcome('unknown', 'EXECUTION_UNCONFIRMED') if rec.dispatched
                           else _refuse(error.code))
            except Exception:
                # An adapter exception can follow dispatch: never claim no action.
                outcome = Outcome('unknown', 'EXECUTION_UNCONFIRMED')
            transcript = call_value = None
            self._after(outcome)
            if self.feedback is not None and outcome.code != 'AUDIT_UNAVAILABLE':
                rec.shown = self.feedback.show(outcome.code, rec.surface, echo=rec.echo)
            if outcome.code == 'CONFIRMATION_PENDING':
                reason = ('REQUEST_EXPIRED' if lease.cancelled or self.clock() >= lease.expires
                          else 'AGENT_PAUSED' if self.killswitch.paused()
                          else 'CONFIRMATION_SURFACE_UNAVAILABLE' if not rec.shown else None)
                if reason is not None:
                    self.confirm.take()
                    outcome = _refuse(reason)
            # After the rewrite: a dropped pending is never reported as one.
            ttl = (max(0, min(15000, int((rec.expires - self.clock()) * 1000)))
                   if outcome.code == 'CONFIRMATION_PENDING' and rec.expires is not None else None)
            self._emit(lease, 'outcome', rec, state=outcome.state, code=outcome.code, rule=rec.rule, ttl=ttl)
            rec.echo = None
            try:
                self.audit.append(rec.row(outcome))
            except AuditUnavailable:
                self._trip('AUDIT_WRITE')
            return outcome
        finally:
            with self.lock:
                self._release(lease)
                if self.active is lease:
                    self.active = None
                self.busy = False

    # ------------------------------------------------------------- the steps
    def _steps(self, lease, origin, transcript, call_value, rec):
        self._alive(lease)
        if self.killswitch.paused():
            self.confirm.take()  # a pause also drops any pending plan
            return _refuse('AGENT_PAUSED')
        if not self.audit.writable():
            self._trip('AUDIT_WRITE')
            return _refuse('AUDIT_UNAVAILABLE')
        rec.surface = self._surface()
        dropped = (None, False)
        if origin == 'handoff':
            rec.path = 'handoff'
            call = self._reparse(call_value)
        else:
            if not bounded_text(transcript, 1024, 4096):
                return _refuse('TRANSCRIPT_SCOPE')
            rec.asr_chars = len(transcript)
            fast = fastpath.match(transcript) if at_least(self.phase, 'P2') else None
            if fast is not None:
                rec.path = 'fast'
                if fast[0] == 'cancel':
                    return self._cancel_pending(self.confirm.take(), rec)
                return self._answer(lease, fast[1], rec)
            # Any other utterance declines a pending confirmation.
            dropped = self.confirm.take()
            if dropped[0] is not None:
                rec.pending = 'expired' if dropped[1] else 'declined'
            if fastpath.negated(transcript):
                return _refuse('NEGATED_COMMAND')
            if self.killswitch.paused():
                return _refuse('AGENT_PAUSED')
            if self.model is None:
                return _refuse('MODEL_UNAVAILABLE')
            rec.path = 'model'
            started = self.clock()
            try:
                raw = self.model.interpret(
                    transcript, timeout_s=min(MODEL_TIMEOUT_S, max(0.001, lease.expires - self.clock())))
            except Exception:
                return _refuse('MODEL_UNAVAILABLE')
            finally:
                rec.model_ms = max(0, min(60000, round((self.clock() - started) * 1000)))
            self._alive(lease)
            try:
                call = parse_tool_call(raw)
            finally:
                raw = None
        rec.tool = call.tool
        rec.args = audit_args(REGISTRY[call.tool], call.args)
        return self._admit(lease, call, origin, rec, dropped)

    def _reparse(self, value):
        try:
            call = check_call(value)
        except Refused:
            self._trip('PARSE_MISMATCH')
            raise Refused('PARSE_MISMATCH') from None
        if dict(call.args) != value['args']:
            self._trip('PARSE_MISMATCH')
            raise Refused('PARSE_MISMATCH')
        return call

    def _admit(self, lease, call, origin, rec, dropped):
        tool = REGISTRY[call.tool]
        if not at_least(self.phase, tool.phase):
            return _refuse('TOOL_DISABLED')
        if call.tool == 'meta.unsupported':
            return _refuse('UNSUPPORTED_COMMAND')
        if call.tool == 'meta.cancel':
            return self._cancel_pending(dropped, rec)
        plan = router.plan(call, rec.surface, lease.nonce, self.phase, tv_routes=origin != 'handoff')
        rec.executor = plan.executor
        try:
            self.rates.admit(plan.scopes, plan.units, record=False)
        except Refused:
            self._rate_hit()
            raise
        rule = needs_confirmation(tool, plan, repeats=self.repeats.count(call.tool),
                                  current_volume=self._volume_now(plan), ceiling=self.volume_ceiling)
        self.repeats.add(call.tool)
        if rule is not None:
            rec.rule = rule
            if (origin == 'handoff' or self.feedback is None or
                    not self.feedback.can_confirm(rec.surface, free_text=tool.free_text)):
                return _refuse('CONFIRMATION_SURFACE_UNAVAILABLE')
            rec.expires = self.confirm.hold(plan, rec.surface, rule, rec.args).expires
            rec.echo = self._card(call)
            return _refuse('CONFIRMATION_PENDING')
        if call.tool in ('browser.web_search', 'browser.youtube_search'):
            rec.echo = call.args['query']  # overlay only (TV), never audited
        return self._execute(lease, plan, rec)

    def _execute(self, lease, plan, rec):
        executor = {'browser': self.browser, 'tv': self.tv}.get(plan.executor)
        if executor is None:
            return _refuse('BROWSER_NOT_OPENED' if plan.executor == 'browser' else 'TV_CHANNEL_UNAVAILABLE')
        if self.killswitch.paused():
            return _refuse('AGENT_PAUSED')
        if plan.executor == 'tv' and self.killswitch.tv_off():
            return _refuse('TV_CHANNEL_UNAVAILABLE')
        try:
            self.rates.admit(plan.scopes, plan.units)
        except Refused:
            self._rate_hit()
            raise
        window = OWNER_OPEN_WINDOW_S if plan.action == 'navigate' else 0
        deadline = self.clock() + window + EXEC_SLACK_S

        def gate():
            if lease.cancelled:
                return 'REQUEST_EXPIRED'
            if self.killswitch.paused():
                return 'AGENT_PAUSED'
            if plan.executor == 'tv' and self.killswitch.tv_off():
                return 'TV_CHANNEL_UNAVAILABLE'
            return None

        self._emit(lease, 'doing', rec)
        rec.dispatched = True  # pessimistic until the executor's own report is validated
        result = executor.run(plan, gate=gate, deadline=deadline)
        valid = checked(plan, result)
        if valid is None:
            return Outcome('unknown', 'EXECUTION_UNCONFIRMED')
        state, code, dispatched, display = valid
        rec.dispatched, rec.display = dispatched, display
        return Outcome(state, code)

    # ------------------------------------------------------------------ confirmation
    def _answer(self, lease, answer, rec):
        pending, expired = self.confirm.take()
        if pending is None:
            return _refuse('UNSUPPORTED_COMMAND')  # yes/no with nothing pending
        rec.tool, rec.args, rec.rule = pending.plan.tool, pending.audit_args, pending.rule
        if expired:
            return _refuse('CONFIRMATION_EXPIRED')
        if answer != 'yes':
            return _refuse('CONFIRMATION_DECLINED')
        if pending.plan.digest() != pending.digest:
            return _refuse('TOOL_SCHEMA')
        if not at_least(self.phase, REGISTRY[pending.plan.tool].phase):
            return _refuse('TOOL_DISABLED')
        rec.executor = pending.plan.executor
        return self._execute(lease, pending.plan, rec)

    def _cancel_pending(self, taken, rec):
        pending, expired = taken
        if pending is None or expired:
            return _refuse('NOTHING_PENDING')
        rec.pending = 'cancelled'
        return Outcome('verified', 'PENDING_CANCELLED')

    @staticmethod
    def _card(call):
        """Exact app + title for the on-TV card (overlay only; never audited)."""
        if call.tool == 'tv.open_content':
            return '%s %s: %s' % (call.args['app'], call.args['kind'], call.args['text'])
        return call.tool

    # ------------------------------------------------------------- helpers
    def _volume_now(self, plan):
        if plan.action == 'volume' and plan.param('direction') == 'up' and self.tv is not None:
            try:
                return self.tv.read_volume()
            except Exception:
                return None
        return None

    def _surface(self):
        try:
            if self.surface_probe is not None:
                value = self.surface_probe()
            else:
                value = 'unknown'
                if self.browser is not None:
                    value = self.browser.surface(self.clock() + 5)
                if value in ('browser_none', 'unknown') and self.tv is not None and \
                        at_least(self.phase, 'P3'):
                    value = self.tv.surface()
        except Exception:
            value = 'unknown'
        return value if value in router.SURFACES else 'unknown'

    def _emit(self, lease, kind, rec, **fields):
        """Never raises: an observer fault inside _steps would otherwise become an
        unknown outcome and count toward the kill-switch streak."""
        if self.events is None:
            return
        try:
            fields.update(tool=rec.tool if rec.tool in REGISTRY else None, slot=_slot(rec))
            self.events(lease, kind, fields)
        except Exception:
            pass

    def _trip(self, reason):
        self.killswitch.pause(reason)

    def _rate_hit(self):
        self.rate_hits.add()
        if self.rate_hits.count() >= RATE_HITS_TRIP:
            self._trip('RATE_TRIPS')

    def _after(self, outcome):
        if outcome.state == 'unknown':
            self.unknown_streak += 1
            if self.unknown_streak >= UNKNOWN_STREAK_TRIP:
                self._trip('UNKNOWN_STREAK')
        elif outcome.state in ('verified', 'accepted', 'rejected'):
            self.unknown_streak = 0
