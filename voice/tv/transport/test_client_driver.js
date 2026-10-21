'use strict';
// Test-only: fixture files contain throwaway credentials and synthetic PCM.
const fs=require('fs'),{connect}=require('./tls_pcm_client'),{createPcmAdapter}=require('./pcm_provider_adapter');
const x=JSON.parse(fs.readFileSync(process.argv[2],'utf8'));let readyCalls=0,closeCalls=0,errorCalls=0;
const cfg={address:'127.0.0.1',port:x.port,serverName:x.serverName||'voice.test',ca:fs.readFileSync(x.ca),certificate:fs.readFileSync(x.certificate),privateKey:fs.readFileSync(x.privateKey),serverCertificateSha256:x.pin,deviceKey:Buffer.from(x.deviceKey,'hex')};
const receipt={cleanExit:true,nativeStartAccepted:true,nativeStopAccepted:true,nativeStopFailed:false,networkSealInstalled:true,supervision:{allReaped:true,supervisionComplete:true}};
(async()=>{let session;
 try{
  session=await connect(cfg,{onReady:()=>{readyCalls++;},onClose:()=>{closeCalls++;},onError:()=>{errorCalls++;}});
  if(x.mode==='wait'){const ended=await session.closed;return {connected:true,reason:ended.reason,outcome:ended.outcome};}
  if(x.mode==='adapter'){
   let stopCalls=0,reapCalls=0;const a=createPcmAdapter(session);a.start((hooks,done)=>{const p={stop:()=>{stopCalls++;},reap:async()=>{reapCalls++;return receipt;}};hooks.onReady(p);return p;});
   let offset=0;for(const size of [1,17,3183,1,3198]){const b=Buffer.alloc(size);for(let i=0;i<size;i++)b[i]=(offset+i)%251;offset+=size;if(!a.onPcm(b))throw Error('ADAPTER_REFUSED');b.fill(0);}
   const ended=await a.release();return {connected:true,reason:ended.reason,providerReaped:ended.providerReaped,stopCalls,reapCalls};
  }
  const count=x.mode==='maximum'?100:2;
  for(let n=0;n<count;n++){const b=Buffer.alloc(3200);for(let i=0;i<b.length;i++)b[i]=(n*3200+i)%251;const p=session.sendPcm(b);b.fill(0);await p;}
  if(x.mode==='cancel')await session.cancel();else await session.release();
  const ended=await session.closed;return {connected:true,reason:ended.reason,released:ended.released,outcome:ended.outcome,actionAcknowledged:ended.actionAcknowledged};
 }catch(e){if(session)await session.close();return {connected:!!session,error:e.code||'TEST_FAILURE'};}
})().then(result=>{process.stdout.write(JSON.stringify({...result,readyCalls,closeCalls,errorCalls})+'\n');},()=>{process.stdout.write('{"error":"TEST_FAILURE"}\n');process.exitCode=1;});
