// Test-only anonymous-pipe driver; all HTTP/native/PCM boundaries are inert.
'use strict';
const path=require('path'),readline=require('readline'),EventEmitter=require('events');
const root=__dirname;
const F=require(path.join(root,'ui_capture_fixture')),H=require(path.join(root,'ui_heartbeat'));
let fixture=null,hb=null,serial=0,chain=Promise.resolve();const pending=new Map();
function output(v){process.stdout.write(JSON.stringify(v)+'\n');}
const http={request(options,callback){
 const req=new EventEmitter();req.destroy=()=>{};
 req.end=body=>{const id=++serial;pending.set(id,{callback,req});output({kind:'http',id,packet:JSON.parse(Buffer.from(body).toString('ascii'))});};return req;
}};
async function command(m){
 if(m.op==='init'){
  fixture=F.fixture(m.options||{});let random=0;
  hb=H.create({key:Buffer.alloc(32,255),voiceKey:'e'.repeat(64),address:'192.0.2.2',port:4181,service:fixture.service},{http,randomBytes:n=>Buffer.alloc(n,++random)});
 }else if(m.op==='poll'){const ok=await hb.poll();await F.flush();return {ok,status:fixture.status(),stats:fixture.stats()};}
 else if(m.op==='pcm')fixture.pcm();
 else if(m.op==='hold')fixture.holdResult(m.value);
 else if(m.op==='resolve')fixture.resolveLate();
 else if(m.op==='advance')await fixture.advance(m.value);
 else if(m.op==='close'){if(hb)hb.close();if(fixture)await fixture.close();await F.flush();return {closed:true};}
 else if(m.op==='vector')return {canonical:H.canonical(m.value)};
 else throw Error('OP');
 await F.flush();return {status:fixture.status(),stats:fixture.stats()};
}
const input=readline.createInterface({input:process.stdin});input.on('line',line=>{
 const m=JSON.parse(line);
 if(m.kind==='reply'){
  const p=pending.get(m.id);if(!p)throw Error('MISSING');pending.delete(m.id);
  const reply=new EventEmitter();reply.statusCode=m.status||200;reply.destroy=()=>{};p.callback(reply);
  reply.emit('data',Buffer.from(JSON.stringify(m.packet),'ascii'));reply.emit('end');return;
 }
 chain=chain.then(async()=>{const value=await command(m);output({kind:'result',id:m.id,value});}).catch(e=>{output({kind:'failure',id:m.id,error:e.name+':'+e.message});process.exitCode=1;});
});input.on('close',()=>{if(hb)hb.close();});
