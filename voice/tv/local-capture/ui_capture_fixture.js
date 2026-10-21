'use strict';
// Actual owner arbiter -> UI adapter -> session -> PCM adapter; fake bus/provider/TLS.
const assert=require('assert'),EventEmitter=require('events'),Owner=require('./owner_service');
const tick=()=>new Promise(r=>setImmediate(r));
async function flush(){for(let i=0;i<12;i++)await tick();}
function deferred(){let resolve;const promise=new Promise(r=>{resolve=r;});return{promise,resolve};}
function fixture(options={}){
 let at=0,next=0,active=true,qualified=options.qualified!==false,provider=null,hooks=null,receipt=null,done=null,starts=0,stops=0,releases=0,launched=0,captureAttempted=false,captureStarted=false;
 let remainingStartFailures=options.nativeStartFailures||0;
 const timers=new Map(),calls=[],chunks=[],secrets=[],handle=new EventEmitter();handle.registerMethod=()=>{};
 const later=(fn,ms)=>{const id=++next;timers.set(id,{at:at+ms,fn});return id;},clear=id=>timers.delete(id);
 const transports=[];function makeTransport(){const end=deferred();let ended=false;
 const transport={authenticatedTransportReady:true,availablePcmBytes:65536,closed:end.promise,watch(){return()=>{};},trySendPcm(b){chunks.push(Buffer.from(b));return true;},
  release(){releases++;if(!options.holdResult&&!ended){ended=true;this.authenticatedTransportReady=false;end.resolve(options.resultUnknown?{released:true,reason:'RESULT_UNCONFIRMED',actionAcknowledged:false,outcome:options.resultUnknown==='terminal'?{state:'unknown',code:'COMMAND_UNCONFIRMED'}:null}:{released:true,reason:'released',actionAcknowledged:true,outcome:{state:'accepted',code:'BROWSER_NAVIGATION_ACCEPTED'}});}return end.promise;},
  cancel(){if(!ended){ended=true;this.authenticatedTransportReady=false;end.resolve(options.cancelLateAccepted?{released:true,reason:'released',actionAcknowledged:true,outcome:{state:'accepted',code:'BROWSER_NAVIGATION_ACCEPTED'}}:{released:false,reason:'cancelled'});}return end.promise;},close(){return this.cancel();}};transports.push({transport,end});return transport;}
 const session={connect:async()=>{if(options.authWait)await options.authWait.promise;return makeTransport();},launch(value,finished){
  launched++;hooks=value;done=finished;receipt=null;captureAttempted=false;captureStarted=false;
  provider={stop(){if(captureAttempted&&!receipt){stops++;receipt={cleanExit:captureStarted&&!options.stopFailed,nativeStartAccepted:captureStarted,nativeStopAccepted:!options.stopFailed,nativeStopFailed:!!options.stopFailed,nativeNeverStarted:false,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}};}},
   async reap(){this.stop();if(!receipt)receipt={cleanExit:false,nativeStartAccepted:false,nativeStopAccepted:false,nativeStopFailed:false,nativeNeverStarted:true,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}};done(receipt);return receipt;}};
  setImmediate(()=>value.onReady(provider));return provider;
 }};
 function nativeCall(uri,raw){
  assert(uri.startsWith('luna://com.webos.service.voiceinput.hidraw/r2d2-ui/'));
  const call=new EventEmitter(),row={method:uri.split('/').pop(),body:JSON.parse(raw),cancelled:false,call};calls.push(row);
  call.cancel=()=>{row.cancelled=true;if(options.cancelFails)throw Error('BUS_CANCEL');};
  row.reply=(value,sender='com.webos.service.voiceinput.hidraw')=>call.emit('response',{senderServiceName:()=>sender,payload:()=>JSON.stringify(value)});
  setImmediate(()=>{
   if(row.cancelled)return;
   if(row.method==='getDevices')row.reply({returnValue:true,devices:options.devices||[{id:'remote7',key:'synthetic',deviceType:'remote'}]},options.foreignReply?'foreign.service':undefined);
   if(row.method==='startStreaming'){
    starts++;captureAttempted=true;
    if(remainingStartFailures>0){
     // Mirrors native Session::begin: a failed native START still requires
     // protective STOP. Its receipt has T but no S; it never emits voice start.
     remainingStartFailures--;provider.stop();row.reply({returnValue:false});return;
    }
    captureStarted=true;if(!options.holdStart){row.reply({returnValue:true,socketPath:'owner-pipe://voice'});setImmediate(()=>row.reply({returnValue:true,id:'remote7',state:'voice start'}));}
   }
   if(row.method==='stopStreaming'){provider.stop();row.reply({returnValue:!options.stopReplyFails});}
  });return call;
 }
 handle.call=nativeCall;handle.subscribe=nativeCall;
 const config={isActive:()=>active,isUiQualified:()=>qualified,getTlsConfig(){const s={};for(const k of ['ca','certificate','privateKey','deviceKey'])s[k]=Buffer.alloc(32,7);secrets.push(s);return s;},provider:{mode:'capture',device:'',backendProfile:1,maxMs:6000}};
 const service=Owner.bind(handle,config,{now:()=>at,setTimeout:later,clearTimeout:clear,session,ui:{now:()=>at,setTimeout:later,clearTimeout:clear,session}});
 function physical(method,request=1,caller='com.webos.starfish.keyfilters'){
  const replies=[];handle.emit('request',{category:()=>'/capture',method:()=>method,senderServiceName:()=>caller,sender:()=>':physical',payload:()=>JSON.stringify({issuedAt:at,request,...(method==='begin'?{deviceId:'physical'}:{})}),respond:raw=>replies.push(JSON.parse(raw))});return replies;
 }
 async function advance(ms){at+=ms;for(let i=0;i<30;i++){const due=[...timers].filter(([,v])=>v.at<=at);if(!due.length)break;for(const [id,v]of due)if(timers.delete(id))v.fn();await flush();}}
 return{service,calls,chunks,secrets,timers,physical,advance,holdResult:v=>{options.holdResult=v;},resolveLate:()=>{const row=transports[transports.length-1];row.end.resolve({released:true,reason:'released',actionAcknowledged:true,outcome:{state:'accepted',code:'BROWSER_NAVIGATION_ACCEPTED'}});},setActive:v=>{active=v;},setQualified:v=>{qualified=v;},pcm:()=>hooks.onPcm(Buffer.alloc(6400,7)),stats:()=>({starts,stops,releases,launched}),
  command:(action,request='1234567890abcdef')=>service.uiCommand({action,request}),status:()=>service.uiStatus(),close:()=>service.close()};
}
module.exports={fixture,flush,deferred};
