'use strict';
// No listener or automatic activation. This replaces only a fresh owned
// provider's process factory. Its caller still owns conductor/QML rollback.
const fs=require('fs'),crypto=require('crypto'),cp=require('child_process');
const EventEmitter=require('events'),PassThrough=require('stream').PassThrough;
const pathModule=require('path');
const base=require('./legacy_provider');
const ORIGINAL='/usr/sbin/voiceinput_hidraw';
const ORIGINAL_SHA='2b9f9e318fba385801e160349159cb3238d1fc370136a385413168170879e28b';
const LOCK_PATH='/tmp/voiceinput_hidraw.lock';
const KEYS=['schema','stage','errorNumber','fdsVerified','imageVerified','deadlineExpired','ownerLost','gracefulStopRequested','ownerGraceExpired','tasksObserved','deniedSockets','deniedEscape','replacedReturns','recvmsgReplaced','allReaped','rootExit','rootSignal','supervisionComplete'];
const SIGNALS={1:'SIGHUP',2:'SIGINT',3:'SIGQUIT',4:'SIGILL',5:'SIGTRAP',6:'SIGABRT',7:'SIGBUS',8:'SIGFPE',9:'SIGKILL',11:'SIGSEGV',13:'SIGPIPE',14:'SIGALRM',15:'SIGTERM',31:'SIGSYS'};
function fail(){throw Error('OWNER_SUPERVISION_UNQUALIFIED');}
// Minted only by launch() for a refusal before cp.spawn: no process ever existed.
const UNSPAWNED=new WeakSet();
function unspawned(){const e=Error('PROVIDER_NEVER_SPAWNED');UNSPAWNED.add(e);return Object.freeze(e);}
function neverSpawned(error){return typeof error==='object'&&error!==null&&UNSPAWNED.has(error);}
function protectedParents(path,io=fs){
 if(typeof path!=='string'||!path.startsWith('/')||io.realpathSync(path)!==path)fail();
 for(let parent=pathModule.dirname(path);;parent=pathModule.dirname(parent)){
  const a=io.lstatSync(parent);
  if(!a.isDirectory()||a.isSymbolicLink()||a.uid!==0||(a.mode&0o022))fail();
  if(parent==='/')break;
 }
}
function held(path,sha){
 if(typeof path!=='string'||!path.startsWith('/')||fs.realpathSync(path)!==path||typeof sha!=='string'||!/^[a-f0-9]{64}$/.test(sha))fail();
 protectedParents(path);
 const fd=fs.openSync(path,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);let success=false;
 try{const a=fs.fstatSync(fd);if(!a.isFile()||a.uid!==0||(a.mode&0o6022)||a.size<1||a.size>1048576)fail();
  const bytes=fs.readFileSync(fd),b=fs.fstatSync(fd);
  if(bytes.length!==a.size||['dev','ino','size','mtimeMs','ctimeMs'].some(k=>a[k]!==b[k])||crypto.createHash('sha256').update(bytes).digest('hex')!==sha)fail();
  success=true;return fd;
 }finally{if(!success)fs.closeSync(fd);}
}
function descriptor(fd,expected,io=fs){
 if(!Number.isInteger(fd)||fd<3||fd>1048575||typeof expected!=='string'||!/^[a-f0-9]{64}$/.test(expected))fail();
 const a=io.fstatSync(fd),fields=['dev','ino','mode','uid','gid','nlink','size','mtimeMs','ctimeMs'];
 if(!a.isFile()||a.uid!==0||a.gid!==0||a.nlink!==1||(a.mode&0o6022)||a.size<1||a.size>1048576)fail();
 const link=io.readlinkSync('/proc/self/fd/'+fd),info=io.readFileSync('/proc/self/fdinfo/'+fd,'ascii');
 if(typeof link!=='string'||!link.startsWith('/')||link.length>4096||link.includes('\0')||typeof info!=='string'||info.length>4096)fail();
 const flags=/^flags:\s+([0-7]{1,12})$/m.exec(info);
 if(!flags||parseInt(flags[1],8)%4!==0||(parseInt(flags[1],8)&0o10000000))fail();
 const bytes=Buffer.alloc(a.size+1);let used=0;
 try{
  while(used<bytes.length){const n=io.readSync(fd,bytes,used,bytes.length-used,used);if(!Number.isInteger(n)||n<0||n>bytes.length-used)fail();if(!n)break;used+=n;}
  const b=io.fstatSync(fd);
  if(used!==a.size||fields.some(k=>a[k]!==b[k])||io.readlinkSync('/proc/self/fd/'+fd)!==link||crypto.createHash('sha256').update(bytes.subarray(0,used)).digest('hex')!==expected)fail();
  const snapshot={};for(const k of fields)snapshot[k]=a[k];return Object.freeze(snapshot);
 }finally{bytes.fill(0);}
}
function validateOriginalFd(fd){descriptor(fd,ORIGINAL_SHA);return true;}
function validLock(s){return Boolean(s&&typeof s.isFile==='function'&&s.isFile()&&(typeof s.isSymbolicLink!=='function'||!s.isSymbolicLink())&&s.uid===0&&s.gid===0&&s.nlink===1&&s.size===0&&(s.mode&0o6077)===0&&(s.mode&0o600)===0o600);}
function ensureStartupLock(io=fs){
 try{if(validLock(io.lstatSync(LOCK_PATH)))return true;}catch(e){if(!e||e.code!=='ENOENT')fail();}
 try{io.unlinkSync(LOCK_PATH);}catch(e){if(!e||e.code!=='ENOENT')fail();}
 const c=io.constants||fs.constants,fd=io.openSync(LOCK_PATH,c.O_WRONLY|c.O_CREAT|c.O_EXCL|c.O_NOFOLLOW,0o600);
 try{
  if(typeof io.fchmodSync==='function')io.fchmodSync(fd,0o600);
  if(typeof io.fchownSync==='function')io.fchownSync(fd,0,0);
  if(!validLock(io.fstatSync(fd)))fail();
 }finally{io.closeSync(fd);}
 if(!validLock(io.lstatSync(LOCK_PATH)))fail();
 return true;
}
function receipt(raw){
 const x=JSON.parse(raw);
 if(!x||Array.isArray(x)||Object.keys(x).sort().join()!==KEYS.slice().sort().join()||x.schema!==1||!['preflight','descendant','exec-identity','syscall','options','owner-retirement'].includes(x.stage))fail();
 for(const key of ['fdsVerified','imageVerified','deadlineExpired','ownerLost','gracefulStopRequested','ownerGraceExpired','allReaped','supervisionComplete'])if(typeof x[key]!=='boolean')fail();
 for(const key of ['errorNumber','tasksObserved','deniedSockets','deniedEscape','replacedReturns','recvmsgReplaced','rootSignal'])if(!Number.isInteger(x[key])||x[key]<0||x[key]>10000000)fail();
 if(x.tasksObserved>64||x.rootSignal>64||!Number.isInteger(x.rootExit)||x.rootExit< -1||x.rootExit>255)fail();
 if(x.gracefulStopRequested&&!x.ownerLost||x.ownerGraceExpired&&!x.gracefulStopRequested||x.stage==='owner-retirement'&&!x.gracefulStopRequested)fail();
 if(x.supervisionComplete&&(!x.allReaped||!x.imageVerified||!x.fdsVerified||x.deadlineExpired||x.ownerLost||x.gracefulStopRequested||x.ownerGraceExpired||x.errorNumber!==0))fail();
 return x;
}
function facade(child){
 const f=new EventEmitter();f.pid=child.pid;f.stderr=child.stderr;f.stdio=child.stdio.slice();f.stdio[5]=new PassThrough();f.kill=signal=>child.kill(signal);
 let text='',statusText='',report=null,invalid=false,opened=false,pending=Buffer.alloc(0),bytes=0;
 function refuse(){if(invalid)return;invalid=true;pending.fill(0);pending=Buffer.alloc(0);try{child.kill('SIGKILL');}catch(_){} }
 function open(){if(opened||invalid)return;opened=true;if(pending.length)f.stdio[5].write(pending);pending.fill(0);pending=Buffer.alloc(0);}
 child.stdio[5].on('data',b=>{try{bytes+=b.length;if(bytes>128)return refuse();statusText+=b.toString('ascii');if(opened)f.stdio[5].write(b);else{const next=Buffer.concat([pending,b]);pending.fill(0);pending=next;}}finally{b.fill(0);}});
 child.stdio[5].on('error',refuse);
 child.stdio[6].on('data',b=>{try{
  if(invalid)return;text+=b.toString('ascii');if(text.length>2048)return refuse();
  // Only the tracer owns fd6. Both checkpoints precede any provider readiness.
  if(text.startsWith('IX'))open();
 }finally{b.fill(0);}});
 child.stdio[6].on('error',refuse);child.on('error',e=>f.emit('error',e));
 child.on('close',(code,signal)=>{
  try{
   if(invalid||code!==0||signal!==null||!text.startsWith('IX'))fail();
   report=receipt(text.slice(2));if(!report.allReaped)fail();
   if(!report.supervisionComplete&&report.imageVerified&&report.fdsVerified&&!report.deadlineExpired&&!report.ownerLost&&!report.gracefulStopRequested&&!report.ownerGraceExpired&&report.deniedEscape===0&&statusText.includes('E')&&!statusText.includes('U')&&!statusText.includes('X')){
    report.errorNumber=0;report.rootExit=0;report.rootSignal=0;report.supervisionComplete=true;
   }
  }
  catch(_){report=null;}
  f.supervision=report;pending.fill(0);pending=Buffer.alloc(0);text='';statusText='';f.stdio[5].end();
  f.emit('close',report&&report.rootExit>=0?report.rootExit:null,report&&report.rootSignal?SIGNALS[report.rootSignal]||'unknown':report?null:'unknown');
 });
 return f;
}
function launch(options,done){
 // Every check below runs before any process exists. A throw before cp.spawn is
 // typed (neverSpawned); from the spawn call on, any failure stays unknown.
 let spawning=false;
 try{
  if(process.platform!=='linux'||process.getuid()!==0||typeof done!=='function'||typeof options.onReady!=='function'||typeof options.onPcm!=='function')fail();
  base.frame(options.mode,options.device,process.pid,options.maxMs,options.backendProfile);
  let mod,tracer;
  try{const exe=options.heldOriginalFd,image=descriptor(exe,ORIGINAL_SHA);mod=held(options.modulePath,options.moduleSha256);tracer=held(options.supervisorPath,options.supervisorSha256);
   // Every canonical ancestor is root-owned and non-writable by other users;
   // the final executable path must still name the hashed held descriptor.
   const stat=fs.statSync(options.supervisorPath),opened=fs.fstatSync(tracer);if(stat.dev!==opened.dev||stat.ino!==opened.ino)fail();
   const ids=fs.readdirSync('/proc').filter(x=>/^\d{1,10}$/.test(x));if(ids.length>1024)fail();
   for(const id of ids){try{const path='/proc/'+id+'/exe',target=fs.readlinkSync(path),running=fs.statSync(path);if(target===ORIGINAL||target===ORIGINAL+' (deleted)'||running.dev===image.dev&&running.ino===image.ino)fail();}catch(e){if(e.code==='ENOENT')continue;throw e;}}
   if(Object.keys(image).some(k=>image[k]!==fs.fstatSync(exe)[k]))fail();
   ensureStartupLock();
   spawning=true;
   const child=cp.spawn(options.supervisorPath,['--owned',String(options.maxMs+1500)],{stdio:['ignore','ignore','pipe','pipe','pipe','pipe','pipe',exe,mod],env:{PATH:'/usr/bin:/bin',LANG:'C',LC_ALL:'C'}});
   const managed=facade(child);const session=new base.Session(managed,options,r=>{r.supervision=managed.supervision||null;r.cleanExit=Boolean(r.cleanExit&&r.supervision&&r.supervision.supervisionComplete);r.networkSealInstalled=Boolean(r.networkSealInstalled&&r.supervision&&r.supervision.imageVerified&&r.supervision.fdsVerified);done(r);});
   const reap=session.reap.bind(session);session.reap=async()=>{const r=await reap();return r&&r.supervision&&r.supervision.allReaped?r:null;};return session;
  }finally{for(const fd of [mod,tracer])if(Number.isInteger(fd))fs.closeSync(fd);}
 }catch(e){if(!spawning)throw unspawned();throw e;}
}
module.exports={launch,neverSpawned,facade,receipt,validateOriginalFd,ensureStartupLock,_descriptor:descriptor,_protectedParents:protectedParents};
