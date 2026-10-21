'use strict';
const assert=require('assert'),crypto=require('crypto');
const {_descriptor:descriptor,validateOriginalFd,_protectedParents:protectedParents}=require('./supervised_provider');
const bytes=Buffer.from('held original image fixture'),expected=crypto.createHash('sha256').update(bytes).digest('hex');
function fixture(change={}){
 const stat={dev:1,ino:2,mode:0o100755,uid:0,gid:0,nlink:1,size:bytes.length,mtimeMs:4,ctimeMs:5,isFile:()=>true,...change.stat};
 let reads=0,stats=0;
 return{fstatSync(){stats++;return{...stat,...(stats>1?change.after:{})};},
  readlinkSync(){return'/held/original';},readFileSync(){return change.info||'pos:\t0\nflags:\t0100000\n';},
  readSync(fd,b,o,n,p){assert.equal(fd,9);assert.equal(p,reads);const count=Math.max(0,Math.min(n,bytes.length-p));bytes.copy(b,o,p,p+count);reads+=count;return count;}};
}
let groups=0;
const a=descriptor(9,expected,fixture());assert.equal(a.ino,2);assert(Object.isFrozen(a));groups++;
assert.throws(()=>descriptor(9,'0'.repeat(64),fixture()),/OWNER_SUPERVISION_UNQUALIFIED/);groups++;
for(const info of ['flags:\t0100002\n','flags:\t0100001\n','flags:\t010000000\n','']){
 const io=fixture();io.readFileSync=()=>info;assert.throws(()=>descriptor(9,expected,io),/OWNER_SUPERVISION_UNQUALIFIED/);
}groups++;
for(const stat of [{uid:501},{gid:20},{nlink:2},{mode:0o100777},{isFile:()=>false},{size:1048577}])assert.throws(()=>descriptor(9,expected,fixture({stat})),/OWNER_SUPERVISION_UNQUALIFIED/);groups++;
for(const after of [{ino:6},{size:bytes.length+1},{mtimeMs:6},{ctimeMs:6}])assert.throws(()=>descriptor(9,expected,fixture({after})),/OWNER_SUPERVISION_UNQUALIFIED/);groups++;
for(const fd of [0,1,2,-1,1.5,1048576,'9'])assert.throws(()=>validateOriginalFd(fd),/OWNER_SUPERVISION_UNQUALIFIED/);groups++;
// Each validation reads by explicit position; the caller-owned descriptor's
// shared current offset is neither relied on nor moved between presses.
for(let i=0;i<2;i++)assert.equal(descriptor(9,expected,fixture()).size,bytes.length);groups++;
const parents=[];
protectedParents('/root/private/module.so',{realpathSync:p=>p,lstatSync:p=>{parents.push(p);return{isDirectory:()=>true,isSymbolicLink:()=>false,uid:0,mode:0o40755};}});
assert.deepEqual(parents,['/root/private','/root','/']);groups++;
for(const unsafe of ['/root/private','/root','/']){
 const io={realpathSync:p=>p,lstatSync:p=>({isDirectory:()=>true,isSymbolicLink:()=>false,uid:0,mode:p===unsafe?0o40777:0o40755})};
 assert.throws(()=>protectedParents('/root/private/module.so',io),/OWNER_SUPERVISION_UNQUALIFIED/);
}groups++;
for(const change of [{uid:501},{isSymbolicLink:()=>true},{isDirectory:()=>false}])assert.throws(()=>protectedParents('/root/private/module.so',{realpathSync:p=>p,lstatSync:()=>({isDirectory:()=>true,isSymbolicLink:()=>false,uid:0,mode:0o40755,...change})}),/OWNER_SUPERVISION_UNQUALIFIED/);groups++;
console.log(JSON.stringify({groups,nativeCalls:0,modelCalls:0}));
