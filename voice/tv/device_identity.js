'use strict';
// Root-owned, one-time enrolled TV identity. The installer must establish code
// integrity and independently authenticate enrollment; this storage module does
// not bootstrap trust. It never prints certificates, private keys or failures.
// Mutable DER/read buffers are cleared. Immutable strings/KeyObjects and flash
// wear-levelling are not claimed to support forensic erasure.
const fs=require('fs'),crypto=require('crypto');
const PARENT='/var/lib',DIR=PARENT+'/local.owner.r2d2pair',FILE=DIR+'/identity.json';
const MAX=32768,PEM_MAX=8192,ROTATE_S=30*86400,SKEW_S=300,KIND='r2d2-tv-device-identity';
const KEYS=['certificate','certificateSha256','enrolledAt','expiresAt','kind','privateKey','schema','sparkCertificate','sparkCertificateSha256'];
const SPKI_P256=Buffer.from('3059301306072a8648ce3d020106082a8648ce3d030107034200','hex'),ALG_P256=Buffer.from('301306072a8648ce3d020106082a8648ce3d030107','hex');
const CODES=['DEVICE_IDENTITY','DEVICE_IDENTITY_EXPIRED','DEVICE_IDENTITY_MISSING','DEVICE_IDENTITY_EXISTS','DEVICE_IDENTITY_CLOSE'];
function fail(code){throw Object.assign(new Error(code),{code});}
function need(v,code='DEVICE_IDENTITY'){if(!v)fail(code);}
function coded(e){return CODES.includes(e&&e.message)?e.message:'DEVICE_IDENTITY';}
const hex64=s=>typeof s==='string'&&/^[a-f0-9]{64}$/.test(s);
const sameHex=(a,b)=>hex64(a)&&hex64(b)&&crypto.timingSafeEqual(Buffer.from(a,'hex'),Buffer.from(b,'hex'));
function pem(s,label){
 need(typeof s==='string'&&s.length>0&&s.length<=PEM_MAX&&/^[\x0a\x20-\x7e]+$/.test(s));
 const b='-----BEGIN '+label+'-----',e='-----END '+label+'-----';
 need(s.split(b).length===2&&s.split(e).length===2&&s.startsWith(b)&&(s.endsWith(e)||s.endsWith(e+'\n'))&&s.split('-----BEGIN ').length===2);
 return s;
}
// Certificate pin = SHA-256 of the DER, 64 lowercase hex (the same form as the host contracts' certPinDer).
function pinDer(der){need(Buffer.isBuffer(der)&&der.length>0);return crypto.createHash('sha256').update(der).digest('hex');}
function certificate(s){pem(s,'CERTIFICATE');const x=new crypto.X509Certificate(s);return{x,sha:pinDer(x.raw)};}
function stamp(n){return Number.isSafeInteger(n)&&n>0&&n<2**40;}
function wallSeconds(wallNow){const ms=wallNow();need(Number.isSafeInteger(ms)&&ms>0);return Math.floor(ms/1000);}
// Pure record check. Throws DEVICE_IDENTITY for any shape/crypto fault, DEVICE_IDENTITY_EXPIRED outside the window.
function validate(v,wallNow=Date.now){try{return check(v,wallNow);}catch(e){fail(coded(e));}}
function check(v,wallNow){
 need(v&&typeof v==='object'&&!Array.isArray(v)&&Object.keys(v).sort().join(',')===KEYS.join(','));
 need(v.schema===1&&v.kind===KIND&&hex64(v.certificateSha256)&&hex64(v.sparkCertificateSha256));
 const tv=certificate(v.certificate),spark=certificate(v.sparkCertificate);
 need(sameHex(tv.sha,v.certificateSha256)&&sameHex(spark.sha,v.sparkCertificateSha256)&&!sameHex(tv.sha,spark.sha));
 const spki=tv.x.publicKey.export({type:'spki',format:'der'});need(spki.length===91&&spki.subarray(0,SPKI_P256.length).equals(SPKI_P256));
 pem(v.privateKey,'PRIVATE KEY');const key=crypto.createPrivateKey({key:v.privateKey,format:'pem'});
 const der=key.export({type:'pkcs8',format:'der'});try{const i=der.indexOf(ALG_P256);need(key.type==='private'&&key.asymmetricKeyType==='ec'&&i>=0&&i<=8&&tv.x.checkPrivateKey(key));}finally{der.fill(0);}
 need(stamp(v.enrolledAt)&&stamp(v.expiresAt)&&v.enrolledAt<v.expiresAt&&v.expiresAt<=v.enrolledAt+ROTATE_S);
 const now=wallSeconds(wallNow);need(now>=v.enrolledAt-SKEW_S&&now<v.expiresAt,'DEVICE_IDENTITY_EXPIRED');
 return v;
}
function missing(e){return e&&e.code==='ENOENT';}
// /var/lib must be a real root directory nobody else can write; DIR root:root 0700, not a symlink, realpath equal.
function checkParent(io){const p=io.lstatSync(PARENT);need(p.isDirectory()&&!p.isSymbolicLink()&&p.uid===0&&p.gid===0&&(p.mode&0o022)===0&&io.realpathSync(PARENT)===PARENT);}
function checkDir(io){const d=io.lstatSync(DIR);need(d.isDirectory()&&!d.isSymbolicLink()&&d.uid===0&&d.gid===0&&(d.mode&0o7777)===0o700&&io.realpathSync(DIR)===DIR);return d;}
function checkStore(io=fs){try{checkParent(io);try{checkDir(io);}catch(e){if(!missing(e))throw e;}return true;}catch(e){fail('DEVICE_IDENTITY');}}
function syncDir(io,expected){const fd=io.openSync(DIR,io.constants.O_RDONLY|io.constants.O_NOFOLLOW|(io.constants.O_DIRECTORY||0));try{const s=io.fstatSync(fd);need(s.dev===expected.dev&&s.ino===expected.ino);io.fsyncSync(fd);}finally{io.closeSync(fd);}}
const ID=['dev','ino','mode','uid','gid','nlink','size'];
function load(io=fs,wallNow=Date.now){
 let data=null,fd=null,value=null;
 try{
  const C=io.constants;need(C&&Number.isInteger(C.O_NOFOLLOW)&&Number.isInteger(C.O_RDONLY));
  checkParent(io);checkDir(io);
  const a=io.lstatSync(FILE);
  need(a.isFile()&&!a.isSymbolicLink()&&a.uid===0&&a.gid===0&&a.nlink===1&&(a.mode&0o7777)===0o600&&Number.isSafeInteger(a.size)&&a.size>0&&a.size<=MAX&&io.realpathSync(FILE)===FILE);
  fd=io.openSync(FILE,C.O_RDONLY|C.O_NOFOLLOW|(C.O_NONBLOCK||0));
  const b=io.fstatSync(fd);need(b.isFile()&&ID.every(k=>a[k]===b[k]));
  data=Buffer.alloc(a.size+1);let used=0,loops=0;
  for(;;){need(++loops<=64);const n=io.readSync(fd,data,used,data.length-used,used);need(Number.isInteger(n)&&n>=0&&n<=data.length-used);if(n===0)break;used+=n;}
  need(used===a.size);const c=io.fstatSync(fd);need(ID.every(k=>a[k]===c[k]));
  const text=data.toString('latin1',0,used);need(/^[\x20-\x7e]+$/.test(text));
  value=JSON.parse(text);need(JSON.stringify(value)===text&&value&&typeof value==='object'&&Object.keys(value).join(',')===KEYS.join(','));
  check(value,wallNow);
  const out={};for(const k of KEYS)out[k]=value[k];
  const held=fd;fd=null;try{io.closeSync(held);}catch(_){fail('DEVICE_IDENTITY_CLOSE');}return Object.freeze(out);
 }catch(e){fail(missing(e)?'DEVICE_IDENTITY_MISSING':coded(e));}
 finally{if(data)data.fill(0);data=null;value=null;if(fd!==null)try{io.closeSync(fd);}catch(_){fail('DEVICE_IDENTITY_CLOSE');}}
}
// Canonical bytes: sorted keys, no whitespace (load() requires exactly this form).
function canonical(v){const o={};for(const k of KEYS)o[k]=v[k];return JSON.stringify(o);}
function write(record,io=fs,options={}){
 let bytes=null,fd=null,temp=null,renamed=false;
 try{
  need(options&&typeof options==='object'&&Object.keys(options).every(k=>k==='replace'||k==='wallNow'));
  const replace=options.replace===true,wallNow=options.wallNow||Date.now,C=io.constants;
  need(typeof wallNow==='function'&&C&&Number.isInteger(C.O_EXCL)&&Number.isInteger(C.O_NOFOLLOW));
  check(record,wallNow);bytes=Buffer.from(canonical(record),'latin1');need(bytes.length<=MAX&&/^[\x20-\x7e]+$/.test(bytes.toString('latin1')));
  checkParent(io);try{io.lstatSync(DIR);}catch(e){if(!missing(e))throw e;io.mkdirSync(DIR,0o700);}
  const dir=checkDir(io);
  let present=true;try{io.lstatSync(FILE);}catch(e){if(!missing(e))throw e;present=false;}
  if(present&&!replace){let valid=false;try{load(io,wallNow);valid=true;}catch(_){}need(!valid,'DEVICE_IDENTITY_EXISTS');}
  temp=DIR+'/.identity-'+crypto.randomBytes(8).toString('hex')+'.tmp';
  fd=io.openSync(temp,C.O_WRONLY|C.O_CREAT|C.O_EXCL|C.O_NOFOLLOW,0o600);io.fchmodSync(fd,0o600);
  const s=io.fstatSync(fd);need(s.isFile()&&s.uid===0&&s.gid===0&&s.nlink===1&&(s.mode&0o7777)===0o600&&s.size===0);
  let n=0,loops=0;while(n<bytes.length){need(++loops<=64);const k=io.writeSync(fd,bytes,n,bytes.length-n,n);need(Number.isInteger(k)&&k>0&&k<=bytes.length-n);n+=k;}
  io.fsyncSync(fd);io.closeSync(fd);fd=null;
  io.renameSync(temp,FILE);renamed=true;syncDir(io,dir);
  const back=load(io,wallNow);need(sameHex(back.certificateSha256,record.certificateSha256)&&sameHex(back.sparkCertificateSha256,record.sparkCertificateSha256));
  return Object.freeze({certificateSha256:back.certificateSha256,sparkCertificateSha256:back.sparkCertificateSha256,enrolledAt:back.enrolledAt,expiresAt:back.expiresAt});
 }catch(e){fail(coded(e));}
 finally{if(fd!==null)try{io.closeSync(fd);}catch(_){}if(temp&&!renamed)try{io.unlinkSync(temp);}catch(_){}if(bytes)bytes.fill(0);}
}
// Revocation on the TV side. Best-effort zero-fill before unlink ([H] flash wear levelling may keep old blocks).
function remove(io=fs){
 try{
  checkParent(io);let dir;try{dir=checkDir(io);}catch(e){if(missing(e))return false;throw e;}
  let a;try{a=io.lstatSync(FILE);}catch(e){if(missing(e))return false;throw e;}
  need(!a.isDirectory());
  if(a.isFile()&&!a.isSymbolicLink()&&a.nlink===1&&a.uid===0&&a.gid===0&&a.size>0&&a.size<=MAX){
   const fd=io.openSync(FILE,io.constants.O_WRONLY|io.constants.O_NOFOLLOW|(io.constants.O_NONBLOCK||0));
   try{const b=io.fstatSync(fd);need(b.dev===a.dev&&b.ino===a.ino&&b.nlink===1);const z=Buffer.alloc(a.size);let n=0;while(n<z.length){const k=io.writeSync(fd,z,n,z.length-n,n);need(Number.isInteger(k)&&k>0);n+=k;}io.fsyncSync(fd);}finally{io.closeSync(fd);}
  }
  io.unlinkSync(FILE);syncDir(io,dir);return true;
 }catch(e){fail('DEVICE_IDENTITY');}
}
module.exports={load,write,remove,validate,checkStore,canonical,pinDer,PARENT,DIR,FILE,MAX,PEM_MAX,ROTATE_S,SKEW_S,KIND,KEYS};
