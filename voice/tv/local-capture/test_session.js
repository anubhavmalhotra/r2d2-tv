'use strict';
const assert=require('assert'),{start,evidence,SETTLE_MS,MAX_MS}=require('./session');
const native=require('./supervised_provider'),fs=require('fs'),cp=require('child_process'),crypto=require('crypto');
const turn=()=>new Promise(r=>setImmediate(r));
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r;});return{promise,resolve};}
const provider={mode:'capture',device:'',backendProfile:1,maxMs:10000};
function config(){const secret={};for(const k of ['ca','certificate','privateKey','deviceKey'])secret[k]=Buffer.alloc(32,9);return{secret,getTlsConfig:()=>secret,provider};}
let groups=0;
const watchdog=setTimeout(()=>{throw Error('SESSION_TEST_TIMEOUT');},3000);
// owner_service.js's own latch predicate for a closed receipt.
const stuck=r=>!r||r.cleanupComplete!==true&&(r.providerStarted!==false&&r.providerReaped!==true||r.nativeStopFailed===true);
function cancelled(){const end=deferred();return{authenticatedTransportReady:true,availablePcmBytes:65536,closed:end.promise,watch(){return()=>{};},
 cancel(){this.authenticatedTransportReady=false;end.resolve({released:false,reason:'cancelled'});return end.promise;},close(){return this.cancel();}};}
// A fake root Linux in which every pre-spawn check of the real launch() passes unless
// told otherwise; only the synchronous launch call runs inside it.
function world(change,fn){
 const ORIGINAL='/usr/sbin/voiceinput_hidraw',saved={},platform=Object.getOwnPropertyDescriptor(process,'platform'),getuid=process.getuid;
 const file={dev:1,ino:2,mode:0o100755,uid:0,gid:0,nlink:1,size:4,mtimeMs:4,ctimeMs:5,isFile:()=>true};
 const stubs={
  fstatSync:fd=>fd===9?{...file}:{...file,ino:fd},readlinkSync:p=>p.startsWith('/proc/self/fd/')?'/held/original':p==='/proc/77/exe'&&change.liveOriginal?ORIGINAL:'/usr/bin/other',
  readFileSync:(p,enc)=>typeof p==='string'?'pos:\t0\nflags:\t0100000\n':Buffer.alloc(4,1),readSync:(fd,b,o,n,pos)=>{const k=Math.max(0,Math.min(n,4-pos));b.fill(1,o,o+k);return k;},
  realpathSync:p=>p,lstatSync:p=>p==='/tmp/voiceinput_hidraw.lock'?{isFile:()=>true,isSymbolicLink:()=>false,uid:0,gid:0,nlink:1,size:0,mode:0o100600}:{isDirectory:()=>true,isSymbolicLink:()=>false,uid:0,mode:0o40755},openSync:p=>p==='/m/supervisor'?11:10,closeSync(){},
  statSync:p=>p==='/m/supervisor'?{...file,ino:11}:{...file,ino:99},readdirSync:()=>['77','self']};
 // Every image hash "matches": the held original's pinned SHA is not something a test can produce.
 const sha='2b9f9e318fba385801e160349159cb3238d1fc370136a385413168170879e28b',hash=crypto.createHash,spawn=cp.spawn;
 try{
  Object.defineProperty(process,'platform',{value:'linux',configurable:true});process.getuid=()=>0;
  for(const k of Object.keys(stubs)){saved[k]=fs[k];fs[k]=stubs[k];}
  crypto.createHash=()=>({update(){return this;},digest:()=>sha});
  cp.spawn=change.spawn||(()=>{throw Error('spawn EAGAIN');});
  return fn({mode:'capture',device:'',maxMs:10000,backendProfile:1,heldOriginalFd:9,modulePath:'/m/module.so',moduleSha256:sha,supervisorPath:'/m/supervisor',supervisorSha256:sha});
 }finally{
  Object.defineProperty(process,'platform',platform);process.getuid=getuid;for(const k of Object.keys(saved))fs[k]=saved[k];crypto.createHash=hash;cp.spawn=spawn;
 }
}
function thrown(fn){try{fn();}catch(e){return e;}assert.fail('did not throw');}
async function shutdownReceipt(change){
 const c=config(),end=deferred();let complete=null;
 const transport={authenticatedTransportReady:true,availablePcmBytes:65536,closed:end.promise,
  watch(){return()=>{};},cancel(){this.authenticatedTransportReady=false;end.resolve({released:false,reason:'cancelled'});return end.promise;},close(){return this.cancel();}};
 const receipt={cleanExit:false,exited:true,nativeStartAccepted:true,nativeStopAccepted:false,nativeStopFailed:false,nativeNeverStarted:false,
  networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true},...change};
 const job=start(c,{connect:async()=>transport,launch(options,done){
  const child={stop(){},async reap(){done(receipt);return receipt;}};
  complete=()=>options.onReady(child);setImmediate(complete);return child;
 }});
 await job.ready;await job.cancel();return job.closed;
}
(async()=>{
 let c=config(),calls=0;const s=start(c,{connect:async()=>{throw Error('PRIVATE ERROR');},launch(){calls++;}});const r=await s.closed;assert.equal(calls,0);assert.equal(r.cleanupComplete,true);assert(!JSON.stringify(r).includes('PRIVATE'));assert(Object.values(c.secret).every(b=>b.every(v=>v===0)));groups++;
 c=config();const gate=deferred(),done=deferred();let closed=false;
 const transport={authenticatedTransportReady:true,availablePcmBytes:65536,closed:done.promise,close(){closed=true;this.authenticatedTransportReady=false;done.resolve({released:false});return done.promise;}};
 const pending=start(c,{connect:async()=>{await gate.promise;return transport;},launch(){calls++;}});void pending.cancel();gate.resolve();const p=await pending.closed;assert(closed);assert.equal(calls,0);assert.equal(p.cleanupComplete,true);assert(Object.values(c.secret).every(b=>b.every(v=>v===0)));groups++;
 c=config();const end=deferred();const t={authenticatedTransportReady:true,availablePcmBytes:65536,closed:end.promise,watch(){return()=>{};},cancel(){this.authenticatedTransportReady=false;end.resolve({released:false});return end.promise;},close(){return this.cancel();}};
 const failed=start(c,{connect:async()=>t,launch(){throw Error('unknown post-spawn state');}});const failedResult=await failed.closed;assert.equal(failedResult.cleanupComplete,false);assert.equal(failedResult.providerStarted,true);groups++;
 // A reaped process can still have a truncated/failed native terminal stream.
 // Process exit alone cannot replace both missing STOP/never-started evidence.
 const uncertain=await shutdownReceipt({});assert.equal(uncertain.providerReaped,true);assert.equal(uncertain.nativeStopAccepted,false);assert.equal(uncertain.cleanupComplete,false);groups++;
 const graceful=await shutdownReceipt({cleanExit:true});assert.equal(graceful.cleanupComplete,true);groups++;
 for(const evidence of [{nativeNeverStarted:true,nativeStartAccepted:false},{nativeStopAccepted:true}]){
  const known=await shutdownReceipt(evidence);assert.equal(known.cleanupComplete,true);groups++;
 }
 const stopFailed=await shutdownReceipt({cleanExit:true,nativeStopFailed:true});assert.equal(stopFailed.cleanupComplete,false);groups++;
 // Feedback only: onAudio fires once, after the first ADMITTED PCM chunk and outside
 // the provider's synchronous PCM callback; a throwing listener changes nothing.
 for(const refused of [false,true]){
  const a=config(),stop=deferred();let heard=0,inside=false,pcm=null,sent=0;
  const tr={authenticatedTransportReady:true,availablePcmBytes:refused?0:65536,closed:stop.promise,watch(){return()=>{};},trySendPcm(){sent++;return true;},
   cancel(){this.authenticatedTransportReady=false;stop.resolve({released:false,reason:'cancelled'});return stop.promise;},close(){return this.cancel();}};
  const fine={cleanExit:true,exited:true,nativeStartAccepted:true,nativeStopAccepted:true,nativeStopFailed:false,nativeNeverStarted:false,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}};
  const job=start({...a,onAudio(){assert.equal(inside,false);heard++;throw Error('feedback fault');}},{connect:async()=>tr,launch(options,done){
   assert(!Object.prototype.hasOwnProperty.call(options,'onAudio'));pcm=options.onPcm;
   const child={stop(){},discard(){},async reap(){done(fine);return fine;}};setImmediate(()=>options.onReady(child));return child;}});
  await job.ready;
  inside=true;const first=pcm(Buffer.alloc(3200,1));inside=false;
  assert.equal(first,!refused);assert.equal(heard,0);await turn();
  if(!refused){inside=true;assert.equal(pcm(Buffer.alloc(3200,2)),true);inside=false;await turn();assert.equal(sent,2);}
  assert.equal(heard,refused?0:1);
  const closed=await job.cancel();assert.equal(closed.cleanupComplete,true);groups++;
 }
 // A launch refused before any process exists says so; from the spawn call on it never does.
 {
  const hooks={onReady(){},onPcm(){}},done=()=>{};
  for(const refuse of [()=>native.launch({...provider,...hooks},done),()=>native.launch(null,done),()=>native.launch({...provider,...hooks}),
   ()=>world({},o=>native.launch({...o,maxMs:50,...hooks},done)),()=>world({},o=>native.launch({...o,heldOriginalFd:2,...hooks},done)),
   ()=>world({liveOriginal:true},o=>native.launch({...o,...hooks},done))]){
   const e=thrown(refuse);assert.equal(native.neverSpawned(e),true,String(e));assert.equal(e.message,'PROVIDER_NEVER_SPAWNED');
  }
  let spawned=0;
  for(const change of [{spawn(){spawned++;throw Error('spawn EAGAIN');}},{spawn(){spawned++;return {};}}]){
   const e=thrown(()=>world(change,o=>native.launch({...o,...hooks},done)));assert.equal(native.neverSpawned(e),false,String(e));
  }
  assert.equal(spawned,2);
  for(const e of [Error('PROVIDER_NEVER_SPAWNED'),Object.assign(Error('x'),{code:'PROVIDER_NEVER_SPAWNED'}),null,undefined,'PROVIDER_NEVER_SPAWNED'])assert.equal(native.neverSpawned(e),false);
  groups++;
 }
 // Session: a typed pre-spawn refusal is a clean start failure (no latch); a throw at or
 // after the spawn call, or any untyped throw, stays exactly as strict as before.
 {
  const r=await start(config(),{connect:async()=>cancelled(),launch:(o,d)=>native.launch(o,d)}).closed;
  assert.deepEqual({reason:r.reason,providerStarted:r.providerStarted,providerReaped:r.providerReaped,cleanupComplete:r.cleanupComplete,nativeStopFailed:r.nativeStopFailed},
   {reason:'PROVIDER_START_FAILED',providerStarted:false,providerReaped:false,cleanupComplete:true,nativeStopFailed:false});
  assert.equal(stuck(r),false);
  for(const launch of [(o,d)=>world({},w=>native.launch({...o,...w},d)),()=>{throw Error('PROVIDER_NEVER_SPAWNED');}]){
   const job=start(config(),{connect:async()=>cancelled(),launch,setTimeout:()=>0,clearTimeout(){}});const u=await job.closed;
   assert.deepEqual({providerStarted:u.providerStarted,cleanupComplete:u.cleanupComplete,nativeStopFailed:u.nativeStopFailed},{providerStarted:true,cleanupComplete:false,nativeStopFailed:true});
   assert.equal(stuck(u),true);
  }
  groups++;
 }
 // settled: the provider's own receipt when it lands within 3 s of the close, else null.
 {
  const fine={cleanExit:true,exited:true,nativeStartAccepted:true,nativeStopAccepted:true,nativeStopFailed:false,nativeNeverStarted:false,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}};
  const timers=new Map();let next=0;const fake={setTimeout(fn,ms){timers.set(++next,{fn,ms});return next;},clearTimeout(id){timers.delete(id);}};
  function late(){
   let deliver=null;
   const job=start(config(),{connect:async()=>cancelled(),...fake,launch(o,done){deliver=r=>done(r);const c={stop(){},discard(){},async reap(){return null;}};setImmediate(()=>o.onReady(c));return c;}});
   return {job,deliver:r=>deliver(r)};
  }
  // On time: settled is the receipt the close already used.
  const on=start(config(),{connect:async()=>cancelled(),...fake,launch(o,done){const c={stop(){},discard(){},async reap(){done(fine);return fine;}};setImmediate(()=>o.onReady(c));return c;}});
  await on.ready;const onReceipt=await on.cancel();assert.equal(onReceipt.cleanupComplete,true);assert.equal(onReceipt.networkSealInstalled,true);
  assert.strictEqual(await on.settled,fine);assert.equal(timers.size,0);
  // Late: the close latches (no receipt yet); the clean receipt then settles it.
  let l=late();await l.job.ready;let closed=await l.job.cancel();assert.equal(stuck(closed),true);assert.equal(closed.networkSealInstalled,false);
  assert.equal(timers.size,1);assert.equal([...timers.values()][0].ms,SETTLE_MS);assert.equal(SETTLE_MS,3000);
  let seen='pending';l.job.settled.then(v=>{seen=v;});await turn();assert.equal(seen,'pending');
  l.deliver(fine);await turn();assert.strictEqual(seen,fine);assert.equal(timers.size,0);assert.equal(evidence(seen,true).cleanupComplete,true);
  // Absent: the window ends with null, and a receipt after it changes nothing.
  l=late();await l.job.ready;closed=await l.job.cancel();assert.equal(stuck(closed),true);
  const [id,t]=[...timers][0];timers.delete(id);t.fn();assert.strictEqual(await l.job.settled,null);l.deliver(fine);await turn();assert.strictEqual(await l.job.settled,null);
  // A launch that never spawned has nothing to wait for.
  const none=start(config(),{connect:async()=>cancelled(),...fake,launch:(o,d)=>native.launch(o,d)});await none.closed;assert.strictEqual(await none.settled,null);assert.equal(timers.size,0);
  groups++;
 }
 // evidence() is the one clean test, on time or late: reaped, stopped and sealed.
 {
  const fine={cleanExit:true,exited:true,nativeStartAccepted:true,nativeStopAccepted:true,nativeStopFailed:false,nativeNeverStarted:false,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}};
  assert.equal(evidence(fine,true).cleanupComplete,true);assert.equal(evidence(null,false).cleanupComplete,true);assert.equal(evidence(null,true).cleanupComplete,false);
  for(const change of [{nativeStopFailed:true},{nativeStopFailed:undefined},{networkSealInstalled:false},{supervision:{allReaped:true,supervisionComplete:false}},{supervision:null},
   {nativeStopAccepted:false,cleanExit:false},{nativeStopAccepted:false,exited:false}])assert.equal(evidence({...fine,...change},true).cleanupComplete,false,JSON.stringify(change));
  for(const change of [{nativeStopAccepted:false},{nativeStopAccepted:false,cleanExit:false,nativeNeverStarted:true}])assert.equal(evidence({...fine,...change},true).cleanupComplete,true,JSON.stringify(change));
  // A start LG never accepted, whose traced tree was fully reaped after the failing exit.
  const refused={cleanExit:false,exited:true,nativeStartAccepted:false,nativeStopAccepted:false,nativeStopFailed:false,nativeNeverStarted:false,captureBytes:0,
   networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:false}};
  assert.equal(evidence(refused,true).cleanupComplete,true);assert.equal(stuck(evidence(refused,true)),false);
  for(const change of [{nativeStartAccepted:true},{nativeStartAccepted:undefined},{nativeStopFailed:true},{nativeStopFailed:undefined},{captureBytes:3200},{captureBytes:undefined},
   {networkSealInstalled:false},{supervision:{allReaped:false,supervisionComplete:false}},{supervision:null}])
   assert.equal(evidence({...refused,...change},true).cleanupComplete,false,JSON.stringify(change));
  assert(Object.isFrozen(evidence(fine,true)));groups++;
 }
 assert.throws(()=>start({getTlsConfig(){},provider:{...provider,device:'remote0'}}));assert.throws(()=>start({getTlsConfig(){},provider:{...provider,backendProfile:0}}));for(const maxMs of [6001,9999,10001,12000,'10000',100])assert.throws(()=>start({getTlsConfig(){},provider:{...provider,maxMs}}),/LOCAL_CAPTURE_CONFIG/,String(maxMs));groups++;
 // The provider lease: 10000 (the native cap) and the previous activator's 6000.
 assert.deepEqual(MAX_MS,[10000,6000]);
 for(const maxMs of MAX_MS){
  const leases=[],job=start({getTlsConfig:()=>({}),provider:{...provider,maxMs}},{connect:async()=>cancelled(),launch(o){leases.push(o.maxMs);throw Error('fixture');}});
  const r=await job.closed;assert.deepEqual(leases,[maxMs]);assert.equal(r.reason,'PROVIDER_START_FAILED');
 }
 groups++;
 console.log(JSON.stringify({groups,nativeCalls:0,modelCalls:0}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;}).finally(()=>clearTimeout(watchdog));
