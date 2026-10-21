/* Actual generated viewer in Chromium. Media and TV endpoints are local fixtures;
   these checks prove UI/geometry/dispatch, never audio, decoding or device latency. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');
const ROOT = path.resolve(__dirname, '..');
const ORIGIN = 'http://192.0.2.10:4177';
const TOKEN = 'a'.repeat(48);
let groups = 0;

async function fixture(browser, html, options = {}) {
  const context = await browser.newContext({viewport:options.viewport || {width:1920,height:1080}, reducedMotion:options.reducedMotion || 'no-preference'});
  const page = await context.newPage(), errors = [];
  page.on('pageerror', e => errors.push(e.message));
  await page.route('**/*', route => {
    assert.equal(route.request().url(), ORIGIN + '/' + TOKEN + '/viewer', 'unmocked network request');
    return route.fulfill({status:200,contentType:'text/html',body:html});
  });
  await page.addInitScript(({origin,token,voice}) => {
    const base = '/' + token + '/';
    const f = window.uiFixture = {posts:[],requests:[],launches:[],active:'1'.repeat(16),revision:0,serial:2,
      tabs:[{id:'1'.repeat(16),label:'New tab'},{id:'2'.repeat(16),label:'youtube.com'}],fullscreen:false,
      voice:{schema:1,available:voice,state:voice?'idle':'unavailable',request:null}};
    function response(url,value,status=200) {
      const r = new Response(JSON.stringify(value),{status,headers:{'Content-Type':'application/json'}});
      Object.defineProperty(r,'url',{value:origin+url}); return r;
    }
    window.fetch = async (url, opts={}) => {
      if (typeof url !== 'string' || !url.startsWith(base)) throw Error('UNEXPECTED_NETWORK');
      f.requests.push(url); const route=url.slice(base.length), method=opts.method || 'GET';
      if (method==='POST') {
        const value=JSON.parse(opts.body); f.posts.push({route,value});
        if(route==='tabs') {
          if(value.action==='new') { const id=(++f.serial).toString(16).padStart(16,'0');f.tabs.push({id,label:'New tab'});f.active=id; }
          if(value.action==='switch') f.active=value.id;
          if(value.action==='close') { if(f.tabs.length===1)f.tabs[0].label='New tab';else { f.tabs=f.tabs.filter(t=>t.id!==value.id);if(f.active===value.id)f.active=f.tabs[0].id; } }
          f.revision++;
        }
        if(route==='presentation')f.fullscreen=false;
        if(route==='voice') {
          if(!voice)return response(url,{error:'unavailable'},409);
          f.voice.request=value.request;
          f.voice.state=value.action==='start'?'preparing':value.action==='release'?'finishing':'refused';
          return response(url,f.voice,202);
        }
        if(!['tabs','presentation','metrics','navigate','history','input','end'].includes(route))throw Error('UNEXPECTED_MUTATION');
        return response(url,route==='metrics'&&f.metricsReply?f.metricsReply:{},route==='presentation'?202:200);
      }
      if(route==='tabs')return response(url,{tabs:f.tabs,active:f.active,revision:f.revision});
      if(route==='presentation')return response(url,{fullscreen:f.fullscreen,revision:0});
      if(route==='editor')return response(url,{state:'none',kind:null,multiline:null,maxLength:null,focusId:null,fieldId:null,value:null,selectionStart:null,selectionEnd:null,selectionDirection:null,rect:null,revision:0,nextSeq:1,edit:null});
      // A non-qualified service must never result in a recording indicator.
      if(route==='voice') {
        if(f.voice.state==='preparing')f.voice.state='listening';
        else if(f.voice.state==='finishing')f.voice.state='accepted';
        return response(url,f.voice);
      }
      if(route==='stream.mp4') {
        let timer;
        return new Response(new ReadableStream({start(c){c.enqueue(new Uint8Array(64));timer=setInterval(()=>c.enqueue(new Uint8Array(64)),200);},cancel(){clearInterval(timer);}}));
      }
      throw Error('UNEXPECTED_ROUTE');
    };
    const buffered={length:1,start:()=>0,end:()=>10};
    class Buffer extends EventTarget {
      constructor(){super();this.buffered=buffered;this.updating=false;}
      appendBuffer(){this.updating=true;setTimeout(()=>{this.updating=false;this.dispatchEvent(new Event('updateend'));},0);}
      remove(){this.appendBuffer();} abort(){this.updating=false;}
    }
    class Source extends EventTarget {
      constructor(){super();this.readyState='closed';setTimeout(()=>{this.readyState='open';this.dispatchEvent(new Event('sourceopen'));},0);}
      static isTypeSupported(){return true;} addSourceBuffer(){return new Buffer();} endOfStream(){this.readyState='ended';}
    }
    window.MediaSource=Source;
    URL.createObjectURL=()=> 'blob:ui-fixture'; URL.revokeObjectURL=()=>{};
    for(const [name,value] of Object.entries({src:'',readyState:4,videoWidth:3840,videoHeight:2160,currentTime:1,seeking:false,buffered}))
      Object.defineProperty(HTMLMediaElement.prototype,name,{configurable:true,get(){return value;},set(){}});
    Object.defineProperty(HTMLMediaElement.prototype,'paused',{configurable:true,get(){return !this.uiPlaying;}});
    for(const [name,value] of Object.entries({videoWidth:3840,videoHeight:2160}))
      Object.defineProperty(HTMLVideoElement.prototype,name,{configurable:true,get(){return value;}});
    HTMLMediaElement.prototype.play=function(){this.uiPlaying=true;queueMicrotask(()=>this.dispatchEvent(new Event('playing')));return Promise.resolve();};
    HTMLMediaElement.prototype.pause=function(){this.uiPlaying=false;};
    HTMLVideoElement.prototype.requestVideoFrameCallback=undefined;
    HTMLVideoElement.prototype.getVideoPlaybackQuality=()=>({totalVideoFrames:0,droppedVideoFrames:0});
    window.webOSServiceBridge=function(){};
    window.webOSServiceBridge.prototype.call=function(uri,raw){f.launches.push({uri,payload:JSON.parse(raw)});queueMicrotask(()=>this.onservicecallback('{"returnValue":true}'));};
    window.webOSServiceBridge.prototype.cancel=function(){};
    window.close=()=>{f.closed=true;};
  },{origin:ORIGIN,token:TOKEN,voice:options.voice===true});
  await page.goto(ORIGIN+'/'+TOKEN+'/viewer');
  await page.waitForFunction(()=>document.getElementById('state-panel').hidden && document.querySelectorAll('.tab-button').length===2);
  return {context,page,errors};
}

async function back(page) {
  await page.evaluate(()=>{
    document.dispatchEvent(new KeyboardEvent('keydown',{key:'Back',keyCode:461,bubbles:true,cancelable:true}));
    document.dispatchEvent(new KeyboardEvent('keyup',{key:'Back',keyCode:461,bubbles:true,cancelable:true}));
  });
}

// Check hit targets, not just scrollWidth: hidden/clipped tabs can still report
// plausible widths. Long titles and transient status must not displace controls.
async function tabsWithinWindow(page) {
  const geometry=await page.evaluate(()=>{
    const rect=e=>{const r=e.getBoundingClientRect();return {x:r.x,y:r.y,right:r.right,bottom:r.bottom,w:r.width,h:r.height};};
    const list=document.getElementById('tabs-list'),strip=document.getElementById('tabstrip');
    return {width:innerWidth,height:innerHeight,scrollX,scrollY,
      bodyScroll:document.body.scrollLeft,rootScroll:document.documentElement.scrollLeft,
      list:rect(list),strip:rect(strip),toolbar:rect(document.getElementById('toolbar')),
      page:rect(document.getElementById('page')),overflow:list.scrollWidth-list.clientWidth,
      targets:[...strip.querySelectorAll('button')].map(e=>{
        const r=rect(e),hit=document.elementFromPoint(r.x+r.w/2,r.y+r.h/2);
        return {...r,label:e.getAttribute('aria-label')||e.textContent,hit:e===hit||e.contains(hit)};
      })};
  });
  assert.deepEqual([geometry.scrollX,geometry.scrollY,geometry.bodyScroll,geometry.rootScroll],[0,0,0,0]);
  assert(geometry.overflow<=1,JSON.stringify(geometry));
  assert.equal(geometry.toolbar.x,0);assert.equal(geometry.page.x,0);
  assert(geometry.strip.x>=0 && geometry.strip.right<=geometry.width+1);
  for(const t of geometry.targets) {
    assert(t.x>=geometry.strip.x-1 && t.right<=geometry.strip.right+1,JSON.stringify(t));
    assert(t.y>=geometry.toolbar.bottom-1 && t.bottom<=geometry.page.y+1,JSON.stringify(t));
    assert(t.w>=43.5 && t.h>=43.5 && t.hit,JSON.stringify(t));
  }
}

async function run() {
  const temp=fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(),'r2d2-browser-ui-')));
  let browser;
  try {
    const out=path.join(temp,'build');
    execFileSync('python3',['-B','tools/build.py','--config','config/example.json','--output',out],{cwd:ROOT});
    const html=execFileSync('python3',['-B','-c',
      "import sys; from build_viewer import build; sys.stdout.buffer.write(build('a'*48,history_enabled=True,tabs_enabled=True,text_enabled=True,audio_enabled=True,layout_enabled=True,fixed_viewport=True))"],
      {cwd:path.join(out,'spark/runtime'),maxBuffer:2*1024*1024}).toString();
    browser=await chromium.launch({headless:true});
    const {context,page,errors}=await fixture(browser,html);
    for(const width of [1280,1920,3840]) {
      await page.setViewportSize({width,height:width*9/16});
      const geometry=await page.evaluate(()=>{
        const box=id=>{const r=document.getElementById(id).getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom};};
        return {toolbar:box('toolbar'),tabs:box('tabstrip'),page:box('page'),go:box('go'),mic:box('mic'),address:box('address'),
          targets:[...document.querySelectorAll('#toolbar button,#tabstrip button')].filter(e=>!e.hidden).map(e=>{const r=e.getBoundingClientRect();return {id:e.id,w:r.width,h:r.height};}),wordmark:!!document.querySelector('.brand')};
      });
      assert.equal(geometry.wordmark,false);
      assert(geometry.tabs.y>=geometry.toolbar.bottom-1);
      assert(geometry.page.y>=geometry.tabs.bottom-1);
      assert(Math.abs(geometry.page.bottom-width*9/16)<2);
      assert(geometry.go.width<=geometry.go.height+2 && geometry.mic.width<=geometry.mic.height+2);
      assert(geometry.address.width>width*.45);
      for(const t of geometry.targets)assert(t.w>=43.5 && t.h>=43.5,JSON.stringify(t));
    }
    groups++;
    await page.setViewportSize({width:1920,height:1080});
    if(process.argv[2]) {
      const dir=path.resolve(process.argv[2]);fs.mkdirSync(dir,{recursive:true});
      await page.screenshot({path:path.join(dir,'runtime-browser-compact.png'),animations:'disabled'});
    }
    assert.match(await page.locator('#go').getAttribute('aria-label'),/^Go\b/);
    for(const id of ['back','forward','reload','mic','browse-page','end-session'])
      assert(await page.locator('#'+id).getAttribute('aria-label'),id+' needs accessible name');
    const before=await page.locator('#go').boundingBox();await page.locator('#go').focus();
    assert.deepEqual(await page.locator('#go').boundingBox(),before,'focus must not move target');
    groups++;

    await page.locator('#address').click();await page.locator('#address').fill('café & cats');
    assert.equal(await page.evaluate(()=>uiFixture.posts.filter(p=>p.route==='navigate').length),0);
    await page.keyboard.press('Enter');
    await page.waitForFunction(()=>uiFixture.posts.some(p=>p.route==='navigate'));
    assert.equal(await page.evaluate(()=>uiFixture.posts.find(p=>p.route==='navigate').value.url),'https://www.google.com/search?q=caf%C3%A9%20%26%20cats');
    await back(page);assert(await page.locator('#toolbar').isVisible());
    groups++;

    for(let n=2;n<8;n++) {
      await page.locator('#new-tab').click();
      await page.waitForFunction(n=>document.querySelectorAll('.tab-button').length===n,n+1);
      assert.equal(await page.evaluate(()=>document.activeElement.id),'address');
      await tabsWithinWindow(page);await back(page);
    }
    assert(await page.locator('#new-tab').isDisabled());
    await page.evaluate(()=>{uiFixture.tabs.forEach(t=>t.label='a'.repeat(60)+'.com');});
    await page.waitForFunction(()=>document.querySelector('.tab-button').textContent.length===64);
    for(const width of [960,1280,1920,3840]) {
      await page.setViewportSize({width,height:width*9/16});
      await tabsWithinWindow(page);
      if(process.argv[2] && [1280,1920].includes(width)) {
        const dir=path.resolve(process.argv[2]);fs.mkdirSync(dir,{recursive:true});
        await page.screenshot({path:path.join(dir,'eight-tabs-'+width+'.png'),animations:'disabled'});
      }
      // Sequential remote navigation must keep select and close targets visible,
      // including the first tab after creating the eighth.
      await page.locator('.tab-button').first().focus();
      for(let i=0;i<15;i++){await page.keyboard.press('ArrowRight');await tabsWithinWindow(page);}
      for(let i=0;i<15;i++){await page.keyboard.press('ArrowLeft');await tabsWithinWindow(page);}
      assert.equal(await page.evaluate(()=>document.activeElement.dataset.tabId),'1'.repeat(16));
      await page.locator('#mic').click();
      await page.waitForFunction(()=>!document.getElementById('voice-status').hidden);
      await tabsWithinWindow(page);
      await page.evaluate(()=>{document.getElementById('tabs-status').textContent='Updating tabs...';});
      await tabsWithinWindow(page);await back(page);
      await page.evaluate(()=>{document.getElementById('tabs-status').textContent='';});
    }
    await page.setViewportSize({width:1280,height:720});
    await page.locator('.tab-button').last().focus();
    await page.keyboard.press('ArrowUp');assert.equal(await page.evaluate(()=>document.activeElement.id),'address');
    await page.keyboard.press('ArrowDown');assert.equal(await page.evaluate(()=>document.activeElement.className),'tab-button');
    // Closing the first, middle, then active last tab leaves the remainder visible.
    for(const index of [0,3,5]) {
      const count=await page.locator('.tab-close').count();
      await page.locator('.tab-close').nth(index).click();
      await page.waitForFunction(n=>document.querySelectorAll('.tab-button').length===n,count-1);
      await tabsWithinWindow(page);
      assert(await page.locator('#new-tab').isEnabled());
    }
    groups++;

    await page.locator('#browse-page').click();
    assert.equal(await page.locator('#toolbar').isVisible(),false);
    const rect=await page.locator('#page').boundingBox();assert.deepEqual(rect,{x:0,y:0,width:1280,height:720});
    await page.mouse.move(640,360);
    await page.mouse.wheel(0,180);
    await page.waitForFunction(()=>uiFixture.posts.some(p=>p.route==='input'&&p.value.kind==='wheel'));
    await back(page);assert(await page.locator('#toolbar').isVisible());
    await tabsWithinWindow(page);
    groups++;

    await page.locator('#mic').click();
    await page.waitForFunction(()=>!document.getElementById('voice-status').hidden);
    assert(!/^(Listening|Processing)/.test(await page.locator('#voice-label').textContent()),'unqualified mic must not claim capture');
    assert.equal(await page.evaluate(()=>uiFixture.posts.filter(p=>p.route==='voice'&&p.value.action==='start').length),0);
    await back(page);
    groups++;

    if(process.argv[2]) {
      const dir=path.resolve(process.argv[2]);fs.mkdirSync(dir,{recursive:true});
      await page.setViewportSize({width:1920,height:1080});
      await page.screenshot({path:path.join(dir,'runtime-browser-tabs-stress.png'),animations:'disabled'});
    }
    await page.locator('#end-session').click();
    await page.waitForFunction(()=>uiFixture.launches.length===1);
    assert.equal(await page.evaluate(()=>uiFixture.posts.filter(p=>p.route==='end').length),1);
    assert.deepEqual(await page.evaluate(()=>uiFixture.launches[0].payload),{id:'local.owner.applibrary'});
    assert.deepEqual(errors,[]);groups++;
    await context.close();

    // Voice receipt card: top-center in #page over the notice slot, inert to
    // pointer and focus, no layout change. Synthetic receipt text only.
    const voiceCard={v:1,seq:5,phase:'result',ageMs:0,tone:'sent',word:'SENT',headline:'Searching YouTube...',detail:'',badge:'sent',meter:null,meterMs:null,dwellMs:20000,mood:'verified',echo:'lo-fi music'};
    const receipt=await fixture(browser,html);
    const pageBefore=await receipt.page.evaluate(()=>{const r=document.getElementById('page').getBoundingClientRect();return {page:[r.x,r.y,r.width,r.height],focus:document.activeElement.id};});
    await receipt.page.evaluate(card=>{uiFixture.metricsReply={ok:true,voice:card};},voiceCard);
    await receipt.page.waitForFunction(()=>!document.getElementById('voice-card').hidden);
    // Measure the settled card, not its 180 ms entrance.
    await receipt.page.evaluate(()=>Promise.all(document.getElementById('voice-card').getAnimations().map(a=>a.finished)));
    const cardGeometry=()=>receipt.page.evaluate(()=>{
      const card=document.getElementById('voice-card'),r=card.getBoundingClientRect(),p=document.getElementById('page').getBoundingClientRect(),s=getComputedStyle(card);
      const hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2),disc=card.querySelector('.voice-disc').getBoundingClientRect();
      const glyphs=[...card.querySelectorAll('svg')].filter(e=>getComputedStyle(e).display!=='none').map(e=>e.dataset.glyph);
      return {x:r.x,y:r.y,w:r.width,h:r.height,page:[p.x,p.y,p.width,p.height],width:innerWidth,scroll:[scrollX,scrollY,document.documentElement.scrollLeft],
        hit:hit&&hit.id,z:s.zIndex,pointer:s.pointerEvents,animation:s.animationName,font:s.fontFamily,disc:[disc.width,disc.height],glyphs,
        title:card.querySelector('#voice-card-title').textContent,detail:card.querySelector('#voice-card-detail').textContent,focus:document.activeElement.id,
        focusable:card.querySelectorAll('[tabindex],button,input,a').length};
    });
    for(const width of [1280,1920,3840]) {
      await receipt.page.setViewportSize({width,height:width*9/16});
      for(const mode of ['compact','page']) {
        if(mode==='page')await receipt.page.locator('#browse-page').click();
        const g=await cardGeometry();
        assert.deepEqual(g.scroll,[0,0,0],JSON.stringify(g));
        assert(Math.abs(g.y-(mode==='page'?54:g.page[1]+18))<=1,JSON.stringify(g));
        assert(Math.abs(g.x+g.w/2-g.width/2)<=1 && g.w>=Math.min(560,g.width-32)-1 && g.w<=960.5,JSON.stringify(g));
        assert.equal(g.h,156);assert.equal(g.z,'6');assert.equal(g.pointer,'none');assert.equal(g.hit,'video');assert.equal(g.focusable,0);
        assert.deepEqual(g.disc,[56,56]);assert.deepEqual(g.glyphs,['arrow','arrow']);assert.match(g.font,/^Arial/);assert.equal(g.animation,'voice-in');
        assert.equal(g.title,'Searching YouTube...');assert.equal(g.detail,'“lo-fi music”');
        if(width===1920&&mode==='compact')assert(Math.abs(g.y-162)<=4,JSON.stringify(g));
        if(process.argv[2]&&width===1920) {
          const dir=path.resolve(process.argv[2]);fs.mkdirSync(dir,{recursive:true});
          await receipt.page.screenshot({path:path.join(dir,'voice-card-'+mode+'.png'),animations:'disabled'});
        }
        if(mode==='page'){await back(receipt.page);assert(await receipt.page.locator('#toolbar').isVisible());}
      }
    }
    await receipt.page.setViewportSize({width:1920,height:1080});
    const pageAfter=await receipt.page.evaluate(()=>{const r=document.getElementById('page').getBoundingClientRect();return {page:[r.x,r.y,r.width,r.height]};});
    assert.deepEqual(pageAfter.page,pageBefore.page,'the card must not move the page');
    await receipt.page.mouse.click(960,240);
    await receipt.page.waitForFunction(()=>uiFixture.posts.some(p=>p.route==='input'&&p.value.click===true));
    await receipt.page.evaluate(()=>{uiFixture.metricsReply={ok:true,voice:null};});
    assert(!(await receipt.page.evaluate(()=>JSON.stringify(uiFixture.posts))).includes('lo-fi'));
    assert.deepEqual(receipt.errors,[]);groups++;
    await receipt.context.close();

    const reduced=await fixture(browser,html,{reducedMotion:'reduce'});
    await reduced.page.locator('#go').focus();
    assert(await reduced.page.locator('#go').evaluate(e=>getComputedStyle(e).transitionDuration.split(',').every(v=>parseFloat(v)===0)));
    await reduced.page.evaluate(card=>{uiFixture.metricsReply={ok:true,voice:card};},voiceCard);
    await reduced.page.waitForFunction(()=>!document.getElementById('voice-card').hidden);
    assert.equal(await reduced.page.locator('#voice-card').evaluate(e=>getComputedStyle(e).animationName),'none');
    assert.deepEqual(reduced.errors,[]);groups++;
    await reduced.context.close();

    const capture=await fixture(browser,html,{voice:true});
    await capture.page.locator('#mic').click();
    await capture.page.waitForFunction(()=>document.getElementById('mic').getAttribute('aria-pressed')==='true');
    assert.match(await capture.page.locator('#voice-label').textContent(),/^Listening/);
    await capture.page.locator('#mic').click();
    await capture.page.waitForFunction(()=>document.getElementById('voice-label').textContent==='Voice command accepted.');
    const actions=await capture.page.evaluate(()=>uiFixture.posts.filter(p=>p.route==='voice').map(p=>p.value));
    assert.deepEqual(actions.map(v=>v.action),['start','release']);
    assert(actions.every(v=>v.schema===1&&v.request===actions[0].request));
    await capture.page.locator('#mic').click();
    await capture.page.waitForFunction(()=>document.getElementById('mic').getAttribute('aria-pressed')==='true');
    await back(capture.page);
    await capture.page.waitForFunction(()=>uiFixture.posts.some(p=>p.route==='voice'&&p.value.action==='cancel'));
    assert(await capture.page.locator('#voice-status').isHidden());
    assert.deepEqual(capture.errors,[]);groups++;
    await capture.context.close();
    console.log(JSON.stringify({groups,actualChromium:true,mediaSimulated:true,externalRequests:0,deviceCalls:0}));
  } finally {if(browser)await browser.close();fs.rmSync(temp,{recursive:true,force:true});}
}
run().catch(error=>{console.error(error);process.exitCode=1;});
