'use strict';
// Passive evdev input only. No grab, writes, event injection, key logging or
// guessed microphone key. Installation supplies a separately calibrated profile.
const FIELDS=['dev','ino','rdev','uid','gid','mode','nlink'];
const failures=new WeakSet();
function fail(code){const error=Object.assign(new Error(code),{code});failures.add(error);return error;}
function closed(error){return error&&typeof error==='object'&&failures.has(error)?error:fail('OWNER_KEY_UNAVAILABLE');}
function need(ok,code){if(!ok)throw fail(code);}
function profile(value){
  need(value&&typeof value==='object'&&!Array.isArray(value)&&
    Object.keys(value).sort().join(',')==='eventBytes,identitySha256,keyCode,schema'&&value.schema===1&&
    [16,24].includes(value.eventBytes)&&Number.isInteger(value.keyCode)&&value.keyCode>0&&value.keyCode<=0x2ff&&
    typeof value.identitySha256==='string'&&/^[a-f0-9]{64}$/.test(value.identitySha256),'OWNER_KEY_PROFILE');
  return Object.freeze({...value});
}
function same(a,b){return a&&b&&FIELDS.every(k=>Number.isSafeInteger(a[k])&&a[k]>=0&&a[k]===b[k]);}
function create(value,dependencies={}){
  const expected=profile(value),fs=dependencies.fs||require('fs'),crypto=dependencies.crypto||require('crypto');
  const arch=dependencies.arch||process.arch,uid=dependencies.uid||(()=>process.getuid()),euid=dependencies.euid||(()=>process.geteuid());
  const now=dependencies.now||(()=>{const t=process.hrtime();return t[0]*1000+Math.floor(t[1]/1000000);});
  const width={arm:16,ia32:16,arm64:24,x64:24}[arch];
  need(width===expected.eventBytes&&(dependencies.endianness||require('os').endianness)()==='LE','OWNER_KEY_ABI');
  const buffer=Buffer.alloc(width*64);
  let fd=null,selected=null,saved=null,down=false,synced=false,poisoned=false,lastCheck=-Infinity,lastTime=-Infinity;
  function clock(){const t=now();need(Number.isSafeInteger(t)&&t>=lastTime,'OWNER_KEY_CLOCK');lastTime=t;return t;}
  function bounded(path,limit,budget){
    budget();let held=null;const bytes=Buffer.alloc(limit+1);
    try{
      need(fs.realpathSync(path)===path,'OWNER_KEY_SYMLINK');
      held=fs.openSync(path,fs.constants.O_RDONLY|fs.constants.O_NONBLOCK|fs.constants.O_NOFOLLOW);
      const before=fs.fstatSync(held);need(before.isFile()&&before.uid===0&&!(before.mode&0o022),'OWNER_KEY_META');
      let used=0,calls=0;for(;;){budget();need(++calls<=16,'OWNER_KEY_META');
        const n=fs.readSync(held,bytes,used,bytes.length-used,null);need(Number.isInteger(n)&&n>=0&&n<=bytes.length-used,'OWNER_KEY_META');used+=n;
        need(used<=limit,'OWNER_KEY_META');if(n===0)break;}
      const after=fs.fstatSync(held);need(same(before,after)&&before.size===after.size,'OWNER_KEY_RACE');
      need(bytes.subarray(0,used).every(b=>b===10||b>=32&&b<=126),'OWNER_KEY_META');
      return bytes.subarray(0,used).toString('ascii');
    }finally{bytes.fill(0);if(held!==null){try{fs.closeSync(held);}catch(_){poisoned=true;throw fail('OWNER_KEY_CLOSE');}}}
  }
  function bit(text,code){
    need(/^[a-f0-9]+(?: [a-f0-9]+)*$/.test(text),'OWNER_KEY_META');
    const bits=width===16?32:64,words=text.split(' ').reverse(),word=words[Math.floor(code/bits)];
    need(words.every(w=>w.length<=bits/4),'OWNER_KEY_META');
    if(!word)return false;
    // Select a hex nibble, avoiding JS's signed 32-bit shifts for 64-bit sysfs words.
    const nibble=word[word.length-1-Math.floor((code%bits)/4)];
    return !!(nibble&&(parseInt(nibble,16)&(1<<(code%4))));
  }
  function numberMatches(number,text){
    need(Number.isSafeInteger(number)&&number>=0&&/^[0-9]{1,10}:[0-9]{1,10}$/.test(text),'OWNER_KEY_NODE');
    const parts=text.split(':').map(Number);
    const major=(((Math.floor(number/256)&0xfff)|(Math.floor(number/4294967296)&0xfffff000))>>>0);
    const minor=(((number&0xff)|(Math.floor(number/4096)&0xffffff00))>>>0);
    return parts[0]===major&&parts[1]===minor;
  }
  function snapshot(remote,currentBudget=()=>true){
    need(remote&&remote.metadata&&typeof remote.metadata.device==='string'&&
      remote.metadata.device.startsWith('/sys/devices/')&&remote.metadata.device.length<=1024&&
      typeof remote.address==='string'&&/^(?:[a-f0-9]{2}:){5}[a-f0-9]{2}$/i.test(remote.address),'OWNER_KEY_REMOTE');
    need(fs.realpathSync(remote.metadata.device)===remote.metadata.device,'OWNER_KEY_REMOTE');
    const began=clock(),budget=()=>need(clock()-began<1000&&currentBudget()===true,'OWNER_KEY_BUDGET');
    const names=[];let directory=null;
    try{directory=fs.opendirSync('/sys/class/input');for(;;){budget();const e=directory.readSync();if(!e)break;
      need(names.length<64,'OWNER_KEY_BOUND');names.push(e.name);}}
    finally{if(directory)try{directory.closeSync();}catch(_){poisoned=true;throw fail('OWNER_KEY_CLOSE');}}
    const matches=[];
    for(const name of names.filter(n=>/^event(?:0|[1-9][0-9]{0,3})$/.test(n)).sort()){
      budget();const classAlias='/sys/class/input/'+name,alias=classAlias+'/device',device=fs.realpathSync(alias);
      // A similarly named keyboard or another remote cannot become authority.
      if(!device.startsWith(remote.metadata.device+'/input/')||
        !/^input[0-9]+$/.test(device.slice((remote.metadata.device+'/input/').length)))continue;
      const eventPath=fs.realpathSync(classAlias);need(eventPath===device+'/'+name,'OWNER_KEY_SCOPE');
      const read=(suffix,limit)=>bounded(device+'/'+suffix,limit,budget).trim();
      const ids=['bustype','vendor','product','version'].map(n=>read('id/'+n,16).toLowerCase());
      need(ids.every(s=>/^[a-f0-9]{4}$/.test(s)),'OWNER_KEY_META');
      const uniq=read('uniq',64).toLowerCase();if(uniq!==remote.address.toLowerCase())continue;
      const ev=read('capabilities/ev',256).toLowerCase(),key=read('capabilities/key',1024).toLowerCase();
      const digest=crypto.createHash('sha256').update(JSON.stringify([...ids,uniq,ev,key])).digest('hex');
      if(digest!==expected.identitySha256)continue;
      need(bit(ev,1)&&bit(key,expected.keyCode),'OWNER_KEY_CAPABILITY');
      const path='/dev/input/'+name;need(fs.realpathSync(path)===path,'OWNER_KEY_SYMLINK');const node=fs.lstatSync(path);
      need(node.isCharacterDevice()&&node.uid===0&&!(node.mode&0o022)&&node.nlink===1,'OWNER_KEY_NODE');
      const devnum=bounded(eventPath+'/dev',64,budget).trim();
      need(numberMatches(node.rdev,devnum)&&fs.realpathSync(alias)===device&&fs.realpathSync(classAlias)===eventPath,'OWNER_KEY_RACE');
      matches.push({path,alias,device,classAlias,eventPath,digest,node});
    }
    need(matches.length===1,'OWNER_KEY_UNIQUE');budget();return matches[0];
  }
  function equivalent(a,b){return a&&b&&['path','alias','device','classAlias','eventPath','digest'].every(k=>a[k]===b[k])&&same(a.node,b.node);}
  function check(full=false,scan=true,currentBudget=()=>true){
    need(typeof currentBudget==='function'&&currentBudget()===true,'OWNER_KEY_BUDGET');
    need(!poisoned&&fd!==null,'OWNER_KEY_CLOSED');
    need(same(saved.node,fs.fstatSync(fd))&&same(saved.node,fs.lstatSync(saved.path))&&
      fs.realpathSync(saved.path)===saved.path&&fs.realpathSync(saved.alias)===saved.device&&
      fs.realpathSync(saved.classAlias)===saved.eventPath,'OWNER_KEY_RACE');
    const t=clock();
    if(scan&&(full||t-lastCheck>=100)){need(equivalent(saved,snapshot(selected,currentBudget)),'OWNER_KEY_RACE');lastCheck=clock();}
  }
  function close(){
    const held=fd;fd=null;saved=null;selected=null;down=false;synced=false;buffer.fill(0);
    if(held!==null)try{fs.closeSync(held);}catch(_){poisoned=true;}
    return !poisoned;
  }
  function open(remote){
    need(uid()===0&&euid()===0&&!poisoned&&fd===null,'OWNER_KEY_AUTHORITY');
    try{const first=snapshot(remote),second=snapshot(remote);need(equivalent(first,second),'OWNER_KEY_RACE');
      selected={address:remote.address,metadata:{device:remote.metadata.device}};saved=second;
      fd=fs.openSync(saved.path,fs.constants.O_RDONLY|fs.constants.O_NONBLOCK|fs.constants.O_NOFOLLOW);
      check(true);down=false;synced=false;return poll(true);
    }catch(error){close();throw closed(error);}
  }
  function poll(discard=false,currentBudget=()=>true){
    let pressed=false,released=false,empty=false,ignored=false;
    try{
      check(false,true,currentBudget);const ignore=discard||!synced;
      for(let call=0;call<8;call++){
        need(currentBudget()===true,'OWNER_KEY_BUDGET');
        let n;try{n=fs.readSync(fd,buffer,0,buffer.length,null);}catch(error){
          if(error.code==='EAGAIN'||error.code==='EWOULDBLOCK'){empty=true;break;}throw fail('OWNER_KEY_READ');}
        need(Number.isInteger(n)&&n>0&&n<=buffer.length&&n%width===0,'OWNER_KEY_FRAME');
        try{for(let pos=0;pos<n;pos+=width){
          const off=pos+width-8,type=buffer.readUInt16LE(off),code=buffer.readUInt16LE(off+2),value=buffer.readInt32LE(off+4);
          need(!(type===0&&code===3),'OWNER_KEY_DROPPED');
          if(type!==1||code!==expected.keyCode)continue;
          need(value===0||value===1||value===2,'OWNER_KEY_FRAME');
          if(value===0){if(down&&!ignore)released=true;down=false;}
          else {if(value===1&&!down&&!ignore)pressed=true;if(value===1&&!down&&ignore)ignored=true;down=true;}
        }}finally{buffer.fill(0);}
      }
      // Overflow/backlog never delays a queued press into new authority.
      if(!empty){synced=false;pressed=false;released=false;}
      else synced=true;
      // A release arriving during metadata work must be drained before return.
      // This trailing held/path check cannot start another full metadata scan.
      // ignored/tapped are feedback only: an edge seen while discarding, or a
      // press already released in the same batch. Neither grants authority.
      check(false,false,currentBudget);return Object.freeze({pressed:pressed&&down,released,down,ready:synced,
        ignored,tapped:pressed&&released&&!down});
    }catch(error){close();throw closed(error);}
  }
  return Object.freeze({open,poll,close});
}
module.exports={profile,create};
