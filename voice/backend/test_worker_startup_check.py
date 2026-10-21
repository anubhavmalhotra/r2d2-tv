import unittest
from types import SimpleNamespace
import worker_startup_check as c

class Tests(unittest.TestCase):
    def test_actual_worker_api_is_warmed_and_closed_without_command(self):
        events=[]
        class Worker:
            sealed={'fixed':'metadata'};process=SimpleNamespace(poll=lambda:None)
            def __init__(self,*args,**kwargs):events.append(('constructed',kwargs['qualified']))
            def start(self,**kwargs):events.append(('start',kwargs['timeout_s']))
            def close(self):events.append(('close',));return True
            def call(self,*args,**kwargs):raise AssertionError('no input allowed')
        result=c.check('/python','/asr','/intent',factory=Worker)
        self.assertTrue(result['ready']);self.assertTrue(result['cleanupComplete'])
        self.assertEqual(events,[('constructed',True),('start',120),('close',)])
    def test_failed_start_closes_and_does_not_export_exception(self):
        events=[]
        class Worker:
            def __init__(self,*args,**kwargs):pass
            def start(self,**kwargs):raise RuntimeError('not exported')
            def close(self):events.append('close');return False
        result=c.check('/python','/asr','/intent',factory=Worker)
        self.assertFalse(result['ready']);self.assertEqual(result['failure'],'WORKER_CLEANUP_UNCONFIRMED')
        self.assertNotIn('not exported',str(result));self.assertEqual(events,['close'])

if __name__=='__main__':unittest.main()
