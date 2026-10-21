"""Real loopback TLS1.3 and actual Python VoiceIngress, synthetic PCM only."""
import hashlib,importlib.util,json,os,secrets,shutil,socket,ssl,subprocess,sys,tempfile,threading,time,unittest
from pathlib import Path
HERE=Path(__file__).resolve().parent
BACKEND=Path(os.environ.get('R2D2_VOICE_BACKEND', str(HERE.parents[1]/'backend'))).resolve()
sys.path.insert(0,str(BACKEND));import ingress,service
NODE=shutil.which('node');OPENSSL=shutil.which('openssl')
class Interop(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.temp=tempfile.TemporaryDirectory(prefix='r2d2-tls-public-fixtures-',dir=None);cls.root=Path(cls.temp.name);cls.root.chmod(0o700)
  def cmd(*a):subprocess.run([OPENSSL,*a],cwd=cls.root,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=15)
  cmd('req','-x509','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout','ca.key','-out','ca.pem','-days','1','-subj','/CN=R2D2 Test CA')
  for name,usage in [('server','serverAuth'),('client','clientAuth')]:
   cmd('req','-new','-newkey','ec','-pkeyopt','ec_paramgen_curve:P-256','-nodes','-keyout',name+'.key','-out',name+'.csr','-subj','/CN='+('voice.test'if name=='server'else'fixture-client'))
   (cls.root/(name+'.ext')).write_text('basicConstraints=CA:FALSE\nkeyUsage=digitalSignature\nextendedKeyUsage='+usage+'\n'+('subjectAltName=DNS:voice.test,IP:127.0.0.1\n'if name=='server'else''))
   cmd('x509','-req','-in',name+'.csr','-CA','ca.pem','-CAkey','ca.key','-CAcreateserial','-out',name+'.pem','-days','1','-extfile',name+'.ext')
  cls.serverpin=hashlib.sha256(ssl.PEM_cert_to_DER_cert((cls.root/'server.pem').read_text())).hexdigest()
  cls.clientpin=hashlib.sha256(ssl.PEM_cert_to_DER_cert((cls.root/'client.pem').read_text())).hexdigest()
 @classmethod
 def tearDownClass(cls):cls.temp.cleanup()
 def run_case(self,mode='normal',bad_pin=False,bad_name=False,bad_hmac=False,refuse_arm=False,tls12=False,fragment=False,slow_asr=False,bad_client_pin=False,bad_client_certificate=False):
  key=secrets.token_bytes(32);state={'asrCalls':0,'executions':0,'armCalls':0,'cancelCalls':0};errors=[]
  class ASR:
   qualified=True
   def transcribe(_,audio,rate,timeout_s):
    state['asrCalls']+=1
    if slow_asr:time.sleep(1.2)
    state['audioBytes']=len(audio);state['audioHash']=hashlib.sha256(audio).hexdigest();state['audioRef']=audio;return 'fixture command'
  class Controller:
   def arm(_):
    state['armCalls']+=1
    if refuse_arm:raise ingress.Refused('CONTROLLER_BUSY')
    return object()
   def execute(_,lease,text):state['executions']+=1;return ingress.Outcome('accepted','FIXTURE')
   def cancel(_,lease=None):state['cancelCalls']+=1
  context=ingress.tls_context(str(self.root/'server.pem'),str(self.root/'server.key'),str(self.root/'ca.pem'))
  if tls12:context.minimum_version=context.maximum_version=ssl.TLSVersion.TLSv1_2
  if fragment:
   class FragmentSSL(ssl.SSLSocket):
    def sendall(self,data,*args,**kwargs):
     if len(data)==32:
      super().sendall(data[:1],*args,**kwargs);time.sleep(0.01);return super().sendall(data[1:],*args,**kwargs)
     return super().sendall(data,*args,**kwargs)
   context.sslsocket_class=FragmentSSL
  reservation=socket.socket();reservation.bind(('127.0.0.1',0));port=reservation.getsockname()[1];reservation.close()
  class Broker(ingress.VoiceIngress):
   def connection(self,sock):
    state['tls']=sock.version();out=super().connection(sock);state['outcome']=out.code;return out
  broker=Broker(Controller(),ingress.Device('0'*64 if bad_client_pin else self.clientpin,key),ASR())
  # Production VoiceService owns the real TLS wrapper, terminal frame and close.
  # TLS1.2 is solely a negative fixture; production rejects its construction.
  if tls12:
   context.minimum_version=context.maximum_version=ssl.TLSVersion.TLSv1_3
  server=service.VoiceService('127.0.0.1',port,context,broker,qualified=True)
  if tls12:context.minimum_version=context.maximum_version=ssl.TLSVersion.TLSv1_2
  def serve():
   try:server.serve(max_connections=1,lifetime_s=8)
   except Exception as e:errors.append(type(e).__name__)
  thread=threading.Thread(target=serve);thread.start()
  until=time.monotonic()+3
  while not errors and (server.listener is None or server.listener.getsockname()[1]!=port) and time.monotonic()<until:time.sleep(.005)
  opts={'port':port,'ca':str(self.root/'ca.pem'),'certificate':str(self.root/('server.pem'if bad_client_certificate else'client.pem')),'privateKey':str(self.root/('server.key'if bad_client_certificate else'client.key')),'pin':'0'*64 if bad_pin else self.serverpin,'serverName':'wrong.test'if bad_name else'voice.test','deviceKey':(secrets.token_bytes(32)if bad_hmac else key).hex(),'mode':mode}
  f=self.root/('case-'+secrets.token_hex(4)+'.json');f.write_text(json.dumps(opts));f.chmod(0o600)
  try:
   p=subprocess.run([NODE,str(HERE/'test_client_driver.js'),str(f)],capture_output=True,text=True,timeout=18);self.assertEqual(p.returncode,0,p.stderr);result=json.loads(p.stdout)
  finally:f.unlink();server.close();thread.join(8)
  self.assertFalse(thread.is_alive(),'server thread leaked')
  if 'audioRef'in state:self.assertEqual(len(state.pop('audioRef')),0,'backend retained PCM')
  return result,state,errors
 def test_real_tls13_pcm_release_and_source_buffer_copy(self):
  r,s,e=self.run_case();self.assertEqual(r['reason'],'released');self.assertEqual(s['tls'],'TLSv1.3');self.assertEqual(s['audioBytes'],6400);self.assertEqual(s['audioHash'],hashlib.sha256(bytes(i%251 for i in range(6400))).hexdigest());self.assertEqual(s['executions'],1);self.assertEqual(r['readyCalls'],1);self.assertEqual(r['closeCalls'],1)
 def test_odd_provider_chunks_coalesce_without_reordering(self):
  r,s,e=self.run_case('adapter',fragment=True);self.assertEqual(r['reason'],'released');self.assertTrue(r['providerReaped']);self.assertEqual(s['audioBytes'],6400);self.assertEqual(s['audioHash'],hashlib.sha256(bytes(i%251 for i in range(6400))).hexdigest())
 def test_asr_longer_than_old_one_second_close_completes(self):
  r,s,e=self.run_case(slow_asr=True);self.assertEqual(r['reason'],'released');self.assertTrue(r['actionAcknowledged']);self.assertEqual(s['asrCalls'],1)
 def test_server_only_certificate_cannot_authenticate_as_client(self):
  r,s,e=self.run_case(bad_client_certificate=True);self.assertEqual(r['readyCalls'],0);self.assertFalse(r['connected']);self.assertNotIn('tls',s);self.assertEqual(s['armCalls'],0)
 def test_wrong_client_certificate_pin_never_becomes_ready(self):
  r,s,e=self.run_case(bad_client_pin=True);self.assertEqual(r['readyCalls'],0);self.assertEqual(s['armCalls'],0);self.assertEqual(s['asrCalls'],0)
 def test_cancellation_never_invokes_asr(self):
  r,s,e=self.run_case('cancel');self.assertEqual(r['reason'],'cancelled');self.assertEqual(s['outcome'],'VOICE_CANCELLED');self.assertEqual(s['asrCalls'],0);self.assertEqual(s['executions'],0)
 def test_exact_ten_second_byte_bound(self):
  r,s,e=self.run_case('maximum');self.assertEqual(r['reason'],'released');self.assertEqual(s['audioBytes'],320000)
 def test_wrong_server_pin_never_becomes_ready(self):
  r,s,e=self.run_case(bad_pin=True);self.assertEqual(r['readyCalls'],0);self.assertFalse(r['connected']);self.assertEqual(s['asrCalls'],0)
 def test_wrong_server_hostname_never_becomes_ready(self):
  r,s,e=self.run_case(bad_name=True);self.assertEqual(r['readyCalls'],0);self.assertFalse(r['connected']);self.assertEqual(s['asrCalls'],0)
 def test_tls12_refused_before_readiness(self):
  r,s,e=self.run_case(tls12=True);self.assertEqual(r['readyCalls'],0);self.assertFalse(r['connected']);self.assertEqual(s['asrCalls'],0)
 def test_bad_hmac_server_eof_is_not_controller_ack(self):
  r,s,e=self.run_case('wait',bad_hmac=True);self.assertEqual(s['outcome'],'DEVICE_NOT_AUTHENTICATED');self.assertEqual(s['asrCalls'],0);self.assertEqual(r.get('reason'),'SERVER_REFUSED');self.assertEqual(r['outcome']['code'],'DEVICE_NOT_AUTHENTICATED')
 def test_backend_arm_refusal_closes_transport_promptly(self):
  r,s,e=self.run_case('wait',refuse_arm=True);self.assertEqual(s['outcome'],'CONTROLLER_BUSY');self.assertEqual(s['asrCalls'],0);self.assertEqual(r.get('reason'),'SERVER_REFUSED');self.assertEqual(r['outcome']['code'],'CONTROLLER_BUSY')
if __name__=='__main__':unittest.main()
