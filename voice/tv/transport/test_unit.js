'use strict';
// Deterministic host-only fault injection. Real TLS is tested separately.
const assert=require('assert'),fs=require('fs'),vm=require('vm'),crypto=require('crypto'),{EventEmitter}=require('events');
const {createPcmAdapter}=require('./pcm_provider_adapter');
const good=()=>({cleanExit:true,nativeStartAccepted:true,nativeStopAccepted:true,nativeStopFailed:false,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}});
const tick=()=>new Promise(r=>setImmediate(r));let tests=0;
async function test(name,fn){await fn();tests++;process.stdout.write('ok '+name+'\n');}
function fakeTransport(){let ready=true,closedResolve;const listeners=[];const state={sent:[],releases:0,cancels:0,closes:0};const closed=new Promise(r=>closedResolve=r);
 const t={state,get authenticatedTransportReady(){return ready;},availablePcmBytes:65536,closed,watch(l){listeners.push(l);return()=>{};},trySendPcm(b){state.sent.push(Buffer.from(b));return true;},
 async release(){state.releases++;ready=false;closedResolve({released:true,reason:'released'});},async cancel(){state.cancels++;ready=false;closedResolve({released:false,reason:'cancelled'});},async close(){state.closes++;ready=false;closedResolve({released:false,reason:'TRANSPORT_FAILED'});},fail(){ready=false;for(const l of listeners)if(l.onError)l.onError({code:'TRANSPORT_FAILED'});}};return t;}
function fakeProvider(receipt=good()){return {stops:0,reaps:0,stop(){this.stops++;},async reap(){this.reaps++;return receipt;}};}
function bind(t,r=good()){const a=createPcmAdapter(t),p=fakeProvider(r);a.start(h=>{h.onReady(p);return p;});return {a,p};}
function fixture({challenge=true,timers={setTimeout,clearTimeout}}={}){
 const raw=Buffer.from('public-test-peer'),pin=crypto.createHash('sha256').update(raw).digest('hex');
 class Socket extends EventEmitter{constructor(){super();this.authorized=true;this.destroyed=false;this.frames=[];this.block=false;this.pending=[];}
 setNoDelay(){}getProtocol(){return'TLSv1.3';}getPeerCertificate(){return{raw};}
 write(frame,cb){this.frames.push({ref:frame,bytes:Buffer.from(frame)});if(this.block){this.pending.push(cb);return false;}queueMicrotask(()=>cb());return true;}
 destroy(){if(!this.destroyed){this.destroyed=true;queueMicrotask(()=>this.emit('close'));}}
 flush(){this.block=false;for(const cb of this.pending.splice(0))cb();this.emit('drain');}
 }
 const socket=new Socket(),exports={};const fakeTls={createSecureContext:()=>({}),checkServerIdentity:()=>undefined,connect:()=>{queueMicrotask(()=>{socket.emit('secureConnect');if(challenge)socket.emit('data',Buffer.alloc(32,1));});return socket;}};
 const source=fs.readFileSync(require.resolve('./tls_pcm_client'),'utf8');
 const mod={exports:{}};vm.runInNewContext('(function(require,module,Buffer,setTimeout,clearTimeout){'+source+'\n})')((n)=>n==='tls'?fakeTls:require(n),mod,Buffer,timers.setTimeout,timers.clearTimeout);
 const config={address:'127.0.0.1',port:12345,serverName:'voice.test',ca:Buffer.from('ca'),certificate:Buffer.from('cert'),privateKey:Buffer.from('key'),serverCertificateSha256:pin,deviceKey:Buffer.alloc(32,7)};
 function result(value){const b=Buffer.from(JSON.stringify(value));const f=Buffer.alloc(b.length+4);f.writeUInt32BE(b.length);b.copy(f,4);socket.emit('data',f);}
 return {socket,api:mod.exports,config,result};
}
function clockTimers(){
 let now=0,next=0;const held=new Map();return{
  setTimeout(fn,ms){const id=++next;held.set(id,{at:now+ms,fn});return id;},clearTimeout(id){held.delete(id);},
  async advance(ms){const end=now+ms;for(;;){const due=[...held].filter(([,t])=>t.at<=end).sort((a,b)=>a[1].at-b[1].at)[0];
   if(!due)break;now=due[1].at;held.delete(due[0]);due[1].fn();await tick();}now=end;await tick();},
  count:()=>held.size
 };
}
(async()=>{
 await test('stop rejection never releases PCM for execution',async()=>{for(const r of [ {...good(),nativeStopAccepted:false},{...good(),nativeStopFailed:true},{...good(),nativeStartAccepted:false},{...good(),nativeStopFailed:undefined}]){const t=fakeTransport(),{a,p}=bind(t,r);assert(a.onPcm(Buffer.alloc(3200)));const end=await a.release();assert.strictEqual(t.state.releases,0);assert.strictEqual(t.state.cancels,1);assert.strictEqual(end.reason,'PROVIDER_REAP_UNQUALIFIED');assert(p.stops>0);}});
 await test('explicit failed-stop event also refuses a contradictory receipt',async()=>{const t=fakeTransport(),{a,p}=bind(t);p.events=new Set(['U']);a.onPcm(Buffer.alloc(3200));await a.release();assert.strictEqual(t.state.releases,0);});
 await test('input copy is synchronous and odd pieces retain order',async()=>{const t=fakeTransport(),{a}=bind(t);let pos=0;for(const n of [1,3,3197,3199]){const b=Buffer.alloc(n);for(let i=0;i<n;i++)b[i]=(pos++)%251;assert(a.onPcm(b));b.fill(0);}await a.release();assert(Buffer.concat(t.state.sent).equals(Buffer.from(Array.from({length:6400},(_,i)=>i%251))));assert.strictEqual(t.state.releases,1);});
 await test('odd final sample cancels rather than truncates',async()=>{const t=fakeTransport(),{a}=bind(t);a.onPcm(Buffer.alloc(641));await a.release();assert.strictEqual(t.state.releases,0);assert.strictEqual(t.state.cancels,1);});
 await test('queue pressure stops provider synchronously',async()=>{const t=fakeTransport(),{a,p}=bind(t);t.availablePcmBytes=0;assert.strictEqual(a.onPcm(Buffer.alloc(3200)),false);assert(p.stops>0);await a.closed;assert.strictEqual(t.state.releases,0);});
 await test('lost transport stops and reaps provider',async()=>{const t=fakeTransport(),{a,p}=bind(t);t.fail();assert(p.stops>0);const end=await a.closed;assert(end.providerReaped);assert.strictEqual(t.state.releases,0);});
 await test('unready transport cannot invoke factory',async()=>{const t=fakeTransport();await t.close();assert.throws(()=>createPcmAdapter(t),/TRANSPORT_NOT_READY/);});
 await test('release waits for actual transport closure',async()=>{const t=fakeTransport(),{a}=bind(t);let close;const ended=new Promise(r=>close=r);t.closed=ended;t.release=async()=>{};a.onPcm(Buffer.alloc(640));let done=false;const release=a.release().then(()=>done=true);await tick();assert(!done);close({released:true});await release;assert(done);});
 await test('failed release also waits for actual transport closure',async()=>{const t=fakeTransport(),{a}=bind(t,{...good(),nativeStopAccepted:false});let close;t.closed=new Promise(r=>close=r);t.cancel=async()=>{};a.onPcm(Buffer.alloc(640));let done=false;const release=a.release().then(()=>done=true);await tick();assert(!done);close({released:false});await release;assert(done);});
 await test('challenge is required before readiness',async()=>{const f=fixture({challenge:false});let done=false;const p=f.api.connect(f.config).then(s=>{done=true;return s;});await tick();assert(!done);f.socket.emit('data',Buffer.alloc(32));const s=await p;await s.close();});
 await test('write buffers wiped after completion',async()=>{const f=fixture(),s=await f.api.connect(f.config);const b=Buffer.alloc(3200,93);const p=s.sendPcm(b);b.fill(0);await p;assert(f.socket.frames[1].bytes.subarray(8).every(n=>n===93));assert(f.socket.frames[1].ref.every(n=>n===0));await s.close();});
 await test('queued PCM owns a synchronous copy',async()=>{const f=fixture(),s=await f.api.connect(f.config);f.socket.block=true;s.trySendPcm(Buffer.alloc(3200,1));const b=Buffer.alloc(3200,9);s.trySendPcm(b);b.fill(0);f.socket.flush();await tick();assert(f.socket.frames[2].bytes.subarray(8).every(n=>n===9));await s.close();});
 await test('bounded pending queue aborts and wipes active frame',async()=>{const f=fixture(),s=await f.api.connect(f.config);f.socket.block=true;for(let i=0;i<20;i++)assert(s.trySendPcm(Buffer.alloc(3200,9)));assert.strictEqual(s.trySendPcm(Buffer.alloc(3200)),false);const e=await s.closed;assert.strictEqual(e.reason,'PCM_NOT_ADMITTED');assert(f.socket.frames[1].ref.every(n=>n===0));});
 await test('cancel sequence ignores discarded unsent frames',async()=>{const f=fixture(),s=await f.api.connect(f.config);f.socket.block=true;assert(s.trySendPcm(Buffer.alloc(3200)));assert(s.trySendPcm(Buffer.alloc(3200)));const p=s.cancel();f.socket.flush();await p;assert.strictEqual(f.socket.frames.length,3);assert.strictEqual(f.socket.frames[2].bytes.readUInt32BE(0),1);assert.strictEqual(f.socket.frames[2].bytes.readUInt32BE(4),0xffffffff);f.result({schema:1,state:'refused',code:'VOICE_CANCELLED'});await s.closed;});
 await test('PCM frame count is bounded',async()=>{const f=fixture(),s=await f.api.connect(f.config);for(let i=0;i<500;i++)await s.sendPcm(Buffer.alloc(2));assert.strictEqual(s.trySendPcm(Buffer.alloc(2)),false);await s.closed;});
 await test('accepted action before release is a protocol fault',async()=>{const f=fixture(),s=await f.api.connect(f.config);f.result({schema:1,state:'accepted',code:'BROWSER_NAVIGATION_ACCEPTED'});assert.strictEqual((await s.closed).reason,'SERVER_PROTOCOL');});
 await test('terminal response can arrive before the release write callback',async()=>{const f=fixture(),s=await f.api.connect(f.config);await s.sendPcm(Buffer.alloc(640));f.socket.block=true;const p=s.release();p.catch(()=>{});let done=false;s.closed.then(()=>done=true);f.result({schema:1,state:'accepted',code:'FIXTURE'});f.socket.emit('end');await tick();assert(!done,'response must wait for the actual write completion');f.socket.flush();await p;const r=await s.closed;assert.strictEqual(r.reason,'released');assert(r.controlFlushed&&r.actionAcknowledged);});
 await test('terminal response cannot authorize a queued unsent release',async()=>{const f=fixture(),s=await f.api.connect(f.config);f.socket.block=true;s.trySendPcm(Buffer.alloc(640));const p=s.release().catch(()=>{});f.result({schema:1,state:'accepted',code:'FIXTURE'});assert.strictEqual((await s.closed).reason,'SERVER_PROTOCOL');await p;});
 await test('early response with failed terminal write cannot acknowledge action',async()=>{const f=fixture(),s=await f.api.connect(f.config);await s.sendPcm(Buffer.alloc(640));f.socket.block=true;const p=s.release().catch(()=>{});f.result({schema:1,state:'accepted',code:'FIXTURE'});f.socket.destroy();const r=await s.closed;assert(!r.controlFlushed&&!r.actionAcknowledged&&!r.released);await p;});
 await test('protocol failure after write callback cannot promote an early response',async()=>{const f=fixture(),s=await f.api.connect(f.config);await s.sendPcm(Buffer.alloc(640));f.socket.block=true;const p=s.release().catch(()=>{});f.result({schema:1,state:'accepted',code:'FIXTURE'});f.socket.flush();f.socket.emit('data',Buffer.from([0]));const r=await s.closed;await p;assert.strictEqual(r.reason,'SERVER_PROTOCOL');assert(!r.actionAcknowledged&&!r.released);assert.strictEqual(r.outcome,null);});
 await test('oversized result is refused without allocation from its size',async()=>{const f=fixture(),s=await f.api.connect(f.config);const b=Buffer.alloc(4);b.writeUInt32BE(0xffffffff);f.socket.emit('data',b);assert.strictEqual((await s.closed).reason,'SERVER_PROTOCOL');});
 await test('EOF after release is unconfirmed not action completion',async()=>{const f=fixture(),s=await f.api.connect(f.config);await s.sendPcm(Buffer.alloc(640));await s.release();f.socket.emit('end');const e=await s.closed;assert.strictEqual(e.reason,'RESULT_UNCONFIRMED');assert.strictEqual(e.actionAcknowledged,false);assert.strictEqual(e.controlFlushed,true);});
 await test('valid cold-browser result after105seconds is still received without extending audio',async()=>{
  const timers=clockTimers(),f=fixture({timers}),s=await f.api.connect(f.config);await s.sendPcm(Buffer.alloc(640));await s.release();
  let closed=false;s.closed.then(()=>closed=true);await timers.advance(109500);assert(!closed);
  assert(!s.authenticatedTransportReady);f.result({schema:1,state:'accepted',code:'BROWSER_NAVIGATION_ACCEPTED'});
  const end=await s.closed;assert(end.actionAcknowledged&&end.released);assert.equal(timers.count(),0);
 });
 await test('audio lifetime covers the 9.2 s hold and its stop, and is still finite',async()=>{
  // Hello to release flushed; 1 s under Spark's AUDIO_DEADLINE (voice/backend/ingress.py, 16 s).
  const timers=clockTimers(),f=fixture({timers}),s=await f.api.connect(f.config);assert.equal(f.api.AUDIO_MS,15000);
  await s.sendPcm(Buffer.alloc(640));await timers.advance(f.api.AUDIO_MS-1);assert(s.authenticatedTransportReady);
  await s.release();f.result({schema:1,state:'accepted',code:'BROWSER_NAVIGATION_ACCEPTED'});const end=await s.closed;assert(end.released&&end.actionAcknowledged);
  const t2=clockTimers(),g=fixture({timers:t2}),u=await g.api.connect(g.config);await u.sendPcm(Buffer.alloc(640));
  await t2.advance(g.api.AUDIO_MS-1);assert(u.authenticatedTransportReady);await t2.advance(1);
  const late=await u.closed;assert.equal(late.reason,'AUDIO_DEADLINE');assert(!late.released&&!late.actionAcknowledged);
 });
 await test('partial result cannot renew finite result deadline or imply action completion',async()=>{
  const timers=clockTimers(),f=fixture({timers}),s=await f.api.connect(f.config);await s.sendPcm(Buffer.alloc(640));await s.release();
  await timers.advance(f.api.RESULT_TIMEOUT_MS-1);f.socket.emit('data',Buffer.from([0,0]));await timers.advance(1);
  const end=await s.closed;assert.equal(end.reason,'RESULT_UNCONFIRMED');assert(!end.actionAcknowledged);assert.equal(end.outcome,null);assert.equal(timers.count(),0);
 });
 process.stdout.write('PASS '+tests+' tests\n');
// A failed control leaves its session's real 15 s audio timer behind: exit once stderr is out.
})().catch(e=>{process.exitCode=1;process.stderr.write(e.stack+'\n',()=>process.exit(1));});
