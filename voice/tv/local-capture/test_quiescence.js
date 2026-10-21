'use strict';
// Synthetic target filesystem. No real /proc, unit or root target is read.
// One host-only child checks that an inert standalone entry exits naturally.
const fs=require('fs'),path=require('path'),os=require('os'),crypto=require('crypto'),assert=require('assert/strict');
const vm=require('vm'),Module=require('module'),cp=require('child_process');
const {assertQuiescent,CODE}=require('./quiescence');
const ROOT='/var/lib/local.owner.r2d2mic',NODE='/usr/bin/node',UNIT='/etc/systemd/system/owner-old.service';
const BOOT='12345678-1234-1234-1234-123456789abc';
const NODE_TOMBSTONE="'use strict';\nmodule.exports={};\n";
const sha=b=>crypto.createHash('sha256').update(b).digest('hex');
function fixture(){
 const tmp=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'r2d2-quiescence-'))),map=p=>path.join(tmp,p),recordPath=ROOT+'/local-capture/retirement.json';
 function put(p,b,mode=0o600){const q=map(p);fs.mkdirSync(path.dirname(q),{recursive:true,mode:0o700});fs.writeFileSync(q,b);fs.chmodSync(q,mode);return q;}
 put('/proc/sys/kernel/random/boot_id',BOOT+'\n',0o444);put(NODE,'synthetic-exact-node',0o755);
 const entryPath=ROOT+'/start.sh',entry=Buffer.from('#!/bin/sh\nexit 0\n');put(entryPath,entry,0o700);
 const unit=Buffer.from('[Service]\nExecStart=/bin/sh '+entryPath+'\n');put(UNIT,unit,0o644);
 const image=fs.statSync(map(NODE),{bigint:true});
 const record={schema:1,mode:'retired-raw-hid-owner-current-boot',bootId:BOOT,
  oldOwner:{pid:'123',startTicks:'456'},executable:{path:NODE,dev:String(image.dev),ino:String(image.ino),sha256:sha(Buffer.from('synthetic-exact-node'))},
  rawEntryPath:ROOT+'/resident_mic.js',argv:[[NODE,ROOT+'/resident_mic.js'],[NODE,'/usr/palm/services/jsservicelauncher/bootstrap-node.js','--','/dev/null','^_~',ROOT]],
  unit:{path:UNIT,sha256:sha(unit)},entry:{path:entryPath,sha256:sha(entry),tombstone:'sh'}};
 function save(){put(recordPath,JSON.stringify(record));}
 function proc(pid,args,ticks='900'){
  fs.mkdirSync(map('/proc/'+pid),{recursive:true,mode:0o700});
  const fields=['S',...Array(18).fill('0'),ticks,'0'];put('/proc/'+pid+'/stat',pid+' (node with spaces) '+fields.join(' ')+'\n',0o444);
  put('/proc/'+pid+'/cmdline',Buffer.from(args.join('\0')+'\0'),0o444);
  const exe=map('/proc/'+pid+'/exe');try{fs.unlinkSync(exe);}catch(_){}fs.symlinkSync(path.relative(path.dirname(exe),map(NODE)),exe);
 }
 proc('789',[NODE,'/some/unrelated.js']);save();
 function rootStats(s){const x=Object.create(Object.getPrototypeOf(s));Object.assign(x,s,{uid:0n,gid:0n});return x;}
 const io={constants:fs.constants,
  lstatSync:(p,o)=>rootStats(fs.lstatSync(map(p),o)),statSync:(p,o)=>rootStats(fs.statSync(map(p),o)),
  fstatSync:(fd,o)=>rootStats(fs.fstatSync(fd,o)),realpathSync:p=>{const q=fs.realpathSync(map(p));return q===tmp?'/':q.startsWith(tmp+'/')?q.slice(tmp.length):q;},
  openSync:(p,f)=>fs.openSync(map(p),f),closeSync:fd=>fs.closeSync(fd),readSync:(...a)=>fs.readSync(...a),
  readdirSync:p=>fs.readdirSync(map(p)),readlinkSync:p=>{const q=fs.realpathSync(map(p));return q.startsWith(tmp+'/')?q.slice(tmp.length):q;}};
 const run=()=>assertQuiescent(recordPath,{fs:io,getuid:()=>0,platform:()=> 'linux'});
 return{tmp,map,recordPath,record,put,save,proc,io,run,close:()=>fs.rmSync(tmp,{recursive:true,force:true})};
}
let groups=0;
function test(fn){const f=fixture();try{fn(f);groups++;}finally{f.close();}}
function refused(fn){assert.throws(fn,e=>e.message===CODE&&e.code===CODE);}
test(f=>{assert.equal(f.run(),true);});
test(f=>{fs.unlinkSync(f.map(f.recordPath));refused(f.run);});
test(f=>{fs.chmodSync(f.map(f.recordPath),0o644);refused(f.run);});
test(f=>{fs.chmodSync(path.dirname(f.map(f.recordPath)),0o755);refused(f.run);});
test(f=>{fs.chmodSync(f.map(ROOT),0o777);refused(f.run);});
test(f=>{f.record.bootId='22345678-1234-1234-1234-123456789abc';f.save();refused(f.run);});
test(f=>{f.proc('123',[NODE,'/unrelated.js'],'456');refused(f.run);});
test(f=>{f.proc('123',[NODE,'/unrelated.js'],'457');assert.equal(f.run(),true);});
test(f=>{f.proc('456',f.record.argv[0]);refused(f.run);});
test(f=>{f.proc('456',f.record.argv[1]);refused(f.run);});
test(f=>{f.record.argv=[[NODE,'/unrelated.js']];f.save();refused(f.run);});
test(f=>{f.put(f.record.entry.path,'#!/bin/sh\n/usr/bin/node old.js\n',0o700);f.record.entry.sha256=sha(Buffer.from('#!/bin/sh\n/usr/bin/node old.js\n'));f.save();refused(f.run);});
test(f=>{const unit=Buffer.from('[Service]\nExecStart=/bin/sh /different/entry.sh\n');f.put(UNIT,unit,0o644);f.record.unit.sha256=sha(unit);f.save();refused(f.run);});
test(f=>{f.put(NODE,'different-node',0o755);refused(f.run);});
test(f=>{const read=f.io.readlinkSync;let n=0;f.io.readlinkSync=p=>{const value=read(p);if(++n===2)f.proc('789',[NODE,'/some/unrelated.js'],'901');return value;};refused(f.run);});
test(f=>{const original=f.io.statSync;f.io.statSync=(p,o)=>{if(p.endsWith('/exe'))throw Object.assign(new Error('PRIVATE_ERROR'),{code:'EACCES'});return original(p,o);};refused(f.run);});
test(f=>{const read=f.io.readdirSync;f.io.readdirSync=p=>p==='/proc'?Array.from({length:4097},(_,i)=>String(i+1)):read(p);refused(f.run);});
test(f=>{fs.linkSync(f.map(f.recordPath),f.map(f.recordPath+'.hardlink'));refused(f.run);});
test(f=>{const p=f.map(f.recordPath),q=p+'.copy';fs.renameSync(p,q);fs.symlinkSync(path.basename(q),p);refused(f.run);});
function cold(f){
 delete f.record.oldOwner;delete f.record.argv;delete f.record.unit;
 f.record.schema=2;f.record.mode='raw-entry-tombstoned-current-boot';
 f.record.mountNamespace='mnt:[123456]';f.record.rawEntryPath=ROOT+'/main.js';
 const entry=Buffer.from(NODE_TOMBSTONE);
 f.record.entry={path:f.record.rawEntryPath,sha256:sha(entry),tombstone:'node'};
 f.put(f.record.entry.path,entry,0o600);fs.unlinkSync(f.map(UNIT));
 const link=f.io.readlinkSync;
 f.io.readlinkSync=p=>['/proc/self/ns/mnt','/proc/1/ns/mnt'].includes(p)?'mnt:[123456]':link(p);
 f.save();return f;
}
test(f=>{cold(f);assert.equal(f.run(),true);}); // No old PID or unit is fabricated.
test(f=>{
 cold(f);assert.equal(f.run(),true);
 const source=fs.readFileSync(f.map(f.record.entry.path),'utf8');
 const denied=()=>{throw new Error('TOMBSTONE_HOST_SIDE_EFFECT');};
 const guest={running:true,turns:0},module={exports:{}};
 const context=vm.createContext({process:{exit:denied},require:denied,
  setTimeout:denied,setInterval:denied,setImmediate:denied,guest});
 const load=vm.runInContext(Module.wrap(source),context,{timeout:1000});
 assert.doesNotThrow(()=>load(module.exports,context.require,module,'/shared/owner/main.js','/shared/owner'),
  'importing the retired guest must not exit or disturb its shared worker');
 assert.equal(typeof module.exports,'object');assert.notEqual(module.exports,null);
 assert.deepEqual(Object.keys(module.exports),[]);
 vm.runInContext('guest.turns++;',context,{timeout:1000});
 assert.deepEqual(guest,{running:true,turns:1});
});
test(f=>{
 cold(f);assert.equal(f.run(),true);
 const child=cp.spawnSync(process.execPath,[f.map(f.record.entry.path)],
  {encoding:'utf8',timeout:2000,stdio:['ignore','pipe','pipe']});
 assert.equal(child.error,undefined);assert.equal(child.signal,null);
 assert.equal(child.status,0);assert.equal(child.stdout,'');assert.equal(child.stderr,'');
});
test(f=>{cold(f);const entry=Buffer.from("'use strict';\nprocess.exit(0);\n");f.put(f.record.entry.path,entry);f.record.entry.sha256=sha(entry);f.save();refused(f.run);});
test(f=>{cold(f);f.record.oldOwner={pid:'123',startTicks:'456'};f.save();refused(f.run);});
test(f=>{cold(f);f.record.entry.path=ROOT+'/sidecar.js';f.put(f.record.entry.path,NODE_TOMBSTONE);f.save();refused(f.run);});
test(f=>{cold(f);const bytes=Buffer.from("'use strict';\nrequire('./resident_mic');\n");f.put(f.record.entry.path,bytes);f.record.entry.sha256=sha(bytes);f.save();refused(f.run);});
test(f=>{cold(f);f.proc('456',[NODE,f.record.rawEntryPath,ROOT+'/config.json']);refused(f.run);});
test(f=>{cold(f);f.proc('456',[NODE,'/usr/palm/services/jsservicelauncher/bootstrap-node.js','--','/dev/null','^_~',ROOT]);refused(f.run);});
test(f=>{cold(f);f.proc('456',[NODE,'--require',ROOT+'/recording.js','/unrelated.js']);refused(f.run);});
test(f=>{cold(f);f.put(ROOT+'/local-capture/main.js','synthetic new owner');f.put(ROOT+'/local-capture/config.json','{}');f.proc('456',[NODE,ROOT+'/local-capture/main.js',ROOT+'/local-capture/config.json']);assert.equal(f.run(),true);});
function bootstrapped(f,change=()=>{}){
 cold(f);const d=ROOT+'/local-capture';
 f.put(d+'/bootstrap_entry.js','module.exports={};');f.put(d+'/package.json','{"main":"bootstrap_entry.js"}');f.put(d+'/config.json','{}');
 const args=[NODE,'/usr/palm/services/jsservicelauncher/bootstrap-node.js','--','/dev/null',d,d,d+'/config.json','--original-fd','9'];
 change(args);f.proc('456',args);return{d,args};
}
test(f=>{bootstrapped(f);assert.equal(f.run(),true);});
test(f=>{const{d}=bootstrapped(f);fs.chmodSync(f.map(d),0o777);refused(f.run);});
test(f=>{const{d}=bootstrapped(f);fs.unlinkSync(f.map(d+'/bootstrap_entry.js'));refused(f.run);});
test(f=>{bootstrapped(f,args=>{args[4]='^_~';});refused(f.run);});
test(f=>{bootstrapped(f,args=>{args[8]='2';});refused(f.run);});
test(f=>{cold(f);f.proc('456',[NODE,'-e',"require('"+ROOT+"/main.js')"]);refused(f.run);});
test(f=>{cold(f);f.proc('456',[NODE,'-e',"require('"+ROOT+"/local-capture/main.js')"]);refused(f.run);});
test(f=>{cold(f);fs.symlinkSync('../main.js',f.map(ROOT+'/local-capture/aliased.js'));f.proc('456',[NODE,ROOT+'/local-capture/aliased.js']);refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/writable.js';f.put(script,'synthetic new owner',0o666);f.proc('456',[NODE,script]);refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/writable/main.js';f.put(script,'synthetic new owner');fs.chmodSync(path.dirname(f.map(script)),0o777);f.proc('456',[NODE,script]);refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/missing.js';f.proc('456',[NODE,script]);refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/hardlink.js';f.put(script,'synthetic new owner');fs.linkSync(f.map(script),f.map(script+'.other'));f.proc('456',[NODE,script]);refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/changing.js';f.put(script,'synthetic new owner');f.proc('456',[NODE,script]);const read=f.io.readdirSync;let n=0;f.io.readdirSync=p=>{if(p==='/proc'&&++n===2)f.put(script,'changed new owner');return read(p);};refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/changing-parent/main.js';f.put(script,'synthetic new owner');f.proc('456',[NODE,script]);const read=f.io.readdirSync;let n=0;f.io.readdirSync=p=>{if(p==='/proc'&&++n===2)fs.chmodSync(path.dirname(f.map(script)),0o755);return read(p);};refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/final-file.js';f.put(script,'synthetic new owner');f.proc('456',[NODE,script]);const stat=f.io.statSync;let n=0;f.io.statSync=(p,o)=>{if(p==='/proc/456/exe'&&++n===4)f.put(script,'late changed new owner');return stat(p,o);};refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/final-parent/main.js';f.put(script,'synthetic new owner');f.proc('456',[NODE,script]);const stat=f.io.statSync;let n=0;f.io.statSync=(p,o)=>{if(p==='/proc/456/exe'&&++n===4)fs.chmodSync(path.dirname(f.map(script)),0o755);return stat(p,o);};refused(f.run);});
test(f=>{cold(f);const script=ROOT+'/local-capture/nonroot.js';f.put(script,'synthetic new owner');f.proc('456',[NODE,script]);const stat=f.io.lstatSync;f.io.lstatSync=(p,o)=>{const s=stat(p,o);if(p!==script)return s;return Object.assign(Object.create(Object.getPrototypeOf(s)),s,{uid:1n});};refused(f.run);});
test(f=>{cold(f);const link=f.io.readlinkSync;f.io.readlinkSync=p=>p==='/proc/1/ns/mnt'?'mnt:[999]':link(p);refused(f.run);});
test(f=>{cold(f);const link=f.io.readlinkSync;let n=0;f.io.readlinkSync=p=>p==='/proc/self/ns/mnt'&&++n===2?'mnt:[999]':link(p);refused(f.run);});
test(f=>{cold(f);const read=f.io.readdirSync;let n=0;f.io.readdirSync=p=>{if(p==='/proc'&&++n===2)f.proc('456',[NODE,f.record.rawEntryPath]);return read(p);};refused(f.run);});
test(f=>{cold(f);const link=f.io.readlinkSync;let changed=false;f.io.readlinkSync=p=>{if(p.endsWith('/exe')&&!changed){changed=true;f.put(f.record.entry.path,'different bytes');}return link(p);};refused(f.run);});
console.log(JSON.stringify({groups,deviceCalls:false,recordCreatedForTarget:false}));
