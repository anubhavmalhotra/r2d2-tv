'use strict';
const assert=require('assert'),EventEmitter=require('events'),{bind,PRESS_MS,UI_PRESS_MS}=require('./owner_service');
const turn=()=>new Promise(r=>setImmediate(r));
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});promise.catch(()=>{});return{promise,resolve,reject};}
let groups=0;
function fixture(feedback,extra={}){
 let now=0,active=true,cleanup=true,holdResult=false,nextTimer=0;const jobs=[],timers=new Map(),handle=new EventEmitter();handle.registerMethod=()=>{};
 const service=bind(handle,{getTlsConfig(){throw Error('not used by fake job');},isActive:()=>active,provider:{},feedback},{now:()=>now,setTimeout(callback,delay){const id=++nextTimer;timers.set(id,{at:now+delay,callback});return id;},clearTimeout(id){timers.delete(id);},start(cfg){
  const ready=deferred(),closed=deferred();const job={cfg,ready:ready.promise,closed:closed.promise,releases:0,cancels:[],go:()=>ready.resolve({providerReady:true}),audio:()=>cfg.onAudio(),end:receipt=>closed.resolve(receipt),complete:()=>closed.resolve({cleanupComplete:cleanup}),release(){this.releases++;if(!holdResult)closed.resolve({cleanupComplete:cleanup});return closed.promise;},cancel(reason){this.cancels.push(reason);ready.reject(Error('cancelled'));closed.resolve({cleanupComplete:cleanup,reason});return closed.promise;}};jobs.push(job);return job;
 },...extra});
 function request(method,body,peer=':peer.1',caller='com.webos.starfish.keyfilters'){
  const replies=[];handle.emit('request',{category:()=>'/capture',method:()=>method,senderServiceName:()=>caller,sender:()=>peer,payload:()=>JSON.stringify({issuedAt:now,...body}),respond:raw=>replies.push(JSON.parse(raw))});return replies;
 }
 function advance(to){assert(to>=now);while(true){const due=[...timers].filter(([,v])=>v.at<=to).sort((a,b)=>a[1].at-b[1].at)[0];if(!due)break;now=due[1].at;timers.delete(due[0]);due[1].callback();}now=to;}
 return{service,jobs,request,advance,timers,setTime:v=>{now=v;},setActive:v=>{active=v;},setCleanup:v=>{cleanup=v;},setHold:v=>{holdResult=v;}};
}
// ---- Physical-press voice feedback (owner_service hooks + voice_feedback.js) ----
const V=require('./voice_feedback'),crypto=require('crypto'),fs=require('fs'),path=require('path');
const HMAC=crypto.createHmac.bind(crypto),VK='a'.repeat(64),SPARK=['192','168','60','80'].join('.');
const GOLDEN=JSON.parse(fs.readFileSync(path.join(__dirname,'..','test_voice_phase_golden.json'),'utf8'));
const CLOSED=new Set([...Object.values(V.FAULTS),...Object.values(V.NOTICES),...Object.values(V.CODES),V.SENT]);
// Independent oracle: Python's json.dumps(sort_keys=True,separators=(',',':')) for flat rows.
function canonical(o){return '{'+Object.keys(o).sort().map(k=>JSON.stringify(k)+':'+JSON.stringify(o[k])).join(',')+'}';}
function pingKey(cert){return crypto.createHash('sha256').update(Buffer.concat([Buffer.from('R2D2-RESIDENT-PTT-v1\0','ascii'),Buffer.from(VK,'hex'),Buffer.from(cert,'hex')])).digest();}
function verifyPing(body,cert){
 const o=JSON.parse(body),mac=o.mac;delete o.mac;
 assert.deepEqual(Object.keys(o).sort(),['boot','code','cut','deadlineMs','phase','press','schema','seq']);
 assert.equal(mac,HMAC('sha256',pingKey(cert)).update(Buffer.from('R2D2-voice-phase-v1\0'+canonical(o),'ascii')).digest('hex'));
 assert.equal(body,canonical(Object.assign({},o,{mac})));return o;
}
function verifyLatch(body,cert){
 const o=JSON.parse(body),mac=o.mac;delete o.mac;
 assert.deepEqual(Object.keys(o).sort(),['boot','facts','reason','schema','seq','state']);
 assert.equal(o.schema,1);assert(/^[a-f0-9]{16}$/.test(o.boot));assert(Number.isSafeInteger(o.seq)&&o.seq>=1);assert(['latched','cleared'].includes(o.state));
 assert(Number.isInteger(o.facts)&&o.facts>=0&&o.facts<128);assert(o.reason==='OTHER'||V.LATCH_REASONS.has(o.reason));
 assert.equal(mac,HMAC('sha256',pingKey(cert)).update(Buffer.from('R2D2-owner-latch-v1\0'+canonical(o),'ascii')).digest('hex'));
 assert.equal(body,canonical(Object.assign({},o,{mac})));return o;
}
function recorder(throwing=false){
 const calls=[];let n=0;
 const record=name=>(...args)=>{calls.push([name,...args]);if(throwing)throw Error('feedback fault');return name==='begin'?{press:++n}:undefined;};
 return {calls,begin:record('begin'),listening:record('listening'),released:record('released'),settle:record('settle'),refused:record('refused'),latch:record('latch')};
}
async function ownerFeedbackGroups(){
 let groups=0;
 // What the owner reports, and that a throwing feedback module changes no capture reply or state.
 const outcomes=[];
 for(const throwing of [false,true]){
  const v=recorder(throwing),f=fixture(v),seen=[];
  const r1=f.request('begin',{request:1,deviceId:'physical'});assert.equal(typeof f.jobs[0].cfg.onAudio,'function');
  f.jobs[0].go();await turn();seen.push(r1[0].providerReady,f.service.status().state);
  f.jobs[0].audio();f.jobs[0].audio();f.setTime(5300);
  seen.push(f.request('release',{request:1})[0].returnValue,f.jobs[0].releases);f.jobs[0].audio();await turn();seen.push(f.service.status().state);
  // A press released before any audio sends no "released"; its close still settles.
  f.setTime(9000);const r2=f.request('begin',{request:2,deviceId:'physical'});f.jobs[1].go();await turn();
  seen.push(r2[0].providerReady,f.request('release',{request:2})[0].returnValue);await turn();
  // A quick tap: the keyfilter cancels before the provider is ready.
  f.setTime(12000);f.request('begin',{request:3,deviceId:'physical'});seen.push(f.request('cancel',{request:3})[0].returnValue);await turn();
  seen.push(f.jobs[2].cancels.join());
  const press=x=>throwing?null:{press:x};
  assert.deepEqual(v.calls,[['begin'],['listening',press(1)],['released',press(1),5300],
   ['settle',press(1),{cleanupComplete:true},{ready:true,heard:true,released:true,stuck:false}],
   ['begin'],['settle',press(2),{cleanupComplete:true},{ready:true,heard:false,released:true,stuck:false}],
   ['begin'],['settle',press(3),{cleanupComplete:true,reason:'CAPTURE_CANCELLED'},{ready:false,heard:false,released:false,stuck:false}]]);
  outcomes.push(JSON.stringify(seen));await f.service.close();
 }
 assert.equal(outcomes[0],outcomes[1]);groups++;
 // Refused fresh presses say why: voice off (unresolved cleanup) or busy (an on-screen
 // capture holds the owner). Stale, inactive and replayed requests say nothing.
 {
  const v=recorder(),f=fixture(v);f.request('begin',{request:1,deviceId:'physical'});f.setCleanup(false);
  await f.service.cancelActive('POWER_UNKNOWN');await turn();
  assert.equal(f.request('begin',{request:2,deviceId:'physical'})[0].returnValue,false);
  assert.equal(f.request('begin',{request:2,deviceId:'physical'})[0].returnValue,false);
  f.setActive(false);assert.equal(f.request('begin',{request:3,deviceId:'physical'})[0].returnValue,false);
  assert.deepEqual(v.calls.filter(c=>c[0]==='refused'),[['refused','STOP_UNCONFIRMED'],['refused','STOP_UNCONFIRMED']]);
  assert.deepEqual(v.calls.find(c=>c[0]==='settle')[3],{ready:false,heard:false,released:false,stuck:true});
  await f.service.close();groups++;
 }
 {
  const v=recorder(),handle=new EventEmitter();handle.registerMethod=()=>{};
  const service=bind(handle,{getTlsConfig(){},isActive:()=>true,isUiQualified:()=>true,provider:{},feedback:v},
   {now:()=>0,startUi(){return {ready:new Promise(()=>{}),closed:new Promise(()=>{}),cancel(){},release(){}};}});
  assert.equal(service.uiCommand({action:'start',request:'0123456789abcdef'}).state,'preparing');
  const replies=[];handle.emit('request',{category:()=>'/capture',method:()=>'begin',senderServiceName:()=>'com.webos.starfish.keyfilters',
   sender:()=>':peer.1',payload:()=>JSON.stringify({issuedAt:0,request:1,deviceId:'physical'}),respond:raw=>replies.push(JSON.parse(raw))});
  assert.equal(replies[0].returnValue,false);assert.deepEqual(v.calls,[['refused','BUSY']]);groups++;
 }
 // A newer press still supersedes an in-flight one; standby settles with its own reason.
 {
  const v=recorder(),f=fixture(v);f.setHold(true);f.request('begin',{request:1,deviceId:'physical'});f.jobs[0].go();await turn();f.jobs[0].audio();
  f.request('release',{request:1});f.request('begin',{request:2,deviceId:'physical'});await turn();
  assert.deepEqual(f.jobs[0].cancels,['SUPERSEDED']);
  assert.deepEqual(v.calls.map(c=>c[0]),['begin','listening','released','begin','settle']);assert.equal(v.calls[4][2].reason,'SUPERSEDED');
  f.jobs[1].go();await turn();await f.service.cancelActive('TV_INACTIVE');await turn();
  assert.equal(v.calls[5][2].reason,'TV_INACTIVE');await f.service.close();groups++;
 }
 return groups;
}
function harness(options={}){
 let time=0,next=0,surface=options.surface||'other',allowed=true,idn=0;
 const timers=new Map(),wire=[],pings=[],gets=[],toasts=[],latches=[],receipts=(options.receipts||[]).slice(),ids=(options.ids||[]).slice();
 const cert=options.cert||'c'.repeat(64),loads=[];
 const later=(fn,ms)=>{const id=++next;timers.set(id,{at:time+ms,fn});return id;},clear=id=>{timers.delete(id);};
 const flush=async()=>{for(let i=0;i<40;i++)await Promise.resolve();};
 async function advance(ms){
  const end=time+ms;await flush();
  for(let n=0;;n++){assert(n<100000,'spin');const due=[...timers].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at||a[0]-b[0])[0];
   if(!due)break;time=Math.max(time,due[1].at);timers.delete(due[0]);due[1].fn();await flush();}
  time=end;await flush();
 }
 const http={request(o,callback){
  assert.equal(o.host,SPARK);assert.equal(o.port,4181);assert.equal(o.headers.Host,SPARK+':4181');
  const req=new EventEmitter();let gone=false;req.destroy=()=>{gone=true;};
  req.end=body=>{
   let reply;
   if(o.method==='POST'){
    assert(['/'+VK+'/voice-phase','/'+VK+'/owner-latch'].includes(o.path));assert.deepEqual(Object.keys(o.headers).sort(),['Content-Length','Content-Type','Host']);
    assert.equal(o.headers['Content-Type'],'application/json');assert(Buffer.isBuffer(body)&&body.length<=256);assert.equal(o.headers['Content-Length'],body.length);
    const text=body.toString('ascii');
    if(o.path.endsWith('/owner-latch')){latches.push(Object.assign(verifyLatch(text,cert),{at:time,text}));reply=options.latch?options.latch(latches[latches.length-1]):{status:200,body:{ok:true}};}
    else{wire.push(text);pings.push(Object.assign(verifyPing(text,cert),{at:time}));
     reply=options.phase?options.phase(pings[pings.length-1]):{status:200,body:{ok:true,surface}};}
   }else{
    assert.equal(o.method,'GET');assert.equal(body,undefined);assert.deepEqual(Object.keys(o.headers),['Host']);
    const m=/^\/a{64}\/voice-receipt\/([a-f0-9]{16})$/.exec(o.path);assert(m);gets.push({id:m[1],at:time});
    reply=receipts.length?receipts.shift():options.receipt||{status:404,body:{}};
    if(typeof reply==='function')reply=reply(m[1]);
   }
   if(reply==='hang')return;
   if(reply==='error'){queueMicrotask(()=>req.emit('error',Error('ECONNREFUSED')));return;}
   const deliver=()=>{if(gone)return;const res=new EventEmitter();res.statusCode=reply.status;res.destroy=()=>{};callback(res);
    res.emit('data',Buffer.from(JSON.stringify(reply.body)));res.emit('end');res.emit('close');req.emit('close');};
   if(reply.delay)later(deliver,reply.delay);else queueMicrotask(deliver);
  };
  return req;
 }};
 const deps={http,setTimeout:later,clearTimeout:clear,now:()=>time,
  identity:{load(){loads.push(time);if(options.identityFails)throw Error('DEVICE_IDENTITY');return {certificateSha256:cert};}},
  randomBytes(n){assert.equal(n,8);return Buffer.from(ids.length?ids.shift():(++idn).toString(16).padStart(16,'0'),'hex');},
  toast:text=>{toasts.push(text);return true;}};
 if(options.deps)Object.assign(deps,options.deps);
 const voice=V.create({sparkIp:SPARK,httpPort:4181,voiceKey:VK,allowed:()=>allowed},deps);
 return {voice,advance,wire,pings,gets,toasts,latches,timers,loads,receipts,now:()=>time,
  set(o){if('surface' in o)surface=o.surface;if('allowed' in o)allowed=o.allowed;}};
}
const RELEASED={ready:true,heard:true,released:true,stuck:false};
const done=(toast,extra={})=>Object.assign({status:200,body:{schema:1,press:null,done:true,toast,shown:false}},extra);
function row(id,fields){return {status:200,body:Object.assign({schema:1,press:id,done:true,toast:null,shown:false},fields)};}
async function feedbackGroups(){
 let groups=0;
 // Closed copy: eight TV fault codes; every fixed text is printable ASCII, at most 60 characters.
 assert.deepEqual(Object.keys(V.FAULTS).sort(),['BUSY','CAPTURE_LOST','NO_SPEECH','OWNER_KEY_RELEASED','RESULT_UNCONFIRMED','START_UNCONFIRMED','STOP_UNCONFIRMED','TRANSPORT_FAILED']);
 for(const text of CLOSED)assert(/^[\x20-\x7e]{1,60}$/.test(text)&&V.printable(text),text);
 assert(Object.isFrozen(V.FAULTS)&&Object.isFrozen(V.CODES)&&Object.isFrozen(V.NOTICES));
 for(const bad of ['','x'.repeat(61),'I’m sorry','tab\there',null,7])assert.equal(V.printable(bad),false);groups++;
 // Golden wire vectors shared with voice/backend/test_voice_e2e.py: byte for byte.
 const goldenKey=V.deviceKey(VK,GOLDEN.tvCertificateSha256);assert(goldenKey.equals(pingKey(GOLDEN.tvCertificateSha256)));
 for(const ping of GOLDEN.pings){const value=JSON.parse(ping);delete value.mac;assert.equal(V.sign(goldenKey,value),ping);verifyPing(ping,GOLDEN.tvCertificateSha256);}
 assert.throws(()=>V.deviceKey(VK,'z'.repeat(64)));assert.throws(()=>V.deviceKey('A'.repeat(64),GOLDEN.tvCertificateSha256));groups++;
 // The owner pipeline reproduces the golden presses (ids, boot and seq order), MAC keys load
 // after the current turn and are wiped after each press's last ping.
 {
  const macKeys=[],real=crypto.createHmac;crypto.createHmac=function(alg,key){if(Buffer.isBuffer(key))macKeys.push(key);return real.apply(crypto,arguments);};
  try{
   const h=harness({cert:GOLDEN.tvCertificateSha256,ids:['0123456789abcdef','00000000000000b1','fedcba9876543210']});
   const p1=h.voice.begin();assert.equal(h.wire.length,0);for(let i=0;i<20;i++)await Promise.resolve();
   // The identity (and MAC key) load waits for a later turn than the capture start's own work.
   assert.equal(h.loads.length,0);assert.equal(h.wire.length,0);
   await h.advance(1);assert.deepEqual(h.loads,[0]);h.voice.listening(p1);await h.advance(1);h.voice.released(p1,1200);
   h.voice.settle(p1,{reason:'released',cleanupComplete:true,outcome:{state:'verified',code:'TV_FOREGROUND_VERIFIED'},actionAcknowledged:true},RELEASED);
   await h.advance(3100);
   const p2=h.voice.begin();await h.advance(1);h.voice.listening(p2);await h.advance(400);
   h.voice.settle(p2,{reason:'PCM_INCOMPLETE',cleanupComplete:true},RELEASED);await h.advance(10);
   assert.equal(h.wire.length,6);
   for(const i of [0,2,3,5])assert.equal(h.wire[i],GOLDEN.pings[i]);
   for(const i of [1,4]){const got=JSON.parse(h.wire[i]),want=JSON.parse(GOLDEN.pings[i]);assert.equal(got.deadlineMs,V.HOLD_MS-1);
    delete got.mac;delete want.mac;got.deadlineMs=want.deadlineMs;assert.deepEqual(got,want);}
   assert.deepEqual(h.toasts,['Listening...','Processing voice command...','Voice command sent','Listening...','Hold the microphone button while speaking']);
   assert(macKeys.length>=6&&macKeys.every(k=>k.every(b=>b===0)));
   assert.deepEqual(h.voice.status(),{closed:false,presses:0,requests:0,timers:0,children:0});
  }finally{crypto.createHmac=real;}
  groups++;
 }
 // The hold limit: listening counts down to the keyfilter's 9.2 s release (never past Spark's
 // 10 s deadlineMs bound) and a release at the limit, or past it, is "released" with cut true.
 {
  assert.equal(V.HOLD_MS,9200);assert.equal(V.DEADLINE_MAX_MS,10000);
  const h=harness(),cuts=[];
  for(const [held,cut] of [[V.HOLD_MS,true],[V.HOLD_MS+20,true],[10200,true],[V.HOLD_MS-5,true],[V.HOLD_MS-6,false],[5200,false],[NaN,false]]){
   const p=h.voice.begin();await h.advance(1);h.voice.listening(p);await h.advance(Number.isFinite(held)?held-1:1);
   h.voice.released(p,held);await h.advance(1);
   const mine=h.pings.filter(x=>x.press===h.pings[h.pings.length-1].press);
   assert.deepEqual(mine.map(x=>x.phase),['pressed','listening','released']);
   assert.equal(mine[1].deadlineMs,V.HOLD_MS-1);assert(mine[1].deadlineMs<=V.DEADLINE_MAX_MS);
   assert.equal(mine[2].deadlineMs,null);cuts.push(mine[2].cut);assert.equal(mine[2].cut,cut,String(held));
   h.voice.settle(p,{reason:'released',cleanupComplete:true,outcome:{state:'verified',code:'TV_FOREGROUND_VERIFIED'},actionAcknowledged:true},RELEASED);await h.advance(4000);
  }
  assert.deepEqual(cuts,[true,true,true,true,false,false,false]);
  // A late first PCM counts down what is left of the limit, never below zero.
  const late=h.voice.begin();await h.advance(V.HOLD_MS-1500);h.voice.listening(late);await h.advance(1);
  assert.equal(h.pings[h.pings.length-1].deadlineMs,1500);h.voice.close();groups++;
 }
 // Surface-aware: Home or the viewer showing the press suppresses the toast; an old Spark
 // (404), a malformed reply or a ping past its 300 ms budget means "other".
 for(const [phase,toasted,wait] of [[{status:200,body:{ok:true,surface:'home'}},false,5],[{status:200,body:{ok:true,surface:'viewer'}},false,5],
  [{status:200,body:{ok:true,surface:'other'}},true,5],[{status:404,body:{}},true,5],[{status:200,body:{ok:true,surface:'home',extra:1}},true,5],
  [{status:200,body:{ok:true,surface:'tv'}},true,5],['error',true,5],['hang',true,V.PING_MS]]){
  const h=harness({phase:()=>phase});const p=h.voice.begin();await h.advance(1);h.voice.listening(p);
  if(wait>5){await h.advance(wait-2);assert.deepEqual(h.toasts,[]);}
  await h.advance(5);assert.deepEqual(h.toasts,toasted?['Listening...']:[]);h.voice.close();
 }
 groups++;
 // Receipt: re-poll while Spark has only admitted the command, then its closed copy once.
 {
  const h=harness({receipts:[id=>row(id,{done:false}),id=>row(id,{done:false}),id=>row(id,{toast:'Volume up 2'})]});
  const p=h.voice.begin();await h.advance(1);h.voice.listening(p);h.voice.released(p,900);
  h.voice.settle(p,{reason:'released',cleanupComplete:true,outcome:{state:'accepted',code:'AGENT_CALL_ADMITTED'},actionAcknowledged:true},RELEASED);
  await h.advance(2000);
  assert.equal(h.gets.length,3);assert(h.gets.every(g=>g.id===h.pings[0].press));assert(h.gets[1].at-h.gets[0].at>=250&&h.gets[2].at-h.gets[1].at>=250);
  assert.deepEqual(h.toasts,['Listening...','Processing voice command...','Volume up 2']);assert.equal(h.pings.length,3);groups++;
 }
 for(const fields of [{shown:true},{shown:true,toast:'Muted'},{toast:null}]){
  const h=harness({surface:'home',receipts:[id=>row(id,fields)]});const p=h.voice.begin();await h.advance(1);
  h.voice.settle(p,{reason:'released',cleanupComplete:true,outcome:null},RELEASED);await h.advance(100);
  assert.equal(h.gets.length,1);assert.deepEqual(h.toasts,[]);
 }
 groups++;
 // An older Spark has no route (404, or any 4xx), and a malformed row is not trusted:
 // the fixed code mapping, at once, with no fault ping.
 for(const [reply,receipt,dispatched,text] of [
  [{status:404,body:{}},{reason:'released',outcome:{state:'accepted',code:'AGENT_CALL_ADMITTED'},actionAcknowledged:true},true,'Voice command sent'],
  [{status:404,body:{}},{reason:'released',outcome:{state:'refused',code:'VOICE_WARMING_UP'}},true,'Voice model warming up - try again in a moment'],
  [{status:403,body:{}},{reason:'released',outcome:{state:'refused',code:'RATE_LIMITED'}},true,'Too many requests, wait a moment'],
  [{status:404,body:{}},{reason:'RESULT_UNCONFIRMED',outcome:null},true,'Sent, but not confirmed. Check the TV'],
  [{status:404,body:{}},{reason:'released',outcome:{state:'unknown',code:'EXECUTION_UNCONFIRMED'}},true,'Sent, but not confirmed. Check the TV'],
  [{status:404,body:{}},{reason:'TRANSPORT_CLOSED',providerStarted:true,outcome:null},false,'Voice command unavailable'],
  [id=>row(id,{toast:'I’m sorry, I cannot do that'}),{reason:'released',outcome:{state:'refused',code:'AGENT_PAUSED'}},true,'Voice control is paused. Restart Spark to resume'],
  [id=>row(id,{toast:'x'.repeat(61)}),{reason:'released',outcome:null,actionAcknowledged:true},true,'Voice command sent'],
  [id=>row('0'.repeat(16),{toast:'Muted'}),{reason:'released',outcome:null,actionAcknowledged:true},true,'Voice command sent'],
  [id=>row(id,{toast:'Muted',transcript:'turn it up'}),{reason:'released',outcome:null,actionAcknowledged:true},true,'Voice command sent']]){
  const h=harness({receipts:[reply]});const p=h.voice.begin();await h.advance(1);
  h.voice.settle(p,Object.assign({cleanupComplete:true},receipt),dispatched?RELEASED:{ready:true,heard:true,released:false,stuck:false});
  await h.advance(10);assert.equal(h.gets.length,1);assert.deepEqual(h.toasts,[text]);assert.deepEqual(h.pings.map(x=>x.phase),['pressed']);
 }
 groups++;
 // Never done within 15 s (or Spark unreachable): this TV's own uncertainty, signed and toasted.
 for(const [reply,dispatched,code] of [[id=>row(id,{done:false}),true,'RESULT_UNCONFIRMED'],['error',true,'RESULT_UNCONFIRMED'],
  [{status:503,body:{}},true,'RESULT_UNCONFIRMED'],[id=>row(id,{done:false}),false,'TRANSPORT_FAILED']]){
  const h=harness({receipt:reply});const p=h.voice.begin();await h.advance(1);
  h.voice.settle(p,{reason:dispatched?'released':'TRANSPORT_CLOSED',providerStarted:true,cleanupComplete:true,outcome:null},dispatched?RELEASED:{ready:true,heard:true,released:false,stuck:false});
  await h.advance(V.RECEIPT_MS-300);assert.deepEqual(h.toasts,[]);assert.equal(h.pings.length,1);
  await h.advance(600);const last=h.pings[h.pings.length-1];
  assert.equal(last.phase,'fault');assert.equal(last.code,code);assert.deepEqual(h.toasts,[V.FAULTS[code]]);
  assert(h.gets.length>=2&&h.gets.length<=V.RECEIPT_MS/250+2,String(h.gets.length));
  const count=h.gets.length;await h.advance(20000);assert.equal(h.gets.length,count);assert.equal(h.voice.status().timers,0);
 }
 groups++;
 // A Spark that holds each receipt request (up to 1.5 s) is asked again without a pause.
 {
  const h=harness({receipt:id=>Object.assign(row(id,{done:false}),{delay:1500})});const p=h.voice.begin();await h.advance(1);
  h.voice.settle(p,{reason:'released',cleanupComplete:true,outcome:null},RELEASED);await h.advance(4600);
  assert.deepEqual(h.gets.map(g=>g.at),[1,1501,3001,4501]);h.voice.close();groups++;
 }
 // Ignored-press hints: a quick tap always ends its press on Spark, but toasts at most once
 // per 3 s; a refused press (busy / voice off) is its own press, ping and toast both limited.
 {
  const h=harness(),tap={reason:'CAPTURE_CANCELLED',cleanupComplete:true},early={ready:false,heard:false,released:false,stuck:false};
  let p=h.voice.begin();await h.advance(1);h.voice.settle(p,tap,early);await h.advance(5);
  assert.deepEqual(h.toasts,['Hold the microphone button while speaking']);
  await h.advance(1000);p=h.voice.begin();await h.advance(1);h.voice.settle(p,tap,early);h.voice.refused('BUSY');await h.advance(5);
  assert.deepEqual(h.pings.map(x=>x.phase+':'+x.code),['pressed:null','fault:OWNER_KEY_RELEASED','pressed:null','fault:OWNER_KEY_RELEASED']);
  assert.equal(h.toasts.length,1);
  await h.advance(2100);h.voice.refused('BUSY');await h.advance(5);
  assert.deepEqual(h.toasts,['Hold the microphone button while speaking','Voice is busy, try again in a moment']);
  const busy=h.pings[h.pings.length-1];assert.equal(busy.code,'BUSY');assert(!h.pings.slice(0,4).some(x=>x.press===busy.press));
  h.voice.refused('STOP_UNCONFIRMED');h.voice.refused('NOT_A_FAULT');await h.advance(3100);h.voice.refused('NOT_A_FAULT');h.voice.refused('STOP_UNCONFIRMED');await h.advance(5);
  assert.deepEqual(h.toasts.slice(2),['Voice is off. Restart the TV to use voice']);
  // The tap after the ready reply is a start failure, not a hint: never rate limited.
  p=h.voice.begin();await h.advance(1);h.voice.settle(p,tap,{ready:true,heard:false,released:false,stuck:false});await h.advance(5);
  assert.equal(h.toasts[h.toasts.length-1],'Remote mic did not start. Try again');groups++;
 }
 // Close reasons -> drop, a fixed fault, or Spark's result.
 const R=(reason,extra={})=>Object.assign({reason,cleanupComplete:true},extra);
 for(const [receipt,facts,want] of [
  [R('TV_INACTIVE'),RELEASED,{drop:true}],[R('CLIENT_CLOSED'),RELEASED,{drop:true}],[R('SUPERSEDED'),RELEASED,{drop:true}],
  [R('POWER_UNKNOWN'),{ready:true,heard:true,released:false},{drop:true}],[R('POWER_UNKNOWN'),RELEASED,{result:true,dispatched:true}],
  [R('released'),{...RELEASED,stuck:true},{code:'STOP_UNCONFIRMED'}],[null,{stuck:true},{code:'STOP_UNCONFIRMED'}],[null,{},{code:'CAPTURE_LOST'}],
  [R('PCM_INCOMPLETE'),RELEASED,{code:'NO_SPEECH'}],[R('PROVIDER_REAP_UNQUALIFIED'),RELEASED,{code:'CAPTURE_LOST'}],
  [R('released'),RELEASED,{result:true,dispatched:true}],[R('RESULT_UNCONFIRMED'),RELEASED,{result:true,dispatched:true}],[R('TRANSPORT_FAILED'),RELEASED,{result:true,dispatched:true}],
  [R('CAPTURE_CANCELLED'),{ready:false},{code:'OWNER_KEY_RELEASED',hint:true}],[R('RELEASE_BEFORE_READY'),{ready:false},{code:'OWNER_KEY_RELEASED',hint:true}],
  [R('CAPTURE_CANCELLED'),{ready:true,heard:false},{code:'START_UNCONFIRMED'}],[R('CAPTURE_CANCELLED'),{ready:true,heard:true},{code:'CAPTURE_LOST'}],
  [R('CAPTURE_DEADLINE'),{ready:true,heard:true},{code:'CAPTURE_LOST'}],[R('TRANSPORT_START_FAILED'),{},{code:'TRANSPORT_FAILED'}],
  [R('PROVIDER_START_TIMEOUT'),{ready:false},{code:'START_UNCONFIRMED'}],[R('PROVIDER_START_FAILED'),{},{code:'START_UNCONFIRMED'}],
  [R('TRANSPORT_CLOSED',{providerStarted:true}),{ready:true,heard:true},{result:true,dispatched:false}],
  [R('TRANSPORT_CLOSED',{providerStarted:false}),{},{code:'CAPTURE_LOST'}],[R('PCM_BACKPRESSURE'),{ready:true,heard:true},{code:'CAPTURE_LOST'}]])
  assert.deepEqual(V.classify(receipt,facts),want,JSON.stringify(receipt));
 groups++;
 // Only the newest press may toast: a new press cancels an older press's receipt wait.
 {
  const h=harness({receipt:'hang'});const p1=h.voice.begin();await h.advance(1);
  h.voice.settle(p1,{reason:'released',cleanupComplete:true,outcome:null},RELEASED);await h.advance(500);assert.equal(h.voice.status().requests,1);
  const p2=h.voice.begin();await h.advance(1);assert.equal(h.voice.status().requests,0);
  h.voice.listening(p1);h.voice.released(p1,10);h.voice.settle(p1,{reason:'PCM_INCOMPLETE',cleanupComplete:true},RELEASED);
  await h.advance(V.RECEIPT_MS+2000);assert.deepEqual(h.toasts,[]);assert.deepEqual(h.pings.map(x=>x.phase),['pressed','pressed']);
  h.voice.listening(p2);await h.advance(5);assert.deepEqual(h.toasts,['Listening...']);h.voice.close();groups++;
 }
 // Standby or shutdown: no toasts; a closed module sends and schedules nothing.
 {
  const h=harness();const p=h.voice.begin();await h.advance(1);h.voice.listening(p);h.set({allowed:false});await h.advance(5);
  assert.deepEqual(h.toasts,[]);h.voice.refused('BUSY');h.voice.settle(p,{reason:'PCM_INCOMPLETE',cleanupComplete:true},RELEASED);await h.advance(5);
  assert.deepEqual(h.pings.map(x=>x.phase),['pressed','listening']);assert.equal(h.gets.length,0);
  h.set({allowed:true});const q=h.voice.begin();h.voice.close();await h.advance(100);
  assert.equal(h.voice.begin(),null);h.voice.listening(q);h.voice.refused('BUSY');await h.advance(100);
  assert.equal(h.pings.length,2);assert.deepEqual(h.voice.status(),{closed:true,presses:0,requests:0,timers:0,children:0});groups++;
 }
 // An identity that cannot load sends no ping; the fixed toasts still appear ("other").
 {
  const h=harness({identityFails:true});const p=h.voice.begin();await h.advance(1);h.voice.listening(p);
  h.voice.settle(p,{reason:'PCM_INCOMPLETE',cleanupComplete:true},RELEASED);await h.advance(5);
  assert.equal(h.wire.length,0);assert.deepEqual(h.toasts,['Listening...','Hold the microphone button while speaking']);groups++;
 }
 // Toast transport: the pinned luna-send, default identity, closed text, bounded and reaped.
 {
  const spawned=[];let verified=0,pinBad=false;
  const spawn=(exe,args,options)=>{const child=new EventEmitter();child.killed=[];child.kill=s=>{child.killed.push(s);};spawned.push({exe,args,options,child});return child;};
  const h=harness({deps:{toast:undefined,spawn,fs:{},verifyCli(io,c){assert.equal(c,crypto);verified++;if(pinBad)throw Error('CLI_PIN');}}});
  const p=h.voice.begin();await h.advance(1);h.voice.listening(p);await h.advance(1);
  assert.equal(spawned.length,1);assert.equal(spawned[0].exe,'/usr/bin/luna-send');
  assert.deepEqual(spawned[0].args,['-n','1','luna://com.webos.notification/createToast','{"sourceId":"com.webos.app.home","message":"Listening..."}']);
  assert.deepEqual(spawned[0].options,{cwd:'/',stdio:'ignore',env:{PATH:'/usr/bin:/bin',LANG:'C'}});
  spawned[0].child.emit('exit',0);assert.deepEqual(spawned[0].child.killed,[]);
  h.voice.released(p,10);h.voice.refused('BUSY');await h.advance(1);assert.equal(spawned.length,3);
  h.voice.settle(p,{reason:'PCM_INCOMPLETE',cleanupComplete:true},RELEASED);await h.advance(1);assert.equal(spawned.length,3,'at most two live toasts');
  await h.advance(1990);assert.deepEqual(spawned[1].child.killed,[]);await h.advance(10);assert.deepEqual(spawned[1].child.killed,['SIGKILL']);assert.deepEqual(spawned[2].child.killed,['SIGKILL']);
  spawned[1].child.emit('exit',null);spawned[2].child.emit('error',Error('x'));pinBad=true;
  const q=h.voice.begin();await h.advance(1);h.voice.listening(q);await h.advance(5);assert.equal(spawned.length,3);assert.equal(verified,4);
  pinBad=false;h.voice.released(q,10);await h.advance(5);assert.equal(spawned.length,4);assert.equal(verified,5);h.voice.close();assert.deepEqual(spawned[3].child.killed,['SIGKILL']);
  assert.equal(h.voice.status().timers,0);groups++;
 }
 // Configuration is validated; nothing is created for a bad one.
 for(const change of [c=>{c.voiceKey='A'.repeat(64);},c=>{c.sparkIp='spark.local';},c=>{c.httpPort=80;},c=>{delete c.allowed;}]){
  const c={sparkIp:SPARK,httpPort:4181,voiceKey:VK,allowed:()=>true};change(c);assert.throws(()=>V.create(c,{identity:{load(){}}}));
 }
 assert.throws(()=>V.create({sparkIp:SPARK,httpPort:4181,voiceKey:VK,allowed:()=>true},{}));groups++;
 // End to end through owner_service: a quick tap, then a released command whose result
 // Spark answers late; capture replies are exactly those of an owner without feedback.
 {
  const h=harness({receipts:[id=>row(id,{done:false}),id=>row(id,{toast:'Muted'})]});
  let now=0;const jobs=[],handle=new EventEmitter();handle.registerMethod=()=>{};
  const service=bind(handle,{getTlsConfig(){},isActive:()=>true,provider:{},feedback:h.voice},{now:()=>now,setTimeout:()=>0,clearTimeout(){},start(cfg){
   const ready=deferred(),closed=deferred();const job={cfg,ready:ready.promise,closed:closed.promise,go:()=>ready.resolve(),end:r=>closed.resolve(r),
    release(){return closed.promise;},cancel(reason){ready.reject(Error('x'));closed.resolve({reason,cleanupComplete:true});return closed.promise;}};jobs.push(job);return job;}});
  const ask=(method,body)=>{const out=[];handle.emit('request',{category:()=>'/capture',method:()=>method,senderServiceName:()=>'com.webos.starfish.keyfilters',
   sender:()=>':peer.1',payload:()=>JSON.stringify(Object.assign({issuedAt:now},body)),respond:raw=>out.push(JSON.parse(raw))});return out;};
  ask('begin',{request:1,deviceId:'physical'});ask('cancel',{request:1});await h.advance(5);
  assert.deepEqual(h.toasts,['Hold the microphone button while speaking']);
  now=4000;await h.advance(4000);const begun=ask('begin',{request:2,deviceId:'physical'});jobs[1].go();await h.advance(1);assert.equal(begun[0].providerReady,true);
  jobs[1].cfg.onAudio();await h.advance(1);now=4000+V.HOLD_MS+100;assert.equal(ask('release',{request:2})[0].returnValue,true);
  jobs[1].end({reason:'released',cleanupComplete:true,providerStarted:true,outcome:{state:'accepted',code:'AGENT_CALL_ADMITTED'},actionAcknowledged:true});
  await h.advance(1000);
  assert.deepEqual(h.toasts,['Hold the microphone button while speaking','Listening...','Processing voice command...','Muted']);
  assert.deepEqual(h.pings.map(x=>x.phase+':'+(x.code||'')+':'+x.cut),['pressed::false','fault:OWNER_KEY_RELEASED:false','pressed::false','listening::false','released::true']);
  assert.equal(h.pings[3].deadlineMs,V.HOLD_MS-1);assert.equal(h.gets.length,2);
  for(const t of h.toasts)assert(CLOSED.has(t)||t==='Muted');
  await service.close();h.voice.close();groups++;
 }
 return groups;
}
// ---- Owner latch: a pre-spawn refusal never latches, a late clean receipt clears, Spark is told ----
const sessions=require('./session'),native=require('./supervised_provider');
const FINE=Object.freeze({cleanExit:true,exited:true,nativeStartAccepted:true,nativeStopAccepted:true,nativeStopFailed:false,nativeNeverStarted:false,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}});
// What session.js closes with when its provider's receipt had not arrived yet.
const STUCK=Object.freeze({reason:'PROVIDER_REAP_UNQUALIFIED',providerStarted:true,providerReaped:false,cleanupComplete:false,nativeStartAccepted:false,nativeStopAccepted:false,
 nativeStopFailed:true,networkSealInstalled:false,pcmBytes:6400,outcome:null,actionAcknowledged:false});
const LATCH_GOLDEN=['{"boot":"00000000000000b1","facts":41,"mac":"f0d63f5dbbab1f18f9de00ee76ff1ae8726e22a65a34ed8feb69bf9ec0c714d3","reason":"PROVIDER_REAP_UNQUALIFIED","schema":1,"seq":1,"state":"latched"}',
 '{"boot":"00000000000000b1","facts":95,"mac":"1d991683bde74c35eb51617151c36683a37649c28714da863003bf473b1765db","reason":"PROVIDER_REAP_UNQUALIFIED","schema":1,"seq":2,"state":"cleared"}'];
// An owner over fake jobs that also expose `settled`, as session.js now does.
function lateOwner(feedback,settles=true){
 let now=0;const jobs=[],handle=new EventEmitter();handle.registerMethod=()=>{};
 const service=bind(handle,{getTlsConfig(){},isActive:()=>true,provider:{},feedback},{now:()=>now,setTimeout:()=>0,clearTimeout(){},start(cfg){
  const ready=deferred(),closed=deferred(),settled=deferred();
  const job={cfg,ready:ready.promise,closed:closed.promise,go:()=>ready.resolve(),end:r=>closed.resolve(r),late:r=>settled.resolve(r),
   release(){return closed.promise;},cancel(reason){ready.reject(Error('x'));closed.resolve({reason,cleanupComplete:true,providerStarted:false});return closed.promise;}};
  if(settles)job.settled=settled.promise;jobs.push(job);return job;}});
 const ask=(method,body)=>{const out=[];handle.emit('request',{category:()=>'/capture',method:()=>method,senderServiceName:()=>'com.webos.starfish.keyfilters',
  sender:()=>':peer.1',payload:()=>JSON.stringify(Object.assign({issuedAt:now},body)),respond:raw=>out.push(JSON.parse(raw))});return out;};
 return {service,jobs,ask,tick:ms=>{now+=ms;}};
}
async function latchGroups(){
 let groups=0;
 // A clean provider receipt that lands after the close clears the latch; the press settles
 // as a lost capture (CAPTURE_LOST), not "Voice is off". Until then a fresh press is only busy.
 {
  const v=recorder(),o=lateOwner(v);o.ask('begin',{request:1,deviceId:'physical'});o.jobs[0].go();await turn();o.jobs[0].cfg.onAudio();await turn();
  o.tick(1500);o.ask('release',{request:1});o.jobs[0].end(STUCK);await turn();
  assert.equal(o.service.status().state,'unavailable');assert.deepEqual(v.calls.filter(c=>c[0]==='latch'),[['latch','latched',STUCK]]);
  assert(!v.calls.some(c=>c[0]==='settle'));
  o.tick(500);assert.equal(o.ask('begin',{request:2,deviceId:'physical'})[0].returnValue,false);assert.equal(o.jobs.length,1);
  assert.deepEqual(v.calls.slice(-1),[['refused','BUSY']]);
  o.jobs[0].late(FINE);await turn();
  assert.equal(o.service.status().state,'idle');
  const cleared=v.calls.filter(c=>c[0]==='latch')[1],settle=v.calls.find(c=>c[0]==='settle');
  assert.deepEqual(cleared,['latch','cleared',{...STUCK,providerReaped:true,cleanupComplete:true,nativeStartAccepted:true,nativeStopAccepted:true,nativeStopFailed:false,networkSealInstalled:true}]);
  assert.deepEqual(settle,['settle',{press:1},cleared[2],{ready:true,heard:true,released:true,stuck:false,recovered:true}]);
  assert.deepEqual(V.classify(settle[2],settle[3]),{code:'CAPTURE_LOST'});
  o.tick(500);o.ask('begin',{request:3,deviceId:'physical'});assert.equal(o.jobs.length,2);await o.service.close();groups++;
 }
 // Anything short of the exact on-time clean test, or no receipt in the window, stays latched.
 for(const late of [null,{...FINE,nativeStopFailed:true},{...FINE,nativeStopFailed:undefined},{...FINE,networkSealInstalled:false},
  {...FINE,supervision:{allReaped:true,supervisionComplete:false}},{...FINE,supervision:null},{...FINE,nativeStopAccepted:false,cleanExit:false}]){
  const v=recorder(),o=lateOwner(v);o.ask('begin',{request:1,deviceId:'physical'});o.jobs[0].go();await turn();o.jobs[0].end(STUCK);await turn();
  o.jobs[0].late(late);await turn();
  assert.equal(o.service.status().state,'unavailable',JSON.stringify(late));
  assert.deepEqual(v.calls.filter(c=>c[0]==='latch'),[['latch','latched',STUCK]]);
  assert.deepEqual(v.calls.find(c=>c[0]==='settle'),['settle',{press:1},STUCK,{ready:true,heard:false,released:false,stuck:true}]);
  o.tick(500);assert.equal(o.ask('begin',{request:2,deviceId:'physical'})[0].returnValue,false);assert.equal(o.jobs.length,1);
  assert.deepEqual(v.calls.slice(-2),[['refused','STOP_UNCONFIRMED'],['latch','latched',STUCK]]);
  assert.equal((await o.service.cancelActive('TV_INACTIVE')).cleanupComplete,false);await o.service.close();
 }
 groups++;
 // On time is unchanged: a clean close never waits; a job without `settled` latches and settles at once.
 for(const settles of [true,false]){
  const v=recorder(),o=lateOwner(v,settles);o.ask('begin',{request:1,deviceId:'physical'});o.jobs[0].go();await turn();
  o.jobs[0].end({...STUCK,reason:'CAPTURE_CANCELLED',providerReaped:true,cleanupComplete:true,nativeStopFailed:false});await turn();
  assert.equal(o.service.status().state,'idle');assert.deepEqual(v.calls.map(c=>c[0]),['begin','settle']);assert.equal(v.calls[1][3].stuck,false);
  o.tick(500);o.ask('begin',{request:2,deviceId:'physical'});o.jobs[1].go();await turn();o.jobs[1].end(STUCK);await turn();
  assert.equal(o.service.status().state,'unavailable');
  assert.deepEqual(v.calls.slice(3).map(c=>c[0]),settles?['latch']:['latch','settle']);
  if(settles){o.jobs[1].late(null);await turn();assert.equal(v.calls[4][3].stuck,true);}
  await o.service.close();
 }
 groups++;
 // Through the real session.js: a stop confirmed late clears; never confirmed stays latched;
 // a launch refused before spawning never latches at all.
 for(const mode of ['late','absent','prespawn']){
  const h=harness(),timers=[];let deliver=null;
  const end=deferred(),transport={authenticatedTransportReady:true,availablePcmBytes:65536,closed:end.promise,watch(){return()=>{};},
   cancel(){this.authenticatedTransportReady=false;end.resolve({released:false,reason:'cancelled'});return end.promise;},close(){return this.cancel();}};
  const launch=mode==='prespawn'?(o,d)=>native.launch(o,d):(o,done)=>{deliver=()=>done(FINE);const c={stop(){},discard(){},async reap(){return null;}};setImmediate(()=>o.onReady(c));return c;};
  const handle=new EventEmitter();handle.registerMethod=()=>{};let now=0;
  const service=bind(handle,{getTlsConfig:()=>({}),isActive:()=>true,provider:{mode:'capture',device:'',backendProfile:1,maxMs:10000},feedback:h.voice},
   {now:()=>now,setTimeout:()=>0,clearTimeout(){},session:{connect:async()=>transport,launch,setTimeout:(fn,ms)=>{timers.push({fn,ms});return timers.length;},clearTimeout(){}}});
  const ask=(method,body)=>{const out=[];handle.emit('request',{category:()=>'/capture',method:()=>method,senderServiceName:()=>'com.webos.starfish.keyfilters',
   sender:()=>':peer.1',payload:()=>JSON.stringify(Object.assign({issuedAt:now},body)),respond:raw=>out.push(JSON.parse(raw))});return out;};
  const began=ask('begin',{request:1,deviceId:'physical'});await h.advance(5);await turn();await h.advance(5);
  if(mode==='prespawn'){
   assert.equal(began[0].returnValue,false);assert.equal(service.status().state,'idle');assert.equal(timers.length,0);
   assert.deepEqual(h.pings.map(x=>x.phase+':'+(x.code||'')),['pressed:','fault:START_UNCONFIRMED']);assert.equal(h.latches.length,0);
   now=500;ask('begin',{request:2,deviceId:'physical'});await h.advance(5);assert.equal(h.pings[2].phase,'pressed');
  }else{
   assert.equal(began[0].providerReady,true);now=200;ask('cancel',{request:1});await h.advance(5);await turn();await h.advance(5);
   assert.equal(service.status().state,'unavailable');assert.equal(timers.length,1);assert.equal(timers[0].ms,3000);
   assert.deepEqual(h.latches.map(x=>x.state+':'+x.reason+':'+x.facts),['latched:CAPTURE_CANCELLED:33']);assert.deepEqual(h.pings.map(x=>x.phase),['pressed']);
   if(mode==='late')deliver();else timers[0].fn();
   await h.advance(5);await turn();await h.advance(5);
   if(mode==='late'){
    assert.equal(service.status().state,'idle');assert.deepEqual(h.latches.map(x=>x.state+':'+x.reason+':'+x.facts),['latched:CAPTURE_CANCELLED:33','cleared:CAPTURE_CANCELLED:95']);
    assert.deepEqual(h.pings.map(x=>x.phase+':'+(x.code||'')),['pressed:','fault:CAPTURE_LOST']);assert.deepEqual(h.toasts,[V.FAULTS.CAPTURE_LOST]);
    await h.advance(V.LATCH_MS*3);assert.equal(h.latches.length,2);
   }else{
    assert.equal(service.status().state,'unavailable');assert.deepEqual(h.pings.map(x=>x.phase+':'+(x.code||'')),['pressed:','fault:STOP_UNCONFIRMED']);
    assert.deepEqual(h.toasts,[V.FAULTS.STOP_UNCONFIRMED]);deliver();await h.advance(5);assert.equal(service.status().state,'unavailable');
    await h.advance(V.LATCH_MS);assert.deepEqual(h.latches.map(x=>x.state),['latched','latched']);
   }
  }
  await service.close();h.voice.close();
 }
 groups++;
 // Wire: POST /<voiceKey>/owner-latch, closed keys, enums and a facts bitmask, MAC'd over its own
 // domain with the phase pings' key; byte for byte for the fixture key, sharing the (boot, seq) counter.
 {
  const h=harness({cert:GOLDEN.tvCertificateSha256,ids:['00000000000000b1','0123456789abcdef']});
  h.voice.latch('latched',STUCK);await h.advance(1);assert.equal(h.latches.length,1);
  const fixture={...STUCK,nativeStartAccepted:true};
  assert.deepEqual(V.latchRow('latched',fixture),{state:'latched',reason:'PROVIDER_REAP_UNQUALIFIED',facts:41});
  assert.equal(V.sign(V.deviceKey(VK,GOLDEN.tvCertificateSha256),JSON.parse(LATCH_GOLDEN[0].replace(/"mac":"[a-f0-9]{64}",/,'')),V.LATCH_DOMAIN),LATCH_GOLDEN[0]);
  for(const g of LATCH_GOLDEN)verifyLatch(g,GOLDEN.tvCertificateSha256);
  assert.equal(h.latches[0].facts,1+32);assert.equal(h.latches[0].seq,1);assert.equal(h.latches[0].boot,'00000000000000b1');
  const p=h.voice.begin();await h.advance(1);assert.equal(h.pings[0].seq,2);assert.equal(h.pings[0].boot,'00000000000000b1');
  h.voice.latch('cleared',{...fixture,providerReaped:true,cleanupComplete:true,nativeStopAccepted:true,nativeStopFailed:false,networkSealInstalled:true});await h.advance(1);
  assert.equal(h.latches[1].seq,3);assert.equal(h.latches[1].state,'cleared');assert.equal(h.latches[1].facts,95);
  const want=JSON.parse(LATCH_GOLDEN[1]);delete want.mac;const got=Object.assign({},h.latches[1]);delete got.at;delete got.text;assert.deepEqual(got,Object.assign(want,{seq:3}));
  // Closed reason allowlist; no receipt at all is OTHER with no facts.
  for(const [r,reason,facts] of [[null,'OTHER',0],[{reason:'released',cleanupComplete:true},'OTHER',4],[{reason:'turn it up'},'OTHER',0],[{reason:'TV_INACTIVE',providerStarted:'yes'},'TV_INACTIVE',0],
   [{reason:'UI_STOP_UNKNOWN',providerStarted:true,providerReaped:true,cleanupComplete:true,nativeStartAccepted:true,nativeStopAccepted:true,nativeStopFailed:true,networkSealInstalled:true},'UI_STOP_UNKNOWN',127]])
   assert.deepEqual(V.latchRow('latched',r),{state:'latched',reason,facts});
  assert.deepEqual(V.LATCH_FACTS,['providerStarted','providerReaped','cleanupComplete','nativeStartAccepted','nativeStopAccepted','nativeStopFailed','networkSealInstalled']);
  h.voice.latch('bogus',STUCK);await h.advance(1);assert.equal(h.latches.length,2);assert.deepEqual(h.toasts,[]);
  h.voice.settle(p,{reason:'TV_INACTIVE',cleanupComplete:true},RELEASED);h.voice.close();groups++;
 }
 // Rate: resent every 30 s while latched, at most LATCH_RESENDS times; cleared sends once and
 // stops; latching again re-arms the budget (and resends at once only after it ran out).
 {
  const h=harness();h.voice.latch('latched',STUCK);await h.advance(1);assert.equal(h.latches.length,1);
  await h.advance(V.LATCH_MS-2);assert.equal(h.latches.length,1);await h.advance(2);assert.equal(h.latches.length,2);
  h.voice.latch('latched',STUCK);await h.advance(1);assert.equal(h.latches.length,2,'same latch while armed: no extra send');
  await h.advance(V.LATCH_MS*(V.LATCH_RESENDS+5));assert.equal(h.latches.length,2+V.LATCH_RESENDS);assert.equal(h.voice.status().timers,0);
  assert(h.latches.every((x,i)=>i===0||x.seq>h.latches[i-1].seq&&x.at-h.latches[i-1].at>=V.LATCH_MS-1));
  assert.equal(V.LATCH_MS,30000);assert(V.LATCH_RESENDS>0&&V.LATCH_RESENDS<=2880);
  h.voice.latch('latched',STUCK);await h.advance(1);assert.equal(h.latches.length,3+V.LATCH_RESENDS);await h.advance(V.LATCH_MS);assert.equal(h.latches.length,4+V.LATCH_RESENDS);
  h.voice.latch('cleared',{...STUCK,cleanupComplete:true});await h.advance(V.LATCH_MS*4);assert.equal(h.latches.length,5+V.LATCH_RESENDS);
  assert.equal(h.latches[h.latches.length-1].state,'cleared');assert.equal(h.voice.status().timers,0);
  // A different latch replaces the row and sends at once.
  h.voice.latch('latched',STUCK);await h.advance(1);h.voice.latch('latched',null);await h.advance(1);
  assert.deepEqual(h.latches.slice(-2).map(x=>x.reason),['PROVIDER_REAP_UNQUALIFIED','OTHER']);
  h.voice.close();assert.deepEqual(h.voice.status(),{closed:true,presses:0,requests:0,timers:0,children:0});
  h.voice.latch('latched',STUCK);await h.advance(V.LATCH_MS*2);assert.equal(h.latches.length,7+V.LATCH_RESENDS);groups++;
 }
 // An older Spark (404), an error, a hang or no identity: harmless, bounded, nothing shown.
 for(const latch of [()=>({status:404,body:{}}),()=>({status:500,body:{}}),()=>'error',()=>'hang',()=>({status:200,body:{ok:true,extra:'x'.repeat(400)}})]){
  const h=harness({latch});h.voice.latch('latched',STUCK);await h.advance(1);assert.equal(h.latches.length,1);
  await h.advance(V.PING_MS);assert.equal(h.voice.status().requests,0);assert.deepEqual(h.toasts,[]);
  await h.advance(V.LATCH_MS);assert.equal(h.latches.length,2);
  const p=h.voice.begin();await h.advance(1);h.voice.listening(p);await h.advance(5);assert.deepEqual(h.toasts,['Listening...']);h.voice.close();
 }
 {
  const h=harness({identityFails:true});h.voice.latch('latched',STUCK);await h.advance(V.LATCH_MS+5);assert.equal(h.latches.length,0);h.voice.close();
 }
 groups++;
 // A late recovery classifies as an ordinary lost capture; standby and shutdown still drop.
 const R=(reason,extra={})=>Object.assign({reason,cleanupComplete:true},extra);
 for(const [receipt,facts,want] of [[R('PROVIDER_REAP_UNQUALIFIED'),{...RELEASED,recovered:true},{code:'CAPTURE_LOST'}],
  [R('CAPTURE_CANCELLED'),{ready:true,heard:false,released:false,stuck:false,recovered:true},{code:'CAPTURE_LOST'}],
  [R('CAPTURE_CANCELLED'),{ready:false,recovered:true},{code:'CAPTURE_LOST'}],[R('released'),{...RELEASED,recovered:true},{code:'CAPTURE_LOST'}],
  [R('TV_INACTIVE'),{...RELEASED,recovered:true},{drop:true}],[R('POWER_UNKNOWN'),{...RELEASED,recovered:true},{drop:true}],[R('SUPERSEDED'),{recovered:true},{drop:true}]])
  assert.deepEqual(V.classify(receipt,facts),want,JSON.stringify([receipt,facts]));
 groups++;
 return groups;
}
(async()=>{
 let f=fixture();
 for(let i=0;i<80;i++){f.setTime(i*11000);assert.equal(f.request('cancel',{request:1},':peer.'+i)[0].returnValue,true);}assert.equal(f.jobs.length,0);assert.equal((await f.service.close()).cleanupComplete,true);groups++;
 f=fixture();f.setTime(3000);assert.equal(f.request('begin',{request:1,deviceId:'physical',issuedAt:0})[0].returnValue,false);assert.equal(f.jobs.length,0);await f.service.close();groups++;
 f=fixture();let replies=f.request('begin',{request:1,deviceId:'physical'});f.jobs[0].go();await turn();assert.equal(replies[0].providerReady,true);assert.equal(f.service.status().state,'provider-ready');
 assert.equal(f.request('cancel',{request:1},':wrong-peer')[0].returnValue,false);assert.equal(f.service.status().state,'provider-ready');await f.service.close();groups++;
 f=fixture();replies=f.request('begin',{request:1,deviceId:'physical'});f.setActive(false);f.jobs[0].go();await turn();assert.equal(replies[0].returnValue,false);assert.equal((await f.service.close()).cleanupComplete,true);groups++;
 f=fixture();f.request('begin',{request:1,deviceId:'physical'});f.setCleanup(false);const r=await f.service.cancelActive('POWER_UNKNOWN');assert.equal(r.cleanupComplete,false);await turn();assert.equal(f.service.status().state,'unavailable');assert.equal(f.request('begin',{request:2,deviceId:'physical'})[0].returnValue,false);assert.equal((await f.service.close()).cleanupComplete,false);groups++;
 f=fixture();const a=f.service.close(),b=f.service.close();assert.strictEqual(a,b);assert.equal((await a).cleanupComplete,true);groups++;
 // A held press remains finite, independently of the QML key-up path.
 f=fixture();f.request('begin',{request:1,deviceId:'physical'});f.jobs[0].go();await turn();f.advance(PRESS_MS-1);assert.deepEqual(f.jobs[0].cancels,[]);f.advance(PRESS_MS);assert.deepEqual(f.jobs[0].cancels,['CAPTURE_DEADLINE']);await turn();await f.service.close();groups++;
 assert.equal(PRESS_MS,13000);assert.equal(UI_PRESS_MS,10000);
 // Physical release ends only the capture lease. Keep the same actual owner
 // while its bounded transport result waits through a cold browser launch,
 // unless a newer physical press arrives and supersedes it.
 f=fixture();f.setHold(true);f.request('begin',{request:1,deviceId:'physical'});f.jobs[0].go();await turn();f.advance(3000);
 assert.equal(f.request('release',{request:1})[0].returnValue,true);assert.equal(f.jobs[0].releases,1);assert.equal(f.timers.size,0);assert.equal(f.service.status().state,'finishing');
 f.advance(109500);assert.deepEqual(f.jobs[0].cancels,[]);assert.equal(f.service.status().state,'finishing');
 const r2=f.request('begin',{request:2,deviceId:'physical'});assert.deepEqual(f.jobs[0].cancels,['SUPERSEDED']);assert.equal(f.jobs.length,2);
 f.jobs[1].go();await turn();assert.equal(r2[0].providerReady,true);assert.equal(f.service.status().state,'provider-ready');
 assert.equal(f.request('release',{request:2})[0].returnValue,true);f.jobs[1].complete();await turn();assert.equal(f.service.status().state,'idle');
 f.request('begin',{request:3,deviceId:'physical'});assert.equal(f.jobs.length,3);await f.service.close();groups++;
 // Power loss and shutdown still cancel a released result wait immediately.
 for(const reason of ['TV_INACTIVE','POWER_UNKNOWN','CLIENT_CLOSED']){
  f=fixture();f.setHold(true);f.request('begin',{request:1,deviceId:'physical'});f.jobs[0].go();await turn();f.request('release',{request:1});
  const receipt=reason==='CLIENT_CLOSED'?await f.service.close():await f.service.cancelActive(reason);
  assert.equal(receipt.cleanupComplete,true);assert.deepEqual(f.jobs[0].cancels,[reason]);assert.equal(f.timers.size,0);await f.service.close();
 }groups++;
 // Zero-compositor-restart keyfilter bridge identity is accepted; other surfacemanager names are rejected.
 f=fixture();
 for(const badCaller of ['com.webos.surfacemanager','com.webos.surfacemanager.other','com.webos.surfacemanager.r2d2_','com.webos.surfacemanager.r2d2_abc'])
  assert.equal(f.request('begin',{request:1,deviceId:'physical'},':peer.1',badCaller)[0].returnValue,false);
 assert.equal(f.jobs.length,0);
 const bridgeReply=f.request('begin',{request:1,deviceId:'physical'},':peer.2','com.webos.surfacemanager.r2d2_1792518903806');
 f.jobs[0].go();await turn();assert.equal(bridgeReply[0].providerReady,true);
 assert.equal(f.request('release',{request:1},':peer.2','com.webos.surfacemanager.r2d2_1792518903806')[0].returnValue,true);
 await f.service.close();groups++;
 groups+=await ownerFeedbackGroups();
 groups+=await feedbackGroups();
 groups+=await latchGroups();
 console.log(JSON.stringify({groups,nativeCalls:0,modelCalls:0}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
