"""Closed voice UI copy: the only text Home, the viewer card and resident toasts show.

Inputs are enums only (phase, state, code, tool, slot, rule, cut). Nothing a
caller supplies is formatted in: {App}, {Site} and {n} come from the fixed tables
below, and an unknown tool or slot falls back to state-only copy. Display text is
printable ASCII plus ’ ‘ “ ”; toasts are printable ASCII, at most 60 characters.
An ellipsis is always three periods: the LG TV's fallback font draws U+2026
mid-line, and receivers still accept U+2026 from older Spark builds.
"""
import re

PHASES = ('idle', 'pressed', 'listening', 'released', 'doing', 'confirm', 'result')
STATES = ('verified', 'accepted', 'refused', 'rejected', 'unknown')
TOOLS = frozenset((
    'browser.open_site', 'browser.web_search', 'browser.youtube_search', 'browser.close',
    'control.back', 'control.play_pause', 'tv.home', 'tv.open_app', 'tv.open_content',
    'tv.volume', 'tv.mute', 'meta.whats_playing', 'meta.cancel', 'meta.unsupported'))
FAULTS = frozenset(('OWNER_KEY_RELEASED', 'START_UNCONFIRMED', 'CAPTURE_LOST', 'STOP_UNCONFIRMED',
                    'NO_SPEECH', 'BUSY', 'TRANSPORT_FAILED', 'RESULT_UNCONFIRMED'))
RULES = ('always', 'repeat', 'ceiling')
CHIPS = ('ready', 'warming', 'starting', 'unavailable', 'paused')
TONES = ('idle', 'ready', 'listen', 'work', 'done', 'sent', 'unsure', 'nothing', 'failed', 'ask')
MOODS = ('idle', 'front', 'listening', 'thinking', 'verified', 'asking', 'dim', 'sleepy')
BADGES = ('confirmed', 'sent', 'unconfirmed', 'nothing', 'changed', 'failed', 'waiting')
METERS = ('hold', 'sweep', 'countdown')
SLOT = re.compile(r'[a-z_]{1,16}(?::[a-z0-9_]{1,16}){0,2}')
CODE = re.compile(r'[A-Z][A-Z0-9_]{0,63}')
DISPLAY = re.compile('[ -~’‘“”]*')
TOAST = re.compile('[ -~]{1,60}')
WORD = re.compile("[A-Z' ]{0,20}")
HEADLINE_MAX, DETAIL_MAX = 40, 72

APPS = {'netflix': 'Netflix', 'apple_tv': 'Apple TV', 'youtube_app': 'YouTube',
        'prime_video': 'Prime Video', 'disney_plus': 'Disney+', 'hbo_max': 'HBO Max',
        'hulu': 'Hulu', 'sling': 'Sling TV', 'tubi': 'Tubi', 'apple_music': 'Apple Music'}
SITES = {'youtube': 'YouTube', 'duckduckgo': 'DuckDuckGo'}
SEARCHES = ('browser.web_search', 'browser.youtube_search')

_WORD = {'confirmed': 'DONE', 'sent': 'SENT', 'unconfirmed': 'NOT SURE', 'nothing': 'NOTHING DONE',
         'changed': 'NOTHING DONE', 'failed': "DIDN'T WORK", 'waiting': 'NEEDS YOUR OK'}
_TONE = {'confirmed': 'done', 'sent': 'sent', 'unconfirmed': 'unsure', 'nothing': 'nothing',
         'changed': 'nothing', 'failed': 'failed', 'waiting': 'ask'}
_BADGE = {'verified': 'confirmed', 'accepted': 'sent', 'unknown': 'unconfirmed',
          'refused': 'nothing', 'rejected': 'failed'}
_DWELL = {'confirmed': 4000, 'sent': 4000, 'unconfirmed': 6000}

_CANNOT = ('I’m sorry, I cannot do that.', '', 'nothing', "I'm sorry, I cannot do that")
_UNSURE = ('Not sure that worked.', 'Check the TV before you try again.', 'unconfirmed',
           'Sent, but not confirmed. Check the TV')
_HEARD = ('I didn’t hear anything.', 'Hold the mic button while you speak.', 'nothing',
          'Hold the microphone button while speaking')
_BUSY = ('Still on your last request.', 'Try again in a moment.', 'nothing',
         'Voice is busy, try again in a moment')
_OFF = ('Voice isn’t available right now.', 'Nothing was done. Try again in a few minutes.',
        'nothing', 'Voice command unavailable')
_CANCELLED = ('Cancelled.', 'Nothing was changed.', 'changed', 'Cancelled. Nothing changed')
_TV_REFUSED = ('The TV refused that.', 'Nothing changed.', 'failed', 'The TV refused that request')
_BROWSER_REFUSED = ('The browser didn’t accept that.', 'Nothing changed.', 'failed',
                    "The browser didn't accept that")
# code -> (headline, detail, badge or None for the state's badge, toast)
_CODES = {
    **dict.fromkeys(('UNSUPPORTED_COMMAND', 'TOOL_SCHEMA', 'ASR_OUTPUT_INVALID', 'TOOL_DISABLED',
                     'TV_TOOL_UNAVAILABLE', 'DEEPLINK_UNQUALIFIED',
                     'CONFIRMATION_SURFACE_UNAVAILABLE'), _CANNOT),
    'NEGATED_COMMAND': ('OK, I won’t.', '', 'nothing', 'OK, nothing done'),
    'AUDIO_EMPTY_OR_SHORT': _HEARD, 'NO_SPEECH': _HEARD,
    'OWNER_KEY_RELEASED': ('Hold the mic a little longer.', 'Keep holding until you finish speaking.',
                           'nothing', 'Hold the microphone button while speaking'),
    'START_UNCONFIRMED': ('The remote’s mic didn’t start.', 'Nothing was sent. Try again.', 'failed',
                          'Remote mic did not start. Try again'),
    'CAPTURE_LOST': ('Lost the remote’s mic.', 'Nothing was sent. Try again.', 'failed',
                     'Lost the remote mic. Nothing was sent'),
    'STOP_UNCONFIRMED': ('Voice is off.', 'Restart the TV to use voice.', 'failed',
                         'Voice is off. Restart the TV to use voice'),
    **dict.fromkeys(('BUSY', 'VOICE_BUSY', 'CONTROLLER_BUSY'), _BUSY),
    'VOICE_WARMING_UP': ('Voice is still warming up.', 'It needs about a minute after the TV wakes.',
                         'nothing', 'Voice model warming up - try again in a moment'),
    **dict.fromkeys(('VOICE_UNAVAILABLE', 'MODEL_UNAVAILABLE', 'ASR_UNAVAILABLE', 'ASR_UNQUALIFIED'), _OFF),
    'AGENT_PAUSED': ('Voice is paused.', 'Restart Spark to resume.', 'nothing',
                     'Voice control is paused. Restart Spark to resume'),
    'RATE_LIMITED': ('That’s a lot of requests.', 'Wait a few seconds, then try again.', 'nothing',
                     'Too many requests, wait a moment'),
    'TRANSPORT_FAILED': ('Can’t reach Spark.', 'Nothing was sent. Check that Spark is on.', 'failed',
                         "Can't reach Spark. Nothing was sent"),
    'BROWSER_NOT_OPENED': ('The R2D2 browser isn’t open.', 'Say “Open YouTube in the browser” first.',
                           'nothing', 'Open the R2D2 browser first'),
    'BROWSER_MEDIA_NO_VIDEO': ('There’s no video to pause here.', '', 'nothing',
                               'No video to play or pause here'),
    'CONFIRMATION_DECLINED': _CANCELLED, 'PENDING_CANCELLED': _CANCELLED,
    'CONFIRMATION_EXPIRED': ('That request expired.', 'Nothing was changed.', 'changed',
                             'Request expired. Nothing changed'),
    'NOTHING_PENDING': ('There’s nothing to cancel.', '', 'nothing', 'Nothing to cancel'),
    **dict.fromkeys(('NAVIGATION_REJECTED', 'HISTORY_REJECTED', 'MEDIA_REJECTED', 'END_REJECTED'),
                    _BROWSER_REFUSED),
    'TV_PERMISSION_DENIED': ('The TV blocked R2D2.', 'Re-pair R2D2 on Spark.', None,
                             'TV blocked R2D2. Re-pair on Spark'),
    'TV_CHANNEL_UNAVAILABLE': ('R2D2 can’t reach the TV controls.',
                               'Nothing was changed. Try again in a moment.', None,
                               "Can't reach TV controls right now"),
    **dict.fromkeys(('EXECUTION_UNCONFIRMED', 'REQUEST_EXPIRED', 'RESULT_UNCONFIRMED'), _UNSURE),
}
_MOOD = {'VOICE_WARMING_UP': 'sleepy', 'AGENT_PAUSED': 'dim',
         **dict.fromkeys(('VOICE_UNAVAILABLE', 'MODEL_UNAVAILABLE', 'ASR_UNAVAILABLE', 'ASR_UNQUALIFIED'), 'dim')}
_FALLBACK = {'verified': ('Done.', '', 'Done'), 'accepted': ('Sent.', '', 'Sent'),
             'refused': ('That didn’t work.', 'Nothing was changed.', 'Voice command unavailable'),
             'rejected': ('That didn’t work.', 'Nothing was changed.', 'Voice command unavailable')}
_CONFIRM_DETAIL = 'Hold the mic button and say “yes” or “no”.'
_TITLES = 'I can’t see titles yet.'


# Every slot the copy can name; anything else is treated as absent.
SLOTS = frozenset(
    ['app:' + key for key in APPS] + ['site:' + key for key in SITES]
    + ['media:' + value for value in ('play', 'pause', 'toggle')]
    + ['mute:on', 'mute:off', 'via:tv', 'via:browser']
    + ['volume:%s:%d' % (way, n) for way in ('up', 'down') for n in range(1, 6)]
    + ['on:' + key for key in (*APPS, 'r2d2_home', 'browser', 'other')]
    + ['page:yt_watch:playing', 'page:yt_watch:paused', 'page:yt_watch:unknown',
       'page:youtube', 'page:web'])


def _parts(tool, slot):
    return tuple(slot.split(':')) if tool is not None and type(slot) is str and slot in SLOTS else ()


def chip(warm, agent):
    if agent == 'paused':
        return 'paused'
    if warm in ('ready', 'grace'):
        return 'ready'
    if warm == 'warming':
        return 'warming'
    if warm in ('unloaded', None):
        return 'starting'
    return 'unavailable'


def _doing(tool, p):
    if tool == 'tv.open_app' and p[:1] == ('app',):
        return 'Opening %s...' % APPS[p[1]]
    if tool == 'tv.home':
        return 'Closing the browser...' if p == ('via', 'browser') else 'Going home...'
    if tool == 'tv.volume' and p[:1] == ('volume',):
        return 'Turning the volume %s %s...' % p[1:]
    if tool == 'tv.mute' and p[:1] == ('mute',):
        return 'Muting...' if p[1] == 'on' else 'Turning the sound on...'
    if tool == 'control.play_pause' and p[:1] == ('media',):
        return {'pause': 'Pausing...', 'play': 'Resuming...', 'toggle': 'Pausing or playing...'}[p[1]]
    if tool == 'browser.open_site' and p[:1] == ('site',):
        return 'Opening %s in the browser...' % SITES[p[1]]
    return {'browser.web_search': 'Searching the web...', 'browser.youtube_search': 'Searching YouTube...',
            'control.back': 'Going back...', 'browser.close': 'Closing the browser...',
            'meta.whats_playing': 'Checking what’s on...'}.get(tool, 'Working on it...')


def _action(tool, p, short=False):
    """Imperative for "{Action} again?"; short drops the browser suffix for toasts."""
    if tool == 'tv.open_app' and p[:1] == ('app',):
        return 'Open ' + APPS[p[1]]
    if tool == 'browser.open_site' and p[:1] == ('site',):
        return 'Open ' + SITES[p[1]] + ('' if short else ' in the browser')
    if tool == 'tv.volume' and p[:1] == ('volume',):
        return 'Turn the volume %s %s' % p[1:]
    if tool == 'tv.mute' and p[:1] == ('mute',):
        return 'Mute' if p[1] == 'on' else 'Turn the sound on'
    if tool == 'control.play_pause' and p[:1] == ('media',):
        return {'pause': 'Pause', 'play': 'Play', 'toggle': 'Play or pause'}[p[1]]
    if tool == 'tv.home':
        return 'Close the browser' if p == ('via', 'browser') else 'Go home'
    return {'control.back': 'Go back', 'browser.close': 'Close the browser',
            'browser.web_search': 'Search the web', 'browser.youtube_search': 'Search YouTube'}.get(tool)


def _confirm(tool, p, rule):
    """-> (headline, toast) for CONFIRMATION_PENDING."""
    if rule == 'ceiling' and tool == 'tv.volume' and p[:2] == ('volume', 'up'):
        return 'Turn the volume up %s?' % p[2], 'Turn volume up %s? Hold mic, say yes or no' % p[2]
    action = _action(tool, p) if rule == 'repeat' else None
    if action is not None:
        return action + ' again?', _action(tool, p, short=True) + ' again? Hold mic, say yes or no'
    return 'Should I go ahead?', 'Confirm? Hold mic, say yes or no'


def _success(state, code, tool, p):
    """Tool-specific verified/accepted copy -> (headline, detail, toast) or None."""
    if code == 'TV_FOREGROUND_VERIFIED' and state == 'verified':
        if tool == 'tv.open_app' and p[:1] == ('app',):
            return '%s is open.' % APPS[p[1]], '', None  # the app itself is the confirmation
        if tool == 'tv.home':
            return 'You’re home.', '', 'Back to R2D2 Home'
    if code == 'BROWSER_SESSION_ENDED' and tool in ('tv.home', 'browser.close'):
        if state == 'verified' and tool == 'browser.close':
            return 'Browser closed.', '', 'Browser closed'
        if state == 'accepted':
            return 'Closing the browser...', '', 'Closing the R2D2 browser'
    if tool == 'tv.volume' and p[:1] == ('volume',):
        if code == 'TV_VOLUME_VERIFIED' and state == 'verified':
            return 'Turned the volume %s %s.' % p[1:], '', 'Volume %s %s' % p[1:]
        if code == 'TV_VOLUME_ACCEPTED' and state == 'accepted':
            return ('Turning the volume %s %s...' % p[1:], 'Watch the TV’s volume bar.',
                    'Sent: volume %s %s' % p[1:])
    if tool == 'tv.mute' and p[:1] == ('mute',) and code == 'TV_MUTE_VERIFIED' and state == 'verified':
        return ('Muted.', '', 'Muted') if p[1] == 'on' else ('Sound is back on.', '', 'Sound on')
    if tool == 'control.play_pause' and p[:1] == ('media',):
        if code == 'BROWSER_MEDIA_VERIFIED' and state == 'verified':
            return {'pause': ('Paused.', '', 'Paused'), 'play': ('Playing.', '', 'Playing'),
                    'toggle': ('Done.', '', 'Done')}[p[1]]
        if code == 'TV_MEDIA_ACCEPTED' and state == 'accepted' and p[1] in ('pause', 'play'):
            return ('Sent “%s” to the app.' % p[1], 'Apps don’t report back.',
                    'Sent %s to the app' % p[1])
    if code == 'BROWSER_NAVIGATION_ACCEPTED' and state == 'accepted':
        if tool == 'browser.open_site' and p[:1] == ('site',):
            name = SITES[p[1]]
            return 'Opening %s in the browser...' % name, '', 'Opening %s in the R2D2 browser' % name
        if tool == 'browser.web_search':
            return 'Searching the web...', '', 'Searching the web in the R2D2 browser'
        if tool == 'browser.youtube_search':
            return 'Searching YouTube...', '', 'Searching YouTube in the R2D2 browser'
    if code == 'BROWSER_HISTORY_ACCEPTED' and state == 'accepted' and tool == 'control.back':
        return 'Going back...', '', 'Going back'
    if tool == 'meta.whats_playing' and state == 'accepted':
        if code == 'TV_STATUS_SHOWN' and p[:1] == ('on',):
            where = p[1]
            if where in APPS:
                return 'You’re watching %s.' % APPS[where], _TITLES, "You're in %s" % APPS[where]
            return {'r2d2_home': ('You’re on R2D2 Home.', '', "You're on R2D2 Home"),
                    'browser': ('You’re in the R2D2 browser.', '', "You're in the R2D2 browser"),
                    'other': ('Another app is open.', _TITLES, 'Another app is open')}[where]
        if code == 'BROWSER_STATUS_SHOWN' and p[:1] == ('page',):
            return {('yt_watch', 'playing'): ('A YouTube video is playing.', _TITLES, 'YouTube video playing'),
                    ('yt_watch', 'paused'): ('A YouTube video is paused.', _TITLES, 'YouTube video paused'),
                    ('yt_watch', 'unknown'): ('You’re watching YouTube.', _TITLES, "You're on YouTube"),
                    ('youtube',): ('You’re on YouTube.', '', "You're on YouTube"),
                    ('web',): ('You’re on a web page.', '', 'R2D2 browser is open')}[p[1:]]
    return None


def result(state, code, tool=None, slot=None):
    """-> (headline, detail, badge, toast, mood) for a final outcome."""
    state = state if type(state) is str and state in STATES else 'refused'
    code = code if type(code) is str and CODE.fullmatch(code) else 'VOICE_UNAVAILABLE'
    tool = tool if type(tool) is str and tool in TOOLS else None
    p = _parts(tool, slot)
    row = _CODES.get(code)
    if row is None and code == 'TV_APP_NOT_INSTALLED':
        name = APPS[p[1]] if p[:1] == ('app',) else None
        row = ((name or 'That app') + ' isn’t installed.', 'Install it on the TV, then try again.',
               'nothing', (name or 'That app') + " isn't installed on this TV")
    if row is None and (state == 'unknown' or code.endswith('_UNCONFIRMED')):
        row = _UNSURE
    if row is None and code.endswith('_REJECTED'):
        row = _TV_REFUSED
    if row is None:
        found = _success(state, code, tool, p) if state in ('verified', 'accepted') else None
        headline, detail, toast = found or _FALLBACK[state]
        row = (headline, detail, None, toast)
    headline, detail, badge, toast = row
    badge = badge or _BADGE[state]
    mood = _MOOD.get(code) or {'confirmed': 'verified', 'unconfirmed': 'asking'}.get(badge, 'idle')
    return headline, detail, badge, toast, mood


def card(phase, *, state=None, code=None, tool=None, slot=None, rule=None, cut=False,
         confirming=False, chip='ready'):
    """Display copy for one view: word, headline, detail, badge, tone, mood, meter,
    toast and the default result dwell (ms). The caller adds meterMs/dwellMs timing."""
    tool = tool if type(tool) is str and tool in TOOLS else None
    if phase == 'pressed':
        return dict(word='GETTING READY', headline='One moment...', detail='Keep holding the mic button.',
                    badge=None, tone='work', mood='front', meter=None, toast=None, dwell=None)
    if phase == 'listening':
        return dict(word='CONFIRMING' if confirming else 'LISTENING', headline='Listening...',
                    detail='Say “yes” or “no”, then let go.' if confirming else 'Speak, then let go.',
                    badge=None, tone='listen', mood='listening', meter='hold', toast=None, dwell=None)
    if phase == 'released':
        return dict(word='THINKING', headline='Got it.',
                    detail='That was the 9-second limit. Using what I heard.' if cut is True else 'Working on it...',
                    badge=None, tone='work', mood='thinking', meter='sweep', toast=None, dwell=None)
    if phase == 'doing':
        return dict(word='ON IT', headline=_doing(tool, _parts(tool, slot)), detail='', badge=None,
                    tone='work', mood='thinking', meter='sweep', toast=None, dwell=None)
    if phase == 'confirm':
        headline, toast = _confirm(tool, _parts(tool, slot), rule)
        return dict(word='NEEDS YOUR OK', headline=headline, detail=_CONFIRM_DETAIL, badge='waiting',
                    tone='ask', mood='asking', meter='countdown', toast=toast, dwell=None)
    if phase == 'result':
        headline, detail, badge, toast, mood = result(state, code, tool, slot)
        word = 'WARMING UP' if code == 'VOICE_WARMING_UP' else _WORD[badge]
        return dict(word=word, headline=headline, detail=detail, badge=badge, tone=_TONE[badge],
                    mood=mood, meter=None, toast=toast, dwell=_DWELL.get(badge, 5000))
    mood = {'warming': 'sleepy', 'starting': 'sleepy', 'unavailable': 'dim', 'paused': 'dim'}.get(chip, 'idle')
    return dict(word='', headline='', detail='', badge=None, tone='ready' if chip == 'ready' else 'idle',
                mood=mood, meter=None, toast=None, dwell=None)
