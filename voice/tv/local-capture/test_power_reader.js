'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm'),crypto=require('crypto'),cp=require('child_process'),path=require('path');
const SRC=fs.readFileSync(__dirname+'/power_reader.js','utf8'),CLI='/usr/bin/luna-send',URI='luna://com.webos.service.tvpower/power/getPowerState';
// Public synthetic bytes only. Translate only this fixture's genuine digest
// to the captured image pin, keeping hash-mismatch and byte bounds observable.
// This exercises exact runtime source; it does not prove a native image hash.
const IMAGE=Buffer.alloc(17864,0x42),FIXTURE_SHA=crypto.createHash('sha256').update(IMAGE).digest('hex');
const PIN='e2f8b3f7e6522bbd2352f9e831e90580136709b0e09c4de71ceae4a285c657c2';
const fixtureCrypto={createHash(name){const h=crypto.createHash(name);return{update(b){h.update(b);return this;},digest(encoding){const value=h.digest(encoding);return value===FIXTURE_SHA?PIN:value;}};}};
let hostChildren=0;
async function run(code,option={}){
 let image=Buffer.from(IMAGE),changed=false,spawned=0,fdOpen=false;
 if(option.pin)image[20]^=1;
 function stat(p){const file=p===CLI;return {dev:1n,ino:changed&&file?44n:3n,mode:file?0o100755n:option.ancestor&&p==='/usr/bin'?0o40777n:0o40755n,uid:0n,gid:0n,nlink:1n,size:file?BigInt(image.length):0n,mtimeNs:1n,ctimeNs:1n,isDirectory:()=>!file,isFile:()=>file,isSymbolicLink:()=>false};}
 const fake={constants:fs.constants,realpathSync:p=>p,lstatSync:stat,fstatSync:()=>stat(CLI),openSync(p){assert.equal(p,CLI);assert(!fdOpen);fdOpen=true;return 7;},closeSync(f){assert.equal(f,7);assert(fdOpen);fdOpen=false;},readSync(f,b,o,len,pos){assert.equal(f,7);return image.copy(b,o,pos,pos+len);}};
 const context={Buffer,Number,BigInt,String,Object,Array,Error,Math,Promise,process:{platform:'linux',getuid:()=>0},setTimeout,clearTimeout,module:{exports:{}},require(name){assert.equal(name,'child_process');return{spawn(exe,args,options){spawned++;hostChildren++;assert.equal(exe,CLI);assert.deepEqual(args,['-n','1',URI,'{}']);assert.equal(options.cwd,'/');assert.deepEqual(JSON.parse(JSON.stringify(options.env)),{PATH:'/usr/bin:/bin',LANG:'C'});if(option.change)changed=true;return cp.spawn(process.execPath,['-e',code],options);}};}};
 vm.runInNewContext(SRC,context,{filename:'collector.js'});
 if(option.verifyOnly){let image=null,error=null;try{image=context.module.exports.verifyCli(fake,fixtureCrypto);}catch(e){error=e.message;}assert(!fdOpen);return {image,error,spawned,exports:Object.keys(context.module.exports).sort()};}
 const r=await context.module.exports.collect(fake,fixtureCrypto,Date.now);assert(!fdOpen);return {r,spawned};
}
(async()=>{let groups=0,x;
 x=await run("console.log(JSON.stringify({returnValue:true,state:'Active',privateData:'NEVER_EXPORT'}))");assert(x.r.complete&&x.r.result.returnValue);assert.equal(x.r.result.state,'active');assert(x.r.reaped);assert(!JSON.stringify(x.r).includes('NEVER_EXPORT'));groups++;
 x=await run("console.log(JSON.stringify({returnValue:true,state:'Power Off'}))");assert(x.r.complete);assert.equal(x.r.result.state,'standby');groups++;
 x=await run("console.log(JSON.stringify({returnValue:true,state:'Unrecognized'}))");assert(x.r.complete);assert.equal(x.r.result.state,'unknown');groups++;
 x=await run("process.stderr.write('PRIVATE_STDERR');console.log(JSON.stringify({returnValue:false,errorCode:-1,errorText:'x'.repeat(600),message:'a\\nb'}))");assert(x.r.complete&&!x.r.result.returnValue);assert.equal(x.r.result.errorText.length,512);assert.equal(x.r.result.message,'a b');assert(!JSON.stringify(x.r).includes('PRIVATE_STDERR'));groups++;
 x=await run("console.log(JSON.stringify({returnValue:false,errorText:{secret:'NEVER_EXPORT'},message:7}))");assert(x.r.complete);assert.equal(x.r.result.errorText,null);assert.equal(x.r.result.message,null);groups++;
 x=await run("console.log('[]')");assert.equal(x.r.error,'REPLY_INVALID');assert(x.r.reaped&&!x.r.complete);groups++;
 x=await run("process.stdout.write('X'.repeat(5000));setInterval(()=>{},1000)");assert.equal(x.r.error,'STDOUT_BOUND');assert(x.r.reaped);groups++;
 x=await run("setInterval(()=>{},1000)");assert.equal(x.r.error,'CHILD_DEADLINE');assert(x.r.termSent&&x.r.reaped);groups++;
 x=await run("process.on('SIGTERM',()=>{});setInterval(()=>{},1000)");assert.equal(x.r.error,'CHILD_DEADLINE');assert(x.r.termSent&&x.r.killSent&&x.r.reaped);groups++;
 for(const flag of['pin','ancestor']){x=await run('throw Error()',{[flag]:true});assert.equal(x.spawned,0);assert.equal(x.r.error,'CLI_PIN');}groups++;
 x=await run("console.log(JSON.stringify({returnValue:true,state:'Active'}))",{change:true});assert.equal(x.r.error,'REPLY_INVALID');assert(x.r.reaped&&!x.r.complete);groups++;
 // The same pinned-image check, exported for voice_feedback's toast spawns: no child of its own.
 x=await run('',{verifyOnly:true});assert.equal(x.error,null);assert.equal(x.image.size,17864n);assert.equal(x.spawned,0);
 assert.deepEqual(x.exports,['CLI','CLI_SHA','collect','verifyCli']);
 for(const flag of['pin','ancestor']){x=await run('',{verifyOnly:true,[flag]:true});assert.equal(x.error,'CLI_PIN');assert.equal(x.image,null);assert.equal(x.spawned,0);}groups++;
 console.log(JSON.stringify({groups,actualHostChildren:hostChildren,targetCalls:0}));
})().catch(e=>{console.error(e);process.exitCode=1;});
