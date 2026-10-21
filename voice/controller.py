"""Finite voice intent controller. Default capabilities perform no I/O."""
from dataclasses import dataclass, field
import json
import re
import secrets
import threading
import time
from urllib.parse import quote, urlsplit


class Refused(Exception):
    pass


def bounded_text(value, limit=160, byte_limit=640):
    return (type(value) is str and 0 < len(value) <= limit and
            len(value.encode('utf-8', errors='surrogatepass')) <= byte_limit and
            value == value.strip() and
            all(ord(c) >= 32 and not 127 <= ord(c) <= 159 and
                not 0xD800 <= ord(c) <= 0xDFFF for c in value))


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise Refused('MODEL_SCHEMA')
        result[key] = value
    return result


def parse_intent(raw):
    if type(raw) is not str or len(raw.encode('utf-8', errors='surrogatepass')) > 2048:
        raise Refused('MODEL_SCHEMA')
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, TypeError, RecursionError):
        raise Refused('MODEL_SCHEMA') from None
    if type(value) is not dict:
        raise Refused('MODEL_SCHEMA')
    action = value.get('action')
    if action == 'browser_open' and set(value) == {'action', 'destination'}:
        if value['destination'] == 'youtube':
            return value
    elif action == 'tv_content_open' and set(value) == {'action', 'provider', 'title'}:
        if value['provider'] == 'apple_tv' and bounded_text(value['title']):
            return value
    elif action == 'web_search' and set(value) == {'action', 'query'}:
        if bounded_text(value['query'], 256, 1024):
            return value
    elif action == 'unsupported' and set(value) == {'action'}:
        return value
    raise Refused('MODEL_SCHEMA')


def title_key(value):
    return ' '.join(value.casefold().split())


def official_apple_url(value):
    if (type(value) is not str or len(value) > 1024 or
            not value.isascii() or any(ord(c) <= 32 or ord(c) == 127 for c in value)):
        return False
    try:
        part = urlsplit(value)
        return (part.scheme == 'https' and part.netloc == 'tv.apple.com' and
                not part.query and not part.fragment and
                bool(re.fullmatch(r'/[A-Za-z0-9._~/-]{3,900}', part.path)) and
                all(segment not in ('.', '..') for segment in part.path.split('/')))
    except ValueError:
        return False


@dataclass(frozen=True, repr=False)
class TitleRecord:
    # Produced only by an independent official-page/catalogue verifier.
    provider: str
    title: str
    url: str
    content_id: str


@dataclass(frozen=True, repr=False)
class BrowserObservation:
    session: str
    owned: bool
    ready: bool
    presented: bool
    url: str | None


@dataclass(frozen=True, repr=False)
class TitleObservation:
    provider: str
    content_id: str
    presented: bool


@dataclass(frozen=True)
class Outcome:
    state: str  # verified / refused / rejected / unknown
    code: str   # Fixed codes only; no transcript, URL, title or exception text.


@dataclass(repr=False)
class Lease:
    nonce: str = field(repr=False)
    expires: float
    consumed: bool = False


class DisabledRuntime:
    """No implicit live route: production must supply reviewed bounded adapters.

    Callbacks must respect timeout_s; never log argument text/URLs. Browser start
    and navigation return accepted/rejected/unknown, not completion. Observations
    must come from the exact owned session/target and its TV presentation receipt.
    """
    browser_enabled = False
    apple_title_enabled = False


class DisabledCatalogue:
    enabled = False


class Controller:
    def __init__(self, model, runtime=None, catalogue=None, *, clock=time.monotonic,
                 wait=time.sleep):
        self.model = model
        self.runtime = runtime or DisabledRuntime()
        self.catalogue = catalogue or DisabledCatalogue()
        self.clock, self.wait = clock, wait
        self.active = None
        self.lock = threading.Lock()
        self.busy = False
        self.dispatched = False

    def arm(self):
        """Trusted PTT owner calls this; never expose it to ASR/model/page data."""
        with self.lock:
            if self.busy:
                raise Refused('CONTROLLER_BUSY')
            if self.active is not None:
                self.active.consumed = True
            self.active = Lease(secrets.token_hex(16), self.clock() + 30)
            return self.active

    def cancel(self, lease=None):
        with self.lock:
            if lease is not None and self.active is not lease:
                return False
            if self.active is not None:
                self.active.consumed = True
            self.active = None
            return True

    def _alive(self, lease):
        if self.active is not lease or self.clock() >= lease.expires:
            raise Refused('REQUEST_EXPIRED')

    def _call(self, lease, function, *args):
        self._alive(lease)
        remaining = lease.expires - self.clock()
        result = function(*args, timeout_s=min(remaining, 5.0))
        self._alive(lease)
        return result

    def _observe(self, lease, read, qualified, *, terminal=None):
        # Finite read-only polling, never repeat a mutation.
        for index in range(20):
            value = self._call(lease, read)
            if qualified(value):
                return value
            if terminal is not None and terminal(value):
                return None
            if index < 19:
                self.wait(min(.1, max(0, lease.expires - self.clock())))
        return None

    def _mutation(self, lease, function, *args):
        self._alive(lease)
        self.dispatched = True
        return self._call(lease, function, *args)

    def execute(self, lease, transcript):
        with self.lock:
            if (self.busy or self.active is not lease or
                    type(lease) is not Lease or lease.consumed):
                return Outcome('refused', 'REQUEST_NOT_ARMED')
            lease.consumed = True
            self.busy = True
            self.dispatched = False
        intent = None
        try:
            self._alive(lease)
            if not bounded_text(transcript, 1024, 4096):
                return Outcome('refused', 'TRANSCRIPT_SCOPE')
            raw = self._call(lease, self.model.interpret, transcript)
            intent = parse_intent(raw)
            raw = None
            if intent['action'] == 'unsupported':
                return Outcome('refused', 'UNSUPPORTED_COMMAND')
            if intent['action'] == 'tv_content_open':
                return self._title(lease, intent['title'])
            url = ('https://www.youtube.com' if intent['action'] == 'browser_open'
                   else 'https://www.google.com/search?q=' + quote(intent['query'], safe=''))
            return self._browser(lease, url)
        except Refused as error:
            if self.dispatched:
                return Outcome('unknown', 'EXECUTION_UNCONFIRMED')
            code = str(error)
            allowed = {'REQUEST_EXPIRED', 'MODEL_SCHEMA', 'TITLE_AMBIGUOUS',
                       'TITLE_UNVERIFIED', 'MODEL_CONFIG', 'MODEL_UNAVAILABLE'}
            return Outcome('refused', code if code in allowed else 'CAPABILITY_UNAVAILABLE')
        except Exception:
            # An adapter exception can follow dispatch: never claim no action.
            return Outcome('unknown', 'EXECUTION_UNCONFIRMED')
        finally:
            transcript = intent = None
            with self.lock:
                if self.active is lease:
                    self.active = None
                self.busy = False

    @staticmethod
    def _browser_ready(value):
        return (type(value) is BrowserObservation and
                bool(re.fullmatch('[a-f0-9]{32}', value.session)) and
                value.owned is True and value.ready is True)

    def _browser(self, lease, url):
        if self.runtime.browser_enabled is not True:
            return Outcome('refused', 'BROWSER_BRIDGE_UNQUALIFIED')
        start = self._mutation(lease, self.runtime.launch_browser, lease.nonce + ':start')
        if start == 'rejected':
            return Outcome('rejected', 'BROWSER_START_REJECTED')
        if start not in ('accepted', 'unknown'):
            return Outcome('unknown', 'BROWSER_START_UNCONFIRMED')
        before = self._observe(lease, self.runtime.browser_observation, self._browser_ready)
        if before is None:
            return Outcome('unknown', 'BROWSER_NOT_READY')
        nav = self._mutation(lease, self.runtime.navigate, before.session, url,
                         lease.nonce + ':navigate')
        if nav == 'rejected':
            return Outcome('rejected', 'NAVIGATION_REJECTED')
        if nav not in ('accepted', 'unknown'):
            return Outcome('unknown', 'NAVIGATION_UNCONFIRMED')
        found = self._observe(lease, self.runtime.browser_observation,
                              lambda v: self._browser_ready(v) and
                              v.session == before.session and v.presented is True and
                              v.url in (url, url + '/' if url == 'https://www.youtube.com' else url),
                              terminal=lambda v: self._browser_ready(v) and
                              v.session != before.session)
        return Outcome('verified', 'BROWSER_TARGET_PRESENTED') if found else Outcome('unknown', 'NAVIGATION_UNCONFIRMED')

    def _title(self, lease, title):
        if self.catalogue.enabled is not True:
            return Outcome('refused', 'TITLE_CATALOGUE_UNQUALIFIED')
        raw = self._call(lease, self.catalogue.search, 'apple_tv', title)
        if type(raw) is not list or len(raw) > 8:
            raise Refused('TITLE_UNVERIFIED')
        candidates = {}
        for item in raw:
            # Search snippets/instructions grant no authority and are discarded.
            if (type(item) is not dict or set(item) != {'title', 'url'} or
                    not bounded_text(item['title']) or
                    title_key(item['title']) != title_key(title) or
                    not official_apple_url(item['url'])):
                continue
            candidates[item['url']] = item
        verified = {}
        for url in candidates:
            row = self._call(lease, self.catalogue.verify_official, url)
            if (type(row) is not TitleRecord or row.provider != 'apple_tv' or
                    not bounded_text(row.title) or title_key(row.title) != title_key(title) or
                    row.url != url or not official_apple_url(row.url) or
                    not isinstance(row.content_id, str) or
                    not re.fullmatch(r'umc\.cmc\.[a-z0-9]{1,96}', row.content_id) or
                    not row.url.endswith('/' + row.content_id)):
                continue
            verified[row.content_id] = row
        if len(verified) != 1:
            raise Refused('TITLE_AMBIGUOUS' if len(verified) > 1 else 'TITLE_UNVERIFIED')
        if self.runtime.apple_title_enabled is not True:
            return Outcome('refused', 'TITLE_DEEPLINK_UNQUALIFIED')
        row = next(iter(verified.values()))
        sent = self._mutation(lease, self.runtime.open_apple_title, row.content_id,
                          row.url, lease.nonce + ':title')
        if sent == 'rejected':
            return Outcome('rejected', 'TITLE_OPEN_REJECTED')
        if sent not in ('accepted', 'unknown'):
            return Outcome('unknown', 'TITLE_OPEN_UNCONFIRMED')
        found = self._observe(lease, self.runtime.title_observation,
                              lambda v: type(v) is TitleObservation and
                              v.provider == 'apple_tv' and v.content_id == row.content_id and
                              v.presented is True)
        return Outcome('verified', 'TITLE_PRESENTED') if found else Outcome('unknown', 'TITLE_OPEN_UNCONFIRMED')
