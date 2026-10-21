import struct,unittest
from pcm_adapter import Pcm16IngressAdapter,PcmRefused,FRAME_BYTES,MAX_AUDIO
class Tests(unittest.TestCase):
 def test_native_post_swap_passes_le_unchanged_pre_swap_be_converts_once(self):
  values=[-32768,-1234,-1,0,1,1234,32767]*50
  le=bytearray().join(struct.pack('<h',v)for v in values);be=bytearray().join(struct.pack('>h',v)for v in values)
  for tap,data in [('capture-notify-after-native-swap',le),('decoder-before-native-swap',be)]:
   a=Pcm16IngressAdapter(tap,clock=lambda:0);frames=a.feed(data[:1])+a.feed(data[1:639])+a.feed(data[639:])+a.finish()
   self.assertEqual(bytearray().join(frames),le);self.assertTrue(all(type(f)is bytearray and len(f)%2==0 and len(f)<=FRAME_BYTES for f in frames));self.assertTrue(a.closed);self.assertEqual(a.pending,bytearray())
  self.assertNotEqual(le,be)
 def test_ten_second_320k_five_hundred_frames_and_original_expiry_are_exact(self):
  now=[0];a=Pcm16IngressAdapter('capture-notify-after-native-swap',clock=lambda:now[0]);out=[]
  for i in range(40):out+=a.feed(bytearray(8000))
  self.assertEqual(sum(map(len,out)),MAX_AUDIO);self.assertEqual(len(out),500)
  with self.assertRaisesRegex(PcmRefused,'PCM_INPUT_BOUND'):a.feed(b'\0\0')
  self.assertTrue(a.closed)
  b=Pcm16IngressAdapter('capture-notify-after-native-swap',clock=lambda:now[0]);b.feed(b'\1');now[0]=10
  with self.assertRaisesRegex(PcmRefused,'PCM_EXPIRED'):b.finish()
  self.assertTrue(b.closed);self.assertEqual(b.pending,bytearray())
 def test_bad_taps_sizes_odd_terminal_and_clock_failure_close_and_clear(self):
  for tap in ['big','little','remote','socket',None]:
   with self.assertRaisesRegex(PcmRefused,'PCM_TAP_UNBOUND'):Pcm16IngressAdapter(tap)
  for value in [b'',b'x'*8193,'PRIVATE_SENTINEL']:
   a=Pcm16IngressAdapter('capture-notify-after-native-swap',clock=lambda:0);a.feed(b'\1')
   with self.assertRaises(PcmRefused):a.feed(value)
   self.assertTrue(a.closed);self.assertEqual(a.pending,bytearray())
  a=Pcm16IngressAdapter('capture-notify-after-native-swap',clock=lambda:0);a.feed(b'\1')
  with self.assertRaisesRegex(PcmRefused,'PCM_PARTIAL_SAMPLE'):a.finish()
  self.assertTrue(a.closed);self.assertEqual(a.pending,bytearray())
  now=[1];a=Pcm16IngressAdapter('capture-notify-after-native-swap',clock=lambda:now[0]);now[0]=0
  with self.assertRaisesRegex(PcmRefused,'PCM_EXPIRED'):a.feed(b'\0\0')
 def test_cancel_is_only_adapter_state_and_outputs_are_caller_owned_mutable(self):
  a=Pcm16IngressAdapter('capture-notify-after-native-swap',clock=lambda:0);a.feed(b'\x01\x02');a.close();self.assertTrue(a.closed);self.assertEqual(a.pending,bytearray())
  with self.assertRaisesRegex(PcmRefused,'PCM_CLOSED'):a.feed(b'\0\0')
  b=Pcm16IngressAdapter('capture-notify-after-native-swap',clock=lambda:0);frames=b.feed(bytearray(range(256))*3);self.assertEqual(len(frames),1);self.assertEqual(len(b.pending),128);b.close();frames[0][:]=b'\0'*len(frames[0]);self.assertEqual(frames[0],bytearray(640))
if __name__=='__main__':unittest.main()
