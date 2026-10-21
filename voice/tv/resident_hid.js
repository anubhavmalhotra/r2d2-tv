'use strict';
// Read-only Magic Remote discovery and report parsing. Device-specific hashes
// belong in the private installation profile, never this generic source.
const READ_BYTES=1024, NODE_FIELDS=['dev','ino','rdev','uid','gid','mode','nlink'];
function need(ok,code){if(!ok)throw Object.assign(new Error(code),{code});}
function profile(value){
  need(value&&typeof value==='object'&&!Array.isArray(value)&&
    Object.keys(value).sort().join(',')==='descriptorBytes,descriptorSha256,identitySha256,schema','HID_PROFILE');
  need(value.schema===1&&Number.isInteger(value.descriptorBytes)&&value.descriptorBytes>0&&value.descriptorBytes<=4096&&
    [value.descriptorSha256,value.identitySha256].every(s=>typeof s==='string'&&/^[a-f0-9]{64}$/.test(s)),'HID_PROFILE');
  return Object.freeze({...value});
}
function event(buffer,n){
  need(Buffer.isBuffer(buffer)&&Number.isInteger(n)&&n>0&&n<=Math.min(buffer.length,READ_BYTES),'REPORT_LENGTH');
  let kind='OTHER',offset=null;
  if(buffer[0]===0xfe){need(n===124||n===125,'FE_SHAPE');kind='FE';offset=2;}
  else if(buffer[0]===0xfd){need(n>=20&&n<=31,'FD_SHAPE');kind='FD';offset=17;}
  const code=offset===null?0:buffer.readUInt16BE(offset);
  if(kind==='FE')need((n===124||buffer[124]===0)&&buffer[4]===1&&buffer[6]===0xad&&buffer[64]===1&&buffer[66]===0xad,'MSBC_FRAME');
  return {kind,event:code===0x800a?'start':code===0x800d?'stop':code===0x808b?'held':'none'};
}
function create(value,dependencies={}){
  const expected=profile(value),fs=dependencies.fs||require('fs'),crypto=dependencies.crypto||require('crypto');
  const now=dependencies.now||(()=>Number(process.hrtime.bigint()/1000000n));
  const uid=dependencies.uid||(()=>process.getuid()),euid=dependencies.euid||(()=>process.geteuid());
  const hash=bytes=>crypto.createHash('sha256').update(bytes).digest('hex');
  function root(){need(uid()===0&&euid()===0,'ROOT');}
  function identity(text,descriptor){
    const fields=Object.create(null);need(text.length<=8192&&!text.includes('\0'),'IDENTITY');
    for(const line of text.split('\n')){if(!line)continue;const at=line.indexOf('=');
      need(at>0&&!Object.prototype.hasOwnProperty.call(fields,line.slice(0,at)),'IDENTITY');fields[line.slice(0,at)]=line.slice(at+1);}
    need(/^[0-9a-fA-F]{4}:[0-9a-fA-F]{8}:[0-9a-fA-F]{8}$/.test(fields.HID_ID||'')&&
      typeof fields.HID_UNIQ==='string'&&/^(?:[A-Fa-f0-9]{2}:){5}[A-Fa-f0-9]{2}$/.test(fields.HID_UNIQ),'IDENTITY');
    return {digest:hash(JSON.stringify([fields.HID_ID.toLowerCase(),fields.HID_UNIQ,descriptor,''])),address:fields.HID_UNIQ.toUpperCase()};
  }
  function sameStat(a,b){return NODE_FIELDS.every(k=>Number.isSafeInteger(a[k])&&a[k]>=0&&a[k]===b[k]);}
  function bounded(path,limit,budget){
    budget();let fd=null;const buffer=Buffer.alloc(limit+1);
    try{
      fd=fs.openSync(path,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);
      const before=fs.fstatSync(fd);need(before.isFile()&&before.uid===0&&!(before.mode&0o022),'FILE_AUTHORITY');
      let used=0,calls=0;
      for(;;){budget();need(++calls<=64,'FILE_READS');const n=fs.readSync(fd,buffer,used,buffer.length-used,null);
        need(Number.isInteger(n)&&n>=0&&n<=buffer.length-used,'FILE_READS');used+=n;need(used<=limit,'FILE_LIMIT');if(n===0)break;}
      const after=fs.fstatSync(fd);need(sameStat(before,after)&&before.size===after.size,'FILE_RACE');
      const result=Buffer.from(buffer.subarray(0,used));
      const held=fd;fd=null;try{fs.closeSync(held);}catch(_){result.fill(0);throw Object.assign(new Error('FILE_CLOSE'),{code:'FILE_CLOSE'});}
      return result;
    } finally {buffer.fill(0);if(fd!==null)fs.closeSync(fd);}
  }
  function deviceNumberMatches(number,text){
    need(Number.isSafeInteger(number)&&number>=0&&/^[0-9]{1,10}:[0-9]{1,10}\n?$/.test(text),'DEVICE_NUMBER');
    const n=BigInt(number),parts=text.trim().split(':').map(BigInt);need(parts.every(p=>p<=0xffffffffn),'DEVICE_NUMBER');
    return parts[0]===(((n>>8n)&0xfffn)|((n>>32n)&0xfffff000n))&&parts[1]===((n&0xffn)|((n>>12n)&0xffffff00n));
  }
  function metadata(budget){
    let directory=null;const names=[];
    try{directory=fs.opendirSync('/sys/class/hidraw');for(;;){budget();const entry=directory.readSync();if(!entry)break;
      need(names.length<8&&/^hidraw(?:0|[1-9][0-9]{0,2})$/.test(entry.name),'CLASS_LIMIT');names.push(entry.name);}}
    finally{if(directory)directory.closeSync();}
    const matches=[];
    for(const name of names.sort()){
      budget();const alias='/sys/class/hidraw/'+name+'/device',device=fs.realpathSync(alias);
      need(device.startsWith('/sys/devices/')&&device.length<=1024,'DEVICE_SCOPE');
      const dp=device+'/report_descriptor',up=device+'/uevent';
      need(fs.realpathSync(dp)===dp&&fs.realpathSync(up)===up,'META_SYMLINK');
      let descriptor=null,uevent=null,devnum=null;
      try{
        descriptor=bounded(dp,4096,budget);const descriptorHash=hash(descriptor);if(descriptorHash!==expected.descriptorSha256)continue;
        need(descriptor.length===expected.descriptorBytes,'DESCRIPTOR_SIZE');
        uevent=bounded(up,8192,budget);const id=identity(uevent.toString('utf8'),descriptorHash);if(id.digest!==expected.identitySha256)continue;
        const path='/dev/'+name;need(fs.realpathSync(path)===path,'DEVICE_SYMLINK');const node=fs.lstatSync(path);
        need(node.isCharacterDevice()&&node.uid===0&&!(node.mode&0o002)&&node.nlink===1,'DEVICE_AUTHORITY');
        devnum=bounded('/sys/class/hidraw/'+name+'/dev',64,budget);need(deviceNumberMatches(node.rdev,devnum.toString('ascii')),'DEVICE_NUMBER');
        need(fs.realpathSync(alias)===device,'TOPOLOGY_RACE');
        matches.push({address:id.address,metadata:{path,alias,device,node,descriptorHash,identityDigest:id.digest,ueventHash:hash(uevent)}});
      }finally{for(const b of [descriptor,uevent,devnum])if(b)b.fill(0);}
    }
    need(matches.length===1,'UNIQUE_DEVICE');return matches[0];
  }
  function same(a,b){
    return a&&b&&a.address===b.address&&['path','alias','device','descriptorHash','identityDigest','ueventHash'].every(k=>a.metadata[k]===b.metadata[k])&&sameStat(a.metadata.node,b.metadata.node);
  }
  function derive(currentBudget=()=>true){
    need(typeof currentBudget==='function','METADATA_BUDGET');
    root();const began=now();let previous=began;
    // Each discovery has its own deadline. A resident I/O handle does not expire
    // after eight seconds, and a later discovery never reuses the old budget.
    const budget=()=>{const t=now();need(Number.isSafeInteger(t)&&t>=previous&&t-began<8000&&currentBudget()===true,'METADATA_BUDGET');previous=t;};
    const before=metadata(budget),after=metadata(budget);need(same(before,after),'REMOTE_IDENTITY');budget();return before;
  }
  function recheck(saved,currentBudget){need(same(saved,derive(currentBudget)),'REMOTE_IDENTITY');return true;}
  function makeIO(){root();return{
    open:path=>fs.openSync(path,fs.constants.O_RDONLY|fs.constants.O_NONBLOCK|fs.constants.O_NOFOLLOW),
    stat:fd=>fs.fstatSync(fd),read:(fd,buffer)=>fs.readSync(fd,buffer,0,buffer.length,null),close:fd=>fs.closeSync(fd)
  };}
  return Object.freeze({READ_BYTES,makeIO,derive,recheck,event});
}
module.exports={create,profile,event,READ_BYTES};
