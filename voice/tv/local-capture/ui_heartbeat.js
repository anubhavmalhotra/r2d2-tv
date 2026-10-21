'use strict';
// Dedicated authenticated UI control channel. voiceKey routes the request but
// never authenticates it. The independently provisioned key stays in memory.
const crypto=require('crypto'),http=require('http');
const ROOT='R2D2-ui-capture-v1\0';
const STATES=new Set(['idle','unavailable','preparing','listening','finishing','accepted','refused','unknown']);
const IDLE=new Set(['idle','unavailable','accepted','refused','unknown']);
function exact(v,keys){return !!v&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).sort().join(',')===keys.split(',').sort().join(',');}
function hex(v,n){return typeof v==='string'&&v.length===n&&/^[a-f0-9]+$/.test(v);}
function canonical(v){if(v===null||typeof v!=='object')return JSON.stringify(v);if(Array.isArray(v))throw Error('UI_WIRE');return '{'+Object.keys(v).sort().map(k=>JSON.stringify(k)+':'+canonical(v[k])).join(',')+'}';}
function validStatus(v){return exact(v,'available,state,request,started,stopped,cleanupComplete')&&typeof v.available==='boolean'&&STATES.has(v.state)&&(v.request===null||hex(v.request,16))&&['started','stopped','cleanupComplete'].every(k=>typeof v[k]==='boolean');}
function create(config,dependencies={}){
 if(!config||!Buffer.isBuffer(config.key)||config.key.length!==32||!hex(config.voiceKey,64)||!config.service||typeof config.service.uiCommand!=='function'||typeof config.service.uiStatus!=='function')throw Error('UI_WIRE_CONFIG');
 const net=require('net');if(net.isIP(config.address)!==4||!Number.isInteger(config.port)||config.port<1024||config.port>65535)throw Error('UI_WIRE_CONFIG');
 const network=dependencies.http||http,random=dependencies.randomBytes||crypto.randomBytes,later=dependencies.setTimeout||setTimeout,clear=dependencies.clearTimeout||clearTimeout;
 const key=crypto.createHmac('sha256',config.key).update(ROOT+'key','ascii').digest();
 const owner=random(16).toString('hex');if(!hex(owner,32)){key.fill(0);throw Error('UI_WIRE_CONFIG');}
 let seq=0,session=null,pending=null,closed=false;
 function mac(label,value){return crypto.createHmac('sha256',key).update(ROOT+label+'\0','ascii').update(canonical(value),'ascii').digest();}
 function cancelUi(){try{const s=config.service.uiStatus();if(s&&s.request&&['preparing','listening','finishing'].includes(s.state))config.service.uiCommand({request:s.request,action:'cancel'});}catch(_){}}
 function poll(){
  if(closed||pending)return Promise.resolve(false);
  if(seq>=2147483647){close();return Promise.resolve(false);}
  let status,nonce;
  try{status=config.service.uiStatus();if(!validStatus(status))throw Error();nonce=random(8).toString('hex');if(!hex(nonce,16))throw Error();}catch(_){close();return Promise.resolve(false);}
  const requestValue={schema:1,owner,session,seq:++seq,nonce,status};
  const signature=mac('request',requestValue);const body=Buffer.from(canonical({...requestValue,mac:signature.toString('hex')}),'ascii');signature.fill(0);
  return new Promise(resolve=>{
   let request=null,response=null,ended=false,total=0,timer=null;const chunks=[];
   function finish(ok){
    if(ended)return;ended=true;if(timer!==null)clear(timer);pending=null;
    for(const chunk of chunks)chunk.fill(0);body.fill(0);
    if(request)request.destroy();if(response)response.destroy();
    if(!ok)cancelUi();resolve(ok);
   }
   pending=()=>finish(false);timer=later(()=>finish(false),1500);
   try{
    request=network.request({host:config.address,port:config.port,path:'/'+config.voiceKey+'/ui-capture',method:'POST',headers:{Host:config.address+':'+config.port,'Content-Type':'application/json','Content-Length':body.length}},reply=>{
     response=reply;if(ended){response.destroy();return;}
     if(reply.statusCode!==200){finish(false);return;}
     reply.on('error',()=>finish(false));reply.on('close',()=>finish(false));
     reply.on('data',chunk=>{
      try{if(ended)return;total+=chunk.length;if(total>4096){finish(false);return;}chunks.push(Buffer.from(chunk));}finally{chunk.fill(0);}
     });
     reply.on('end',()=>{
      if(ended)return;let raw=null,expected=null,provided=null;
      try{
       raw=Buffer.concat(chunks);if(raw.some(byte=>byte>127))throw Error();const value=JSON.parse(raw.toString('ascii'));
       if(!exact(value,'schema,owner,session,seq,nonce,command,mac')||value.schema!==1||value.owner!==owner||value.seq!==requestValue.seq||value.nonce!==nonce||!(value.session===null||hex(value.session,32))||!hex(value.mac,64))throw Error();
       const unsigned={schema:value.schema,owner:value.owner,session:value.session,seq:value.seq,nonce:value.nonce,command:value.command};
       expected=mac('response',unsigned);provided=Buffer.from(value.mac,'hex');if(!crypto.timingSafeEqual(expected,provided))throw Error();
       if(value.command!==null&&(!exact(value.command,'request,action')||!hex(value.command.request,16)||!['start','release','cancel'].includes(value.command.action)))throw Error();
       const current=config.service.uiStatus();if(!validStatus(current))throw Error();
       if(value.session!==session){
        // Echo a changed broker binding on a later poll before executing any
        // command. A released/unfinished utterance cannot move to a new broker.
        if(!current.cleanupComplete||!IDLE.has(current.state)||value.command!==null)throw Error();
        session=value.session;
       }else if(value.command!==null){
        if(session===null)throw Error();config.service.uiCommand(value.command);
       }
       finish(true);
      }catch(_){finish(false);}finally{if(raw)raw.fill(0);if(expected)expected.fill(0);if(provided)provided.fill(0);}
     });
    });
    request.on('error',()=>finish(false));request.on('close',()=>finish(false));request.end(body);
   }catch(_){finish(false);}
  });
 }
 function close(){if(closed)return;closed=true;if(pending)pending();cancelUi();key.fill(0);}
 return Object.freeze({poll,close,enabled:()=>!closed});
}
module.exports={create,canonical,validStatus};
