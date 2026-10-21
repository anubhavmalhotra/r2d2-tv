/* Authored offline VM fixture; no browser or network. */
'use strict';
const assert=require('assert'),fs=require('fs'),vm=require('vm'),path=require('path');
const source=fs.readFileSync(path.join(__dirname,'../spark/runtime/extension/proof.js.in'),'utf8');
async function exercise(code,mode='good') {
  const calls=[],seeds=['snu.lge.com','snu-dev.lge.com','redirect.lgsmartplatform.com','qt-us.ad.lgsmartad.com','156.147.69.32'];
  const box={chrome:{extension:{inIncognitoContext:mode!=='regular',isAllowedIncognitoAccess:async()=>mode!=='denied'},declarativeNetRequest:{
    getEnabledRulesets:async()=>mode==='missing-set'?['easylist','ublock_badware','easyprivacy']:mode==='extra-set'?['owner_lg','easylist','ublock_badware','easyprivacy','unknown']:['owner_lg','easylist','ublock_badware','easyprivacy'],
    testMatchOutcome:async value=>{
      assert.strictEqual(typeof value.type,'string');
      const url=new URL(value.url);assert.strictEqual(url.protocol,value.type==='websocket'?'wss:':'https:');calls.push(value);
      if(mode==='api-failure')throw Error('fixed failure');
      const match=seeds.some(h=>url.hostname===h||url.hostname.endsWith('.'+h));
      return {matchedRules:(match||mode==='false-positive')?[{rulesetId:'owner_lg',ruleId:1}]:[]};
    }}}};
  vm.runInNewContext(code,box,{timeout:1000});
  for(let i=0;i<50&&box.r2d2ProtectionProof.state==='pending';i++)await new Promise(resolve=>setImmediate(resolve));
  return {proof:box.r2d2ProtectionProof,calls};
}
(async()=>{
  let r=await exercise(source);assert.strictEqual(r.proof.state,'complete');assert.strictEqual(r.proof.ownerPolicyExact,true);assert.strictEqual(r.proof.incognito,true);assert.strictEqual(r.proof.rulesetsExact,true);assert.strictEqual(r.proof.ruleSetCount,4);assert.strictEqual(r.calls.length,19);
  r=await exercise(source,'false-positive');assert.strictEqual(r.proof.ownerPolicyExact,false);
  r=await exercise(source,'api-failure');assert.strictEqual(r.proof.state,'unavailable');
  r=await exercise(source,'regular');assert.strictEqual(r.proof.incognito,false);
  r=await exercise(source,'denied');assert.strictEqual(r.proof.incognitoAllowed,false);
  r=await exercise(source,'missing-set');assert.strictEqual(r.proof.rulesetsExact,false);assert.strictEqual(r.proof.ruleSetCount,3);
  r=await exercise(source,'extra-set');assert.strictEqual(r.proof.rulesetsExact,false);assert.strictEqual(r.proof.ruleSetCount,5);
  process.stdout.write('7 fixed extension proof cases passed\n');
})().catch(()=>{process.stderr.write('PROOF_FIXTURE_FAILED\n');process.exitCode=1});
