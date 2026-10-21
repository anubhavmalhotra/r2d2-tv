'use strict';
// Read-only current-boot migration guard. It creates no retirement evidence,
// signals no process and never repairs a missing/changed record.
const fs=require('fs'),crypto=require('crypto'),path=require('path');
const CODE='LOCAL_CAPTURE_OWNERSHIP';
const RAW='/var/lib/local.owner.r2d2mic';
const NODE='/usr/bin/node';
const BOOTSTRAP='/usr/palm/services/jsservicelauncher/bootstrap-node.js';
// A Node entry may be require-loaded by a shared service worker. It must be
// inert in that host; standalone Node exits naturally without opening handles.
const TOMBSTONES=Object.freeze({sh:'#!/bin/sh\nexit 0\n',bash:'#!/bin/bash\nexit 0\n',node:"'use strict';\nmodule.exports={};\n"});
function failure(){return Object.assign(new Error(CODE),{code:CODE});}
function need(value){if(!value)throw failure();}
function exact(value,keys){return value&&typeof value==='object'&&!Array.isArray(value)&&Object.keys(value).sort().join(',')===keys.slice().sort().join(',');}
function safePath(value){return typeof value==='string'&&value.length<=512&&/^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(value)&&value.split('/').every(p=>p!=='.'&&p!=='..');}
function decimal(value,max=24){return typeof value==='string'&&new RegExp('^[1-9][0-9]{0,'+(max-1)+'}$').test(value);}
function hash(value){return typeof value==='string'&&/^[a-f0-9]{64}$/.test(value);}
function same(a,b){return ['dev','ino','mode','uid','gid','nlink','size','mtimeNs','ctimeNs'].every(k=>String(a[k])===String(b[k]));}
function sameImage(a,b){return String(a.dev)===String(b.dev)&&String(a.ino)===String(b.ino);}
function assertQuiescent(recordPath,dependencies={}){
 try{
  need(Object.keys(dependencies).every(k=>['fs','getuid','platform'].includes(k)));
  const io=dependencies.fs||fs,getuid=dependencies.getuid||(()=>process.getuid()),platform=dependencies.platform||(()=>process.platform);
  need(platform()==='linux'&&getuid()===0&&safePath(recordPath));
  const stat=p=>io.lstatSync(p,{bigint:true});
  function canonical(p){need(io.realpathSync(p)===p);}
  function directory(p,privateOnly=false){canonical(p);const s=stat(p);need(s.isDirectory()&&!s.isSymbolicLink()&&s.uid===0n&&s.gid===0n&&(Number(s.mode)&(privateOnly?0o077:0o022))===0);return s;}
  function regular(s,privateOnly=false){need(s.isFile()&&s.uid===0n&&s.gid===0n&&s.nlink===1n&&(Number(s.mode)&(privateOnly?0o7777:0o6022))===(privateOnly?0o600:0));}
  function parents(p,held=null){let dir=path.dirname(p),depth=0;for(;;){need(++depth<=128);const s=directory(dir);if(held){need(!held.has(dir)||same(held.get(dir),s));held.set(dir,s);}if(dir==='/')return;dir=path.dirname(dir);}}
  function bytes(p,limit,privateOnly=false,proc=false){
   if(!proc){canonical(p);parents(p);}
   const before=stat(p);regular(before,privateOnly);
   need(before.size>=0n&&before.size<=BigInt(limit));
   let fd=null,out=Buffer.alloc(limit+1),used=0;
   try{
    fd=io.openSync(p,io.constants.O_RDONLY|io.constants.O_NOFOLLOW|io.constants.O_NONBLOCK);
    need(same(before,io.fstatSync(fd,{bigint:true})));
    while(used<out.length){const n=io.readSync(fd,out,used,out.length-used,null);need(Number.isInteger(n)&&n>=0&&n<=out.length-used);if(!n)break;used+=n;}
    need(used<=limit&&same(before,io.fstatSync(fd,{bigint:true}))&&same(before,stat(p)));
    if(!proc)need(BigInt(used)===before.size);
    return {data:Buffer.from(out.subarray(0,used)),identity:before};
   }finally{out.fill(0);if(fd!==null)io.closeSync(fd);}
  }
  function pinned(p,wanted,limit){
   canonical(p);parents(p);const before=stat(p);regular(before);need(before.size>0n&&before.size<=BigInt(limit));
   let fd=null,buffer=Buffer.alloc(65536),total=0;const digest=crypto.createHash('sha256');
   try{
    fd=io.openSync(p,io.constants.O_RDONLY|io.constants.O_NOFOLLOW|io.constants.O_NONBLOCK);need(same(before,io.fstatSync(fd,{bigint:true})));
    while(true){const n=io.readSync(fd,buffer,0,buffer.length,null);need(Number.isInteger(n)&&n>=0&&n<=buffer.length);if(!n)break;total+=n;need(total<=limit);digest.update(buffer.subarray(0,n));buffer.fill(0);}
    need(BigInt(total)===before.size&&digest.digest('hex')===wanted&&same(before,io.fstatSync(fd,{bigint:true}))&&same(before,stat(p)));return before;
   }finally{buffer.fill(0);if(fd!==null)io.closeSync(fd);}
  }
  directory(path.dirname(recordPath),true);
  const held=bytes(recordPath,16384,true);let record;
  try{record=JSON.parse(held.data.toString('utf8'));}finally{held.data.fill(0);}
  const cold=record&&record.schema===2&&record.mode==='raw-entry-tombstoned-current-boot';
  need(exact(record,cold?['schema','mode','bootId','mountNamespace','executable','rawEntryPath','entry']:['schema','mode','bootId','oldOwner','executable','rawEntryPath','argv','unit','entry']));
  need((cold||record.schema===1&&record.mode==='retired-raw-hid-owner-current-boot')&&typeof record.bootId==='string'&&/^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(record.bootId));
  if(!cold){
  need(exact(record.oldOwner,['pid','startTicks'])&&decimal(record.oldOwner.pid,10)&&Number(record.oldOwner.pid)>1&&Number(record.oldOwner.pid)<=2147483647&&decimal(record.oldOwner.startTicks));
  }
  need(exact(record.executable,['path','dev','ino','sha256'])&&record.executable.path===NODE&&decimal(record.executable.dev)&&decimal(record.executable.ino)&&hash(record.executable.sha256));
  need(safePath(record.rawEntryPath)&&record.rawEntryPath.startsWith(RAW+'/')&&record.rawEntryPath.endsWith('.js')&&!record.rawEntryPath.includes('/local-capture/'));
  if(!cold){
  need(Array.isArray(record.argv)&&record.argv.length>=1&&record.argv.length<=2);
  for(const args of record.argv){
   need(Array.isArray(args)&&args.every(x=>typeof x==='string'&&x.length<=512&&/^[\x21-\x7e]+$/.test(x))&&args[0]===NODE);
   const direct=args.length===2&&args[1]===record.rawEntryPath;
   const bootstrap=args.length===6&&args[1]===BOOTSTRAP&&args[2]==='--'&&
    (args[3]==='/dev/null'||safePath(args[3])&&args[3].startsWith(RAW+'/'))&&
    (args[4]==='^_~'||safePath(args[4])&&args[4].startsWith(RAW+'/'))&&args[5]===path.dirname(record.rawEntryPath);
   need(direct||bootstrap);
  }
  need(new Set(record.argv.map(a=>JSON.stringify(a))).size===record.argv.length);
  need(exact(record.unit,['path','sha256'])&&safePath(record.unit.path)&&hash(record.unit.sha256)&&/^\/(?:etc|var|usr\/lib)\/systemd\/system\/[A-Za-z0-9_.@-]+\.service$/.test(record.unit.path));
  }
  need(exact(record.entry,['path','sha256','tombstone'])&&safePath(record.entry.path)&&record.entry.path.startsWith('/var/lib/')&&hash(record.entry.sha256)&&Object.prototype.hasOwnProperty.call(TOMBSTONES,record.entry.tombstone));
  if(cold){
   // The actual old JS entry itself is fenced. A sidecar marker, shim, or
   // invented unit does not stop a cached launcher from running old bytes.
   need(record.entry.path===record.rawEntryPath&&record.entry.tombstone==='node');
   need(typeof record.mountNamespace==='string'&&/^mnt:\[[0-9]{1,20}\]$/.test(record.mountNamespace));
   need(io.readlinkSync('/proc/self/ns/mnt')===record.mountNamespace&&io.readlinkSync('/proc/1/ns/mnt')===record.mountNamespace);
  }
  const boot=bytes('/proc/sys/kernel/random/boot_id',64,false,true);
  try{need(boot.data.toString('ascii')===record.bootId+'\n');}finally{boot.data.fill(0);}
  const image=pinned(NODE,record.executable.sha256,64*1024*1024);
  need(String(image.dev)===record.executable.dev&&String(image.ino)===record.executable.ino);
  const entry=bytes(record.entry.path,4096);
  try{need(crypto.createHash('sha256').update(entry.data).digest('hex')===record.entry.sha256&&entry.data.equals(Buffer.from(TOMBSTONES[record.entry.tombstone],'ascii')));}finally{entry.data.fill(0);}
  const unit=cold?null:bytes(record.unit.path,65536);
  if(!cold){
  try{
   need(crypto.createHash('sha256').update(unit.data).digest('hex')===record.unit.sha256);
   const text=unit.data.toString('utf8');need(!text.includes('\0')&&!text.includes('\\\n'));
   const lines=text.split(/\r?\n/).filter(s=>/^\s*ExecStart\s*=/.test(s));need(lines.length===1);
   const command=lines[0].replace(/^\s*ExecStart\s*=\s*/,'').trim();
   const expected=record.entry.tombstone==='node'?NODE+' '+record.entry.path:(record.entry.tombstone==='sh'?'/bin/sh':'/bin/bash')+' '+record.entry.path;
   need(command===expected||record.entry.tombstone!=='node'&&command===record.entry.path);
  }finally{unit.data.fill(0);}
  }
  function start(pid){
   const value=bytes('/proc/'+pid+'/stat',8192,false,true);
   try{const text=value.data.toString('ascii'),at=text.lastIndexOf(')');need(text.startsWith(pid+' (')&&at>=3);const fields=text.slice(at+2).trim().split(/\s+/);need(fields.length>19&&decimal(fields[19]));return fields[19];}finally{value.data.fill(0);}
  }
  const gone=e=>e&&['ENOENT','ESRCH'].includes(e.code);
  const newFiles=new Map(),newParents=new Map();
  function newOwnerPath(p){
   try{
    need(safePath(p)&&p.startsWith(RAW+'/local-capture/'));
    canonical(p);parents(p,newParents);const s=stat(p);regular(s);
    need(!newFiles.has(p)||same(newFiles.get(p),s));newFiles.set(p,s);need(newFiles.size<=128);
   }catch(_){throw failure();} // Missing path is not evidence its PID exited.
  }
  for(let pass=0;pass<2;pass++){
   if(!cold){try{need(start(record.oldOwner.pid)!==record.oldOwner.startTicks);}catch(e){if(!gone(e))throw e;}}
   const names=io.readdirSync('/proc');need(names.length<=8192);const ids=names.filter(x=>/^[1-9][0-9]{0,9}$/.test(x));need(ids.length<=4096);let candidates=0;
   for(const pid of ids){
    let command=null;
    try{
     const directoryInfo=stat('/proc/'+pid);need(directoryInfo.isDirectory()&&!directoryInfo.isSymbolicLink());if(directoryInfo.uid!==0n)continue;
     const executable=io.statSync('/proc/'+pid+'/exe',{bigint:true});const link=io.readlinkSync('/proc/'+pid+'/exe');
     if(!sameImage(executable,image)&&link!==NODE&&link!==NODE+' (deleted)')continue;
     need(++candidates<=64&&sameImage(executable,image)&&link===NODE);
     const before=start(pid);command=bytes('/proc/'+pid+'/cmdline',8192,false,true);need(command.data.length>0&&command.data[command.data.length-1]===0);
     const args=command.data.subarray(0,command.data.length-1).toString('utf8').split('\0');
     if(cold){
      // No old PID/argv is invented after reboot. Refuse any root Node
      // launch that names the legacy package, including unrecognized forms.
      // The separately pinned new owner's closure is under local-capture/.
      const serviceDir=RAW+'/local-capture';
      const ownBootstrap=args.length===9&&args.slice(0,6).join('\0')===
       [NODE,BOOTSTRAP,'--','/dev/null',serviceDir,serviceDir].join('\0')&&
       safePath(args[6])&&args[6].startsWith(serviceDir+'/')&&
       args[7]==='--original-fd'&&/^[1-9][0-9]{0,9}$/.test(args[8])&&
       Number(args[8])>=3&&Number(args[8])<=2147483647;
      if(ownBootstrap){
       // The genuine loader receives the protected service directory twice.
       // Admit only that exact layout, pinning its real entry and package;
       // arbitrary directory arguments and embedded code remain refused.
       newOwnerPath(serviceDir+'/bootstrap_entry.js');
       newOwnerPath(serviceDir+'/package.json');
      }
      for(const arg of args)if(arg.includes(RAW)){
       if(ownBootstrap&&arg===serviceDir)continue;
       newOwnerPath(arg);
      }
     }else need(!record.argv.some(a=>a.length===args.length&&a.every((x,i)=>x===args[i])));
     need(before===start(pid)&&sameImage(executable,io.statSync('/proc/'+pid+'/exe',{bigint:true}))&&io.readlinkSync('/proc/'+pid+'/exe')===link&&same(directoryInfo,stat('/proc/'+pid)));
    }catch(e){if(!gone(e))throw e;}
    finally{if(command)command.data.fill(0);}
   }
  }
  need(same(held.identity,stat(recordPath))&&same(image,stat(NODE))&&same(entry.identity,stat(record.entry.path))&&(cold||same(unit.identity,stat(record.unit.path))));
  for(const [p,s] of newFiles){canonical(p);const current=stat(p);regular(current);need(same(s,current));}
  for(const [p,s] of newParents)need(same(s,directory(p)));
  if(cold)need(io.readlinkSync('/proc/self/ns/mnt')===record.mountNamespace&&io.readlinkSync('/proc/1/ns/mnt')===record.mountNamespace);
  return true;
 }catch(_){throw failure();}
}
module.exports={assertQuiescent,CODE};
