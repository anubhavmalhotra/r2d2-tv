'use strict';
// Synthetic protected filesystem and fake addon only. No TV/addon registration.
const fs=require('fs'),path=require('path'),os=require('os'),assert=require('assert');
const DIR='/var/lib/local.owner.r2d2mic/local-capture',OWNER='local.owner.r2d2mic';
const BOOT='/usr/palm/services/jsservicelauncher/bootstrap-node.js';
const PACKAGE=fs.readFileSync(path.join(__dirname,'package.json'));
function fixture(){
 delete require.cache[require.resolve('./bus_context')];const api=require('./bus_context');
 const tmp=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'r2d2-bus-context-'))),map=p=>path.join(tmp,p);
 fs.mkdirSync(map(DIR),{recursive:true,mode:0o700});fs.writeFileSync(map(DIR+'/package.json'),PACKAGE,{mode:0o644});
 const open=new Set(),events=[],buffers=[];
 function root(s){return Object.assign(Object.create(Object.getPrototypeOf(s)),s,{uid:0n,gid:0n});}
 const io={constants:fs.constants,
  realpathSync(p){const q=fs.realpathSync(map(p));return q===tmp?'/':q.slice(tmp.length);},
  lstatSync:(p,o)=>root(fs.lstatSync(map(p),o)),fstatSync:(fd,o)=>root(fs.fstatSync(fd,o)),
  openSync(p,f){const fd=fs.openSync(map(p),f);open.add(fd);return fd;},
  readSync(fd,b,...args){buffers.push(b);return fs.readSync(fd,b,...args);},closeSync(fd){events.push('close-fd');assert(open.delete(fd));fs.closeSync(fd);}};
 const bus={setAppId(){throw Error('OWNER_SETTER_FORBIDDEN');},Handle:function(name){assert.equal(open.size,0);assert.equal(name,OWNER);assert.equal(arguments.length,1);events.push('handle');this.owner=name;}};
 const main={filename:BOOT},cache={[BOOT]:main};
 for(const p of ['/usr/palm/services/jsservicelauncher/bootstrap.js','/usr/palm/frameworks/mojoloader.js','/usr/lib/nodejs/pmloglib.node','/usr/lib/nodejs/palmbus.js','/usr/lib/nodejs/webos.node','/usr/lib/nodejs/webos-sysbus.node'])cache[p]={filename:p};
 cache['/usr/lib/nodejs/palmbus.js'].exports=bus;
 const proc={platform:'linux',getuid:()=>0,getgid:()=>0,execPath:'/usr/bin/node',cwd:()=>DIR,title:OWNER,
  argv:['/usr/bin/node',BOOT,'--','/dev/null',DIR,DIR,DIR+'/config.json','--original-fd','7'],env:{NODE_PATH:'/usr/palm/frameworks:/usr/lib/nodejs'}};
 const deps={fs:io,palmbus:bus,directory:DIR,process:proc,mainModule:main,moduleCache:cache,globals:{}};
 return{api,tmp,map,io,bus,deps,events,open,buffers,run:()=>api.createHandle(deps),close:()=>fs.rmSync(tmp,{recursive:true,force:true})};
}
let groups=0;
function test(fn){const f=fixture();try{fn(f);assert.equal(f.open.size,0);assert(f.buffers.every(b=>b.every(v=>v===0)));groups++;}finally{f.close();}}
function refuse(f){assert.throws(f.run,e=>e.code==='LOCAL_CAPTURE_BUS_CONTEXT'&&e.message===e.code);assert(!f.events.includes('handle'));}
test(f=>{let handle;assert.doesNotThrow(()=>{handle=f.run();},'official bootstrap context must not call an owner setter');assert.equal(handle.owner,OWNER);assert.equal(f.run().owner,OWNER);assert.deepEqual(f.events,['close-fd','handle','close-fd','handle']);assert.deepEqual(f.api.entryArguments(f.deps),{configPath:DIR+'/config.json',heldOriginalFd:7});});
test(f=>{const p=f.map(DIR+'/package.json');fs.writeFileSync(p,PACKAGE.toString().replace(OWNER,'foreign.service'));refuse(f);});
test(f=>{fs.chmodSync(f.map(DIR),0o777);refuse(f);});
test(f=>{fs.chmodSync(f.map('/var/lib'),0o777);refuse(f);});
test(f=>{fs.chmodSync(f.map(DIR+'/package.json'),0o666);refuse(f);});
test(f=>{const p=f.map(DIR+'/package.json');fs.renameSync(p,p+'.copy');fs.symlinkSync('package.json.copy',p);refuse(f);});
test(f=>{fs.linkSync(f.map(DIR+'/package.json'),f.map(DIR+'/other.json'));refuse(f);});
test(f=>{const fn=f.io.lstatSync;f.io.lstatSync=(p,o)=>{const s=fn(p,o);return p.endsWith('/package.json')?Object.assign(Object.create(Object.getPrototypeOf(s)),s,{uid:1n}):s;};refuse(f);});
test(f=>{f.deps.process.getuid=()=>1;refuse(f);});
test(f=>{f.bus.Handle=()=>{throw Error('PRIVATE_ERROR');};refuse(f);});
test(f=>{const fn=f.io.readSync;let first=true;f.io.readSync=(...a)=>{const n=fn(...a);if(first){first=false;fs.chmodSync(f.map(DIR),0o755);}return n;};refuse(f);});
test(f=>{const close=f.io.closeSync;let once=true;f.io.closeSync=fd=>{if(once){once=false;throw Error('PRIVATE_CLOSE');}close(fd);};refuse(f);});
test(f=>{f.deps.mainModule={filename:'/other/bootstrap.js'};refuse(f);});
test(f=>{f.deps.process.argv[1]='/other/bootstrap.js';refuse(f);});
test(f=>{f.deps.process.argv.push('extra');refuse(f);});
test(f=>{f.deps.process.argv[4]='/other/roles';refuse(f);});
test(f=>{f.deps.process.argv[5]='/other/service';refuse(f);});
test(f=>{f.deps.process.argv[6]='/private/../config.json';refuse(f);});
test(f=>{f.deps.process.argv[6]='/private/config.json';refuse(f);});
test(f=>{f.deps.process.argv[6]=DIR+'/private/other.json';f.deps.process.argv[8]='19';assert.deepEqual(f.api.entryArguments(f.deps),{configPath:DIR+'/private/other.json',heldOriginalFd:19});assert.equal(f.run().owner,OWNER);});
test(f=>{f.deps.process.argv[8]='2';refuse(f);});
test(f=>{f.deps.process.argv[8]='2147483648';refuse(f);});
test(f=>{f.deps.process.cwd=()=>'/other';refuse(f);});
test(f=>{f.deps.process.title='foreign.service';refuse(f);});
test(f=>{f.deps.process.env.NODE_PATH='/tmp';refuse(f);});
test(f=>{f.deps.process.env.NODE_OPTIONS='--require=/tmp/code';refuse(f);});
test(f=>{f.deps.process.env.LD_PRELOAD='/tmp/code';refuse(f);});
test(f=>{f.deps.globals.unified_service={};refuse(f);});
test(f=>{f.deps.globals.palmbus=f.bus;refuse(f);});
test(f=>{delete f.deps.moduleCache['/usr/lib/nodejs/webos.node'];refuse(f);});
test(f=>{f.deps.moduleCache['/usr/lib/nodejs/palmbus.js'].exports={};refuse(f);});
for(const name of ['roles','services.json','sources.json'])test(f=>{fs.writeFileSync(f.map(DIR+'/'+name),'foreign');refuse(f);});
console.log(JSON.stringify({groups,nativeCalls:0}));
