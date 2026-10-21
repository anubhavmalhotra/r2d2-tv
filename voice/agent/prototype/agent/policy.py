"""Rate limits, confirmation rules and auto-trip counters."""
from collections import deque
import threading

from .errors import Refused

# scope -> ((limit, window_seconds), ...). Units are calls, except volume_steps.
LIMITS = {
    'utterance': ((12, 60), (240, 3600)),   # counted at arm(), before any PCM/model work
    'dispatch': ((10, 60),),                # all dispatched changes
    'browser': ((6, 60),),                  # navigate/history/media
    'launch': ((4, 60),),                   # tv.open_app / tv home / open_browser
    'volume_steps': ((30, 60),),
    'mute': ((10, 60),),
    'play_pause': ((10, 60),),
    'back': ((10, 60),),
}
CONFIRM_TTL_S = 15
REPEAT_LIMIT, REPEAT_WINDOW_S = 3, 60      # "repeated more than 3 times in 60 s"
UNKNOWN_STREAK_TRIP = 3
RATE_HITS_TRIP, RATE_HITS_WINDOW_S = 3, 600
DEFAULT_VOLUME_CEILING = 35


class RateLimiter:
    def __init__(self, clock, limits=LIMITS):
        self.clock = clock
        self.limits = {scope: tuple(rules) for scope, rules in limits.items()}
        self.events = {scope: deque() for scope in self.limits}
        self.lock = threading.Lock()

    def _used(self, scope, window, now):
        return sum(units for at, units in self.events[scope] if now - at < window)

    def admit(self, scopes, units=1, *, record=True):
        """All-or-nothing: either every scope has room (and is recorded) or
        Refused('RATE_LIMITED') with nothing recorded."""
        now = self.clock()
        with self.lock:
            for scope in scopes:
                if scope not in self.limits:
                    raise Refused('TOOL_SCHEMA')
                cost = units if scope == 'volume_steps' else 1
                for limit, window in self.limits[scope]:
                    if self._used(scope, window, now) + cost > limit:
                        raise Refused('RATE_LIMITED')
            if record:
                longest = {s: max(w for _, w in self.limits[s]) for s in scopes}
                for scope in scopes:
                    queue = self.events[scope]
                    while queue and now - queue[0][0] >= longest[scope]:
                        queue.popleft()
                    queue.append((now, units if scope == 'volume_steps' else 1))


class Recent:
    """Timestamps in a sliding window (repeat rule, rate-hit trip)."""
    def __init__(self, clock, window):
        self.clock, self.window, self.items = clock, window, deque()

    def add(self, key=None):
        self.items.append((self.clock(), key))

    def count(self, key=None):
        now = self.clock()
        while self.items and now - self.items[0][0] >= self.window:
            self.items.popleft()
        return sum(1 for _, k in self.items if key is None or k == key)


def needs_confirmation(tool, plan, *, repeats, current_volume, ceiling):
    """-> None or the rule name. `repeats` = admitted calls of this tool in 60 s."""
    if tool.confirm == 'always':
        return 'always'
    if repeats >= REPEAT_LIMIT and tool.name not in ('meta.whats_playing', 'meta.cancel'):
        return 'repeat'
    if tool.confirm == 'policy' and plan.action == 'volume' and plan.param('direction') == 'up':
        # Unknown current volume fails safe: ask.
        if type(current_volume) is not int or current_volume + plan.param('steps') > ceiling:
            return 'ceiling'
    return None
