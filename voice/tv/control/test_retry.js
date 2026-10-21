'use strict';
// Actual candidate JS receiver and actual Python StopControl, joined by pipes.
// No sockets, TV, Spark, credentials or HID devices. Inert fixed test key only.
const assert=require('assert'),crypto=require('crypto'),path=require('path'),cp=require('child_process'),readline=require('readline');
const C=require('./stop_control');
const KEY=Buffer.alloc(32,7),ATTEMPT='a'.repeat(48);let groups=0;
function clock(){let t=0,id=0;const timers=new Map();return{now:()=>t,setTimeout(fn,ms){timers.set(++id,{fn,at:t+ms});return id;},clearTimeout(i){timers.delete(i);},advance(ms){t+=ms;for(const[i,x]of [...timers])if(x.at<=t){timers.delete(i);x.fn();}},timers};}
function host(){
 const child=cp.spawn(process.env.PYTHON||'python3',['-I','-B','-c','import sys;sys.path.insert(0,sys.argv[1]);import host_fixture;host_fixture.main()',__dirname],{stdio:['pipe','pipe','pipe']});
 const pending=[];let errors='';child.stderr.on('data',b=>{errors+=b;});
 readline.createInterface({input:child.stdout}).on('line',line=>{assert(pending.length);pending.shift()(JSON.parse(line));});
 const closed=new Promise(resolve=>child.on('close',(code,signal)=>resolve({code,signal})));
 return{call(row){return new Promise(resolve=>{pending.push(resolve);child.stdin.write(JSON.stringify(row)+'\n');});},async close(now){const row=await this.call({close:true,now});assert(row.summary.closed&&row.summary.keyCleared);child.stdin.end();const r=await closed;assert.equal(r.code,0);assert.equal(r.signal,null);assert.equal(errors,'');assert.equal(pending.length,0);assert(child.stdout.destroyed&&child.stderr.destroyed);}};
}
async function fixture(){
 const h=host(),k=clock();let mode='ok',cause=null,cleanup=true,lastHost=null,requests=0,cancels=0,state=null;const bodies=[],replies=[];
 const transport={request(body,limits){assert.deepEqual(limits,{timeoutMs:500,maxBytes:512});requests++;bodies.push(body);cause=null;const current=mode;
  let promise;if(current==='stall'){cause='timeout';promise=new Promise(()=>{});}
  else if(current==='lostRequest'){cause='socket';promise=Promise.resolve(null);}
  else {promise=h.call({body:body.toString('base64'),now:k.now(),state}).then(row=>{
   lastHost=row;if(row.error){cause='status';return null;}let b=Buffer.from(row.reply,'base64');
   if(current==='lostReply'){b.fill(0);cause='socket';return null;}
   if(current==='badMac'){const v=JSON.parse(b);b.fill(0);v.mac='0'.repeat(64);b=Buffer.from(JSON.stringify(v));}
   if(current==='replay'){const v=JSON.parse(b);b.fill(0);v.counter--;b=Buffer.from(JSON.stringify(v));}
   if(current==='malformed'){b.fill(0);b=Buffer.from('{}');}
   if(current==='empty'){b.fill(0);b=Buffer.alloc(0);}
   replies.push(b);return b;
  });}
  return{promise,cancel(){cancels++;return cleanup;},diagnostic(){return{status:cause==='status'?400:null,cause};}};
 }};
 const c=C.create(KEY,ATTEMPT,transport,k);
 return{h,k,c,set(m){mode=m;},state(v){state=v;},cleanup(v){cleanup=v;},get lastHost(){return lastHost;},get requests(){return requests;},async poll(m='ok',ms=0){mode=m;k.advance(ms);const p=c.refresh(true);if(m==='stall')k.advance(500);return await p;},async close(){c.close();await new Promise(r=>setImmediate(r));await h.close(k.now());assert(c.summary().closed&&c.summary().keyCleared);assert.equal(k.timers.size,0);assert.equal(cancels,requests);for(const b of bodies.concat(replies))assert(b.every(x=>x===0));}};
}
async function run(fn){const f=await fixture();try{await fn(f);groups++;}finally{await f.close();}}
(async()=>{
 assert.equal(C.GRACE_MS,1500);assert.equal(C.RESPONSE_MS,500);assert.equal(C.LIMIT,384);
 for(const loss of ['lostRequest','lostReply'])await run(async f=>{await f.poll();assert.equal(f.c.remainingMs(),45000);await f.poll(loss,200);assert(!f.c.snapshot().cancelled);assert.equal(f.c.summary().failure,null);assert.equal(f.c.remainingMs(),0);await f.poll('ok',200);assert(!f.c.snapshot().cancelled);assert.equal(f.lastHost.counter,3);assert.equal(f.lastHost.end,45);assert.equal(f.c.remainingMs(),44600);});
 for(const loss of ['lostRequest','lostReply','stall'])await run(async f=>{await f.poll(loss);assert(f.c.snapshot().cancelled);assert(f.c.summary().failure.startsWith('CONTROL_FIRST_'));assert.equal(f.c.remainingMs(),0);});
 await run(async f=>{await f.poll();await f.poll('stall',200);assert.equal(f.k.now(),700);assert(!f.c.snapshot().cancelled);await f.poll('ok');assert(!f.c.snapshot().cancelled);assert.equal(f.c.remainingMs(),44300);});
 await run(async f=>{await f.poll();await f.poll('stall');await f.poll('stall');await f.poll('stall');assert.equal(f.k.now(),1500);assert.equal(f.c.summary().failure,'CONTROL_TIMEOUT');assert(f.c.snapshot().cancelled);const n=f.requests;await f.poll();assert.equal(f.requests,n);});
 await run(async f=>{await f.poll();await f.poll('lostRequest',1501);assert.equal(f.c.summary().failure,'CONTROL_SOCKET');assert(f.c.snapshot().cancelled);});
 await run(async f=>{await f.poll();await f.poll('lostRequest',1499);assert(!f.c.snapshot().cancelled);f.k.advance(1);assert.equal(f.c.remainingMs(),0);assert(f.c.snapshot().cancelled);});
 await run(async f=>{await f.poll();await f.poll('lostRequest',1400);f.set('stall');const p=f.c.refresh(true);assert.equal([...f.k.timers.values()][0].at,1500);f.k.advance(100);await p;assert(f.c.snapshot().cancelled);assert.equal(f.k.now(),1500);});
 await run(async f=>{await f.poll();await f.poll('lostRequest',1200);const p=f.c.refresh(true);await p;assert(!f.c.snapshot().cancelled);});
 for(const bad of ['badMac','replay','malformed','empty'])await run(async f=>{await f.poll();await f.poll(bad,200);assert(f.c.snapshot().cancelled);assert(f.c.summary().failure!==null);assert.equal(f.c.remainingMs(),0);});
 for(const state of [{cancelled:true,reason:'cancel'},{closed:true,reason:'close'},{released:true}])await run(async f=>{await f.poll();await f.poll('lostRequest',100);f.state(state);await f.poll('ok',100);assert.equal(f.c.remainingMs(),0);assert(f.c.snapshot().cancelled||f.c.snapshot().released);});
 await run(async f=>{await f.poll();f.cleanup(false);await f.poll('lostRequest',200);assert.equal(f.c.summary().failure,'CONTROL_CLEANUP');assert(f.c.snapshot().cancelled);});
 await run(async f=>{await f.poll();f.set('stall');const p=f.c.refresh(true);f.c.close();await p;assert(f.c.snapshot().cancelled);assert.equal(f.c.summary().failure,'CONTROL_UNCONFIRMED');});
 await run(async f=>{await f.poll();await f.poll('ok',44000);assert.equal(f.c.remainingMs(),1000);await f.poll('lostRequest',500);assert(!f.c.snapshot().cancelled);f.k.advance(500);assert.equal(f.c.remainingMs(),0);assert.equal(f.c.summary().failure,'CONTROL_EXPIRED');});
 // A valid late reply cannot revive an already expired miss window.
 await run(async f=>{await f.poll();await f.poll('lostRequest',1400);f.k.advance(100);const before=f.requests;await f.poll('ok');assert(f.c.snapshot().cancelled);assert.equal(f.requests,before);});
 console.log(JSON.stringify({groups,actualPythonAndJS:true,networkCalls:0}));
})().catch(e=>{console.error(e);process.exitCode=1;});
