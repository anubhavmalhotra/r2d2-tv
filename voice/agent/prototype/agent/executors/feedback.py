"""Fixed per-code messages and the TV feedback surfaces.

Surfaces:
  * viewer overlay - R2D2 browser in front; viewer.js reads `agent:{seq,code,text}`
    from its heartbeat reply (Trial + viewer change). May echo the query
    (after Cf removal); shown on the TV only, never logged.
  * SSAP toast (P4) - fixed template only, never free text [R] ([H] webOS may keep
    toast history).
Confirmation cards that show free text (tv.open_content title) need the overlay;
a toast can carry only enum-only confirmations (volume ceiling).
"""
from ..registry import at_least
from ..router import BROWSER_FRONT

TEMPLATES = {
    'BROWSER_NAVIGATION_ACCEPTED': 'Opening in the R2D2 browser',
    'BROWSER_HISTORY_ACCEPTED': 'Going back',
    'BROWSER_MEDIA_VERIFIED': 'Done',
    'BROWSER_SESSION_ENDED': 'Browser closed',
    'BROWSER_STATUS_SHOWN': 'Here is what is playing',
    'BROWSER_NOT_OPENED': 'Open the R2D2 browser first',
    'BROWSER_MEDIA_NO_VIDEO': 'No video to play or pause',
    'TV_FOREGROUND_VERIFIED': 'Opened',
    'TV_VOLUME_ACCEPTED': 'Volume changed',
    'TV_VOLUME_VERIFIED': 'Volume changed',
    'TV_MUTE_VERIFIED': 'Done',
    'TV_MEDIA_ACCEPTED': 'Done',
    'TV_STATUS_SHOWN': 'Here is what is on',
    'TV_APP_NOT_INSTALLED': 'That app is not installed',
    'TV_CHANNEL_UNAVAILABLE': 'TV controls are unavailable',
    'TV_TOOL_UNAVAILABLE': 'That is not available yet',
    'CONFIRMATION_PENDING': 'Say yes to confirm, or no',
    'CONFIRMATION_DECLINED': 'Cancelled',
    'CONFIRMATION_EXPIRED': 'Confirmation timed out',
    'PENDING_CANCELLED': 'Cancelled',
    'AGENT_PAUSED': 'Voice control is paused',
    'VOICE_WARMING_UP': 'Voice is warming up, try again shortly',   # warm.py
    'VOICE_UNAVAILABLE': 'Voice is not available right now',
    'RATE_LIMITED': 'Too many requests, wait a moment',
    'UNSUPPORTED_COMMAND': 'Sorry, I cannot do that',
    'NEGATED_COMMAND': 'OK, nothing done',
    'TOOL_SCHEMA': 'Sorry, I cannot do that',
    'TOOL_DISABLED': 'That is not available yet',
}
GENERIC = 'Not sure that worked'
assert all(len(text) <= 60 for text in TEMPLATES.values()) and len(GENERIC) <= 60


def template(code):
    return TEMPLATES.get(code, GENERIC)


class Feedback:
    """overlay(code, text) -> bool and toast(code) -> bool are injected. Neither is
    ever given to the audit log."""
    ECHO_CODES = ('BROWSER_NAVIGATION_ACCEPTED', 'CONFIRMATION_PENDING')

    def __init__(self, phase, *, overlay=None, toast=None):
        self.phase, self.overlay, self.toast = phase, overlay, toast

    def _overlay_up(self, surface):
        return self.overlay is not None and surface in BROWSER_FRONT

    def _toast_up(self):
        return self.toast is not None and at_least(self.phase, 'P4')

    def can_confirm(self, surface, *, free_text):
        if not at_least(self.phase, 'P2'):
            return False  # confirmations need the fast path [R]
        return self._overlay_up(surface) or (not free_text and self._toast_up())

    def show(self, code, surface, *, echo=None):
        try:
            if self._overlay_up(surface):
                text = template(code)
                if echo is not None and code in self.ECHO_CODES:
                    text = text + ': ' + echo
                return self.overlay(code, text) is True
            if self._toast_up():
                return self.toast(code) is True
        except Exception:
            return False
        return False


class FakeOverlay:
    def __init__(self):
        self.shown = []

    def __call__(self, code, text):
        self.shown.append((code, text))
        return True
