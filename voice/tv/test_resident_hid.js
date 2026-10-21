'use strict';
// Synthetic sysfs/devtmpfs and public, non-authorizing records only.
const assert=require('assert'),fs=require('fs'),crypto=require('crypto'),vm=require('vm'),path=require('path');
const H=require('./resident_hid');
const hash=x=>crypto.createHash('sha256').update(x).digest('hex');
const descriptor=Buffer.alloc(202,23),uevent='HID_ID=0005:00000001:00000002\nHID_UNIQ='+['AA','BB','CC','DD','EE','FF'].join(':')+'\n';
const profile=()=>({schema:1,descriptorBytes:202,descriptorSha256:hash(descriptor),
  identitySha256:hash(JSON.stringify(['0005:00000001:00000002',''+['AA','BB','CC','DD','EE','FF'].join(':')+'',hash(descriptor),'']))});
function fixture(options={}){
  let clock=0,step=0,instance=1,name='hidraw7',next=10,scans=0,closed=0,dirClosed=0;
  const files=new Map(),buffers=[];let uid=0,duplicate=false,wrongNumber=false,content=uevent,descriptorBytes=descriptor;
  const node={dev:2,ino:100,rdev:0xf001,uid:0,gid:0,mode:0o20600,nlink:1};
  const device=()=>'/sys/devices/virtual/fixture/'+instance;
  const io={constants:fs.constants,
    opendirSync(p){assert.equal(p,'/sys/class/hidraw');scans++;if(options.onScan)options.onScan(scans,node);let i=0;
      const list=duplicate?[name,'hidraw8']:[name];return{readSync(){return i<list.length?{name:list[i++]}:null;},closeSync(){dirClosed++;}};},
    realpathSync(p){if(p.endsWith('/device'))return device();return p;},
    lstatSync(p){assert(/^\/dev\/hidraw[0-9]+$/.test(p));return{...node,isCharacterDevice:()=>true};},
    openSync(p,flags){assert(flags&fs.constants.O_NOFOLLOW);assert(flags&fs.constants.O_NONBLOCK);
      let bytes;if(p.endsWith('/report_descriptor'))bytes=descriptorBytes;else if(p.endsWith('/uevent'))bytes=Buffer.from(content);else if(p.endsWith('/dev'))bytes=Buffer.from(wrongNumber?'240:2\n':'240:1\n');else{assert.equal(p,'/dev/'+name);bytes=Buffer.from([1,2]);}
      const fd=next++;files.set(fd,{p,bytes,pos:0});return fd;},
    fstatSync(fd){const f=files.get(fd);assert(f);if(f.p.startsWith('/dev/'))return{...node};return{dev:2,ino:fd,rdev:0,uid:options.fileUid||0,gid:0,mode:0o100444,nlink:1,size:f.bytes.length,isFile:()=>true};},
    readSync(fd,b,offset,length,pos){clock+=step;const f=files.get(fd);assert(f);buffers.push(b);const start=pos===null?f.pos:pos,n=Math.min(length,f.bytes.length-start);f.bytes.copy(b,offset,start,start+n);if(pos===null)f.pos+=n;return n;},
    closeSync(fd){assert(files.has(fd));files.delete(fd);closed++;if(options.closeFails)throw Error('CLOSE');}
  };
  const helper=H.create(profile(),{fs:io,crypto,now:()=>clock,uid:()=>uid,euid:()=>uid});
  return{helper,io,node,buffers,files,set(v){({clock=clock,step=step,instance=instance,name=name,uid=uid,duplicate=duplicate,wrongNumber=wrongNumber,content=content,descriptorBytes=descriptorBytes}=v);},stats:()=>({scans,closed,dirClosed,clock})};
}
const cases=[];const test=(name,fn)=>cases.push([name,fn]);
test('profile requires only hashes/size and snapshots caller values',()=>{const p=profile(),h=H.profile(p);p.identitySha256='0'.repeat(64);assert.notEqual(p.identitySha256,h.identitySha256);assert(Object.isFrozen(h));for(const bad of [null,{}, {...profile(),extra:true},{...profile(),descriptorBytes:0},{...profile(),identitySha256:'x'}])assert.throws(()=>H.profile(bad));});
test('derive binds two full snapshots and closes/wipes metadata reads',()=>{const f=fixture(),r=f.helper.derive();assert.equal(r.address,''+['AA','BB','CC','DD','EE','FF'].join(':')+'');assert.equal(r.metadata.path,'/dev/hidraw7');assert.equal(f.stats().scans,2);assert.equal(f.stats().dirClosed,2);assert.equal(f.files.size,0);assert(f.buffers.every(b=>b.every(x=>x===0)));assert(f.helper.recheck(r));});
test('no root means no discovery or HID IO',()=>{const f=fixture();f.set({uid:500});assert.throws(()=>f.helper.derive(),/ROOT/);assert.throws(()=>f.helper.makeIO(),/ROOT/);assert.equal(f.stats().scans,0);});
test('same identity after remote sleep creates a fresh node; old instance recheck refuses',()=>{const f=fixture(),old=f.helper.derive();f.set({instance:2,name:'hidraw8',clock:3600000});f.node.ino=200;const fresh=f.helper.derive();assert.equal(fresh.metadata.path,'/dev/hidraw8');assert.equal(fresh.address,old.address);assert.throws(()=>f.helper.recheck(old),/REMOTE_IDENTITY/);assert(f.helper.recheck(fresh));});
test('wrong address or descriptor cannot be adopted on reconnect',()=>{for(const change of [{content:uevent.replace(''+['AA','BB','CC','DD','EE','FF'].join(':')+'',''+['00','00','00','00','00','01'].join(':')+'')},{descriptorBytes:Buffer.alloc(202,24)}]){const f=fixture();f.set(change);assert.throws(()=>f.helper.derive(),/UNIQUE_DEVICE/);}});
test('ambiguous duplicate matching device refuses',()=>{const f=fixture();f.set({duplicate:true});assert.throws(()=>f.helper.derive(),/UNIQUE_DEVICE/);});
test('device numbers and node ownership remain mandatory',()=>{const f=fixture();f.set({wrongNumber:true});assert.throws(()=>f.helper.derive(),/DEVICE_NUMBER/);for(const edit of [{uid:1},{nlink:0},{mode:0o20666}]){const f=fixture();Object.assign(f.node,edit);assert.throws(()=>f.helper.derive(),/DEVICE_AUTHORITY/);}});
test('second snapshot rejects any held-node ownership change',()=>{for(const field of ['dev','ino','rdev','uid','gid','mode','nlink']){const f=fixture({onScan(n,node){if(n===2)node[field]++;}});assert.throws(()=>f.helper.derive());}});
test('a bounded discovery can fail then a later fresh discovery succeeds',()=>{const f=fixture();f.set({step:4000});assert.throws(()=>f.helper.derive(),/METADATA_BUDGET/);assert.equal(f.files.size,0);f.set({clock:900000,step:0});assert(f.helper.derive());});
test('capture caller deadline clips an otherwise valid eight-second recheck',()=>{
  const f=fixture(),saved=f.helper.derive();f.set({step:10});
  assert.throws(()=>f.helper.recheck(saved,()=>f.stats().clock<50),/METADATA_BUDGET/);
  assert.equal(f.stats().clock,50);assert.equal(f.files.size,0);assert(f.buffers.every(b=>b.every(x=>x===0)));
  f.set({step:0});assert(f.helper.recheck(saved));
});
test('long-lived raw IO has no eight-second metadata expiration',()=>{const f=fixture(),io=f.helper.makeIO();f.set({clock:900000});const fd=io.open('/dev/hidraw7'),buffer=Buffer.alloc(1024);assert.equal(io.read(fd,buffer),2);assert.equal(io.stat(fd).ino,100);io.close(fd);assert.equal(f.files.size,0);});
test('metadata owner and close failures refuse without buffer retention',()=>{for(const options of [{fileUid:2},{closeFails:true}]){const f=fixture(options);assert.throws(()=>f.helper.derive());assert(f.buffers.every(b=>b.every(x=>x===0)));assert.equal(f.files.size,0);}});
test('uevent duplicate keys, malformed addresses and oversized input refuse',()=>{for(const content of [uevent+'HID_UNIQ='+['AA','BB','CC','DD','EE','FF'].join(':')+'\n',uevent.replace(''+['AA','BB','CC','DD','EE','FF'].join(':')+'','remote'),uevent+'A='+ 'x'.repeat(8192)]){const f=fixture();f.set({content});assert.throws(()=>f.helper.derive());assert.equal(f.files.size,0);}});
test('FD and FE start/release parser preserves exact report shapes',()=>{for(const kind of ['FD','FE'])for(const [code,event] of [[0x800a,'start'],[0x800d,'stop'],[0x808b,'held']]){const b=Buffer.alloc(kind==='FE'?124:20);b[0]=kind==='FE'?254:253;b.writeUInt16BE(code,kind==='FE'?2:17);if(kind==='FE'){b[4]=b[64]=1;b[6]=b[66]=0xad;}assert.deepEqual(H.event(b,b.length),{kind,event});}});
test('malformed FE, FD, padding and lengths refuse',()=>{const good=Buffer.alloc(125);good[0]=254;good[4]=good[64]=1;good[6]=good[66]=0xad;assert.equal(H.event(good,125).kind,'FE');for(const offset of [4,6,64,66,124]){const b=Buffer.from(good);b[offset]^=1;assert.throws(()=>H.event(b,125));}const fd=Buffer.alloc(20);fd[0]=253;assert.throws(()=>H.event(fd,19));for(const n of [0,-1,1025,20.5])assert.throws(()=>H.event(fd,n));});
test('bootstrap wrapper calls runtime once and keeps error text private',async()=>{const source=fs.readFileSync(path.join(__dirname,'resident_main.js'),'utf8');for(const fail of [false,true]){let runs=0;const process={exitCode:0};vm.runInNewContext(source,{process,require:p=>{assert.equal(p,'./resident_mic');return{run(){runs++;return fail?Promise.reject(Error('SENSITIVE_ERROR')):Promise.resolve();}};}});await Promise.resolve();assert.equal(runs,1);assert.equal(process.exitCode,fail?1:0);}});
(async()=>{for(const [name,fn] of cases){try{await fn();}catch(e){console.error('FAIL '+name);throw e;}}console.log(cases.length+' resident HID/bootstrap controls passed');})().catch(e=>{console.error(e);process.exitCode=1;});
