'use strict';
// The genuine vendor bootstrap establishes this package's own ACG context.
// Owner code must not call setAppId: the addon restricts its caller script.
const fs=require('fs'),path=require('path');
const OWNER='local.owner.r2d2mic',BOOT='/usr/palm/services/jsservicelauncher/bootstrap-node.js';
const NODE_PATH='/usr/palm/frameworks:/usr/lib/nodejs';
const PACKAGE=Buffer.from('{\n  "name": "local.owner.r2d2mic",\n  "version": "1.0.0",\n  "main": "bootstrap_entry.js",\n  "private": true\n}\n','ascii');
function failure(){return Object.assign(Error('LOCAL_CAPTURE_BUS_CONTEXT'),{code:'LOCAL_CAPTURE_BUS_CONTEXT'});}
function need(v){if(!v)throw failure();}
function same(a,b){return ['dev','ino','mode','uid','gid','nlink','size','mtimeNs','ctimeNs'].every(k=>String(a[k])===String(b[k]));}
function safePath(p){return typeof p==='string'&&p.length<=512&&/^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(p)&&p.split('/').every(x=>x!=='.'&&x!=='..');}
function entryArguments(dependencies={}){
 let buffer=null,fd=null;
 const io=dependencies.fs||fs,proc=dependencies.process||process;
 try{
  need(Object.keys(dependencies).every(k=>['fs','palmbus','directory','process','mainModule','moduleCache','globals'].includes(k)));
  need(proc.platform==='linux'&&proc.getuid()===0&&proc.getgid()===0&&proc.execPath==='/usr/bin/node');
  const directory=dependencies.directory||__dirname;
  const main=dependencies.mainModule||require.main,cache=dependencies.moduleCache||require.cache,globals=dependencies.globals||global;
  need(safePath(directory)&&main&&main.filename===BOOT&&cache[BOOT]===main);
  need(Array.isArray(proc.argv)&&proc.argv.length===9&&proc.argv.slice(0,6).join('\0')===[proc.execPath,BOOT,'--','/dev/null',directory,directory].join('\0'));
  need(proc.cwd()===directory&&proc.title===OWNER&&!globals.unified_service&&!globals.palmbus);
  need(proc.env&&proc.env.NODE_PATH===NODE_PATH&&!proc.env.NODE_OPTIONS&&!proc.env.LD_PRELOAD&&!proc.env.LD_LIBRARY_PATH);
  need(safePath(proc.argv[6])&&proc.argv[6].startsWith(directory+'/')&&proc.argv[7]==='--original-fd'&&/^[1-9][0-9]{0,9}$/.test(proc.argv[8]));
  const heldOriginalFd=Number(proc.argv[8]);need(Number.isSafeInteger(heldOriginalFd)&&heldOriginalFd>=3&&heldOriginalFd<=2147483647);
  for(const p of ['/usr/palm/services/jsservicelauncher/bootstrap.js','/usr/palm/frameworks/mojoloader.js','/usr/lib/nodejs/pmloglib.node','/usr/lib/nodejs/palmbus.js','/usr/lib/nodejs/webos.node','/usr/lib/nodejs/webos-sysbus.node'])need(cache[p]&&cache[p].filename===p);
  const parents=[];let current=directory;
  for(;;){
   need(io.realpathSync(current)===current);
   const s=io.lstatSync(current,{bigint:true});need(s.isDirectory()&&!s.isSymbolicLink()&&s.uid===0n&&s.gid===0n&&!(Number(s.mode)&0o022));parents.push([current,s]);
   if(current==='/')break;current=path.dirname(current);need(parents.length<=128);
  }
  for(const n of ['roles','services.json','sources.json']){try{io.lstatSync(directory+'/'+n);need(false);}catch(e){if(!e||e.code!=='ENOENT')throw e;}}
  const file=directory+'/package.json';need(io.realpathSync(file)===file);
  const before=io.lstatSync(file,{bigint:true});
  need(before.isFile()&&!before.isSymbolicLink()&&before.uid===0n&&before.gid===0n&&before.nlink===1n&&!(Number(before.mode)&0o6022)&&before.size===BigInt(PACKAGE.length));
  fd=io.openSync(file,io.constants.O_RDONLY|io.constants.O_NOFOLLOW|io.constants.O_NONBLOCK);need(same(before,io.fstatSync(fd,{bigint:true})));
  buffer=Buffer.alloc(PACKAGE.length+1);let used=0;
  while(used<buffer.length){const n=io.readSync(fd,buffer,used,buffer.length-used,used);need(Number.isInteger(n)&&n>=0&&n<=buffer.length-used);if(!n)break;used+=n;}
  need(used===PACKAGE.length&&buffer.subarray(0,used).equals(PACKAGE)&&same(before,io.fstatSync(fd,{bigint:true}))&&same(before,io.lstatSync(file,{bigint:true})));
  for(const [p,s]of parents)need(io.realpathSync(p)===p&&same(s,io.lstatSync(p,{bigint:true})));
  io.closeSync(fd);fd=null;buffer.fill(0);buffer=null;
  return Object.freeze({configPath:proc.argv[6],heldOriginalFd});
 }catch(_){throw failure();}
 finally{if(buffer)buffer.fill(0);if(fd!==null)try{io.closeSync(fd);}catch(_){throw failure();}}
}
function createHandle(dependencies={}){
 try{
  entryArguments(dependencies);
  const palmbus=dependencies.palmbus||require('/usr/lib/nodejs/palmbus');
  const cache=dependencies.moduleCache||require.cache;
  need(palmbus&&typeof palmbus.Handle==='function'&&cache['/usr/lib/nodejs/palmbus.js'].exports===palmbus);
  return new palmbus.Handle(OWNER);
 }catch(_){throw failure();}
}
module.exports={createHandle,entryArguments,OWNER};
