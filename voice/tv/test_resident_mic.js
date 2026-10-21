'use strict';
// Actual resident module with inert LS2/HID/HTTP/TLS/process boundaries. No TV,
// network, model, filesystem mutation or speech fixture is used by this suite.
const assert=require('assert'), fs=require('fs'), vm=require('vm'), path=require('path');
const {EventEmitter}=require('events'), crypto=require('crypto');
const filename=path.join(__dirname,'resident_mic.js'), source=fs.readFileSync(filename,'utf8');
let imports=0;
const context={module:{exports:{}},require:()=>{imports++;throw Error('IMPORT_IO');},Buffer,
  __dirname:'/fixture/daemon',process:{hrtime:{bigint:()=>0n}},setTimeout,clearTimeout};
vm.runInNewContext(source,context,{filename});
const R=context.module.exports;
const config=()=>({sparkIp:[192,168,50,2].join('.'),voiceKey:'a'.repeat(64),routeDir:'/owned/route',
  hidProfile:{schema:1,descriptorBytes:202,descriptorSha256:'d'.repeat(64),identitySha256:'e'.repeat(64)},
  ownerKeyProfile:{schema:1,eventBytes:16,keyCode:300,identitySha256:'f'.repeat(64)}});
const targets=['/usr/sbin/voiceconductor','/usr/lib/qml/WebOSCompositor/controllers/StarfishKeyFilter.qml',
  '/usr/lib/qml/KeyFilters/appLaunch.js','/usr/sbin/voiceinput_hidraw'];
const names=['conductor-stub','StarfishKeyFilter.qml','appLaunch.js','conductor-stub'];
const error=code=>Object.assign(Error(code),{code});
const hex16=/^[a-f0-9]{16}$/,printable=/^[\x20-\x7e]{1,60}$/;
// The Spark-side phase key and canonical body, spelled out independently of the resident:
// resident_device_key(voiceKey, tvCertSha256) and Python json.dumps(sort_keys, compact) without mac.
const keyFor=tvSha=>crypto.createHash('sha256').update(Buffer.from('R2D2-RESIDENT-PTT-v1\0','ascii'))
  .update(Buffer.from('a'.repeat(64),'hex')).update(Buffer.from(tvSha,'hex')).digest();
const canonical=v=>'{"boot":'+JSON.stringify(v.boot)+',"code":'+JSON.stringify(v.code)+',"cut":'+JSON.stringify(v.cut)+
  ',"deadlineMs":'+JSON.stringify(v.deadlineMs)+',"phase":'+JSON.stringify(v.phase)+',"press":'+JSON.stringify(v.press)+
  ',"schema":'+JSON.stringify(v.schema)+',"seq":'+JSON.stringify(v.seq)+'}';
// The complete closed toast set. Each is fixed printable ASCII of at most 60 characters.
const TOASTS=['Listening...','Processing voice command...','Voice command sent','Voice command unavailable',
  'Hold the microphone button while speaking','Voice model warming up - try again in a moment',
  'Voice heard (no browser navigation matched)','Remote mic did not start. Try again',
  'Voice is off. Restart the TV to use voice','Voice control is paused. Restart Spark to resume',
  'Too many requests, wait a moment','Voice is busy, try again in a moment','Sent, but not confirmed. Check the TV'];
function report(kind,event='none') {
  const b=Buffer.alloc(kind==='FE'?124:20);b[0]=kind==='FE'?0xfe:0xfd;
  const off=kind==='FE'?2:17;b.writeUInt16BE({start:0x800a,stop:0x800d,held:0x808b,none:0}[event],off);
  if(kind==='FE'){b[4]=b[64]=1;b[6]=b[66]=0xad;}
  return b;
}
function fixture(options={}) {
  // The TV certificate pin and press/boot ids are fixed only for the golden wire vectors.
  const tvSha=options.tvCertSha256||'c'.repeat(64),phaseKey=keyFor(tvSha),ids=(options.ids||[]).slice();
  let time=0,next=1;const timers=new Map(),trace=[],reads=[],pcm=[],configs=[],queue=[],reportTimes=[],toasts=[];
  let power='active',powerError=false,route=true,failClose=false,failStart=false,failStop=false;
  let startDelay=1,httpDelay=1,decodeBad=false,identityBad=false,tlsFail=false,tlsCloseFail=false;
  let held=false,opened=0,closed=0,registered=0,unregistered=0,rechecks=0,decodeCalls=0,releases=0,tlsCloses=0;
  let pendingOutcome=null,hangResult=false,lastRaw=null,reqClosed=0,reqOpened=0,surface='other',identityLoads=0;
  // Spark routes: pings and receipt requests seen, toasts Spark served, and mock faults the resident would swallow.
  const pings=[],receipts=[],served=new Set(),httpErrors=[],hmacKeys=[],wire=[];
  const keyQueue=[];let keyDown=false,keyHeld=false,keyCloses=0,pendingAudio=[];
  const node={dev:1,ino:2,rdev:3,uid:0,gid:0,mode:0o20600,nlink:1};
  const later=(fn,ms)=>{const id=next++;timers.set(id,{at:time+ms,fn});return id;};
  const clear=id=>timers.delete(id);
  const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
  async function advance(ms) {
    const end=time+ms;await flush();let count=0;
    while(true){const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at||a[0]-b[0])[0];
      if(!due)break;if(++count>100000)throw Error('FIXTURE_SPIN');
      time=Math.max(time,due[1].at);timers.delete(due[0]);due[1].fn();await flush();}
    time=Math.max(time,end);await flush();
  }
  async function until(predicate,limit=20000){for(let n=0;n<limit;n+=5){if(predicate())return;await advance(5);}throw Error('FIXTURE_TIMEOUT');}
  const fakeFs={constants:fs.constants,realpathSync:p=>p,
    lstatSync(p){
      if(p==='/owned/route')return{uid:0,mode:0o40755,isDirectory:()=>true};
      const i=targets.indexOf(p),name=i>=0?names[i]:p.split('/').pop();
      assert(names.includes(name));return{dev:7,ino:100+names.indexOf(name)+(i>=0&&!route?10:0),uid:0,mode:0o100444,isFile:()=>true};
    },
    readFileSync(p,encoding){assert.equal(p,'/proc/self/mountinfo');assert.equal(encoding,'utf8');return targets.map(t=>'1 0 0:1 / '+t+' ro - tmpfs tmpfs ro').join('\n');},
    openSync(p,flags){assert.equal(p,'/fixture/daemon/decode-reports');assert(flags&fs.constants.O_NOFOLLOW);return 12;},
    closeSync(fd){assert.equal(fd,12);trace.push('decoder.close');if(options.decoderCloseFail)throw Error('CLOSE');}
  };
  const io={open(p){assert.equal(p,'/dev/hidraw7');held=true;opened++;return 7;},
    stat(fd){assert.equal(fd,7);return{...node};},
    read(fd,b){assert.equal(fd,7);assert(held);reads.push(b);if(options.onRead)options.onRead({elapse:ms=>{time+=ms;},trace});if(!queue.length)throw error('EAGAIN');
      const item=queue.shift();if(item instanceof Error)throw item;item.copy(b);return item.length;},
    close(fd){assert.equal(fd,7);closed++;held=false;if(failClose)throw Error('CLOSE');}
  };
  class Handle extends EventEmitter {
    constructor(owner){super();assert.equal(owner,'local.owner.r2d2mic');if(options.registrationFails)throw Error('BUS');registered++;}
    call(uri,payload){assert(uri.endsWith('/setReport'));const p=JSON.parse(payload),start=p.reportData[1]===3;
      assert.deepEqual(p,{address:''+['AA','BB','CC','DD','EE','FF'].join(':')+'',reportType:'feature',reportData:[249,start?3:4]});
      trace.push(start?'START':'STOP');reportTimes.push({start,at:time});const req=new EventEmitter();let timer;
      req.cancel=()=>{clear(timer);trace.push('bus.cancel');};
      timer=later(()=>{if(start&&!failStart){queue.push(...pendingAudio);pendingAudio=[];}req.emit('response',{payload:()=>JSON.stringify({returnValue:!(start?failStart:failStop)})});},start?startDelay:1);
      return req;
    }
    unregister(){unregistered++;}
  }
  const cp={execFile(file,args,opts,cb){assert.equal(file,'/usr/bin/luna-send');assert.equal(opts.killSignal,'SIGKILL');assert(opts.timeout<=2500);assert.equal(opts.maxBuffer,8192);
      const uri=args[3],payload=JSON.parse(args[4]),child={};
      if(uri.endsWith('/getPowerState'))later(()=>cb(powerError?Error('POWER'):null,JSON.stringify({returnValue:true,state:power})),options.powerDelay||1);
      else if(uri.endsWith('/setReport')){assert.equal(payload.reportData[1],4);trace.push('STOP_FALLBACK');later(()=>cb(null,JSON.stringify({returnValue:!failStop})),1);}
      else {assert(uri.endsWith('/createToast'));
        // Recorded rather than asserted here: luna() deliberately swallows throws.
        toasts.push(payload.message);trace.push('toast');later(()=>cb(null,'{"returnValue":true}'),options.toastDelay||1);}
      return child;
    },
    spawnSync(file,args,opts){assert.equal(file,'/proc/self/fd/12');assert.equal(opts.timeout,3000);assert.equal(opts.killSignal,'SIGKILL');
      assert.equal(opts.stdio[12],12);decodeCalls++;lastRaw=opts.input;trace.push('decode');
      const output=Buffer.alloc(decodeBad?3:(opts.input.length/124)*480,7);pcm.push(output);
      return{status:0,signal:null,stdout:output};}
  };
  // The resident swallows request-side throws, so mock faults are recorded and asserted at shutdown.
  const http={request(opts,cb){try{
      assert.equal(opts.host,config().sparkIp);assert.equal(opts.port,4181);assert.equal(opts.headers.Host,config().sparkIp+':4181');
      const base='/'+config().voiceKey,receipt=opts.path.startsWith(base+'/voice-receipt/')&&opts.path.slice(base.length+15);
      const route=opts.path===base+'/state'?'state':opts.path===base+'/voice-phase'?'phase':receipt&&hex16.test(receipt)?'receipt':assert.fail('path '+opts.path);
      if(route==='state'){assert.equal(opts.method,'GET');assert.deepEqual(Object.keys(opts.headers).sort(),['Host','X-R2D2-TV-Power']);
        assert(['active','standby','unknown'].includes(opts.headers['X-R2D2-TV-Power']));}
      else if(route==='phase'){assert.equal(opts.method,'POST');assert.deepEqual(Object.keys(opts.headers).sort(),['Content-Length','Content-Type','Host']);
        assert.equal(opts.headers['Content-Type'],'application/json');}
      else {assert.equal(opts.method,'GET');assert.deepEqual(Object.keys(opts.headers),['Host']);}
      const req=new EventEmitter();reqOpened++;let timer,ended=false;
      req.destroy=()=>{if(ended)return;ended=true;clear(timer);reqClosed++;req.emit('close');};
      req.end=body=>{try{
        let reply;
        if(route==='state'){assert.equal(body,undefined);reply={status:200,body:options.largeHTTP?'x'.repeat(4097):'{"session":"active"}',delay:httpDelay};}
        else if(route==='phase'){
          assert(Buffer.isBuffer(body)&&body.length<=256&&body.length===opts.headers['Content-Length']);
          const text=body.toString('latin1');assert(/^[\x20-\x7e]+$/.test(text));const ping=JSON.parse(text);
          assert.equal(JSON.stringify(ping),text);assert.deepEqual(Object.keys(ping),['boot','code','cut','deadlineMs','mac','phase','press','schema','seq']);
          assert.equal(ping.mac,crypto.createHmac('sha256',phaseKey).update(Buffer.from('R2D2-voice-phase-v1\0','ascii')).update(canonical(ping)).digest('hex'));
          pings.push({...ping,at:time});wire.push(text);trace.push('ping:'+ping.phase);
          reply=options.phaseReply?options.phaseReply(ping):{json:{ok:true,surface}};
        } else {
          assert.equal(body,undefined);receipts.push({press:receipt,at:time});trace.push('receipt');
          reply=options.sparkReceipt?options.sparkReceipt(receipt):{status:404,body:''};
          if(reply.json&&typeof reply.json.toast==='string')served.add(reply.json.toast);
        }
        if(reply.throw)throw Error('SOCKET');
        if(reply.hang)return;
        timer=later(()=>{if(ended)return;if(reply.error)return void req.emit('error',Error('NET'));
          const res=new EventEmitter();res.statusCode=reply.status||200;res.destroy=()=>{};cb(res);
          res.emit('data',Buffer.from(reply.json!==undefined?JSON.stringify(reply.json):reply.body));res.emit('end');},reply.delay===undefined?1:reply.delay);
      }catch(e){if(e.message!=='SOCKET')httpErrors.push(e);throw e;}};
      return req;
    }catch(e){httpErrors.push(e);throw e;}}};
  const tlsClient={async connect(cfg,hooks){configs.push(cfg);trace.push('connect');
      // A Spark refusal before release ends the stream; the real client keeps it in the close receipt.
      const refuse=options.refuse,refusal=refuse&&{closed:true,controlFlushed:false,actionAcknowledged:refuse.ack===true,
        outcome:{state:refuse.state||'refused',code:refuse.code}};
      if(refuse&&refuse.when==='connect'){if(hooks&&hooks.onClose)hooks.onClose(refusal);throw error('TRANSPORT_CLOSED');}
      if(tlsFail)throw Error('TLS');
      let resolve;const result=new Promise(r=>resolve=r);pendingOutcome=()=>resolve(options.receipt||{closed:true,actionAcknowledged:options.actionAcknowledged!==false,outcome:{code:options.outcome||'AGENT_CALL_ADMITTED'}});
      const session={async sendPcm(b){trace.push('pcm');assert(b.some(x=>x!==0));if(options.onPcm)options.onPcm();
          if(refuse&&refuse.when==='upload'){resolve(refusal);throw error('PCM_NOT_ADMITTED');}},
        async release(){if(refuse&&refuse.when==='release'){resolve(refusal);throw error('SESSION_FINISHING');}
          releases++;trace.push('release');if(!hangResult)pendingOutcome();},closed:result,
        async close(){tlsCloses++;trace.push('tls.close');resolve({closed:true,cancelled:true});if(tlsCloseFail)throw Error('TLS_CLOSE');}};
      return session;}};
  const cryptoDep={createHash:name=>crypto.createHash(name),randomBytes:n=>ids.length?Buffer.from(ids.shift(),'hex'):crypto.randomBytes(n),
    createHmac(name,key){hmacKeys.push(key);return crypto.createHmac(name,key);}};
  const resident=R.createResident(config(),{fs:fakeFs,http,crypto:cryptoDep,cp,palmbus:{Handle},root:'/fixture/daemon',
    now:()=>time,setTimeout:later,clearTimeout:clear,
    ownerKey:options.ownerKey||{open(){if(options.onOwnerKeyOpen)options.onOwnerKeyOpen(reads.length);keyHeld=true;keyQueue.length=0;keyDown=false;return{ready:true};},
      poll(discard=false,budget=()=>true){assert(keyHeld);if(options.keyDetached)throw Error('OWNER_KEY_RACE');
        if(options.onPoll)options.onPoll(budget,{elapse:ms=>{time+=ms;},trace});let pressed=false,released=false,ignored=false;
        while(keyQueue.length){const value=keyQueue.shift();if(value===0){released=keyDown;keyDown=false;}else{if(value===1&&!keyDown&&!discard){pressed=true;trace.push('KEY_PRESS');}if(value===1&&!keyDown&&discard)ignored=true;keyDown=true;}}
        return{pressed:pressed&&keyDown,released,down:keyDown,ready:true,ignored,tapped:pressed&&released&&!keyDown};},
      close(){const wasHeld=keyHeld;if(keyHeld)keyCloses++;keyHeld=false;return !(wasHeld&&options.keyCloseFail);}},
    shape:{READ_BYTES:1024,makeIO:()=>io},remoteAddress:{derive(){if(options.deriveFails)throw Error('ASLEEP');return{address:''+['AA','BB','CC','DD','EE','FF'].join(':')+'',metadata:{path:'/dev/hidraw7',device:'/sys/devices/fixture/hid',node:{...node}}};},recheck(selected,budget){rechecks++;if(options.onRecheck)options.onRecheck(rechecks,keyQueue,{elapse:ms=>{time+=ms;},budget});if(options.recheckFails)throw Error('IDENTITY');}},
    recording:{event(b,n){const kind=b[0]===0xfe?'FE':'FD',off=kind==='FE'?2:17;
      if(kind==='FE')assert.equal(n,124);const code=b.readUInt16BE(off);return{kind,event:({32778:'start',32781:'stop',32907:'held'})[code]||'none'};}},
    tlsClient,deviceIdentity:{load(){identityLoads++;if(options.identityCloseFailure||options.identityCloseOnce&&identityLoads===1)throw error('DEVICE_IDENTITY_CLOSE');if(identityBad)throw Error('IDENTITY');return{sparkCertificateSha256:'b'.repeat(64),certificateSha256:tvSha,sparkCertificate:'PUBLIC',certificate:'PUBLIC',privateKey:'NON_AUTHORIZING_FIXTURE'};}}
  });
  return {resident,trace,reads,pcm,configs,queue,keyQueue,node,reportTimes,toasts,pings,receipts,hmacKeys,wire,advance,until,complete:()=>pendingOutcome(),
    begin(){return resident.start();},async ready(){resident.start();await until(()=>resident.status().state==='ready');},
    press(release='FD'){if(options.realKey){options.realKey.push(0);options.realKey.push(1);}else keyQueue.push(0,1);pendingAudio=[report('FE','held'),report('FE',release==='FE'?'stop':'held')];if(release==='FD')pendingAudio.push(report('FD','stop'));
      if(release==='short')pendingAudio=[report('FE','stop')];},
    async shutdown(){const job=resident.stop();await until(()=>resident.status().state.startsWith('closed')||resident.status().state==='cleanup-pending');await job;
      // Only the closed set, or text Spark served that passes the resident's own validation.
      for(const message of toasts)assert(TOASTS.includes(message)||served.has(message)&&printable.test(message),'unlisted toast '+message);
      assert.deepEqual(httpErrors,[]);},
    stats:()=>({opened,closed,registered,unregistered,rechecks,decodeCalls,releases,tlsCloses,reqOpened,reqClosed,time,lastRaw,keyCloses,identityLoads}),
    set(values){({power=power,powerError=powerError,route=route,failClose=failClose,failStart=failStart,failStop=failStop,surface=surface,
      startDelay=startDelay,httpDelay=httpDelay,decodeBad=decodeBad,identityBad=identityBad,tlsFail=tlsFail,tlsCloseFail=tlsCloseFail,hangResult=hangResult}=values);}
  };
}
const cases=[];const test=(name,fn)=>cases.push([name,fn]);
test('import performs no require or I/O; CLI remains explicit',()=>{assert.equal(imports,0);assert.equal(typeof R.run,'function');});
test('only configured private literal IPv4 and bounded ports admitted',()=>{
  assert.equal(R.configuration(config()).httpPort,4181);
  for(const ip of ['localhost','8.8.8.8','127.0.0.1',[192,168,'050',2].join('.'),[10,0,0,999].join('.'),'::1'])assert.throws(()=>R.configuration({...config(),sparkIp:ip}));
  for(const port of [true,0,65536,4182])assert.throws(()=>R.configuration({...config(),httpPort:port}));
});
test('route and capability validation; no browser capability needed',()=>{
  for(const routeDir of ['/owned/../etc','/owned//route','relative','/owned/route/'])assert.throws(()=>R.configuration({...config(),routeDir}));
  assert.throws(()=>R.configuration({...config(),voiceKey:'bad'}));assert(!('browserKey' in R.configuration({...config(),browserKey:'unused'})));
});
test('HID profile is required and snapshotted rather than a public identity constant',()=>{
  const cfg=config();const admitted=R.configuration(cfg);cfg.hidProfile.identitySha256='f'.repeat(64);
  assert.equal(admitted.hidProfile.identitySha256,'e'.repeat(64));
  for(const hidProfile of [null,{}, {...config().hidProfile,descriptorBytes:0},{...config().hidProfile,identitySha256:'bad'}])assert.throws(()=>R.configuration({...config(),hidProfile}));
});
test('production factory imports only the packaged helper closure without doing device I/O',()=>{
  const requested=[],module={exports:{}},cfg=config();let profileSeen=null;
  const require=name=>{requested.push(name);if(name==='/fixture/resident_hid')return{create(p){profileSeen=p;return{};}};
    if(name==='/fixture/owner_key')return{create(){return{};}};
    if(['/fixture/tls_pcm_client','/fixture/device_identity'].includes(name))return{};assert.fail('unpackaged import '+name);};
  vm.runInNewContext(source,{module,require,Buffer,__dirname:'/fixture',process:{hrtime:{bigint:()=>0n}},setTimeout,clearTimeout},{filename});
  const resident=module.exports.createResident(cfg,{fs:{},http:{},crypto:{},cp:{},palmbus:{}});
  assert.equal(resident.status().state,'idle');assert.equal(profileSeen.identitySha256,cfg.hidProfile.identitySha256);
  assert.deepEqual(requested,['/fixture/resident_hid','/fixture/tls_pcm_client','/fixture/device_identity','/fixture/owner_key']);
});
test('failed/unknown power never means active',()=>{for(const x of [null,{}, {returnValue:false,state:'active'},{returnValue:true,state:'unexpected'}])assert.equal(R.powerState(x),'unknown');
  assert.equal(R.powerState({returnValue:true,state:'Active'}),'active');assert.equal(R.powerState({returnValue:true,state:'standby'}),'standby');});
test('held node ownership metadata and unlink are checked',()=>{const n={dev:1,ino:2,rdev:3,uid:0,gid:0,mode:0o20600,nlink:1};assert(R.sameNode(n,{...n}));for(const key of Object.keys(n))assert(!R.sameNode(n,{...n,[key]:n[key]+1}));assert(!R.sameNode({...n,nlink:0},{...n,nlink:0}));});
test('initial power failure keeps device unopened and reports unknown',async()=>{const f=fixture();f.set({powerError:true});f.begin();await f.advance(1000);assert.equal(f.stats().opened,0);assert.equal(f.resident.status().power,'unknown');await f.shutdown();assert(f.resident.status().cleanupComplete);});
test('route mismatch refuses without mutation or device open',async()=>{const f=fixture();f.set({route:false});f.begin();await f.advance(1000);assert.equal(f.stats().opened,0);await f.shutdown();});
test('standby closes and wake derives a new owned handle',async()=>{const f=fixture();await f.ready();f.set({power:'standby'});await f.advance(5200);assert.equal(f.stats().closed,1);assert.equal(f.resident.status().power,'standby');f.set({power:'active'});await f.advance(5200);assert.equal(f.stats().opened,2);await f.shutdown();assert.equal(f.stats().closed,2);});
for(const release of ['FD','FE'])test(release+' release stops before PCM and wipes raw/PCM/key buffers',async()=>{
  const f=fixture();await f.ready();f.press(release);await f.until(()=>f.resident.status().processed===1);assert(f.trace.indexOf('STOP')<f.trace.indexOf('decode'));assert(f.trace.indexOf('decode')<f.trace.indexOf('connect'));
  assert.equal(f.stats().releases,1);assert(f.stats().lastRaw.every(x=>x===0));assert(f.pcm.every(b=>b.every(x=>x===0)));assert(f.reads.every(b=>b.every(x=>x===0)));
  for(const cfg of f.configs)for(const k of ['ca','certificate','privateKey','deviceKey'])assert(cfg[k].every(x=>x===0));
  await f.shutdown();assert(f.resident.status().cleanupComplete);
});
test('global admitted outcome never invokes app launch',async()=>{const f=fixture();await f.ready();f.press();await f.until(()=>f.resident.status().processed===1);await f.shutdown();});
test('unknown result is fixed toast only',async()=>{const f=fixture({outcome:'PRIVATE_ARBITRARY_TEXT',actionAcknowledged:false});await f.ready();f.press();await f.until(()=>f.resident.status().processed===1);await f.shutdown();});
test('accepted Controller code is generic feedback without a TV action',async()=>{const f=fixture({outcome:'BROWSER_NAVIGATION_ACCEPTED'});await f.ready();f.press();await f.until(()=>f.resident.status().processed===1);await f.shutdown();});
test('unconfirmed START is not retried and never sends PCM',async()=>{const f=fixture();await f.ready();f.set({failStart:true});f.press();await f.advance(100);assert.equal(f.trace.filter(x=>x==='START').length,1);assert(f.trace.includes('STOP'));assert.equal(f.stats().decodeCalls,0);
  assert.deepEqual(f.toasts,['Remote mic did not start. Try again']);await f.shutdown();});
test('unconfirmed STOP prevents decoding and further capture; cleanup remains unknown',async()=>{const f=fixture();await f.ready();f.set({failStop:true});f.press();await f.advance(100);f.press();await f.advance(500);assert.equal(f.trace.filter(x=>x==='START').length,1);assert.equal(f.stats().decodeCalls,0);
  assert.deepEqual(f.toasts,['Listening...','Voice is off. Restart the TV to use voice']);await f.shutdown();assert.equal(f.resident.status().stopConfirmed,false);assert.equal(f.resident.status().cleanupComplete,false);});
test('signal during pending START joins reply then STOP before unregister',async()=>{const f=fixture();await f.ready();f.set({startDelay:100});f.press();await f.until(()=>f.trace.includes('START'));const job=f.resident.stop();await f.advance(200);await job;assert(f.trace.includes('STOP'));assert.equal(f.stats().decodeCalls,0);assert.equal(f.stats().unregistered,1);assert(f.resident.status().cleanupComplete);});
test('detach during capture stops exact address and discards audio',async()=>{const f=fixture();await f.ready();f.press('none');await f.until(()=>f.trace.includes('START'));f.node.nlink=0;await f.advance(20);assert(f.trace.includes('STOP'));assert.equal(f.stats().decodeCalls,0);
  assert.deepEqual(f.toasts,['Listening...','Voice command unavailable']);f.set({route:false});await f.shutdown();});
test('standby during capture stops and discards audio',async()=>{const f=fixture();await f.ready();await f.advance(4600);f.set({power:'standby'});f.press('none');await f.advance(600);assert(f.trace.includes('STOP'));assert.equal(f.stats().decodeCalls,0);
  assert.deepEqual(f.toasts,['Listening...']);await f.shutdown();});
test('duplicate press never renews the 5.5 second limit',async()=>{const f=fixture();await f.ready();f.press('none');f.queue.push(report('FD','start'));await f.advance(5600);assert.equal(f.trace.filter(x=>x==='START').length,1);assert.equal(f.stats().releases,1);await f.advance(100);assert.equal(f.trace.filter(x=>x==='START').length,1);await f.shutdown();});
test('two released utterances use sequential capture and independent TLS sessions',async()=>{const f=fixture();await f.ready();for(let i=1;i<=2;i++){f.press();await f.until(()=>f.resident.status().processed===i);}assert.equal(f.stats().releases,2);assert.equal(f.stats().tlsCloses,2);await f.shutdown();});
test('invalid decoder output is wiped and never transmitted',async()=>{const f=fixture();await f.ready();f.set({decodeBad:true});f.press();await f.advance(100);assert.equal(f.configs.length,0);assert(f.pcm[0].every(x=>x===0));assert(f.stats().lastRaw.every(x=>x===0));await f.shutdown();});
test('decoder close failure wipes returned PCM before refusing',async()=>{const f=fixture({decoderCloseFail:true});await f.ready();f.press();await f.advance(100);assert.equal(f.configs.length,0);assert(f.pcm[0].every(x=>x===0));
  assert.deepEqual(f.toasts,['Listening...','Processing voice command...','Voice is off. Restart the TV to use voice']);await f.shutdown();assert.equal(f.resident.status().cleanupComplete,false);});
test('identity and TLS failures wipe all available mutable audio/key buffers',async()=>{for(const failure of ['identityBad','tlsFail']){const f=fixture();await f.ready();f.set({[failure]:true});f.press();await f.advance(100);assert.equal(f.stats().releases,0);assert(f.pcm[0].every(x=>x===0));for(const cfg of f.configs)assert(cfg.privateKey.every(x=>x===0));
  assert.deepEqual(f.toasts,['Listening...','Processing voice command...','Voice command unavailable']);await f.shutdown();}});
test('identity descriptor close uncertainty is sticky in resident cleanup',async()=>{const f=fixture({identityCloseFailure:true});await f.ready();f.press();await f.advance(100);assert.equal(f.configs.length,0);assert(f.pcm[0].every(x=>x===0));await f.shutdown();assert.equal(f.resident.status().cleanupComplete,false);});
test('TLS close failure stays incomplete but key and audio buffers still cleared',async()=>{const f=fixture();await f.ready();f.set({tlsCloseFail:true});f.press();await f.advance(100);assert(f.configs[0].privateKey.every(x=>x===0));assert(f.pcm[0].every(x=>x===0));await f.shutdown();assert.equal(f.resident.status().cleanupComplete,false);});
test('shutdown while awaiting inference closes the exact session and drains owner loops',async()=>{const f=fixture();await f.ready();f.set({hangResult:true});f.press();await f.until(()=>f.stats().releases===1);await f.shutdown();assert.equal(f.stats().tlsCloses,1);assert(f.resident.status().cleanupComplete);assert.equal(f.stats().reqOpened,f.stats().reqClosed);});
test('shutdown during PCM send never releases action authority',async()=>{let f;f=fixture({onPcm:()=>void f.resident.stop()});await f.ready();f.press();await f.advance(100);assert.equal(f.stats().releases,0);assert.equal(f.stats().tlsCloses,1);assert(f.pcm[0].every(x=>x===0));assert(f.resident.status().cleanupComplete);});
test('busy presses are discarded; only a fresh later press starts another utterance',async()=>{const f=fixture();await f.ready();f.set({hangResult:true});f.press();await f.until(()=>f.stats().releases===1);f.press();f.complete();await f.advance(20);assert.equal(f.trace.filter(x=>x==='START').length,1);f.set({hangResult:false});f.press();await f.until(()=>f.resident.status().processed===2);assert.equal(f.trace.filter(x=>x==='START').length,2);await f.shutdown();});
test('a press while a command is still running says busy once and starts nothing',async()=>{
  const f=fixture();await f.ready();f.set({hangResult:true});f.press();await f.until(()=>f.stats().releases===1);
  f.press();await f.advance(200);f.press();await f.advance(200);
  assert.equal(f.toasts.filter(t=>t==='Voice is busy, try again in a moment').length,1);
  assert.equal(f.trace.filter(x=>x==='START').length,1);
  f.complete();await f.advance(20);assert.equal(f.toasts[f.toasts.length-1],'Voice command sent');
  f.set({hangResult:false});f.press();await f.until(()=>f.resident.status().processed===2);await f.shutdown();
});
test('an unreadable power reply after release keeps the result',async()=>{
  const f=fixture();await f.ready();f.set({hangResult:true});f.press();await f.until(()=>f.stats().releases===1);
  f.set({powerError:true});await f.advance(6000);assert.equal(f.resident.status().power,'unknown');assert.equal(f.stats().tlsCloses,0);
  f.complete();await f.advance(20);assert.equal(f.toasts[f.toasts.length-1],'Voice command sent');
  f.set({powerError:false});await f.shutdown();
});
test('standby after release still closes the session without a result toast',async()=>{
  const f=fixture();await f.ready();f.set({hangResult:true});f.press();await f.until(()=>f.stats().releases===1);
  f.set({power:'standby'});await f.advance(6000);assert.equal(f.stats().tlsCloses,1);
  assert(!f.toasts.includes('Voice command sent'));assert(!f.toasts.includes('Voice command unavailable'));await f.shutdown();
});
test('a tap too short to start capture gets the hold hint',async()=>{
  const f=fixture();await f.ready();f.keyQueue.push(1,0);await f.advance(50);
  assert.equal(f.resident.status().captures,0);assert.deepEqual(f.toasts,['Hold the microphone button while speaking']);await f.shutdown();
});
test('shutdown joins owned toast process completion',async()=>{const f=fixture({toastDelay:400});await f.ready();f.press();await f.until(()=>f.resident.status().processed===1);const job=f.resident.stop();await f.advance(20);assert.equal(f.stats().unregistered,0);await f.advance(400);await job;assert(f.resident.status().cleanupComplete);});
test('device-close failure is sticky and never reported as clean',async()=>{const f=fixture();await f.ready();f.set({failClose:true});await f.shutdown();assert.equal(f.resident.status().cleanupComplete,false);});
test('duplicate service registration refuses without killing any process',async()=>{const f=fixture({registrationFails:true});const result=await f.begin();assert.equal(result.cleanupComplete,false);assert.equal(f.stats().opened,0);});
test('bounded HTTP body and cancelled request close without diagnostic leakage',async()=>{for(const largeHTTP of [false,true]){const f=fixture({largeHTTP});f.set({httpDelay:1000});f.begin();await f.advance(5);await f.shutdown();assert.equal(f.stats().reqOpened,f.stats().reqClosed);assert.equal(f.stats().releases,0);}});
test('start is one-shot and stop before start cannot open HID',async()=>{const f=fixture();await f.resident.stop();await f.begin();assert.equal(f.stats().opened,0);assert.throws(()=>f.begin(),/ALREADY_STARTED/);});
test('missing calibration is inactive with no FD fallback',()=>{
  const cfg=config();delete cfg.ownerKeyProfile;assert.throws(()=>R.configuration(cfg),/OWNER_KEY_PROFILE/);
  for(const keyCode of [0,-1,0x300,true])assert.throws(()=>R.configuration({...config(),ownerKeyProfile:{...config().ownerKeyProfile,keyCode}}));
});
test('FD start alone cannot issue START or capture',async()=>{const f=fixture();await f.ready();f.queue.push(report('FD','start'));await f.advance(100);assert.equal(f.resident.status().captures,0);assert(!f.trace.includes('START'));await f.shutdown();});
test('physical key starts before any FD marker and physical release stops capture',async()=>{
  const f=fixture();await f.ready();f.press('none');await f.until(()=>f.trace.includes('START'));
  assert(f.trace.indexOf('KEY_PRESS')<f.trace.indexOf('START'));await f.advance(10);f.keyQueue.push(0);
  await f.until(()=>f.resident.status().processed===1);assert(f.stats().time<1000);assert.equal(f.trace.filter(x=>x==='START').length,1);await f.shutdown();assert.equal(f.stats().keyCloses,1);
});
test('held repeat never starts an utterance',async()=>{const f=fixture();await f.ready();f.keyQueue.push(2);await f.advance(100);assert(!f.trace.includes('START'));await f.shutdown();});
test('key endpoint loss during capture cancels and still stops',async()=>{
  const options={},f=fixture(options);await f.ready();f.press('none');await f.until(()=>f.trace.includes('START'));options.keyDetached=true;
  await f.advance(20);assert(f.trace.includes('STOP'));assert.equal(f.stats().decodeCalls,0);await f.shutdown();
});
test('key close uncertainty keeps cleanup unqualified',async()=>{const f=fixture({keyCloseFail:true});await f.ready();await f.shutdown();assert.equal(f.resident.status().cleanupComplete,false);});
test('release during final identity check refuses START',async()=>{
  const f=fixture({onRecheck(n,q){if(n===2)q.push(0);}});await f.ready();f.press('none');await f.advance(100);
  assert(!f.trace.includes('START'));assert.equal(f.stats().decodeCalls,0);
  assert.deepEqual(f.toasts,['Hold the microphone button while speaking']);await f.shutdown();
});
test('expired pre-START metadata never sends START or PCM',async()=>{
  const f=fixture({onRecheck(n,q,clock){if(n===2)clock.elapse(5500);}});
  await f.ready();f.press('none');await f.advance(10);
  assert(!f.trace.includes('START'));assert.equal(f.stats().decodeCalls,0);await f.shutdown();
});
// Hook throws are swallowed by capture(), so budget-propagation failures are
// recorded in flags and asserted afterwards; the loops are bounded.
const stopped=f=>f.until(()=>f.reportTimes.some(r=>!r.start));
test('in-capture metadata receives the original deadline; the hold limit then submits after a fresh identity check',async()=>{
  let captureCheck=false,budgetLost=false,decodesAtFinalCheck=null,f;
  f=fixture({onRecheck(n,q,clock){if(n===3){captureCheck=true;
    assert.equal(typeof clock.budget,'function');
    for(let i=0;i<6000;i++){if(!clock.budget())throw error('METADATA_BUDGET');clock.elapse(1);}
    budgetLost=true;
  }if(n===4)decodesAtFinalCheck=f.stats().decodeCalls;}});
  await f.ready();f.press('none');await stopped(f);
  assert(captureCheck);assert(!budgetLost,'capture deadline not passed into recheck');
  const start=f.reportTimes.find(r=>r.start),stop=f.reportTimes.find(r=>!r.start);
  assert(start&&stop);assert.equal(stop.at-start.at,5500);
  await f.advance(200);
  assert.equal(decodesAtFinalCheck,0);assert.equal(f.stats().decodeCalls,1);assert.equal(f.stats().releases,1);
  assert.deepEqual(f.toasts,['Listening...','Processing voice command...','Voice command sent']);
  await f.shutdown();assert(f.resident.status().cleanupComplete);
});
test('hold limit reached inside the capture read loop submits the captured audio',async()=>{
  let reads=0;const f=fixture({onRead({elapse,trace}){if(trace.includes('START')&&++reads===2)elapse(5500);}});
  await f.ready();f.press('none');await stopped(f);await f.advance(200);
  assert.equal(f.trace.filter(x=>x==='START').length,1);assert.equal(f.stats().decodeCalls,1);assert.equal(f.stats().releases,1);
  await f.shutdown();
});
test('hold limit reached inside the owner-key poll submits the captured audio',async()=>{
  let polls=0;const f=fixture({onPoll(budget,{elapse,trace}){if(trace.includes('START')&&++polls===2){elapse(5500);if(!budget())throw error('OWNER_KEY_BUDGET');}}});
  await f.ready();f.press('none');await stopped(f);await f.advance(200);
  assert.equal(f.stats().decodeCalls,1);assert.equal(f.stats().releases,1);await f.shutdown();
});
test('budget faults before the hold limit are fence failures and discard audio',async()=>{
  const hooks=[{onRecheck(n){if(n===3)throw error('METADATA_BUDGET');}},
    {onPoll(budget,{trace}){if(trace.includes('START'))throw error('OWNER_KEY_BUDGET');}}];
  for(const hook of hooks){
    const f=fixture(hook);await f.ready();f.press('none');await stopped(f);await f.advance(200);
    assert(f.reportTimes.find(r=>!r.start).at<5500);
    assert.equal(f.stats().decodeCalls,0);assert.equal(f.stats().releases,0);assert.equal(f.toasts[f.toasts.length-1],'Voice command unavailable');
    await f.shutdown();
  }
});
test('a fence fault at the hold limit still discards audio',async()=>{
  for(const failing of [3,4]){
    let budgetLost=false;
    const f=fixture({onRecheck(n,q,clock){if(n===3){for(let i=0;i<6000&&clock.budget();i++)clock.elapse(1);
      if(clock.budget())budgetLost=true;
      if(failing===3)throw error('REMOTE_IDENTITY');throw error('METADATA_BUDGET');}if(n===failing)throw error('REMOTE_IDENTITY');}});
    await f.ready();f.press('none');await f.advance(5800);
    assert(!budgetLost,'capture deadline not passed into recheck');
    assert.equal(f.stats().decodeCalls,0);assert.equal(f.stats().releases,0);assert.equal(f.toasts[f.toasts.length-1],'Voice command unavailable');
    await f.shutdown();
  }
});
test('a hold limit before START still never sends START',async()=>{
  const f=fixture({onPoll(budget,{elapse,trace}){if(!trace.includes('START')&&trace.includes('KEY_PRESS'))elapse(5500);}});
  await f.ready();f.press('none');await f.advance(100);
  assert(!f.trace.includes('START'));assert.equal(f.stats().decodeCalls,0);await f.shutdown();
});
for(const [when,code,message] of [['upload','VOICE_WARMING_UP','Voice model warming up - try again in a moment'],
  ['release','CONTROLLER_BUSY','Voice is busy, try again in a moment'],['connect','AGENT_PAUSED','Voice control is paused. Restart Spark to resume']])
  test('a Spark refusal before release ('+when+') is shown instead of failing silently',async()=>{
    const f=fixture({refuse:{when,code}});await f.ready();f.press();await f.advance(300);
    assert.equal(f.resident.status().processed,1);assert.equal(f.stats().releases,0);assert.equal(f.toasts[f.toasts.length-1],message);
    if(when==='connect')assert(!f.trace.includes('pcm'));
    assert(f.pcm[0].every(x=>x===0));for(const cfg of f.configs)assert(cfg.privateKey.every(x=>x===0));
    await f.shutdown();assert(f.resident.status().cleanupComplete);
  });
test('only a refused early receipt is recovered; anything else stays a failure',async()=>{
  const f=fixture({refuse:{when:'upload',code:'BROWSER_NAVIGATION_ACCEPTED',state:'accepted',ack:true}});
  await f.ready();f.press();await f.advance(200);
  assert.equal(f.resident.status().processed,0);assert(!f.toasts.includes('Voice command sent'));
  assert.equal(f.toasts[f.toasts.length-1],'Voice command unavailable');await f.shutdown();
});
test('receipt codes map to fixed toasts and missing confirmation reads as uncertain',async()=>{
  for(const [receipt,message] of [
    [{closed:true,controlFlushed:true,cancelled:false,actionAcknowledged:false,outcome:null},'Sent, but not confirmed. Check the TV'],
    [{closed:true,controlFlushed:true,cancelled:false,actionAcknowledged:false,outcome:{state:'unknown',code:'EXECUTION_UNCONFIRMED'}},'Sent, but not confirmed. Check the TV'],
    [{closed:true,controlFlushed:true,actionAcknowledged:false,outcome:{state:'refused',code:'AUDIO_EMPTY_OR_SHORT'}},'Hold the microphone button while speaking'],
    [{closed:true,controlFlushed:true,actionAcknowledged:false,outcome:{state:'refused',code:'RATE_LIMITED'}},'Too many requests, wait a moment'],
    [{closed:true,controlFlushed:true,actionAcknowledged:false,outcome:{state:'refused',code:'UNSUPPORTED_COMMAND'}},'Voice command unavailable']]){
    const f=fixture({receipt});await f.ready();f.press();await f.until(()=>f.resident.status().processed===1);
    assert.equal(f.toasts[f.toasts.length-1],message);await f.shutdown();
  }
});
test('every toast literal in the resident is fixed printable ASCII of at most 60 characters',()=>{
  for(const message of TOASTS)assert(/^[\x20-\x7e]{1,60}$/.test(message),message);
  assert(/^[\x00-\x7f]*$/.test(source),'resident source must stay ASCII');
  // Every message-like literal (words separated by a space) must be a listed toast.
  const literals=[...source.matchAll(/'([^'\\\n]*)'/g)].map(m=>m[1])
    .filter(s=>/[A-Za-z] +[A-Za-z(]/.test(s)&&!['use strict','power off'].includes(s));
  for(const literal of literals)assert(TOASTS.includes(literal),'unlisted toast literal '+literal);
  for(const message of TOASTS)assert(source.includes("toast('"+message+"')")||source.includes("'"+message+"':")||source.includes("?'"+message+"'")||
    source.includes(":'"+message+"'")||source.includes(",'"+message+"')"),'unused listed toast '+message);
  // Surface-gated toasts take a listed literal or a closed fault code's fixed text.
  for(const line of source.split('\n').filter(l=>/\bnotice\(press,/.test(l)&&!/function notice/.test(l)))
    assert(TOASTS.some(m=>line.endsWith(",'"+m+"');"))||line.includes('FAULTS[code])'),'unlisted notice '+line);
  // Spark-provided text reaches toast() only through the one validated receipt path.
  assert.deepEqual([...source.matchAll(/\btoast\(([^')][^)]*)\)/g)].map(m=>m[1]).sort(),['message','message','text']);
  assert.equal(source.split("typeof raw.toast==='string'&&/^[\\x20-\\x7e]{1,60}$/.test(raw.toast)").length,2);
});
const done=(f,n=1)=>f.until(()=>f.resident.status().processed===n);
test('phase pings are MAC-bound enums: one boot, a fresh press id, strictly increasing seq',async()=>{
  const f=fixture();await f.ready();f.press();await done(f);f.press();await done(f,2);await f.advance(10);
  assert.deepEqual(f.pings.map(p=>p.phase),['pressed','listening','released','pressed','listening','released']);
  assert(f.pings.every((p,i)=>p.seq===i+1&&p.schema===1&&p.boot===f.pings[0].boot&&hex16.test(p.boot)&&hex16.test(p.press)&&p.code===null&&p.cut===false));
  const first=f.pings[0].press,second=f.pings[3].press;assert.notEqual(first,second);
  assert(f.pings.every((p,i)=>p.press===(i<3?first:second)));
  // Pressed is decided before capture and leaves with START; listening follows the START ack.
  const starts=f.reportTimes.filter(r=>r.start);assert.equal(f.pings[0].at,starts[0].at);
  assert(f.trace.indexOf('KEY_PRESS')<f.trace.indexOf('ping:pressed'));
  assert(f.pings[1].at>starts[0].at&&f.pings[1].deadlineMs===5500-(f.pings[1].at-starts[0].at));
  assert(f.pings.filter(p=>p.phase!=='listening').every(p=>p.deadlineMs===null));
  assert(f.trace.indexOf('STOP')<f.trace.indexOf('ping:released')&&f.trace.indexOf('ping:released')<f.trace.indexOf('release'));
  assert.deepEqual(f.receipts.map(r=>r.press),[first,second]);
  // One identity load per press for its MAC key, besides the PCM session's own.
  assert.equal(f.stats().identityLoads,4);
  await f.shutdown();
});
test('released is marked cut only when the hold limit ended capture',async()=>{
  const f=fixture();await f.ready();f.press('none');await done(f);
  assert.deepEqual(f.pings.map(p=>[p.phase,p.cut]),[['pressed',false],['listening',false],['released',true]]);
  await f.shutdown();
});
test('a slow, failing or absent Spark never delays START, STOP, upload or the result',async()=>{
  const timeline=f=>JSON.stringify([f.reportTimes,f.stats().releases]);
  const base=fixture();await base.ready();base.press();await base.until(()=>base.stats().releases===1);const expected=timeline(base);await base.shutdown();
  const sent=['Listening...','Processing voice command...','Voice command sent'];
  for(const [phaseReply,toasts] of [[()=>({hang:true}),sent],[()=>({error:true,delay:50}),sent],[()=>({throw:true}),sent],
    [()=>({status:500,body:'{"ok":true,"surface":"home"}'}),sent],[()=>({json:{ok:true,surface:'home'},delay:250}),['Voice command sent']]]){
    const f=fixture({phaseReply});await f.ready();f.press();await f.until(()=>f.stats().releases===1);
    assert.equal(timeline(f),expected);await done(f);
    assert.deepEqual(f.toasts,toasts);await f.shutdown();assert.equal(f.stats().reqOpened,f.stats().reqClosed);
  }
  // A hung ping is abandoned at its 300 ms budget, so the gated toast still follows.
  const f=fixture({phaseReply:()=>({hang:true})});await f.ready();f.press('none');await f.until(()=>f.trace.includes('ping:listening'));
  const at=f.stats().time;await f.until(()=>f.toasts.includes('Listening...'));assert(f.stats().time-at>=295&&f.stats().time-at<=310);
  assert(f.trace.filter(x=>x==='START').length===1&&!f.trace.includes('STOP'));await f.shutdown();
});
test('a visible Home or viewer suppresses the toasts it renders; an unclear reply means other',async()=>{
  for(const surface of ['home','viewer']){
    const f=fixture({sparkReceipt:press=>({json:{schema:1,press,done:true,toast:null,shown:true}})});f.set({surface});await f.ready();
    f.press();await done(f);f.set({failStart:true});f.press();await f.advance(400);
    assert.deepEqual(f.pings.map(p=>p.phase),['pressed','listening','released','pressed','fault']);
    assert.deepEqual(f.toasts,[]);await f.shutdown();
  }
  for(const phaseReply of [()=>({json:{ok:true,surface:'tv'}}),()=>({json:{ok:true,surface:'home',extra:1}}),()=>({json:{ok:1,surface:'home'}}),
    ()=>({json:['ok','surface']}),()=>({status:404,json:{ok:true,surface:'home'}}),()=>({body:'{"ok":true,"surface":"home"}'+' '.repeat(240)})]){
    const f=fixture({phaseReply});await f.ready();f.press();await done(f);
    assert.deepEqual(f.toasts,['Listening...','Processing voice command...','Voice command sent']);await f.shutdown();
  }
});
test('Spark receipt copy: a valid toast is shown, shown or null suppresses, anything else keeps the fixed mapping',async()=>{
  const reply=(press,extra={})=>({json:{schema:1,press,done:true,toast:'Volume up 3',shown:false,...extra}});
  const fallback='Voice command sent';
  for(const [sparkReceipt,expected] of [[p=>reply(p),'Volume up 3'],[p=>reply(p,{toast:'Back to R2D2 Home'}),'Back to R2D2 Home'],
    [p=>reply(p,{toast:null,shown:true}),null],[p=>reply(p,{toast:null}),null],[p=>reply(p,{shown:true}),null],
    [p=>reply(p,{toast:'x'.repeat(61)}),fallback],[p=>reply(p,{toast:''}),fallback],[p=>reply(p,{toast:'Café is open'}),fallback],
    [p=>reply(p,{toast:'Two\nlines'}),fallback],[p=>reply(p,{toast:7}),fallback],[p=>reply(p,{done:false,toast:null}),fallback],
    [p=>reply(p,{done:'true'}),fallback],[p=>reply(p,{shown:0}),fallback],[p=>reply(p,{press:'0'.repeat(16)}),fallback],
    [p=>reply(p,{schema:2}),fallback],[p=>reply(p,{extra:null}),fallback],[p=>({json:{schema:1,press:p,done:true,toast:'Volume up 3'}}),fallback],
    [()=>({status:404,body:''}),fallback],[()=>({body:'not json'}),fallback],[()=>({error:true}),fallback],
    [p=>({body:JSON.stringify(reply(p).json)+' '.repeat(200)}),fallback]]){
    const f=fixture({sparkReceipt});await f.ready();f.press();await done(f);
    assert.deepEqual(f.receipts.map(r=>r.press),[f.pings[0].press]);
    assert.deepEqual(f.toasts,['Listening...','Processing voice command...',...expected===null?[]:[expected]]);
    await f.shutdown();assert.equal(f.stats().reqOpened,f.stats().reqClosed);
  }
});
test('Spark copy also covers early refusals, and an unanswered receipt falls back within 1.7 s',async()=>{
  let f=fixture({refuse:{when:'upload',code:'VOICE_WARMING_UP'},sparkReceipt:press=>({json:{schema:1,press,done:true,toast:'Voice is warming up',shown:false}})});
  await f.ready();f.press();await done(f);assert.equal(f.toasts[f.toasts.length-1],'Voice is warming up');await f.shutdown();
  f=fixture({sparkReceipt:()=>({hang:true})});await f.ready();f.press();await f.until(()=>f.stats().releases===1);
  const at=f.stats().time;await done(f);assert(f.stats().time-at>=1700&&f.stats().time-at<=1710);
  assert.equal(f.toasts[f.toasts.length-1],'Voice command sent');await f.shutdown();assert.equal(f.stats().reqOpened,f.stats().reqClosed);
});
test('a press while the receipt is pending is a busy press, never a silent discard or a capture',async()=>{
  const f=fixture({sparkReceipt:()=>({hang:true})});await f.ready();f.press();await f.until(()=>f.stats().releases===1);
  f.press();await f.advance(200);assert.equal(f.toasts.filter(t=>t==='Voice is busy, try again in a moment').length,1);
  const busy=f.pings.find(p=>p.phase==='fault');assert.equal(busy.code,'BUSY');assert.notEqual(busy.press,f.pings[0].press);
  assert.equal(f.trace.filter(x=>x==='START').length,1);await done(f);await f.shutdown();
});
const faultCases=[
  ['START_UNCONFIRMED',{},async f=>{f.set({failStart:true});f.press();}],
  ['STOP_UNCONFIRMED',{},async f=>{f.set({failStop:true});f.press();}],
  ['CAPTURE_LOST',{},async f=>{f.press('none');await f.until(()=>f.trace.includes('START'));f.node.nlink=0;}],
  ['CAPTURE_LOST',{},async f=>{f.set({decodeBad:true});f.press();}],
  ['NO_SPEECH',{},async f=>f.press('short')],
  ['OWNER_KEY_RELEASED',{},async f=>f.keyQueue.push(1,0)],
  ['OWNER_KEY_RELEASED',{onRecheck(n,q){if(n===2)q.push(0);}},async f=>f.press('none')],
  ['TRANSPORT_FAILED',{},async f=>{f.set({tlsFail:true});f.press();}],
  ['RESULT_UNCONFIRMED',{receipt:{closed:true,controlFlushed:true,cancelled:false,actionAcknowledged:false,outcome:null}},async f=>f.press()],
  ['BUSY',{},async f=>{f.set({hangResult:true});f.press();await f.until(()=>f.stats().releases===1);f.press();}]];
const faultToasts={START_UNCONFIRMED:'Remote mic did not start. Try again',STOP_UNCONFIRMED:'Voice is off. Restart the TV to use voice',
  CAPTURE_LOST:'Voice command unavailable',NO_SPEECH:'Hold the microphone button while speaking',OWNER_KEY_RELEASED:'Hold the microphone button while speaking',
  TRANSPORT_FAILED:'Voice command unavailable',RESULT_UNCONFIRMED:'Sent, but not confirmed. Check the TV',BUSY:'Voice is busy, try again in a moment'};
test('each TV fault pings its closed code first, then toasts only when no R2D2 surface shows it',async()=>{
  for(const [code,options,act] of faultCases)for(const surface of ['other','home']){
    const f=fixture(options);f.set({surface});await f.ready();await act(f);await f.advance(400);
    const faults=f.pings.filter(p=>p.phase==='fault');
    assert.deepEqual(faults.map(p=>[p.code,p.deadlineMs,p.cut]),[[code,null,false]],code);
    // A fault inside a capture belongs to that press; a press that grants nothing is its own.
    const owner=f.pings.find(p=>p.phase==='pressed');
    if(code==='OWNER_KEY_RELEASED'&&!options.onRecheck||code==='BUSY')assert(!owner||owner.press!==faults[0].press);
    else assert.equal(faults[0].press,owner.press,code);
    if(surface==='home')assert.deepEqual(f.toasts,[],code);
    else{assert.equal(f.toasts[f.toasts.length-1],faultToasts[code],code);assert(f.trace.lastIndexOf('toast')>f.trace.indexOf('ping:fault'),code);}
    if(code==='CAPTURE_LOST')f.set({route:false});
    await f.shutdown();
  }
});
// Golden wire vectors, byte for byte, shared with voice/backend/test_voice_e2e.py, which
// POSTs them to Spark's real /voice-phase route: the resident's own signing code and
// Spark's parse_ping/ping_mac/resident_device_key must agree on every byte.
const GOLDEN_TV_CERT_SHA256='714f7108ae1387dc72ec6b79a0c0f8e596205dbc8159d2c538493ba664ed26a9';
const GOLDEN_PINGS=JSON.parse(process.env.R2D2_GOLDEN_DUMP?'[]':fs.readFileSync(path.join(__dirname,'test_voice_phase_golden.json'),'utf8')).pings;
test('phase pings are byte-identical to the golden vectors Spark verifies',async()=>{
  const f=fixture({tvCertSha256:GOLDEN_TV_CERT_SHA256,ids:['0123456789abcdef','00000000000000b1','fedcba9876543210']});
  await f.ready();f.press();await f.until(()=>f.resident.status().processed===1);
  await f.advance(3100);f.press('short');await f.advance(400);
  if(process.env.R2D2_GOLDEN_DUMP)console.log(JSON.stringify(f.wire));
  else assert.deepEqual(f.wire,GOLDEN_PINGS);
  await f.shutdown();
});
test('every press MAC key is wiped after its last ping, including tap and busy presses',async()=>{
  const f=fixture();await f.ready();f.keyQueue.push(1,0);await f.advance(3100);
  f.set({hangResult:true});f.press();await f.until(()=>f.stats().releases===1);f.press();await f.advance(200);
  f.complete();await done(f);await f.advance(10);
  assert.deepEqual(f.pings.map(p=>p.phase),['fault','pressed','listening','released','fault']);
  assert.equal(new Set(f.hmacKeys).size,3);assert(f.hmacKeys.every(k=>k.length===32&&k.every(x=>x===0)));
  await f.shutdown();assert(f.resident.status().cleanupComplete);
});
test('identity failure only skips pings; a descriptor-close doubt from the press key stays sticky',async()=>{
  let f=fixture();await f.ready();f.set({identityBad:true});f.press();await f.advance(400);
  assert.deepEqual(f.pings,[]);
  assert.deepEqual(f.toasts,['Listening...','Processing voice command...','Voice command unavailable']);await f.shutdown();
  f=fixture({identityCloseOnce:true});await f.ready();f.press();await done(f);await f.advance(10);
  assert.deepEqual(f.pings,[]);assert.equal(f.stats().releases,1);assert.equal(f.toasts[f.toasts.length-1],'Voice command sent');
  await f.shutdown();assert.equal(f.resident.status().cleanupComplete,false);
});
test('actual owner_key reader joins resident START, physical release and descriptor cleanup',async()=>{
  const key=require('./test_owner_key').fixture(),f=fixture({ownerKey:key.reader,realKey:key});
  await f.ready();f.press('none');await f.until(()=>f.trace.includes('START'));await f.advance(10);key.push(0);
  await f.until(()=>f.resident.status().processed===1);assert(f.trace.includes('STOP'));assert(f.stats().time<1000);
  await f.shutdown();assert.equal(key.files.size,0);assert(key.buffers.every(b=>b.every(x=>x===0)));assert(f.resident.status().cleanupComplete);
});
test('actual owner_key SYN_DROPPED during capture stops and withholds PCM',async()=>{
  const key=require('./test_owner_key').fixture(),f=fixture({ownerKey:key.reader,realKey:key});
  await f.ready();f.press('none');await f.until(()=>f.trace.includes('START'));key.push(0,3,0);await f.advance(20);
  assert(f.trace.includes('STOP'));assert.equal(f.stats().decodeCalls,0);assert.equal(key.files.size,0);await f.shutdown();
});
test('actual release during pre-START metadata scan is drained before START',async()=>{
  const key=require('./test_owner_key').fixture();let armed=false,polls=0,released=false;
  const ownerKey={open:r=>key.reader.open(r),close:()=>key.reader.close(),poll(discard){if(armed&&++polls===2)key.set({clock:101});return key.reader.poll(discard);}};
  const scan=key.fake.opendirSync;key.fake.opendirSync=function(p){if(armed&&polls===2&&!released){key.push(0);released=true;}return scan(p);};
  const f=fixture({ownerKey,realKey:key});await f.ready();armed=true;f.press('none');await f.advance(10);
  assert(released);assert(!f.trace.includes('START'));assert.equal(f.stats().decodeCalls,0);await f.shutdown();assert.equal(key.files.size,0);
});
test('actual pre-START poll cannot rescan after its final key drain',async()=>{
  const key=require('./test_owner_key').fixture();let armed=false,polls=0,drained=false,lateScan=false;
  const ownerKey={open:r=>key.reader.open(r),close:()=>key.reader.close(),poll(discard){if(armed)polls++;return key.reader.poll(discard);}};
  const read=key.fake.readSync;key.fake.readSync=function(fd,...args){try{return read(fd,...args);}catch(e){if(armed&&polls===2&&e.code==='EAGAIN'&&key.files.get(fd).bytes===null){drained=true;key.set({clock:101});}throw e;}};
  const scan=key.fake.opendirSync;key.fake.opendirSync=function(p){if(drained&&polls===2){lateScan=true;key.push(0);}return scan(p);};
  const f=fixture({ownerKey,realKey:key});await f.ready();armed=true;f.press('none');await f.advance(10);
  assert(drained);assert.equal(lateScan,false);assert(f.trace.includes('START'));await f.shutdown();assert.equal(key.files.size,0);
});
test('initial asleep remote clears firstOpen so later wake open does not perform firstOpen drain',async()=>{
  const options={deriveFails:true},f=fixture(options);f.begin();await f.advance(250);
  assert.equal(f.stats().opened,0);assert.equal(f.resident.status().state,'unavailable');
  options.deriveFails=false;await f.until(()=>f.resident.status().state==='ready');
  assert.equal(f.stats().opened,1);assert.equal(f.reads.length,1);
  f.press();await f.until(()=>f.resident.status().processed===1);await f.shutdown();
});
test('CLI private config rejects wrong owner/mode/nonfile before dependencies and closes fd',async()=>{
  for(const change of [{uid:1},{mode:0o100644},{isFile:()=>false},{size:8193}]) {
    let closes=0,opens=0,reads=0;const module={exports:{}},process={exitCode:0};
    const fakeFs={constants:fs.constants,openSync(p,flags){opens++;assert.equal(p,'/fixture/resident_config.json');assert(flags&fs.constants.O_NONBLOCK);assert(flags&fs.constants.O_NOFOLLOW);return 42;},
      fstatSync(){return{uid:0,mode:0o100600,size:50,isFile:()=>true,...change};},closeSync(fd){assert.equal(fd,42);closes++;},readSync(){reads++;throw Error('UNEXPECTED_READ');}};
    const require=name=>{assert.equal(name,'fs');return fakeFs;};require.main=module;
    vm.runInNewContext(source,{module,require,Buffer,process,__dirname:'/fixture'}, {filename});
    for(let i=0;i<10;i++)await Promise.resolve();assert.equal(opens,1);assert.equal(closes,1);assert.equal(reads,0);assert.equal(process.exitCode,1);
  }
});
test('CLI malformed/grown config is bounded and its mutable read buffer is wiped',async()=>{
  for(const bytes of [Buffer.from('{bad json'),Buffer.alloc(8193,65)]) {
    let buffer=null,closes=0;const module={exports:{}},process={exitCode:0};
    const fakeFs={constants:fs.constants,openSync(){return 42;},fstatSync(){return{uid:0,mode:0o100600,size:Math.min(bytes.length,8192),isFile:()=>true};},
      closeSync(){closes++;},readSync(fd,b,offset,length,position){assert.equal(length,8193);assert.equal(position,0);buffer=b;bytes.copy(b);return bytes.length;}};
    const require=name=>{assert.equal(name,'fs');return fakeFs;};require.main=module;
    vm.runInNewContext(source,{module,require,Buffer,process,__dirname:'/fixture'},{filename});
    for(let i=0;i<10;i++)await Promise.resolve();assert.equal(process.exitCode,1);assert.equal(closes,1);assert(buffer.every(x=>x===0));
  }
});
const watchdog=setTimeout(()=>{console.error('resident suite did not settle');process.exitCode=1;},10000);
(async()=>{let completed=0;try{for(const [name,fn] of cases){await fn();completed++;}assert.equal(completed,cases.length);console.log(completed+' resident microphone controls passed');}
catch(error){console.error('FAIL '+cases[completed][0]);console.error(error);process.exitCode=1;}finally{clearTimeout(watchdog);}})();
