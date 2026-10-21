'use strict';
// One physical press, one authenticated Spark stream and one supervised LG child.
// Only tests replace these concrete production implementations.
const tls=require('../transport/tls_pcm_client');
const {createPcmAdapter}=require('../transport/pcm_provider_adapter');
const native=require('./supervised_provider');
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});return{promise,resolve,reject};}
let priorProviderReaped=Promise.resolve();
// How long a closed session still listens for its provider's own receipt.
const SETTLE_MS=3000;
// Provider leases main.js admits: 10000 now, 6000 from the previous activator.
const MAX_MS=Object.freeze([10000,6000]);
// The one test of a provider's own receipt, used on time and for a late receipt.
// started=false means no process was ever spawned: nothing can still be live.
function evidence(receipt,started){
 const reaped=!!(receipt&&receipt.supervision&&receipt.supervision.allReaped===true&&receipt.supervision.supervisionComplete===true);
 // Exit is a fallback only when the native parser also accepted its terminal
 // stream. A reaped child with missing/failed cleanup evidence stays unknown.
 const stopped=!!(receipt&&receipt.nativeStopFailed===false&&(receipt.nativeNeverStarted===true||receipt.nativeStopAccepted===true||receipt.exited===true&&receipt.cleanExit===true));
 // A provider that failed before LG accepted its start: the tracer then kills and
 // reaps every task it traced, so with no start, no PCM and no failed stop nothing
 // can still capture, although the failing exit leaves supervision incomplete.
 const refused=!!(receipt&&receipt.nativeStartAccepted===false&&receipt.nativeStopFailed===false&&receipt.captureBytes===0&&
  receipt.networkSealInstalled===true&&receipt.supervision&&receipt.supervision.allReaped===true);
 return Object.freeze({providerStarted:started,providerReaped:reaped,cleanupComplete:!started||reaped&&stopped&&receipt.networkSealInstalled===true||refused,
  nativeStartAccepted:!!(receipt&&receipt.nativeStartAccepted),nativeStopAccepted:!!(receipt&&receipt.nativeStopAccepted),
  nativeStopFailed:started&&(!receipt||receipt.nativeStopFailed!==false),networkSealInstalled:!!(receipt&&receipt.networkSealInstalled===true)});
}
function start(config,dependencies={}){
 if(!config||typeof config.getTlsConfig!=='function'||!config.provider||!['capture','ui-capture'].includes(config.provider.mode)||config.provider.device!==''||config.provider.backendProfile!==1||!MAX_MS.includes(config.provider.maxMs))throw Error('LOCAL_CAPTURE_CONFIG');
 const connect=dependencies.connect||tls.connect,launch=dependencies.launch||native.launch;
 const later=dependencies.setTimeout||setTimeout,clear=dependencies.clearTimeout||clearTimeout;
 const ready=deferred(),closed=deferred(),providerDone=deferred(),settled=deferred();ready.promise.catch(()=>{});
 const prevProvider=dependencies.launch?null:priorProviderReaped;if(!dependencies.launch)priorProviderReaped=providerDone.promise.catch(()=>{});
 let transport=null,adapter=null,provider=null,ended=false,ending=false,providerReceipt=null,readyTimer=null,launchAttempted=false,settleTimer=null;
 // Optional, feedback only: told once, after the first admitted PCM, outside the PCM callback.
 const onAudio=typeof config.onAudio==='function'?config.onAudio:null;let heard=false;
 function audio(onPcm){return bytes=>{const ok=onPcm(bytes);if(ok===true&&onAudio&&!heard){heard=true;Promise.resolve().then(onAudio).catch(()=>{});}return ok;};}
 function finish(reason,result){
  providerDone.resolve();
  if(ended)return;ended=true;ending=true;clearTimeout(readyTimer);ready.reject(Error('LOCAL_CAPTURE_UNAVAILABLE'));
  closed.resolve(Object.freeze({reason,...evidence(providerReceipt,launchAttempted),pcmBytes:result&&Number.isInteger(result.pcmBytes)?result.pcmBytes:0,
   outcome:result&&result.outcome||null,actionAcknowledged:!!(result&&result.actionAcknowledged)}));
  // A provider still closing may confirm its stop a moment later. settled carries
  // its own receipt if it arrives within SETTLE_MS of this close, else null.
  if(!launchAttempted||providerReceipt){settle(providerReceipt);return;}
  settleTimer=later(()=>{settleTimer=null;settle(null);},SETTLE_MS);
  if(settleTimer&&typeof settleTimer.unref==='function')settleTimer.unref();
 }
 function settle(receipt){if(settleTimer!==null){clear(settleTimer);settleTimer=null;}settled.resolve(receipt||null);}
 function stopProvider(){if(provider)try{if(typeof provider.discard==='function')provider.discard();else provider.stop();}catch(_){}}
 function cancel(reason='CAPTURE_CANCELLED'){
  if(ended)return closed.promise;ending=true;clearTimeout(readyTimer);stopProvider();
  if(adapter){
   // Close also interrupts an already released result wait. It cannot undo an
   // action Spark may already have accepted; its terminal result stays unknown.
   if(transport)void transport.close();
   Promise.resolve(adapter.cancel(reason)).then(r=>finish(reason,r),()=>finish('CAPTURE_CLEANUP_UNKNOWN'));
  }else if(transport)Promise.resolve(transport.close()).then(()=>transport.closed).then(()=>finish(reason),()=>finish('TRANSPORT_CLEANUP_UNKNOWN'));
  // A pending authentication continuation closes itself and never launches.
  return closed.promise;
 }
 function release(){
  if(ended||ending)return closed.promise;
  if(!provider||!adapter){void cancel('RELEASE_BEFORE_READY');return closed.promise;}
  ending=true;clearTimeout(readyTimer);
  Promise.resolve(adapter.release()).then(r=>finish(r.reason,r),()=>finish('CAPTURE_CLEANUP_UNKNOWN'));
  return closed.promise;
 }
 Promise.resolve().then(async()=>{
  if(prevProvider){try{await prevProvider;}catch(_){}}
  let secret=null;
  try{secret=config.getTlsConfig();transport=await connect(secret);}
  finally{if(secret)for(const k of ['ca','certificate','privateKey','deviceKey'])if(Buffer.isBuffer(secret[k]))secret[k].fill(0);secret=null;}
  if(ending){await transport.close();await transport.closed;finish('CAPTURE_CANCELLED');return;}
  adapter=createPcmAdapter(transport);
  adapter.closed.then(r=>finish(r.reason,r),()=>finish('CAPTURE_CLEANUP_UNKNOWN'));
  readyTimer=setTimeout(()=>void cancel('PROVIDER_START_TIMEOUT'),3000);
  try{
   provider=adapter.start((hooks,done)=>{
    // Started from the launch call on. Only the provider's own typed refusal
    // before spawning proves nothing ran; any other throw stays unknown.
    launchAttempted=true;
    try{
     return launch({...config.provider,onReady:value=>{
      hooks.onReady(value);
      if(ending||!adapter.authenticatedTransportReady){value.stop();return;}
      clearTimeout(readyTimer);ready.resolve(Object.freeze({providerReady:true}));
     },onPcm:audio(hooks.onPcm)},receipt=>{providerReceipt=receipt;providerDone.resolve();if(ended)settle(receipt);done(receipt);});
    }catch(e){if(native.neverSpawned(e))launchAttempted=false;throw e;}
   });
  }catch(_){providerDone.resolve();void cancel('PROVIDER_START_FAILED');}
 }).catch(()=>{providerDone.resolve();if(adapter||transport)void cancel('TRANSPORT_START_FAILED');else finish('TRANSPORT_START_FAILED');});
 return Object.freeze({ready:ready.promise,closed:closed.promise,settled:settled.promise,release,cancel});
}
module.exports={start,evidence,SETTLE_MS,MAX_MS};
