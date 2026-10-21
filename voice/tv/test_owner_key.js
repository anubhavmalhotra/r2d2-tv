'use strict';
const assert=require('assert'),fs=require('fs'),crypto=require('crypto'),path=require('path'),vm=require('vm');
const K=require('./owner_key');
const hash=s=>crypto.createHash('sha256').update(s).digest('hex');
const address=['AA','BB','CC','DD','EE','FF'].join(':');
function bitmap(width,code){const words=Array(Math.floor(code/(width===16?32:64))+1).fill('0');
  words[words.length-1]= (1n<<BigInt(code%(width===16?32:64))).toString(16);return words.reverse().join(' ');}
function event(width,type,code,value){const b=Buffer.alloc(width);b.writeUInt16LE(type,width-8);b.writeUInt16LE(code,width-6);b.writeInt32LE(value,width-4);return b;}
function fixture(options={}){
  const width=options.width||16,code=300,files=new Map(),buffers=[],queue=[],opens=[];
  let clock=0,next=10,closed=0,dirClosed=0,name='event7',device='/sys/devices/fixture/hid/input/input9',closeFailure=false,fd=null;
  const node={dev:2,ino:100,rdev:13*256+64,uid:0,gid:0,mode:0o20600,nlink:1};
  const ids=['0005','0001','0002','0003'],data={uniq:address.toLowerCase()+'\n','capabilities/ev':'3\n','capabilities/key':bitmap(width,code)+'\n'};
  ids.forEach((v,i)=>data['id/'+['bustype','vendor','product','version'][i]]=v+'\n');
  const profile={schema:1,eventBytes:width,keyCode:code,identitySha256:hash(JSON.stringify([...ids,address.toLowerCase(),'3',bitmap(width,code)]))};
  const remote={address,metadata:{device:'/sys/devices/fixture/hid'}};
  const fake={constants:fs.constants,
    opendirSync(p){assert.equal(p,'/sys/class/input');let pos=0;const names=options.duplicate?[name,'event8']:['input9',name];
      return{readSync(){return pos<names.length?{name:names[pos++]}:null;},closeSync(){dirClosed++;if(options.dirCloseFailure)throw Error('CLOSE');}};},
    realpathSync(p){for(const item of [name,'event8']){const alias='/sys/class/input/'+item;
      if(p===alias+'/device')return device;if(p===alias)return options.foreignEventPath?'/sys/devices/foreign/'+item:device+'/'+item;
      if(p===alias+'/dev')return device+'/'+item+'/dev';}return p;},
    lstatSync(p){assert(p==='/dev/input/'+name||p==='/dev/input/event8');return{...node,isCharacterDevice:()=>true};},
    openSync(p,flags){assert.equal(flags,fs.constants.O_RDONLY|fs.constants.O_NONBLOCK|fs.constants.O_NOFOLLOW);opens.push(p);
      let bytes=null;if(!p.startsWith('/dev/input/')){const key=p.endsWith('/dev')?'dev':p.slice(device.length+1);assert(key==='dev'||key in data);bytes=Buffer.from(key==='dev'?'13:64\n':data[key]);}
      const n=next++;files.set(n,{path:p,bytes,pos:0});if(bytes===null)fd=n;return n;},
    fstatSync(n){const f=files.get(n);assert(f);return f.bytes===null?{...node}:{dev:2,ino:200,rdev:0,uid:0,gid:0,mode:0o100444,nlink:1,size:f.bytes.length,isFile:()=>true};},
    readSync(n,b,offset,length,position){const f=files.get(n);assert(f);buffers.push(b);clock+=options.step||0;
      if(f.bytes===null){if(options.onRead)options.onRead(queue,node);if(!queue.length)throw Object.assign(Error('NO_DATA'),{code:'EAGAIN'});
        const item=queue.shift();if(item instanceof Error)throw item;item.copy(b,offset);return item.length;}
      const count=Math.min(length,f.bytes.length-f.pos);f.bytes.copy(b,offset,f.pos,f.pos+count);f.pos+=count;return count;},
    closeSync(n){assert(files.has(n));const isEvent=files.get(n).bytes===null;files.delete(n);closed++;if(isEvent&&closeFailure)throw Error('CLOSE');}
  };
  const reader=K.create(profile,{fs:fake,crypto,arch:width===16?'arm':'arm64',uid:()=>0,euid:()=>0,now:()=>clock});
  return{reader,profile,remote,node,data,queue,files,buffers,opens,code,width,fake,
    push(value,key=code,type=1){queue.push(event(width,type,key,value));},
    open(){return reader.open(remote);},set(v){({clock=clock,name=name,device=device,closeFailure=closeFailure}=v);},stats:()=>({closed,dirClosed,fd,clock})};
}
const cases=[];const test=(name,fn)=>cases.push([name,fn]);
test('import and factory perform no filesystem IO; profile has no default key',()=>{
  let requires=0;const module={exports:{}};vm.runInNewContext(fs.readFileSync(path.join(__dirname,'owner_key.js'),'utf8'),{module,Buffer,require(){requires++;throw Error('IO');}});assert.equal(requires,0);
  const f=fixture();assert.equal(f.opens.length,0);assert.equal(f.files.size,0);
  for(const bad of [undefined,{}, {...f.profile,keyCode:0},{...f.profile,keyCode:768},{...f.profile,keyCode:true},{...f.profile,eventBytes:20},{...f.profile,extra:'private'}])assert.throws(()=>K.profile(bad));
  const p=K.profile(f.profile);f.profile.keyCode=2;assert.equal(p.keyCode,300);assert(Object.isFrozen(p));
});
test('ABI must match explicit target event width; unsupported architecture refuses',()=>{const f=fixture();for(const arch of ['arm64','unknown'])assert.throws(()=>K.create(f.profile,{fs:{},crypto,arch}),/OWNER_KEY_ABI/);assert.throws(()=>K.create(f.profile,{fs:{},crypto,arch:'arm',endianness:()=> 'BE'}),/OWNER_KEY_ABI/);});
for(const width of [16,24])test('actual '+width+' byte stream binds current remote and only calibrated key initiates',()=>{
  const f=fixture({width});const a=f.open();assert(a.ready);assert.equal(f.files.size,1);f.push(1,299);assert(!f.reader.poll().pressed);
  f.push(1);const p=f.reader.poll();assert(p.pressed&&p.down);f.push(2);assert(!f.reader.poll().pressed);f.push(0);const r=f.reader.poll();assert(r.released&&!r.down);
  assert(f.reader.close());assert.equal(f.files.size,0);assert(f.buffers.every(b=>b.every(x=>x===0)));
});
test('repeat without a known initial down never becomes a fresh press',()=>{const f=fixture();f.open();f.push(2);assert(!f.reader.poll().pressed);f.reader.close();});
test('real class alias requires canonical dev read; exact old lookup refuses',()=>{
  const f=fixture();assert.notEqual(f.fake.realpathSync('/sys/class/input/event7/dev'),'/sys/class/input/event7/dev');
  assert(f.open().ready);assert(f.opens.includes('/sys/devices/fixture/hid/input/input9/event7/dev'));assert(f.reader.close());
  const current=fs.readFileSync(path.join(__dirname,'owner_key.js'),'utf8'),needle="bounded(eventPath+'/dev',64,budget)";
  assert.equal(current.split(needle).length,2);const old=current.replace(needle,"bounded('/sys/class/input/'+name+'/dev',64,budget)");
  const module={exports:{}};vm.runInNewContext(old,{module,require,Buffer,process});const g=fixture();
  const reader=module.exports.create(g.profile,{fs:g.fake,crypto,arch:'arm',uid:()=>0,euid:()=>0,now:()=>0});
  assert.throws(()=>reader.open(g.remote),/OWNER_KEY_SYMLINK/);assert.equal(g.files.size,0);
});
test('event class alias outside exact input device cannot be adopted',()=>{const f=fixture({foreignEventPath:true});assert.throws(()=>f.open(),/OWNER_KEY_SCOPE/);assert.equal(f.files.size,0);});
test('startup queue is discarded, repeat cannot become press, release then fresh press works',()=>{
  const f=fixture();f.push(1);assert(!f.open().pressed);f.push(2);assert(!f.reader.poll().pressed);f.push(0);f.reader.poll();f.push(1);assert(f.reader.poll().pressed);f.reader.close();
});
test('busy queue is discarded and cannot renew when still held',()=>{
  const f=fixture();f.open();f.push(1);assert(!f.reader.poll(true).pressed);f.push(2);assert(!f.reader.poll().pressed);f.push(0);f.reader.poll();f.push(1);assert(f.reader.poll().pressed);f.reader.close();
});
test('already released short press does not authorize delayed START',()=>{const f=fixture();f.open();f.push(1);f.push(0);const r=f.reader.poll();assert(!r.pressed&&!r.down);assert(r.tapped);f.reader.close();});
test('discarded and batched edges are feedback only, never a press',()=>{
  const f=fixture();f.open();f.push(1);const busy=f.reader.poll(true);assert(busy.ignored&&!busy.pressed&&!busy.tapped);
  f.push(0);const up=f.reader.poll();assert(!up.ignored&&!up.tapped);f.push(1);const held=f.reader.poll();assert(held.pressed&&!held.tapped&&!held.ignored);
  f.reader.close();
});
test('input drop cancels and closes, never guesses current key state',()=>{const f=fixture();f.open();f.push(1);f.push(0,3,0);assert.throws(()=>f.reader.poll(),/OWNER_KEY_DROPPED/);assert.equal(f.files.size,0);});
test('malformed and partial records reject and wipe',()=>{for(const row of [Buffer.alloc(1),event(16,1,300,7)]){const f=fixture();f.open();f.queue.push(row);assert.throws(()=>f.reader.poll(),/OWNER_KEY_FRAME/);assert.equal(f.files.size,0);assert(f.buffers.every(b=>b.every(x=>x===0)));}});
test('read failure and EOF revoke the exact handle',()=>{for(const row of [Buffer.alloc(0),Object.assign(Error('PRIVATE_PAYLOAD'),{code:'ENODEV'})]){const f=fixture();f.open();f.queue.push(row);assert.throws(()=>f.reader.poll(),/^Error: OWNER_KEY_/);assert.equal(f.files.size,0);}});
test('bounded backlog loses authority until completely drained; stale down is never replayed',()=>{
  const f=fixture();f.open();for(let i=0;i<9;i++)f.push(i?2:1);const first=f.reader.poll();assert(!first.ready&&!first.pressed);const second=f.reader.poll();assert(second.ready&&!second.pressed);f.push(0);f.reader.poll();f.push(1);assert(f.reader.poll().pressed);f.reader.close();
});
test('different remote or wrong sysfs ancestry cannot be selected',()=>{
  for(const change of ['uniq','parent','digest']){const f=fixture();if(change==='uniq')f.data.uniq='not-the-paired-device\n';if(change==='parent')f.set({device:'/sys/devices/unrelated/input/input9'});if(change==='digest')f.data['id/product']='0004\n';
    assert.throws(()=>f.open(),/OWNER_KEY_UNIQUE/);assert.equal(f.files.size,0);}
});
test('multiple matching input devices refuse instead of choosing first',()=>{const f=fixture({duplicate:true});assert.throws(()=>f.open(),/OWNER_KEY_UNIQUE/);assert.equal(f.files.size,0);});
test('calibrated capability must advertise EV_KEY and that exact code',()=>{
  const f=fixture();f.data['capabilities/key']='0\n';f.profile.identitySha256=hash(JSON.stringify(['0005','0001','0002','0003',address.toLowerCase(),'3','0']));
  const reader=K.create(f.profile,{fs:f.fake,crypto,arch:'arm',uid:()=>0,euid:()=>0,now:()=>0});assert.throws(()=>reader.open(f.remote),/OWNER_KEY_CAPABILITY/);assert.equal(f.files.size,0);
});
test('held node identity, unlink and permissions refuse',()=>{
  for(const edit of [{ino:101},{nlink:0},{mode:0o20666}]){const f=fixture();f.open();Object.assign(f.node,edit);assert.throws(()=>f.reader.poll(),/OWNER_KEY_RACE/);assert.equal(f.files.size,0);}
  for(const edit of [{uid:1},{mode:0o20622},{nlink:2}]){const f=fixture();Object.assign(f.node,edit);assert.throws(()=>f.open(),/OWNER_KEY_NODE/);}
});
test('periodic full metadata recheck rejects change even with unchanged dev inode',()=>{const f=fixture();f.open();f.data['id/product']='0004\n';f.set({clock:101});assert.throws(()=>f.reader.poll(),/OWNER_KEY_UNIQUE/);assert.equal(f.files.size,0);});
test('clock reversal and bounded metadata reads refuse',()=>{const f=fixture();f.open();f.set({clock:-1});assert.throws(()=>f.reader.poll(),/OWNER_KEY_CLOCK/);assert.equal(f.files.size,0);const slow=fixture({step:600});assert.throws(()=>slow.open(),/OWNER_KEY_BUDGET/);assert.equal(slow.files.size,0);});
test('capture caller deadline clips metadata and clears the exact key handle',()=>{
  const options={},f=fixture(options);f.open();f.set({clock:101});options.step=10;
  assert.throws(()=>f.reader.poll(false,()=>f.stats().clock<131),/OWNER_KEY_BUDGET/);
  assert.equal(f.stats().clock,131);assert.equal(f.files.size,0);assert(f.buffers.every(b=>b.every(x=>x===0)));
});
test('remote argument is snapshotted and cannot change subsequent binding',()=>{const f=fixture();f.open();f.remote.address='changed';f.remote.metadata.device='/sys/devices/foreign';f.set({clock:101});assert(f.reader.poll().ready);f.reader.close();});
test('failed event close remains sticky and blocks reopen',()=>{const f=fixture();f.open();f.set({closeFailure:true});assert.equal(f.reader.close(),false);assert.equal(f.reader.close(),false);assert.throws(()=>f.open(),/OWNER_KEY_AUTHORITY/);});
test('metadata threshold crossed at EAGAIN does not start a trailing scan',()=>{
  const f=fixture();f.open();let drained=false,trailingScans=0;
  const read=f.fake.readSync;f.fake.readSync=function(fd,...args){try{return read(fd,...args);}catch(e){if(e.code==='EAGAIN'&&f.files.get(fd).bytes===null){drained=true;f.set({clock:101});}throw e;}};
  const scan=f.fake.opendirSync;f.fake.opendirSync=function(p){if(drained)trailingScans++;return scan(p);};
  f.push(1);assert(f.reader.poll().pressed);assert.equal(trailingScans,0);
  // Due metadata runs at the next poll before reading this release.
  drained=false;f.push(0);const released=f.reader.poll();assert(released.released&&!released.down);f.reader.close();
});
test('metadata directory close failure poisons reader; no capture endpoint opens',()=>{const f=fixture({dirCloseFailure:true});assert.throws(()=>f.open(),/OWNER_KEY_CLOSE/);assert.equal(f.files.size,0);assert.equal(f.reader.close(),false);});
test('profile and filesystem errors have no raw input payload in fixed reader errors',()=>{const f=fixture();f.open();f.queue.push(Object.assign(Error('transcript=PRIVATE fixture /private/path token'),{code:'EIO'}));let error;try{f.reader.poll();}catch(e){error=e;}assert.equal(error.message,'OWNER_KEY_READ');assert.equal(f.files.size,0);});
function run(){for(const [name,fn] of cases){try{fn();}catch(error){console.error('FAIL '+name);throw error;}}console.log(cases.length+' owner key controls passed');}
module.exports={fixture,event};
if(require.main===module)run();
