'use strict';
const assert=require('assert'),EventEmitter=require('events');
const Controller=require('./controller'),Owner=require('./owner_service'),Session=require('./session'),Legacy=require('./legacy_provider'),V=require('./voice_feedback');
const turn=()=>new Promise(r=>setImmediate(r));
async function flush(){for(let i=0;i<10;i++)await turn();}
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r;});return{promise,resolve};}
let count=0;async function test(name,fn){await fn();count++;}
function fixture(options={}){
 const handle=new EventEmitter(),methods=[];handle.registerMethod=(c,m)=>methods.push(c+'/'+m);
 let clock=0,next=1,active=true,launched=0,starts=0,stops=0,provider=null,hooks=null,done=null,receipt=null;
 const calls=[],chunks=[],secrets=[];let transport=null;
 function makeTransport(){
  const end=deferred();let ended=false,watch=null;
  transport={authenticatedTransportReady:true,availablePcmBytes:65536,closed:end.promise,
   watch(value){watch=value;return()=>{watch=null;};},trySendPcm(bytes){chunks.push(Buffer.from(bytes));return true;},
   release(){if(!ended){ended=true;this.authenticatedTransportReady=false;end.resolve({released:true,reason:'released',outcome:{state:'accepted',code:'BROWSER_NAVIGATION_ACCEPTED'},actionAcknowledged:true});}return end.promise;},
   cancel(){if(!ended){ended=true;this.authenticatedTransportReady=false;end.resolve({released:false,reason:'cancelled'});}return end.promise;},
   close(){return this.cancel();}};return transport;
 }
 const config={isActive:()=>active,getTlsConfig(){const v={};for(const k of ['ca','certificate','privateKey','deviceKey'])v[k]=Buffer.alloc(32,7);secrets.push(v);return v;},provider:{mode:'capture',device:'',backendProfile:1,maxMs:10000},feedback:options.feedback};
 const deps={now:()=>clock,session:{connect:async()=>{if(options.authWait)await options.authWait.promise;return makeTransport();},launch(value,closed){
  launched++;assert.equal(value.device,'');assert.equal(value.backendProfile,1);hooks=value;done=closed;
  provider={stop(){if(starts&&!receipt){stops++;receipt={cleanExit:!options.stopFailed,nativeStartAccepted:true,nativeStopAccepted:!options.stopFailed,nativeStopFailed:!!options.stopFailed,nativeNeverStarted:false,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}};}},
   async reap(){this.stop();if(!receipt)receipt={cleanExit:false,nativeStartAccepted:false,nativeStopAccepted:false,nativeStopFailed:false,nativeNeverStarted:true,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}};done(receipt);return receipt;}};
  setImmediate(()=>value.onReady(provider));return provider;
 }}};
 const owner=Owner.bind(handle,config,deps);
 const service={callbackMap:{},call(uri,method,raw){
  const token=next++;calls.push({uri,method,body:JSON.parse(raw)});
  setImmediate(()=>{
   function response(value){const entry=service.callbackMap[token];if(entry){entry.callback(JSON.stringify(value));if(!entry.isSubscription&&service.callbackMap[token]===entry)delete service.callbackMap[token];}}
   if(uri==='luna://local.owner.r2d2mic'){
    handle.emit('request',{category:()=>'/capture',method:()=>method.split('/').pop(),senderServiceName:()=>Owner.CALLER||'com.webos.starfish.keyfilters',sender:()=>':keyfilter.1',payload:()=>raw,respond:text=>response(JSON.parse(text))});return;
   }
   assert.equal(uri,'luna://com.webos.service.voiceinput.hidraw');
    if(method==='/getDevices'){const seq=options.devicesSequence;const list=seq?seq[Math.min(seq.length-1,(options._invCount=(options._invCount||0)+1)-1)]:(options.devices||[{id:'remote7',key:'private-key',deviceType:'remote',deviceName:'Synthetic'}]);response({returnValue:true,devices:list});}
    if(method==='/startStreaming'){starts++;const id=JSON.parse(raw).id;if(options.splitStart){response({returnValue:true,socketPath:'owner-pipe://voice',subscribed:true});setImmediate(()=>response({returnValue:true,id,state:'voice start'}));}else response({returnValue:true,id,state:'voice start',socketPath:'owner-pipe://voice'});}
    if(method==='/stopStreaming'){provider.stop();response({returnValue:true});}
   });return token;
  },cancel(token){delete this.callbackMap[token];}};
 const controller=Controller.create(service,1001,()=>clock);
 return{controller,owner,handle,methods,calls,chunks,secrets,config,setActive(v){active=v;},setTime(v){clock=v;controller.tick();},get launched(){return launched;},get starts(){return starts;},get stops(){return stops;},pcm(buffer){const r=hooks.onPcm(buffer);buffer.fill(0);return r;},async close(){controller.close();await flush();return owner.close();}};
}
function request(handle,method,body,peer=':keyfilter.1',caller='com.webos.starfish.keyfilters'){
 body={issuedAt:0,...body};
 const replies=[];handle.emit('request',{category:()=>'/capture',method:()=>method,senderServiceName:()=>caller,sender:()=>peer,payload:()=>JSON.stringify(body),respond:raw=>replies.push(JSON.parse(raw))});return replies;
}
(async()=>{
 await test('physical press actual inventory LG start PCM Spark release and rearm',async()=>{
  const f=fixture();assert.deepEqual(f.methods,['/capture/begin','/capture/release','/capture/cancel']);
  assert.equal(f.controller.key(20,true,false,'remote-input'),false);assert.equal(f.launched,0);
  f.controller.key(1001,true,false,'remote-input');await flush();assert.equal(f.launched,1);assert.equal(f.starts,1);
  const start=f.calls.find(x=>x.method==='/startStreaming');assert.equal(start.body.id,'remote7');assert.equal(start.body.key,'private-key');
  assert(f.secrets.every(x=>Object.values(x).every(b=>b.every(v=>v===0))));
  f.controller.key(1001,true,true,'remote-input');assert.equal(f.starts,1);
  const pcm=Buffer.alloc(6400,0x5a);assert(f.pcm(pcm));assert(pcm.every(x=>x===0));
  f.controller.key(1001,false,false,'foreign-input');await flush();assert.equal(f.stops,0);
  f.controller.key(1001,false,false,'remote-input');await flush();f.controller.tick();
  assert.equal(f.stops,1);assert.equal(Buffer.concat(f.chunks).length,6400);assert(Buffer.concat(f.chunks).every(x=>x===0x5a));assert.equal(f.owner.status().state,'idle');
  f.controller.key(1001,true,false,'remote-input');await flush();assert.equal(f.launched,2);const closed=await f.close();assert.equal(closed.cleanupComplete,true);
 });
 await test('empty initial getDevices while handleStartService runs retries on tick and starts remote0 with split startStreaming reply',async()=>{
  const f=fixture({splitStart:true,devicesSequence:[[],[],[{id:'remote0',key:'magic-key',deviceType:'remote',deviceName:'Synthetic remote'}]]});
  f.controller.key(1001,true,false,'remote-input');await flush();assert.equal(f.starts,0);
  f.setTime(150);await flush();assert.equal(f.starts,0);
  f.setTime(300);await flush();assert.equal(f.starts,1);assert.equal(f.stops,0);
  const pcm=Buffer.alloc(3200,0x3c);assert(f.pcm(pcm));
  f.controller.key(1001,false,false,'remote-input');await flush();f.controller.tick();
  assert.equal(f.stops,1);assert.equal(Buffer.concat(f.chunks).length,3200);
  const start=f.calls.find(x=>x.method==='/startStreaming');assert.equal(start.body.id,'remote0');assert.equal(start.body.key,'magic-key');
  assert.equal((await f.close()).cleanupComplete,true);
 });
 await test('held voice key releases once at the capture deadline before key-up',async()=>{
  const f=fixture({splitStart:true});f.controller.key(1001,true,false,'input');await flush();
  assert(f.pcm(Buffer.alloc(6400,0x2a)));f.setTime(Controller.HOLD_MS-1);await flush();assert.equal(f.stops,0);
  f.setTime(Controller.HOLD_MS);await flush();f.controller.tick();assert.equal(f.stops,1);
  assert.equal(f.calls.filter(x=>x.method==='/capture/release').length,1);
  assert.equal(f.calls.filter(x=>x.method==='/capture/cancel').length,0);
  assert.equal(f.owner.status().state,'idle');
  f.controller.key(1001,true,true,'input');f.controller.key(1001,false,false,'input');await flush();
  assert.equal(f.launched,1);assert.equal(f.stops,1);assert.equal((await f.close()).cleanupComplete,true);
 });
 await test('a 10 s hold is released at the 9.2 s limit and the press feedback reports it as cut',async()=>{
  // One hold limit: the keyfilter's release, voice_feedback's countdown and cut test. The
  // owned provider's 10 s lease outlasts it by the 800 ms the 5200/6000 pair had.
  assert.equal(Controller.HOLD_MS,9200);assert.equal(V.HOLD_MS,Controller.HOLD_MS);
  assert.equal(Session.MAX_MS[0]-Controller.HOLD_MS,800);assert(Controller.PRESS_MS>Controller.HOLD_MS+2000);
  assert.equal(Owner.PRESS_MS,Controller.PRESS_MS);assert(V.HOLD_MS<=V.DEADLINE_MAX_MS);
  for(const [keyUp,held,cut] of [[10000,Controller.HOLD_MS,true],[Controller.HOLD_MS-100,Controller.HOLD_MS-100,false]]){
   const calls=[],feedback={begin:()=>({press:1}),listening:()=>{calls.push(['listening']);},released:(_,ms)=>{calls.push(['released',ms]);},settle(){},refused(){},latch(){}};
   const f=fixture({splitStart:true,feedback});f.controller.key(1001,true,false,'input');await flush();
   assert(f.pcm(Buffer.alloc(6400,0x2a)));await flush();
   f.setTime(Math.min(keyUp,Controller.HOLD_MS)-1);await flush();assert.equal(f.stops,0);
   if(keyUp<Controller.HOLD_MS){f.setTime(keyUp);f.controller.key(1001,false,false,'input');}else f.setTime(Controller.HOLD_MS);
   await flush();f.controller.tick();await flush();assert.equal(f.stops,1);
   f.setTime(keyUp);f.controller.key(1001,false,false,'input');await flush();f.controller.tick();
   assert.deepEqual(calls,[['listening'],['released',held]]);assert.equal(held>=V.HOLD_MS-5,cut);
   assert.equal(f.calls.filter(x=>x.method==='/capture/release').length,1);assert.equal(f.calls.filter(x=>x.method==='/capture/cancel').length,0);
   assert.equal((await f.close()).cleanupComplete,true);
  }
 });
 await test('release during empty inventory cancels retries without starting capture later',async()=>{
  const f=fixture({devicesSequence:[[],[{id:'remote7',key:'key',deviceType:'remote'}]]});
  f.controller.key(1001,true,false,'input');await flush();assert.equal(f.starts,0);
  f.controller.key(1001,false,false,'input');await flush();f.setTime(300);await flush();
  assert.equal(f.calls.filter(x=>x.method==='/getDevices').length,1);assert.equal(f.starts,0);
  assert.equal(f.calls.filter(x=>x.method==='/capture/release').length,0);
  assert.equal(f.calls.filter(x=>x.method==='/capture/cancel').length,1);
  assert.equal((await f.close()).cleanupComplete,true);
 });
 await test('release during pending TLS prevents any provider launch and wipes key buffers',async()=>{
  const gate=deferred(),f=fixture({authWait:gate});f.controller.key(1001,true,false,'input');await flush();f.controller.key(1001,false,false,'input');await flush();gate.resolve();await flush();
  assert.equal(f.launched,0);assert(f.secrets.every(x=>Object.values(x).every(b=>b.every(v=>v===0))));assert.equal((await f.close()).cleanupComplete,true);
 });
 await test('multiple eligible LG remotes do not start either microphone',async()=>{
  const f=fixture({devices:[{id:'remote3',key:'a',deviceType:'remote'},{id:'remote7',key:'b',deviceType:'remote'}]});f.controller.key(1001,true,false,'input');await flush();assert.equal(f.starts,0);assert.equal((await f.close()).cleanupComplete,true);
 });
 await test('owner caller and peer are checked rather than a sender payload',async()=>{
  const f=fixture();const bad=request(f.handle,'begin',{request:1,deviceId:'input'},':attacker','com.fake');assert.equal(bad[0].returnValue,false);assert.equal(f.launched,0);
  f.controller.key(1001,true,false,'input');await flush();const wrong=request(f.handle,'cancel',{request:1},':other-keyfilter');assert.equal(wrong[0].returnValue,false);assert.equal(f.stops,0);await f.close();
 });
 await test('cancel overtaking begin retires exact request without launch',async()=>{
  const f=fixture();assert.equal(request(f.handle,'cancel',{request:9})[0].returnValue,true);assert.equal(request(f.handle,'begin',{request:9,deviceId:'input'})[0].returnValue,false);assert.equal(f.launched,0);await f.close();
 });
 await test('future cancel while older request exists prevents late next begin',async()=>{
  const f=fixture();f.controller.key(1001,true,false,'input');await flush();assert.equal(request(f.handle,'cancel',{request:2})[0].returnValue,true);assert.equal(f.starts,1);
  request(f.handle,'cancel',{request:1});await flush();assert.equal(f.owner.status().state,'idle');
  assert.equal(request(f.handle,'begin',{request:2,deviceId:'input'})[0].returnValue,false);await f.close();
 });
 await test('power unknown refuses begin and cancels actual owned provider',async()=>{
  const f=fixture();f.setActive(false);f.controller.key(1001,true,false,'input');await flush();assert.equal(f.launched,0);f.controller.tick();
  f.setActive(true);f.controller.key(1001,true,false,'input');await flush();assert.equal(f.launched,1);f.setActive(false);const r=await f.owner.cancelActive('POWER_UNKNOWN');assert.equal(r.cleanupComplete,true);await f.close();
 });
 await test('failed native stop is retained as unresolved and no second provider starts',async()=>{
  const f=fixture({stopFailed:true});f.controller.key(1001,true,false,'input');await flush();f.pcm(Buffer.alloc(6400,1));f.controller.key(1001,false,false,'input');await flush();f.controller.tick();
  assert.equal(f.owner.status().state,'unavailable');assert.equal((await f.close()).cleanupComplete,false);
 });
 await test('held press watchdog cancels once without replaying repeat input',async()=>{
  const f=fixture();f.controller.key(1001,true,false,'input');await flush();f.setTime(Controller.PRESS_MS);await flush();assert.equal(f.stops,1);f.controller.key(1001,true,true,'input');await flush();assert.equal(f.launched,1);await f.close();
 });
 await test('52-byte deferred device handshake requires explicit backend profile',async()=>{
  const f=Legacy.frame('capture','',123,10000,1);assert.equal(f.length,52);assert.equal(f[8],0);assert.equal(f.readUInt32LE(40),10000);assert.equal(f.readUInt32LE(48),1);assert.throws(()=>Legacy.frame('capture','remote0',123,10000));
  // The native provider's own lease cap (owned_provider.cpp, kMaxLeaseMs) is 10000.
  assert.equal(Legacy.frame('capture','',123,6000,1).readUInt32LE(40),6000);assert.throws(()=>Legacy.frame('capture','',123,10001,1));
 });
 console.log(JSON.stringify({groups:count,nativeCalls:0,modelCalls:0}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
