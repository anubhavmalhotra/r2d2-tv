'use strict';
const assert=require('assert'),fs=require('fs'),api=require('./ui_qualification');
const provider={moduleSha256:'b'.repeat(64),supervisorSha256:'c'.repeat(64)};
const record={schema:1,owner:'local.owner.r2d2mic',category:'/r2d2-ui',backendProfile:1,providerSha256:'2b9f9e318fba385801e160349159cb3238d1fc370136a385413168170879e28b',...provider,enabledForTargetTrial:true};
function fixture(text=JSON.stringify(record)){
 let file=Buffer.from(text),changed=false,closed=0;const buffers=[];
 const meta={dev:1,ino:2,mode:0o100600,uid:0,gid:0,nlink:1,size:file.length,mtimeMs:1,ctimeMs:1,isFile:()=>true,isDirectory:()=>false,isSymbolicLink:()=>false};
 const dir={...meta,ino:1,mode:0o40700,size:0,isFile:()=>false,isDirectory:()=>true};
 const io={constants:fs.constants,realpathSync:p=>p,lstatSync:p=>p==='/fixture/record'?meta:dir,fstatSync:()=>({...meta,ino:changed?3:meta.ino}),openSync:()=>9,
  closeSync:()=>{closed++;},readSync(fd,b,off,len,pos){buffers.push(b);const n=Math.min(len,Math.max(0,file.length-pos));file.copy(b,off,pos,pos+n);return n;}};
 return{io,meta,dir,buffers,changed:()=>{changed=true;},closed:()=>closed};
}
let groups=0;
function test(fn){fn();groups++;}
test(()=>{const f=fixture();assert(api.qualified('/fixture/record',provider,f.io));assert.equal(f.closed(),1);assert(f.buffers.every(b=>b.every(x=>x===0)));});
for(const [key,value]of [['owner','foreign.service'],['category','/'],['moduleSha256','d'.repeat(64)],['supervisorSha256','d'.repeat(64)],['backendProfile',0],['enabledForTargetTrial',false],['nativeCaptureVerified',true]])test(()=>{const f=fixture(JSON.stringify({...record,[key]:value}));assert(!api.qualified('/fixture/record',provider,f.io));});
for(const change of [f=>{f.meta.mode=0o100644;},f=>{f.meta.nlink=2;},f=>{f.meta.uid=1;},f=>{f.dir.mode=0o40777;},f=>{f.changed();},f=>{f.io.realpathSync=()=>'/other';}])test(()=>{const f=fixture();change(f);assert(!api.qualified('/fixture/record',provider,f.io));assert(f.buffers.every(b=>b.every(x=>x===0)));});
test(()=>{const f=fixture('d'.repeat(64)+'\n');const key=api.readKey('/fixture/record','a'.repeat(64),f.io);assert.equal(key.length,32);assert(key.every(x=>x===0xdd));key.fill(0);assert(f.buffers.every(b=>b.every(x=>x===0)));});
for(const text of ['a'.repeat(64),'0'.repeat(64),'g'.repeat(64),'é'.repeat(64)])test(()=>{const f=fixture(text);assert.throws(()=>api.readKey('/fixture/record','a'.repeat(64),f.io),/UI_QUALIFICATION/);assert(f.buffers.every(b=>b.every(x=>x===0)));});
console.log(JSON.stringify({groups,privateFilesRead:0,nativeCalls:0}));
