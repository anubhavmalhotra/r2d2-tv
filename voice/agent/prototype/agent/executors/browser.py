"""Browser executor over channel v2. P0 ships only an in-memory fake.

Channel v2 contract (to be implemented on Spark by the rebased browser_channel /
Trial control host; every call takes timeout_s <= 5 and never raises on a refusal):

  manager_state(*, timeout_s) -> {'state': 'idle'|'starting'|'ready'|'stopping',
        'session': 32-hex|None, 'claimed': bool, 'starts': int, 'cleanup': bool}
      `starts` counts session starts seen by the Manager, so owner_open can bind
      only to a start observed inside its own window [R]. `cleanup` is the
      verified-cleanup bit of the last stop, meaningful when idle.
  observe(session, *, timeout_s) -> {'ready': bool, 'matched': bool,
        'generation': int 0..256, 'surfaceClass': 'browser_youtube_watch'|
        'browser_youtube'|'browser_other', 'video': {'count': 0..4, 'paused': bool|None}}
      `matched` comes only from a fresh main-thread check of the owned page
      against the last navigate target, never from a queue ACK. No URL,
      title or page text is ever returned.
  navigate(session, op, url, *, timeout_s)      -> 'accepted'|'rejected'|'unknown'
  history(session, op, 'back', *, timeout_s)    -> same
  media_key(session, op, *, timeout_s)          -> same   (Trial main thread
      checks the owned page is youtube.com/watch in the same step [R])
  end(session, op, *, timeout_s)                -> same   (sets the viewer flag,
      waits <= 8 s for the viewer's own /end, then request_stop('agent-ended') [R])

The channel re-validates every URL against its own allowlist and refuses a
repeated op. This executor also re-validates (defense in depth).
"""
import re

from ..router import allowed_url
from .base import ExecResult

OWNER_OPEN_START_S = 60   # [R] wait for the TV's own open.js start
OWNER_OPEN_READY_S = 80   # then poll for ready + claimed
OWNER_OPEN_WINDOW_S = OWNER_OPEN_START_S + OWNER_OPEN_READY_S  # <= 140 s [D]
POLL_S = 0.5
NAV_VERIFY_S = 10
MEDIA_FLIP_S = 2
END_VERIFY_S = 10
CALL_TIMEOUT_S = 5

_SESSION = re.compile(r'[a-f0-9]{32}')
_PARAMS = {'navigate': {'url'}, 'history': {'action'}, 'media': {'want'}, 'end': {'reason'},
           'observe': set()}
_CLASSES = ('browser_youtube_watch', 'browser_youtube', 'browser_other')


def _state_ok(v):
    return (type(v) is dict and set(v) == {'state', 'session', 'claimed', 'starts', 'cleanup'} and
            v['state'] in ('idle', 'starting', 'ready', 'stopping') and
            (v['session'] is None or (type(v['session']) is str and _SESSION.fullmatch(v['session']))) and
            type(v['claimed']) is bool and type(v['starts']) is int and v['starts'] >= 0 and
            type(v['cleanup']) is bool)


def _obs_ok(v):
    if (type(v) is not dict or set(v) != {'ready', 'matched', 'generation', 'surfaceClass', 'video'} or
            type(v['ready']) is not bool or type(v['matched']) is not bool or
            type(v['generation']) is not int or not 0 <= v['generation'] <= 256 or
            v['surfaceClass'] not in _CLASSES):
        return False
    video = v['video']
    return (type(video) is dict and set(video) == {'count', 'paused'} and
            type(video['count']) is int and 0 <= video['count'] <= 4 and
            (video['paused'] is None or type(video['paused']) is bool))


class BrowserExecutor:
    def __init__(self, channel, *, clock, wait):
        self.channel, self.clock, self.wait = channel, clock, wait
        self.operations = set()
        self._gate = None

    # -- reads (never mutate) ---------------------------------------------------
    def _timeout(self, deadline):
        return max(0.001, min(CALL_TIMEOUT_S, deadline - self.clock()))

    def _state(self, deadline):
        try:
            value = self.channel.manager_state(timeout_s=self._timeout(deadline))
        except Exception:
            return None
        return value if _state_ok(value) else None

    def _observe(self, session, deadline):
        try:
            value = self.channel.observe(session, timeout_s=self._timeout(deadline))
        except Exception:
            return None
        return value if _obs_ok(value) else None

    def _newer(self, session, before, deadline):
        obs = self._observe(session, deadline)
        return obs if obs is not None and obs['generation'] > before['generation'] else None

    def _claimed(self, deadline):
        st = self._state(deadline)
        if st and st['state'] == 'ready' and st['claimed'] and st['session']:
            return st['session']
        return None

    def surface(self, deadline):
        """Trusted surface for the router: Manager state + owned-page class only."""
        session = self._claimed(deadline)
        if session is None:
            return 'browser_none'
        obs = self._observe(session, deadline)
        return obs['surfaceClass'] if obs else 'browser_none'

    def _poll(self, deadline, seconds, read, done):
        end = min(deadline, self.clock() + seconds)
        while True:
            if self._gate is not None and self._gate() is not None:
                return None
            value = read()
            if value is not None and done(value):
                return value
            if self._gate is not None and self._gate() is not None:
                return None
            if self.clock() + POLL_S > end:
                return None
            self.wait(POLL_S)

    def _owner_open(self, deadline):
        """[R] Bind only to a session whose start is observed inside the window."""
        st = self._state(deadline)
        if st is None:
            return None
        if st['state'] == 'ready' and st['claimed'] and st['session']:
            return st['session']
        if st['state'] != 'idle':
            return None  # starting before admission, unclaimed or stopping: never bind
        base = st['starts']
        started = self._poll(deadline, OWNER_OPEN_START_S, lambda: self._state(deadline),
                             lambda s: s['starts'] > base)
        if started is None or started['starts'] != base + 1 or started['session'] is None:
            return None
        bound, starts = started['session'], started['starts']
        ready = self._poll(deadline, OWNER_OPEN_READY_S, lambda: self._state(deadline),
                           lambda s: (s['starts'] != starts or s['session'] != bound or
                                      (s['state'] == 'ready' and s['claimed'])))
        if ready is None or ready['starts'] != starts or ready['session'] != bound:
            return None
        return bound

    # -- plan validation ----------------------------------------------------------
    def _valid(self, plan):
        if plan.executor != 'browser' or plan.action not in _PARAMS:
            return False
        params = dict(plan.params)
        if set(params) != _PARAMS[plan.action]:
            return False
        if (type(plan.op) is not str or
                not re.fullmatch(r'[a-f0-9]{32}:' + plan.action, plan.op) or
                plan.op in self.operations or len(self.operations) >= 4096):
            return False
        if plan.action == 'navigate':
            return allowed_url(params['url'])
        if plan.action == 'history':
            return params['action'] == 'back'
        if plan.action == 'media':
            return params['want'] in ('play', 'pause', 'toggle')
        if plan.action == 'end':
            return params['reason'] in ('close', 'home')
        return True

    # -- execution ------------------------------------------------------------------
    def _dispatch(self, method, *args, deadline):
        try:
            result = method(*args, timeout_s=self._timeout(deadline))
        except Exception:
            return 'unknown'
        return result if result in ('accepted', 'rejected', 'unknown') else 'invalid'

    def run(self, plan, *, gate, deadline):
        if not self._valid(plan):
            return ExecResult('refused', 'TOOL_SCHEMA', False)
        prev_gate = self._gate
        self._gate = gate
        try:
            action = plan.action
            session = self._owner_open(deadline) if action == 'navigate' else self._claimed(deadline)
            if session is None:
                blocked = gate()
                if blocked is not None:
                    return ExecResult('refused', blocked, False)
                return ExecResult('refused', 'BROWSER_NOT_OPENED', False)
            if action == 'observe':
                obs = self._observe(session, deadline)
                if obs is None:
                    return ExecResult('refused', 'BROWSER_NOT_OPENED', False)
                return ExecResult('accepted', 'BROWSER_STATUS_SHOWN', False,
                                  (('surface', obs['surfaceClass']), ('paused', obs['video']['paused'])))
            before = self._observe(session, deadline)
            if before is None:
                return ExecResult('refused', 'BROWSER_NOT_OPENED', False)
            if action == 'media':
                return self._media(plan, session, before, gate, deadline)
            blocked = gate()
            if blocked is not None:
                return ExecResult('refused', blocked, False)
            self.operations.add(plan.op)  # consumed before dispatch: never repeated
            if action == 'navigate':
                sent = self._dispatch(self.channel.navigate, session, plan.op, plan.param('url'),
                                      deadline=deadline)
                if sent == 'rejected':
                    return ExecResult('rejected', 'NAVIGATION_REJECTED', True)
                # `matched` is relative to the last *accepted* target
                # (browser_channel.py bumps `generation` on every accepted
                # navigate), so require a new generation too: a lost or dropped navigate
                # must not inherit the previous command's match.
                found = sent != 'invalid' and self._poll(
                    deadline, NAV_VERIFY_S, lambda: self._newer(session, before, deadline),
                    lambda o: o['ready'] and o['matched'])
                return (ExecResult('accepted', 'BROWSER_NAVIGATION_ACCEPTED', True) if found
                        else ExecResult('unknown', 'NAVIGATION_UNCONFIRMED', True))
            if action == 'history':
                sent = self._dispatch(self.channel.history, session, plan.op, 'back', deadline=deadline)
                if sent == 'rejected':
                    return ExecResult('rejected', 'HISTORY_REJECTED', True)
                found = sent != 'invalid' and self._poll(
                    deadline, NAV_VERIFY_S, lambda: self._observe(session, deadline),
                    lambda o: o['generation'] > before['generation'])
                return (ExecResult('accepted', 'BROWSER_HISTORY_ACCEPTED', True) if found
                        else ExecResult('unknown', 'HISTORY_UNCONFIRMED', True))
            # end
            st0 = self._state(deadline)
            blocked = gate() or ('REQUEST_EXPIRED' if self.clock() >= deadline else None)
            if blocked is not None:
                return ExecResult('refused', blocked, False)
            sent = self._dispatch(self.channel.end, session, plan.op, deadline=deadline)
            if sent == 'rejected':
                return ExecResult('rejected', 'END_REJECTED', True)
            base = st0['starts'] if st0 else -1
            done = sent != 'invalid' and self._poll(
                deadline, END_VERIFY_S, lambda: self._state(deadline),
                lambda s: s['state'] == 'idle' and s['cleanup'] and s['starts'] == base)
            if not done:
                return ExecResult('unknown', 'END_UNCONFIRMED', True)
            # Session end is verified; Home foreground is not observable before P3.
            state = 'verified' if plan.param('reason') == 'close' else 'accepted'
            return ExecResult(state, 'BROWSER_SESSION_ENDED', True)
        finally:
            self._gate = prev_gate

    def _media(self, plan, session, before, gate, deadline):
        video = before['video']
        if (before['surfaceClass'] != 'browser_youtube_watch' or video['count'] < 1 or
                type(video['paused']) is not bool):
            return ExecResult('refused', 'BROWSER_MEDIA_NO_VIDEO', False)
        want = plan.param('want')
        target = {'play': False, 'pause': True, 'toggle': not video['paused']}[want]
        if video['paused'] == target:
            return ExecResult('verified', 'BROWSER_MEDIA_VERIFIED', False)  # read matches; no key sent
        blocked = gate()
        if blocked is not None:
            return ExecResult('refused', blocked, False)
        self.operations.add(plan.op)
        sent = self._dispatch(self.channel.media_key, session, plan.op, deadline=deadline)
        if sent == 'rejected':
            return ExecResult('rejected', 'MEDIA_REJECTED', True)
        flipped = sent != 'invalid' and self._poll(
            deadline, MEDIA_FLIP_S, lambda: self._observe(session, deadline),
            lambda o: o['video']['paused'] is target)
        return (ExecResult('verified', 'BROWSER_MEDIA_VERIFIED', True) if flipped
                else ExecResult('unknown', 'MEDIA_UNCONFIRMED', True))


class FakeBrowserChannel:
    """In-memory stand-in for channel v2 + Manager. Its URL allowlist is written
    independently of router.allowed_url (the third check lives in other code)."""
    _GOOGLE = re.compile(r'https://www\.google\.com/search\?q=[A-Za-z0-9%._~-]{1,1500}')
    _DDG = re.compile(r'https://duckduckgo\.com/\?q=[A-Za-z0-9%._~-]{1,1500}')
    _YT = re.compile(r'https://www\.youtube\.com/results\?search_query=[A-Za-z0-9%+._~-]{1,1500}')
    _EXACT = ('https://www.youtube.com', 'https://duckduckgo.com')

    def __init__(self, clock):
        self.clock = clock
        self.state, self.session, self.claimed, self.starts, self.cleanup = 'idle', None, False, 0, True
        self.tile_at = None          # clock time at which the owner presses the browser tile
        self.ready_after = 3.0
        self.claim_on_ready = True
        self.page = None
        self.generation = 0
        self.surface_class = 'browser_other'
        self.video_count, self.paused = 0, None
        self.results = {}            # action -> forced 'accepted'|'rejected'|'unknown'|other
        self.effects = {'navigate': True, 'history': True, 'media_key': True, 'end': True}
        self.raise_on = set()
        self.ops = set()
        self.calls = []              # (action, op[, url]) - no transcript ever reaches here
        self._started_at = None
        self._target = None

    def open_session(self, *, claimed=True, surface='browser_other', video=0, paused=None):
        self.starts += 1
        self.state, self.session, self.claimed = 'ready', '%032x' % (0xabc0 + self.starts), claimed
        self.surface_class, self.video_count, self.paused = surface, video, paused

    def _tick(self):
        now = self.clock()
        if self.state == 'idle' and self.tile_at is not None and now >= self.tile_at:
            self.tile_at = None
            self.starts += 1
            self.state, self.session, self.claimed = 'starting', '%032x' % (0xabc0 + self.starts), False
            self._started_at = now
        if self.state == 'starting' and now - self._started_at >= self.ready_after:
            self.state, self.claimed = 'ready', self.claim_on_ready

    def _check(self, name):
        if name in self.raise_on:
            raise RuntimeError('channel failure')

    def manager_state(self, *, timeout_s):
        self._check('manager_state')
        self._tick()
        return {'state': self.state, 'session': self.session, 'claimed': self.claimed,
                'starts': self.starts, 'cleanup': self.cleanup}

    def observe(self, session, *, timeout_s):
        self._check('observe')
        self._tick()
        if session != self.session or self.state != 'ready':
            return None
        return {'ready': True, 'matched': self.page is not None and self.page == self._target,
                'generation': self.generation, 'surfaceClass': self.surface_class,
                'video': {'count': self.video_count, 'paused': self.paused}}

    def _mutate(self, action, session, op, extra=()):
        self.calls.append((action, op) + tuple(extra))
        self._check(action)
        if session != self.session or self.state != 'ready' or op in self.ops:
            return 'rejected'
        self.ops.add(op)
        return self.results.get(action, 'accepted')

    def navigate(self, session, op, url, *, timeout_s):
        search_ok = bool(self._DDG.fullmatch(url) or self._GOOGLE.fullmatch(url)) and not re.search(r'%21|%5C|%EF%BC%81|%EF%BC%BC|%EF%B9%97', url, re.I)
        if not (url in self._EXACT or search_ok or self._YT.fullmatch(url)):
            self.calls.append(('navigate', op, url))
            return 'rejected'
        result = self._mutate('navigate', session, op, (url,))
        if result in ('accepted', 'unknown') and self.effects['navigate']:
            self._target, self.page = url, url
            self.generation += 1
            self.surface_class = 'browser_youtube' if 'youtube' in url else 'browser_other'
        return result

    def history(self, session, op, action, *, timeout_s):
        result = self._mutate('history', session, op)
        if result in ('accepted', 'unknown') and self.effects['history']:
            self.generation += 1
        return result

    def media_key(self, session, op, *, timeout_s):
        result = self._mutate('media_key', session, op)
        if result in ('accepted', 'unknown') and self.effects['media_key'] and self.paused is not None:
            self.paused = not self.paused
        return result

    def end(self, session, op, *, timeout_s):
        result = self._mutate('end', session, op)
        if result in ('accepted', 'unknown') and self.effects['end']:
            self.state, self.session, self.claimed, self.cleanup = 'idle', None, False, True
            self.page = self._target = None
        return result
