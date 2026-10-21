'use strict';
// On-screen voice feedback for physical presses, owned by the root capture
// owner. Enums and closed text only: MAC-signed phase pings to Spark, fixed
// toasts only when no R2D2 surface shows the press, and Spark's closed copy
// for results. Capture never waits on anything here: it cannot delay, retry
// or fail START, and an old, slow or absent Spark only leaves the fixed text.
const crypto=require('crypto'),net=require('net');
const PHASE_DOMAIN='R2D2-voice-phase-v1\0',LATCH_DOMAIN='R2D2-owner-latch-v1\0',DEVICE_DOMAIN='R2D2-RESIDENT-PTT-v1\0';
const TOAST_URI='luna://com.webos.notification/createToast',CLI='/usr/bin/luna-send';
const SURFACES=['home','viewer','other'];
// controller.js ends a held press 9.2 s after begin (HOLD_MS, releaseAt); listening counts
// down to it. DEADLINE_MAX_MS is Spark's parse_ping bound on deadlineMs (voice_status.py).
const HOLD_MS=9200,DEADLINE_MAX_MS=10000,HINT_MS=3000,RECEIPT_MS=15000,PING_MS=300,RECEIPT_GET_MS=1700,REPOLL_MS=250,TOAST_KILL_MS=2000,TOASTS=2;
// Closed TV-side faults, each with the fixed toast used when no R2D2 surface shows it.
const FAULTS=Object.freeze({OWNER_KEY_RELEASED:'Hold the microphone button while speaking',
 START_UNCONFIRMED:'Remote mic did not start. Try again',CAPTURE_LOST:'Voice command unavailable',
 STOP_UNCONFIRMED:'Voice is off. Restart the TV to use voice',NO_SPEECH:'Hold the microphone button while speaking',
 BUSY:'Voice is busy, try again in a moment',TRANSPORT_FAILED:'Voice command unavailable',
 RESULT_UNCONFIRMED:'Sent, but not confirmed. Check the TV'});
const NOTICES=Object.freeze({listening:'Listening...',released:'Processing voice command...'});
// Fixed result text for a Spark without /voice-receipt, keyed by its stream outcome code.
const SENT='Voice command sent';
const CODES=Object.freeze({VOICE_WARMING_UP:'Voice model warming up - try again in a moment',
 AUDIO_EMPTY_OR_SHORT:'Hold the microphone button while speaking',AGENT_PAUSED:'Voice control is paused. Restart Spark to resume',
 RATE_LIMITED:'Too many requests, wait a moment',VOICE_BUSY:'Voice is busy, try again in a moment',
 CONTROLLER_BUSY:'Voice is busy, try again in a moment',RECOGNITION_ONLY_NO_ACTION:'Voice heard (no browser navigation matched)'});
// session.js close reasons. DROP: standby, shutdown or a newer press; nothing is shown.
const DROP=new Set(['TV_INACTIVE','POWER_UNKNOWN','CLIENT_CLOSED','SUPERSEDED']);
// The keyfilter ended the press itself (key-up or a QML-side failure) before a valid release.
const GATE=new Set(['CAPTURE_CANCELLED','RELEASE_BEFORE_READY']);
const START=new Set(['PROVIDER_START_TIMEOUT','PROVIDER_START_FAILED','PROVIDER_NOT_ADMITTED']);
// Released, but the stream was cancelled locally: nothing reached Spark's agent.
const KEPT=new Set(['PROVIDER_REAP_UNQUALIFIED','CAPTURE_CLEANUP_UNKNOWN']);
// Spark (or the network) ended an authenticated stream before release, e.g. a warm refusal.
const STREAM=new Set(['TRANSPORT_CLOSED','TRANSPORT_FAILED']);
// Owner latch reports: the close reasons this TV itself produces (anything else is
// OTHER) and the receipt facts, bit 0 first. Resent every 30 s while latched, bounded.
const LATCH_REASONS=new Set(['CAPTURE_CANCELLED','CAPTURE_CLEANUP_UNKNOWN','CAPTURE_DEADLINE','CLIENT_CLOSED','PARENT_CONTROL_LOST',
 'PCM_BACKPRESSURE','PCM_INCOMPLETE','PCM_NOT_ADMITTED','POWER_UNKNOWN','PROVIDER_CLOSED_UNQUALIFIED','PROVIDER_NOT_ADMITTED',
 'PROVIDER_REAP_UNQUALIFIED','PROVIDER_START_FAILED','PROVIDER_START_TIMEOUT','RELEASE_BEFORE_READY','SUPERSEDED',
 'TRANSPORT_CLEANUP_UNKNOWN','TRANSPORT_CLOSED','TRANSPORT_FAILED','TRANSPORT_START_FAILED','TV_INACTIVE',
 'UI_NATIVE_CLEANUP_UNKNOWN','UI_NATIVE_UNKNOWN','UI_PROVIDER_UNKNOWN','UI_STOP_UNKNOWN','VOICE_CANCELLED']);
const LATCH_FACTS=['providerStarted','providerReaped','cleanupComplete','nativeStartAccepted','nativeStopAccepted','nativeStopFailed','networkSealInstalled'];
const LATCH_MS=30000,LATCH_RESENDS=120;
const own=(o,k)=>Object.prototype.hasOwnProperty.call(o,k);
const hex=(v,n)=>typeof v==='string'&&v.length===n&&/^[a-f0-9]+$/.test(v);
function printable(text){return typeof text==='string'&&/^[\x20-\x7e]{1,60}$/.test(text);}
// Canonical JSON with sorted keys, then the MAC over the domain plus that canonical text.
function sign(key,value,domain=PHASE_DOMAIN){
 const names=Object.keys(value).sort();
 const mac=crypto.createHmac('sha256',key).update(domain,'ascii').update(JSON.stringify(value,names),'ascii').digest('hex');
 return JSON.stringify(Object.assign({},value,{mac}),names.concat('mac').sort());
}
// The resident PCM device key (main.js tlsConfig derives the same bytes).
function deviceKey(voiceKey,certificateSha256){
 if(!hex(voiceKey,64)||!hex(certificateSha256,64))throw Error('VOICE_FEEDBACK_KEY');
 return crypto.createHash('sha256').update(Buffer.from(DEVICE_DOMAIN,'ascii'))
  .update(Buffer.from(voiceKey,'hex')).update(Buffer.from(certificateSha256,'hex')).digest();
}
// One closed receipt -> the closed latch row: state, reason enum and a facts bitmask.
function latchRow(state,receipt){
 const reason=receipt&&typeof receipt.reason==='string'&&LATCH_REASONS.has(receipt.reason)?receipt.reason:'OTHER';
 let facts=0;LATCH_FACTS.forEach((name,bit)=>{if(receipt&&receipt[name]===true)facts|=1<<bit;});
 return Object.freeze({state,reason,facts});
}
// One closed session receipt -> nothing (drop), a TV fault code, or Spark's result.
function classify(receipt,facts){
 const reason=receipt&&typeof receipt.reason==='string'?receipt.reason:null;
 // Its stop was confirmed only after the press closed: the latch cleared, the capture was lost.
 if(facts.recovered)return reason&&DROP.has(reason)?{drop:true}:{code:'CAPTURE_LOST'};
 // After release Spark may already be acting: one unreadable power reply (the owner
 // cancels the stream) must not drop that result. Standby still shows nothing.
 if(reason==='POWER_UNKNOWN'&&facts.released&&!facts.stuck)return {result:true,dispatched:true};
 if(reason&&DROP.has(reason))return {drop:true};
 if(facts.stuck)return {code:'STOP_UNCONFIRMED'};
 if(!receipt)return {code:'CAPTURE_LOST'};
 if(reason==='PCM_INCOMPLETE')return {code:'NO_SPEECH'};
 if(facts.released)return KEPT.has(reason)?{code:'CAPTURE_LOST'}:{result:true,dispatched:true};
 // Before the provider was ready the keyfilter cannot have tried START: a quick tap.
 if(reason&&GATE.has(reason))return facts.ready?{code:facts.heard?'CAPTURE_LOST':'START_UNCONFIRMED'}:{code:'OWNER_KEY_RELEASED',hint:true};
 if(reason==='TRANSPORT_START_FAILED')return {code:'TRANSPORT_FAILED'};
 if(reason&&START.has(reason))return {code:'START_UNCONFIRMED'};
 if(reason&&STREAM.has(reason)&&receipt.providerStarted===true)return {result:true,dispatched:false};
 return {code:'CAPTURE_LOST'};
}
// The fixed mapping an older Spark (no /voice-receipt) keeps, from the stream outcome.
function fallback(receipt,dispatched){
 const outcome=receipt&&receipt.outcome&&typeof receipt.outcome==='object'?receipt.outcome:null,code=outcome&&outcome.code;
 if(receipt&&receipt.actionAcknowledged===true)return SENT;
 if(typeof code==='string'&&own(CODES,code))return CODES[code];
 // After release the command may have run; a missing confirmation is uncertainty.
 if(dispatched&&(!outcome||outcome.state==='unknown'))return FAULTS.RESULT_UNCONFIRMED;
 return FAULTS.CAPTURE_LOST;
}
function receiptRow(raw,press){
 if(!raw||typeof raw!=='object'||Array.isArray(raw)||Object.keys(raw).sort().join()!=='done,press,schema,shown,toast'||raw.schema!==1||
  raw.press!==press||typeof raw.done!=='boolean'||typeof raw.shown!=='boolean'||!(raw.toast===null||printable(raw.toast)))return undefined;
 return raw;
}
function create(config,dependencies={}){
 if(!config||!hex(config.voiceKey,64)||net.isIP(config.sparkIp)!==4||!Number.isInteger(config.httpPort)||config.httpPort<1024||config.httpPort>65535||
  typeof config.allowed!=='function'||!dependencies.identity||typeof dependencies.identity.load!=='function')throw Error('VOICE_FEEDBACK_CONFIG');
 const identity=dependencies.identity,http=dependencies.http||require('http'),random=dependencies.randomBytes||crypto.randomBytes;
 const later=dependencies.setTimeout||setTimeout,clear=dependencies.clearTimeout||clearTimeout;
 const now=dependencies.now||(()=>{const t=process.hrtime();return t[0]*1000+t[1]/1000000;});
 let closed=false,boot=null,seq=0,lastHint=-Infinity,latest=null,latched=null,latchTimer=null,latchLeft=0;
 const presses=new Set(),requests=new Set(),timers=new Set(),children=new Set();
 function timer(fn,ms){const id=later(()=>{timers.delete(id);fn();},ms);timers.add(id);return id;}
 function untimer(id){clear(id);timers.delete(id);}
 function allowed(){try{return !closed&&config.allowed()===true;}catch(_){return false;}}
 // The toast transport the legacy resident used: luna-send's default identity, because
 // the owner's LS2 role allows only bluetooth2/tvpower. Same pinned CLI as power_reader.
 function luna(text){
  if(children.size>=TOASTS)return false;
  (dependencies.verifyCli||require('./power_reader').verifyCli)(dependencies.fs||require('fs'),crypto);
  const child=(dependencies.spawn||require('child_process').spawn)(CLI,['-n','1',TOAST_URI,JSON.stringify({sourceId:'com.webos.app.home',message:text})],
   {cwd:'/',stdio:'ignore',env:{PATH:'/usr/bin:/bin',LANG:'C'}});
  children.add(child);
  const kill=timer(()=>{try{child.kill('SIGKILL');}catch(_){}},TOAST_KILL_MS);
  const done=()=>{untimer(kill);children.delete(child);};
  child.once('error',done);child.once('exit',done);
  return true;
 }
 function toast(text){
  if(!allowed()||!printable(text))return false;
  try{return (dependencies.toast||luna)(text)!==false;}catch(_){return false;}
 }
 // One bounded plain-HTTP exchange with Spark: {status,value}, else null. Never rejects.
 function exchange(path,method,headers,body,limit,timeout,press){
  if(closed)return Promise.resolve(null);
  return new Promise(resolve=>{
   let req=null,res=null,done=false,size=0,chunks=[];
   const finish=value=>{
    if(done)return;done=true;untimer(id);requests.delete(cancel);if(press)press.cancels.delete(cancel);
    if(req)try{req.destroy();}catch(_){}if(res)try{res.destroy();}catch(_){}
    for(const chunk of chunks)chunk.fill(0);chunks=[];resolve(value);
   };
   const cancel=()=>finish(null),id=timer(cancel,timeout);requests.add(cancel);if(press)press.cancels.add(cancel);
   try{
    req=http.request({host:config.sparkIp,port:config.httpPort,path:'/'+config.voiceKey+path,method,
     headers:Object.assign({Host:config.sparkIp+':'+config.httpPort},headers)},response=>{
     res=response;if(done){try{res.destroy();}catch(_){}return;}
     res.on('error',cancel);
     res.on('data',part=>{if(done)return;size+=part.length;if(size>limit)return cancel();chunks.push(Buffer.from(part));});
     res.on('end',()=>{
      let value=null,data=null;
      try{if(res.statusCode===200){data=Buffer.concat(chunks);value=JSON.parse(data.toString('utf8'));}}catch(_){value=null;}
      finally{if(data)data.fill(0);}
      finish({status:res.statusCode,value});
     });
     res.on('close',cancel);
    });
    // Once a response exists its own end/close decides (Node may close the request first).
    req.on('error',cancel);req.on('close',()=>{if(!res)cancel();});req.end(body);
   }catch(_){cancel();}
  });
 }
 // A press has a random id and one MAC key, loaded after the current turn so identity
 // parsing never sits in front of the capture start. finish() wipes it after its last ping.
 function open(){
  const press={id:null,key:null,at:now(),ready:null,toasts:Promise.resolve(),dropped:false,finished:false,cancels:new Set()};
  try{const id=random(8).toString('hex');if(hex(id,16))press.id=id;}catch(_){}
  press.ready=new Promise(resolve=>timer(resolve,0)).then(()=>{
   if(closed||!press.id)return;
   try{press.key=deviceKey(config.voiceKey,identity.load().certificateSha256);}catch(_){press.key=null;}
  }).catch(()=>{});
  presses.add(press);
  if(presses.size>16)for(const old of presses){if(old===press||presses.size<=16)break;drop(old);presses.delete(old);}
  return press;
 }
 function finish(press){
  if(press.finished)return;press.finished=true;
  // Queued after every ping already bound to this press, so each has its MAC first.
  void press.ready.then(()=>{if(press.key)press.key.fill(0);press.key=null;presses.delete(press);});
 }
 function live(press){return !!press&&!press.dropped&&!closed;}
 function drop(press){
  if(!press)return;press.dropped=true;
  for(const cancel of [...press.cancels])cancel();press.cancels.clear();
  if(latest===press)latest=null;finish(press);
 }
 // One (boot, seq) counter for phase pings and latch reports: strictly increasing per boot.
 function stamp(){
  if(!boot||seq>=2**31){const next=random(8).toString('hex');if(!hex(next,16))return null;boot=next;seq=0;}
  return {boot,seq:++seq};
 }
 // Fire-and-forget, enums only: resolves to the R2D2 surface showing voice, 'other' on any doubt.
 function phase(press,name,code=null,deadlineMs=null,cut=false){
  if(closed||!press.id)return Promise.resolve('other');
  let value;
  try{
   const at=stamp();if(!at)return Promise.resolve('other');
   value={schema:1,boot:at.boot,seq:at.seq,press:press.id,phase:name,code,deadlineMs,cut};
  }catch(_){return Promise.resolve('other');}
  return press.ready.then(()=>{
   if(!press.key||closed)return 'other';
   const body=Buffer.from(sign(press.key,value),'ascii');
   if(body.length>256)return 'other';
   return exchange('/voice-phase','POST',{'Content-Type':'application/json','Content-Length':body.length},body,256,PING_MS).then(reply=>{
    const raw=reply&&reply.status===200?reply.value:null;
    return raw&&typeof raw==='object'&&!Array.isArray(raw)&&Object.keys(raw).sort().join()==='ok,surface'&&raw.ok===true&&
     SURFACES.includes(raw.surface)?raw.surface:'other';
   });
  }).catch(()=>'other');
 }
 // A toast only when no R2D2 surface renders this press, in the press's own order.
 function notice(press,surface,text){
  press.toasts=press.toasts.then(()=>surface).then(where=>{if(where==='other'&&live(press))toast(text);}).catch(()=>{});
  return press.toasts;
 }
 function show(press,text){
  press.toasts=press.toasts.then(()=>{if(text!==null&&live(press))toast(text);}).catch(()=>{});
  return press.toasts;
 }
 // hint: feedback for a press that granted nothing; its toast at most once every 3 s.
 function fault(press,code,hint=false){
  if(!own(FAULTS,code))code='CAPTURE_LOST';
  const surface=phase(press,'fault',code);
  if(hint){const t=now();if(t-lastHint<HINT_MS)return press.toasts;lastHint=t;}
  return notice(press,surface,FAULTS[code]);
 }
 function pause(ms,press){
  return new Promise(resolve=>{
   const wake=()=>{untimer(id);press.cancels.delete(wake);resolve();};
   const id=timer(wake,ms);press.cancels.add(wake);
  });
 }
 // Spark's closed copy for a finished press. A Spark that only admitted the command
 // answers done:false; keep asking for up to 15 s. Any 4xx (an older Spark has no
 // route) keeps the fixed mapping at once; never done is this TV's own uncertainty.
 async function result(press,receipt,dispatched){
  const deadline=now()+RECEIPT_MS;
  while(live(press)&&press.id){
   const began=now();
   const reply=await exchange('/voice-receipt/'+press.id,'GET',{},undefined,256,RECEIPT_GET_MS,press);
   if(!live(press))return;
   if(reply&&reply.status===200){
    const row=receiptRow(reply.value,press.id);
    if(row===undefined)return show(press,fallback(receipt,dispatched));
    if(row.done)return show(press,row.shown?null:row.toast);
   }else if(reply&&reply.status>=400&&reply.status<500)return show(press,fallback(receipt,dispatched));
   if(now()>=deadline)break;
   await pause(Math.max(0,REPOLL_MS-(now()-began)),press);
  }
  if(!press.id&&live(press))return show(press,fallback(receipt,dispatched));
  if(live(press))return fault(press,dispatched?'RESULT_UNCONFIRMED':'TRANSPORT_FAILED');
 }
 // The owner accepted a physical press. Only the newest press may still toast.
 function begin(){
  if(closed)return null;
  if(latest)drop(latest);
  const press=open();latest=press;
  void phase(press,'pressed');
  return press;
 }
 // First admitted PCM: the remote's mic is really streaming.
 function listening(press){
  if(!live(press))return;
  const left=Math.max(0,Math.min(DEADLINE_MAX_MS,Math.floor(HOLD_MS-(now()-press.at))));
  notice(press,phase(press,'listening',null,left),NOTICES.listening);
 }
 // A valid release with audio. held: keyfilter-clock ms since begin; the gate's own
 // 9.2 s release is the hold limit ("cut").
 function released(press,held){
  if(!live(press))return;
  notice(press,phase(press,'released',null,null,Number.isFinite(held)&&held>=HOLD_MS-5),NOTICES.released);
 }
 function settle(press,receipt,facts){
  if(!press)return;
  if(!live(press)){finish(press);return;}
  const verdict=classify(receipt,facts||{});
  if(verdict.drop||!allowed()){drop(press);return;}
  const done=()=>{if(latest===press)latest=null;finish(press);};
  if(verdict.code){void fault(press,verdict.code,verdict.hint===true);done();return;}
  void result(press,receipt,verdict.dispatched).then(done,done);
 }
 // A fresh press the owner refused: its own press id, ping and toast at most once every 3 s.
 function refused(code){
  if(closed||!allowed()||!own(FAULTS,code))return;
  const t=now();if(t-lastHint<HINT_MS)return;lastHint=t;
  const press=open();void notice(press,phase(press,'fault',code),FAULTS[code]);finish(press);
 }
 // One signed latch report, fire-and-forget: any reply (an older Spark answers 404) is ignored.
 // The phase pings' device key, loaded after the current turn and wiped once signed.
 function report(row){
  if(closed)return;
  let value;
  try{const at=stamp();if(!at)return;value={schema:1,boot:at.boot,seq:at.seq,state:row.state,reason:row.reason,facts:row.facts};}catch(_){return;}
  void new Promise(resolve=>timer(resolve,0)).then(()=>{
   if(closed)return;
   let key=null,body;
   try{key=deviceKey(config.voiceKey,identity.load().certificateSha256);body=Buffer.from(sign(key,value,LATCH_DOMAIN),'ascii');}
   catch(_){return;}finally{if(key)key.fill(0);}
   if(body.length>256)return;
   return exchange('/owner-latch','POST',{'Content-Type':'application/json','Content-Length':body.length},body,256,PING_MS);
  }).catch(()=>{});
 }
 function resend(){
  latchTimer=null;if(closed||!latched||latchLeft<1)return;
  latchLeft--;report(latched);if(latchLeft>0)latchTimer=timer(resend,LATCH_MS);
 }
 // The owner latched (no new capture until a TV restart) or cleared that latch. While
 // latched the same row is resent every 30 s, at most 120 times; the owner latching
 // again (a refused press) re-arms that budget and resends at once only once it ran out.
 function latch(state,receipt){
  if(closed||state!=='latched'&&state!=='cleared')return;
  const row=latchRow(state,receipt);
  if(state==='latched'&&latched&&latched.reason===row.reason&&latched.facts===row.facts){
   latchLeft=LATCH_RESENDS;if(latchTimer===null)resend();return;
  }
  if(latchTimer!==null){untimer(latchTimer);latchTimer=null;}
  latched=state==='latched'?row:null;latchLeft=LATCH_RESENDS;
  report(row);
  if(latched)latchTimer=timer(resend,LATCH_MS);
 }
 function close(){
  if(closed)return;closed=true;latest=null;latched=null;latchTimer=null;
  for(const cancel of [...requests])cancel();
  for(const press of [...presses]){press.dropped=true;for(const cancel of [...press.cancels])cancel();press.cancels.clear();if(press.key){press.key.fill(0);press.key=null;}}
  presses.clear();
  for(const id of [...timers])untimer(id);
  for(const child of [...children])try{child.kill('SIGKILL');}catch(_){}
  children.clear();
 }
 function status(){return Object.freeze({closed,presses:presses.size,requests:requests.size,timers:timers.size,children:children.size});}
 return Object.freeze({begin,listening,released,settle,refused,latch,close,status});
}
module.exports={create,sign,deviceKey,classify,fallback,latchRow,printable,FAULTS,NOTICES,CODES,SENT,HOLD_MS,DEADLINE_MAX_MS,HINT_MS,RECEIPT_MS,PING_MS,PHASE_DOMAIN,
 LATCH_DOMAIN,LATCH_REASONS,LATCH_FACTS,LATCH_MS,LATCH_RESENDS};
