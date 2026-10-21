"""Host-only C formatter → real JS receipt/facade checks; no target execution."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent


class SupervisorEnvelope(unittest.TestCase):
    def test_actual_native_formatter_matches_parser_and_owner_loss_is_not_success(self):
        source = (HERE / 'provider_supervisor.c').read_text()
        lines = [line for line in source.splitlines() if line.startswith(' char report[1024];')]
        self.assertEqual(len(lines), 1)
        writes = [line for line in source.splitlines() if line.startswith(' if(n<=0||n>=(int)sizeof(report)||write(REPORT_FD,report,')]
        self.assertEqual(len(writes), 1)
        # Compile the actual native snprintf/write statement, rather than
        # maintaining a fabricated JSON schema that can drift from production.
        prefix = r'''
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
#define REPORT_FD 1
int main(int argc,char **argv) {
 int owner_lost=argc>1?atoi(argv[1]):0,owner_grace_expired=argc>2?atoi(argv[2]):0;
 const char *stage=owner_lost?"owner-retirement":"syscall";
 int error_no=0,fds_verified=1,image_verified=1,expired=0,all_reaped=1,root_exit=0,root_signal=0,okay=1;
 struct {int requested;} retirement={owner_lost};
 unsigned tasks_n=2,sockets_denied=0,escapes_denied=0,returns_replaced=0,recvmsg_replaced=0;
'''
        with tempfile.TemporaryDirectory(prefix='r2d2-supervision-envelope-') as tmp:
            root = Path(tmp)
            c = root / 'formatter.c'
            c.write_text(prefix + lines[0] + '\n' + writes[0] + '\nreturn 0;\n}\n')
            exe = root / 'formatter'
            compiled = subprocess.run([shutil.which('cc'), '-Wall', '-Wextra', '-Werror', str(c), '-o', str(exe)],
                                      capture_output=True, timeout=20)
            self.assertEqual(compiled.returncode, 0, compiled.stderr.decode())
            reports = [subprocess.run([str(exe), *args], check=True, capture_output=True, timeout=5).stdout
                       for args in [[], ['1'], ['1', '1']]]
            for i, report in enumerate(reports):
                (root / f'report-{i}.json').write_bytes(report)
            script = r'''
const fs=require('fs'),assert=require('assert'),EventEmitter=require('events'),{PassThrough}=require('stream');
const {receipt,facade}=require(process.argv[1]);
const raw=[0,1,2].map(i=>fs.readFileSync(process.argv[2]+'/report-'+i+'.json','ascii'));
const reports=raw.map(receipt);
assert.equal(reports[0].supervisionComplete,true);
assert.equal(reports[0].ownerLost,false);
for(const r of reports.slice(1)){assert.equal(r.ownerLost,true);assert.equal(r.gracefulStopRequested,true);assert.equal(r.supervisionComplete,false);}
assert.equal(reports[1].ownerGraceExpired,false);assert.equal(reports[2].ownerGraceExpired,true);
function reject(change){assert.throws(()=>receipt(JSON.stringify({...reports[0],...change})),/OWNER_SUPERVISION_UNQUALIFIED/);}
reject({ownerLost:'false'});reject({ownerLost:true});reject({gracefulStopRequested:true});reject({ownerGraceExpired:true});
reject({stage:'owner-retirement'});reject({extra:true});
const missing={...reports[0]};delete missing.ownerLost;
assert.throws(()=>receipt(JSON.stringify(missing)),/OWNER_SUPERVISION_UNQUALIFIED/);
for(let i=0;i<raw.length;i++){
 const child=new EventEmitter();child.stderr=new PassThrough();child.stdio=[];
 for(let n=0;n<7;n++)child.stdio[n]=new PassThrough();child.kill=()=>{};child.pid=123;
 const f=facade(child);let closed=false;f.on('close',(code,signal)=>{closed=true;assert.equal(code,0);assert.equal(signal,null);});
 child.stdio[6].emit('data',Buffer.from('IX'+raw[i]));child.emit('close',0,null);
 assert(closed);assert.deepEqual(f.supervision,reports[i]);
}
{
 const child=new EventEmitter();child.stderr=new PassThrough();child.stdio=[];
 for(let n=0;n<7;n++)child.stdio[n]=new PassThrough();child.kill=()=>{};child.pid=123;
 const f=facade(child);let closed=false;f.on('close',(code,signal)=>{closed=true;assert.equal(code,0);assert.equal(signal,null);});
 const teardownRace=JSON.stringify({...reports[0],errorNumber:3,rootExit:-1,rootSignal:9,supervisionComplete:false});
 child.stdio[6].emit('data',Buffer.from('IX'+teardownRace));
 child.stdio[5].emit('data',Buffer.from('KBLSTY0000190000000002ZE'));
 child.emit('close',0,null);
 assert(closed);assert.equal(f.supervision.supervisionComplete,true);assert.equal(f.supervision.rootExit,0);assert.equal(f.supervision.rootSignal,0);
}
console.log(JSON.stringify({groups:4,nativeCalls:0,modelCalls:0}));
'''
            result = subprocess.run([shutil.which('node'), '-e', script,
                                     str(HERE / 'supervised_provider.js'), str(root)],
                                    check=True, capture_output=True, timeout=10)
            self.assertIn(b'"groups":4', result.stdout)


if __name__ == '__main__':
    unittest.main()
