import copy
import types
import unittest
from .cleanup_join import cleaned_current
from .current_host import CurrentHostCapture
from .test_consumer import good


class Tests(unittest.TestCase):
    def test_cleanup_cannot_be_inferred_from_thread_exit_or_stop_ack(self):
        for key in ('streamStopObserved','rawWiped','pcmWiped','transportClosed','temporaryCleanup',
                    'childReaped','credentialsWiped','fourGuardsRetained','homeRestored',
                    'generatedCredentialSourcesRemoved'):
            row=good();row['tv'][key]=False
            with self.assertRaises(ValueError,msg=key):cleaned_current(row)
        for key in ('cleanupVerified','sourcesRemoved','pipesClosed','drainersJoined'):
            row=good();row['owner'][key]=False
            with self.assertRaises(ValueError,msg=key):cleaned_current(row)
        for key in ('workerCleanupComplete','socketClosed','listenerClosed','credentialsRemoved'):
            row=good();row['server'][key]=False
            with self.assertRaises(ValueError,msg=key):cleaned_current(row)

    def test_original_presence_child_signal_bad_pipe_or_short_quiet_refuses(self):
        for target,key,value in [('tv','quietMs',1999),('tv','pcmBytes',0),('owner','remainingOwned',1),
                                 ('tvAdapter','forcedTermination',True)]:
            row=good();row[target][key]=value
            with self.assertRaises(ValueError):cleaned_current(row)
        for key,value in [('childSignal','SIGKILL'),('deviceClosed',False),('identityChangeMask',1)]:
            row=good();row['tv']['nativeDiagnostic'][key]=value
            with self.assertRaises(ValueError):cleaned_current(row)
        row=good();row['tv']['nativeDiagnostic']['manifestRemoveTransport']['stdoutClosed']=False
        with self.assertRaises(ValueError):cleaned_current(row)

    def test_schema_rejects_private_fields_and_boolean_as_count(self):
        for target,key,value in [('server','transcript','PRIVATE'),('owner','childExit',True),
                                 ('tv','pcmBytes',True)]:
            row=good();row[target][key]=value
            with self.assertRaises(ValueError):cleaned_current(row)

    def test_cleanup_observer_is_once_current_and_not_a_command_admission(self):
        host,tv=types.ModuleType('host'),types.ModuleType('tv')
        tv.run=tv.result=tv.qualify=lambda:None
        row=good();row['server']['expectedIntentMatched']=False
        host.run=lambda *a,**kw:copy.deepcopy(row)
        capture=CurrentHostCapture(host,tv,{'source':'0'*64},qualified=True)
        clean=[];delivered=[]
        capture.bind_cleanup(lambda current:clean.append(cleaned_current(current)))
        with self.assertRaises(ValueError):capture.bind_cleanup(lambda _:None)
        with self.assertRaises(ValueError):capture(lambda r:delivered.append(r),timeout_s=250)
        self.assertEqual(clean,[True]);self.assertEqual(delivered,[])
        self.assertIsNone(capture.cleanup_observer)
        with self.assertRaises(ValueError):capture(lambda _:None,timeout_s=250)
        self.assertEqual(clean,[True])


if __name__=='__main__':unittest.main()
