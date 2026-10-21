'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs'), vm = require('node:vm');
const corePath = process.env.R2D2_HOME_CORE_SOURCE || require.resolve('../tv/home/core.js.in');
const {Service, normalize, voiceValue, weatherValue} = require(corePath);
const LIST = 'luna://com.webos.applicationManager/listApps';
const LAUNCH = 'luna://com.webos.applicationManager/launch';
const REMOVE = 'luna://com.webos.appInstallService/remove';
const STORE = 'com.webos.app.discovery';
const catalog = {appId:'local.owner.applibrary',library:['netflix','com.apple.appletv'],shortcuts:[{id:STORE},{id:'com.webos.app.tinybrowser'}]};
const app = (id, extra={}) => ({id,title:id,type:'web',visible:true,removable:true,systemApp:false,...extra});
const reply = apps => ({returnValue:true,apps});
function fixture(handler, ServiceClass=Service) {
  const calls=[];
  function Bridge() {}
  Bridge.prototype.cancel=function(){};
  Bridge.prototype.call=function(uri,raw){
    const payload=JSON.parse(raw);calls.push({uri,payload});
    Promise.resolve().then(()=>handler(uri,payload,calls)).then(value=>{
      if(value!==undefined)this.onservicecallback(JSON.stringify(value));
    });
  };
  return {service:new ServiceClass(Bridge,100),calls};
}
async function flush() { for(let i=0;i<30;i++)await Promise.resolve(); }
function timedFixture(handler) {
  let now=0,serial=0;const timers=new Map(),module={exports:{}};
  vm.runInNewContext(fs.readFileSync(corePath,'utf8'),{module,Date:{now:()=>now},setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,at:now+delay,delay});return id;},clearTimeout(id){timers.delete(id);}});
  const f=fixture(handler,module.exports.Service);
  return {...f,timers,async advance(end){await flush();for(;;){const row=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];if(!row||row[1].at>end)break;timers.delete(row[0]);now=row[1].at;row[1].fn();await flush();}now=end;}};
}
// Voice feed fixture: core in a vm with a virtual clock and a scripted fetch.
const VOICE_KEY='b'.repeat(64),VOICE_BASE='http://{{SPARK_IP}}:4181/'+VOICE_KEY+'/voice/';
const IDLE={v:1,seq:1,phase:'idle',ageMs:0,tone:'idle',word:'',headline:'',detail:'',badge:null,meter:null,meterMs:null,dwellMs:null,mood:'idle',chip:'ready'};
const view=patch=>({...IDLE,...patch});
const ascii=value=>JSON.stringify(value).replace(/[\u007f-\uffff]/g,c=>'\\u'+c.charCodeAt(0).toString(16).padStart(4,'0'));
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function voiceFixture({keyed=true,withFetch=true}={}) {
  let now=0,serial=0;const timers=new Map(),module={exports:{}},calls=[],updates=[];
  function fetch(url,init){
    const call={url,init};calls.push(call);
    return new Promise((resolve,reject)=>{
      init.signal.addEventListener('abort',()=>reject(new Error('aborted')));
      call.fail=()=>reject(new TypeError('network'));
      call.respond=(body,extra={})=>{const bytes=typeof body==='string'?new Uint8Array(Buffer.from(body,'latin1')):body;let sent=false;
        resolve({status:200,url,body:{getReader(){return {read(){if(sent)return Promise.resolve({done:true});sent=true;return Promise.resolve({done:false,value:bytes});},cancel(){return Promise.resolve();}};}},...extra});};
    });
  }
  const sandbox={module,Date:{now:()=>now},AbortController,setTimeout(fn,delay){const id=++serial;timers.set(id,{fn,at:now+delay,delay});return id;},clearTimeout(id){timers.delete(id);}};
  if(withFetch)sandbox.fetch=fetch;
  vm.runInNewContext(fs.readFileSync(corePath,'utf8'),sandbox);
  const service=new module.exports.Service(null,100,keyed?{origin:'http://{{SPARK_IP}}:4181',key:VOICE_KEY}:null);
  const feed=new module.exports.VoiceFeed(service,(value,failures,replay)=>updates.push({seq:value&&value.seq,failures,replay}));
  return {service,feed,calls,updates,timers,now:()=>now,delays:()=>[...timers.values()].map(timer=>timer.delay).sort((a,b)=>a-b),
    async advance(ms){const end=now+ms;await settle();for(;;){const row=[...timers].sort((a,b)=>a[1].at-b[1].at)[0];if(!row||row[1].at>end)break;timers.delete(row[0]);now=row[1].at;row[1].fn();await settle();}now=end;}};
}
let groups=0;
async function run(){
  const normalized=normalize(reply([app('netflix'),app('com.apple.appletv',{systemApp:undefined}),app(STORE),app('local.owner.applibrary')]),catalog);
  assert.equal(normalized.find(x=>x.id==='netflix').removable,true);
  assert.equal(normalized.find(x=>x.id==='com.apple.appletv').removable,false);
  assert.equal(normalized.find(x=>x.id===STORE).removable,false);
  assert.equal(normalized.some(x=>x.id===catalog.appId),false);groups++;

  {
    // Weather carries no location: zip is "" (an older Spark sent five digits); Home accepts either and never keeps it.
    const observedAt=new Date(Math.floor(Date.now()/60000)*60000-600000).toISOString().replace('.000Z','Z');
    const live={state:'live',zip:'',temperatureF:64,condition:'Clear',icon:'clear',observedAt};
    const off={state:'unavailable',zip:'',temperatureF:null,condition:null,icon:null,observedAt:null};
    assert.deepEqual(weatherValue(live),live);
    assert.deepEqual(weatherValue({...live,zip:'00000'}),live);
    assert.deepEqual(weatherValue({...live,state:'stale',zip:'00000'}),{...live,state:'stale'});
    assert.deepEqual(weatherValue(off),off);
    assert.deepEqual(weatherValue({...off,zip:'00000'}),off);
    for(const zip of [null,0,undefined,'0000','000000','abcde',' 0000','00000 ','0000\n',['00000']])
      assert.throws(()=>weatherValue({...live,zip}),String(zip));
    const {zip,...withoutZip}=live;assert.equal(zip,'');assert.throws(()=>weatherValue(withoutZip));
    assert.throws(()=>weatherValue({...live,city:'Sentinel'}));
    assert.throws(()=>weatherValue({...off,temperatureF:64}));
    groups++;
  }

  {
    let inventory=[app('netflix'),app(STORE,{removable:false,systemApp:true})];
    let removed=false,checks=0;
    const f=fixture(uri=>{
      if(uri===REMOVE){removed=true;return {returnValue:true};}
      if(uri===LIST){if(removed&&++checks===2)inventory=inventory.filter(x=>x.id!=='netflix');return reply(inventory);}
      throw Error('unexpected operation');
    });
    await f.service.refresh(catalog);
    const result=await f.service.removeApp('netflix');
    assert.deepEqual(result,{removed:true,id:'netflix'});
    assert.equal(checks,2,'ack alone must not confirm removal');
    assert.equal(f.calls.filter(x=>x.uri===REMOVE).length,1);
    assert.deepEqual(f.calls.find(x=>x.uri===REMOVE).payload,{id:'netflix'});
    assert.equal(f.service.apps.some(x=>x.id==='netflix'),false);groups++;
  }
  {
    let reads=0;
    const f=fixture(uri=>{assert.equal(uri,LIST);return reply([app('netflix',{systemApp:++reads>1})]);});
    await f.service.refresh(catalog);
    await assert.rejects(f.service.removeApp('netflix'),/no longer eligible/);
    assert.equal(f.calls.some(x=>x.uri===REMOVE),false);groups++;
  }
  {
    let reads=0;
    const f=fixture(uri=>{assert.equal(uri,LIST);return reply(++reads===1?[app('netflix')]:[app('netflix'),app('netflix',{systemApp:true})]);});
    await f.service.refresh(catalog);
    await assert.rejects(f.service.removeApp('netflix'),/no longer eligible/);
    assert.equal(f.calls.some(x=>x.uri===REMOVE),false);groups++;
  }
  {
    const f=fixture(uri=>{assert.equal(uri,LIST);return reply([app(STORE),app('netflix',{removable:false}),app('com.apple.appletv',{systemApp:undefined})]);});
    await f.service.refresh(catalog);
    for(const id of [STORE,'netflix','com.apple.appletv','local.owner.applibrary','com.webos.app.tinybrowser','../../bad'])await assert.rejects(f.service.removeApp(id),/cannot be deleted/);
    assert.equal(f.calls.length,1);groups++;
  }
  {
    const f=fixture(uri=>uri===REMOVE?{returnValue:false,errorCode:-1}:reply([app('netflix')]));
    await f.service.refresh(catalog);
    await assert.rejects(f.service.removeApp('netflix'),/did not allow/);
    assert.equal(f.calls.filter(x=>x.uri===REMOVE).length,1);
    assert.deepEqual(f.service.apps,[]);groups++;
  }
  {
    let accepted=false;
    const f=fixture(uri=>{if(uri===REMOVE){accepted=true;return {returnValue:true};}return reply(accepted?[null]:[app('netflix')]);});
    await f.service.refresh(catalog);
    await assert.rejects(f.service.removeApp('netflix'),/could not be confirmed/);
    assert.equal(f.calls.filter(x=>x.uri===REMOVE).length,1);groups++;
  }
  {
    let accepted=false;
    const f=fixture(uri=>{if(uri===REMOVE){accepted=true;return {returnValue:true};}if(accepted)setTimeout(()=>f.service.cancelAll(),10);return reply([app('netflix')]);});
    await f.service.refresh(catalog);
    const removal=f.service.removeApp('netflix');
    await assert.rejects(f.service.removeApp('netflix'),/cannot be deleted/);
    await assert.rejects(removal,/cancelled/);
    assert.equal(f.calls.filter(x=>x.uri===REMOVE).length,1);groups++;
  }
  {
    const f=fixture(uri=>uri===LIST?reply([app(STORE,{removable:false,systemApp:true})]):{returnValue:true});
    await f.service.refresh(catalog);
    assert.equal(f.calls.length,1,'store must never open in background');
    await f.service.openStore();
    assert.deepEqual(f.calls[1],{uri:LAUNCH,payload:{id:STORE}});
    await assert.rejects(f.service.openStore('another-app'),/not available/);
    f.service.apps=[];await assert.rejects(f.service.openStore(),/not available/);groups++;
  }
  {
    const f=timedFixture(uri=>uri===REMOVE?{returnValue:true}:reply([app('netflix')]));
    await f.service.refresh(catalog);
    let settled=false;const removal=f.service.removeApp('netflix').catch(error=>{settled=true;return error;});
    await flush();assert.equal([...f.timers.values()].filter(timer=>timer.delay===300).length,1);
    assert.equal(f.service.pending.length,1,'the poll wait must be registered for cancellation');
    f.service.cancelAll();await flush();
    assert.equal(settled,true,'cancellation must settle without waiting300ms');
    assert.match((await removal).message,/cancelled/);
    assert.equal(f.service.removing,false);assert.equal(f.service.pending.length,0);assert.equal(f.timers.size,0);
    const requests=f.calls.length;await f.advance(1000);assert.equal(f.calls.length,requests,'cancelled polls cannot read or replay removal');groups++;
  }
  {
    let current=[app('netflix')];
    const f=timedFixture(uri=>uri===REMOVE?{returnValue:true}:reply(current));
    await f.service.refresh(catalog);
    const removal=f.service.removeApp('netflix').catch(error=>error);await flush();
    // A newer complete inventory must survive the stale removal's delayed
    // rejection, even if a caller refreshes without first calling cancelAll.
    current=[app('netflix'),app('com.apple.appletv')];await f.service.refresh(catalog);
    await f.advance(300);assert.match((await removal).message,/cancelled/);
    assert.deepEqual(Array.from(f.service.apps,entry=>entry.id),['netflix','com.apple.appletv']);
    await f.service.launch('com.apple.appletv');
    assert.equal(f.calls.filter(call=>call.uri===REMOVE).length,1);assert.equal(f.service.pending.length,0);groups++;
  }
  {
    const callbacks=[];
    function Bridge(){};Bridge.prototype.cancel=function(){};Bridge.prototype.call=function(){callbacks.push(this.onservicecallback);};
    const service=new Service(Bridge,100),refresh=service.refresh(catalog).catch(error=>error);
    callbacks[0](JSON.stringify(reply([app('netflix')])));service.cancelAll();
    const outcome=await refresh;assert(outcome instanceof Error,'cancellation must reject a resolved but unconsumed inventory response');
    assert.match(outcome.message,/newer app refresh/);
    assert.deepEqual(service.apps,[],'a resolved-before-cancel response cannot restore stale launch authority');groups++;
  }
  {
    // voiceValue accepts only Spark's exact view: closed enums, bounded counts, closed charset.
    const listening=view({seq:9,phase:'listening',tone:'listen',word:'LISTENING',headline:'Listening...',detail:'Say “yes” or “no”, then let go.',meter:'hold',meterMs:5500,mood:'listening'});
    const accepted=[IDLE,listening,view({seq:0}),view({seq:9999999999,ageMs:600000,chip:'paused'}),
      view({phase:'released',tone:'work',word:'THINKING',headline:'Got it.',meter:'sweep',mood:'thinking'}),
      view({phase:'doing',tone:'work',word:'ON IT',headline:'Opening Netflix...',meter:'sweep',meterMs:1200,mood:'thinking'}),
      view({phase:'confirm',tone:'ask',word:'NEEDS YOUR OK',headline:'Turn the volume up 5?',detail:'Hold the mic button and say “yes” or “no”.',badge:'waiting',meter:'countdown',meterMs:14000,dwellMs:15000,mood:'asking'}),
      view({phase:'result',tone:'failed',word:'DIDN’T WORK',headline:'I’m sorry, I cannot do that.',badge:'failed',dwellMs:20000,mood:'dim',chip:'unavailable'}),
      view({phase:'result',tone:'done',word:"DIDN'T WORK",headline:'x'.repeat(40),detail:'~'.repeat(72),badge:'changed',dwellMs:0,mood:'verified'})];
    for(const value of accepted){const copy=voiceValue(JSON.parse(ascii(value)));assert.deepEqual(copy,value);assert.notEqual(copy,value);}
    const reordered=Object.fromEntries(Object.entries(listening).reverse());assert.deepEqual(voiceValue(reordered),listening);
    const {chip,...missing}=IDLE;
    const rejected=[null,undefined,[],'idle',1,missing,{...IDLE,echo:'lo-fi music'},{...IDLE,transcript:''},{...IDLE,code:'UNSUPPORTED_COMMAND'},
      view({v:2}),view({v:'1'}),view({seq:-1}),view({seq:1.5}),view({seq:'1'}),view({seq:10000000000}),view({seq:null}),view({ageMs:-1}),view({ageMs:600001}),view({ageMs:2.5}),
      view({phase:'thinking'}),view({phase:'IDLE'}),view({tone:'warn'}),view({mood:'happy'}),view({chip:'offline'}),view({chip:'idle'}),
      {...listening,word:'listening'},{...listening,word:'A'.repeat(21)},{...listening,word:'DONE!'},{...listening,word:null},
      {...listening,headline:'x'.repeat(41)},{...listening,headline:'Tab\there'},{...listening,headline:'Café'},{...listening,headline:'\u202eevil'},{...listening,headline:'line\u2028break'},{...listening,headline:'🎤'},{...listening,headline:1},
      {...listening,detail:'x'.repeat(73)},{...listening,detail:'ü'},{...listening,detail:null},
      {...listening,badge:'ok'},{...listening,badge:''},{...listening,meter:'bar'},{...listening,meterMs:-1},{...listening,meterMs:20001},{...listening,meterMs:'5'},{...listening,dwellMs:20001},{...listening,dwellMs:1.5},
      {...listening,meterMs:null},{...listening,meter:null},{...listening,meter:'countdown',meterMs:null},{...listening,headline:''},
      view({word:'DONE'}),view({headline:'Done.'}),view({detail:'Nothing was changed.'}),view({badge:'confirmed'}),view({meter:'sweep'})];
    for(const value of rejected)assert.throws(()=>voiceValue(value),undefined,JSON.stringify(value));
    groups++;
  }
  {
    // Without a configured browser key, or without fetch, there is no feed at all.
    for(const options of [{keyed:false},{withFetch:false}]){
      const f=voiceFixture(options);assert.equal(f.feed.available(),false);
      f.feed.start();await f.advance(60000);
      assert.deepEqual([f.calls.length,f.timers.size,f.updates.length,f.feed.running],[0,0,0,false]);
    }
    groups++;
  }
  {
    // since=0 first; since then follows seq; an unchanged seq is not re-announced; a lower seq (Spark restart) is.
    const f=voiceFixture();f.feed.start();f.feed.start();await settle();
    assert.equal(f.calls.length,1,'start is idempotent');assert.equal(f.calls[0].url,VOICE_BASE+'0');
    assert.deepEqual(Object.fromEntries(Object.entries(f.calls[0].init).filter(([name])=>name!=='signal')),{method:'GET',mode:'cors',credentials:'omit',cache:'no-store',redirect:'error',referrerPolicy:'no-referrer'});
    assert.deepEqual(f.delays(),[25000],'the long-poll read outlasts the 20 s server hold');
    assert.deepEqual([f.feed.pending.length,f.service.pending.length],[1,0]);
    f.calls[0].respond(ascii(view({seq:5})));await settle();
    assert.deepEqual(f.updates,[{seq:5,failures:0,replay:true}]);assert.deepEqual(f.delays(),[250]);
    await f.advance(249);assert.equal(f.calls.length,1,'250 ms floor after a valid reply');
    await f.advance(1);assert.equal(f.calls[1].url,VOICE_BASE+'5');
    f.calls[1].respond(ascii(view({seq:5})));await f.advance(250);
    assert.equal(f.updates.length,1,'a held reply with the same seq is not news');assert.equal(f.calls[2].url,VOICE_BASE+'5');
    f.calls[2].respond(ascii(view({seq:7,phase:'pressed',tone:'work',word:'GETTING READY',headline:'One moment...',mood:'front'})));await f.advance(250);
    f.calls[3].respond(ascii(view({seq:2})));await f.advance(250);
    assert.deepEqual(f.updates.slice(1),[{seq:7,failures:0,replay:false},{seq:2,failures:0,replay:false}]);
    assert.equal(f.calls[4].url,VOICE_BASE+'2');
    groups++;
  }
  {
    // Every failure kind counts; backoff is 1, 2, 4, 8, 16 then 30 s; success resets it and is announced.
    const f=voiceFixture();f.feed.start();await settle();
    const failures=[
      call=>call.fail(),
      call=>call.respond(ascii(IDLE),{status:500}),
      call=>call.respond(ascii(IDLE),{url:call.url+'/'}),
      call=>call.respond(ascii({...IDLE,headline:'Café'}).replace('\\u00e9','\u00e9')),
      call=>call.respond(ascii(IDLE).replace('"chip":"ready"','"chip":"ready","pad":"'+'x'.repeat(400)+'"')),
      call=>call.respond('{"v":1,'),
      call=>call.respond(ascii({...IDLE,echo:null})),
      call=>call.respond(ascii(IDLE),{body:null})];
    const expected=[1000,2000,4000,8000,16000,30000,30000,30000];
    for(let i=0;i<failures.length;i++){
      failures[i](f.calls[i]);await settle();
      assert.deepEqual(f.updates[i],{seq:null,failures:i+1,replay:true},'failure '+i);
      assert.deepEqual(f.delays(),[expected[i]],'backoff after failure '+(i+1));
      await f.advance(expected[i]-1);assert.equal(f.calls.length,i+1);
      await f.advance(1);assert.equal(f.calls.length,i+2);assert.equal(f.calls[i+1].url,VOICE_BASE+'0','replay is kept until a valid reply');
    }
    f.calls[8].respond(ascii(view({seq:4})));await settle();
    assert.deepEqual(f.updates[8],{seq:4,failures:0,replay:true});assert.deepEqual(f.delays(),[250]);
    await f.advance(250);await f.advance(25000);
    assert.deepEqual(f.updates[9],{seq:null,failures:1,replay:false},'an unanswered long-poll times out as a failure');
    assert.deepEqual(f.delays(),[1000]);await f.advance(1000);
    f.calls[10].respond(ascii(view({seq:4})));await settle();
    assert.deepEqual(f.updates[10],{seq:4,failures:0,replay:false},'recovery is announced even with an unchanged seq');
    assert.equal(f.calls[10].url,VOICE_BASE+'4');
    groups++;
  }
  {
    // stop() aborts the held request, leaves no timers, ignores late replies, and a restart replays from 0.
    const f=voiceFixture();f.feed.start();await settle();
    f.calls[0].respond(ascii(view({seq:3})));await f.advance(250);
    const held=f.calls[1];f.feed.stop();await settle();
    assert.deepEqual([held.init.signal.aborted,f.feed.pending.length,f.timers.size,f.feed.running],[true,0,0,false]);
    held.respond(ascii(view({seq:4})));await f.advance(120000);
    assert.deepEqual([f.calls.length,f.updates.length],[2,1]);
    f.feed.start();await settle();assert.equal(f.calls[2].url,VOICE_BASE+'0');
    f.calls[2].respond(ascii(view({seq:4})));f.feed.stop();await f.advance(120000);
    assert.deepEqual([f.updates.length,f.timers.size,f.calls.length],[1,0,3],'a reply racing stop() is dropped');
    f.feed.start();await settle();f.calls[3].fail();await settle();
    assert.deepEqual(f.delays(),[1000]);f.feed.stop();
    assert.equal(f.timers.size,0,'stop() also clears a pending backoff');await f.advance(60000);assert.equal(f.calls.length,4);
    groups++;
  }
  {
    // Service.cancelAll (every app refresh and pagehide) never touches the feed; service reads still obey it.
    const f=voiceFixture();f.feed.start();await settle();
    f.service.apps=[{id:'com.webos.app.tinybrowser'}];const status=f.service.browserStatus();await settle();
    assert.equal(f.calls[1].url,'http://{{SPARK_IP}}:4181/'+VOICE_KEY+'/status');
    assert.deepEqual(f.delays(),[2500,25000]);assert.deepEqual([f.service.pending.length,f.feed.pending.length],[1,1]);
    f.service.cancelAll();await settle();
    assert.equal(await status,'offline');
    assert.deepEqual([f.calls[1].init.signal.aborted,f.calls[0].init.signal.aborted,f.feed.pending.length],[true,false,1]);
    f.calls[0].respond(ascii(view({seq:6})));await settle();
    assert.deepEqual(f.updates,[{seq:6,failures:0,replay:true}]);
    groups++;
  }
  console.log(JSON.stringify({groups,liveTvTested:false}));
}
run().catch(error=>{console.error(error);process.exitCode=1;});
