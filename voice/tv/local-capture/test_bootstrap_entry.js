'use strict';
// Actual facade in a CommonJS VM; no native addon, config, FD or target access.
const fs=require('fs'),vm=require('vm'),Module=require('module'),assert=require('assert');
const BOOT='/usr/palm/services/jsservicelauncher/bootstrap-node.js';
const DIR='/var/lib/local.owner.r2d2mic/local-capture';
const source=fs.readFileSync(require('path').join(__dirname,'bootstrap_entry.js'),'utf8');
async function scenario(mode){
 const calls=[],proc={exitCode:undefined},main={filename:BOOT},mod={exports:{},parent:{filename:BOOT}};
 const configPath=DIR+(mode==='official-alt'?'/private/other.json':'/config.json'),heldFd=mode==='official-alt'?19:7;
 function req(name){calls.push(name);if(name==='./bus_context')return{entryArguments(){return{configPath,heldOriginalFd:heldFd};}};
  if(name==='./main')return{run(config,fd){calls.push([config,fd]);if(mode==='reject')return Promise.reject(Error('PRIVATE_ERROR'));return Promise.resolve();}};
  throw Error('UNEXPECTED_REQUIRE');}
 req.main=mode==='ordinary-import'?{filename:'/host/test.js'}:mode==='direct'?mod:main;
 if(mode==='wrong-parent')mod.parent={filename:'/other/loader.js'};
 const wrapped=vm.runInNewContext(Module.wrap(source),{Promise,process:proc,Error},{timeout:1000});
 if(mode==='wrong-parent')assert.throws(()=>wrapped(mod.exports,req,mod,'/owned/bootstrap_entry.js','/owned'),/LOCAL_CAPTURE_BUS_CONTEXT/);
 else wrapped(mod.exports,req,mod,'/owned/bootstrap_entry.js','/owned');
 await Promise.resolve();await Promise.resolve();
 assert.deepEqual(Object.keys(mod.exports),[]);assert.equal('run' in mod.exports,false);
 if(['ordinary-import','direct','wrong-parent'].includes(mode)){assert.deepEqual(calls,[]);assert.equal(proc.exitCode,mode==='direct'?1:undefined);}
 else{assert.deepEqual(calls,['./bus_context','./main',[configPath,heldFd]]);assert.equal(proc.exitCode,mode==='reject'?1:undefined);}
}
(async()=>{for(const mode of ['ordinary-import','direct','wrong-parent','official','official-alt','reject'])await scenario(mode);console.log(JSON.stringify({groups:6,nativeCalls:0}));})().catch(e=>{console.error(e);process.exitCode=1;});
