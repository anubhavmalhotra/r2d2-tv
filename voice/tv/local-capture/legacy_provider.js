'use strict';
// Finite owned-child pipe/receipt parser only. The supervised factory is the
// sole production launcher; there is no unconstrained LD_PRELOAD launch route.
const STAGES={a:'status-ready',b:'abi-and-fds-valid',c:'config-valid',d:'parent-pipe-bound',e:'controls-ready',t:'single-thread-verified',n:'no-new-privileges',K:'network-installed',f:'symbols-resolved',g:'hooks-installed'};
const OWN_EVENTS='BLMRSXTUNECFKOPD'+Object.keys(STAGES).join('');
const SIGNALS=['SIGABRT','SIGALRM','SIGBUS','SIGFPE','SIGHUP','SIGILL','SIGINT','SIGKILL','SIGPIPE','SIGQUIT','SIGSEGV','SIGSYS','SIGTERM','SIGTRAP','SIGXCPU','SIGXFSZ'];
function loaderDiagnostic(buffer){
 // Read at most 4 KiB before K. Project only loader grammar, never arbitrary
 // stderr, full paths, device names or LS2 payloads. No private input is admitted.
 const s=buffer.toString('ascii');let m;
 if((m=/undefined symbol: ([A-Za-z_][A-Za-z0-9_]{0,179})(?:[,\s]|$)/.exec(s)))return {kind:'undefined-symbol',detail:m[1]};
 if((m=/([A-Za-z0-9_+.-]{1,96}\.so(?:\.[0-9]+)*): cannot open shared object file:/.exec(s)))return {kind:'missing-library',detail:m[1]};
 if((m=/version [`']((?:GLIBC|GLIBCXX|CXXABI)_[0-9.]{1,24})['`] not found/.exec(s)))return {kind:'missing-version',detail:m[1]};
 if(/wrong ELF class|invalid ELF header|ELF file data encoding not little-endian/.test(s))return {kind:'invalid-elf',detail:null};
 if(/object .* from LD_PRELOAD cannot be preloaded/.test(s))return {kind:'preload-refused',detail:null};
 return {kind:buffer.length?'unclassified-startup-stderr':'none',detail:null};
}
function fail(){throw Error('OWNER_PROVIDER_UNQUALIFIED');}
function frame(mode,device,parent,maxMs,backendProfile){
 if(backendProfile!==(mode==='probe'?0:1)||!['probe','capture','ui-capture'].includes(mode)||!(device===''||/^remote\d{1,20}$/.test(device))||!Number.isInteger(parent)||parent<1||!Number.isInteger(maxMs)||maxMs<100||maxMs>10000)fail();
 const b=Buffer.alloc(52);b.writeUInt32LE(0x31564f52,0);b.writeUInt32LE(parent,4);b.write(device,8,'ascii');b.writeUInt32LE(maxMs,40);b.writeUInt32LE(mode==='probe'?0:mode==='capture'?1:2,44);b.writeUInt32LE(backendProfile,48);return b;
}
class Session{
 constructor(child,{mode,device,maxMs,backendProfile,onReady,onPcm},done){
  this.child=child;this.mode=mode;this.done=done;this.events=new Set();this.bytes=0;this.ended=false;this.closing=false;this.draining=false;this.ready=false;this.inventoryReplies=0;this.devicesObserved=0;this.remoteRecords=0;this.probeBuffer=Buffer.alloc(0);this.probeBytes=0;this.summary=null;this.summaryPending=null;this.parseFailed=false;
  this.constructorStage='none';this.startupStderr=Buffer.alloc(0);this.startupStderrBytes=0;this.startupStderrTruncated=false;
  this.closeReceipt=null;this.closed=new Promise(resolve=>{this.resolveClosed=resolve;});
  this.deadline=setTimeout(()=>this.stop(),maxMs+100);this.killTimer=null;
  if(child.stderr){
   child.stderr.on('data',buffer=>{try{
    if(this.ended||this.events.has('K'))return;
    const remaining=4096-this.startupStderr.length;this.startupStderrBytes=Math.min(4097,this.startupStderrBytes+buffer.length);
    if(buffer.length>remaining)this.startupStderrTruncated=true;
    if(remaining>0){const next=Buffer.concat([this.startupStderr,buffer.subarray(0,remaining)]);this.startupStderr.fill(0);this.startupStderr=next;}
    // glibc may ignore a bad LD_PRELOAD and continue the original executable.
    // Do not leave that unsealed fallback alive until the ordinary deadline.
    if(loaderDiagnostic(this.startupStderr).kind==='preload-refused'){this.stop();try{child.kill('SIGKILL');}catch(_){}}
   }finally{buffer.fill(0);}});
   child.stderr.on('error',()=>this.stop());
  }
  let statusBytes=0;
  child.stdio[5].on('data',buffer=>{if(this.ended)return;statusBytes+=buffer.length;if(statusBytes>128)return this.stop();
   for(const byte of buffer){const c=String.fromCharCode(byte);
    if(this.summaryPending!==null){
     this.summaryPending+=c;
     if(this.summaryPending.length===18){const match=/^Y([a-f0-9]{8})([a-f0-9]{8})Z$/.exec(this.summaryPending);this.summaryPending=null;
      if(!match||this.summary){this.parseFailed=true;this.events.add('F');this.stop();continue;}
      const bytes=parseInt(match[1],16),frames=parseInt(match[2],16);
      if(bytes>320000||bytes%2||frames>160000||(bytes===0)!==(frames===0)){this.parseFailed=true;this.events.add('F');this.stop();continue;}
      this.summary={bytes,frames};
     }continue;
    }
    if(c==='Y'){if(this.summary){this.parseFailed=true;this.events.add('F');this.stop();}else this.summaryPending='Y';continue;}
    if(!OWN_EVENTS.includes(c)){this.parseFailed=true;this.events.add('F');this.stop();continue;}this.events.add(c);if(STAGES[c])this.constructorStage=STAGES[c];if(c==='K'){this.startupStderr.fill(0);this.startupStderr=Buffer.alloc(0);}if(c==='F'||c==='U'||c==='X')this.stop();}
   if(!this.closing&&!this.ready&&this.events.has('B')&&this.events.has('L')&&this.events.has('K')){this.ready=true;try{onReady(this);}catch(_){this.stop();}}
  });
  child.stdio[3].on('data',buffer=>{
   try{
    if(this.ended||this.closing&&!this.draining||!this.ready||!buffer.length){this.stop();return;}
    if(mode==='probe'){this.consumeProbe(buffer);return;}
    if(this.bytes+buffer.length>320000||!onPcm(buffer)){this.stop();return;}this.bytes+=buffer.length;
   }finally{buffer.fill(0);}
  });
  for(const n of [3,4,5])child.stdio[n].on('error',()=>this.stop());
  child.on('error',()=>this.stop());
  child.on('close',(code,signal)=>{
   if(this.ended)return;this.ended=true;clearTimeout(this.deadline);clearTimeout(this.killTimer);
   this.probeBuffer.fill(0);this.probeBuffer=Buffer.alloc(0);
   const diagnostic=this.events.has('K')?{kind:'none',detail:null}:loaderDiagnostic(this.startupStderr);this.startupStderr.fill(0);this.startupStderr=Buffer.alloc(0);
   const zeroByteStop=mode!=='probe'&&!this.parseFailed&&this.bytes===0&&this.summary!==null&&this.summary.bytes===0&&this.summary.frames===0&&this.events.has('S')&&this.events.has('T')&&!this.events.has('U')&&this.events.has('E');
   this.closeReceipt={schema:1,exitCode:Number.isInteger(code)&&code>=0&&code<=255?code:null,exitSignal:SIGNALS.includes(signal)?signal:signal===null?null:'unknown',constructorStage:this.constructorStage,startupStderrBytes:this.startupStderrBytes,startupStderrTruncated:this.startupStderrTruncated,loaderDiagnostic:diagnostic,ready:this.ready,networkSealInstalled:this.events.has('K'),selfCallerObserved:this.events.has('P'),hardwareObserverStarted:this.events.has('O'),inventoryReplies:this.inventoryReplies,devicesObserved:this.devicesObserved,remoteRecords:this.remoteRecords,callerObserved:this.events.has('M'),reservationObserved:this.events.has('R'),nativeStartAccepted:this.events.has('S'),nativeStopAccepted:this.events.has('T'),nativeStopFailed:this.events.has('U'),captureBytes:this.bytes,physicalStopVerified:false,physicalMicVerified:false,exited:true,cleanExit:code===0&&signal===null&&this.events.has('E')&&(!this.events.has('F')||zeroByteStop)&&!this.events.has('X')&&(mode==='probe'||(this.events.has('S')&&(this.events.has('T')||this.events.has('N'))||this.events.has('N'))&&!this.events.has('U')),restoredOriginal:false};
   this.closeReceipt.nativeNeverStarted=this.events.has('N')&&!this.events.has('S')&&!this.events.has('X');
   this.closeReceipt.nativePcmBytes=this.summary?this.summary.bytes:null;this.closeReceipt.nativeFrames=this.summary?this.summary.frames:null;
   this.closeReceipt.pcmCountConfirmed=!!(this.summary&&this.summaryPending===null&&this.summary.bytes===this.bytes);
   if(mode!=='probe'&&!this.closeReceipt.pcmCountConfirmed)this.closeReceipt.cleanExit=false;
   this.resolveClosed(this.closeReceipt);this.done(this.closeReceipt);
  });
  child.stdio[4].write(frame(mode,device,process.pid,maxMs,backendProfile),e=>{if(e)this.stop();});
 }
 consumeProbe(buffer){
  this.probeBytes+=buffer.length;if(this.probeBytes>3*(8192+4))return this.stop();
  const combined=Buffer.concat([this.probeBuffer,buffer]);this.probeBuffer.fill(0);this.probeBuffer=combined;
  while(this.probeBuffer.length>=4){
   const n=this.probeBuffer.readUInt32LE(0);if(n<1||n>8192||this.inventoryReplies>=3)return this.stop();
   if(this.probeBuffer.length<4+n)return;
   let counts;try{counts=projectInventory(this.probeBuffer.subarray(4,4+n));}catch(_){return this.stop();}
   const remainder=Buffer.from(this.probeBuffer.subarray(4+n));this.probeBuffer.fill(0);this.probeBuffer=remainder;
   ++this.inventoryReplies;this.devicesObserved=Math.max(this.devicesObserved,counts.devices);this.remoteRecords=Math.max(this.remoteRecords,counts.remotes);
  }
 }
 async reap(){
  this.stop();if(this.ended)return this.closeReceipt;
  // stop() owns the 1.5 s kill fallback. Reaping requires the actual child close
  // event; a successful kill request, receipt flag, or process scan is not enough.
  let timer;const receipt=await Promise.race([this.closed,new Promise(resolve=>{timer=setTimeout(()=>resolve(null),2500);})]);
  clearTimeout(timer);return receipt;
 }
 stop(){if(this.ended||this.closing)return;this.closing=true;
  try{this.child.stdio[4].end(Buffer.from('Q'));}catch(_){}
  this.killTimer=setTimeout(()=>{if(!this.ended){try{this.child.kill('SIGKILL');}catch(_){}}},1500);
 }
 releaseDrain(){if(this.ended)return;this.draining=true;this.stop();}
 discard(){this.draining=false;this.stop();}
}
function projectInventory(bytes){
 // Native response is never returned, retained or incorporated into an error.
 const value=JSON.parse(bytes.toString('utf8'));
 if(!value||Array.isArray(value)||value.returnValue!==true||!Array.isArray(value.devices)||value.devices.length>32||Object.keys(value).some(k=>!['returnValue','devices','subscribed'].includes(k)))fail();
 let remotes=0;const ids=new Set();
 for(const d of value.devices){if(!d||typeof d!=='object'||Array.isArray(d)||typeof d.id!=='string'||d.id.length>128||ids.has(d.id)||typeof d.key!=='string'||d.key.length>128||typeof d.deviceType!=='string'||d.deviceType.length>64||typeof d.deviceName!=='string'||d.deviceName.length>128)fail();
  ids.add(d.id);if(d.deviceType==='remote'&&/^remote[0-9]{1,20}$/.test(d.id))++remotes;}
 return {devices:value.devices.length,remotes};
}
module.exports={Session,frame,projectInventory,loaderDiagnostic};
