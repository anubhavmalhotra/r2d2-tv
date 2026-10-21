'use strict';
const assert=require('assert'), {EventEmitter}=require('events'), fs=require('fs');
const M=require('./main');
const ip=['192','168','60','80'].join('.');
function config() {
  return {schema:1,mode:'lg-local-capture',sparkIp:ip,httpPort:4181,pcmPort:4182,voiceKey:'a'.repeat(64),migrationRecord:'/fixture/migration.json',
    provider:{backendProfile:1,maxMs:10000,modulePath:'/fixture/owned-provider.so',moduleSha256:'b'.repeat(64),
      supervisorPath:'/fixture/provider-supervisor',supervisorSha256:'c'.repeat(64)}};
}
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function fixture(states=['active'], cleanup=true, uiOptions=null, voiceFeedback) {
  let at=0, sequence=0, unregistered=0, identityReads=0, owner=null, queries=0, closed=0, identityModule=null, httpModule=null;
  const timers=new Map(), requests=[], chunks=[], cancellations=[], heartbeats=[];
  const bus=new EventEmitter();
  const uiStats={qualificationReads:0,keyReads:0,created:0,polls:0,closed:0,key:null};
  bus.unregister=()=>{unregistered++;};
  bus.call=()=>assert.fail('owner palmbus power query must never run');
  function observation(state) {
    if (state && typeof state === 'object') return state;
    return {schema:1,kind:'local-power-reader',readOnly:true,captureStarted:false,policyChanged:false,
      uri:M.POWER,defaultIdentity:true,cliVerified:true,complete:state!=='error',spawned:true,pid:432,
      exitObserved:true,closed:true,reaped:true,exitCode:0,signal:null,termSent:false,killSent:false,
      stdoutBytes:43,stderrBytes:0,result:state==='error'?null:{returnValue:true,state,errorCode:null,errorText:null,message:null},
      error:state==='error'?'REPLY_INVALID':null,elapsedMs:1};
  }
  const input=config();if(uiOptions){input.uiQualificationPath='/fixture/ui-trial.json';input.uiCaptureKeyPath='/fixture/ui-key';}
  const runtime=M.createRuntime(input,{voiceFeedback,
    uiQualification:{qualified(){uiStats.qualificationReads++;return uiOptions&&uiOptions.allowed===true;},readKey(){uiStats.keyReads++;if(uiOptions.badKey)throw Error('KEY');uiStats.key=Buffer.alloc(32,7);return uiStats.key;}},
    uiHeartbeat:{create(options){uiStats.created++;assert.strictEqual(options.key,uiStats.key);let enabled=true;return{poll:async()=>{uiStats.polls++;return true;},close(){if(enabled)uiStats.closed++;enabled=false;},enabled:()=>enabled};}},
    heldOriginalFd:9,nativeProvider:{validateOriginalFd:fd=>{assert.equal(fd,9);return true;}},
    readPower:async (io,crypt,now)=>{assert.strictEqual(io,fs);assert.equal(typeof crypt.createHash,'function');assert.equal(now(),at);queries++;const state=states[Math.min(queries-1,states.length-1)];if(typeof state==='function')return state();return observation(state);},
    now:()=>at,setTimeout:(f,ms)=>{const id=++sequence;timers.set(id,{f,at:at+ms});return id;},
    clearTimeout:id=>timers.delete(id),getHandle:()=>bus,
    assertQuiescent:path=>{assert.equal(path,'/fixture/migration.json');return true;},
    identity:identityModule={load(){identityReads++;return {sparkCertificateSha256:'d'.repeat(64),
      certificateSha256:'e'.repeat(64),sparkCertificate:'public-server-cert',certificate:'public-device-cert',privateKey:'test-only-private-material'};}},
    bind(handle,options){
      assert.strictEqual(handle,bus);owner=options;
      assert.equal(options.provider.heldOriginalFd,9);
      assert.equal(options.provider.mode,'capture');assert.equal(options.provider.device,'');
      return {cancelActive:async why=>{cancellations.push(why);},close:async()=>{closed++;return {cleanupComplete:cleanup};},status:()=>({})};
    },
    http:httpModule={request(options,callback){
      heartbeats.push(options);const request=new EventEmitter();request.destroy=()=>{};
      request.end=()=>queueMicrotask(()=>{
        const response=new EventEmitter();response.destroy=()=>{};response.statusCode=200;
        callback(response);const bytes=Buffer.from('{"session":"none"}');chunks.push(bytes);
        response.emit('data',bytes);response.emit('end');
      });return request;
    }}
  });
  async function advance(ms) {
    at+=ms;
    for(let i=0;i<20;i++) {
      const due=[...timers.entries()].filter(([,v])=>v.at<=at).sort((a,b)=>a[1].at-b[1].at);
      if(!due.length)break;
      for(const [id,v] of due)if(timers.delete(id))v.f();
      await tick();
    }
  }
  return {runtime,advance,uiStats,elapse(ms){at+=ms;},observation,get owner(){return owner;},identityModule,httpModule,
    stats:()=>({unregistered,identityReads,queries,closed,timers:timers.size}),requests,chunks,cancellations,heartbeats};
}
async function main() {
  for(const change of [x=>{x.mode='raw-hid';},x=>{x.sparkIp='198.51.100.10';},
    x=>{x.provider.backendProfile=2;},x=>{x.provider.maxMs=6001;},x=>{x.provider.maxMs=10001;},x=>{x.provider.maxMs=9999;},x=>{x.provider.maxMs='10000';},x=>{x.provider.device='remote0';},
    x=>{x.provider.modulePath='/fixture/../owned-provider.so';},x=>{x.provider.supervisorPath='/fixture/..';},x=>{x.schema=true;}]) {
    const x=config();change(x);assert.throws(()=>M.configuration(x));
  }
  assert(!Object.hasOwn(M.configuration(config()).provider,'device'));
  // 10000 is the production lease; the previous activator's 6000 still runs.
  assert.equal(M.configuration(config()).provider.maxMs,10000);
  {const old=config();old.provider.maxMs=6000;assert.equal(M.configuration(old).provider.maxMs,6000);}
  assert.deepEqual(M.configuration(M.configuration(config())),M.configuration(config()));
  const dotted=config();dotted.migrationRecord='/var/lib/local.owner.r2d2mic/migration.json';
  dotted.provider.modulePath='/var/lib/local.owner.r2d2mic/local-capture/owned-provider.so';
  assert.equal(M.configuration(dotted).migrationRecord,dotted.migrationRecord);
  assert.equal(M.powerState({returnValue:false,state:'active'}),'unknown');
  assert.equal(M.powerState({returnValue:true,state:'Active'}),'active');
  let handles=0;
  const unretired=M.createRuntime(config(),{assertQuiescent:()=>false,
    heldOriginalFd:9,nativeProvider:{validateOriginalFd:()=>true},
    bind:()=>assert.fail('provider service must not be bound before retirement'),
    getHandle:()=>{handles++;assert.fail('bus must not be registered before retirement');}});
  assert.throws(()=>unretired.start());assert.equal(handles,0);
  for(const heldOriginalFd of [undefined,-1,0,2,3.5,'9']) {
    const noImage=M.createRuntime(config(),{heldOriginalFd,
      nativeProvider:{validateOriginalFd:()=>assert.fail('invalid inherited FD')},
      getHandle:()=>assert.fail('missing inherited original must not register bus')});
    assert.throws(()=>noImage.start());
  }
  const changedImage=M.createRuntime(config(),{heldOriginalFd:9,
    nativeProvider:{validateOriginalFd:()=>false},getHandle:()=>assert.fail('unverified inherited image')});
  assert.throws(()=>changedImage.start());

  const f=fixture(['active','standby']);
  assert.equal(f.runtime.status().active,false);
  const done=f.runtime.start();await tick();
  assert.equal(f.runtime.status().active,true);assert.equal(f.owner.isActive(),true);
  assert.equal(f.stats().identityReads,0);
  const first=f.owner.getTlsConfig(), second=f.owner.getTlsConfig();
  assert.equal(f.stats().identityReads,2);assert.notStrictEqual(first.privateKey,second.privateKey);
  assert(first.deviceKey.equals(second.deviceKey));assert.equal(first.deviceKey.length,32);
  for(const cfg of [first,second])for(const k of ['ca','certificate','privateKey','deviceKey'])cfg[k].fill(0);
  assert.equal(f.heartbeats[0].headers['X-R2D2-TV-Power'],'active');
  assert(f.chunks.every(x=>x.every(byte=>byte===0)));
  await f.advance(5000);
  assert.equal(f.runtime.status().active,false);
  assert(f.cancellations.includes('TV_INACTIVE'));
  assert.equal(f.heartbeats[1].headers['X-R2D2-TV-Power'],'standby');
  assert.throws(()=>f.owner.getTlsConfig());
  const closed=await f.runtime.stop();await done;
  assert.equal(closed.cleanupComplete,true);
  assert.deepEqual(f.stats(),{unregistered:1,identityReads:2,queries:2,closed:1,timers:0});
  assert.equal(f.requests.length,0);

  const stale=fixture();const staleDone=stale.runtime.start();await tick();
  stale.elapse(M.FRESH+1);assert.equal(stale.owner.isActive(),false);
  assert.throws(()=>stale.owner.getTlsConfig());await stale.runtime.stop();await staleDone;

  const unknown=fixture(['error']);const unknownDone=unknown.runtime.start();await tick();
  assert.equal(unknown.runtime.status().power,'unknown');assert.equal(unknown.owner.isActive(),false);
  assert(unknown.cancellations.includes('POWER_UNKNOWN'));await unknown.runtime.stop();await unknownDone;

  // stop cancels active capture immediately, but waits for the actual reader
  // retirement rather than abandoning its Promise or registering another poll.
  let finishPower=null;
  const pending=fixture([()=>new Promise(resolve=>{finishPower=resolve;})]);
  const pendingDone=pending.runtime.start();await tick();assert.equal(pending.stats().queries,1);
  let stopped=false;const pendingStop=pending.runtime.stop().then(value=>{stopped=true;return value;});
  await tick();assert(pending.cancellations.includes('CLIENT_CLOSED'));assert.equal(stopped,false);
  assert.equal(pending.runtime.status().active,false);assert.equal(pending.runtime.status().cleanupComplete,false);
  finishPower(pending.observation('active'));const pendingStatus=await pendingStop;await pendingDone;
  assert(pendingStatus.cleanupComplete);assert.equal(pending.stats().queries,1);assert.equal(pending.heartbeats.length,0);

  const childUnknown=fixture();const unreaped=childUnknown.observation('active');
  Object.assign(unreaped,{complete:false,exitObserved:false,closed:false,reaped:false,exitCode:null,result:null,error:'REAP_UNCONFIRMED'});
  const failed=fixture([unreaped]);const failedDone=failed.runtime.start();await tick();
  assert.equal(failed.runtime.status().stopping,true);await failedDone;assert.equal(failed.runtime.status().active,false);
  assert.equal(failed.runtime.status().cleanupComplete,false);assert.equal(failed.stats().queries,1);
  await failed.advance(30000);assert.equal(failed.stats().queries,1);
  assert.equal((await failed.runtime.stop()).cleanupComplete,false);assert.equal(failed.heartbeats.length,0);

  const preSpawn=fixture([{schema:1,kind:'local-power-reader',readOnly:true,captureStarted:false,policyChanged:false,
    uri:M.POWER,defaultIdentity:true,cliVerified:false,complete:false,spawned:false,pid:null,
    exitObserved:false,closed:false,reaped:false,exitCode:null,signal:null,termSent:false,killSent:false,
    stdoutBytes:0,stderrBytes:0,result:null,error:'CLI_PIN',elapsedMs:0}]);
  const preSpawnDone=preSpawn.runtime.start();await tick();assert.equal(preSpawn.runtime.status().power,'unknown');
  assert.equal(preSpawn.runtime.status().stopping,false);assert.equal((await preSpawn.runtime.stop()).cleanupComplete,true);await preSpawnDone;

  const uncertain=fixture([()=>Promise.reject(Error('PRIVATE_READER_ERROR'))]);const uncertainDone=uncertain.runtime.start();await tick();
  assert.equal(uncertain.runtime.status().stopping,true);await uncertainDone;assert.equal(uncertain.runtime.status().cleanupComplete,false);
  assert.equal(uncertain.stats().queries,1);assert.equal((await uncertain.runtime.stop()).cleanupComplete,false);

  for (const change of [v=>{v.complete=false;},v=>{v.result.returnValue=false;},v=>{v.result.state='on';},v=>{v.cliVerified=false;}]) {
    const source=fixture(),value=source.observation('active');change(value);const guarded=fixture([value]);
    const guardedDone=guarded.runtime.start();await tick();assert.equal(guarded.runtime.status().active,false);
    assert.throws(()=>guarded.owner.getTlsConfig());await guarded.runtime.stop();await guardedDone;
  }

  const dirty=fixture(['active'],false);const dirtyDone=dirty.runtime.start();await tick();
  assert.equal((await dirty.runtime.stop()).cleanupComplete,false);await dirtyDone;
  assert.equal(dirty.stats().unregistered,1);

  // Exercise the entry's actual bounded reader, including buffer erasure and
  // rejection of a changed opened file. No root files or credentials are read.
  const bytes=Buffer.from(JSON.stringify(config()));let changed=false, closedFd=0, held=null;
  const meta={dev:1,ino:2,uid:0,gid:0,nlink:1,mode:0o100600,size:bytes.length,mtimeMs:1,ctimeMs:1,
    isFile:()=>true,isSymbolicLink:()=>false};
  const io={constants:fs.constants,realpathSync:p=>p,
    lstatSync:p=>p==='/fixture'?{uid:0,mode:0o40700,isDirectory:()=>true,isSymbolicLink:()=>false}:meta,
    fstatSync:()=>({...meta,ino:changed?3:2}),openSync:()=>99,closeSync:()=>{closedFd++;},
    readSync(fd,buffer,offset,length,pos){assert.equal(fd,99);held=buffer;
      const n=Math.min(length,bytes.length-pos);if(n>0)bytes.copy(buffer,offset,pos,pos+n);return Math.max(0,n);}};
  assert.equal(M.configuration(M.readConfig('/fixture/config.json',io)).mode,'lg-local-capture');
  assert(held.every(x=>x===0));assert.equal(closedFd,1);
  changed=true;assert.throws(()=>M.readConfig('/fixture/config.json',io));assert.equal(closedFd,2);

  // Exercise the production entry's ownership of the inherited image: it is
  // closed once, after cleanup, including setup/start failures.
  for(const failure of ['none','config','start','cleanup']) {
    const proc=new EventEmitter();proc.getuid=()=>0;
    const events=[];let stopped=false;
    await M.run('/fixture/config.json',9,{process:proc,
      fs:{closeSync:fd=>{assert.equal(fd,9);assert.equal(stopped,failure!=='config');events.push('close');}},
      readConfig:()=>{if(failure==='config')throw Error('CONFIG');return config();},
      createRuntime:(cfg,deps)=>{
        assert.equal(deps.heldOriginalFd,9);
        return {start:async()=>{events.push('start');if(failure==='start')throw Error('START');},
          stop:async()=>{stopped=true;events.push('stop');return {cleanupComplete:failure!=='cleanup'};},
          status:()=>({cleanupComplete:failure!=='cleanup'})};
      }}).then(()=>assert(['none','cleanup'].includes(failure)),()=>assert(['config','start'].includes(failure)));
    assert.equal(events.filter(x=>x==='close').length,1);
    assert.equal(events[events.length-1],'close');
    assert.equal(proc.listenerCount('SIGINT')+proc.listenerCount('SIGTERM'),0);
    if(failure==='cleanup')assert.equal(proc.exitCode,1);
  }
  for(const optional of [{uiCaptureKeyPath:'/fixture/key'},{uiQualificationPath:'/fixture/trial'},{uiQualificationPath:'/fixture/trial',uiCaptureKeyPath:'/fixture/trial'}])assert.throws(()=>M.configuration({...config(),...optional}));
  const ui=fixture(['active'],true,{allowed:true});const uiDone=ui.runtime.start();await tick();
  assert(ui.owner.isUiQualified());assert.equal(ui.uiStats.created,1);assert.equal(ui.uiStats.polls,1);assert(ui.uiStats.key.every(x=>x===0));
  await ui.advance(500);assert.equal(ui.uiStats.polls,2);assert.equal(ui.stats().queries,1);
  assert((await ui.runtime.stop()).cleanupComplete);await uiDone;assert.equal(ui.uiStats.closed,1);assert.equal(ui.stats().timers,0);assert.equal(ui.owner.isUiQualified(),false);
  for(const options of [{allowed:false},{allowed:true,badKey:true}]){
    const disabled=fixture(['active'],true,options);const disabledDone=disabled.runtime.start();await tick();
    assert.equal(disabled.owner.isUiQualified(),false);assert.equal(disabled.uiStats.created,0);assert.equal(disabled.owner.isActive(),true);
    assert((await disabled.runtime.stop()).cleanupComplete);await disabledDone;
  }
  // Physical-press feedback: created at start with the validated endpoint and the runtime's
  // own http/identity/timers, handed to the owner, gated on standby, closed exactly once.
  const made=[];let closes=0;
  const fake={create(cfg,deps){const obj={begin(){},close(){closes++;}};made.push({cfg,deps,obj});return obj;}};
  const fb=fixture(['active','standby'],true,null,fake);assert.equal(made.length,0);
  const fbDone=fb.runtime.start();await tick();
  assert.equal(made.length,1);assert.deepEqual(Object.keys(made[0].cfg).sort(),['allowed','httpPort','sparkIp','voiceKey']);
  assert.equal(made[0].cfg.sparkIp,ip);assert.equal(made[0].cfg.httpPort,4181);assert.equal(made[0].cfg.voiceKey,'a'.repeat(64));
  assert.deepEqual(Object.keys(made[0].deps).sort(),['clearTimeout','http','identity','now','setTimeout']);
  assert.strictEqual(fb.owner.feedback,made[0].obj);assert.strictEqual(made[0].deps.http,fb.httpModule);assert.strictEqual(made[0].deps.identity,fb.identityModule);
  assert.equal(made[0].cfg.allowed(),true);assert.equal(fb.stats().identityReads,0);assert.equal(fb.heartbeats.length,1);
  await fb.advance(5000);assert.equal(made[0].cfg.allowed(),false);
  assert((await fb.runtime.stop()).cleanupComplete);await fbDone;assert.equal(closes,1);assert.equal(made[0].cfg.allowed(),false);assert.equal(fb.stats().timers,0);
  // A failing or absent feedback module leaves capture exactly as before.
  for(const broken of [{create(){throw Error('VOICE');}},null]){
    const plain=fixture(['active'],true,null,broken);const plainDone=plain.runtime.start();await tick();
    assert.strictEqual(plain.owner.feedback,null);assert.equal(plain.owner.isActive(),true);
    assert((await plain.runtime.stop()).cleanupComplete);await plainDone;
  }
  // The real module by default: inert until a press, closed with the runtime.
  const real=fixture();const realDone=real.runtime.start();await tick();
  assert.equal(typeof real.owner.feedback.settle,'function');assert.equal(real.owner.feedback.status().closed,false);
  assert((await real.runtime.stop()).cleanupComplete);await realDone;
  assert.equal(real.owner.feedback.status().closed,true);assert.equal(real.stats().timers,0);assert.equal(real.stats().identityReads,0);
  console.log('local capture entry: power, standby, freshness, credentials, cleanup, config, independent UI loop and voice feedback controls passed');
}
const watchdog=setTimeout(()=>{console.error('local capture entry test did not finish');process.exit(1);},2000);
main().then(()=>clearTimeout(watchdog),error=>{clearTimeout(watchdog);console.error(error);process.exitCode=1;});
