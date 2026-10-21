'use strict';
// Uses the genuine owner Handle supplied by main. It neither creates a bus
// identity nor calls the physical keyfilter controller.
const sessions=require('./session');
const SERVICE='com.webos.service.voiceinput.hidraw';
const URI='luna://'+SERVICE+'/r2d2-ui/';
function deferred(){let resolve,reject;const promise=new Promise((a,b)=>{resolve=a;reject=b;});promise.catch(()=>{});return{promise,resolve,reject};}
function selectRemote(value){
 if(!value||value.returnValue!==true||!Array.isArray(value.devices)||value.devices.length>32)throw Error('UI_INVENTORY');
 let selected=null;const seen=new Set();
 for(const d of value.devices){
  if(!d||Array.isArray(d)||typeof d.id!=='string'||seen.has(d.id))throw Error('UI_INVENTORY');seen.add(d.id);
  if(d.deviceType!=='remote')continue;
  if(selected||!/^remote[0-9]{1,8}$/.test(d.id)||typeof d.key!=='string'||d.key.length>128||!/^[\x20-\x7e]*$/.test(d.key))throw Error('UI_INVENTORY');
  selected={id:d.id,key:d.key};
 }
 return selected;
}
function start(handle,config,dependencies={}){
 if(!handle||typeof handle.call!=='function'||typeof handle.subscribe!=='function'||!config||typeof config.isActive!=='function')throw Error('UI_CAPTURE_CONFIG');
 const now=dependencies.now||(()=>{const t=process.hrtime();return t[0]*1000+t[1]/1000000;}),later=dependencies.setTimeout||setTimeout,clear=dependencies.clearTimeout||clearTimeout;
 const began=now(),end=began+10000,ready=deferred(),pending=new Set(),timers=new Set();
 let last=began,selected=null,stage='preparing',started=false,stopped=false,startIssued=false,ending=false,ended=false,tries=0,localCleanupFailed=false;
 let socketAck=false,startAck=false,submitted=false;
 function clock(){const t=now();if(!Number.isFinite(t)||t<last)throw Error('UI_CLOCK');last=t;return t;}
 function active(){try{return config.isActive()===true&&clock()<end;}catch(_){return false;}}
 function timer(fn,ms){const id=later(()=>{timers.delete(id);fn();},ms);timers.add(id);return id;}
 function untimer(id){clear(id);timers.delete(id);}
 const job=(dependencies.start||sessions.start)({getTlsConfig:config.getTlsConfig,provider:{...config.provider,mode:'ui-capture'}},dependencies.session||{});
 function state(value){stage=value;if(typeof config.onState==='function')try{config.onState(status());}catch(_){}}
 function status(){return Object.freeze({state:stage,started,stopped});}
 function invoke(method,body,subscription,callback){
  if(ended)throw Error('UI_CLOSED');
  const due=clock()+2000;let call=null,timeout=null,closed=false;
  function retire(){
   if(closed)return;closed=true;pending.delete(retire);if(timeout!==null)untimer(timeout);
   if(call){call.removeListener('response',response);call.removeListener('error',failure);try{call.cancel();}catch(_){localCleanupFailed=true;}}
  }
  function failure(){retire();void cancel('UI_NATIVE_UNKNOWN');}
  function response(message){
   if(closed||ended)return;
   try{
    if(clock()>=due&&(!subscription||!started)||!active()&&!ending)throw Error('UI_EXPIRED');
    if(!message||typeof message.senderServiceName!=='function'||message.senderServiceName()!==SERVICE||typeof message.payload!=='function')throw Error('UI_PEER');
    const raw=message.payload();if(typeof raw!=='string'||Buffer.byteLength(raw)>8192)throw Error('UI_REPLY');
    const value=JSON.parse(raw);if(!value||Array.isArray(value)||value.returnValue!==true)throw Error('UI_REPLY');
    if(!subscription)retire();callback(value);
   }catch(_){failure();}
  }
  call=handle[subscription?'subscribe':'call'](URI+method,JSON.stringify(body));
  if(!call||typeof call.on!=='function'||typeof call.removeListener!=='function'||typeof call.cancel!=='function')throw Error('UI_CALL');
  pending.add(retire);call.on('response',response);call.on('error',failure);
  timeout=timer(()=>{if(subscription&&started){timeout=null;return;}failure();},2000);
  return retire;
 }
 function stopNative(callback){
  if(!startIssued||!selected)return false;
  invoke('stopStreaming',{id:selected.id,key:selected.key},false,callback);return true;
 }
 function cancel(reason='CAPTURE_CANCELLED'){
  if(ended)return closed;
  if(!ending){ending=true;state('finishing');try{stopNative(()=>{});}catch(_){}}
  ready.reject(Error('UI_CAPTURE_UNAVAILABLE'));
  void job.cancel(reason);return closed;
 }
 function release(){
  if(ended||ending)return closed;
  if(!started||!active())return cancel('RELEASE_BEFORE_READY');
  ending=true;state('finishing');
  try{if(!stopNative(()=>{
   if(ended)return;
   // STOP completed the LS2 conversation. Retire its START subscription before
   // provider exit and the potentially long Spark result wait.
   for(const retire of [...pending])retire();
   if(localCleanupFailed){void job.cancel('UI_NATIVE_CLEANUP_UNKNOWN');return;}
   submitted=true;void job.release();
  }))void job.cancel('UI_STOP_UNKNOWN');}
  catch(_){void job.cancel('UI_STOP_UNKNOWN');}
  return closed;
 }
 function inventory(){
  if(ending||ended)return;if(!active())return void cancel('TV_INACTIVE');
  try{tries++;invoke('getDevices',{},false,value=>{
   if(ending||ended)return;
   try{
    selected=selectRemote(value);
    if(!selected){if(tries<12&&clock()+150<end){timer(inventory,150);return;}throw Error('UI_REMOTE_MISSING');}
    startIssued=true;
    invoke('startStreaming',{id:selected.id,key:selected.key,socketType:'socket',subscribe:true},true,value=>{
     if(ending||ended)return;
     if(value.socketPath!==undefined){if(value.socketPath!=='owner-pipe://voice')throw Error('UI_SOCKET');socketAck=true;}
     if(value.id!==undefined||value.state!==undefined){if(value.id!==selected.id||value.state!=='voice start')throw Error('UI_START');startAck=true;}
     if(socketAck&&startAck&&!started){started=true;state('listening');ready.resolve(Object.freeze({providerReady:true}));}
    });
   }catch(_){void cancel('UI_NATIVE_UNKNOWN');}
  });}catch(_){void cancel('UI_NATIVE_UNKNOWN');}
 }
 const closed=Promise.resolve(job.closed).then(receipt=>{
  ended=true;ending=true;ready.reject(Error('UI_CAPTURE_UNAVAILABLE'));
  for(const retire of [...pending])retire();for(const id of [...timers])untimer(id);
  if(selected){selected.id='';selected.key='';selected=null;}
  started=started||!!(receipt&&receipt.nativeStartAccepted);stopped=!!(receipt&&receipt.nativeStopAccepted);
  const result=Object.freeze({...receipt,uiSubmitted:submitted,nativeStartAccepted:started,cleanupComplete:!!(receipt&&receipt.cleanupComplete===true&&!localCleanupFailed)});
  const knownOutcome=result.outcome&&['accepted','verified','refused','rejected'].includes(result.outcome.state);
  state(result.cleanupComplete&&(!submitted||knownOutcome)?(result.actionAcknowledged===true?'accepted':'refused'):'unknown');
  return result;
 },()=>{
  ended=true;for(const retire of [...pending])retire();for(const id of [...timers])untimer(id);
  if(selected){selected.id='';selected.key='';selected=null;}state('unknown');return Object.freeze({cleanupComplete:false});
 });
 job.ready.then(()=>{if(!ending&&!ended)inventory();},()=>{if(!ended)void cancel('UI_PROVIDER_UNKNOWN');});
 // Only an explicit authenticated release submits this UI utterance. Silence
 // or a lost UI controller cannot turn the deadline into action authority.
 timer(()=>{if(!ending&&!ended)void cancel('CAPTURE_DEADLINE');},5200);
 return Object.freeze({ready:ready.promise,closed,release,cancel,status});
}
module.exports={start,selectRemote,URI};
