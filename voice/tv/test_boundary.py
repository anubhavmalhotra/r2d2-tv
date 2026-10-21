"""Authored generic fixture only; private original-source controls are separate."""
import hashlib,unittest
from unittest.mock import patch
import patch_firmware as p
Q=b'Item {\n    signal goMagicTips()\n        function preProcess(key, pressed, autoRepeat, deviceId) {\n            return genericWork(key);\n        }\n}\n'
A=b"function handleHotkeys(key, pressed) {\n switch(key) {\n    case WebOS.Key_webOS_Voice:\n        call('com.webos.service.voiceconductor', '/setInputEvent');\n        return KeyPolicy.Accepted;\n    case WebOS.Key_webOS_TonePlusVoice:\n        return keepOtherKey();\n }\n}\n"
class Tests(unittest.TestCase):
 def fixture(self):return {'StarfishKeyFilter.qml':Q,'appLaunch.js':A}
 def bindings(self):return {n:hashlib.sha256(b).hexdigest()for n,b in self.fixture().items()}
 def test_hash_drift_unknown_keys_wrong_types_and_repeat_refuse(self):
  with self.assertRaisesRegex(ValueError,'PREIMAGE_DRIFT'):p.transform(self.fixture())
  with patch.dict(p.PINS,self.bindings(),clear=True):
   good=self.fixture();out=p.transform(good)
   for bad in ({},dict(good,extra=b'PRIVATE_SENTINEL'),dict(good,**{'StarfishKeyFilter.qml':Q+b' '}),dict(good,**{'appLaunch.js':A.decode()}),out):
    with self.assertRaises(ValueError):p.transform(bad)
 def test_authored_early_gate_and_vendor_guard_leave_other_bytes_exact(self):
  with patch.dict(p.PINS,self.bindings(),clear=True):out=p.transform(self.fixture())
  q=out['StarfishKeyFilter.qml'].decode();a=out['appLaunch.js'].decode()
  self.assertEqual(q.replace(p.SIGNAL,'',1).replace(p.GATE,'',1).encode(),Q)
  self.assertIn(p.GATE,q);self.assertLess(q.index(p.GATE),q.index('genericWork(key)'));self.assertNotIn('setInputEvent',a)
  self.assertEqual(a[a.index('    case WebOS.Key_webOS_TonePlusVoice:'):],A.decode()[A.decode().index('    case WebOS.Key_webOS_TonePlusVoice:'):])
  self.assertIn('catch (ignored)',q);self.assertIn('return KeyPolicy.Accepted;',q)
if __name__=='__main__':unittest.main()
