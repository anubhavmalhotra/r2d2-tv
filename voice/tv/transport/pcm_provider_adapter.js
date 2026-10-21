'use strict';
// Synchronous callback adapter for owned_provider.Session. It copies PCM before
// that provider wipes its callback buffer; only 3,200 carry bytes are retained.
const {MAX_AUDIO,MAX_CHUNK}=require('./tls_pcm_client');
function createPcmAdapter(transport){
 if(!transport||transport.authenticatedTransportReady!==true)throw Error('TRANSPORT_NOT_READY');
 let provider=null,providerReady=false,ending=false,draining=false,ended=false,total=0,used=0,carry=Buffer.alloc(MAX_CHUNK),finishPromise=null;
 let stopCode=null,needsCount=false;let resolveClosed;const closed=new Promise(r=>{resolveClosed=r;});
 function wipe(){carry.fill(0);used=0;}
 function receiptOK(r){return !!(r&&r.cleanExit===true&&r.nativeStartAccepted===true&&r.nativeStopAccepted===true&&r.nativeStopFailed===false&&!(provider&&provider.events&&provider.events.has('U'))&&r.networkSealInstalled===true&&r.supervision&&r.supervision.allReaped===true&&r.supervision.supervisionComplete===true);}
 function stopped(reason,clean,result=null){if(ended)return;ended=true;ending=true;wipe();unsubscribe();resolveClosed(Object.freeze({reason,providerReaped:clean,pcmBytes:total,outcome:result&&result.outcome||null,actionAcknowledged:!!(result&&result.actionAcknowledged)}));}
 async function stopAndReap(){
  if(!provider)return false;
  try{provider.stop();const r=await provider.reap();return receiptOK(r)&&(!needsCount||r.pcmCountConfirmed===true&&r.nativePcmBytes===total);}catch(_){return false;}
 }
 function cancel(code='VOICE_CANCELLED'){
  if(finishPromise){
   if(draining&&!ended){draining=false;stopCode=code;wipe();if(provider)try{if(typeof provider.discard==='function')provider.discard();else provider.stop();}catch(_){}
    try{void Promise.resolve(transport.cancel()).catch(()=>transport.close());}catch(_){void transport.close();}}
   return finishPromise;
  }ending=true;stopCode=code;wipe();
  // Stop hardware immediately, before waiting for network delivery or reaping.
  if(provider){try{if(typeof provider.discard==='function')provider.discard();else provider.stop();}catch(_){}}
  finishPromise=Promise.resolve().then(async()=>{try{if(transport.authenticatedTransportReady)await transport.cancel();else await transport.close();}catch(_){await transport.close();}
   await transport.closed;const clean=await stopAndReap();stopped(code,clean);return closed;});return finishPromise;
 }
 function fail(code){void cancel(code);return false;}
 const unsubscribe=transport.watch({onError:()=>{fail('TRANSPORT_FAILED');},onClose:()=>{if(!ending)fail('TRANSPORT_CLOSED');}});
 function attach(value){
  if(ending||ended||transport.authenticatedTransportReady!==true||!value||typeof value.stop!=='function'||typeof value.reap!=='function'||provider&&provider!==value){if(value&&typeof value.stop==='function')try{value.stop();}catch(_){}throw Error('PROVIDER_NOT_ADMITTED');}
  provider=value;return value;
 }
 function onReady(value){try{attach(value);providerReady=true;}catch(_){fail('PROVIDER_NOT_ADMITTED');}}
 function onPcm(bytes){
  if(ending&&!draining||ended||!providerReady||!Buffer.isBuffer(bytes)||bytes.length<1||total+bytes.length>MAX_AUDIO||transport.authenticatedTransportReady!==true)return fail('PCM_NOT_ADMITTED');
  const toFlush=Math.floor((used+bytes.length)/MAX_CHUNK)*MAX_CHUNK;
  if(toFlush>transport.availablePcmBytes)return fail('PCM_BACKPRESSURE');
  total+=bytes.length;let offset=0;
  while(offset<bytes.length){const take=Math.min(MAX_CHUNK-used,bytes.length-offset);bytes.copy(carry,used,offset,offset+take);used+=take;offset+=take;
   if(used===MAX_CHUNK){if(!transport.trySendPcm(carry)){wipe();return fail('PCM_BACKPRESSURE');}wipe();}}
  return true;
 }
 function release(){
  if(finishPromise)return finishPromise;ending=true;
  draining=!!(provider&&typeof provider.releaseDrain==='function');
  needsCount=draining;
  if(provider){try{if(draining)provider.releaseDrain();else provider.stop();}catch(_){draining=false;}}
  finishPromise=Promise.resolve().then(async()=>{
   const clean=await stopAndReap();draining=false;
   if(stopCode||!clean||used%2||total<640){wipe();try{await transport.cancel();}catch(_){await transport.close();}await transport.closed;stopped(stopCode||(!clean?'PROVIDER_REAP_UNQUALIFIED':'PCM_INCOMPLETE'),clean);return closed;}
   try{if(used&&!transport.trySendPcm(carry.subarray(0,used)))throw Error('PCM_BACKPRESSURE');wipe();await transport.release();const result=await transport.closed;stopped(result.released?'released':result.reason,true,result);}
   catch(_){wipe();await transport.close();stopped('TRANSPORT_FAILED',true);}return closed;
  });return finishPromise;
 }
 function providerClosed(receipt){if(ending)return;if(!receiptOK(receipt)){fail('PROVIDER_CLOSED_UNQUALIFIED');return;}void release();}
 function start(factory){
  if(typeof factory!=='function'||provider||ending||!transport.authenticatedTransportReady)throw Error('PROVIDER_NOT_ADMITTED');
  // Factory alone owns native launch and LS2; this module imports no hardware API.
  try{const value=factory({onReady,onPcm},providerClosed);attach(value);return value;}catch(_){fail('PROVIDER_START_FAILED');throw Error('PROVIDER_START_FAILED');}
 }
 return Object.freeze({start,attach,onReady,onPcm,providerClosed,release,cancel,close:()=>cancel('CLIENT_CLOSED'),closed,get authenticatedTransportReady(){return transport.authenticatedTransportReady===true&&!ending;}});
}
module.exports={createPcmAdapter};
