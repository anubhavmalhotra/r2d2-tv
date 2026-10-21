"""ToolCall -> Plan (validation layer 2). Pure: no I/O, no side effects.

Every URL comes from a fixed template plus percent-encoded text; every app ID from
a fixed table. The model never supplies either. Executors accept only Plan and
re-check it against their own allowlists (layer 3).
"""
from dataclasses import dataclass
import hashlib
import re
import unicodedata
from urllib.parse import quote, quote_plus, unquote

from .errors import Refused
from .registry import APP_IDS, HOME_ID, REGISTRY, SITES, at_least
from .validate import canonical

# Implicit web search has one provider and one query parameter; no suggestions.
GOOGLE_PREFIX = 'https://www.google.com/search?q='
DDG_PREFIX = 'https://duckduckgo.com/?q='
YT_PREFIX = 'https://www.youtube.com/results?search_query='
# Exact patterns, no '&' or '#'. Bound 1500 keeps every URL under the channel's
# 1600-char limit (voice/backend/browser_channel.py).
GOOGLE_PATTERN = re.compile(r'https://www\.google\.com/search\?q=[A-Za-z0-9%._~-]{1,1500}')
DDG_PATTERN = re.compile(r'https://duckduckgo\.com/\?q=[A-Za-z0-9%._~-]{1,1500}')
YT_PATTERN = re.compile(r'https://www\.youtube\.com/results\?search_query=[A-Za-z0-9%+._~-]{1,1500}')

BROWSER_FRONT = ('browser_youtube_watch', 'browser_youtube', 'browser_other')
SURFACES = frozenset(BROWSER_FRONT + ('browser_none', 'r2d2_home', 'input_other', 'unknown') +
                     tuple('app_' + key for key in APP_IDS))
_NONCE = re.compile(r'[a-f0-9]{32}')


def site_urls():
    return frozenset(site.url for site in SITES.values())


_ESCAPES = re.compile(r'(?:[^%]|%[0-9A-F]{2})*')
_REFUSED_DECODED = frozenset(('Cc', 'Cf', 'Cs', 'Co', 'Cn', 'Zl', 'Zp'))
_MAX_DECODES = 4


def _clean_query(tail, *, bang):
    """Layer-3 checks on an encoded query, independent of the
    router: only well-formed upper-hex escapes of valid UTF-8 (what quote() emits for
    validated Text), no control/format/unassigned characters once decoded, and, for
    DuckDuckGo, no '!' or '\\' at any decoding depth (so %2521g is refused too)."""
    if not _ESCAPES.fullmatch(tail):
        return False
    try:
        text = unquote(tail, errors='strict')
    except UnicodeDecodeError:
        return False
    if any(unicodedata.category(c) in _REFUSED_DECODED for c in text):
        return False
    for _ in range(_MAX_DECODES):
        folded = unicodedata.normalize('NFKC', text)
        if bang and ('!' in folded or '\\' in folded):
            return False
        if any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in folded):
            return False
        decoded = unquote(text)
        if decoded == text:
            return True
        text = decoded
    return False


def _no_bang(url):
    # [H] DuckDuckGo treats "!bang" and a leading "\\" as redirects off the
    # allowlist. Decode and NFKC the query so no encoding of either gets through.
    return _clean_query(url[len(DDG_PREFIX):], bang=True)


def allowed_url(url):
    """Browser allowlist generated from sites.json plus the fixed search templates."""
    return (type(url) is str and url.isascii() and len(url) <= 1600 and
            (url in site_urls() or
             # Keep the existing normalized web-query guard when changing providers.
             (GOOGLE_PATTERN.fullmatch(url) is not None and _clean_query(url[len(GOOGLE_PREFIX):], bang=True)) or
             (DDG_PATTERN.fullmatch(url) is not None and _no_bang(url)) or
             (YT_PATTERN.fullmatch(url) is not None and _clean_query(url[len(YT_PREFIX):], bang=False))))


@dataclass(frozen=True)
class Plan:
    tool: str
    executor: str     # 'browser' | 'tv' | 'agent'
    action: str
    op: str           # lease nonce + ':' + action, used once
    params: tuple     # sorted ((name, value), ...), agent-built only
    scopes: tuple     # rate-limit scopes
    units: int = 1    # volume steps count against 'volume_steps'

    def param(self, name):
        return dict(self.params)[name]

    def digest(self):
        body = {'tool': self.tool, 'executor': self.executor, 'action': self.action,
                'op': self.op, 'params': [list(p) for p in self.params]}
        return hashlib.sha256(canonical(body)).hexdigest()


def _plan(call, route, nonce, params, units=1):
    return Plan(call.tool, route.executor, route.action, nonce + ':' + route.action,
                tuple(sorted(params.items())), route.scopes, units)


def choose_route(tool, surface):
    front = 'browser' if surface in BROWSER_FRONT else 'other'
    for route in tool.routes:
        if route.when in ('always', front):
            return route
    raise Refused('TOOL_SCHEMA')


def plan(call, surface, nonce, phase, *, tv_routes=True):
    """Raises Refused with TOOL_DISABLED / TV_TOOL_UNAVAILABLE / UNSUPPORTED_COMMAND /
    TOOL_SCHEMA. `tv_routes=False` is used for P1 handoff calls (browser only)."""
    if surface not in SURFACES or type(nonce) is not str or not _NONCE.fullmatch(nonce):
        raise Refused('TOOL_SCHEMA')
    tool = REGISTRY[call.tool]
    route = choose_route(tool, surface)
    if route.executor == 'tv' and route.phase is None:
        raise Refused('TV_TOOL_UNAVAILABLE')
    if not at_least(phase, route.phase):
        raise Refused('TOOL_DISABLED')
    if route.executor == 'tv' and not tv_routes:
        raise Refused('TOOL_DISABLED')
    args = call.args
    if route.executor == 'none':
        raise Refused('UNSUPPORTED_COMMAND')
    if route.action == 'navigate':
        if call.tool == 'browser.open_site':
            url = SITES[args['site']].url
        elif call.tool == 'browser.web_search':
            url = GOOGLE_PREFIX + quote(args['query'], safe='')
        else:
            url = YT_PREFIX + quote_plus(args['query'])
        if not allowed_url(url):
            raise Refused('TOOL_SCHEMA')
        return _plan(call, route, nonce, {'url': url})
    if route.action == 'history':
        return _plan(call, route, nonce, {'action': 'back'})
    if route.action == 'media' and route.executor == 'browser':
        return _plan(call, route, nonce, {'want': args['action']})
    if route.action == 'media':
        if args['action'] == 'toggle':
            raise Refused('TV_TOOL_UNAVAILABLE')  # SSAP gives no paused read
        return _plan(call, route, nonce, {'action': args['action']})
    if route.action == 'end':
        return _plan(call, route, nonce, {'reason': 'home' if call.tool == 'tv.home' else 'close'})
    if route.action in ('observe', 'foreground', 'cancel', 'confirm'):
        return _plan(call, route, nonce, {})
    if route.action == 'home':
        return _plan(call, route, nonce, {'app_id': HOME_ID})
    if route.action == 'launch_app':
        return _plan(call, route, nonce, {'key': args['app'], 'app_id': APP_IDS[args['app']]})
    if route.action == 'open_content':
        return _plan(call, route, nonce, {'key': args['app'], 'app_id': APP_IDS[args['app']],
                                          'kind': args['kind'], 'text': args['text']})
    if route.action == 'volume':
        return _plan(call, route, nonce, {'direction': args['direction'], 'steps': args['steps']},
                     units=args['steps'])
    if route.action == 'mute':
        return _plan(call, route, nonce, {'mute': args['state'] == 'on'})
    raise Refused('TOOL_SCHEMA')
