'use strict';
const assert=require('assert');
const {fixture,flush,deferred}=require('./ui_capture_fixture');
let groups=0;async function test(fn){await fn();groups++;}
(async()=>{
 await test(async()=>{const f=fixture({qualified:false});assert.deepEqual(f.status(),{available:false,state:'unavailable',request:null,started:false,stopped:false,cleanupComplete:true});f.command('start');await flush();assert.equal(f.stats().launched,0);await f.close();});
 await test(async()=>{const f=fixture();f.command('start');await flush();assert.equal(f.status().state,'listening');assert.equal(f.stats().starts,1);assert(f.pcm());f.command('release');await flush();assert.equal(f.status().state,'accepted');assert(f.status().started&&f.status().stopped&&f.status().cleanupComplete);assert.deepEqual(f.stats(),{starts:1,stops:1,releases:1,launched:1});assert.equal(Buffer.concat(f.chunks).length,6400);assert(f.calls.every(c=>c.cancelled));assert.equal(f.timers.size,0);assert(f.secrets.every(s=>Object.values(s).every(b=>b.every(x=>x===0))));f.command('start');await flush();assert.equal(f.stats().launched,1);await f.close();});
 await test(async()=>{const f=fixture({foreignReply:true});f.command('start');await flush();assert.equal(f.stats().starts,0);assert.equal(f.status().state,'refused');assert.equal(f.stats().releases,0);await f.close();});
 await test(async()=>{
  const f=fixture({nativeStartFailures:1});f.command('start');await flush();
  assert.deepEqual(f.status(),{available:true,state:'refused',request:'1234567890abcdef',started:false,stopped:true,cleanupComplete:true});
  assert.deepEqual(f.stats(),{starts:1,stops:1,releases:0,launched:1});assert.equal(f.chunks.length,0);assert(f.calls.every(c=>c.cancelled));
  f.command('start','abcdef1234567890');await flush();assert.equal(f.status().state,'listening');assert(f.pcm());f.command('release','abcdef1234567890');await flush();
  assert.equal(f.status().state,'accepted');assert.deepEqual(f.stats(),{starts:2,stops:2,releases:1,launched:2});await f.close();
 });
 await test(async()=>{const f=fixture();f.physical('begin');await flush();assert.equal(f.status().available,false);f.command('start');await flush();assert.equal(f.stats().launched,1);await f.close();});
 await test(async()=>{const f=fixture();f.command('start');await flush();assert.equal(f.physical('begin')[0].returnValue,false);assert.equal(f.stats().launched,1);f.physical('cancel',1,'local.owner.r2d2mic');assert.equal(f.status().state,'listening');await f.close();});
 await test(async()=>{const gate=deferred(),f=fixture({authWait:gate});f.command('start');f.command('release');gate.resolve();await flush();assert.equal(f.stats().launched,0);assert.equal(f.status().state,'refused');await f.close();});
 await test(async()=>{const f=fixture({holdStart:true});f.command('start');await flush();assert.equal(f.stats().starts,1);assert.equal(f.status().state,'preparing');f.command('release');await flush();assert.equal(f.stats().releases,0);assert(f.calls.some(c=>c.method==='stopStreaming'));assert.equal(f.status().state,'refused');await f.close();});
 await test(async()=>{const f=fixture({stopReplyFails:true});f.command('start');await flush();f.pcm();f.command('release');await flush();assert.equal(f.stats().releases,0);assert.equal(f.status().state,'refused');await f.close();});
 await test(async()=>{const f=fixture({stopFailed:true});f.command('start');await flush();f.pcm();f.command('cancel');await flush();assert.equal(f.status().state,'unknown');assert.equal(f.status().cleanupComplete,false);assert.equal(f.status().available,false);f.command('start','abcdef1234567890');assert.equal(f.stats().launched,1);await f.close();});
 await test(async()=>{const f=fixture();f.command('cancel');f.command('start');await flush();assert.equal(f.stats().launched,0);await f.close();});
 await test(async()=>{const f=fixture();f.command('start');await flush();f.pcm();await f.advance(5200);assert.equal(f.stats().releases,0);assert.equal(f.status().state,'refused');await f.close();});
 await test(async()=>{const f=fixture();f.command('start');await flush();f.setActive(false);await f.service.cancelActive('TV_INACTIVE');await flush();assert.equal(f.status().available,false);assert.equal(f.stats().releases,0);await f.close();});
 await test(async()=>{const f=fixture({cancelFails:true});f.command('start');await flush();f.pcm();f.command('release');await flush();assert.equal(f.status().state,'unknown');assert.equal(f.status().cleanupComplete,false);await f.close();});
 await test(async()=>{const f=fixture({holdResult:true});f.command('start');await flush();f.pcm();f.command('release');await flush();assert.equal(f.status().state,'finishing');assert(f.calls.every(c=>c.cancelled));f.calls.find(c=>c.method==='startStreaming').reply({returnValue:false});assert.equal(f.status().state,'finishing');f.command('cancel');await flush();assert.equal(f.status().state,'unknown');assert.equal(f.status().cleanupComplete,true);await f.close();});
 await test(async()=>{const f=fixture({holdResult:true,cancelLateAccepted:true});f.command('start');await flush();f.pcm();f.command('release');await flush();f.command('cancel');await flush();assert.equal(f.status().state,'unknown');assert.equal(f.status().cleanupComplete,true);await f.close();});
 for(const resultUnknown of [true,'terminal'])await test(async()=>{const f=fixture({resultUnknown});f.command('start');await flush();f.pcm();f.command('release');await flush();assert.equal(f.status().state,'unknown');assert.equal(f.status().cleanupComplete,true);assert.equal(f.stats().releases,1);await f.close();});
 await test(async()=>{const f=fixture();for(const body of [{action:'start',request:'x'},{action:'start',request:'1234567890abcdef',device:'remote7'},{action:'begin',request:'1234567890abcdef'}])f.service.uiCommand(body);await flush();assert.equal(f.stats().launched,0);await f.close();});
 console.log(JSON.stringify({groups,nativeCalls:0,modelCalls:0}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
