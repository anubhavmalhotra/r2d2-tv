"""Owned child channel plus an explicit TV viewer handoff; disabled by default.

Navigation acceptance and a live owned target do not prove TV presentation.
"""
import math
import re
import threading
import time

if __package__:
    from .controller import Refused
else:
    from controller import Refused


def current(check):
    # Trusted invocation predicate only; a failed check never grants authority.
    if check is None:return True
    try:return callable(check) and check() is True
    except Exception:return False

def deadline(timeout_s):
    if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 5):
        raise Refused('CONTROL_TIMEOUT')
    return time.monotonic() + timeout_s


class ChannelBrowserAdapter:
    apple_title_enabled = False

    def __init__(self, manager, control_spawn, presentation=None, *, viewer=None, qualified=False, require_idle=False):
        if type(require_idle) is not bool: raise ValueError("CONTROL_IDLE_POLICY")
        self.require_idle = require_idle
        self.manager, self.control_spawn = manager, control_spawn
        self.presentation, self.viewer = presentation, viewer
        self.browser_enabled = (qualified is True and control_spawn is not None and
                                viewer is not None and viewer.enabled is True)
        self.operations = set()
        self.lock = threading.Lock()
        self.identity = None
        self._starts = 0
        self._last_session_id = None
        self._auto_open = None
        self._active_nonce = None
        self._active_is_current = None
        self._dispatched_nonce = None

    def bind_invocation(self, nonce, *, is_current=None):
        with self.lock:
            self._active_nonce = nonce
            self._active_is_current = is_current
            self._dispatched_nonce = None

    def arm_auto_open(self, operation, *, is_current=None, nonce=None):
        with self.lock:
            if nonce is not None and self._active_nonce is not None and nonce != self._active_nonce:
                return
            self._auto_open = {
                'operation': operation,
                'is_current': is_current if is_current is not None else self._active_is_current,
                'nonce': nonce if nonce is not None else self._active_nonce,
                'idle_reads': 0,
            }

    def clear_auto_open(self, nonce=None, *, keep_invocation=False):
        with self.lock:
            if nonce is None or (self._auto_open is not None and self._auto_open.get('nonce') == nonce):
                self._auto_open = None
            if not keep_invocation and (nonce is None or self._active_nonce == nonce):
                self._active_nonce = None
                self._active_is_current = None
                self._dispatched_nonce = None

    def was_dispatched(self, nonce):
        with self.lock:
            return nonce is not None and self._dispatched_nonce == nonce

    def launch_browser(self, operation, *, timeout_s, is_current=None):
        if (not self.browser_enabled or type(timeout_s) not in (int, float) or
                not math.isfinite(timeout_s) or not 0 < timeout_s <= 5 or
                type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:start', operation)):
            return 'rejected'
        end = deadline(timeout_s)
        if not self.lock.acquire(timeout=max(0, end-time.monotonic())):
            return 'rejected'
        created, identity, viewer_url = False, None, None
        try:
            if is_current is None:
                is_current = self._active_is_current
            if operation in self.operations or len(self.operations) >= 256:
                return 'rejected'
            self.operations.add(operation)
            self.identity = None
            if not self.manager.lock.acquire(timeout=max(0, end-time.monotonic())):
                return 'rejected'
            try:
                if time.monotonic() >= end or not current(is_current):
                    return 'rejected'
                state = self.manager.status()
                if state == {'state': 'idle'}:
                    status, body = self.manager.start()
                    if status != 202:
                        return 'rejected'
                    created = True
                    self._dispatched_nonce = self._active_nonce
                    identity = body.get('id')
                elif state == {'state': 'ready'} and not self.require_idle:
                    session = self.manager.session
                    # Do not steal another /open page's pending claim.
                    if not session or session.get('claimed') is not True:
                        return 'rejected'
                    identity, viewer_url = session.get('id'), session.get('url')
                    if type(viewer_url) is not str:
                        return 'rejected'
                else:
                    return 'rejected'
                if type(identity) is not str or not re.fullmatch('[a-f0-9]{32}', identity):
                    return 'unknown' if created else 'rejected'
                self.identity = identity
                if identity != self._last_session_id:
                    self._last_session_id = identity
                    self._starts += 1
            finally:
                self.manager.lock.release()
            remaining = end-time.monotonic()
            if remaining <= 0:
                return 'unknown' if created else 'rejected'
            if not created and self.control_spawn.client(identity, timeout_s=remaining) is None:
                self.identity = None
                return 'rejected'
            remaining = end-time.monotonic()
            if remaining <= 0:
                return 'unknown'
            try:
                if not current(is_current):return 'unknown' if created else 'rejected'
                self._dispatched_nonce = self._active_nonce
                outcome = self.viewer.open(identity, operation, viewer_url=viewer_url, timeout_s=remaining)
            except Exception:
                outcome = 'unknown'
            if outcome == 'rejected' and created:
                # No TV handoff took place, but starting the owned child did.
                # Exact-id stop only; a changed session must never be stopped.
                if self.manager.lock.acquire(timeout=max(0, end-time.monotonic())):
                    try:
                        if time.monotonic() < end:
                            self.manager.command('stop', {'id': identity})
                    finally:
                        self.manager.lock.release()
                self.identity = None
                return 'unknown'
            if outcome == 'rejected':
                self.identity = None
            return outcome if outcome in ('accepted', 'rejected', 'unknown') else 'unknown'
        finally:
            self.lock.release()

    def manager_state(self, *, timeout_s):
        if not self.browser_enabled:
            return None
        end = deadline(timeout_s)
        auto_launch = None
        if not self.lock.acquire(timeout=max(0, end-time.monotonic())):
            return None
        try:
            if self._auto_open is not None and getattr(self.manager, 'state', None) == 'idle':
                self._auto_open['idle_reads'] += 1
                if self._auto_open['idle_reads'] >= 2:
                    auto_launch = self._auto_open
                    self._auto_open = None
        finally:
            self.lock.release()
        if auto_launch is not None:
            remaining = end - time.monotonic()
            if remaining > 0:
                starts_before = self._starts
                outcome = self.launch_browser(auto_launch['operation'], timeout_s=remaining,
                                              is_current=auto_launch['is_current'])
                if outcome == 'rejected' and self._starts == starts_before:
                    with self.lock:
                        self._starts += 1
        if not self.lock.acquire(timeout=max(0, end-time.monotonic())):
            return None
        try:
            if not self.manager.lock.acquire(timeout=max(0, end-time.monotonic())):
                return None
            try:
                status_fn = getattr(self.manager, 'status', None)
                if callable(status_fn):
                    try:
                        status_fn()
                    except Exception:
                        pass
                st = self.manager.state
                if st not in ('idle', 'starting', 'ready', 'stopping'):
                    return None
                session = self.manager.session
                sid = (session.get('id') if type(session) is dict and
                       type(session.get('id')) is str and re.fullmatch('[a-f0-9]{32}', session['id'])
                       else None)
                claimed = bool(type(session) is dict and session.get('claimed') is True)
                if sid is not None and sid != self._last_session_id:
                    self._last_session_id = sid
                    self._starts += 1
                elif sid is None:
                    self._last_session_id = None
                if st == 'ready' and claimed and sid is not None and not self.require_idle:
                    self.identity = sid
                return {'state': st, 'session': sid, 'claimed': claimed,
                        'starts': self._starts, 'cleanup': st == 'idle'}
            finally:
                self.manager.lock.release()
        finally:
            self.lock.release()

    def _current(self, end, session_hint=None):
        if not self.lock.acquire(timeout=max(0, end-time.monotonic())):
            return None
        try:
            identity = self.identity
            if identity is None and session_hint is not None and not self.require_idle:
                identity = session_hint
        finally:
            self.lock.release()
        if identity is None or not self.manager.lock.acquire(timeout=max(0, end-time.monotonic())):
            return None
        try:
            session = self.manager.session
            if (not session or session.get('id') != identity or self.manager.state != 'ready' or
                    session.get('claimed') is not True):
                return None
        finally:
            self.manager.lock.release()
        remaining = end-time.monotonic()
        return self.control_spawn.client(identity, timeout_s=remaining) if remaining > 0 else None

    def browser_observation(self, *, timeout_s):
        if not self.browser_enabled:
            return None
        end = deadline(timeout_s)
        client = self._current(end)
        remaining = end-time.monotonic()
        return client.observe(timeout_s=remaining, presentation=self.presentation) if client is not None and remaining > 0 else None

    def observe(self, session, *, timeout_s):
        if not self.browser_enabled or type(session) is not str or not re.fullmatch('[a-f0-9]{32}', session):
            return None
        with self.lock:
            active_check = self._active_is_current
        if active_check is not None and not current(active_check):
            return None
        end = deadline(timeout_s)
        client = self._current(end, session_hint=session)
        remaining = end-time.monotonic()
        if client is None or client.session != session or remaining <= 0:
            return None
        obs = client.observe(session, timeout_s=remaining)
        if type(obs) is dict and not obs.get('ready') and hasattr(client, 'request'):
            wait_end = min(end, time.monotonic() + 0.6)
            while type(obs) is dict and not obs.get('ready') and time.monotonic() + 0.02 < wait_end:
                if active_check is not None and not current(active_check):
                    return None
                time.sleep(min(0.05, max(0.0, wait_end - time.monotonic())))
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                obs = client.observe(session, timeout_s=remaining)
        return obs

    def navigate(self, session, second, third, *, timeout_s, is_current=None):
        if not self.browser_enabled:
            return 'rejected'
        if is_current is None:
            with self.lock:
                is_current = self._active_is_current
        if type(second) is str and second.endswith(':navigate'):
            operation, url = second, third
        else:
            url, operation = second, third
        end = deadline(timeout_s)
        client = self._current(end, session_hint=session)
        remaining = end-time.monotonic()
        if client is None or client.session != session or remaining <= 0 or not current(is_current):
            return 'rejected'
        with self.lock:
            self._dispatched_nonce = self._active_nonce
        return client.queue_navigation({'url': url}, operation=operation, timeout_s=remaining)

    def history(self, session, operation, action, *, timeout_s, is_current=None):
        if not self.browser_enabled or action != 'back':
            return 'rejected'
        if is_current is None:
            with self.lock:
                is_current = self._active_is_current
        end = deadline(timeout_s)
        client = self._current(end, session_hint=session)
        remaining = end-time.monotonic()
        if client is None or client.session != session or remaining <= 0 or not current(is_current): return 'rejected'
        with self.lock:
            self._dispatched_nonce = self._active_nonce
        return client.history(operation, action, timeout_s=remaining)

    def media_key(self, session, operation, *, timeout_s, is_current=None):
        if not self.browser_enabled:
            return 'rejected'
        if is_current is None:
            with self.lock:
                is_current = self._active_is_current
        end = deadline(timeout_s)
        client = self._current(end, session_hint=session)
        remaining = end-time.monotonic()
        if client is None or client.session != session or remaining <= 0 or not current(is_current): return 'rejected'
        with self.lock:
            self._dispatched_nonce = self._active_nonce
        return client.media_key(operation, timeout_s=remaining)

    def end(self, session, operation, *, timeout_s, is_current=None):
        if not self.browser_enabled or type(operation) is not str or not re.fullmatch('[a-f0-9]{32}:end', operation):
            return 'rejected'
        if is_current is None:
            with self.lock:
                is_current = self._active_is_current
        end = deadline(timeout_s)
        client = self._current(end, session_hint=session)
        remaining = end-time.monotonic()
        if client is None or client.session != session or remaining <= 0 or not current(is_current): return 'rejected'
        with self.lock:
            self._dispatched_nonce = self._active_nonce
        return client.end(operation, timeout_s=remaining)
