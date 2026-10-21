'use strict';
const assert=require('assert'),crypto=require('crypto'),EventEmitter=require('events'),{create,canonical}=require('./ui_heartbeat');
const ROOT='R2D2-ui-capture-v1\0',key=Buffer.alloc(32,17),voiceKey='a'.repeat(64),binding='b'.repeat(32);
const derived=crypto.createHmac('sha256',key).update(ROOT+'key','ascii').digest();
function sign(kind,value){return crypto.createHmac('sha256',derived).update(ROOT+kind+'\0','ascii').update(canonical(value),'ascii').digest('hex');}
function fixture(){
 let n=0,command=null,alter=v=>v,hold=false;const timers=new Map(),commands=[],requests=[],buffers=[];
 let status={available:true,state:'idle',request:null,started:false,stopped:false,cleanupComplete:true};
 const service={uiStatus:()=>({...status}),uiCommand:value=>{commands.push(value);if(value.action==='start')status={...status,state:'preparing',request:value.request,cleanupComplete:false};if(value.action==='cancel')status={...status,state:'refused',cleanupComplete:true};}};
 const client=create({key,address:'198.51.100.20',port:4181,voiceKey,service},{randomBytes:len=>Buffer.alloc(len,++n),setTimeout:(fn,ms)=>{const id=++n;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),http:{request(options,callback){
  assert.equal(options.path,'/'+voiceKey+'/ui-capture');assert.equal(options.method,'POST');
  const request=new EventEmitter();request.destroy=()=>{};request.end=body=>{
   buffers.push(body);const value=JSON.parse(body.toString());const unsigned={...value};delete unsigned.mac;assert.equal(value.mac,sign('request',unsigned));requests.push(value);
   request.reply=()=>queueMicrotask(()=>{
    const response=new EventEmitter();response.destroy=()=>{};response.statusCode=200;callback(response);
    let row=alter({schema:1,owner:value.owner,session:binding,seq:value.seq,nonce:value.nonce,command});
    row={...row,mac:sign('response',row)};if(f.badMac)row.mac='0'.repeat(64);
    const raw=Buffer.from(canonical(row),'ascii');buffers.push(raw);response.emit('data',raw);response.emit('end');
   });if(!hold)request.reply();
  };return request;
 }}});
 const f={client,commands,requests,buffers,timers,badMac:false,setCommand:v=>{command=v;},setAlter:v=>{alter=v;},setStatus:v=>{status={...status,...v};},setHold:v=>{hold=v;},status:()=>status};return f;
}
let groups=0;async function test(fn){await fn();groups++;}
(async()=>{
 await test(async()=>{const f=fixture();assert(await f.client.poll());assert.equal(f.requests[0].session,null);f.setCommand({request:'1234567890abcdef',action:'start'});assert(await f.client.poll());assert.equal(f.requests[1].session,binding);assert.deepEqual(f.commands,[{request:'1234567890abcdef',action:'start'}]);assert(f.buffers.every(b=>b.every(x=>x===0)));assert.equal(f.timers.size,0);f.client.close();});
 for(const change of [v=>({...v,owner:'c'.repeat(32)}),v=>({...v,nonce:'c'.repeat(16)}),v=>({...v,seq:v.seq+1}),v=>({...v,extra:true}),v=>({...v,command:{request:'1234567890abcdef',action:'start',extra:true}})])await test(async()=>{const f=fixture();await f.client.poll();f.setStatus({state:'listening',request:'1234567890abcdef',cleanupComplete:false});f.setAlter(change);assert.equal(await f.client.poll(),false);assert.equal(f.commands.at(-1).action,'cancel');f.client.close();});
 await test(async()=>{const f=fixture();await f.client.poll();f.setStatus({state:'listening',request:'1234567890abcdef',cleanupComplete:false});f.badMac=true;assert.equal(await f.client.poll(),false);assert.equal(f.commands.at(-1).action,'cancel');f.client.close();});
 await test(async()=>{const f=fixture();f.setCommand({request:'1234567890abcdef',action:'start'});assert.equal(await f.client.poll(),false);assert.equal(f.commands.length,0);f.client.close();});
 await test(async()=>{const f=fixture();await f.client.poll();f.setAlter(v=>({...v,session:'c'.repeat(32)}));f.setStatus({state:'finishing',request:'1234567890abcdef',cleanupComplete:false});assert.equal(await f.client.poll(),false);assert.equal(f.commands.at(-1).action,'cancel');f.client.close();});
 await test(async()=>{const f=fixture();await f.client.poll();f.setAlter(v=>({...v,session:'c'.repeat(32)}));assert(await f.client.poll());f.setCommand({request:'1234567890abcdef',action:'start'});assert(await f.client.poll());assert.equal(f.requests.at(-1).session,'c'.repeat(32));assert.equal(f.commands.at(-1).action,'start');f.client.close();});
 await test(async()=>{const f=fixture();f.setHold(true);const wait=f.client.poll();assert.equal(await f.client.poll(),false);assert.equal(f.requests.length,1);assert.equal([...f.timers.values()][0].ms,1500);[...f.timers.values()][0].fn();assert.equal(await wait,false);assert.equal(f.timers.size,0);f.client.close();});
 await test(async()=>{const f=fixture();f.setHold(true);const wait=f.client.poll();f.client.close();assert.equal(await wait,false);assert.equal(f.client.enabled(),false);assert.equal(await f.client.poll(),false);assert(f.buffers.every(b=>b.every(x=>x===0)));});
 console.log(JSON.stringify({groups,networkCalls:0,nativeCalls:0}));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});
