'use strict';
// Read only the fixed native power getter using luna-send's default identity.
const CLI='/usr/bin/luna-send',URI='luna://com.webos.service.tvpower/power/getPowerState';
const CLI_SHA='e2f8b3f7e6522bbd2352f9e831e90580136709b0e09c4de71ceae4a285c657c2';
function need(v){if(!v)throw Error('CLI_PIN');}
function same(a,b){return ['dev','ino','mode','uid','gid','nlink','size','mtimeNs','ctimeNs'].every(k=>String(a[k])===String(b[k]));}
// The pinned luna-send image, re-verified before every spawn; throws CLI_PIN.
function verifyCli(fs,crypto){need(process.platform==='linux'&&process.getuid()===0);const dirs=[];for(let p='/usr/bin';;p=p.slice(0,p.lastIndexOf('/'))||'/'){need(fs.realpathSync(p)===p);const s=fs.lstatSync(p,{bigint:true});need(s.isDirectory()&&!s.isSymbolicLink()&&s.uid===0n&&s.gid===0n&&!(Number(s.mode)&0o022));dirs.push([p,s]);if(p==='/')break;}
 need(fs.realpathSync(CLI)===CLI);const a=fs.lstatSync(CLI,{bigint:true});need(a.isFile()&&!a.isSymbolicLink()&&a.uid===0n&&a.gid===0n&&a.nlink===1n&&a.size===17864n&&!(Number(a.mode)&0o6022)&&(Number(a.mode)&0o111));let fd=null,b=Buffer.alloc(17865),used=0;
 try{fd=fs.openSync(CLI,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);need(same(a,fs.fstatSync(fd,{bigint:true})));while(used<b.length){const n=fs.readSync(fd,b,used,b.length-used,used);need(Number.isInteger(n)&&n>=0&&n<=b.length-used);if(!n)break;used+=n;}need(used===17864&&crypto.createHash('sha256').update(b.subarray(0,used)).digest('hex')===CLI_SHA&&same(a,fs.fstatSync(fd,{bigint:true}))&&same(a,fs.lstatSync(CLI,{bigint:true})));
 }finally{b.fill(0);if(fd!==null)fs.closeSync(fd);}for(const[p,s]of dirs)need(fs.realpathSync(p)===p&&same(s,fs.lstatSync(p,{bigint:true})));return a;
}
function collect(fs,crypto,clock){
 const began=clock();const out={schema:1,kind:'local-power-reader',readOnly:true,captureStarted:false,policyChanged:false,uri:URI,defaultIdentity:true,cliVerified:false,complete:false,spawned:false,pid:null,exitObserved:false,closed:false,reaped:false,exitCode:null,signal:null,termSent:false,killSent:false,stdoutBytes:0,stderrBytes:0,result:null,error:null,elapsedMs:0};
 return new Promise(resolve=>{
  let child=null,settled=false,stopping=false,timer=null,kill=null,reap=null,body=Buffer.alloc(0),image=null;
  function finish(){if(settled)return;settled=true;for(const t of[timer,kill,reap])if(t!==null)clearTimeout(t);body.fill(0);out.complete=out.error===null&&out.reaped&&out.result!==null;out.elapsedMs=Math.max(0,clock()-began);resolve({...out,result:out.result?{...out.result}:null});}
  function stop(code){if(settled||stopping)return;stopping=true;if(out.error===null)out.error=code;
   if(child&&out.spawned&&!out.exitObserved){try{out.termSent=child.kill('SIGTERM')===true;}catch(_){}kill=setTimeout(()=>{if(!out.exitObserved)try{out.killSent=child.kill('SIGKILL')===true;}catch(_){}},500);}
   reap=setTimeout(()=>{if(!out.closed){out.error='REAP_UNCONFIRMED';if(child){child.stdout.destroy();child.stderr.destroy();}finish();}},2000);
  }
  try{image=verifyCli(fs,crypto);out.cliVerified=true;}catch(_){out.error='CLI_PIN';finish();return;}
  try{
   child=require('child_process').spawn(CLI,['-n','1',URI,'{}'],{cwd:'/',stdio:['ignore','pipe','pipe'],env:{PATH:'/usr/bin:/bin',LANG:'C'}});
   child.once('spawn',()=>{out.spawned=true;out.pid=child.pid;});child.once('error',()=>{if(out.spawned)stop('CHILD_ERROR');else out.error='SPAWN_FAILED';});
   child.stdout.on('data',b=>{if(settled){b.fill(0);return;}out.stdoutBytes+=b.length;if(out.stdoutBytes>4096){b.fill(0);stop('STDOUT_BOUND');return;}const n=Buffer.concat([body,b]);body.fill(0);body=n;b.fill(0);});
   child.stderr.on('data',b=>{if(settled){b.fill(0);return;}out.stderrBytes+=b.length;b.fill(0);if(out.stderrBytes>8192)stop('STDERR_BOUND');});child.stdout.on('error',()=>stop('STDOUT_IO'));child.stderr.on('error',()=>stop('STDERR_IO'));
   child.once('exit',(code,signal)=>{out.exitObserved=true;out.exitCode=Number.isInteger(code)?code:null;out.signal=['SIGTERM','SIGKILL'].includes(signal)?signal:null;if(kill!==null){clearTimeout(kill);kill=null;}});
   child.once('close',()=>{out.closed=true;out.reaped=out.exitObserved;
    if(out.error===null&&(out.exitCode!==0||out.signal!==null||!out.reaped))out.error='CHILD_EXIT';
    if(out.error===null)try{
     need(fs.realpathSync(CLI)===CLI&&same(image,fs.lstatSync(CLI,{bigint:true})));
     const v=JSON.parse(body.toString('utf8'));if(!v||typeof v!=='object'||Array.isArray(v)||typeof v.returnValue!=='boolean')throw Error();
     const s=typeof v.state==='string'?v.state.toLowerCase():'';
     out.result={returnValue:v.returnValue,state:v.returnValue?(['active','on'].includes(s)?'active':['standby','suspend','off','power off'].includes(s)?'standby':'unknown'):'unknown',errorCode:Number.isSafeInteger(v.errorCode)&&v.errorCode>=-2147483648&&v.errorCode<=2147483647?v.errorCode:null,errorText:null,message:null};
     if(!v.returnValue)for(const k of['errorText','message'])if(typeof v[k]==='string')out.result[k]=v[k].replace(/[^\x20-\x7e]/g,' ').slice(0,512);
    }catch(_){out.error='REPLY_INVALID';out.result=null;}
    finish();});
   timer=setTimeout(()=>stop('CHILD_DEADLINE'),2500);
  }catch(_){out.error='SPAWN_FAILED';if(child)stop('SPAWN_FAILED');else finish();}
 });
}
module.exports={collect,verifyCli,CLI,CLI_SHA};
