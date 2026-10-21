"""Inert local reversion controls. Never invokes an activation entry."""
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
 ('no-auto-reap','remote_lifecycle.py','        self.reap()\n        with self.lock:\n            if self.started',
  '        with self.lock:\n            if self.started','test_lifecycle.Tests.test_two_completed_attempts_without_manual_reap'),
 ('stale-event-release','remote_lifecycle.py','not self.sequence < sequence <= 2**53 - 1',
  'not 0 < sequence <= 2**53 - 1','test_lifecycle.Tests.test_repeats_and_out_of_order_events_do_not_release_or_queue'),
 ('release-not-required','remote_lifecycle.py'," or not signal['released']",'',
  'test_lifecycle.Tests.test_completion_without_release_never_dispatches'),
 ('cancel-after-admission-only','remote_lifecycle.py','                consumer.cancel()\n            job',
  '                pass\n            job','test_lifecycle.Tests.test_cancel_during_real_adapter_lock_wait_prevents_manager_start'),
 ('request-not-sent','remote_lifecycle.py',"            job['signals']._request(reason)",'            pass',
  'test_lifecycle.Tests.test_cancel_before_result_invalidates_consumer_then_requests_stop'),
 ('thread-return-as-cleanup','remote_lifecycle.py',"clean = factory_ok and (not job['entered'] or job['joined'])",'clean = factory_ok',
  'test_lifecycle.Tests.test_bad_stop_proof_or_failure_remains_quarantined_after_thread_exit'),
 ('no-capture-deadline','remote_lifecycle.py',"now - self.job['began'] >= 250","now - self.job['began'] >= 9999",
  'test_lifecycle.Tests.test_capture_deadline_and_stuck_worker_cannot_be_rearmed'),
 ('resume-live-attempt','remote_lifecycle.py','self.started is None or self.job is not None or self.closing',
  'self.started is None or self.closing','test_lifecycle.Tests.test_suspend_refuses_new_capture_until_old_worker_reaped'),
 ('stop-ack-as-cessation','cleanup_join.py',"'startForwarded', 'stopForwarded', 'streamStopObserved', 'pcmWiped'",
  "'startForwarded', 'stopForwarded', 'pcmWiped'",'test_cleanup_join.Tests.test_cleanup_cannot_be_inferred_from_thread_exit_or_stop_ack'),
 ('cleanup-as-command','current_host.py','   qualified_current(row)\n','   pass\n',
  'test_cleanup_join.Tests.test_cleanup_observer_is_once_current_and_not_a_command_admission'),
]

def main():
    rows=[]
    for name,file,before,after,test in CASES:
        with tempfile.TemporaryDirectory(prefix='voice-lifecycle-mutation-') as folder:
            root=Path(folder)/'source';target=root/PREFIX
            shutil.copytree(HERE,target,ignore=shutil.ignore_patterns('__pycache__'))
            for relative in ('voice/backend/controller.py','voice/backend/channel_adapter.py','spark/manager.py.in'):
                copied=root/relative;copied.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(REPO/relative,copied)
            path=target/file;source=path.read_text()
            if source.count(before)!=1:raise ValueError('MUTATION_TARGET')
            path.write_text(source.replace(before,after,1))
            run=subprocess.run([sys.executable,'-B','-m','unittest','voice.backend.physical_capture.'+test],cwd=root,
                               stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=10)
            killed=run.returncode!=0 and b'FAIL:' in run.stdout and b'SyntaxError' not in run.stdout
            rows.append(dict(name=name,killed=killed))
            if not killed:raise ValueError('MUTATION_SURVIVED:'+name)
    result=dict(schema=1,nativeActions=0,controls=rows)
    print(json.dumps(result,sort_keys=True))

if __name__=='__main__':main()
