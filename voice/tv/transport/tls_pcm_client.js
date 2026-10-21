'use strict';
// Memory-only, single-utterance TLS 1.3 client. No file, DNS, HTTP or logging API.
const tls=require('tls'),net=require('net'),crypto=require('crypto');
// Post-release only: admission30 + browser executor155 + feedback9 + send2
// seconds, with4 seconds framing/scheduling margin. Audio/auth limits are separate.
const RATE=16000,MAX_AUDIO=320000,MAX_CHUNK=3200,MAX_CHUNKS=500,MAX_PENDING=65536,RESULT_TIMEOUT_MS=200000;
// Hello to release flushed: the 9.2 s hold, its 2 s stop bound, the provider reap and
// margin, 1 s under Spark's 16 s AUDIO_DEADLINE (voice/backend/ingress.py).
const AUDIO_MS=15000;
const DOMAIN=Buffer.from('R2D2-PTT-v1\0','ascii');
function fault(code){const e=new Error(code);e.code=code;return e;}
function localAddress(address){
 if(net.isIP(address)===4){const p=address.split('.').map(Number);return p[0]===10||p[0]===127||p[0]===192&&p[1]===168||p[0]===172&&p[1]>=16&&p[1]<=31;}
 if(net.isIP(address)===6){const a=address.toLowerCase();return a==='::1'||/^(fc|fd)[0-9a-f]{2}:/.test(a);}
 return false;
}
function config(x){
 const keys=['address','port','serverName','ca','certificate','privateKey','serverCertificateSha256','deviceKey'];
 if(!x||typeof x!=='object'||Array.isArray(x)||Object.keys(x).sort().join()!==keys.sort().join()||!localAddress(x.address)||!Number.isInteger(x.port)||x.port<1||x.port>65535||typeof x.serverName!=='string'||x.serverName.length>253||!(/^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$/.test(x.serverName)||net.isIP(x.serverName))||typeof x.serverCertificateSha256!=='string'||!/^[a-f0-9]{64}$/.test(x.serverCertificateSha256)||!Buffer.isBuffer(x.deviceKey)||x.deviceKey.length!==32)throw fault('CLIENT_CONFIG');
 for(const k of ['ca','certificate','privateKey'])if(!Buffer.isBuffer(x[k])||x[k].length<1||x[k].length>65536)throw fault('CLIENT_CONFIG');
 return Object.freeze({...x});
}
function connect(options,hooks={}){
 let cfg;try{cfg=config(options);if(!hooks||Object.keys(hooks).some(k=>!['onReady','onError','onClose'].includes(k))||Object.values(hooks).some(v=>typeof v!=='function'))throw fault('CLIENT_CONFIG');}catch(e){return Promise.reject(fault('CLIENT_CONFIG'));}
 return new Promise((resolve,reject)=>{
  let socket=null,state='connecting',ready=false,readySettled=false,ended=false,terminal=null,errorCode=null,sequence=0,chunks=0,total=0,pending=0,writing=false,current=null,serverBytes=0,tlsAuthenticated=false,outcome=null,terminalFlushed=false;
  let key=Buffer.from(cfg.deviceKey),challenge=Buffer.alloc(32),challengeUsed=0,connectTimer=null,lifetimeTimer=null,closeTimer=null;
  let resultBuffer=Buffer.alloc(260),resultUsed=0,resultLength=null,terminalWriting=null,pendingOutcome=null;
  const queue=[],watchers=new Set();let resolveClosed;const closed=new Promise(r=>{resolveClosed=r;});
  function notify(name,value){for(const listener of [hooks,...watchers])if(typeof listener[name]==='function'){try{listener[name](value);}catch(_){if(name==='onReady')abort('CLIENT_CALLBACK_FAILED');}}}
  function wipeSecrets(){if(key){key.fill(0);key=null;}if(challenge){challenge.fill(0);challenge=null;}}
  function clearQueued(code){while(queue.length){const item=queue.shift();if(item.pcm){pending-=item.pcm.length;item.pcm.fill(0);}if(item.reject)item.reject(fault(code));}}
  function settleClosed(){
   if(ended)return;ended=true;ready=false;state='closed';pendingOutcome=null;terminalWriting=null;clearTimeout(connectTimer);clearTimeout(lifetimeTimer);clearTimeout(closeTimer);wipeSecrets();resultBuffer.fill(0);clearQueued(errorCode||'CLIENT_CLOSED');
   if(current){current.frame.fill(0);current=null;}pending=0;
   if(!readySettled){readySettled=true;reject(fault(errorCode||'TRANSPORT_CLOSED'));}
   const receipt=Object.freeze({reason:errorCode||(terminal==='release'?'released':terminal==='cancel'?'cancelled':'SERVER_REFUSED'),released:terminal==='release'&&terminalFlushed&&!errorCode,cancelled:terminal==='cancel',controlFlushed:terminalFlushed,outcome,actionAcknowledged:!!(outcome&&['accepted','verified'].includes(outcome.state)),closed:true});
   resolveClosed(receipt);notify('onClose',receipt);watchers.clear();
  }
  function abort(code){
   if(ended||errorCode)return;errorCode=code;ready=false;state='closing';pendingOutcome=null;terminalWriting=null;wipeSecrets();clearQueued(code);notify('onError',Object.freeze({code}));
   if(socket)socket.destroy();else settleClosed();
  }
  function completeOutcome(value){
   outcome=Object.freeze({state:value.state,code:value.code});pendingOutcome=null;resultBuffer.fill(0);
   ready=false;state='closing';clearTimeout(lifetimeTimer);clearTimeout(closeTimer);socket.destroy();
  }
  function write(frame){return new Promise((res,rej)=>{
   if(!socket||socket.destroyed){frame.fill(0);return rej(fault('TRANSPORT_CLOSED'));}
   let callback=false,drained=false,done=false,timer;
   function finish(error){if(done)return;done=true;clearTimeout(timer);socket.removeListener('drain',drain);socket.removeListener('close',close);if(callback||socket.destroyed)frame.fill(0);error?rej(fault(error)):res();}
   function check(){if(callback&&drained)finish();}
   function drain(){drained=true;check();}
   function close(){frame.fill(0);finish('TRANSPORT_CLOSED');}
   socket.once('close',close);timer=setTimeout(()=>{abort('TRANSPORT_BACKPRESSURE');finish('TRANSPORT_BACKPRESSURE');},1000);
   try{const accepted=socket.write(frame,e=>{callback=true;frame.fill(0);if(e)finish('TRANSPORT_WRITE_FAILED');else check();});drained=accepted;if(!accepted)socket.once('drain',drain);check();}
   catch(_){frame.fill(0);finish('TRANSPORT_WRITE_FAILED');}
  });}
  async function pump(){
   if(writing||ended)return;writing=true;
   try{while(queue.length&&!ended&&!errorCode){
    const item=queue.shift(),size=item.pcm?item.pcm.length:item.kind==='cancel'?0xffffffff:0;
    const frame=Buffer.alloc(8+(item.pcm?size:0));frame.writeUInt32BE(sequence++,0);frame.writeUInt32BE(size,4);
    if(item.pcm){item.pcm.copy(frame,8);item.pcm.fill(0);}current={frame};
    if(item.kind)terminalWriting=item.kind;
    try{await write(frame);if(ended||errorCode)throw fault(errorCode||'CLIENT_CLOSED');if(item.pcm)pending-=size;if(item.resolve)item.resolve(Object.freeze({flushed:true,actionAcknowledged:false}));}
    catch(e){if(item.reject)item.reject(e);abort(e.code||'TRANSPORT_WRITE_FAILED');break;}
    finally{frame.fill(0);current=null;}
    if(item.kind){
     terminal=item.kind;terminalFlushed=true;terminalWriting=null;ready=false;state='result';clearTimeout(lifetimeTimer);
     if(pendingOutcome)completeOutcome(pendingOutcome);
     else closeTimer=setTimeout(()=>abort('RESULT_UNCONFIRMED'),item.kind==='release'?RESULT_TIMEOUT_MS:2000);
     break;
    }
   }}finally{writing=false;}
  }
  function admit(buffer,wantPromise){
   if(!ready||state!=='ready'||!Buffer.isBuffer(buffer)||buffer.length<2||buffer.length>MAX_CHUNK||buffer.length%2||total+buffer.length>MAX_AUDIO||chunks>=MAX_CHUNKS||pending+buffer.length>MAX_PENDING){abort('PCM_NOT_ADMITTED');return wantPromise?Promise.reject(fault('PCM_NOT_ADMITTED')):false;}
   const pcm=Buffer.from(buffer);chunks++;total+=pcm.length;pending+=pcm.length;let result=true;
   const item={pcm};if(wantPromise)result=new Promise((r,j)=>{item.resolve=r;item.reject=j;});queue.push(item);pump();return result;
  }
  function finish(kind){
   if(ended)return Promise.resolve(Object.freeze({flushed:false,closed:true,actionAcknowledged:false}));
   if(state==='finishing'||state==='closing')return Promise.reject(fault('SESSION_FINISHING'));
   if(!ready||state!=='ready'){abort('TRANSPORT_NOT_READY');return Promise.reject(fault('TRANSPORT_NOT_READY'));}
   if(kind==='release'&&total<640){abort('AUDIO_EMPTY_OR_SHORT');return Promise.reject(fault('AUDIO_EMPTY_OR_SHORT'));}
   state='finishing';ready=false;wipeSecrets();if(kind==='cancel')clearQueued('VOICE_CANCELLED');
   const result=new Promise((r,j)=>queue.push({kind,resolve:r,reject:j}));pump();return result;
  }
  const session=Object.freeze({
   get authenticatedTransportReady(){return ready&&state==='ready'&&!ended;},
   get availablePcmBytes(){return Math.max(0,MAX_PENDING-pending);},
   get acceptedPcmBytes(){return total;},closed,
   sendPcm:b=>admit(b,true),trySendPcm:b=>admit(b,false),release:()=>finish('release'),cancel:()=>finish('cancel'),
   close:()=>{abort('CLIENT_CLOSED');return closed;},
   watch:listener=>{if(!listener||Object.keys(listener).some(k=>!['onError','onClose'].includes(k))||Object.values(listener).some(v=>typeof v!=='function')||watchers.size>=8)throw fault('CLIENT_CALLBACK');if(ended){if(listener.onClose)closed.then(listener.onClose);return()=>{};}watchers.add(listener);return()=>watchers.delete(listener);}
  });
  function pin(cert){return cert&&Buffer.isBuffer(cert.raw)&&crypto.timingSafeEqual(crypto.createHash('sha256').update(cert.raw).digest(),Buffer.from(cfg.serverCertificateSha256,'hex'));}
  try{
   const context=tls.createSecureContext({ca:cfg.ca,cert:cfg.certificate,key:cfg.privateKey,minVersion:'TLSv1.3',maxVersion:'TLSv1.3',secureOptions:crypto.constants.SSL_OP_NO_TICKET});
   socket=tls.connect({host:cfg.address,port:cfg.port,servername:net.isIP(cfg.serverName)?undefined:cfg.serverName,secureContext:context,minVersion:'TLSv1.3',maxVersion:'TLSv1.3',rejectUnauthorized:true,
    checkServerIdentity:(_,cert)=>tls.checkServerIdentity(cfg.serverName,cert)||(!pin(cert)?fault('SERVER_PIN_MISMATCH'):undefined)});
   socket.setNoDelay(true);
   connectTimer=setTimeout(()=>abort('TRANSPORT_CONNECT_TIMEOUT'),5000);
   socket.on('error',()=>abort(tlsAuthenticated?'TRANSPORT_FAILED':'TLS_AUTHENTICATION_FAILED'));
   socket.on('end',()=>{if(!outcome&&!pendingOutcome)abort(terminalFlushed?'RESULT_UNCONFIRMED':'TRANSPORT_EOF');});socket.on('close',settleClosed);
   socket.once('secureConnect',()=>{if(!socket.authorized||socket.getProtocol()!=='TLSv1.3'||tls.checkServerIdentity(cfg.serverName,socket.getPeerCertificate())||!pin(socket.getPeerCertificate()))return abort('TLS_AUTHENTICATION_FAILED');tlsAuthenticated=true;state='challenge';});
   socket.on('data',chunk=>{
    try{
     if(state!=='challenge'){
      // VoiceService sends exactly one bounded canonical terminal Outcome.
      // It may refuse after hello but before capture, so surface it immediately.
      if(!['hello','ready','finishing','result'].includes(state)||resultUsed+chunk.length>resultBuffer.length)return abort('SERVER_PROTOCOL');
      chunk.copy(resultBuffer,resultUsed);resultUsed+=chunk.length;
      if(resultUsed>=4&&resultLength===null){resultLength=resultBuffer.readUInt32BE(0);if(resultLength<1||resultLength>256)return abort('SERVER_PROTOCOL');}
      if(resultLength===null||resultUsed<resultLength+4)return;
      if(resultUsed!==resultLength+4)return abort('SERVER_PROTOCOL');
      let value;try{value=JSON.parse(resultBuffer.subarray(4,resultUsed).toString('utf8'));}catch(_){return abort('SERVER_PROTOCOL');}
      if(!value||Object.keys(value).sort().join()!=='code,schema,state'||value.schema!==1||!['accepted','verified','refused','rejected','unknown'].includes(value.state)||typeof value.code!=='string'||!/^[A-Z][A-Z0-9_]{0,63}$/.test(value.code)||JSON.stringify(value)!==resultBuffer.subarray(4,resultUsed).toString('utf8'))return abort('SERVER_PROTOCOL');
      // An action response before our release is not a valid completion.
      if(value.state!=='refused'&&!(terminal==='release'&&terminalFlushed)&&terminalWriting!=='release')return abort('SERVER_PROTOCOL');
      // A peer may read the terminal frame and reply before Node invokes our
      // write callback. Keep the authenticated response bounded until that
      // write really completes; an unsent queued release cannot reach here.
      if(terminalWriting){pendingOutcome=value;return;}
      completeOutcome(value);return;
     }
     serverBytes+=chunk.length;if(serverBytes>32)return abort('SERVER_PROTOCOL');
     chunk.copy(challenge,challengeUsed);challengeUsed+=chunk.length;if(challengeUsed<32)return;
     const tag=crypto.createHmac('sha256',key).update(DOMAIN).update(challenge).digest('hex');wipeSecrets();state='hello';clearTimeout(connectTimer);
     lifetimeTimer=setTimeout(()=>abort('AUDIO_DEADLINE'),AUDIO_MS);
     const hello=Buffer.from(JSON.stringify({schema:1,mode:'command',rate:RATE,channels:1,format:'s16le',tag}),'utf8');const frame=Buffer.alloc(4+hello.length);frame.writeUInt32BE(hello.length);hello.copy(frame,4);hello.fill(0);
     write(frame).then(()=>{if(ended||errorCode||state!=='hello')return;state='ready';ready=true;notify('onReady',Object.freeze({authenticatedTransportReady:true,controllerArmAcknowledged:false}));if(ready&&!readySettled){readySettled=true;resolve(session);}},()=>abort('TRANSPORT_WRITE_FAILED'));
    }finally{chunk.fill(0);}
   });
  }catch(_){abort('CLIENT_TLS_CONFIG');}
 });
}
module.exports={connect,localAddress,RATE,MAX_AUDIO,MAX_CHUNK,MAX_PENDING,RESULT_TIMEOUT_MS,AUDIO_MS};
