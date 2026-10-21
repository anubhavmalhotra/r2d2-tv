import json,unittest
from hidraw_contract import *
def raw(v):return json.dumps(v).encode()
def inventory():return {'returnValue':True,'devices':[{'id':'remote7','key':'SYNTHETIC_KEY','deviceType':'remote','deviceName':'Synthetic fixture'}]}
class FakeService:
 def __init__(self):self.calls=[]
 def call(self,method,payload):
  self.calls.append((method,payload))
  if method.endswith('/getDevices'):assert payload=={};return raw(inventory())
  if method.endswith('/startStreaming'):
   assert payload=={'id':'remote7','key':'SYNTHETIC_KEY','socketType':'socket','subscribe':True};return raw({'returnValue':True,'id':'remote7','state':'voice start','socketPath':'/var/voiceinput/synthetic.soc'})
  if method.endswith('/stopStreaming'):assert payload=={'id':'remote7','key':'SYNTHETIC_KEY'};return raw({'returnValue':True})
  raise AssertionError('UNREVIEWED_METHOD')
class Tests(unittest.TestCase):
 def record(self,clock=lambda:0):return parse_inventory(raw(inventory()),clock=clock)[0][0]
 def test_exact_fake_service_enum_start_stop_no_internal_identity_fields_or_socket(self):
  service=FakeService();records,projection=parse_inventory(service.call(*inventory_request()),clock=lambda:0);self.assertEqual(projection,{'devicesObserved':1,'remoteRecords':1,'nativeCallerOrCaptureQualified':False});s=HidrawCaptureContract(records[0]);s.observe_start(service.call(*s.start_request()));self.assertTrue(s.startAck)
  with self.assertRaisesRegex(HidrawRefused,'CAPTURE_SOCKET_BINDING_UNQUALIFIED'):s.read_parameters()
  s.observe_stop(service.call(*s.stop_request()));self.assertTrue(s.stopAck);self.assertEqual(s._key,bytearray());self.assertIsNone(s.path);self.assertEqual(len(service.calls),3)
  self.assertFalse(s.summary()['physicalMicStoppedProven']);self.assertFalse(s.summary()['microphoneCaptured']);self.assertNotIn('SYNTHETIC_KEY',repr(s));self.assertNotIn('remote7',repr(s))
  for method,payload in service.calls:self.assertTrue(method.startswith(SERVICE+'/'));self.assertFalse(set(payload)&{'senderServiceName','keyType','clientId','nBufferSize'})
 def test_source_key_record_one_use_stale_and_extra_or_duplicate_reply_refuse(self):
  record=self.record();s=HidrawCaptureContract(record)
  with self.assertRaisesRegex(HidrawRefused,'DEVICE_STALE'):HidrawCaptureContract(record)
  now=[0];record=self.record(lambda:now[0]);now[0]=3
  with self.assertRaisesRegex(HidrawRefused,'DEVICE_STALE'):HidrawCaptureContract(record)
  self.assertEqual(record.key,bytearray())
  for bad in (b'null',b'[]',raw(dict(inventory(),extra='PRIVATE_SENTINEL')),raw(inventory()).replace(b'"returnValue": true',b'"returnValue": true,"returnValue": true'),b'x'*8193,raw({'returnValue':True,'devices':[dict(inventory()['devices'][0],deviceName='\ud800')]})):
   with self.assertRaises(HidrawRefused):parse_inventory(bad)
 def test_unknown_start_no_retry_only_one_owned_id_key_stop_request(self):
  s=HidrawCaptureContract(self.record());s.start_request();s.start_unknown()
  with self.assertRaisesRegex(HidrawRefused,'START_REPLAY'):s.start_request()
  method,payload=s.stop_request();self.assertEqual(payload,{'id':'remote7','key':'SYNTHETIC_KEY'})
  with self.assertRaisesRegex(HidrawRefused,'STOP_REPLAY'):s.stop_request()
  s.stop_unknown();self.assertEqual(s._key,bytearray());self.assertIsNone(s.path);self.assertFalse(s.summary()['stopReplyObserved'])
 def test_foreign_id_paths_start_states_and_stop_ack_never_promote_physical_cleanup(self):
  good={'returnValue':True,'id':'remote7','state':'voice start','socketPath':'/var/voiceinput/synthetic.soc'}
  for delta in ({'id':'remote8'},{'socketPath':'/tmp/voice.soc'},{'socketPath':'/var/voiceinput/../voice.soc'},{'state':'voice stop'},{'returnValue':False},{'extra':'PRIVATE_SENTINEL'}):
   s=HidrawCaptureContract(self.record());s.start_request()
   with self.assertRaises(HidrawRefused):s.observe_start(raw(dict(good,**delta)))
   self.assertEqual(s.state,'start-unknown');self.assertFalse(s.startAck);s.stop_request();s.stop_unknown();self.assertEqual(s._key,bytearray())
  s=HidrawCaptureContract(self.record());s.start_request();s.observe_start(raw(good));s.stop_request();s.observe_stop(raw({'returnValue':True,'id':'remote7','state':'voice stop','reason':'audio device stopped'}));self.assertFalse(s.summary()['physicalMicStoppedProven'])
 def test_unknown_stop_or_retired_contract_cannot_retry_or_expose_private_material(self):
  s=HidrawCaptureContract(self.record());s.start_request();s.stop_request()
  with self.assertRaises(HidrawRefused):s.observe_stop(raw({'returnValue':False,'errorText':'PRIVATE_SENTINEL'}))
  self.assertEqual(s.state,'stop-unknown');self.assertEqual(s._key,bytearray());self.assertIsNone(s.path)
  with self.assertRaises(HidrawRefused):s.stop_request()
  s.close();self.assertEqual(s.summary()['state'],'retired');self.assertNotIn('SYNTHETIC_KEY',json.dumps(s.summary()))
 def test_original_inventory_and_phase_expiry_no_renewal_and_single_ten_second_stop_decision(self):
  now=[0];s=HidrawCaptureContract(self.record(lambda:now[0]));now[0]=3
  with self.assertRaisesRegex(HidrawRefused,'START_EXPIRED'):s.start_request()
  self.assertEqual(s._key,bytearray())
  now[0]=0;s=HidrawCaptureContract(self.record(lambda:now[0]));s.start_request();now[0]=2
  with self.assertRaisesRegex(HidrawRefused,'START_EXPIRED'):s.observe_start(raw({'returnValue':True,'id':'remote7','state':'voice start','socketPath':'/var/voiceinput/synthetic.soc'}))
  self.assertEqual(s.state,'start-unknown');now[0]=10;decision=s.tick();self.assertEqual(decision,(SERVICE+'/stopStreaming',{'id':'remote7','key':'SYNTHETIC_KEY'}));self.assertIsNone(s.tick());self.assertEqual(s.state,'stop-pending');self.assertNotEqual(s._key,bytearray());now[0]=12;s.tick();self.assertEqual(s.state,'stop-unknown');self.assertEqual(s._key,bytearray());self.assertFalse(s.stopAck)
 def test_invalid_clock_never_creates_unbounded_cleanup_lease(self):
  for bad in (float('nan'),float('inf'),'invalid',-1):
   now=[0];s=HidrawCaptureContract(self.record(lambda:now[0]));s.start_request();now[0]=bad;self.assertIsNone(s.tick());self.assertEqual(s.state,'stop-unknown');self.assertEqual(s._key,bytearray());self.assertFalse(s.stopAck)
  now=[0];s=HidrawCaptureContract(self.record(lambda:now[0]));s.start_request();now[0]=float('nan')
  with self.assertRaisesRegex(HidrawRefused,'SESSION_CLOCK'):s.stop_request()
  self.assertEqual(s.state,'stop-unknown');self.assertEqual(s._key,bytearray())
if __name__=='__main__':unittest.main()
