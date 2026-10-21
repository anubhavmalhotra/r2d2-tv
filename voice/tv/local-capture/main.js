'use strict';
// Explicit alternative entry. Importing this file starts no service or device.
const fs = require('fs');
const net = require('net');
const crypto = require('crypto');
const OWNER = 'local.owner.r2d2mic';
const POWER = 'luna://com.webos.service.tvpower/power/getPowerState';
const PERIOD = 5000, FRESH = 7500;
const code = () => Object.assign(new Error('LOCAL_CAPTURE_CONFIG'), {code:'LOCAL_CAPTURE_CONFIG'});
function need(value) { if (!value) throw code(); }
function exact(value, keys) {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).sort().join(',') === keys.slice().sort().join(',');
}
function safePath(value) {
  return typeof value === 'string' && value.length <= 512 &&
    /^\/(?:[A-Za-z0-9_.-]+\/)*[A-Za-z0-9_.-]+$/.test(value) &&
    value.split('/').every(part => part !== '.' && part !== '..');
}
function configuration(value) {
  const fields=['schema','mode','sparkIp','httpPort','pcmPort','voiceKey','provider','migrationRecord'];
  if(value&&Object.prototype.hasOwnProperty.call(value,'uiQualificationPath'))fields.push('uiQualificationPath','uiCaptureKeyPath');
  need(exact(value,fields));
  need(value.schema === 1 && value.mode === 'lg-local-capture' && net.isIP(value.sparkIp) === 4);
  const parts = value.sparkIp.split('.').map(Number);
  need(parts[0] === 10 || parts[0] === 192 && parts[1] === 168 ||
    parts[0] === 172 && parts[1] >= 16 && parts[1] <= 31);
  need([value.httpPort,value.pcmPort].every(x => Number.isInteger(x) && x >= 1024 && x <= 65535) &&
    value.httpPort !== value.pcmPort && typeof value.voiceKey === 'string' && /^[a-f0-9]{64}$/.test(value.voiceKey));
  need(safePath(value.migrationRecord));
  if(fields.includes('uiQualificationPath'))need(safePath(value.uiQualificationPath)&&safePath(value.uiCaptureKeyPath)&&value.uiQualificationPath!==value.uiCaptureKeyPath);
  const p = value.provider;
  need(exact(p, ['backendProfile','maxMs','modulePath','moduleSha256','supervisorPath','supervisorSha256']));
  // 10000 (the native lease cap) outlasts the keyfilter's 9.2 s hold release; 6000 is
  // the previous activator's value, still run (holds past ~6 s then end in a fault).
  need(p.backendProfile === 1 && (p.maxMs === 10000 || p.maxMs === 6000));
  for (const name of ['modulePath','supervisorPath'])
    need(safePath(p[name]));
  for (const name of ['moduleSha256','supervisorSha256'])
    need(typeof p[name] === 'string' && /^[a-f0-9]{64}$/.test(p[name]));
  return Object.freeze({...value, provider:Object.freeze({...p})});
}
function readConfig(path, io=fs) {
  let fd=null, raw=null;
  try {
    need(typeof path === 'string' && path.startsWith('/') && io.realpathSync(path) === path);
    const a = io.lstatSync(path);
    need(a.isFile() && !a.isSymbolicLink() && a.uid === 0 && a.gid === 0 &&
      a.nlink === 1 && (a.mode & 0o7777) === 0o600 && a.size > 0 && a.size <= 8192);
    const parent = io.lstatSync(require('path').dirname(path));
    need(parent.isDirectory() && !parent.isSymbolicLink() && parent.uid === 0 && !(parent.mode & 0o022));
    fd = io.openSync(path, io.constants.O_RDONLY | io.constants.O_NOFOLLOW | io.constants.O_NONBLOCK);
    const fields = ['dev','ino','mode','uid','gid','nlink','size','mtimeMs','ctimeMs'];
    need(fields.every(k => a[k] === io.fstatSync(fd)[k]));
    raw=Buffer.alloc(a.size+1); let used=0;
    while (used < raw.length) {
      const n=io.readSync(fd,raw,used,raw.length-used,used);
      need(Number.isInteger(n) && n >= 0 && n <= raw.length-used);
      if (!n) break;
      used+=n;
    }
    need(used === a.size && fields.every(k => a[k] === io.fstatSync(fd)[k] && a[k] === io.lstatSync(path)[k]));
    const value=JSON.parse(raw.subarray(0,used).toString('utf8'));
    return configuration(value);
  } catch (_) { throw code(); }
  finally { if (raw) raw.fill(0); if (fd !== null) io.closeSync(fd); }
}
function powerState(reply) {
  if (!reply || reply.returnValue !== true || typeof reply.state !== 'string') return 'unknown';
  const state=reply.state.toLowerCase();
  if (state === 'active' || state === 'on') return 'active';
  return ['standby','suspend','off','power off'].includes(state) ? 'standby' : 'unknown';
}
function monotonic() { const t=process.hrtime(); return t[0]*1000+t[1]/1000000; }
function createRuntime(input, dependencies={}) {
  const config=configuration(input), now=dependencies.now || monotonic;
  // This is an actual inherited descriptor, never a JSON-provided assertion.
  // The entry owns its lifetime; per-utterance factories only borrow it.
  const heldOriginalFd=dependencies.heldOriginalFd;
  const provider=Object.freeze({...config.provider,mode:'capture',device:'',heldOriginalFd});
  const nativeProvider=dependencies.nativeProvider || require('./supervised_provider');
  const later=dependencies.setTimeout || setTimeout, clear=dependencies.clearTimeout || clearTimeout;
  const http=dependencies.http || require('http');
  const identity=dependencies.identity || require('../device_identity');
  const bind=dependencies.bind || require('./owner_service').bind;
  const quiescent=dependencies.assertQuiescent || require('./quiescence').assertQuiescent;
  const getHandle=dependencies.getHandle || (() => require('./bus_context').createHandle());
  const readPower=dependencies.readPower || require('./power_reader').collect;
  const uiQualification=dependencies.uiQualification || require('./ui_qualification');
  const uiHeartbeat=dependencies.uiHeartbeat || require('./ui_heartbeat');
  // On-screen press feedback is optional: a missing module leaves capture unchanged.
  let voiceFeedback=dependencies.voiceFeedback;
  if (voiceFeedback === undefined) try { voiceFeedback=require('./voice_feedback'); } catch (_) { voiceFeedback=null; }
  let bus=null, service=null, power='unknown', observed=-Infinity, started=false, stopping=false;
  let completion=null, sleepWake=null, cleanupComplete=false, cleanupFailed=false;
  let uiChannel=null,uiCompletion=null,uiWake=null,uiAuthorized=false,voice=null;
  const pending=new Set();
  const active=() => !stopping && power === 'active' && now() >= observed && now()-observed <= FRESH;
  function tlsConfig() {
    need(active());
    need(quiescent(config.migrationRecord) === true);
    const keys=identity.load();
    return {address:config.sparkIp,port:config.pcmPort,serverName:'r2d2-public-transport.test',
      serverCertificateSha256:keys.sparkCertificateSha256,ca:Buffer.from(keys.sparkCertificate,'ascii'),
      certificate:Buffer.from(keys.certificate,'ascii'),privateKey:Buffer.from(keys.privateKey,'ascii'),
      deviceKey:crypto.createHash('sha256').update(Buffer.from('R2D2-RESIDENT-PTT-v1\0','ascii'))
        .update(Buffer.from(config.voiceKey,'hex')).update(Buffer.from(keys.certificateSha256,'hex')).digest()};
  }
  async function queryPower() {
    let value=null;
    try { value=await readPower(fs,crypto,now); }
    catch (_) { cleanupFailed=true; stopping=true; power='unknown'; return 'unknown'; }
    // A failed pre-spawn verification owns no child. Once a child may exist,
    // only observed exit AND close establish retirement; never cancel its wait.
    const noChild=value && value.spawned === false && value.pid === null &&
      value.exitObserved === false && value.reaped === false && typeof value.closed === 'boolean' &&
      value.exitCode === null && value.signal === null && value.termSent === false && value.killSent === false &&
      value.result === null && (value.error === 'CLI_PIN' || value.error === 'SPAWN_FAILED');
    const retired=value && value.spawned === true && Number.isInteger(value.pid) && value.pid > 0 &&
      value.exitObserved === true && value.closed === true && value.reaped === true;
    if (!noChild && !retired) {
      cleanupFailed=true; stopping=true; power='unknown'; return 'unknown';
    }
    if (!retired || value.schema !== 1 || value.kind !== 'local-power-reader' ||
        value.readOnly !== true || value.defaultIdentity !== true || value.uri !== POWER ||
        value.policyChanged !== false || value.captureStarted !== false || value.cliVerified !== true ||
        value.complete !== true || value.error !== null || value.exitCode !== 0 || value.signal !== null ||
        value.termSent !== false || value.killSent !== false) return 'unknown';
    return value.result && value.result.returnValue === true &&
      ['active','standby'].includes(value.result.state) ? value.result.state : 'unknown';
  }
  function heartbeat() {
    return new Promise(resolve => {
      if (stopping) return resolve(false);
      let request=null, response=null, done=false, bytes=0;
      const finish=ok => {
        if (done) return; done=true; clear(timer); pending.delete(cancel);
        if (request) request.destroy(); if (response) response.destroy(); resolve(ok);
      };
      const cancel=() => finish(false), timer=later(cancel,2000); pending.add(cancel);
      try {
        request=http.request({host:config.sparkIp,port:config.httpPort,
          path:'/'+config.voiceKey+'/state',method:'GET',
          headers:{Host:config.sparkIp+':'+config.httpPort,'X-R2D2-TV-Power':power}}, reply => {
          response=reply; if (done) { response.destroy(); return; }
          reply.on('error',cancel);
          reply.on('data',chunk => { bytes+=chunk.length; chunk.fill(0); if (bytes>4096) cancel(); });
          reply.on('end',() => finish(reply.statusCode === 200)); reply.on('close',cancel);
        });
        request.on('error',cancel); request.on('close',cancel); request.end();
      } catch (_) { cancel(); }
    });
  }
  function sleep(ms) {
    return new Promise(resolve => {
      if (stopping) return resolve();
      const timer=later(finish,ms);
      function finish() { clear(timer); if (sleepWake === finish) sleepWake=null; resolve(); }
      sleepWake=finish;
    });
  }
  function stopUi(){if(uiChannel)uiChannel.close();if(uiWake)uiWake();}
  function stopVoice(){const v=voice;voice=null;if(v)try{v.close();}catch(_){}}
  async function uiLoop(){
    try{
      while(!stopping&&uiChannel.enabled()){
        const began=now();await uiChannel.poll();
        if(stopping||!uiChannel.enabled())break;
        await new Promise(resolve=>{
          const timer=later(finish,Math.max(0,500-(now()-began)));
          function finish(){clear(timer);if(uiWake===finish)uiWake=null;resolve();}uiWake=finish;
        });
      }
    }finally{uiAuthorized=false;stopUi();}
  }
  async function loop() {
    try {
      while (!stopping) {
        const began=now(), next=await queryPower();
        if (stopping) break;
        power=next; observed=now();
        if (!active()) await service.cancelActive(power === 'standby' ? 'TV_INACTIVE' : 'POWER_UNKNOWN');
        if (!stopping) await heartbeat();
        await sleep(Math.max(0,PERIOD-(now()-began)));
      }
    } finally {
      stopping=true;
      stopUi();if(uiCompletion)try{await uiCompletion;}catch(_){cleanupFailed=true;}
      for (const cancel of [...pending]) cancel();
      stopVoice();
      if (sleepWake) sleepWake();
      if (service) try {
        const receipt=await service.close();
        if (!receipt || receipt.cleanupComplete !== true) cleanupFailed=true;
      } catch (_) { cleanupFailed=true; }
      if (bus) try { bus.removeAllListeners(); bus.unregister(); } catch (_) { cleanupFailed=true; }
      cleanupComplete=!cleanupFailed && pending.size === 0;
    }
  }
  function start() {
    if (started) throw code(); started=true;
    try {
      need(Number.isInteger(heldOriginalFd) && heldOriginalFd >= 3);
      need(nativeProvider.validateOriginalFd(heldOriginalFd) === true);
      need(quiescent(config.migrationRecord) === true);
      // Additional collision guard only. The installer must independently reap
      // the old raw-HID owner and replace its startup entry before this runs;
      // an old helper may hold HID before registering an LS2 identity.
      bus=getHandle();
      // Enums and closed text only; toasts stop at standby and shutdown.
      try {
        if (voiceFeedback) voice=voiceFeedback.create({sparkIp:config.sparkIp,httpPort:config.httpPort,voiceKey:config.voiceKey,
          allowed:()=>!stopping&&power!=='standby'},{http,identity,setTimeout:later,clearTimeout:clear,now});
      } catch (_) { voice=null; }
      service=bind(bus,{provider,getTlsConfig:tlsConfig,isActive:active,isUiQualified:()=>uiAuthorized&&!!uiChannel&&uiChannel.enabled(),feedback:voice});
      if(config.uiQualificationPath&&uiQualification.qualified(config.uiQualificationPath,provider)){
        let key=null;
        try{
          key=uiQualification.readKey(config.uiCaptureKeyPath,config.voiceKey);
          uiChannel=uiHeartbeat.create({key,address:config.sparkIp,port:config.httpPort,voiceKey:config.voiceKey,service},{http,setTimeout:later,clearTimeout:clear});
          uiAuthorized=true;uiCompletion=uiLoop();
          // Observe a rejected loop immediately; loop() still checks it while
          // joining teardown so a failed cleanup is never reported complete.
          uiCompletion.catch(()=>{uiAuthorized=false;stopUi();});
        }catch(_){uiAuthorized=false;stopUi();}
        finally{if(key)key.fill(0);}
      }
    } catch (_) {
      stopping=true;stopVoice();
      if (bus) try { bus.removeAllListeners(); bus.unregister(); } catch (_) { cleanupFailed=true; }
      throw code();
    }
    completion=loop(); return completion;
  }
  async function stop() {
    stopping=true; power='unknown';
    stopUi();stopVoice();
    for (const cancel of [...pending]) cancel();
    if (sleepWake) sleepWake();
    if (service) try { await service.cancelActive('CLIENT_CLOSED'); } catch (_) { cleanupFailed=true; }
    if (completion) try { await completion; } catch (_) { cleanupFailed=true; cleanupComplete=false; }
    return status();
  }
  function status() { return Object.freeze({power,active:active(),started,stopping,cleanupComplete}); }
  return Object.freeze({start,stop,status});
}
async function run(path, heldOriginalFd, dependencies={}) {
  const proc=dependencies.process || process, io=dependencies.fs || fs;
  let runtime=null, stop=null;
  need(Number.isInteger(heldOriginalFd) && heldOriginalFd >= 3);
  try {
    need(proc.getuid && proc.getuid() === 0);
    const config=(dependencies.readConfig || readConfig)(path);
    runtime=(dependencies.createRuntime || createRuntime)(config,{heldOriginalFd});
    stop=() => { void runtime.stop(); };
    proc.on('SIGINT',stop); proc.on('SIGTERM',stop);
    await runtime.start();
    if (!runtime.status().cleanupComplete) proc.exitCode=1;
  } finally {
    if (stop) { proc.removeListener('SIGINT',stop); proc.removeListener('SIGTERM',stop); }
    try {
      if (runtime && (await runtime.stop()).cleanupComplete !== true) proc.exitCode=1;
    } finally { io.closeSync(heldOriginalFd); }
  }
}
module.exports={OWNER,POWER,PERIOD,FRESH,configuration,readConfig,powerState,createRuntime,run};
if (require.main === module) {
  if (process.argv.length !== 5 || process.argv[3] !== '--original-fd' || !/^[1-9][0-9]*$/.test(process.argv[4])) process.exitCode=1;
  else run(process.argv[2],Number(process.argv[4])).catch(() => { process.exitCode=1; });
}
