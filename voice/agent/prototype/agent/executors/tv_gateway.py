"""TV gateway: the only table of SSAP requests the system can ever send,
plus the TV executor. P0 ships only a fake transport; nothing here opens a socket.

Every SSAP URI and permission name is [H] (general knowledge of LG's local API)
except the reads and the launch allowlist.

Transport contract (implemented later over a TLS/WebSocket SSAP client's
Connection, never its pair() [R]):
  connect(*, pin_sha256, timeout_s) -> None
      TLS to the configured TV address (tv_ip), port 3001; raise CertificateChanged if the peer cert's
      SHA-256 != pin_sha256. No trust-on-first-use: the pin is mandatory.
  register(*, client_key, timeout_s) -> dict
      Sends `register` carrying the saved key only. It never sends a
      pairingType, never forcePairing and never writes a key. Returns the reply
      payload untouched (the gateway inspects it).
  request(uri, payload, *, timeout_s) -> dict      one SSAP request, reply payload
  close() -> None
"""
import hashlib
import os
from pathlib import Path
import re
import stat
import threading
from types import MappingProxyType

from ..errors import Refused
from ..policy import RateLimiter
from ..registry import APP_IDS, BROWSER_ID, HOME_ID, REGISTRY, at_least
from ..validate import bounded_text, strict_loads
from .base import ExecResult
from .feedback import TEMPLATES

# name -> (uri, first phase, kind)
SSAP = MappingProxyType({
    'foreground': ('ssap://com.webos.applicationManager/getForegroundAppInfo', 'P3', 'read'),
    'installed': ('ssap://com.webos.applicationManager/listApps', 'P3', 'read'),
    'launch': ('ssap://system.launcher/launch', 'P3', 'write'),
    'volume_up': ('ssap://audio/volumeUp', 'P4', 'write'),
    'volume_down': ('ssap://audio/volumeDown', 'P4', 'write'),
    'volume_read': ('ssap://audio/getVolume', 'P4', 'read'),
    'set_mute': ('ssap://audio/setMute', 'P4', 'write'),
    'audio_status': ('ssap://audio/getStatus', 'P4', 'read'),
    'play': ('ssap://media.controls/play', 'P4', 'write'),
    'pause': ('ssap://media.controls/pause', 'P4', 'write'),
    'toast': ('ssap://system.notifications/createToast', 'P4', 'write'),
})
# [R] No pointer-input socket, no input, no CLOSE, settings or power. [H] names;
# this exact list is reviewed before P4 enrollment.
P4_MANIFEST = ('READ_INSTALLED_APPS', 'READ_RUNNING_APPS', 'READ_APP_STATUS', 'LAUNCH',
               'CONTROL_AUDIO', 'CONTROL_INPUT_MEDIA_PLAYBACK', 'WRITE_NOTIFICATION_TOAST')
FORBIDDEN_PERMISSIONS = ('CONTROL_INPUT_JOYSTICK', 'CONTROL_INPUT_TEXT', 'CONTROL_MOUSE_AND_KEYBOARD',
                         'CONTROL_INPUT_TV', 'CONTROL_POWER', 'CONTROL_DISPLAY', 'CONTROL_TV_SCREEN',
                         'WRITE_SETTINGS', 'CLOSE', 'READ_SETTINGS', 'CONTROL_INPUT_POINTER')
# The Spark manager address is a build parameter (config "spark_ip"). The default
# is an RFC 5737 documentation address; a deployment passes its own spark_ip.
DEFAULT_SPARK_IP = '192.0.2.10'
_IPV4 = re.compile(r'(?:(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])\.){3}(?:25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])')


def open_url_pattern(spark_ip):
    """The only browser-open URL shape for this Spark: its manager port and 64-hex key."""
    if type(spark_ip) is not str or not _IPV4.fullmatch(spark_ip):
        raise ValueError('SPARK_IP')
    return re.compile(r'http://%s:4181/[a-f0-9]{64}/open' % re.escape(spark_ip))


OPEN_URL = open_url_pattern(DEFAULT_SPARK_IP)
_HEX32 = re.compile(r'[a-f0-9]{32}')
_KEY = re.compile(r'[a-f0-9]{32,128}')
_PIN = re.compile(r'[a-f0-9]{64}')
MUTATION_LIMITS = {'mutation': ((2, 1.0), (120, 3600))}   # the gateway's own limits
READ_LIMITS = {'read': ((8, 1.0), (1800, 3600))}          # [D-proto]
DEADLINE_S = 3
_REVERSE = MappingProxyType({app_id: key for key, app_id in APP_IDS.items()})
OPEN_CONTENT_APPS = frozenset(REGISTRY['tv.open_content'].spec['app'].values)


class CertificateChanged(Exception):
    pass


_PARAM_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,31}')


def _flat_params(params):
    """Deep-link params: 1-4 identifier keys, each a printable-ASCII string <= 1024."""
    return (type(params) is dict and 1 <= len(params) <= 4 and
            all(type(k) is str and _PARAM_KEY.fullmatch(k) is not None and type(v) is str and
                0 < len(v) <= 1024 and v.isascii() and v.isprintable() for k, v in params.items()))


def private_json(path, limit=8192):
    """Private-file rules: regular, owned by us, single link, 0600-or-stricter,
    non-symlink path, bounded size, fstat == lstat."""
    path = Path(path)
    st = os.lstat(path)
    if not (stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and st.st_nlink == 1 and
            st.st_mode & 0o077 == 0 and 0 < st.st_size <= limit and path.resolve() == path.absolute()):
        raise Refused('TV_CHANNEL_UNAVAILABLE')
    with open(path, 'rb') as handle:
        fst = os.fstat(handle.fileno())
        if (fst.st_ino, fst.st_dev, fst.st_size) != (st.st_ino, st.st_dev, st.st_size):
            raise Refused('TV_CHANNEL_UNAVAILABLE')
        body = handle.read(limit + 1)
    return strict_loads(body, limit, 'TV_CHANNEL_UNAVAILABLE')


def op_ids(op, count):
    """32-hex one-use gateway opIds derived from the plan op (nonce:action)."""
    return [hashlib.sha256(('%s#%d' % (op, i)).encode()).hexdigest()[:32] for i in range(count)]


class TVGateway:
    def __init__(self, transport, *, key_path, pin_path, killswitch, phase, clock,
                 open_url_path=None, on_trip=None, deeplinks=None, spark_ip=DEFAULT_SPARK_IP):
        self.transport = transport
        self.open_url = open_url_pattern(spark_ip)
        self.key_path, self.pin_path, self.open_url_path = key_path, pin_path, open_url_path
        self.killswitch, self.phase, self.clock = killswitch, phase, clock
        self.on_trip = on_trip
        self.deeplinks = dict(deeplinks or {})  # app key -> qualified builder (none in v1)
        self.connected = False
        self.used = set()
        self.inflight = threading.Lock()
        self.mutations = RateLimiter(clock, MUTATION_LIMITS)
        self.reads = RateLimiter(clock, READ_LIMITS)
        self.last_error_class = None  # 'permission' | 'other' (for P3 qualification)

    # -- connection [R] ---------------------------------------------------------
    def _trip(self, reason):
        self.killswitch.set_tv_off(reason)
        if reason in ('PAIRING_PROMPT', 'CERT_CHANGED'):
            self.killswitch.pause(reason)   # auto-trip: the whole agent goes to Paused
        if self.on_trip is not None:
            self.on_trip(reason)

    def _drop(self):
        self.connected = False
        try:
            self.transport.close()
        except Exception:
            pass

    def _check_gate(self, gate=None, *, close_on_refusal=False):
        reason = gate() if gate is not None else None
        if reason is None and self.killswitch.tv_off():
            reason = 'TV_CHANNEL_UNAVAILABLE'
        if reason is not None:
            if close_on_refusal:
                self._drop()
            raise Refused(reason)

    def _connect(self, gate=None):
        self._check_gate(gate)
        if self.connected:
            return
        key = None
        try:
            # Refuse before connecting: no TCP connection without key + pin files.
            key_doc = private_json(self.key_path)
            pin_doc = private_json(self.pin_path)
            if (type(key_doc) is not dict or set(key_doc) != {'client-key'} or
                    type(key_doc['client-key']) is not str or not _KEY.fullmatch(key_doc['client-key']) or
                    type(pin_doc) is not dict or set(pin_doc) != {'sha256'} or
                    type(pin_doc['sha256']) is not str or not _PIN.fullmatch(pin_doc['sha256'])):
                raise Refused('TV_CHANNEL_UNAVAILABLE')
            key = key_doc['client-key']
            self._check_gate(gate)
            try:
                self.transport.connect(pin_sha256=pin_doc['sha256'], timeout_s=DEADLINE_S)
            except CertificateChanged:
                self._drop()
                self._trip('CERT_CHANGED')
                raise Refused('TV_CHANNEL_UNAVAILABLE') from None
            except Exception:
                self._drop()
                raise Refused('TV_CHANNEL_UNAVAILABLE') from None
            self._check_gate(gate, close_on_refusal=True)
            try:
                reply = self.transport.register(client_key=key, timeout_s=DEADLINE_S)
            except Exception:
                self._drop()
                raise Refused('TV_CHANNEL_UNAVAILABLE') from None
            self._check_gate(gate, close_on_refusal=True)
            if type(reply) is not dict or 'pairingType' in reply or reply.get('client-key') != key:
                # Close at once, latch TV tools off, never retry, never write a key.
                self._drop()
                self._trip('PAIRING_PROMPT' if type(reply) is dict and 'pairingType' in reply
                           else 'PERMISSION_DENIED')
                raise Refused('TV_PERMISSION_DENIED')
            self.connected = True
        except Refused:
            raise
        except Exception:
            raise Refused('TV_CHANNEL_UNAVAILABLE') from None
        finally:
            key = key_doc = None

    # -- the single send path ------------------------------------------------------
    def _payload_ok(self, name, payload):
        if type(payload) is not dict:
            return False
        if name == 'launch':
            app = payload.get('id')
            if type(app) is not str:  # unhashable ids would raise TypeError
                return False
            if set(payload) == {'id'}:
                return app in _REVERSE or app == HOME_ID
            if set(payload) != {'id', 'params'} or type(payload['params']) is not dict:
                return False
            params = payload['params']
            if app == BROWSER_ID:
                target = params.get('contentTarget')
                return (set(params) == {'contentTarget'} and type(target) is str and
                        re.fullmatch(self.open_url.pattern + r'#voice-session=[a-f0-9]{32}', target) is not None)
            builder = self.deeplinks.get(_REVERSE.get(app))
            # Defense in depth: whatever a future builder
            # returns, the gateway sends only a small flat map of printable ASCII.
            return (builder is not None and getattr(builder, 'qualified', False) is True and
                    _flat_params(params) and builder.validate(params) is True)
        if name == 'set_mute':
            return set(payload) == {'mute'} and type(payload['mute']) is bool
        if name == 'toast':
            return set(payload) == {'message'} and payload['message'] in TEMPLATES.values()
        return payload == {}

    def _send(self, name, payload, op_id=None, *, gate=None):
        """Raises Refused only before anything is sent. Returns (status, reply)."""
        uri, phase, kind = SSAP[name]
        if not at_least(self.phase, phase):
            raise Refused('TV_TOOL_UNAVAILABLE')
        if not self._payload_ok(name, payload):
            raise Refused('TOOL_SCHEMA')
        if kind == 'write' and (type(op_id) is not str or not _HEX32.fullmatch(op_id) or
                                op_id in self.used or len(self.used) >= 4096):
            raise Refused('TOOL_SCHEMA')
        if not self.inflight.acquire(blocking=False):
            raise Refused('TV_CHANNEL_UNAVAILABLE')  # at most one request in flight
        try:
            (self.mutations if kind == 'write' else self.reads).admit(
                ('mutation',) if kind == 'write' else ('read',), record=False)
            self._connect(gate)
            (self.mutations if kind == 'write' else self.reads).admit(
                ('mutation',) if kind == 'write' else ('read',))
            # Connect/register and callers' live reads may have blocked since
            # the executor admitted this plan. Revoke before the actual send.
            self._check_gate(gate)
            if kind == 'write':
                self.used.add(op_id)
            try:
                reply = self.transport.request(uri, payload, timeout_s=DEADLINE_S)
            except Exception:
                self._drop()
                return 'unknown', None
            if type(reply) is dict and 'pairingType' in reply:
                self._drop()
                self._trip('PAIRING_PROMPT')
                return 'unknown', None
            if type(reply) is dict and reply.get('returnValue') is True:
                return 'accepted', reply
            if type(reply) is dict and reply.get('returnValue') is False:
                text = str(reply.get('errorText', ''))[:64].lower()
                self.last_error_class = ('permission' if reply.get('errorCode') in (401, 403, '401', '403')
                                         or 'denied' in text else 'other')
                return 'rejected', None
            return 'unknown', None
        finally:
            self.inflight.release()

    # -- reads (enumerated observations only; no raw TV reply leaves) ----------------
    def foreground(self):
        status, reply = self._send('foreground', {})
        if status != 'accepted' or type(reply.get('appId')) is not str:
            return None
        app = reply['appId']
        if app == HOME_ID:
            return 'r2d2_home'
        if app == BROWSER_ID:
            return 'browser'
        return _REVERSE.get(app, 'other')

    def installed_keys(self):
        status, reply = self._send('installed', {})
        apps = reply.get('apps') if status == 'accepted' else None
        if type(apps) is not list or len(apps) > 512:
            return None
        ids = {a.get('id') for a in apps if type(a) is dict}
        return frozenset(key for key, app in APP_IDS.items() if app in ids)

    def volume_read(self):
        status, reply = self._send('volume_read', {})
        if status != 'accepted':
            return None
        value = reply.get('volume', (reply.get('volumeStatus') or {}).get('volume')
                          if type(reply.get('volumeStatus')) is dict else None)
        return value if type(value) is int and 0 <= value <= 100 else None

    def mute_read(self):
        status, reply = self._send('audio_status', {})
        value = reply.get('mute') if status == 'accepted' else None
        return value if type(value) is bool else None

    # -- mutations -------------------------------------------------------------------
    def launch_app(self, key, op_id, *, gate=None):
        if key not in APP_IDS:
            raise Refused('TOOL_SCHEMA')
        installed = self.installed_keys()  # live listApps
        if installed is None:
            raise Refused('TV_CHANNEL_UNAVAILABLE')
        if key not in installed:
            raise Refused('TV_APP_NOT_INSTALLED')
        return self._send('launch', {'id': APP_IDS[key]}, op_id, gate=gate)

    def home(self, op_id, *, gate=None):
        return self._send('launch', {'id': HOME_ID}, op_id, gate=gate)

    def open_browser(self, session, op_id):
        """[R] The caller sends only the 32-hex session; OPEN_URL (which embeds
        the manager key) is a gateway constant read from a 0600 file."""
        if type(session) is not str or not _HEX32.fullmatch(session) or self.open_url_path is None:
            raise Refused('TOOL_SCHEMA')
        doc = private_json(self.open_url_path)
        if type(doc) is not dict or set(doc) != {'url'} or type(doc['url']) is not str or \
                not self.open_url.fullmatch(doc['url']):
            raise Refused('TV_CHANNEL_UNAVAILABLE')
        try:
            return self._send('launch', {'id': BROWSER_ID,
                                         'params': {'contentTarget': doc['url'] + '#voice-session=' + session}},
                              op_id)
        finally:
            doc = None

    def launch_content(self, key, kind, text, op_id, *, gate=None):
        builder = self.deeplinks.get(key)
        if builder is None or getattr(builder, 'qualified', False) is not True:
            raise Refused('DEEPLINK_UNQUALIFIED')
        try:
            params = builder.build(kind, text)
        except Exception:
            raise Refused('DEEPLINK_UNQUALIFIED') from None
        return self._send('launch', {'id': APP_IDS[key], 'params': params}, op_id, gate=gate)

    def volume_step(self, direction, op_id, *, gate=None):
        return self._send({'up': 'volume_up', 'down': 'volume_down'}[direction], {}, op_id, gate=gate)

    def set_mute(self, mute, op_id, *, gate=None):
        return self._send('set_mute', {'mute': mute}, op_id, gate=gate)

    def media(self, action, op_id, *, gate=None):
        if action not in ('play', 'pause'):
            raise Refused('TV_TOOL_UNAVAILABLE')
        return self._send(action, {}, op_id, gate=gate)

    def toast(self, code, op_id):
        if code not in TEMPLATES:
            raise Refused('TOOL_SCHEMA')
        return self._send('toast', {'message': TEMPLATES[code]}, op_id)


class TVExecutor:
    VERIFY_POLLS, VERIFY_GAP_S = 10, 0.3   # foreground verification
    STEP_GAP_S = 0.55                      # [D-proto] keeps volume under the 2 req/s gateway limit
    _PARAMS = {'launch_app': {'key', 'app_id'}, 'home': {'app_id'}, 'volume': {'direction', 'steps'},
               'mute': {'mute'}, 'media': {'action'}, 'foreground': set(),
               'open_content': {'key', 'app_id', 'kind', 'text'}}

    def __init__(self, gateway, *, clock, wait):
        self.gateway, self.clock, self.wait = gateway, clock, wait
        self.toast_seq = 0

    def surface(self):
        try:
            fg = self.gateway.foreground()
        except Exception:
            return 'unknown'
        if fg == 'r2d2_home':
            return 'r2d2_home'
        if fg in APP_IDS:
            return 'app_' + fg
        return 'input_other' if fg == 'other' else 'unknown'

    def read_volume(self):
        try:
            return self.gateway.volume_read()
        except Exception:
            return None

    def toast(self, code):
        self.toast_seq += 1
        try:
            status, _ = self.gateway.toast(code, op_ids('toast:%d:%s' % (self.toast_seq, code), 1)[0])
        except Exception:
            return False
        return status == 'accepted'

    def _valid(self, plan):
        params = dict(plan.params)
        if plan.executor != 'tv' or plan.action not in self._PARAMS or set(params) != self._PARAMS[plan.action]:
            return False
        if plan.action == 'open_content':
            # Layer-3 re-check of every open_content param, not only the app pair
            # (executors re-validate plans).
            return (type(params['key']) is str and params['key'] in OPEN_CONTENT_APPS and
                    APP_IDS.get(params['key']) == params['app_id'] and
                    params['kind'] in ('search', 'title') and bounded_text(params['text'], 160, 640))
        if plan.action == 'launch_app':
            return type(params['key']) is str and APP_IDS.get(params['key']) == params['app_id']
        if plan.action == 'home':
            return params['app_id'] == HOME_ID
        if plan.action == 'volume':
            return params['direction'] in ('up', 'down') and type(params['steps']) is int and \
                1 <= params['steps'] <= 5
        if plan.action == 'mute':
            return type(params['mute']) is bool
        if plan.action == 'media':
            return params['action'] in ('play', 'pause')
        return True

    def _verify_foreground(self, want, deadline):
        for index in range(self.VERIFY_POLLS):
            try:
                if self.gateway.foreground() == want:
                    return True
            except Exception:
                pass
            if index + 1 < self.VERIFY_POLLS and self.clock() + self.VERIFY_GAP_S < deadline:
                self.wait(self.VERIFY_GAP_S)
        return False

    def run(self, plan, *, gate, deadline):
        if not self._valid(plan):
            return ExecResult('refused', 'TOOL_SCHEMA', False)
        action, gw = plan.action, self.gateway
        owner_gate = gate

        def gate():
            return owner_gate() or ('REQUEST_EXPIRED' if self.clock() >= deadline else None)
        if action == 'foreground':
            try:
                fg = gw.foreground()
            except Refused as error:
                return ExecResult('refused', error.code, False)
            if fg is None:
                return ExecResult('refused', 'TV_CHANNEL_UNAVAILABLE', False)
            return ExecResult('accepted', 'TV_STATUS_SHOWN', False, (('app', fg),))
        if action == 'volume':
            return self._volume(plan, gate, deadline)
        ids = op_ids(plan.op, 1)
        blocked = gate()
        if blocked is not None:
            return ExecResult('refused', blocked, False)
        try:
            if action == 'launch_app':
                status, _ = gw.launch_app(plan.param('key'), ids[0], gate=gate)
            elif action == 'open_content':
                status, _ = gw.launch_content(plan.param('key'), plan.param('kind'), plan.param('text'), ids[0], gate=gate)
            elif action == 'home':
                status, _ = gw.home(ids[0], gate=gate)
            elif action == 'mute':
                status, _ = gw.set_mute(plan.param('mute'), ids[0], gate=gate)
            else:
                status, _ = gw.media(plan.param('action'), ids[0], gate=gate)
        except Refused as error:
            return ExecResult('refused', error.code, False)
        if status == 'rejected':
            code = 'TV_LAUNCH_REJECTED' if action in ('launch_app', 'open_content', 'home') else 'TV_REQUEST_REJECTED'
            return ExecResult('rejected', code, True)
        if status not in ('accepted', 'unknown'):
            return ExecResult('unknown', 'TV_UNCONFIRMED', True)
        if action in ('launch_app', 'open_content', 'home'):
            want = 'r2d2_home' if action == 'home' else plan.param('key')
            return (ExecResult('verified', 'TV_FOREGROUND_VERIFIED', True)
                    if self._verify_foreground(want, deadline) else ExecResult('unknown', 'TV_UNCONFIRMED', True))
        if action == 'mute':
            try:
                seen = gw.mute_read()
            except Exception:
                seen = None
            return (ExecResult('verified', 'TV_MUTE_VERIFIED', True) if seen is plan.param('mute')
                    else ExecResult('unknown', 'TV_UNCONFIRMED', True))
        return (ExecResult('accepted', 'TV_MEDIA_ACCEPTED', True) if status == 'accepted'
                else ExecResult('unknown', 'TV_UNCONFIRMED', True))

    def _volume(self, plan, gate, deadline):
        direction, steps = plan.param('direction'), plan.param('steps')
        ids = op_ids(plan.op, steps)
        start = self.read_volume()
        for index in range(steps):
            if index:
                self.wait(self.STEP_GAP_S)
            blocked = gate() or ('REQUEST_EXPIRED' if self.clock() >= deadline else None)
            if blocked is not None:
                return (ExecResult('refused', blocked, False) if index == 0
                        else ExecResult('unknown', 'TV_UNCONFIRMED', True))
            try:
                status, _ = self.gateway.volume_step(direction, ids[index], gate=gate)
            except Refused as error:
                return (ExecResult('refused', error.code, False) if index == 0
                        else ExecResult('unknown', 'TV_UNCONFIRMED', True))
            if status == 'rejected' and index == 0:
                return ExecResult('rejected', 'TV_REQUEST_REJECTED', True)
            if status != 'accepted':
                return ExecResult('unknown', 'TV_UNCONFIRMED', True)  # stop: never send more
        end = self.read_volume()
        if start is not None and end == max(0, min(100, start + (steps if direction == 'up' else -steps))):
            return ExecResult('verified', 'TV_VOLUME_VERIFIED', True)
        return ExecResult('accepted', 'TV_VOLUME_ACCEPTED', True)


class FakeSSAPTransport:
    """In-memory TV. Records URIs and payload key sets only (never key material)."""
    def __init__(self):
        self.cert_ok = True
        self.register_mode = 'ok'     # 'ok' | 'prompt' | 'denied' | 'raise'
        self.foreground_id = HOME_ID
        self.installed = list(APP_IDS.values()) + [HOME_ID, BROWSER_ID]
        self.volume, self.mute = 20, False
        self.forced = {}              # uri -> reply dict or Exception instance
        self.launch_effect = True
        self.connects = 0
        self.registers = 0
        self.requests = []            # (uri, sorted payload keys)
        self.launched = []            # launch payload ids (catalog/system IDs only, never keys)
        self.closed = 0

    def connect(self, *, pin_sha256, timeout_s):
        self.connects += 1
        if not self.cert_ok:
            raise CertificateChanged()

    def register(self, *, client_key, timeout_s):
        self.registers += 1
        if self.register_mode == 'raise':
            raise OSError('reset')
        if self.register_mode == 'prompt':
            return {'pairingType': 'PROMPT', 'returnValue': True}
        if self.register_mode == 'denied':
            return {'returnValue': False}
        return {'client-key': client_key}

    def request(self, uri, payload, *, timeout_s):
        self.requests.append((uri, tuple(sorted(payload))))
        forced = self.forced.get(uri)
        if isinstance(forced, Exception):
            raise forced
        if forced is not None:
            return forced
        ok = {'returnValue': True}
        if uri.endswith('/launch'):
            self.launched.append(payload['id'])
            if self.launch_effect:
                self.foreground_id = payload['id']
            return ok
        if uri.endswith('/getForegroundAppInfo'):
            return {'returnValue': True, 'appId': self.foreground_id}
        if uri.endswith('/listApps'):
            return {'returnValue': True, 'apps': [{'id': i, 'title': 'x'} for i in self.installed]}
        if uri.endswith('/volumeUp'):
            self.volume = min(100, self.volume + 1)
        elif uri.endswith('/volumeDown'):
            self.volume = max(0, self.volume - 1)
        elif uri.endswith('/getVolume'):
            return {'returnValue': True, 'volume': self.volume}
        elif uri.endswith('/setMute'):
            self.mute = payload['mute']
        elif uri.endswith('/getStatus'):
            return {'returnValue': True, 'mute': self.mute, 'volume': self.volume}
        return ok

    def close(self):
        self.closed += 1
