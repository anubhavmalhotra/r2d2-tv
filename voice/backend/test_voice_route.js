'use strict';
const assert = require('node:assert/strict');
const {createVoiceRoute} = require('./voice_route.js');
let groups = 0;
function run(fn) { fn(); groups += 1; }
run(() => {
    const sent=[]; let now=100;
    const route=createVoiceRoute([1001],{qualified:true,send:e=>(sent.push(e),true)},()=>now);
    assert.equal(route.handle(1001,true,false),true);
    assert.equal(route.handle(1001,true,true),true);
    assert.equal(route.handle(1001,true,false),true);
    assert.equal(route.handle(1001,false,false),true);
    assert.deepEqual(sent,['down','up']);
    assert.equal(route.handle(1002,true,false),false); // Existing controls pass through.
});
run(() => {
    for (const owner of [null,{qualified:false,send:()=>{throw Error('unreachable');}},
                         {qualified:true,send:()=>false},{qualified:true,send:()=>{throw Error('unavailable');}}]) {
        const route=createVoiceRoute([1001],owner,()=>100);
        for (const event of [[true,false],[true,true],[false,false],[null,null]]) {
            assert.equal(route.handle(1001,...event),true); // No vendor fallback.
        }
        assert.equal(route.active(),false);
    }
});
run(() => {
    let now=100; const sent=[];
    const route=createVoiceRoute([1001],{qualified:true,send:e=>(sent.push(e),true)},()=>now);
    route.handle(1001,true,false); now+=10000; route.tick();
    assert.deepEqual(sent,['down','cancel']); assert.equal(route.active(),false);
    route.handle(1001,false,false); assert.deepEqual(sent,['down','cancel']);
});
run(() => {
    const sent=[]; const route=createVoiceRoute([1001],{qualified:true,send:e=>(sent.push(e),true)},()=>100);
    route.handle(1001,true,false); route.cancel(); route.close(); route.handle(1001,true,false);
    assert.deepEqual(sent,['down','cancel']); assert.equal(route.active(),false);
});
run(() => {
    const sent=[]; const route=createVoiceRoute([1001,1002],{qualified:true,send:e=>(sent.push(e),true)},()=>100);
    for(let i=0;i<257;i++){route.handle(1001,true,false);route.handle(1001,false,false);}
    assert.equal(sent.length,512); assert.equal(route.handle(1002,true,false),true);
    assert.equal(route.active(),false);
});
run(() => {
    let vendor=0; const sent=[];
    const route=createVoiceRoute([1001],{qualified:false,send:e=>(sent.push(e),true)},()=>100);
    // A minimal source-authored ordering harness: earliest preProcess consumes
    // before boot-popup/default handler/voice conductor could be dispatched.
    function preProcess(key,pressed){if(route.handle(key,pressed,false))return 'accepted';vendor++;return 'continue';}
    assert.equal(preProcess(1001,true),'accepted');assert.equal(preProcess(1001,false),'accepted');
    assert.equal(vendor,0);assert.deepEqual(sent,[]);
});
run(() => {
    const sent=[];
    const route=createVoiceRoute([1001],{qualified:true,send:e=>(sent.push(e),e==='down')},()=>100);
    route.handle(1001,true,false);route.handle(1001,false,false);
    assert.deepEqual(sent,['down','up','cancel']);
    assert.equal(route.stopUnconfirmed(),true);
    // A broken release/cancel link cannot reopen capture or fall back to LG.
    route.handle(1001,true,false);route.handle(1001,false,false);route.tick();
    assert.deepEqual(sent,['down','up','cancel']);
});
console.log(JSON.stringify({passed:true,groups,tvCalls:0,vendorCalls:0,firmwareExecuted:false}));
