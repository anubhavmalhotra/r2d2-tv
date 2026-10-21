'use strict';
const assert=require('assert'),cp=require('child_process'),path=require('path'),{EventEmitter}=require('events');
const root=process.env.MODULE_DIR||__dirname,M=require(path.join(root,'manifest_transport')),C=require(path.join(root,'capture_child'));
const fields={schema:{min:1,max:1},complete:'boolean',pcmBytes:{min:0,max:320000}},success={schema:1,complete:true,pcmBytes:0};
let groups=0,realChildren=0;
function manifest(spawn,method='addOneManifest'){return M.manifest({executable:'/reviewed/tool',method,manifestPath:'/owned/example.manifest.json'},{spawn});}
function capture(spawn,extra={}){return C.captureChild({executable:'/reviewed/provider',args:[],input:Buffer.from('{}'),deadlineMs:1000,resultFields:fields,...extra},{spawn,validateResult:x=>x.complete});}
function fake(value,options={}){let child,inputRef,stdoutRef;return{spawn(){child=new EventEmitter();child.stdout=new EventEmitter();child.stderr=new EventEmitter();child.stdin={on(){},end(b,callback){inputRef=b;if(callback)callback();}};child.kill=()=>{if(options.closeOnKill)queueMicrotask(()=>emit(true));return true;};function emit(late=false){stdoutRef=Buffer.from(typeof value==='string'?value:JSON.stringify(value));child.stdout.emit('data',stdoutRef);child.stderr.emit('data',Buffer.from('private diagnostic'));if(!options.noStdoutClose)child.stdout.emit('close');child.stderr.emit('close');child.emit('close',options.code||0,null);}
 if(!options.waitForKill)queueMicrotask(()=>emit());return child;},get input(){return inputRef;},get stdout(){return stdoutRef;}};}
function realSpawn(value,order,state){return(_file,_args,options)=>{
 const real=cp.spawn(process.execPath,['-e','process.stdin.resume();process.stdin.on("end",()=>process.stdout.write('+JSON.stringify(JSON.stringify(value))+'));'],{stdio:['pipe','pipe','pipe']});realChildren++;
 const proxy=new EventEmitter();proxy.stdin=real.stdin;proxy.stdout=new EventEmitter();proxy.stderr=new EventEmitter();proxy.kill=s=>real.kill(s);let out=false,err=false,exit=null,sent=false;
 real.stdout.on('data',b=>proxy.stdout.emit('data',b));real.stderr.on('data',b=>proxy.stderr.emit('data',b));
 function joinedActual(){if(sent||!out||!err||!exit)return;sent=true;state.actualClosed=true;
  if(order==='streams-first'){proxy.stdout.emit('close');proxy.stderr.emit('close');proxy.emit('close',exit.code,exit.signal);}
  else{proxy.emit('close',exit.code,exit.signal);queueMicrotask(()=>{assert(!state.resolved,'must wait for both observed pipe-close events');proxy.stderr.emit('close');queueMicrotask(()=>{assert(!state.resolved,'must still wait for stdout close');setTimeout(()=>proxy.stdout.emit('close'),5);});});}}
 real.stdout.on('close',()=>{out=true;joinedActual();});real.stderr.on('close',()=>{err=true;joinedActual();});real.on('close',(code,signal)=>{exit={code,signal};joinedActual();});real.on('error',e=>proxy.emit('error',e));if(options.stdio[0]==='ignore')real.stdin.end();return proxy;
};}
(async()=>{
 for(const order of ['streams-first','child-first'])for(const kind of ['add','remove','capture'].filter(k=>!process.env.JOIN_KIND||(process.env.JOIN_KIND==='capture'?k==='capture':k!=='capture'))){const state={resolved:false,actualClosed:false},spawn=realSpawn(kind==='capture'?success:{returnValue:true},order,state);const p=kind==='capture'?capture(spawn):manifest(spawn,kind==='add'?'addOneManifest':'removeOneManifest');p.then(()=>state.resolved=true);const r=await p;assert(state.actualClosed&&state.resolved&&r.accepted&&r.childClosed&&r.stdoutClosed&&r.stderrClosed);if(kind!=='capture')assert(r.ack);groups++;}
 let f=fake({returnValue:false,errorText:'private path and details'}),r=await manifest(f.spawn);assert(!r.ack&&!JSON.stringify(r).includes('private'));groups++;
 f=fake({returnValue:true},{code:1});r=await manifest(f.spawn);assert(!r.ack&&!r.accepted&&r.resultValid);groups++;
 f=fake('not json');r=await capture(f.spawn);assert(!r.accepted&&r.result===null&&f.input.every(x=>x===0));groups++;
 f=fake({...success,url:'private'});r=await capture(f.spawn);assert(!r.accepted&&r.result===null&&!JSON.stringify(r).includes('private'));groups++;
 f=fake({...success,pcmBytes:320001});r=await capture(f.spawn);assert(!r.accepted);groups++;
 f=fake({...success,complete:false});r=await capture(f.spawn);assert(!r.accepted);groups++;
 f=fake(success,{waitForKill:true,closeOnKill:true});r=await capture(f.spawn,{deadlineMs:10});assert(r.timedOut&&!r.accepted&&r.transportError==='TIMEOUT'&&r.childClosed&&r.stdoutClosed&&r.stderrClosed);groups++;
 f=fake(success,{noStdoutClose:true});r=await capture(f.spawn,{deadlineMs:10});assert(!r.accepted&&!r.stdoutClosed&&r.timedOut&&r.result===null);groups++;
 f=fake('x'.repeat(4097));r=await capture(f.spawn);assert(!r.accepted&&r.transportError==='OUTPUT_LIMIT'&&r.stdoutBytes===4097&&f.stdout.every(x=>x===0));groups++;
 r=await capture(()=>{throw Error('private spawn path');},{deadlineMs:10});assert(!r.accepted&&!r.started&&r.inputWiped&&r.transportError==='SPAWN'&&!JSON.stringify(r).includes('private'));groups++;
 for(const mutate of [x=>{x.raw='private';},x=>{x.pcmBytes=320001;},x=>{x.complete={private:true};}]){f=fake(success);r=await C.captureChild({executable:'/reviewed/provider',args:[],input:Buffer.from('{}'),deadlineMs:1000,resultFields:fields},{spawn:f.spawn,validateResult:x=>{mutate(x);return true;}});assert(!r.accepted&&r.result===null);groups++;}
 f=fake(success);r=await C.captureChild({executable:'/reviewed/provider',args:[],input:Buffer.from('{}'),deadlineMs:1000,resultFields:fields},{spawn:f.spawn,validateResult:x=>{assert(Object.isFrozen(x));try{x.raw='private';}catch(_){}return true;}});assert(r.accepted&&Object.isFrozen(r.result)&&Object.keys(r.result).sort().join(',')==='complete,pcmBytes,schema'&&r.result.pcmBytes===0);groups++;
 assert.throws(()=>C.captureChild({resultFields:{raw:'string'}}),/RESULT_SCHEMA/);groups++;
 console.log(JSON.stringify({groups,localHarmlessChildren:realChildren,tvCalls:0,busCalls:0}));
})().catch(()=>{process.stderr.write('TEST_FAILED\n');process.exitCode=1;});
