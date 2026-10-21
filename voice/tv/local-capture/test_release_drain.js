'use strict';
const assert=require('assert'),EventEmitter=require('events'),{PassThrough}=require('stream');
const {Session}=require('./legacy_provider'),{createPcmAdapter}=require('../transport/pcm_provider_adapter');
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r;});return{promise,resolve};}
let count=0;
async function run(options={}){
 const terminal=deferred(),chunks=[];let releases=0,cancels=0,closed=false;
 const transport={authenticatedTransportReady:true,availablePcmBytes:65536,closed:terminal.promise,
  watch(){return()=>{};},trySendPcm(b){chunks.push(Buffer.from(b));return true;},
  release(){releases++;closed=true;this.authenticatedTransportReady=false;terminal.resolve({released:true,reason:'released'});return terminal.promise;},
  cancel(){cancels++;closed=true;this.authenticatedTransportReady=false;terminal.resolve({released:false,reason:'cancelled'});return terminal.promise;},close(){return this.cancel();}};
 const child=new EventEmitter();child.stderr=new PassThrough();child.stdio=[];
 for(const n of [3,4,5])child.stdio[n]=new PassThrough();child.kill=()=>{};
 let session,adapter=createPcmAdapter(transport),first=Buffer.alloc(3200,1),late=Buffer.alloc(3200,2),endOnce=false;
 child.stdio[4].end=()=>{if(endOnce)return;endOnce=true;setImmediate(()=>{
  if(options.cancelDrain)void adapter.cancel('VOICE_CANCELLED');
  if(!options.zeroBytes)child.stdio[3].emit('data',late);
  const summary=options.zeroBytes?'Y0000000000000000Z':'Y'+(options.badCount?'00001902':'00001900')+'00000002Z';
  // Split the terminal frame across callbacks; stream boundaries are not frames.
  child.stdio[5].emit('data',Buffer.from((options.failed||options.zeroBytes?'F':'')+(options.stopFailed?'U':'T')+summary.slice(0,9)));
  if(!options.truncated)child.stdio[5].emit('data',Buffer.from(summary.slice(9)+'E'));
  child.emit('close',0,null);
 });};
 session=adapter.start((hooks,done)=>new Session(child,{mode:'capture',device:'',maxMs:200,backendProfile:1,onReady:hooks.onReady,onPcm:hooks.onPcm},r=>{r.supervision={allReaped:true,supervisionComplete:true};done(r);}));
 child.stdio[5].emit('data',Buffer.from('KBLS'));if(!options.zeroBytes)child.stdio[3].emit('data',first);else first.fill(0);
 const result=await adapter.release();const receipt=await session.closed;
 if(options.zeroBytes)late.fill(0);
 assert(first.every(x=>x===0));assert(late.every(x=>x===0));assert(closed);
 return{result,receipt,chunks,releases,cancels};
}
(async()=>{
 let r=await run();assert.equal(r.releases,1);assert.equal(r.result.pcmBytes,6400);assert.equal(r.receipt.nativePcmBytes,6400);assert.equal(r.receipt.nativeFrames,2);assert.equal(r.receipt.pcmCountConfirmed,true);assert.equal(r.receipt.cleanExit,true);assert(Buffer.concat(r.chunks).equals(Buffer.concat([Buffer.alloc(3200,1),Buffer.alloc(3200,2)])));count++;
 for(const options of [{badCount:true},{failed:true},{stopFailed:true},{truncated:true},{cancelDrain:true}]){
  r=await run(options);assert.equal(r.releases,0);assert.notEqual(r.result.reason,'released');assert(r.cancels>0);
  if(options.badCount){assert.equal(r.receipt.pcmCountConfirmed,false);assert.equal(r.receipt.cleanExit,false);}
  if(options.failed||options.stopFailed||options.truncated)assert.equal(r.receipt.cleanExit,false);
  count++;
 }
 r=await run({zeroBytes:true});assert.equal(r.releases,0);assert.equal(r.result.reason,'PCM_INCOMPLETE');assert.equal(r.result.providerReaped,true);assert.equal(r.receipt.pcmCountConfirmed,true);assert.equal(r.receipt.cleanExit,true);count++;
 console.log(JSON.stringify({groups:count,nativeCalls:0,modelCalls:0}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
