'use strict';
// Registers on the SAME owner handle as the standalone mode. No extra identity,
// broad permissions, conductor call or raw-HID reader is created here.
const sessions=require('./session');
let uiCapture=null;try{uiCapture=require('./ui_capture');}catch(_){}
const CALLER='com.webos.starfish.keyfilters';
const METHODS=['begin','release','cancel'];
const REASONS=new Set(['TV_INACTIVE','POWER_UNKNOWN','CLIENT_CLOSED']);
function validCaller(v){return v===CALLER||typeof v==='string'&&/^com\.webos\.surfacemanager\.r2d2_[0-9]{1,20}$/.test(v);}
// A physical press the keyfilter never releases is cancelled after PRESS_MS: longer than the
// keyfilter's 9.2 s hold release plus its 2 s stop bound, and than the provider's own lease
// (maxMs, 10 s) plus supervised teardown (maxMs + 1.5 s). UI capture keeps its own 10 s bound.
const PRESS_MS=13000,UI_PRESS_MS=10000;
function validDevice(v){return typeof v==='string'&&v.length>0&&v.length<=128&&/^[\x20-\x7e]+$/.test(v)||Number.isSafeInteger(v)&&v>=0&&v<=2147483647;}
function bind(handle,config,dependencies={}){
 if(!handle||typeof handle.registerMethod!=='function'||typeof handle.on!=='function'||typeof handle.removeListener!=='function'||!config||typeof config.getTlsConfig!=='function'||typeof config.isActive!=='function')throw Error('OWNER_CAPTURE_CONFIG');
 const start=dependencies.start||sessions.start,now=dependencies.now||Date.now,later=dependencies.setTimeout||setTimeout,clear=dependencies.clearTimeout||clearTimeout,states=new Map();let current=null,closed=false,closePromise=null,unresolved=null;
 // The latch a closed press may still clear with its provider's late receipt.
 let recovering=null;
 let uiLast={state:'idle',request:null,started:false,stopped:false,cleanupComplete:true};
 // Optional physical-press feedback (voice_feedback.js). Fire-and-forget: it is never
 // awaited, and a missing or throwing module cannot change any capture decision.
 const voice=config.feedback&&typeof config.feedback.begin==='function'?config.feedback:null;
 function feedback(name,...args){if(!voice)return null;try{return voice[name](...args);}catch(_){return null;}}
 function active(){try{return config.isActive()===true;}catch(_){return false;}}
 // No new capture starts while unresolved is set. Spark is told why (enums only).
 function latch(receipt){unresolved=receipt||{cleanupComplete:false};feedback('latch','latched',receipt||null);}
 function uiAvailable(){try{return !closed&&!unresolved&&(!current||current.kind==='ui')&&active()&&typeof config.isUiQualified==='function'&&config.isUiQualified()===true;}catch(_){return false;}}
 function uiStatus(){
  const available=uiAvailable();
  if(current&&current.kind!=='ui')return Object.freeze({available:false,state:'unavailable',request:null,started:false,stopped:false,cleanupComplete:false});
  return Object.freeze({available,...uiLast,state:!available&&!current&&uiLast.request===null?'unavailable':uiLast.state});
 }
 function reply(message,value,own=null){try{message.respond(JSON.stringify(value));}catch(_){if(own&&current===own)void own.job.cancel('PARENT_CONTROL_LOST');}}
 function failed(message){reply(message,{returnValue:false,errorCode:-1,errorText:'Local voice unavailable'});}
 function status(){return Object.freeze({mode:'lg-local-capture',state:closed?'closed':unresolved?'unavailable':current?(current.released?'finishing':current.ready?'provider-ready':'preparing'):'idle'});}
 function clearCaptureDeadline(own){if(own.timer!==null){clear(own.timer);own.timer=null;}}
 function cancelActive(reason='TV_INACTIVE'){
  if(!REASONS.has(reason))reason='CLIENT_CLOSED';
  if(current){clearCaptureDeadline(current);if(current.kind==='ui'&&current.actionReleased)current.outcomeUnknown=true;return current.job.cancel(reason);}
  return Promise.resolve(unresolved||Object.freeze({cleanupComplete:true,providerStarted:false,providerReaped:false}));
 }
 function uiCommand(body){
  if(!body||Array.isArray(body)||Object.keys(body).sort().join(',')!=='action,request'||!['start','release','cancel'].includes(body.action)||typeof body.request!=='string'||!/^[a-f0-9]{16}$/.test(body.request))return uiStatus();
  if(body.action!=='start'){
   if(current&&current.kind==='ui'&&current.id===body.request){
    clearCaptureDeadline(current);current.released=true;uiLast={...uiLast,state:'finishing',cleanupComplete:false};
    if(body.action==='release'&&current.ready&&active()&&!current.actionReleased){current.actionReleased=true;void current.job.release();}
    else if(body.action==='cancel'||!current.actionReleased){current.outcomeUnknown=!!current.actionReleased;void current.job.cancel('CAPTURE_CANCELLED');}
   }else if(!current&&body.request!==uiLast.request){
    // An authenticated cancel/release may overtake START. Keep its tombstone.
    uiLast={state:'refused',request:body.request,started:false,stopped:false,cleanupComplete:true};
   }
   return uiStatus();
  }
  if(body.request===uiLast.request||current||!uiAvailable())return uiStatus();
  let own=null,job;
  try{
   const startUi=dependencies.startUi||uiCapture&&uiCapture.start;if(typeof startUi!=='function')throw Error('UI_CAPTURE_UNAVAILABLE');
   job=startUi(handle,{getTlsConfig:config.getTlsConfig,provider:config.provider,isActive:active,onState(value){
    if(current!==own||!own)return;uiLast={...uiLast,state:own.outcomeUnknown&&['accepted','refused'].includes(value.state)?'unknown':value.state,started:value.started,stopped:value.stopped};
   }},dependencies.ui||{});
  }catch(_){uiLast={state:'refused',request:body.request,started:false,stopped:false,cleanupComplete:true};return uiStatus();}
  own={kind:'ui',peer:null,id:body.request,job,ready:false,released:false,actionReleased:false,outcomeUnknown:false,timer:null};current=own;
  uiLast={state:'preparing',request:body.request,started:false,stopped:false,cleanupComplete:false};
  own.timer=later(()=>{own.timer=null;if(current===own&&!own.released)void job.cancel('CAPTURE_DEADLINE');},UI_PRESS_MS);
  job.ready.then(()=>{
   if(current!==own||closed||own.released||!uiAvailable()){void job.cancel('TV_INACTIVE');return;}
   own.ready=true;uiLast={...uiLast,state:'listening',started:true};
  },()=>{});
  job.closed.then(receipt=>{
   clearCaptureDeadline(own);
   if(!receipt||receipt.cleanupComplete!==true&&(receipt.providerStarted!==false&&receipt.providerReaped!==true||receipt.nativeStopFailed===true))latch(receipt);
   if(current!==own)return;
   const knownOutcome=receipt&&receipt.outcome&&['accepted','verified','refused','rejected'].includes(receipt.outcome.state);
   const known=receipt&&receipt.cleanupComplete===true&&!own.outcomeUnknown&&(!receipt.uiSubmitted||knownOutcome)&&(!receipt.actionAcknowledged||own.actionReleased);
   uiLast={state:known?(receipt.actionAcknowledged===true?'accepted':'refused'):'unknown',request:own.id,started:!!(receipt&&receipt.nativeStartAccepted),stopped:!!(receipt&&receipt.nativeStopAccepted),cleanupComplete:!!(receipt&&receipt.cleanupComplete===true)};
   current=null;
  },()=>{clearCaptureDeadline(own);latch(null);if(current===own){uiLast={...uiLast,state:'unknown',cleanupComplete:false};current=null;}});
  return uiStatus();
 }
 function request(message){
  let method,peer,body,time;
  try{
   if(closed||message.category()!=='/capture'||!METHODS.includes(method=message.method())||!validCaller(message.senderServiceName()))throw Error();
   peer=message.sender();if(typeof peer!=='string'||!peer.length||peer.length>256)throw Error();
   const raw=message.payload();if(typeof raw!=='string'||Buffer.byteLength(raw)>1024)throw Error();
   body=JSON.parse(raw);const keys=method==='begin'?'deviceId,issuedAt,request':'issuedAt,request';time=now();
   if(!Number.isSafeInteger(time)||!body||Array.isArray(body)||Object.keys(body).sort().join(',')!==keys||!Number.isSafeInteger(body.issuedAt)||body.issuedAt<0||time-body.issuedAt<-2000||time-body.issuedAt>2000||!Number.isSafeInteger(body.request)||body.request<1||body.request>2147483647||method==='begin'&&!validDevice(body.deviceId))throw Error();
  }catch(_){failed(message);return;}
  // Old bus generations cannot replay after their bounded retention expires:
  // all requests also carry a two-second same-TV timestamp deadline.
  for(const [name,row]of states)if((!current||current.peer!==name)&&time-row.seenAt>10000)states.delete(name);
  if(!states.has(peer)){if(states.size>=64){failed(message);return;}states.set(peer,{last:0,seenAt:time});}
  const record=states.get(peer),last=record.last;record.seenAt=time;
  if(method==='begin'){
   const freshRequest=body.request>last||body.request===1&&last>1000000;
   if(current&&current.kind==='physical'&&freshRequest&&!unresolved&&active()){
    const prev=current;clearCaptureDeadline(prev);current=null;
    try{void prev.job.cancel('SUPERSEDED');}catch(_){}
   }
   if(current||unresolved||!freshRequest||!active()){
    failed(message);
    // A fresh press refused while the TV is on still says why (at most once per 3 s).
    // While a late stop confirmation may still clear the latch, it is only busy.
    if(freshRequest&&active()){
     const off=!!unresolved&&recovering!==unresolved;
     feedback('refused',off?'STOP_UNCONFIRMED':'BUSY');if(off)feedback('latch','latched',unresolved);
    }
    return;
   }
   record.last=body.request;
   let own=null,job;
   // First admitted PCM means the remote's mic is really streaming ("listening").
   const onAudio=()=>{if(own&&current===own&&!own.heard&&!own.released){own.heard=true;feedback('listening',own.press);}};
   try{job=start({getTlsConfig:config.getTlsConfig,provider:config.provider,onAudio},dependencies.session||{});}catch(_){failed(message);return;}
   own={kind:'physical',peer,id:body.request,device:body.deviceId,issuedAt:body.issuedAt,job,ready:false,released:false,heard:false,press:null,timer:null};current=own;
   own.press=feedback('begin');
   // Independent of QML key-up or a caller disappearing. The native provider
   // has its own shorter capture deadline and supervised teardown.
   own.timer=later(()=>{own.timer=null;if(current===own&&!own.released)void job.cancel('CAPTURE_DEADLINE');},PRESS_MS);
   job.ready.then(()=>{
    if(current!==own||closed||!active()){void job.cancel('TV_INACTIVE');failed(message);return;}
    own.ready=true;reply(message,{returnValue:true,request:own.id,providerReady:true},own);
   },()=>failed(message));
   job.closed.then(receipt=>{
    clearCaptureDeadline(own);
    const stuck=!receipt||receipt.cleanupComplete!==true&&(receipt.providerStarted!==false&&receipt.providerReaped!==true||receipt.nativeStopFailed===true);
    const facts={ready:own.ready,heard:own.heard,released:own.released,stuck};
    if(stuck)latch(receipt);if(current===own)current=null;
    if(!stuck||!receipt||!job.settled||typeof job.settled.then!=='function'){feedback('settle',own.press,receipt,facts);return;}
    // The provider's own receipt may still arrive (bounded by the session). Only the
    // same clean test as an on-time receipt clears the latch; anything else keeps it.
    const held=unresolved;recovering=held;
    Promise.resolve(job.settled).then(late=>{
     let clean=null;
     try{const check=late?sessions.evidence(late,true):null;if(check&&check.cleanupComplete===true)clean=Object.freeze({...receipt,...check});}catch(_){clean=null;}
     if(recovering===held)recovering=null;
     if(!clean||unresolved!==held){feedback('settle',own.press,receipt,facts);return;}
     unresolved=null;feedback('latch','cleared',clean);
     feedback('settle',own.press,clean,{...facts,stuck:false,recovered:true});
    },()=>{if(recovering===held)recovering=null;feedback('settle',own.press,receipt,facts);});
   },()=>{clearCaptureDeadline(own);latch(null);if(current===own)current=null;feedback('settle',own.press,null,{ready:own.ready,heard:own.heard,released:own.released,stuck:true});});
   return;
  }
  // A cancel that overtakes begin retires that exact request before any launch.
  if(!current){if(body.request>=last)record.last=body.request;reply(message,{returnValue:true,request:body.request});return;}
  if(current.peer!==peer){failed(message);return;}
  if(current.id!==body.request){
   // Retire a future request whose release overtakes begin, without touching
   // another in-flight utterance or accepting a delayed begin afterward.
   if(body.request>last){record.last=body.request;reply(message,{returnValue:true,request:body.request});}
   else failed(message);
   return;
  }
  const own=current;
  if(method==='release'&&own.ready&&active()){
   // The press lease ends here. Native capture/drain retains its own bounds;
   // the authenticated transport separately bounds the post-release result.
   clearCaptureDeadline(own);own.released=true;void own.job.release();
   if(own.heard)feedback('released',own.press,body.issuedAt-own.issuedAt);
  }else{clearCaptureDeadline(own);void own.job.cancel(method==='release'?'RELEASE_BEFORE_READY':'CAPTURE_CANCELLED');}
  reply(message,{returnValue:true,request:own.id},own);
 }
 for(const method of METHODS)handle.registerMethod('/capture',method);
 handle.on('request',request);
 function close(){if(closePromise)return closePromise;closed=true;handle.removeListener('request',request);closePromise=Promise.resolve(cancelActive('CLIENT_CLOSED'));return closePromise;}
 return Object.freeze({close,cancelActive,status,uiCommand,uiStatus});
}
module.exports={bind,validDevice,PRESS_MS,UI_PRESS_MS};
