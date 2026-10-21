'use strict';
// Private authorization to test this exact installed candidate. This is not
// capture/STOP evidence, a browser preference, or an HTTP assertion.
const fs=require('fs'),path=require('path');
const PROVIDER='2b9f9e318fba385801e160349159cb3238d1fc370136a385413168170879e28b';
function need(value){if(!value)throw Error('UI_QUALIFICATION');}
function safePath(value){return typeof value==='string'&&value.length<=512&&/^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(value)&&value.split('/').every(x=>x!=='.'&&x!=='..');}
function same(a,b){return ['dev','ino','mode','uid','gid','nlink','size','mtimeMs','ctimeMs'].every(k=>a[k]===b[k]);}
function readProtected(file,maxBytes=8192,io=fs){
 let fd=null,raw=null;
 try{
  need(safePath(file)&&io.realpathSync(file)===file);const parents=[];
  for(let p=path.dirname(file);;p=path.dirname(p)){
   need(io.realpathSync(p)===p);const s=io.lstatSync(p);
   need(s.isDirectory()&&!s.isSymbolicLink()&&s.uid===0&&s.gid===0&&!(s.mode&0o022));parents.push([p,s]);if(p==='/')break;
  }
  const before=io.lstatSync(file);need(before.isFile()&&!before.isSymbolicLink()&&before.uid===0&&before.gid===0&&before.nlink===1&&(before.mode&0o7777)===0o600&&before.size>0&&before.size<=maxBytes);
  fd=io.openSync(file,io.constants.O_RDONLY|io.constants.O_NOFOLLOW|io.constants.O_NONBLOCK);need(same(before,io.fstatSync(fd)));
  raw=Buffer.alloc(before.size+1);let used=0;
  while(used<raw.length){const n=io.readSync(fd,raw,used,raw.length-used,used);need(Number.isInteger(n)&&n>=0&&n<=raw.length-used);if(!n)break;used+=n;}
  need(used===before.size&&same(before,io.fstatSync(fd))&&same(before,io.lstatSync(file)));
  for(const [p,s]of parents)need(io.realpathSync(p)===p&&same(s,io.lstatSync(p)));
  io.closeSync(fd);fd=null;const result=Buffer.from(raw.subarray(0,used));raw.fill(0);raw=null;return result;
 }catch(_){throw Error('UI_QUALIFICATION');}
 finally{if(raw)raw.fill(0);if(fd!==null)io.closeSync(fd);}
}
function qualified(file,provider,io=fs){
 let raw=null;
 try{
  raw=readProtected(file,8192,io);const value=JSON.parse(raw.toString('utf8'));
  const expected={schema:1,owner:'local.owner.r2d2mic',category:'/r2d2-ui',backendProfile:1,providerSha256:PROVIDER,moduleSha256:provider.moduleSha256,supervisorSha256:provider.supervisorSha256,enabledForTargetTrial:true};
  return !!value&&!Array.isArray(value)&&Object.keys(value).sort().join(',')===Object.keys(expected).sort().join(',')&&Object.entries(expected).every(([key,want])=>value[key]===want);
 }catch(_){return false;}finally{if(raw)raw.fill(0);}
}
function readKey(file,voiceKey,io=fs){
 let raw=null,result=null;
 try{
  raw=readProtected(file,65,io);const text=raw.toString('utf8');need(/^[a-f0-9]{64}\n?$/.test(text));
  need(text.trim()!==voiceKey);result=Buffer.from(text.trim(),'hex');need(result.some(x=>x!==0));return result;
 }catch(_){if(result)result.fill(0);throw Error('UI_QUALIFICATION');}finally{if(raw)raw.fill(0);}
}
module.exports={qualified,readKey,readProtected,safePath};
