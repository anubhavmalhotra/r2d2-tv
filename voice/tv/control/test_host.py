import hashlib,hmac,json,unittest
import stop_control as C

class Signals:
    def __init__(self):self.state=dict(released=False,cancelled=False,closed=False,reason=None)
    def snapshot(self):return dict(self.state)

class Tests(unittest.TestCase):
    def setUp(self):
        self.now=0;self.s=Signals();self.key=bytearray([7])*32
        self.c=C.StopControl(self.s,Signals,self.key,'a'*48,clock=lambda:self.now)
    def tearDown(self):
        self.c.close();self.assertTrue(self.c.summary()['keyCleared']);self.assertTrue(self.c.summary()['closed'])
    def request(self,n,bad=False):
        row=dict(v=1,attempt='a'*48,counter=n)
        row['mac']='0'*64 if bad else hmac.new(self.key,C.message('request','a'*48,n),hashlib.sha256).hexdigest()
        return C.canonical(row)
    def test_gap_boundary(self):
        self.c.respond(self.request(1));end=self.c._end;self.now=1
        self.c.respond(self.request(17));self.assertEqual(self.c._counter,17);self.assertEqual(self.c._end,end)
        self.assertRaisesRegex(ValueError,'CONTROL_REPLAY',self.c.respond,self.request(34))
        self.assertEqual(self.c._counter,17)
    def test_first_strict(self):
        self.assertRaisesRegex(ValueError,'CONTROL_REPLAY',self.c.respond,self.request(2));self.assertIsNone(self.c._end)
    def test_replay(self):
        self.c.respond(self.request(1));self.c.respond(self.request(3))
        for n in (1,2,3):self.assertRaisesRegex(ValueError,'CONTROL_REPLAY',self.c.respond,self.request(n))
    def test_bad_mac_does_not_consume(self):
        self.c.respond(self.request(1));self.assertRaisesRegex(ValueError,'CONTROL_AUTH',self.c.respond,self.request(3,True))
        self.assertEqual(self.c._counter,1);self.c.respond(self.request(3));self.assertEqual(self.c._counter,3)
    def test_absolute_lease(self):
        self.c.respond(self.request(1));self.now=44.999;self.c.respond(self.request(17))
        self.assertEqual(self.c._end,45);self.now=45;self.assertRaisesRegex(ValueError,'CONTROL_EXPIRED',self.c.respond,self.request(18))
    def test_cancel_signed_sticky(self):
        self.c.respond(self.request(1));self.s.state.update(cancelled=True,reason='cancel')
        row=json.loads(self.c.respond(self.request(3)));self.assertTrue(row['cancelled'])
        supplied=row.pop('mac');self.assertEqual(supplied,hmac.new(self.key,C.message('response','a'*48,3,row),hashlib.sha256).hexdigest())
        self.s.state.update(cancelled=False,reason=None);self.assertTrue(json.loads(self.c.respond(self.request(4)))['cancelled'])
    def test_limit(self):
        self.c.respond(self.request(1))
        for n in range(17,384,16):self.c.respond(self.request(n))
        self.c.respond(self.request(384));self.assertRaises(ValueError,self.c.respond,self.request(385))
    def test_first_admission_not_extended(self):
        self.now=25;self.assertRaisesRegex(ValueError,'CONTROL_EXPIRED',self.c.respond,self.request(1));self.assertIsNone(self.c._end)

if __name__=='__main__':unittest.main()
