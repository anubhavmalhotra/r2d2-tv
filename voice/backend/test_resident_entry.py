"""Resident entry: private-file, authenticated HTTP and inert actual assembly tests."""
import contextlib
from email.message import Message
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import signal
import socket
import ssl
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import Mock, patch

from voice.backend import resident_entry as re
from voice.backend import voice_status
from voice.backend.resident_utterance import resident_device_key

# Synthetic, non-authorizing DER/PEM envelopes: these tests exercise byte/pin
# validation, not TLS acceptance. Real TLS/cert possession is covered by the
# separate ResidentUtteranceServer tests.
CERT = ssl.DER_cert_to_PEM_cert(b'\x30\x03\x02\x01\x01')
TV_CERT = ssl.DER_cert_to_PEM_cert(b'\x30\x03\x02\x01\x02')
KEY_PEM = '-----BEGIN '+ 'PRIVATE KEY' + '-----\nMAA=\n-----END PRIVATE KEY-----\n'
KEY, BROWSER_KEY = 'a' * 64, 'b' * 64


class Base(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args): pass
    def setup(self):
        super().setup(); self.connection.settimeout(1)
    def reply(self, code, value=None, cors=False):
        raw = json.dumps(value or {}, separators=(',', ':')).encode()
        self.send_response(code)
        for k, v in [('Content-Length', str(len(raw))), ('Connection', 'close'),
                     ('Cache-Control', 'no-store'), ('Content-Type', 'application/json')]:
            self.send_header(k, v)
        if cors: self.send_header('Access-Control-Allow-Origin', 'null')
        self.end_headers(); self.wfile.write(raw); self.close_connection = True
    def voice_feed(self, since): return None
    def do_GET(self):
        # Mirrors manager.py's browser-key voice route and its handler hook.
        prefix = '/' + BROWSER_KEY + '/voice/'
        if self.path.startswith(prefix) and self.path[len(prefix):].isdigit():
            value = self.voice_feed(int(self.path[len(prefix):]))
            if value is False:
                self.close_connection = True; return
            return self.reply(200, value, cors=self.headers.get('Origin') == 'null') if value is not None else self.reply(404)
        self.reply(202, {'browser': 'get'})
    def do_POST(self): self.reply(202, {'browser': 'post'})


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='resident-entry-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.now = 1000000
        self.cfg = dict(enabled=True, mode='resident', python=str(Path(sys.executable).resolve()),
                        asr_path='/synthetic/asr', intent_path='/synthetic/intent')
        for field, name, content in (
                ('voice_key_path','voice-key', KEY), ('certificate','cert',CERT),
                ('private_key','key',KEY_PEM), ('enrollment_path','enrollment',json.dumps(self.record())),
                ('ssap_key_path','ssap-key',json.dumps({'client-key':'c'*64})),
                ('ssap_pin_path','ssap-pin',json.dumps({'sha256':'d'*64}))):
            self.cfg[field] = str(self.write(name, content))
        self.manager = types.SimpleNamespace(lock=threading.RLock(), status=lambda: {'state': 'idle'})
        self.manager.control_spawn = types.SimpleNamespace(manager=self.manager)
    def write(self, name, content):
        path = self.root / name; path.write_text(content); path.chmod(0o600); return path
    def record(self):
        return dict(schema=1,kind='r2d2-tv-enrollment',tvCertificate=TV_CERT,
                    tvCertificateSha256=hashlib.sha256(ssl.PEM_cert_to_DER_cert(TV_CERT)).hexdigest(),
                    sparkCertificateSha256=hashlib.sha256(ssl.PEM_cert_to_DER_cert(CERT)).hexdigest(),
                    deviceId='e'*16, enrolledAt=self.now-100, expiresAt=self.now+1000)
    def entry(self, **kw):
        return re.ResidentEntry(self.manager, BROWSER_KEY, self.cfg,
            spark_ip='192.0.2.10', tv_ip='192.0.2.20', http_port=4181,
            base_handler=Base, wall=lambda:self.now, **kw)
    def request(self, owner, *, method='GET', path=None, peer='192.0.2.20', headers=None, body=b''):
        # Real HTTP parsing through an inherited local socketpair; no bind/connect.
        server, client = socket.socketpair()
        client.settimeout(2)
        errors = []
        def run():
            try: owner.http_handler(server, (peer, 1), None)
            except BaseException as exc: errors.append(type(exc).__name__)
            finally: server.close()
        thread = threading.Thread(target=run); thread.start()
        try:
            path = path if path is not None else '/'+KEY+'/state'
            headers = headers if headers is not None else [('Host','192.0.2.10:4181'),('X-R2D2-TV-Power','active')]
            raw = method+' '+path+' HTTP/1.1\r\n'+''.join(k+': '+v+'\r\n' for k,v in headers)+'\r\n'
            # These requests have no body. Do not half-close after send: the
            # refusing handler may already have replied and closed its socket.
            client.sendall(raw.encode('ascii')+body)
            output = bytearray()
            while True:
                piece=client.recv(4096)
                if not piece: break
                output.extend(piece)
            head,body=bytes(output).split(b'\r\n\r\n',1)
            return int(head.split()[1]),json.loads(body),head
        finally:
            client.close(); thread.join(2); self.assertFalse(thread.is_alive()); self.assertFalse(errors)


class PrivateFiles(Fixture):
    def test_identity_and_existing_record_read_without_writes(self):
        before={p.name:p.read_bytes() for p in self.root.iterdir()}
        ident=re.ExistingIdentity(self.cfg['certificate'],self.cfg['private_key'])
        enroll=re.ExistingEnrollment(self.cfg['enrollment_path'],ident,wall=lambda:self.now)
        self.assertEqual(enroll.current(),self.record())
        self.assertEqual(ident().sha256,self.record()['sparkCertificateSha256'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.root.iterdir()})
    def test_expired_future_long_or_boolean_windows_refuse(self):
        for changes in ({'expiresAt':self.now},{'enrolledAt':self.now+301,'expiresAt':self.now+500},
                        {'expiresAt':self.now+re.ROTATE_S},{'enrolledAt':True}):
            row=self.record();row.update(changes);self.write('enrollment',json.dumps(row))
            with self.subTest(changes=changes),self.assertRaisesRegex(ValueError,'EXPIRED'): self.entry()
    def test_changed_spark_tv_pin_deviceid_and_extra_fields_refuse(self):
        for changes in ({'sparkCertificateSha256':'0'*64},{'tvCertificateSha256':'0'*64},
                        {'deviceId':'private text'},{'extra':'refused'}):
            row=self.record();row.update(changes);self.write('enrollment',json.dumps(row))
            with self.subTest(changes=list(changes)),self.assertRaises(ValueError): self.entry()
    def test_each_current_read_rechecks_enrollment_expiry_and_spark_identity(self):
        owner=self.entry(); self.addCleanup(owner.close)
        self.now+=1100
        with self.assertRaisesRegex(ValueError,'EXPIRED'):owner.enrollment.current()
        self.now-=1100;self.write('cert',TV_CERT)
        with self.assertRaisesRegex(ValueError,'PIN'):owner.enrollment.current()
    def test_duplicate_json_and_extra_pem_material_refuse(self):
        self.write('enrollment','{"schema":1,"schema":1}')
        with self.assertRaises(ValueError):self.entry()
        for value in (CERT+KEY_PEM,'prefix'+CERT,CERT+CERT):
            with self.assertRaisesRegex(ValueError,'CERTIFICATE'):re.certificate_sha256(value)
    def test_owner_only_regular_single_link_and_symlink_refusal(self):
        path=Path(self.cfg['voice_key_path'])
        path.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'PRIVATE_FILE'):self.entry()
        path.chmod(0o600);os.link(path,self.root/'linked')
        with self.assertRaisesRegex(ValueError,'PRIVATE_FILE'):self.entry()
        (self.root/'linked').unlink();path.unlink();path.symlink_to(self.root/'cert')
        with self.assertRaisesRegex(ValueError,'PRIVATE_FILE'):self.entry()
    def test_fifo_refuses_without_blocking(self):
        path=Path(self.cfg['voice_key_path']);path.unlink();os.mkfifo(path,0o600)
        previous=signal.signal(signal.SIGALRM,lambda *a: (_ for _ in ()).throw(AssertionError('FIFO_BLOCK')))
        signal.setitimer(signal.ITIMER_REAL,1)
        try:
            with self.assertRaisesRegex(ValueError,'PRIVATE_FILE'):self.entry()
        finally:signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,previous)
    def test_held_file_replacement_and_close_failure_are_not_accepted(self):
        path=Path(self.cfg['voice_key_path']);original=os.lstat
        def replaced(p):
            if Path(p)==path:
                row=original(p);values=list(row);values[1]+=1;return os.stat_result(values)
            return original(p)
        with patch.object(re.os,'lstat',side_effect=replaced), self.assertRaises(ValueError):
            re._private_read(path,65)
        original_close=os.close
        def closed(fd):original_close(fd);raise OSError('private detail')
        with patch.object(re.os,'close',side_effect=closed),self.assertRaisesRegex(ValueError,'^RESIDENT_PRIVATE_CLOSE$'):
            re._private_read(path,65)
    def test_voice_key_must_be_distinct_and_canonical(self):
        for key in (BROWSER_KEY, 'A'*64, 'a'*63, ' '+KEY, KEY+' '):
            self.write('voice-key',key)
            with self.assertRaisesRegex(ValueError,'VOICE_KEY'):self.entry()


class Assembly(Fixture):
    def test_actual_constructor_has_one_p5_controller_and_no_external_start(self):
        with patch('socket.socket',side_effect=AssertionError('SOCKET')),patch('subprocess.Popen',side_effect=AssertionError('MODEL')):
            owner=self.entry()
            # One VoiceStatus, created before the controller and shared by every feed.
            for holder in (owner.helper,owner.helper.resident,owner.helper.controller):
                self.assertIs(holder.voice_status,owner.voice_status)
            self.assertIs(owner.helper.controller.feedback.status,owner.voice_status)
            self.assertIs(owner.helper.controller.feedback.show('CONFIRMATION_PENDING','unknown'),True)
            self.assertEqual(owner.pool.summary()['state'],'empty')
            self.assertIs(owner.helper.runtime.manager,self.manager)
            self.assertIs(owner.helper.controller,owner.helper.resident._controller)
            self.assertEqual(owner.pool.factory.prompt_phase,'P5')
            self.assertIs(owner.source.ssap,owner.helper.power_reader)
            self.assertFalse(owner.hook.running)
            self.assertIsNone(owner.helper.resident._listener)
            self.assertTrue(owner.close())
    def test_warm_only_starts_monitor_and_is_idempotent(self):
        owner=self.entry();self.addCleanup(owner.close)
        owner.hook.start=Mock(return_value=True)
        with patch.object(owner.pool,'begin_warm',side_effect=AssertionError('WARM')):
            self.assertTrue(owner.warm());self.assertTrue(owner.warm())
        owner.hook.start.assert_called_once()
    def test_serve_starts_once_waits_without_fixed_service_deadline_and_closes(self):
        owner=self.entry();self.addCleanup(owner.close)
        owner.helper.start=Mock(return_value=True)
        thread=threading.Thread(target=owner.serve);thread.start()
        end=time.monotonic()+1
        while not owner.helper.start.called and time.monotonic()<end:time.sleep(.001)
        self.assertTrue(thread.is_alive());self.assertFalse(owner.serve())
        self.assertTrue(owner.close());thread.join(1);self.assertFalse(thread.is_alive())
        owner.helper.start.assert_called_once();self.assertFalse(owner.warm())
    def test_listener_early_exit_closes_both_owners(self):
        owner=self.entry();owner.helper.start=Mock(return_value=True)
        owner.helper.resident.stopped.set()
        with self.assertRaisesRegex(RuntimeError,'LISTENER_STOPPED'):owner.serve()
        self.assertTrue(owner._closed);self.assertTrue(owner.hook.stopping.is_set())
    def test_start_failure_closes_helper_before_hook(self):
        owner=self.entry();order=[]
        owner.helper.start=Mock(side_effect=RuntimeError('START'))
        owner.helper.close=Mock(side_effect=lambda **k:order.append('helper') or True)
        owner.hook.close=Mock(side_effect=lambda **k:order.append('hook') or True)
        with self.assertRaisesRegex(RuntimeError,'START'):owner.serve()
        self.assertEqual(order,['helper','hook'])
    def test_cleanup_failure_stays_false_and_does_not_skip_hook(self):
        owner=self.entry();owner.helper.close=Mock(side_effect=RuntimeError('close'))
        owner.hook.close=Mock(return_value=True)
        self.assertFalse(owner.close(.25));self.assertFalse(owner.close())
        owner.hook.close.assert_called_once()
    def test_cleanup_uses_one_absolute_budget(self):
        owner=self.entry();seen=[]
        def first(**kw):seen.append(kw['timeout_s']);time.sleep(.02);return True
        owner.helper.close=Mock(side_effect=first)
        owner.hook.close=Mock(side_effect=lambda **kw:seen.append(kw['timeout_s']) or True)
        self.assertTrue(owner.close(.1));self.assertLess(seen[1],seen[0]-.01)


class Http(Fixture):
    def setUp(self):
        super().setUp();self.owner=self.entry();self.addCleanup(self.owner.close)
    def test_actual_http_active_standby_off_unknown_only(self):
        self.owner.hook.note_tv_power=Mock()
        for power in ('active','standby','off','unknown','Active',''):
            status,body,headers=self.request(self.owner,headers=[('Host','192.0.2.10:4181'),('X-R2D2-TV-Power',power)])
            self.assertEqual(status,200);self.assertEqual(set(body),{'warm','session','agent'});self.assertEqual(body['session'],'none')
            self.assertEqual(body['agent'],'ok')
            self.assertIn(b'Cache-Control: no-store',headers)
            self.owner.hook.note_tv_power.assert_called_with(power if power in ('active','standby','off') else 'unknown')
    def test_auth_refusals_never_note_activity(self):
        self.owner.hook.note_tv_power=Mock()
        host=[('Host','192.0.2.10:4181')]
        cases=[{'peer':'192.0.2.21'}, {'headers':[('Host','foreign')]},
               {'headers':host+[('Origin','null')]},{'headers':host+[('Host','192.0.2.10:4181')]},
               {'headers':host+[('X-R2D2-TV-Power','active'),('x-r2d2-tv-power','off')]},
               {'headers':host+[('Content-Length','1')]},{'headers':host+[('Transfer-Encoding','chunked')]},
               {'headers':host+[('Expect','anything')]},{'headers':host+[('X-Large','a'*2048)]}]
        for case in cases:
            with self.subTest(case=list(case)):
                self.assertEqual(self.request(self.owner,**case)[0],403)
        self.owner.hook.note_tv_power.assert_not_called()
    def test_expiry_or_shutdown_refuses_heartbeat(self):
        self.now+=1100;self.assertEqual(self.request(self.owner)[0],403)
        self.now-=1100;self.owner.close();self.assertEqual(self.request(self.owner)[0],403)
    def test_heartbeat_post_refuses_browser_routes_delegate(self):
        self.owner.hook.note_tv_power=Mock()
        self.assertEqual(self.request(self.owner,method='POST')[0],405)
        self.assertEqual(self.request(self.owner,path='/'+BROWSER_KEY+'/status')[1],{'browser':'get'})
        self.assertEqual(self.request(self.owner,method='POST',path='/'+BROWSER_KEY+'/start')[1],{'browser':'post'})
        self.assertEqual(self.request(self.owner,path='/'+KEY+'/state?x')[1],{'browser':'get'})
        self.owner.hook.note_tv_power.assert_not_called()

    def test_state_reports_agent_pause_and_feeds_readiness(self):
        self.owner.hook.note_tv_power=Mock()
        self.assertEqual(self.owner.voice_status.home_view()['chip'],'starting')
        with patch.object(type(self.owner.hook),'state',new_callable=lambda: property(lambda hook: 'ready')):
            self.assertEqual(self.request(self.owner)[1],{'warm':'ready','session':'none','agent':'ok'})
            self.assertEqual(self.owner.voice_status.home_view()['chip'],'ready')
            self.owner.helper.killswitch.pause('TEST')
            self.assertEqual(self.request(self.owner)[1]['agent'],'paused')
        self.assertEqual(self.owner.voice_status.home_view()['chip'],'paused')


class VoiceHttp(Fixture):
    HOST=[('Host','192.0.2.10:4181')]
    def setUp(self):
        super().setUp();self.owner=self.entry();self.addCleanup(self.owner.close)
        self.device=resident_device_key(KEY,self.record()['tvCertificateSha256'])
        self.boot,self.seq='1'*16,0
    def row(self,phase='pressed',press='a'*16,mac=None,**changes):
        self.seq+=1
        row=dict(schema=1,boot=self.boot,seq=self.seq,press=press,phase=phase,code=None,deadlineMs=None,cut=False)
        row.update(changes)
        row['mac']=mac or voice_status.ping_mac(self.device,{k:v for k,v in row.items() if k!='mac'})
        return row
    def post(self,body,headers=None,**kw):
        body=body if type(body) is bytes else json.dumps(body,separators=(',',':')).encode()
        headers=headers if headers is not None else self.HOST+[('Content-Type','application/json'),('Content-Length',str(len(body)))]
        return self.request(self.owner,method='POST',path='/'+KEY+'/voice-phase',headers=headers,body=body,**kw)
    def test_authenticated_phase_ping_updates_status_and_reports_surface(self):
        status,body,_=self.post(self.row('listening',deadlineMs=5400))
        self.assertEqual((status,body),(200,{'ok':True,'surface':'other'}))
        view=self.owner.voice_status.home_view()
        self.assertEqual((view['phase'],view['meterMs']),('listening',5400))
        self.owner.voice_status.wait_home(0)
        self.assertEqual(self.post(self.row('released'))[1],{'ok':True,'surface':'home'})
    def test_replay_bad_mac_and_shape_refuse_without_display_change(self):
        first=self.row()
        self.assertEqual(self.post(first)[0],200)
        before=self.owner.voice_status.home_view()['seq']
        other=resident_device_key(KEY,'0'*64)
        cases=[first,dict(first,seq=first['seq']-1 or 1),self.row(mac='0'*64),
               self.row(mac=voice_status.ping_mac(other,{k:v for k,v in self.row().items() if k!='mac'})),
               dict(self.row(),extra=1),dict(self.row(),phase='doing'),
               b'{"schema":1,"schema":1}',b'\xff'*10,b'[]',b'{'+b' '*300+b'}']
        for body in cases:
            with self.subTest(body=body):self.assertEqual(self.post(body)[0],403)
        self.assertEqual(self.owner.voice_status.home_view()['seq'],before)
        self.boot='2'*16;self.seq=0
        self.assertEqual(self.post(self.row('pressed',press='b'*16))[0],200)
    def test_phase_ping_http_gates(self):
        body=json.dumps(self.row()).encode()
        good=self.HOST+[('Content-Type','application/json'),('Content-Length',str(len(body)))]
        cases=[{'peer':'192.0.2.21'},{'headers':[('Host','foreign')]+good[1:]},
               {'headers':good+[('Origin','null')]},{'headers':good+[('Transfer-Encoding','chunked')]},
               {'headers':good+[('Expect','anything')]},{'headers':good+[('Content-Type','text/plain')]},
               {'headers':self.HOST+[('Content-Type','text/plain'),('Content-Length',str(len(body)))]},
               {'headers':self.HOST+[('Content-Type','application/json'),('Content-Length','0'+str(len(body)))]},
               {'headers':self.HOST+[('Content-Type','application/json'),('Content-Length','257')]},
               {'headers':self.HOST+[('Content-Type','application/json')]},
               {'headers':good+[('X-Large','a'*2048)]}]
        for case in cases:
            with self.subTest(case=case):self.assertEqual(self.post(body,**case)[0],403)
        self.assertEqual(self.owner.voice_status.home_view()['phase'],'idle')
        self.assertEqual(self.post(body,headers=good)[0],200)
    def test_methods_and_foreign_keys(self):
        self.assertEqual(self.request(self.owner,path='/'+KEY+'/voice-phase')[0],405)
        self.assertEqual(self.request(self.owner,method='POST',path='/'+KEY+'/voice-receipt/'+'a'*16)[0],405)
        self.assertEqual(self.request(self.owner,path='/'+BROWSER_KEY+'/voice-receipt/'+'a'*16)[1],{'browser':'get'})
        self.assertEqual(self.request(self.owner,path='/'+KEY+'/voice-receipt/'+'a'*16+'?x')[1],{'browser':'get'})
        self.assertEqual(self.request(self.owner,path='/'+KEY+'/voice-receipt/'+'A'*16)[1],{'browser':'get'})
    def test_receipt_shape_and_gates(self):
        self.post(self.row('pressed'));self.post(self.row('listening'));self.post(self.row('released'))
        token=self.owner.voice_status.begin_utterance()
        self.owner.voice_status.outcome(token,'refused','RATE_LIMITED')
        path='/'+KEY+'/voice-receipt/'+'a'*16
        status,body,headers=self.request(self.owner,path=path,headers=self.HOST)
        self.assertEqual((status,body),(200,dict(schema=1,press='a'*16,done=True,toast='Too many requests, wait a moment',shown=False)))
        self.assertIn(b'Cache-Control: no-store',headers)
        for case in ({'peer':'192.0.2.21'},{'headers':self.HOST+[('Origin','null')]},
                     {'headers':self.HOST+[('Content-Length','1')]},{'headers':[('Host','x')]}):
            with self.subTest(case=case):self.assertEqual(self.request(self.owner,path=path,**case)[0],403)
    def feed(self,since,**kw):
        return self.request(self.owner,path='/'+BROWSER_KEY+'/voice/'+str(since),
                            headers=self.HOST+[('Origin','null')],**kw)
    def test_home_feed_replies_now_with_cors_and_ascii_view(self):
        status,body,headers=self.feed(0)
        self.assertEqual(status,200);self.assertEqual(body['seq'],1);self.assertEqual(body['phase'],'idle')
        self.assertIn(b'Access-Control-Allow-Origin: null',headers)
        self.assertLessEqual(len(json.dumps(body,separators=(',',':'))),512)
    def test_home_feed_is_absent_once_voice_stops(self):
        # A stopped owner never serves a frozen chip: Home sees 404, goes offline and backs off.
        self.assertEqual(self.feed(0)[0],200)
        self.assertTrue(self.owner.close())
        status,body,headers=self.feed(0)
        self.assertEqual((status,body),(404,{}));self.assertNotIn(b'Access-Control-Allow-Origin',headers)
    def test_home_feed_waits_then_wakes_on_change(self):
        seq=self.owner.voice_status.home_view()['seq']
        out=[]
        thread=threading.Thread(target=lambda:out.append(self.feed(seq)));thread.start()
        end=time.monotonic()+2
        while self.owner.voice_status._home_waiter is None and time.monotonic()<end:time.sleep(.005)
        self.assertTrue(thread.is_alive())
        self.post(self.row('pressed'))
        thread.join(2);self.assertFalse(thread.is_alive())
        self.assertEqual((out[0][1]['seq'],out[0][1]['phase']),(seq+1,'pressed'))
    def test_home_feed_client_gone_or_close_releases_the_slot(self):
        seq=self.owner.voice_status.home_view()['seq']
        server,client=socket.socketpair();done=threading.Event();replies=[]
        def run():
            with patch.object(Base,'reply',autospec=True,side_effect=lambda *a,**k:replies.append(a[1])):
                try:self.owner.http_handler(server,('192.0.2.20',1),None)
                finally:server.close();done.set()
        thread=threading.Thread(target=run);thread.start()
        client.sendall(('GET /'+BROWSER_KEY+'/voice/'+str(seq)+' HTTP/1.1\r\nHost: 192.0.2.10:4181\r\nOrigin: null\r\n\r\n').encode())
        end=time.monotonic()+2
        while self.owner.voice_status._home_waiter is None and time.monotonic()<end:time.sleep(.005)
        self.assertIsNotNone(self.owner.voice_status._home_waiter)
        client.close()
        self.assertTrue(done.wait(2));thread.join(1)
        self.assertEqual(replies,[])
        out=[]
        thread=threading.Thread(target=lambda:out.append(self.feed(seq)));thread.start()
        end=time.monotonic()+2
        while self.owner.voice_status._home_waiter is None and time.monotonic()<end:time.sleep(.005)
        self.owner.close()
        thread.join(2);self.assertFalse(thread.is_alive());self.assertEqual(out[0][0],200)
    def test_serve_loop_pushes_on_change_and_keeps_only_a_visible_card_alive(self):
        pushes=[]
        push=Mock(side_effect=lambda manager,status,timeout_s:pushes.append((manager,status,timeout_s,time.monotonic())))
        self.owner.helper.start=Mock(return_value=True)
        with patch.object(re,'_card_pusher',return_value=push):
            thread=threading.Thread(target=self.owner.serve);thread.start()
            end=time.monotonic()+3
            while not pushes and time.monotonic()<end:time.sleep(.005)
            self.assertIs(pushes[0][0],self.manager);self.assertIs(pushes[0][1],self.owner.voice_status)
            self.assertEqual(pushes[0][2],.25)
            time.sleep(.3);count=len(pushes);time.sleep(.6)
            self.assertEqual(len(pushes),count)  # an idle viewer gets no keepalive pushes
            sent=time.monotonic()
            self.post(self.row('pressed'))
            while len(pushes)==count and time.monotonic()<end:time.sleep(.002)
            self.assertLess(pushes[count][3]-sent,.2)
            time.sleep(.6)
            self.assertGreaterEqual(len(pushes),count+3)  # a visible card stays alive at the tick rate
            self.assertTrue(self.owner.close());thread.join(1);self.assertFalse(thread.is_alive())
        self.assertLessEqual(len(pushes),count+1+int((time.monotonic()-sent)*4)+2)
    def test_missing_or_failing_pusher_is_a_no_op(self):
        for pusher in (None,Mock(side_effect=RuntimeError('push'))):
            owner=self.entry();owner.helper.start=Mock(return_value=True)
            with self.subTest(pusher=pusher),patch.object(re,'_card_pusher',return_value=pusher):
                thread=threading.Thread(target=owner.serve);thread.start()
                time.sleep(.3);self.assertTrue(thread.is_alive())
                self.assertTrue(owner.close());thread.join(1);self.assertFalse(thread.is_alive())
    def test_actual_viewer_pusher_is_found(self):
        self.assertTrue(callable(re._card_pusher()))


if __name__=='__main__':unittest.main()
