"""Mutate copies only; each repair omission must fail its regression."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
HERE=Path(__file__).resolve().parent
REPO=HERE.parents[2]
PREFIX='voice/backend/physical_capture/'
CASES=[
 ('consumer-replay','physical_consumer.py',"if self.state!='new':return self._outcome('refused','CONSUMER_REPLAY')","if False:return self._outcome('refused','CONSUMER_REPLAY')",'test_consumer.ConsumerTests.test_real_controller_and_channel_navigate_once_after_current_join'),
 ('duplicate-delivery','physical_consumer.py','delivered+=1','delivered=1','test_consumer.ConsumerTests.test_bad_or_duplicate_delivery_consumes_without_action'),
 ('foreign-thread','physical_consumer.py','threading.get_ident()!=self.thread or self.clock()>=end','False or self.clock()>=end','test_consumer.ConsumerTests.test_foreign_thread_cannot_deliver_current_result'),
 ('final-admission-deadline','physical_consumer.py',"if self.clock()>=end:\n     result=self._outcome('refused','CAPTURE_EXPIRED',capture=True);return result","if False:\n     result=self._outcome('refused','CAPTURE_EXPIRED',capture=True);return result",'test_consumer.ConsumerTests.test_dispatch_deadline_rechecked_after_final_idle_lock'),
 ('idle-only-policy','voice_control/channel_adapter.py',"elif state == {'state': 'ready'} and not self.require_idle:","elif state == {'state': 'ready'}:",'test_consumer.ConsumerTests.test_atomic_idle_gate_refuses_racing_browser_session'),
 ('post-lock-current','voice_control/channel_adapter.py','if time.monotonic() >= end or not current(is_current):','if time.monotonic() >= end:','test_real_manager.RealManagerTests.test_cancel_during_adapter_lock_with_real_manager_no_start'),
 ('pre-navigation-current','voice_control/channel_adapter.py',' or not current(is_current):\n            return \'rejected\'',' :\n            return \'rejected\'','test_consumer.ConsumerTests.test_cancel_during_navigation_client_lookup_prevents_queue'),
 ('captured-owner-identity','physical_consumer.py','self.owner.manager is m and self.owner.runtime is r and','True and self.owner.runtime is r and','test_consumer.ConsumerTests.test_owner_swapped_during_capture_refuses'),
 ('physical-stop-gate','join_schema.py',"need(qualify_physical(value['tv'],server['pcmBytes'])is True)","need(True)",'test_consumer.ConsumerTests.test_bad_or_duplicate_delivery_consumes_without_action'),
 ('expected-intent','join_schema.py','need(all(server[k]is True for k in SERVER_FLAGS))',"need(all(server[k]is True for k in SERVER_FLAGS-{'expectedIntentMatched'}))",'test_consumer.ConsumerTests.test_all_server_success_and_cleanup_flags_required'),
 ('owned-cleanup','join_schema.py',"('cleanupVerified','sourcesRemoved','pipesClosed','drainersJoined')","('cleanupVerified','pipesClosed','drainersJoined')",'test_consumer.ConsumerTests.test_all_server_success_and_cleanup_flags_required'),
 ('host-replay','current_host.py',"if self.used:raise ValueError('CURRENT_HOST_REPLAY')","if False:raise ValueError('CURRENT_HOST_REPLAY')",'test_current_host.CurrentHostTests.test_actual_run_signature_delivers_current_only_once'),
 ('host-source-identity','current_host.py',"return (type(self.host)is types.ModuleType", "return True or (type(self.host)is types.ModuleType",'test_current_host.CurrentHostTests.test_replaced_source_or_bad_result_refuses_delivery'),
]

def main():
 results=[]
 with tempfile.TemporaryDirectory(prefix='physical-consumer-mutations-')as folder:
  for name,file,old,new,test in CASES:
   root=Path(folder)/name
   package=root/PREFIX
   shutil.copytree(HERE,package,ignore=shutil.ignore_patterns('__pycache__'))
   for relative in ('voice/backend/controller.py','voice/backend/channel_adapter.py','spark/manager.py.in'):
    target=root/relative;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(REPO/relative,target)
   relative='voice/backend/channel_adapter.py' if file=='voice_control/channel_adapter.py' else PREFIX+file
   p=root/relative;raw=p.read_text()
   if raw.count(old)!=1:raise RuntimeError('MUTATION_SLOT_'+name)
   p.write_text(raw.replace(old,new))
   run=subprocess.run([sys.executable,'-B','-m','unittest','voice.backend.physical_capture.'+test],cwd=root,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=12)
   # Require a real assertion failure; syntax/import failure is not a killed defect.
   killed=run.returncode!=0 and b'FAIL:'in run.stdout and b'SyntaxError'not in run.stdout
   results.append({'name':name,'detected':killed})
   if not killed:
    print(run.stdout.decode(errors='replace'));raise RuntimeError('SURVIVING_MUTATION_'+name)
 print(json.dumps({'schema':1,'nativeExecuted':False,'mutations':results},sort_keys=True))
if __name__=='__main__':main()
