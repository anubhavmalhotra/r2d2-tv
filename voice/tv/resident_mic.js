'use strict';
/** Resident Magic Remote capture. Importing this module performs no I/O.
 * Route installation belongs to the installer: this process never mounts, kills
 * another process, removes a legacy service, or changes the compositor.
 * Speech and credentials are never logged or written by this module.
 */
const OWNER = 'local.owner.r2d2mic';
const SET_REPORT = 'luna://com.webos.service.bluetooth2/hid/internal/setReport';
const POWER = 'luna://com.webos.service.tvpower/power/getPowerState';
const TOAST = 'luna://com.webos.notification/createToast';
const TARGETS = [
  ['/usr/sbin/voiceconductor', 'conductor-stub'],
  ['/usr/lib/qml/WebOSCompositor/controllers/StarfishKeyFilter.qml', 'StarfishKeyFilter.qml'],
  ['/usr/lib/qml/KeyFilters/appLaunch.js', 'appLaunch.js'],
  ['/usr/sbin/voiceinput_hidraw', 'conductor-stub']
];
const ENV = {LANG:'C', LC_ALL:'C', PATH:'/usr/sbin:/usr/bin:/sbin:/bin',
  HOME:'/tmp', NODE_PATH:'/usr/palm/frameworks:/usr/lib/nodejs:/usr/lib/node_modules'};
const CAPTURE_MS = 5500, MAX_REPORTS = 400, MAX_RAW_BYTES = MAX_REPORTS * 124;
// Budget faults that mean only "the hold limit passed", not a broken fence.
const DEADLINE_CODES = new Set(['CAPTURE_DEADLINE', 'METADATA_BUDGET', 'OWNER_KEY_BUDGET']);
// Closed TV-side faults, each with the fixed toast used when no R2D2 surface shows it.
const FAULTS = Object.freeze({OWNER_KEY_RELEASED:'Hold the microphone button while speaking',
  START_UNCONFIRMED:'Remote mic did not start. Try again',CAPTURE_LOST:'Voice command unavailable',
  STOP_UNCONFIRMED:'Voice is off. Restart the TV to use voice',NO_SPEECH:'Hold the microphone button while speaking',
  BUSY:'Voice is busy, try again in a moment',TRANSPORT_FAILED:'Voice command unavailable',
  RESULT_UNCONFIRMED:'Sent, but not confirmed. Check the TV'});
const PHASE_DOMAIN = 'R2D2-voice-phase-v1\0';
const SURFACES = ['home', 'viewer', 'other'];
const fault = code => Object.assign(new Error(code), {code});
function configuration(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw fault('CONFIG');
  const ip = value.sparkIp;
  if (typeof ip !== 'string' || !/^(?:0|[1-9][0-9]{0,2})(?:\.(?:0|[1-9][0-9]{0,2})){3}$/.test(ip)) throw fault('CONFIG');
  const p = ip.split('.').map(Number);
  if (p.some(n => n > 255) || !(p[0]===10 || p[0]===192&&p[1]===168 || p[0]===172&&p[1]>=16&&p[1]<=31)) throw fault('CONFIG');
  const out = {sparkIp:ip, httpPort:value.httpPort===undefined?4181:value.httpPort,
    pcmPort:value.pcmPort===undefined?4182:value.pcmPort, voiceKey:value.voiceKey,
    routeDir:value.routeDir,hidProfile:value.hidProfile,ownerKeyProfile:value.ownerKeyProfile};
  if (![out.httpPort,out.pcmPort].every(n=>Number.isInteger(n)&&n>=1024&&n<=65535) ||
      out.httpPort===out.pcmPort || typeof out.voiceKey!=='string'||!/^[a-f0-9]{64}$/.test(out.voiceKey) ||
      typeof out.routeDir!=='string' || out.routeDir.length>512 || !/^\/(?:[A-Za-z0-9_-]+\/)*[A-Za-z0-9_-]+$/.test(out.routeDir)) throw fault('CONFIG');
  const h=out.hidProfile;
  if(!h||typeof h!=='object'||Array.isArray(h)||Object.keys(h).sort().join(',')!=='descriptorBytes,descriptorSha256,identitySha256,schema'||
    h.schema!==1||!Number.isInteger(h.descriptorBytes)||h.descriptorBytes<1||h.descriptorBytes>4096||
    ![h.descriptorSha256,h.identitySha256].every(s=>typeof s==='string'&&/^[a-f0-9]{64}$/.test(s)))throw fault('CONFIG');
  out.hidProfile=Object.freeze({...h});
  const k=out.ownerKeyProfile;
  if(!k||typeof k!=='object'||Array.isArray(k)||Object.keys(k).sort().join(',')!=='eventBytes,identitySha256,keyCode,schema'||
    k.schema!==1||![16,24].includes(k.eventBytes)||!Number.isInteger(k.keyCode)||k.keyCode<1||k.keyCode>0x2ff||
    typeof k.identitySha256!=='string'||!/^[a-f0-9]{64}$/.test(k.identitySha256))throw fault('OWNER_KEY_PROFILE');
  out.ownerKeyProfile=Object.freeze({...k});
  return Object.freeze(out);
}
function powerState(reply) {
  if (!reply || reply.returnValue!==true || typeof reply.state!=='string') return 'unknown';
  const state=reply.state.toLowerCase();
  if (state==='active'||state==='on') return 'active';
  if (['standby','suspend','off','power off'].includes(state)) return 'standby';
  return 'unknown';
}
function sameNode(a,b) {
  return a&&b&&['dev','ino','rdev','uid','gid','mode','nlink'].every(k=>a[k]===b[k])&&b.nlink>0;
}
function createResident(input, dependencies={}) {
  const config=configuration(input), fs=dependencies.fs||require('fs');
  const http=dependencies.http||require('http'), crypto=dependencies.crypto||require('crypto');
  const cp=dependencies.cp||require('child_process'), root=dependencies.root||__dirname;
  const palmbus=dependencies.palmbus||require('/usr/lib/nodejs/palmbus');
  const hid=dependencies.shape&&dependencies.remoteAddress&&dependencies.recording?null:
    require(root+'/resident_hid').create(config.hidProfile);
  const shape=dependencies.shape||hid,remote=dependencies.remoteAddress||hid,recording=dependencies.recording||hid;
  const tlsClient=dependencies.tlsClient||require(root+'/tls_pcm_client');
  const identity=dependencies.deviceIdentity||require(root+'/device_identity');
  const ownerKey=dependencies.ownerKey||require(root+'/owner_key').create(config.ownerKeyProfile);
  const now=dependencies.now||(()=>Number(process.hrtime.bigint()/1000000n));
  const later=dependencies.setTimeout||setTimeout, clear=dependencies.clearTimeout||clearTimeout;
  let stopping=false, started=false, finished=null, power='unknown', bus=null;
  let io=null, fd=null, selected=null, activeSession=null, firstOpen=true, discarding=false;
  let lastState='idle', captures=0, processed=0, stopConfirmed=true, cleanupComplete=false;
  let closeFailed=false, lastCheck=-Infinity, sessionClose=null, releasedSession=false, lastHint=-Infinity;
  let boot=null, seq=0;
  const waits=new Set(), requests=new Set(), children=new Set(), busCalls=new Set();
  const childJobs=new Set();
  function status() { return Object.freeze({power,state:lastState,captures,processed,stopping,stopConfirmed,cleanupComplete}); }
  function sleep(ms) {
    if (stopping) return Promise.resolve();
    return new Promise(resolve=>{const finish=()=>{clear(timer);waits.delete(finish);resolve();};
      const timer=later(finish,ms);waits.add(finish);});
  }
  function fences() {
    try {
      if (fs.realpathSync(config.routeDir)!==config.routeDir) return false;
      const directory=fs.lstatSync(config.routeDir);
      if (!directory.isDirectory()||directory.uid!==0||(directory.mode&0o022)) return false;
      const mounts=fs.readFileSync('/proc/self/mountinfo','utf8');
      if (mounts.length>262144) return false;
      const points=new Set(mounts.trim().split('\n').map(line=>line.split(' ')[4]));
      return TARGETS.every(([target,name])=>{
        const source=fs.lstatSync(config.routeDir+'/'+name), current=fs.lstatSync(target);
        return points.has(target)&&source.isFile()&&current.isFile()&&source.uid===0&&current.uid===0&&
          !(source.mode&0o022)&&!(current.mode&0o022)&&source.dev===current.dev&&source.ino===current.ino;
      });
    } catch (_) { return false; }
  }
  function busCall(uri,payload,timeout=1500) {
    return new Promise(resolve=>{
      let request=null, done=false;
      const finish=value=>{if(done)return;done=true;clear(timer);busCalls.delete(cancel);
        if(request){try{request.removeAllListeners();request.cancel();}catch(_){closeFailed=true;}}
        resolve(value);};
      const cancel=()=>finish(null), timer=later(cancel,timeout);busCalls.add(cancel);
      try {
        request=bus.call(uri,JSON.stringify(payload));
        request.on('error',cancel);
        request.on('response',message=>{let value=null;try{const text=message.payload();if(typeof text==='string'&&text.length<=8192)value=JSON.parse(text);}catch(_){}finish(value);});
      } catch (_) { cancel(); }
    });
  }
  function luna(uri,payload,timeout=2000) {
    const job=new Promise(resolve=>{
      let child=null, done=false;
      function finish(value){if(done)return;done=true;if(child)children.delete(child);resolve(value);}
      try {
        child=cp.execFile('/usr/bin/luna-send',['-n','1','-f',uri,JSON.stringify(payload)],
          {timeout,killSignal:'SIGKILL',maxBuffer:8192,env:ENV},(error,stdout)=>{
            let value=null;try{if(!error&&typeof stdout==='string'&&stdout.length<=8192)value=JSON.parse(stdout);}catch(_){}finish(value);
          });
        if (!done) children.add(child);
      } catch (_) { finish(null); }
    });
    childJobs.add(job);job.then(()=>childJobs.delete(job));return job;
  }
  async function setReport(address,start) {
    const payload={address,reportType:'feature',reportData:[249,start?3:4]};
    const reply=await busCall(SET_REPORT,payload);
    if(reply&&reply.returnValue===true)return true;
    // START is never retried after an uncertain reply. STOP may use the existing
    // second transport, and its acknowledgement still is not physical-off proof.
    if(start)return false;
    const fallback=await luna(SET_REPORT,payload,1500);
    return !!(fallback&&fallback.returnValue===true);
  }
  function toast(message) { if(!stopping) void luna(TOAST,{sourceId:'com.webos.app.home',message},1500); }
  // One bounded plain-HTTP exchange with Spark: the parsed 200 body, else null. Never rejects.
  function exchange(path,method,headers,body,limit,timeout) {
    if(stopping)return Promise.resolve(null);
    return new Promise(resolve=>{
      let req=null,res=null,done=false,size=0,chunks=[];
      const finish=value=>{if(done)return;done=true;clear(timer);requests.delete(cancel);
        if(req)req.destroy();if(res)res.destroy();for(const chunk of chunks)chunk.fill(0);chunks=[];resolve(value);};
      const cancel=()=>finish(null);
      const timer=later(()=>finish(null),timeout);
      try {
        req=http.request({host:config.sparkIp,port:config.httpPort,path:'/'+config.voiceKey+path,method,
          headers:{Host:config.sparkIp+':'+config.httpPort,...headers}},response=>{
          res=response;if(done){res.destroy();return;}
          res.on('error',()=>finish(null));
          res.on('data',part=>{if(done)return;size+=part.length;if(size>limit)return finish(null);chunks.push(Buffer.from(part));});
          res.on('end',()=>{let value=null,data=null;try{if(res.statusCode===200){data=Buffer.concat(chunks);value=JSON.parse(data.toString('utf8'));}}catch(_){value=null;}finally{if(data)data.fill(0);}finish(value);});
          res.on('close',cancel);
        });
        requests.add(cancel);req.on('error',cancel);req.on('close',cancel);req.end(body);
      } catch (_) { finish(null); }
    });
  }
  function reportState() {
    return exchange('/state','GET',{'X-R2D2-TV-Power':power},undefined,4096,2000)
      .then(raw=>raw&&{session:raw.session==='active'?'active':'other'});
  }
  const deviceKey=keys=>crypto.createHash('sha256').update(Buffer.from('R2D2-RESIDENT-PTT-v1\0','ascii'))
    .update(Buffer.from(config.voiceKey,'hex')).update(Buffer.from(keys.certificateSha256,'hex')).digest();
  // A press has a random id and one MAC key (the PCM device key), loaded once and
  // after the current turn so identity parsing never delays START. Wiped at the end.
  function openPress() {
    const press={id:null,key:null,unsure:false,ready:null,toasts:Promise.resolve()};
    try{press.id=crypto.randomBytes(8).toString('hex');}catch(_){}
    press.ready=new Promise(resolve=>later(resolve,0)).then(()=>{
      if(stopping||!press.id)return;
      try{press.key=deviceKey(identity.load());}catch(error){if(error&&error.code==='DEVICE_IDENTITY_CLOSE')press.unsure=true;}
    }).catch(()=>{});
    return press;
  }
  function closePress(press) {
    // Queued after every ping already bound to this press, so each has its MAC first.
    void press.ready.then(()=>{if(press.key)press.key.fill(0);press.key=null;if(press.unsure)closeFailed=true;});
  }
  // Fire-and-forget, enums only: resolves to the R2D2 surface showing voice, 'other' on any doubt.
  function phase(press,name,code=null,deadlineMs=null,cut=false) {
    if(stopping||!press.id)return Promise.resolve('other');
    let value;
    try{if(!boot||seq>=2**31){boot=crypto.randomBytes(8).toString('hex');seq=0;}
      value={schema:1,boot,seq:++seq,press:press.id,phase:name,code,deadlineMs,cut};}catch(_){return Promise.resolve('other');}
    return press.ready.then(()=>{
      if(!press.key)return 'other';
      const names=Object.keys(value).sort();
      value.mac=crypto.createHmac('sha256',press.key).update(PHASE_DOMAIN,'ascii').update(JSON.stringify(value,names),'ascii').digest('hex');
      const body=Buffer.from(JSON.stringify(value,[...names,'mac'].sort()),'ascii');
      if(body.length>256)return 'other';
      return exchange('/voice-phase','POST',{'Content-Type':'application/json','Content-Length':body.length},body,256,300)
        .then(raw=>raw&&typeof raw==='object'&&Object.keys(raw).sort().join()==='ok,surface'&&raw.ok===true&&
          SURFACES.includes(raw.surface)?raw.surface:'other');
    }).catch(()=>'other');
  }
  // A toast only when no R2D2 surface renders this press, in the press's own order.
  function notice(press,surface,message) {
    press.toasts=press.toasts.then(()=>surface).then(where=>{if(where==='other')toast(message);}).catch(()=>{});
    return press.toasts;
  }
  function fail(press,code) { return notice(press,phase(press,'fault',code),FAULTS[code]); }
  // Feedback for presses that grant nothing; at most one every 3 s. Each is its own press.
  function hint(code) { if(now()-lastHint<3000)return; lastHint=now(); const press=openPress(); fail(press,code); closePress(press); }
  // Spark's closed copy for a finished press: text, null (shown or none), or undefined when
  // Spark has no answer, which keeps the fixed mapping. Only printable ASCII reaches a toast.
  function sparkToast(press) {
    if(!press.id)return Promise.resolve(undefined);
    return exchange('/voice-receipt/'+press.id,'GET',{},undefined,256,1700).then(raw=>{
      if(!raw||typeof raw!=='object'||Object.keys(raw).sort().join()!=='done,press,schema,shown,toast'||raw.schema!==1||
        raw.press!==press.id||raw.done!==true||typeof raw.shown!=='boolean'||
        !(raw.toast===null||typeof raw.toast==='string'&&/^[\x20-\x7e]{1,60}$/.test(raw.toast)))return undefined;
      return raw.shown?null:raw.toast;
    }).catch(()=>undefined);
  }
  async function powerLoop() {
    while(!stopping) {
      power=powerState(await luna(POWER,{}));
      // After release the mic is STOP-confirmed and Spark may already be acting;
      // one unreadable power reply must not drop that result. Standby still closes.
      if(power!=='active'&&!(power==='unknown'&&releasedSession))closeSession();
      if(stopping)break;
      await reportState();
      await sleep(5000);
    }
  }
  function current(fresh=false,currentBudget=()=>true) {
    if(currentBudget()!==true)throw fault('CAPTURE_DEADLINE');
    if(stopping||closeFailed||power!=='active'||fd===null||!selected)throw fault('CAPTURE_CANCELLED');
    if(!sameNode(selected.metadata.node,io.stat(fd)))throw fault('DEVICE_CHANGED');
    if(fresh||now()-lastCheck>=100){if(!fences())throw fault('ROUTE_CHANGED');remote.recheck(selected,currentBudget);lastCheck=now();}
    if(currentBudget()!==true)throw fault('CAPTURE_DEADLINE');
  }
  function decode(raw,count) {
    let decoder=null, result=null,pcm=null;
    try {
      decoder=fs.openSync(root+'/decode-reports',fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW);
      result=cp.spawnSync('/proc/self/fd/12',[],{input:raw,stdio:['pipe','pipe','ignore',...Array(9).fill('ignore'),decoder],
        env:{LANG:'C',LC_ALL:'C'},timeout:3000,killSignal:'SIGKILL',maxBuffer:192000});
      if(result.error||result.status!==0||result.signal!==null||!Buffer.isBuffer(result.stdout)||
          result.stdout.length!==count*480||result.stdout.length<960||result.stdout.length>192000)throw fault('DECODE_FAILED');
      pcm=result.stdout;
      const held=decoder;decoder=null;
      try{fs.closeSync(held);}catch(_){closeFailed=true;throw fault('DECODER_CLOSE');}
      result=null;return pcm;
    } finally {
      if(result&&Buffer.isBuffer(result.stdout))result.stdout.fill(0);
      if(decoder!==null)try{fs.closeSync(decoder);}catch(_){closeFailed=true;}
    }
  }
  function closeSession() {
    if(activeSession&&!sessionClose){
      try{sessionClose=Promise.resolve(activeSession.close()).catch(()=>{closeFailed=true;});}
      catch(_){closeFailed=true;sessionClose=Promise.resolve();}
    }
    return sessionClose||Promise.resolve();
  }
  async function sendPcm(pcm) {
    let cfg=null, session=null, receipt=null;
    try {
      let keys;try{keys=identity.load();}catch(error){if(error&&error.code==='DEVICE_IDENTITY_CLOSE')closeFailed=true;throw error;}
      cfg={address:config.sparkIp,port:config.pcmPort,serverName:'r2d2-public-transport.test',
        serverCertificateSha256:keys.sparkCertificateSha256,ca:Buffer.from(keys.sparkCertificate,'ascii'),
        certificate:Buffer.from(keys.certificate,'ascii'),privateKey:Buffer.from(keys.privateKey,'ascii'),deviceKey:deviceKey(keys)};
      if(stopping||power!=='active')throw fault('CAPTURE_CANCELLED');
      try {
        session=await tlsClient.connect(cfg,{onClose:value=>{receipt=value;}});activeSession=session;sessionClose=null;
        for(let pos=0;pos<pcm.length;pos+=3200){
          if(stopping||power!=='active')throw fault('CAPTURE_CANCELLED');
          await session.sendPcm(pcm.subarray(pos,Math.min(pos+3200,pcm.length)));
        }
        if(stopping||power!=='active')throw fault('CAPTURE_CANCELLED');
        await session.release();releasedSession=true;
        return await session.closed;
      } catch(error) {
        // Spark may refuse (warming up, busy, paused) before release, which
        // ends the upload early. Its refusal stays in the close receipt and
        // never acknowledges an action, so report it instead of the stream error.
        if(error&&error.code==='CAPTURE_CANCELLED')throw error;
        if(session){await closeSession();receipt=await session.closed;}
        if(receipt&&receipt.outcome&&receipt.outcome.state==='refused'&&receipt.actionAcknowledged!==true)return receipt;
        throw error;
      }
    } finally {
      pcm.fill(0);
      try {if(session)await closeSession();}
      finally {activeSession=null;sessionClose=null;releasedSession=false;
        if(cfg)for(const key of ['ca','certificate','privateKey','deviceKey'])if(Buffer.isBuffer(cfg[key]))cfg[key].fill(0);}
    }
  }
  // While a command uploads or runs, a new press grants nothing. Read only the
  // owner key's edges (discarding) so the user hears "busy" instead of nothing.
  async function busyWatch(job) {
    let done=false;const settled=job.then(()=>{done=true;},()=>{done=true;});
    while(!done&&!stopping) {
      // Wake at once when the command settles, so the result is never delayed.
      await Promise.race([sleep(50),settled]);
      if(done||stopping)break;
      try{if(ownerKey.poll(true).ignored===true)hint('BUSY');}catch(_){break;}
    }
    return job;
  }
  async function capture(buffer,press) {
    const raw=Buffer.alloc(MAX_RAW_BYTES),address=selected.address,began=now();
    const currentBudget=()=>now()-began<CAPTURE_MS;
    let used=0,released=false,start=false,stop=false,captureError=null,tap=false,sending=false;
    captures++;lastState='capturing';stopConfirmed=false;
    try {
     try {
      current(true,currentBudget);
      const initiatingKey=ownerKey.poll(false,currentBudget);
      tap=initiatingKey.ready===true&&(initiatingKey.released===true||!initiatingKey.down);
      if(!initiatingKey.ready||!initiatingKey.down||initiatingKey.released)throw fault('OWNER_KEY_RELEASED');
      if(!currentBudget())throw fault('CAPTURE_DEADLINE');
      start=await setReport(address,true);
      if(!start)throw fault('START_UNCONFIRMED');
      notice(press,phase(press,'listening',null,Math.max(0,Math.min(6000,Math.floor(CAPTURE_MS-(now()-began))))),'Listening...');
      while(now()-began<CAPTURE_MS&&!released) {
        current(false,currentBudget);
        const key=ownerKey.poll(false,currentBudget);
        if(!key.ready)throw fault('OWNER_KEY_UNCONFIRMED');
        if(key.released||!key.down){released=true;break;}
        for(let k=0;k<64;k++) {
          if(!currentBudget())throw fault('CAPTURE_DEADLINE');
          if(!sameNode(selected.metadata.node,io.stat(fd)))throw fault('DEVICE_CHANGED');
          let n;
          try{n=io.read(fd,buffer);}catch(error){if(error.code==='EAGAIN'||error.code==='EWOULDBLOCK')break;throw fault('DEVICE_CHANGED');}
          if(n<=0)throw fault('DEVICE_CHANGED');
          try {
            const event=recording.event(buffer,n);
            if(event.kind==='FE'){
              if(n<124||used+124>raw.length)throw fault('AUDIO_BOUND');buffer.copy(raw,used,0,124);used+=124;
            }
            if((event.kind==='FE'||event.kind==='FD')&&event.event==='stop'){released=true;break;}
          } finally {buffer.fill(0);}
        }
        if(!released)await sleep(5);
      }
    } catch(error){
      // Reaching the hold limit after a confirmed START ends capture like a
      // release. current(true) below still re-verifies route and remote.
      if(!(start&&!currentBudget()&&error&&DEADLINE_CODES.has(error.code)))captureError=error;
    }
     finally {
      buffer.fill(0);
      try{stop=await setReport(address,false);}finally{stopConfirmed=stop;}
     }
      if(captureError||!start||!stop||stopping||power!=='active'){
        // Fixed codes only: fences and internal faults stay generic.
        if(!stopping&&power==='active')fail(press,!stop||closeFailed?'STOP_UNCONFIRMED':tap?'OWNER_KEY_RELEASED':
          captureError&&captureError.code==='START_UNCONFIRMED'?'START_UNCONFIRMED':'CAPTURE_LOST');
        throw fault('CAPTURE_CANCELLED');
      }
      let receipt=null;
      try {
        current(true);
        if(used<248){fail(press,'NO_SPEECH');return;}
        // Only audio that will really be sent says "got it"; cut marks the hold limit.
        lastState='processing';notice(press,phase(press,'released',null,null,!released),'Processing voice command...');
        const pcm=decode(raw.subarray(0,used),used/124);
        raw.fill(0);sending=true;
        receipt=await busyWatch(sendPcm(pcm));
      } catch(error) {
        if(!stopping&&power==='active')fail(press,closeFailed?'STOP_UNCONFIRMED':sending?'TRANSPORT_FAILED':'CAPTURE_LOST');
        throw error;
      }
      try {
        if(stopping||power==='standby')return;
        const outcome=receipt&&receipt.outcome,code=outcome&&outcome.code;
        // Released with no outcome at all is this TV's own uncertainty.
        if(receipt&&receipt.controlFlushed===true&&receipt.cancelled!==true&&!outcome)return await busyWatch(fail(press,'RESULT_UNCONFIRMED'));
        // Spark's copy decides when it answers for this press; an older or silent Spark
        // keeps the fixed mapping. Earlier phase toasts for this press go first.
        const text=await busyWatch(sparkToast(press).then(value=>press.toasts.then(()=>value)));
        if(stopping||power==='standby')return;
        if(text!==undefined){if(text!==null)toast(text);return;}
        // The shared host Controller owns all actions, including viewer launch.
        // A global command receipt never grants a second action on the TV here.
        if(receipt&&receipt.actionAcknowledged===true)toast('Voice command sent');
        else if(code==='VOICE_WARMING_UP')toast('Voice model warming up - try again in a moment');
        else if(code==='AUDIO_EMPTY_OR_SHORT')toast('Hold the microphone button while speaking');
        else if(code==='AGENT_PAUSED')toast('Voice control is paused. Restart Spark to resume');
        else if(code==='RATE_LIMITED')toast('Too many requests, wait a moment');
        else if(code==='VOICE_BUSY'||code==='CONTROLLER_BUSY')toast('Voice is busy, try again in a moment');
        else if(code==='RECOGNITION_ONLY_NO_ACTION')toast('Voice heard (no browser navigation matched)');
        // After release the command may have run; missing confirmation is uncertainty.
        else if(receipt&&receipt.controlFlushed===true&&receipt.cancelled!==true&&(!outcome||outcome.state==='unknown'))
          toast('Sent, but not confirmed. Check the TV');
        else toast('Voice command unavailable');
      } finally {processed++;}
    } finally {raw.fill(0);}
  }
  function closeDevice() {
    if(fd!==null){const held=fd;fd=null;try{io.close(held);}catch(_){closeFailed=true;}}
    try{if(!ownerKey.close())closeFailed=true;}catch(_){closeFailed=true;}
    selected=null;discarding=false;lastCheck=-Infinity;
  }
  async function mainLoop() {
    io=shape.makeIO();const buffer=Buffer.alloc(shape.READ_BYTES);
    try {
      while(!stopping) {
        if(power!=='active'||closeFailed||!stopConfirmed){closeDevice();lastState='waiting';await sleep(250);continue;}
        try {
          if(fd===null) {
            if(!fences())throw fault('ROUTE_CHANGED');
            selected=remote.derive();fd=io.open(selected.metadata.path);current(true);ownerKey.open(selected);
            if(firstOpen){firstOpen=false;for(let k=0;k<64;k++){try{const n=io.read(fd,buffer);buffer.fill(0);if(n<=0)break;}catch(error){buffer.fill(0);if(error.code!=='EAGAIN'&&error.code!=='EWOULDBLOCK')throw error;break;}}}
          }
          current();
          const key=ownerKey.poll(discarding);
          lastState=discarding?'processing':key.ready?'ready':'waiting';
          // FD/FE are stream evidence, never physical-button authority. Drain
          // old reports before a separately calibrated, current owner key press.
          let empty=false;
          for(let k=0;k<64;k++) {
            let n;try{n=io.read(fd,buffer);}catch(error){if(error.code==='EAGAIN'||error.code==='EWOULDBLOCK'){empty=true;break;}throw error;}
            if(n<=0)throw fault('DEVICE_CHANGED');
            buffer.fill(0);
          }
          const pressed=key.ready&&key.pressed&&key.down&&!discarding&&empty;
          // A press queued while inference was busy is discarded, not replayed.
          if(empty&&key.ready)discarding=false;
          // A tap released within one poll never reaches capture(); say so.
          if(key.ready&&key.tapped===true&&!discarding)hint('OWNER_KEY_RELEASED');
          if(pressed){
            const press=openPress();phase(press,'pressed');
            try{await capture(buffer,press);}finally{closePress(press);}
            discarding=true;
          }else await sleep(5);
        } catch(_) {firstOpen=false;lastState='unavailable';closeDevice();await sleep(100);}
      }
    } finally {buffer.fill(0);closeDevice();}
  }
  async function stop() {
    stopping=true;for(const wake of [...waits])wake();
    closeSession();
    for(const cancel of [...requests])cancel();
    if(finished)await finished;
    return status();
  }
  function start() {
    if(started)throw fault('ALREADY_STARTED');started=true;
    // LS2 owns this exact service name. A duplicate refuses registration; it
    // never searches for, signals or deletes an alleged prior instance.
    finished=(async()=>{
      let ok=true;
      try {
        bus=new palmbus.Handle(OWNER);
        const guarded=job=>job.catch(()=>{ok=false;stopping=true;for(const wake of [...waits])wake();});
        await Promise.all([guarded(powerLoop()),guarded(mainLoop())]);
      } catch(_) {ok=false;stopping=true;for(const wake of [...waits])wake();}
      finally {
        stopping=true;
        for(const cancel of [...busCalls])cancel();
        for(const cancel of [...requests])cancel();
        await closeSession();
        await Promise.all([...childJobs]);
        if(bus)try{bus.removeAllListeners();bus.unregister();}catch(_){ok=false;}
        cleanupComplete=ok&&!closeFailed&&fd===null&&children.size===0&&stopConfirmed;
        lastState=cleanupComplete?'closed':'cleanup-pending';
      }
      return status();
    })();
    return finished;
  }
  return Object.freeze({start,stop,status});
}
async function run() {
  const fs=require('fs'),path=__dirname+'/resident_config.json';
  let fd=null,raw=null,cfg;
  try {
    fd=fs.openSync(path,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);
    const stat=fs.fstatSync(fd);
    if(!stat.isFile()||stat.uid!==0||(stat.mode&0o077)||stat.size<1||stat.size>8192)throw fault('CONFIG');
    raw=Buffer.alloc(8193);const size=fs.readSync(fd,raw,0,raw.length,0);
    if(size!==stat.size||size>8192)throw fault('CONFIG');
    cfg=JSON.parse(raw.subarray(0,size).toString('utf8'));
  } finally {if(raw)raw.fill(0);if(fd!==null)fs.closeSync(fd);}
  const resident=createResident(cfg);cfg=null;
  const stop=()=>{void resident.stop();},nop=()=>{};
  process.on('SIGTERM',stop);process.on('SIGINT',stop);process.on('SIGHUP',nop);
  try {const result=await resident.start();if(!result.cleanupComplete)process.exitCode=1;}
  finally {process.removeListener('SIGTERM',stop);process.removeListener('SIGINT',stop);process.removeListener('SIGHUP',nop);}
}
module.exports={createResident,configuration,powerState,sameNode,run};
if(require.main===module)run().catch(()=>{process.exitCode=1;});
