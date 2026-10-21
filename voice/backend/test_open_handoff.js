// Execute the actual rendered /open script with synthetic manager responses.
// No browser, server, TV or network request is created.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../spark/open.js.in'), 'utf8')
  .replaceAll('{{SPARK_IP}}', '192.0.2.10').replaceAll('{{SPARK_IP_REGEX}}', '192\\.0\\.2\\.10');
const key='c'.repeat(64), sid='a'.repeat(32), viewer='http://192.0.2.10:4177/'+'d'.repeat(48)+'/viewer';
function harness(hash='', response) {
  const requests=[], events={}, timers=[], replaced=[], history=[], status={textContent:''};
  const location={pathname:'/'+key+'/open',origin:'http://192.0.2.10:4181',hash,
    replace(url){replaced.push(url);}};
  const context={location, document:{hidden:false,getElementById:()=>status,addEventListener:(n,f)=>events[n]=f},
    addEventListener:(n,f)=>events[n]=f,
    history:{replaceState(a,b,c){history.push(c);location.hash='';}},
    AbortController, Date, setTimeout(f,n){const item={f,n};timers.push(item);return item;},
    clearTimeout(item){item.cleared=true;},
    fetch(url,opts){
      const name=url.split('/').pop(), body=opts.body?JSON.parse(opts.body):null;
      requests.push({name,body,opts,hash:location.hash});
      const pair=response?response(name,body,requests):
        (name==='start'?[202,{state:'starting',id:sid}]:name==='claim'?[200,{state:'ready',url:viewer}]:[200,{state:'starting'}]);
      return Promise.resolve({status:pair[0],text:()=>Promise.resolve(JSON.stringify(pair[1]))});
    }};
  vm.runInNewContext(source,context);
  return {requests,events,timers,replaced,history,status,context};
}
async function drain(){for(let i=0;i<12;i++) await new Promise(setImmediate);}
(async()=>{
  let h=harness();await drain();
  assert.deepEqual(h.requests.map(x=>x.name),['start','lease','claim']);
  assert.deepEqual(h.replaced,[viewer]);assert.deepEqual(h.history,[]);
  h.events.pagehide();await drain();assert.equal(h.requests.length,3);
  h=harness('#voice-session='+sid);await drain();
  assert.deepEqual(h.requests.map(x=>x.name),['lease','claim']);assert.deepEqual(h.history,['/'+key+'/open']);
  assert(h.requests.every(x=>x.body.id===sid&&x.hash===''&&x.opts.referrerPolicy==='no-referrer'));
  assert.deepEqual(h.replaced,[viewer]);
  for(const invalid of ['#voice-session=bad','#voice-session='+sid+'&other=1','#x='+sid]){
    h=harness(invalid);await drain();assert.deepEqual(h.requests,[]);assert.deepEqual(h.history,['/'+key+'/open']);
    assert.equal(h.replaced.length,0);assert.match(h.status.textContent,/could not start/);
  }
  h=harness('#voice-session='+sid,(name)=>[409,{}]);await drain();
  assert.deepEqual(h.requests.map(x=>x.name),['lease','stop']);assert(h.requests.every(x=>x.body.id===sid));
  assert.equal(h.replaced.length,0);
  h=harness('#voice-session='+sid,()=>[202,{state:'starting'}]);await drain();
  h.events.pagehide();await drain();
  assert.equal(h.requests.filter(x=>x.name==='stop').length,1);assert(!h.requests.some(x=>x.name==='start'));
  for(const t of h.timers.filter(x=>!x.cleared&&x.n===500))t.f();await drain();
  assert.equal(h.requests.filter(x=>x.name==='claim').length,1);
  h=harness('',name=>name==='start'?[409,{state:'ready'}]:[200,{state:'ready'}]);await drain();
  assert.deepEqual(h.requests.map(x=>x.name),['start','status']);assert(h.timers.some(x=>x.n===750));
  console.log('7 open-page handoff groups passed');
})().catch(e=>{console.error(e);process.exitCode=1;});
