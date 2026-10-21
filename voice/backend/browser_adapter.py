"""Trusted in-process hooks to reviewed Manager.start and Trial.queue_navigation.

No HTTP peer masquerading, direct CDP target selection or TV app-launch fallback.
The current manager runs Trial in a subprocess; a reviewed control channel and
fresh presentation observer are still required before activating this adapter.
"""
import re
import threading
from controller import BrowserObservation, Refused


class OwnedBrowserAdapter:
    apple_title_enabled = False

    def __init__(self, manager, binding, observation, *, qualified=False):
        self.manager, self.binding, self.observation = manager, binding, observation
        self.browser_enabled = qualified is True
        self.lock = threading.Lock()
        self.operation_ids = set()

    def launch_browser(self, operation, *, timeout_s):
        if not self.browser_enabled:
            return 'rejected'
        with self.lock:
            if not self._consume(operation, ':start'):
                return 'rejected'
            state = self.manager.status()
            if state == {'state': 'ready'}:
                return 'accepted'
            if state != {'state': 'idle'}:
                return 'rejected'
            status, _ = self.manager.start()
            return 'accepted' if status == 202 else 'rejected'

    def _consume(self, operation, suffix):
        if (type(operation) is not str or not re.fullmatch('[a-f0-9]{32}' + suffix, operation) or
                operation in self.operation_ids or len(self.operation_ids) >= 256):
            return False
        self.operation_ids.add(operation)
        return True

    def browser_observation(self, *, timeout_s):
        if not self.browser_enabled:
            return None
        row = self.observation(timeout_s=timeout_s)
        # The trusted observer must prove current owned target and current TV
        # presentation; the queued-navigation acknowledgement never substitutes.
        if type(row) is not BrowserObservation:
            return None
        return row

    def navigate(self, session, url, operation, *, timeout_s):
        if not self.browser_enabled:
            return 'rejected'
        with self.lock:
            if not self._consume(operation, ':navigate'):
                return 'rejected'
            row = self.browser_observation(timeout_s=timeout_s)
            if (row is None or row.session != session or row.owned is not True or
                    row.ready is not True):
                return 'rejected'
            bound = self.binding(session, timeout_s=timeout_s)
            if (type(bound) is not tuple or len(bound) != 2 or bound[0] != session or
                    not hasattr(bound[1], 'queue_navigation')):
                return 'rejected'
            return 'accepted' if bound[1].queue_navigation({'url': url}) is True else 'rejected'
