"""In-RAM voice UI status for the resident path; one per Manager process.

Inert at construction: no thread, socket or file. One leaf lock guards it.
Writers on the utterance and HTTP threads only swap fields and notify; no I/O,
callback or other lock is ever taken while it is held. Home and the resident
receive enum-derived copy from voice_copy only. The search echo is kept for
8 s and reaches viewer_card() alone, never the plaintext :4181 views or logs.
"""
import collections
import hmac
import json
import re
import threading
import time
import unicodedata

if __package__:
    from . import voice_copy
else:
    import voice_copy

PING_DOMAIN = b'R2D2-voice-phase-v1\0'
PING_KEYS = frozenset(('schema', 'boot', 'seq', 'press', 'phase', 'code', 'deadlineMs', 'cut', 'mac'))
PING_PHASES = ('pressed', 'listening', 'released', 'fault')
HOLD_S, SLICE_S, RECEIPT_S = 20.0, 0.5, 1.5
SEEN_S, BIND_S, ECHO_S, ECHO_MAX = 3.0, 15.0, 8.0, 48
HOME_MAX, CARD_MAX, RECEIPT_MAX = 512, 1024, 256
CONFIRM_TTL_MS = 14000  # used only when the agent detail is missing: under Spark's 15 s
DEADLINE_MAX_MS = 10000  # listening deadlineMs bound: the TV's 9.2 s hold, under its 10 s provider lease
_RANK = {'idle': 0, 'pressed': 1, 'listening': 2, 'released': 3, 'doing': 4, 'confirm': 5, 'result': 5}
_STALE_S = {'pressed': 12.0, 'listening': 12.0, 'released': 210.0, 'doing': 210.0}
_SURFACING = ('tv.home', 'browser.open_site', 'browser.web_search', 'browser.youtube_search')


def _hex(value, size):
    return type(value) is str and re.fullmatch('[a-f0-9]{%d}' % size, value) is not None


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True,
                      allow_nan=False).encode('ascii')


def parse_ping(value):
    """Exact resident phase-ping body -> (unsigned row, mac). Raises ValueError."""
    if not (type(value) is dict and set(value) == PING_KEYS
            and type(value['schema']) is int and value['schema'] == 1
            and _hex(value['boot'], 16) and _hex(value['press'], 16) and _hex(value['mac'], 64)
            and type(value['seq']) is int and 1 <= value['seq'] <= 2 ** 31
            and type(value['phase']) is str and value['phase'] in PING_PHASES
            and (type(value['code']) is str and value['code'] in voice_copy.FAULTS
                 if value['phase'] == 'fault' else value['code'] is None)
            and (value['deadlineMs'] is None or type(value['deadlineMs']) is int
                 and 0 <= value['deadlineMs'] <= DEADLINE_MAX_MS)
            and type(value['cut']) is bool):
        raise ValueError('VOICE_PHASE')
    return {key: item for key, item in value.items() if key != 'mac'}, value['mac']


def ping_mac(key, row):
    return hmac.digest(key, PING_DOMAIN + canonical(row), 'sha256').hex()


def _echo(text):
    """Viewer-only search echo: printable, collapsed, at most 48 code points."""
    if type(text) is not str or not 0 < len(text) <= 1024 or any(
            unicodedata.category(c)[0] == 'C' or unicodedata.category(c) in ('Zl', 'Zp') for c in text):
        return None
    text = ' '.join(text.split())
    return (text if len(text) <= ECHO_MAX else text[:ECHO_MAX - 3] + '...') or None


def _size(value):
    return len(json.dumps(value, separators=(',', ':')).encode('ascii'))


class StatusFeedback:
    """Agent Feedback for the resident path: no SSAP toast and no outcome publication.

    Home, the viewer card and the resident's own receipt toast render outcomes
    from VoiceStatus after the agent's pending rewrite, so show() is exactly True
    and only enum-only confirmations are offered. The search echo is staged here,
    never published: VoiceStatus keeps it only if the final outcome matches.
    """
    def __init__(self, status=None, phase='P5'):
        self.status = status
        self.confirmable = phase in ('P2', 'P3', 'P4', 'P5')

    def can_confirm(self, surface, *, free_text):
        return self.confirmable and not free_text

    def show(self, code, surface, *, echo=None):
        try:
            if code == 'BROWSER_NAVIGATION_ACCEPTED' and echo is not None and self.status is not None:
                self.status.stage_echo(echo)
        except Exception:
            pass
        return True


class VoiceStatus:
    def __init__(self, *, clock=time.monotonic):
        if not callable(clock):
            raise ValueError('VOICE_STATUS_CONFIG')
        self.clock = clock
        self._cond = threading.Condition(threading.Lock())
        self._closed = False
        self._seq = 1
        self._chip = voice_copy.chip(None, 'ok')
        self._phase, self._at, self._owner = 'idle', clock(), None
        self._press = self._deadline = self._state = self._code = None
        self._tool = self._slot = self._rule = self._ttl = None
        self._cut = self._confirming = self._expired = False
        self._echo = None
        self._echo_until = self._pending_until = None
        self._boot, self._boot_seq = None, 0
        self._retired = collections.deque(maxlen=16)
        self._bound = collections.deque(maxlen=16)
        self._last_ping = None
        self._utt, self._u = 0, None
        self._receipts = collections.OrderedDict()
        self._home_seq, self._home_at, self._home_gen, self._home_waiter = 0, None, 0, None
        self._viewer_seq, self._viewer_at = 0, None

    # -- state changes (lock held) ------------------------------------------------
    def _display(self, phase, now, *, press, owner=None, deadline=None, cut=False, state=None,
                 code=None, tool=None, slot=None, rule=None, ttl=None, confirming=False):
        self._phase, self._at, self._owner, self._press = phase, now, owner, press
        self._deadline, self._cut, self._confirming = deadline, cut, confirming
        self._state, self._code, self._tool, self._slot = state, code, tool, slot
        self._rule, self._ttl, self._echo = rule, ttl, None
        self._expired = False
        self._seq += 1
        self._cond.notify_all()

    def _ended(self, now):
        """Whether readers now see this display as idle: a stale live phase, an
        expired confirmation or a result past its dwell."""
        age = max(0.0, now - self._at)
        phase = self._phase
        if phase in _STALE_S:
            return age > _STALE_S[phase]
        if phase == 'confirm':
            return age * 1000 > self._ttl
        return phase == 'result' and age * 1000 > voice_copy.card(
            'result', state=self._state, code=self._code, tool=self._tool, slot=self._slot)['dwell']

    def _expire(self, now):
        # Home renders each seq once and a held poll wakes only on a seq change, so a
        # display that ages into idle takes a new seq exactly once. Without it a lost
        # fault ping would leave "Listening..." on Home until the next press.
        if not self._expired and self._phase != 'idle' and self._ended(now):
            self._expired = True
            self._seq += 1
            self._cond.notify_all()

    def _result(self, press, state, code, now, *, owner=None, tool=None, slot=None, rule=None, ttl=None):
        confirm = code == 'CONFIRMATION_PENDING'
        if confirm:
            ttl = CONFIRM_TTL_MS if ttl is None else ttl
        self._pending_until = now + ttl / 1000 if confirm else None
        self._display('confirm' if confirm else 'result', now, press=press, owner=owner, state=state,
                      code=code, tool=tool, slot=slot, rule=rule, ttl=ttl if confirm else None)
        if press is not None:
            self._receipts[press] = dict(seq=self._seq, state=state, code=code, tool=tool, slot=slot,
                                         rule=rule)
            self._receipts.move_to_end(press)
            while len(self._receipts) > 8:
                self._receipts.popitem(last=False)

    def _current(self, token):
        u = self._u
        return u if not self._closed and u is not None and u['token'] == token and u['open'] else None

    def _advance(self, u, phase, now):
        # Pipeline progress never regresses the same press's display.
        mine = self._owner == u['token'] or u['press'] is not None and self._press == u['press']
        if mine and _RANK[self._phase] >= _RANK[phase]:
            return
        self._display(phase, now, press=u['press'], owner=u['token'],
                      cut=self._cut if mine else False)

    def _surface(self, now):
        if self._home_waiter is not None or self._home_at is not None and 0 <= now - self._home_at <= SEEN_S:
            return 'home'
        if self._viewer_at is not None and 0 <= now - self._viewer_at <= SEEN_S:
            return 'viewer'
        return 'other'

    # -- resident phase pings -----------------------------------------------------
    def phase_ping(self, row):
        """Authenticated row from parse_ping -> visible surface, or None on replay."""
        now = self.clock()
        with self._cond:
            if self._closed:
                return None
            boot, seq, press, phase = row['boot'], row['seq'], row['press'], row['phase']
            if boot == self._boot:
                if seq <= self._boot_seq:
                    return None
            elif boot in self._retired:
                return None
            else:
                if self._boot is not None:
                    self._retired.append(self._boot)
                self._boot = boot
            self._boot_seq = seq
            if phase != 'fault':
                self._last_ping = (press, phase, now)
            elif self._last_ping is not None and self._last_ping[0] == press:
                # A fault ends its own press, which then never binds an upload; a
                # fault for another press (busy or tap hint) leaves the binding alone.
                self._last_ping = None
            u = self._u
            if (phase == 'released' and u is not None and u['press'] is None and u['candidate'] == press
                    and 0 <= now - u['began'] <= BIND_S and press not in self._bound):
                # The upload can authenticate before this ping lands; bind late.
                u['press'] = press
                self._bound.append(press)
                if self._owner == u['token']:
                    self._press = press
                if u['outcome'] is not None:
                    self._receipts[press] = dict(u['outcome'], seq=self._seq if self._owner == u['token'] else 0)
            if phase == 'fault':
                code = row['code']
                if not (code == 'RESULT_UNCONFIRMED' and press in self._receipts):
                    self._result(press, 'unknown' if code == 'RESULT_UNCONFIRMED' else 'refused', code, now)
            elif press != self._press:
                confirming = self._pending_until is not None and now < self._pending_until
                self._display(phase, now, press=press, deadline=row['deadlineMs'] if phase == 'listening' else None,
                              cut=row['cut'] and phase == 'released', confirming=confirming)
            elif _RANK[self._phase] < _RANK[phase]:
                self._display(phase, now, press=press, owner=self._owner,
                              deadline=row['deadlineMs'] if phase == 'listening' else None,
                              cut=row['cut'] and phase == 'released', confirming=self._confirming)
            return self._surface(now)

    # -- Spark pipeline -----------------------------------------------------------
    def begin_utterance(self):
        """TLS-authenticated upload: bind the latest press only if it was released
        within 15 s (or is still pending release); otherwise no press."""
        now = self.clock()
        with self._cond:
            self._utt += 1
            bound = candidate = None
            last = self._last_ping
            if last is not None and 0 <= now - last[2] <= BIND_S and last[0] not in self._bound:
                if last[1] == 'released':
                    bound = last[0]
                    self._bound.append(bound)
                elif last[1] in ('pressed', 'listening'):
                    candidate = last[0]
            self._u = dict(token=self._utt, press=bound, candidate=candidate, began=now, armed=False,
                           open=True, detail=None, echo=None, outcome=None)
            return self._utt

    def armed(self, token):
        with self._cond:
            u = self._current(token)
            if u is not None:
                u['armed'] = True
                self._advance(u, 'released', self.clock())

    def thinking(self, token):
        with self._cond:
            u = self._current(token)
            if u is not None:
                self._advance(u, 'released', self.clock())

    def doing(self, tool, slot):
        with self._cond:
            u = self._u
            if u is None or self._closed or not u['open']:
                return
            tool = tool if type(tool) is str and tool in voice_copy.TOOLS else None
            slot = slot if type(slot) is str and slot in voice_copy.SLOTS else None
            self._display('doing', self.clock(), press=u['press'], owner=u['token'], tool=tool, slot=slot)

    def agent_outcome(self, state, code, tool=None, slot=None, rule=None, ttl_ms=None):
        """Agent detail after its pending rewrite; merged only if the final code matches."""
        now = self.clock()
        with self._cond:
            u = self._u
            if u is None or self._closed or not u['open'] or type(code) is not str:
                return
            ttl = ttl_ms if type(ttl_ms) is int and 0 <= ttl_ms <= 15000 else None
            u['detail'] = dict(
                code=code, tool=tool if type(tool) is str and tool in voice_copy.TOOLS else None,
                slot=slot if type(slot) is str and slot in voice_copy.SLOTS else None,
                rule=rule if type(rule) is str and rule in voice_copy.RULES else None,
                expires=None if ttl is None else now + ttl / 1000)

    def stage_echo(self, text):
        with self._cond:
            u = self._u
            if u is not None and not self._closed and u['open']:
                u['echo'] = _echo(text)

    def outcome(self, token, state, code):
        """Final state/code exactly as sent to the TV (or the warm refusal)."""
        if (state not in voice_copy.STATES or type(code) is not str
                or not voice_copy.CODE.fullmatch(code)):
            state, code = 'refused', 'VOICE_UNAVAILABLE'
        now = self.clock()
        with self._cond:
            u = self._current(token)
            if u is None:
                return
            detail = u['detail'] if u['detail'] is not None and u['detail']['code'] == code else {}
            expires = detail.get('expires')
            ttl = None if expires is None else max(0, min(15000, int((expires - now) * 1000)))
            fields = dict(tool=detail.get('tool'), slot=detail.get('slot'), rule=detail.get('rule'))
            self._result(u['press'], state, code, now, owner=token, ttl=ttl, **fields)
            echo = u['echo']
            if code == 'BROWSER_NAVIGATION_ACCEPTED' and fields['tool'] in voice_copy.SEARCHES and echo:
                self._echo, self._echo_until = echo, now + ECHO_S
            u.update(open=False, echo=None, detail=None, outcome=dict(state=state, code=code, **fields))

    def failed(self, token):
        """Exception path with no frame sent: unknown once armed, else nothing ran."""
        with self._cond:
            u = self._current(token)
            armed = u is not None and u['armed']
        if u is not None:
            self.outcome(token, *(('unknown', 'EXECUTION_UNCONFIRMED') if armed else ('refused', 'VOICE_UNAVAILABLE')))

    def readiness(self, warm, agent):
        value = voice_copy.chip(warm if type(warm) is str else None, 'paused' if agent == 'paused' else 'ok')
        with self._cond:
            if not self._closed and value != self._chip:
                self._chip = value
                self._seq += 1
                self._cond.notify_all()

    # -- readers ------------------------------------------------------------------
    def _view(self, now):
        self._expire(now)
        age = max(0.0, now - self._at)
        phase = self._phase
        if phase in _STALE_S and age > _STALE_S[phase] or phase == 'confirm' and age * 1000 > self._ttl:
            phase = 'idle'
        copy = voice_copy.card(phase, state=self._state, code=self._code, tool=self._tool,
                               slot=self._slot, rule=self._rule, cut=self._cut,
                               confirming=self._confirming, chip=self._chip)
        if phase == 'result' and age * 1000 > copy['dwell']:
            phase = 'idle'
            copy = voice_copy.card('idle', chip=self._chip)
        meter_ms = (self._deadline if phase == 'listening' else
                    max(0, self._ttl - 1000) if phase == 'confirm' else None)
        view = {'v': 1, 'seq': self._seq, 'phase': phase, 'ageMs': min(600000, int(age * 1000)),
                'tone': copy['tone'], 'word': copy['word'], 'headline': copy['headline'],
                'detail': copy['detail'], 'badge': copy['badge'],
                # A hold meter without a known deadline would be a guess: show none.
                'meter': None if phase == 'listening' and meter_ms is None else copy['meter'],
                'meterMs': meter_ms,
                'dwellMs': copy['dwell'] if phase == 'result' else self._ttl if phase == 'confirm' else None,
                'mood': copy['mood'], 'chip': self._chip}
        if _size(view) > HOME_MAX:
            copy = voice_copy.card('idle', chip=self._chip)
            view.update(phase='idle', tone=copy['tone'], word='', headline='', detail='', badge=None,
                        meter=None, meterMs=None, dwellMs=None, mood=copy['mood'])
        return view

    def home_view(self):
        with self._cond:
            return self._view(self.clock())

    def wait_home(self, since, *, timeout_s=HOLD_S, gone=None):
        """Home long-poll. Holds only while since == seq (since != 0), at most 20 s,
        one waiter at a time. Returns the view, or None once the client is gone."""
        since = since if type(since) is int and since >= 0 else 0
        deadline = time.monotonic() + max(0.0, min(HOLD_S, timeout_s))
        with self._cond:
            now = self.clock()
            self._home_gen += 1
            generation = self._home_gen
            self._home_at = now
            if 0 < since <= self._seq:
                self._home_seq = max(self._home_seq, since)  # implicit ack: Home polls only while visible
            self._expire(now)
            self._cond.notify_all()
            if since == 0 or since != self._seq or self._closed:
                return self._view(now)
            self._home_waiter = generation
        try:
            while True:
                with self._cond:
                    if self._closed or self._seq != since or self._home_gen != generation:
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    self._cond.wait(min(SLICE_S, remaining))
                    self._expire(self.clock())
                    if self._closed or self._seq != since or self._home_gen != generation:
                        break
                try:
                    if gone is not None and gone():
                        return None
                except Exception:
                    return None
        finally:
            with self._cond:
                if self._home_waiter == generation:
                    self._home_waiter = None
        with self._cond:
            now = self.clock()
            self._home_at = now
            return self._view(now)

    def viewer_card(self):
        """Home view minus chip plus the viewer-only search echo; None once closed."""
        with self._cond:
            if self._closed:
                return None
            now = self.clock()
            card = self._view(now)
            del card['chip']
            card['echo'] = (self._echo if card['phase'] == 'result' and self._echo is not None
                            and now < self._echo_until else None)
            if _size(card) > CARD_MAX:
                card['echo'] = None
            return card

    def note_viewer_delivered(self, seq):
        if type(seq) is not int:
            return
        with self._cond:
            if 0 < seq <= self._seq:
                self._viewer_seq = max(self._viewer_seq, seq)
                self._viewer_at = self.clock()
                self._cond.notify_all()

    def surface(self):
        with self._cond:
            return self._surface(self.clock())

    def _receipt(self, press, row, now):
        if row is None:
            return dict(schema=1, press=press, done=False, toast=None, shown=False), True
        shown = self._home_seq >= row['seq'] > 0 or self._viewer_seq >= row['seq'] > 0
        phase = 'confirm' if row['code'] == 'CONFIRMATION_PENDING' else 'result'
        toast = voice_copy.card(phase, state=row['state'], code=row['code'], tool=row['tool'],
                                slot=row['slot'], rule=row['rule'])['toast']
        # Wait for an ack only while an R2D2 surface is or is about to be visible.
        hopeless = (self._surface(now) == 'other' and not (
            row['tool'] in _SURFACING and row['code'] in ('TV_FOREGROUND_VERIFIED', 'BROWSER_NAVIGATION_ACCEPTED')))
        reply = dict(schema=1, press=press, done=True, toast=None if shown else toast, shown=shown)
        return reply, not (shown or toast is None or hopeless)

    def receipt(self, press, *, timeout_s=RECEIPT_S):
        """Resident toast decision for one press, held up to 1.5 s for the outcome
        and for a Home or viewer delivery ack."""
        if not _hex(press, 16):
            raise ValueError('VOICE_RECEIPT')
        deadline = time.monotonic() + max(0.0, min(RECEIPT_S, timeout_s))
        with self._cond:
            while True:
                reply, waiting = self._receipt(press, self._receipts.get(press), self.clock())
                remaining = deadline - time.monotonic()
                if not waiting or self._closed or remaining <= 0:
                    return reply
                self._cond.wait(min(SLICE_S, remaining))

    def wait_change(self, seen, timeout_s):
        """Serve-loop pacing: returns the current seq, early when it moves or on close."""
        with self._cond:
            if not self._closed:
                self._expire(self.clock())
            if not self._closed and seen == self._seq and timeout_s > 0:
                self._cond.wait(timeout_s)
                if not self._closed:
                    self._expire(self.clock())
            return self._seq

    def close(self):
        with self._cond:
            self._closed = True
            self._echo = self._u = None
            self._cond.notify_all()
