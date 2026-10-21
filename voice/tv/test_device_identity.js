'use strict';
// Storage/ownership/error-path tests only. Crypto objects and filesystem are
// inert boundaries; these are not real credentials or TLS qualification.
const assert=require('assert'),fs=require('fs'),crypto=require('crypto'),vm=require('vm'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'device_identity.js'),'utf8');
const spki=Buffer.concat([Buffer.from('3059301306072a8648ce3d020106082a8648ce3d030107034200','hex'),Buffer.alloc(65)]);
const algorithm=Buffer.from('301306072a8648ce3d020106082a8648ce3d030107','hex');
const cert=n=>'-----BEGIN CERTIFICATE-----\n'+n+'\n-----END CERTIFICATE-----\n';
const key='-----BEGIN '+ 'PRIVATE KEY' + '-----\nNON_AUTHORIZING_FIXTURE\n-----END PRIVATE KEY-----\n';
const TV=cert('PUBLIC_TV'),SPARK=cert('PUBLIC_SPARK'),sha=s=>crypto.createHash('sha256').update(Buffer.from(s)).digest('hex');
const fakeCrypto={createHash:crypto.createHash,timingSafeEqual:crypto.timingSafeEqual,
  X509Certificate:class{constructor(pem){this.raw=Buffer.from(pem);this.publicKey={export:()=>Buffer.from(spki)};}checkPrivateKey(k){return k.match;}},
  createPrivateKey({key}){return{type:'private',asymmetricKeyType:'ec',match:key.includes('NON_AUTHORIZING_FIXTURE'),export:()=>Buffer.concat([Buffer.from([0]),algorithm,Buffer.alloc(10)])};},
  randomBytes(){assert.fail('load must not enroll or mutate');}};
const moduleObject={exports:{}};
vm.runInNewContext(source,{module:moduleObject,Buffer,require:name=>name==='fs'?{}:name==='crypto'?fakeCrypto:assert.fail('unexpected dependency')},{filename:'device_identity.js'});
const D=moduleObject.exports,wall=()=>1000100*1000;
const record=()=>({schema:1,kind:D.KIND,certificate:TV,certificateSha256:sha(TV),privateKey:key,
  sparkCertificate:SPARK,sparkCertificateSha256:sha(SPARK),enrolledAt:1000000,expiresAt:1001000});
function fixture(options={}){
  let bytes=Buffer.from(options.text===undefined?D.canonical(record()):options.text),opened=0,closed=0,stats=0;const reads=[];
  const fileStat={dev:1,ino:2,rdev:0,uid:0,gid:0,mode:0o100600,nlink:1,size:bytes.length,isFile:()=>true,isSymbolicLink:()=>false};
  const io={constants:fs.constants,
    lstatSync(p){if(p===D.FILE)return{...fileStat,...options.file};assert([D.DIR,D.PARENT].includes(p));return{uid:0,gid:0,mode:p===D.DIR?0o40700:0o40755,isDirectory:()=>true,isSymbolicLink:()=>false,...(p===D.DIR?options.directory:options.parent)};},
    realpathSync:p=>options.alias&&p===D.FILE?'/other':p,
    openSync(p,flags){assert.equal(p,D.FILE);assert(flags&fs.constants.O_NOFOLLOW);assert(flags&fs.constants.O_NONBLOCK);opened++;return 50;},
    fstatSync(fd){assert.equal(fd,50);stats++;return{...fileStat,...(stats===2?options.after:options.held)};},
    readSync(fd,b,offset,length,position){assert.equal(fd,50);reads.push(b);const n=Math.min(length,bytes.length-position);bytes.copy(b,offset,position,position+n);return n;},
    closeSync(fd){assert.equal(fd,50);closed++;if(options.closeFails)throw Error('PRIVATE_CLOSE_DETAIL');}};
  return{io,reads,load:()=>D.load(io,wall),stats:()=>({opened,closed})};
}
const cases=[];const test=(name,fn)=>cases.push([name,fn]);
test('valid canonical load is frozen, closes once and clears read buffers',()=>{const f=fixture(),v=f.load();assert(Object.isFrozen(v));assert.equal(v.certificateSha256,sha(TV));assert.deepEqual(f.stats(),{opened:1,closed:1});assert(f.reads.every(b=>b.every(x=>x===0)));});
test('parent and private directory ownership/modes are required',()=>{for(const options of [{parent:{uid:1}},{parent:{mode:0o40777}},{directory:{mode:0o40755}},{directory:{gid:1}},{directory:{isSymbolicLink:()=>true}}]){const f=fixture(options);assert.throws(f.load,/^Error: DEVICE_IDENTITY$/);assert.equal(f.stats().opened,0);}});
test('file identity rejects symlink, mode, ownership and hard link changes',()=>{for(const file of [{uid:1},{gid:1},{mode:0o100644},{nlink:2},{isSymbolicLink:()=>true},{size:32769},{isFile:()=>false}]){const f=fixture({file});assert.throws(f.load,/^Error: DEVICE_IDENTITY$/);assert.equal(f.stats().opened,0);}});
test('held and final metadata races refuse and close',()=>{for(const options of [{held:{ino:3}},{after:{nlink:2}},{after:{size:1}},{alias:true}]){const f=fixture(options);assert.throws(f.load,/^Error: DEVICE_IDENTITY$/);assert.equal(f.stats().opened,f.stats().closed);}});
test('identity close failure refuses rather than returning credentials or retrying FD',()=>{for(const options of [{closeFails:true},{closeFails:true,text:'{bad}'}]){const f=fixture(options);assert.throws(f.load,/^Error: DEVICE_IDENTITY_CLOSE$/);assert.deepEqual(f.stats(),{opened:1,closed:1});assert(f.reads.every(b=>b.every(x=>x===0)));}});
test('JSON must be canonical with exact keys',()=>{const v=record();for(const text of [JSON.stringify(v),D.canonical(v)+'\n','{bad}',D.canonical({...v,schema:'1'}),D.canonical(v).replace('"schema":1','"schema":1,"extra":0')]){const f=fixture({text});assert.throws(f.load,/^Error: DEVICE_IDENTITY$/);assert.equal(f.stats().closed,1);assert(f.reads.every(b=>b.every(x=>x===0)));}});
test('certificate pins, key match and separate identities remain enforced',()=>{for(const change of [{certificateSha256:'a'.repeat(64)},{sparkCertificateSha256:'a'.repeat(64)},
  {privateKey:key.replace('NON_AUTHORIZING_FIXTURE','WRONG')},{sparkCertificate:TV,sparkCertificateSha256:sha(TV)}]){const f=fixture({text:D.canonical({...record(),...change})});assert.throws(f.load,/^Error: DEVICE_IDENTITY$/);}});
test('expiry and future identity windows refuse with fixed code',()=>{for(const change of [{expiresAt:1000100},{enrolledAt:1000401,expiresAt:1001000}]){const f=fixture({text:D.canonical({...record(),...change})});assert.throws(f.load,/^Error: DEVICE_IDENTITY_EXPIRED$/);}});
test('combined or trailing PEM material is refused',()=>{for(const change of [{certificate:TV+SPARK},{certificate:TV+'extra'},{privateKey:key+key}]){const f=fixture({text:D.canonical({...record(),...change})});assert.throws(f.load,/^Error: DEVICE_IDENTITY$/);}});
for(const [name,fn] of cases){try{fn();}catch(e){console.error('FAIL '+name);throw e;}}
console.log(cases.length+' resident identity storage controls passed');
